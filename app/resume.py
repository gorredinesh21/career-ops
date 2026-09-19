"""Candidate Intelligence v1 — deterministic resume parsing (BRD Module C lite).

Parses plain resume text into sections, matches skills using the SAME skill
dictionary as job postings (so profile and job sides speak one ontology), and
attaches every skill claim to its evidence line. No LLM, no invention: a skill
appears only when the text actually contains it, with the sentence that proves
it stored next to the claim.
"""
import re

from app.classify import SKILLS

SECTION_HEADERS = [
    (r"^(work\s+|professional\s+)?experience\b", "experience"),
    (r"^employment\b|^career\s+history\b", "experience"),
    (r"^education\b|^(academic|academics)\b", "education"),
    (r"^(technical\s+)?skills\b|^tech\s+stack\b|^technologies\b", "skills"),
    (r"^projects\b|^key\s+projects\b|^personal\s+projects\b", "projects"),
    (r"^certifications?\b|^achievements?\b|^awards?\b|^publications?\b", "credentials"),
    (r"^summary\b|^objective\b|^profile\b|^about\b", "summary"),
]

DEPTH_CUES = [
    (0.85, "production use", r"produc\w*|deploy\w*|shipped|live\s+at|maintenance|owned|monitor\w*|on-call|at\s+scale|\d+\s*(k|K|\+)?\s*users|serving\s+\d+|sla|uptime"),
    (0.75, "independent build", r"\b(built|developed|created|implemented|designed|engineered|architected|integrated|migrated|automated)\b"),
    (0.65, "working use", r"\b(used|using|worked\s+on|worked\s+with|applied|wrote)\b"),
    (0.35, "exposure", r"familiar\w*|basic|learning|course|tutorial|studied|exposure|beginner|intro"),
]
DEPTH_FALLBACK = (0.5, "mentioned")


def split_sections(text: str) -> list:
    """Return [(section, line)] with a best-effort section for every
    non-empty line. Headers are detected, everything between two headers
    belongs to the earlier one."""
    out = []
    current = "header"
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        matched = None
        if len(line) < 60:  # headers are short; long lines are content
            for pat, name in SECTION_HEADERS:
                if re.search(pat, line, flags=re.IGNORECASE):
                    matched = name
                    break
        if matched:
            current = matched
            continue  # the header line itself carries no evidence
        out.append((current, line))
    return out


def assess_depth(line: str):
    low = line.lower()
    for confidence, label, pat in DEPTH_CUES:
        if re.search(pat, low):
            return confidence, label
    return DEPTH_FALLBACK


def parse_resume(text: str) -> dict:
    """Extract {skill: [{section, evidence, depth, confidence}]} from text."""
    claims = {}
    sections = split_sections(text)
    for section, line in sections:
        low = line.lower()
        for name, frag in SKILLS.items():
            if re.search(frag, low):
                if section == "skills":
                    conf, depth = 0.55, "listed in skills section"
                else:
                    conf, depth = assess_depth(line)
                claims.setdefault(name, []).append({
                    "section": section,
                    "evidence": line[:220],
                    "depth": depth,
                    "confidence": conf,
                })
    # Keep the strongest claim per skill per section (a long resume repeats
    # the same tech across bullets; we keep the best evidence).
    for name, items in claims.items():
        items.sort(key=lambda c: -c["confidence"])
        seen, best = set(), []
        for c in items:
            if c["section"] in seen:
                continue
            seen.add(c["section"])
            best.append(c)
        claims[name] = best
    return claims


DEPTH_ORDER = {
    "production use": 5, "independent build": 4, "working use": 3,
    "listed in skills section": 2, "mentioned": 1, "exposure": 0,
}


def best_depth(items) -> tuple:
    top = max(items, key=lambda c: DEPTH_ORDER.get(c["depth"], 0))
    return top["depth"], top["confidence"], top["evidence"]


def extract_pdf_text(path_or_bytes) -> str:
    from pypdf import PdfReader
    import io
    if isinstance(path_or_bytes, (bytes, bytearray)):
        reader = PdfReader(io.BytesIO(path_or_bytes))
    else:
        reader = PdfReader(str(path_or_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def github_repos_for(username: str, timeout: int = 8) -> dict:
    """Fetch public repos from the GitHub REST API (no auth: 60 req/hr).
    Returns {'repos': [...]} or raises with a human-readable message."""
    import requests

    url = f"https://api.github.com/users/{username}/repos"
    r = requests.get(url, params={"per_page": 100, "sort": "pushed"}, timeout=timeout,
                     headers={"Accept": "application/vnd.github+json"})
    if r.status_code == 404:
        raise ValueError(f"GitHub user '{username}' not found")
    if r.status_code == 403:
        raise RuntimeError("GitHub API rate limit reached (unauthenticated) — try again in an hour")
    r.raise_for_status()
    repos = []
    for it in r.json():
        repos.append({
            "name": it.get("name", ""),
            "description": it.get("description") or "",
            "language": it.get("language") or "",
            "stars": it.get("stargazers_count", 0),
            "pushed_at": (it.get("pushed_at") or "")[:10],
            "url": it.get("html_url", ""),
            "topics": it.get("topics", []) or [],
            "fork": bool(it.get("fork")),
        })
    return {"repos": repos}


def skills_from_repos(repos: list) -> dict:
    """Link skills to repository evidence: a repo backs a skill when its
    name/description/topics/language mention it. Stars and dates are context,
    never treated as proficiency (BRD Module 8.2)."""
    claims = {}
    for repo in repos:
        text = " ".join([repo["name"], repo["description"], " ".join(repo["topics"]),
                         repo["language"]]).lower()
        for name, frag in SKILLS.items():
            if re.search(frag, text):
                claims.setdefault(name, []).append({
                    "evidence": f"{repo['url']} — {repo['language'] or 'code'}"
                                f"{' · ★' + str(repo['stars']) if repo['stars'] else ''}",
                    "depth": "repo evidence",
                    "confidence": 0.6,
                })
    return claims
