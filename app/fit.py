"""Matching + Gap Engine v2 (BRD Module F) — the decision-support core.

Design rules, all enforced here:
- No single pseudo-precise score. A fit band + visible components.
- Every requirement row is evidence-backed: direct, indirect (via skill
  ontology, labeled), weak, or missing — never silently equivalent.
- Gaps are TYPED: capability/evidence (unknowable from text — the user review
  loop resolves it), presentation, scope, preference mismatch.
- Market commonality: how often each skill is demanded across the family's
  live postings — common gaps are prioritized over rare asks.
"""
from app.resume import SKILL_PARENTS, indirect_evidence_for

DEPTHS_STRONG = {"production use", "independent build", "repo evidence"}


def _profile_index(profile_skills_rows, reviews=None):
    """Best claim per skill + set of strong/listed skills + user verdicts."""
    best, listed, strong = {}, set(), set()
    for s in profile_skills_rows:
        name = s["skill"]
        cur = best.get(name)
        if cur is None or (s["confidence"] or 0) > (cur["confidence"] or 0):
            best[name] = s
        if s["depth"] == "listed in skills section":
            listed.add(name)
        if s["depth"] in DEPTHS_STRONG:
            strong.add(name)
    rejected = {r["skill"] for r in (reviews or []) if r["verdict"] == "rejected"}
    confirmed = {r["skill"] for r in (reviews or []) if r["verdict"] == "confirmed"}
    return best, listed, strong, rejected, confirmed


def market_commonality(conn, job) -> dict:
    """Share of this job's family's active postings demanding each skill."""
    fam_id = job["role_family_id"]
    out = {}
    if not fam_id:
        return out
    total = conn.execute(
        "SELECT COUNT(DISTINCT id) AS n FROM job "
        "WHERE role_family_id = ? AND freshness IN ('active','likely_active')",
        (fam_id,),
    ).fetchone()["n"] or 1
    rows = conn.execute(
        "SELECT js.skill, COUNT(DISTINCT js.job_id) AS n FROM job_skill js "
        "JOIN job j ON j.id = js.job_id "
        "WHERE j.role_family_id = ? AND j.freshness IN ('active','likely_active') "
        "GROUP BY js.skill",
        (fam_id,),
    ).fetchall()
    for r in rows:
        out[r["skill"]] = round(100 * r["n"] / total)
    return out


def band_of(job) -> str:
    lo, hi = job["exp_min"], job["exp_max"]
    if lo is None and hi is None:
        return "not stated"
    if lo is not None and hi is not None:
        return f"{lo:g}–{hi:g} yrs"
    if lo is not None:
        return f"{lo:g}+ yrs"
    return f"≤{hi:g} yrs"


def evaluate(conn, job, profile_skills_rows, reviews=None, user_band=None,
             user_prefs=None) -> dict:
    """Full evidence-based fit evaluation for one job vs one profile."""
    job_skills = conn.execute(
        "SELECT skill, requirement FROM job_skill WHERE job_id = ? ORDER BY requirement, skill",
        (job["id"],),
    ).fetchall()
    best, listed, strong, rejected, confirmed = _profile_index(profile_skills_rows, reviews)
    owned = set(best) - rejected
    commonality = market_commonality(conn, job)

    rows = []
    for r in job_skills:
        skill = r["skill"]
        req = r["requirement"]
        entry = {"skill": skill, "requirement": req, "status": "missing",
                 "depth": "", "evidence": "", "indirect": "", "market": commonality.get(skill)}
        hit = best.get(skill)
        if skill in confirmed and hit is None:
            entry.update(status="match", depth="user-confirmed", evidence="You confirmed this skill in review.")
        elif hit is not None and skill not in rejected:
            strong_hit = skill in strong
            entry.update(status="match" if strong_hit else "weak",
                         depth=hit["depth"],
                         evidence=(hit["evidence"] or "")[:200])
        else:
            child = indirect_evidence_for(skill, owned)
            if child:
                entry.update(status="indirect", depth=f"via {child}",
                             indirect=child,
                             evidence=(best.get(child, {}).get("evidence") or "")[:160])
        rows.append(entry)

    required = [r for r in rows if r["requirement"] == "required"] or rows
    matched = sum(1 for r in required if r["status"] == "match")
    indirect = sum(1 for r in required if r["status"] == "indirect")
    weak = sum(1 for r in required if r["status"] == "weak")

    ratio = matched / len(required) if required else 0
    if ratio >= 0.75:
        band = "Strong Evidence"
    elif ratio >= 0.5:
        band = "Good Evidence"
    elif matched + indirect + weak >= max(1, len(required) // 2):
        band = "Partial Evidence"
    else:
        band = "Major Gap"

    # Experience band alignment (context, not a verdict).
    alignment = "unknown — neither side states experience cleanly"
    if user_band and job["exp_stated"]:
        try:
            lo = job["exp_min"] if job["exp_min"] is not None else 0
            hi = job["exp_max"] if job["exp_max"] is not None else 99
            ulo, uhi = user_band
            if ulo >= lo and uhi <= hi + 1:
                alignment = "aligned — your band matches the stated range"
            elif ulo < lo:
                alignment = f"stretch — posting asks {band_of(job)}, you are at {ulo:g}–{uhi:g} yrs"
            else:
                alignment = "above — you are past the stated band (seniority/comp check)"
        except (TypeError, ValueError):
            pass

    # Preference mismatches (BRD gap type: preference mismatch — inform, don't fix).
    prefs = []
    if user_prefs:
        job_wm = job["work_model"] or ""
        wanted = set(user_prefs.get("work_models") or [])
        if wanted and job_wm and job_wm not in wanted and job_wm != "unspecified":
            prefs.append(f"work model: job is {job_wm}, you wanted {'/'.join(sorted(wanted))}")
        locs = [l.lower() for l in (user_prefs.get("locations") or [])]
        if locs:
            jc = (job["city"] or "").lower()
            if jc and not any(l in jc or jc in l for l in locs):
                prefs.append(f"location: job is in {job['city']}, you wanted {'/'.join(locs)}")

    return {
        "rows": rows,
        "band": band,
        "why": (f"{matched}/{len(required)} required skills directly evidenced"
                + (f", {indirect} more via related skills" if indirect else "")
                + (f", {weak} at weak/exposure level" if weak else "")),
        "alignment": alignment,
        "preference_mismatches": prefs,
        "counts": {"required": len(required), "matched": matched,
                   "indirect": indirect, "weak": weak},
    }


def typed_gaps(evaluation: dict, profile_skills_rows) -> list:
    """Ordered, typed, actionable gaps (BRD gap philosophy)."""
    _, listed, _, _, _ = _profile_index(profile_skills_rows)
    gaps = []
    for r in evaluation["rows"]:
        if r["status"] in ("match",):
            continue
        skill, market = r["skill"], r["market"]
        market_note = ""
        if market is not None:
            market_note = (f" asked by {market}% of live postings in this family"
                           if market >= 30 else f" a rare ask ({market}% of postings)")
        if r["status"] == "indirect":
            gaps.append((skill, f"indirect evidence only ({r['depth']})",
                         f"You have {r['indirect']} evidence; the posting names {skill} "
                         f"directly. Add one truthful line naming {skill} in context if real." + market_note))
        elif r["status"] == "weak":
            gaps.append((skill, "presentation / depth gap",
                         f"Evidence is '{r['depth']}' level. Rewrite the line with an outcome, "
                         "or build one real project." + market_note))
        elif skill in listed:
            gaps.append((skill, "presentation gap",
                         f"'{skill}' is only listed in your skills section — move it into a "
                         "project or work bullet with an outcome." + market_note))
        else:
            gaps.append((skill, "evidence gap or capability gap — only you know which",
                         f"No '{skill}' evidence in your profile. If you have used it, add the "
                         "truthful line; if not, a small real project beats a keyword." + market_note))
    gaps.sort(key=lambda g: -_market_of(g))
    return gaps[:8]


def _market_of(gap) -> int:
    m = gap[2].split("asked by ")[-1].split("%")[0] if "asked by " in gap[2] else "0"
    try:
        return int(m)
    except ValueError:
        return 0


def capability_readiness(conn, job_id: int, profile_lines: list) -> list:
    """Module E signals for the job + coarse profile readiness per signal."""
    from app.jobexpect import CAPABILITY_MEANING, profile_readiness

    caps = conn.execute(
        "SELECT capability, evidence FROM job_capability WHERE job_id = ?", (job_id,)
    ).fetchall()
    ready = profile_readiness({c["capability"]: 1 for c in caps}, profile_lines)
    out = []
    for c in caps:
        out.append({
            "capability": c["capability"],
            "meaning": CAPABILITY_MEANING.get(c["capability"], ""),
            "evidence": c["evidence"],
            "you_have_evidence": ready.get(c["capability"], False),
        })
    return out
