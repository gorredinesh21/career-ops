"""Candidate Intelligence v1 — deterministic resume parsing (BRD Module C lite).

Parses plain resume text into sections, matches skills using the SAME skill
dictionary as job postings (so profile and job sides speak one ontology), and
attaches every skill claim to its evidence line. No LLM, no invention: a skill
appears only when the text actually contains it, with the sentence that proves
it stored next to the claim.
"""
import re

from app.classify import SKILLS
from app.normalize import collapse

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


# ---------------------------------------------------------------- education


DEGREE_PATTERNS = [
    r"\b(?:b\.?\s?tech|b\.?\s?e\.?\b|bachelor(?:'s)?\s+of\s+(?:technology|engineering)|dual\s+degree)\b",
    r"\b(?:m\.?\s?tech|m\.?\s?e\.?\b|m\.?\s?s\.?\b|master(?:'s)?\s+of\s+(?:science|technology|engineering)|mba)\b",
    r"\b(?:ph\.?\s?d\.?\b|doctorate)\b",
    r"\b(?:b\.?\s?sc|bca|b\.?com|bba)\b",
    r"\b(?:m\.?\s?sc|mca)\b",
]
DEGREE_NAMES = ["B.Tech/BE", "M.Tech/MS/MBA", "PhD", "BSc/BCA/BCom", "MSc/MCA"]

INSTITUTE_TIERS = [
    (r"indian\s+institute\s+of\s+technology|\biit\b|ism\s+dhanbad", "IIT / ISM (national institute)"),
    (r"national\s+institute\s+of\s+technology|\bnit\b|iiit", "NIT / IIIT"),
    (r"university|college|institute|school\s+of", "University / college"),
]


def parse_education(text: str) -> list:
    """Extract (degree, institution, years, classification) from the education
    section. Deterministic; anything unparsed stays out rather than guessed."""
    out, seen = [], set()
    section = []
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if len(line) < 60 and re.search(r"^education\b|^(academic|academics)\b", line, re.I):
            current = "edu"
            continue
        if current == "edu":
            if len(line) < 60 and re.search(r"^(skills|experience|projects|certifications?)\b", line, re.I):
                current = None
                continue
            section.append(line)
    # First pass: one entry per line that carries a degree or an institute.
    entries = []
    for line in section:
        degree = next((name for i, pat in enumerate(DEGREE_PATTERNS)
                       if re.search(pat, line, re.I) and (name := DEGREE_NAMES[i]) is not None), None)
        institute = ""
        m = re.search(r"([A-Z][A-Za-z&.,'\- ]{6,70}(?:Institute|University|College|School|IIT|ISM|NIT|IIIT)[A-Za-z&.,'\- ]*)", line)
        if m:
            institute = collapse(m.group(1))
            # the greedy capture can swallow the degree phrase before the
            # institute name ("B.Tech in CSE, IIT Dhanbad") — drop leading
            # segments that are degree/major text, keep the rest
            parts = [p.strip() for p in institute.split(",") if p.strip()]
            kept = []
            for p in parts:
                if kept or not (
                    any(re.search(pat, p, re.I) for pat in DEGREE_PATTERNS)
                    or re.match(r"^(in|of)\b", p, re.I)
                ):
                    kept.append(p)
            institute = ", ".join(kept) if kept else institute
            # institute lines often glue the dates on: "IIT (ISM), DhanbadDec 2021 – May 2025"
            glue = re.search(r"([A-Za-z])(?:Dec|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov)\s*20\d{2}", institute)
            if glue:
                institute = institute[:glue.start() + 1]
        years = ""
        ym = re.search(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*?(20\d{2})\s*(?:-|–|to)\s*(?:[a-z]*\.?\s*?)?(20\d{2}|present|ongoing)"
                       r"|(20\d{2})\s*(?:-|–|to)\s*(?:[a-z]*\.?\s*?)?(20\d{2}|present|ongoing)", line, re.I)
        if ym:
            years = ym.group(0)
        tier = "Other"
        for pat, label in INSTITUTE_TIERS:
            if re.search(pat, line, re.I):
                tier = label
                break
        if degree or institute:
            entries.append({"degree": degree, "institution": institute,
                            "years": years, "classification": tier, "line": line[:160]})

    # Second pass: resumes often split the pair across two lines ("Indian
    # Institute of Technology (ISM), Dhanbad" then "Bachelor of Technology
    # in CSE ..."). Merge an institute-only entry with the adjacent
    # degree-only entry, in either order.
    merged = []
    i = 0
    while i < len(entries):
        e = entries[i]
        nxt = entries[i + 1] if i + 1 < len(entries) else None
        if e["degree"] and not e["institution"] and nxt and nxt["institution"] and not nxt["degree"]:
            e["institution"], e["classification"] = nxt["institution"], nxt["classification"]
            e["years"] = e["years"] or nxt["years"]
            i += 2
        elif not e["degree"] and e["institution"] and nxt and nxt["degree"] and not nxt["institution"]:
            nxt["institution"], nxt["classification"] = e["institution"], e["classification"]
            nxt["years"] = nxt["years"] or e["years"]
            i += 2
            e = nxt
        else:
            i += 1
        merged.append(e)

    out, seen = [], set()
    for e in merged:
        key = (e["degree"] or "", e["institution"])
        if e["degree"] and key not in seen:
            seen.add(key)
            out.append(e)
    return out


def skill_recency(claims: dict) -> dict:
    """Latest year mentioned near each skill's evidence lines (honest proxy:
    empty when no year appears — we do not fabricate recency)."""
    recency = {}
    for skill, items in claims.items():
        years = []
        for c in items:
            for y in re.findall(r"\b(20\d{2})\b", c.get("evidence", "")):
                years.append(int(y))
        if years:
            recency[skill] = max(years)
    return recency


# ---------------------------------------------------------------- skill ontology


# Parent links let a specific skill count as *indirect evidence* for a broad
# requirement (PyTorch ⇒ Deep Learning; FastAPI ⇒ Python). Indirect evidence is
# always labeled as such — never silently treated as direct proof.
SKILL_PARENTS = {
    "PyTorch": "Deep Learning", "TensorFlow": "Deep Learning",
    "scikit-learn": "Machine Learning", "Computer Vision": "Deep Learning",
    "NLP": "Machine Learning", "Deep Learning": "Machine Learning",
    "RAG": "LLMs", "LangChain": "LLMs", "LlamaIndex": "LLMs",
    "Prompt Engineering": "LLMs", "Agents": "LLMs", "OpenAI": "LLMs",
    "Gemini": "LLMs", "Hugging Face": "Machine Learning",
    "Django": "Python", "Flask": "Python", "FastAPI": "Python",
    "Spring Boot": "Java", "React": "JavaScript", "Next.js": "JavaScript",
    "Vue": "JavaScript", "Angular": "JavaScript",
    "Spark": "Data Engineering", "Airflow": "Data Engineering", "dbt": "Data Engineering",
    "Kafka": "Data Engineering", "Vector DBs": "RAG",
    "PostgreSQL": "SQL", "MySQL": "SQL", "BigQuery": "SQL", "Snowflake": "SQL",
    "Databricks": "Data Engineering", "Kubernetes": "Docker",
    "PySpark": "Spark", "Machine Learning": "AI & ML (broad)",
    "DPDK": "C++", "Embedded": "C++",
}


def indirect_evidence_for(skill: str, owned_skills: set) -> str:
    """Return the owned child skill that indirectly supports a broad missing
    requirement, or ''. Walks one level up the ontology only."""
    for child, parent in SKILL_PARENTS.items():
        if parent.lower() == skill.lower() and child in owned_skills:
            return child
    return ""


# ---------------------------------------------------------------- portfolio


def fetch_portfolio(url: str, timeout: int = 10) -> dict:
    """Best-effort fetch of a public portfolio page: extract title + skills
    mentioned in visible text. Failures are returned, not swallowed."""
    import requests as _requests

    if not re.match(r"^https?://", url):
        raise ValueError("Portfolio URL must start with http:// or https://")
    r = _requests.get(url, timeout=timeout, headers={"User-Agent": "CareerOps/1.0"},
                      allow_redirects=True)
    r.raise_for_status()
    html = r.text[:400_000]
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = collapse(re.sub(r"<[^>]+>", "", title_m.group(1))) if title_m else ""
    text = collapse(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html, flags=re.S | re.I))
    found = [name for name, frag in SKILLS.items() if re.search(frag, text.lower())]
    return {"title": title[:160] or url, "skills": sorted(found), "text_length": len(text)}
