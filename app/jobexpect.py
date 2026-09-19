"""Job-level expectations (BRD Module E): seniority and scope signals
extracted from the posting text with evidence snippets — the layer that
separates 'keywords match' from 'this role expects real ownership'."""
import re

CAPABILITY_CUES = {
    "ownership": r"\b(own|ownership|owns|end[- ]to[- ]end|from\s+scratch|greenfield)\b",
    "design": r"\b(architect(?:ing)?|system\s+design|design\s+(?:scalable|large|distributed)|trade[- ]offs?|technical\s+decisions)\b",
    "scale": r"\b(scal(?:e|ing)|millions|billions|high\s+(?:traffic|throughput|volume)|low\s+latency|concurrent|petabyte|terabyte)\b",
    "production": r"\b(production|on[- ]call|incident|reliability|uptime|\bsla\b|observability|monitoring)\b",
    "mentorship": r"\b(mentor(?:ing)?|coach|guide\s+(?:junior|new)|lead(?:ing)?\s+(?:a\s+)?(?:team|junior|engineers)|interns)\b",
    "cross_team": r"\b(cross[- ]functional|stakeholders?|across\s+teams|partner\s+with|work\s+with\s+(?:product|business|leadership|customers?))\b",
}

# What each signal implies, in plain language for the job page.
CAPABILITY_MEANING = {
    "ownership": "expects end-to-end feature ownership, not task execution",
    "design": "expects architecture/design contribution, not just implementation",
    "scale": "scale/traffic is a stated concern — perf thinking matters here",
    "production": "production responsibility (reliability/on-call/incidents) is part of the role",
    "mentorship": "mentoring or leading others is expected",
    "cross_team": "cross-functional communication is a real part of the job",
}


def extract_capabilities(description: str) -> dict:
    """Return {capability: evidence_snippet} for a JD. At most 2 snippets each,
    trimmed; absent signals stay absent — nothing inferred."""
    if not description:
        return {}
    out = {}
    for cap, pat in CAPABILITY_CUES.items():
        snippets = []
        for m in re.finditer(pat, description, flags=re.IGNORECASE):
            start = max(0, m.start() - 60)
            snippet = re.sub(r"\s+", " ", description[start:m.end() + 60]).strip()
            snippets.append(snippet)
            if len(snippets) == 2:
                break
        if snippets:
            out[cap] = " … ".join(snippets)
    return out


def profile_readiness(capabilities: dict, profile_lines: list) -> dict:
    """Compare the job's capability signals against the profile's evidence
    lines (resume bullets). Honest, coarse and labeled as heuristic."""
    text = " ".join(profile_lines).lower()
    readiness = {}
    cues = {
        "ownership": r"\b(owned|own(?:ed)?\s+\w+|end[- ]to[- ]end|responsible\s+for)\b",
        "design": r"\b(designed|architected|design(?:ed)?\s+\w+)\b",
        "scale": r"\b(scal\w+|millions|throughput|latency|concurrent)\b",
        "production": r"\b(production|deployed|on[- ]call|incident|uptime|monitor\w+)\b",
        "mentorship": r"\b(mentor\w*|taught|guided|supervis\w+)\b",
        "cross_team": r"\b(stakeholder|cross[- ]functional|partnered|collaborat\w+ (?:with|across))\b",
    }
    for cap in capabilities:
        readiness[cap] = bool(re.search(cues[cap], text))
    return readiness
