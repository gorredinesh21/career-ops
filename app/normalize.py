"""Deterministic normalizers for raw job text from the ingestion pipelines.

Everything here is rule-based and explainable: no LLM, no invention.
If a field cannot be confidently extracted it is left empty rather than guessed.
"""
import re

# ---------------------------------------------------------------------------
# Cruft: LinkedIn UI fragments that leak into scraped card titles.
# We truncate at the first cruft phrase rather than deleting individual words,
# because everything after "Viewed"/"Be an early applicant"/... is UI chrome.
CRUFT_MARKERS = [
    r"\bViewed\b",
    r"\bBe an early applicant\b",
    r"\bEarly applicant\b",
    r"\bPromoted\b",
    r"\(\s*Verified job\s*\)",
    r"\bVerified job\b",
    r"\b\d*\s*(?:connection|connections|company alumni|school alumni|alumni)\s+work here\b",
    r"\bActively reviewing applicants\b",
    r"\bNew applicant[s]?\b",
    r"\bJust posted\b",
    r"\bActively recruiting\b",
    r"\bReposted\b",
    r"\bEasy Apply\b",
]

WORK_MODEL_PATTERNS = [
    (r"\(\s*Remote\s*\)|\bRemote\b", "remote"),
    (r"\(\s*Hybrid\s*\)|\bHybrid\b", "hybrid"),
    (r"\(\s*On-?site\s*\)|\bOn-?site\b", "onsite"),
]

# Tokens that mark the start of a location inside a scraped title tail.
LOCATION_WORDS = {
    "india", "bengaluru", "bangalore", "hyderabad", "chennai", "mumbai",
    "pune", "delhi", "noida", "gurgaon", "gurugram", "kolkata", "ahmedabad",
    "jaipur", "kochi", "coimbatore", "indore", "chandigarh", "mysore",
    "remote", "bhubaneswar", "nagpur", "vadodara", "surat", "lucknow",
    "greater", "area",
}

# Words that plausibly belong to a job title; used to find where a run-in
# 'Software Engineer Acme India' title ends and the company begins.
ROLE_TOKENS = {
    "software", "development", "developer", "engineer", "engineering",
    "senior", "junior", "associate", "staff", "principal", "lead", "entry-level",
    "data", "ai", "ml", "dl", "genai", "backend", "frontend", "fullstack",
    "full-stack", "front-end", "back-end", "analyst", "scientist", "manager",
    "architect", "specialist", "consultant", "advisor", "intern", "systems",
    "system", "integration", "solutions", "platform", "cloud", "devops",
    "sre", "security", "network", "networking", "application", "applications",
    "technical", "technology", "product", "machine", "learning", "deep",
    "quantitative", "quant", "research", "researcher", "python", "java",
    "automation", "reporting", "analytics", "intelligence", "artificial",
    "llmops", "mlops", "agentic", "e2e", "sdet", "qa", "db",
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def truncate_at_cruft(text: str) -> str:
    """Cut the string at the first LinkedIn UI fragment."""
    cut = len(text)
    for pat in CRUFT_MARKERS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            cut = min(cut, m.start())
    return text[:cut].strip(" ·,|-")


def strip_inline_cruft(text: str) -> str:
    """Remove cruft phrases that appear inside otherwise good text."""
    out = text
    for pat in CRUFT_MARKERS:
        out = re.sub(pat, "", out, flags=re.IGNORECASE)
    return collapse(out).strip(" ·,|-")


def strip_verified(text: str) -> str:
    """Remove the '(Verified job)' badge LinkedIn inserts into the first title
    copy only — it breaks repetition detection when present in just one side."""
    return re.sub(r"\(\s*Verified job\s*\)", " ", text, flags=re.IGNORECASE)


def split_repeated_prefix(text: str):
    """LinkedIn alert titles repeat the job title twice back to back.

    'Backend Dev Backend Dev Acme India (Remote) ...' -> ('Backend Dev', 'Acme India (Remote) ...')
    A leading 'Selected,' badge token is dropped first. Returns (title, rest)
    or (None, text) when no immediate repetition exists.
    """
    words = collapse(strip_verified(text)).split(" ")
    while words and words[0].lower().strip(",") == "selected":
        words = words[1:]
    best = None
    for k in range(len(words) // 2, 0, -1):
        if words[:k] == words[k : 2 * k]:
            best = k
    if best is None:
        return None, collapse(text)
    return " ".join(words[:best]), " ".join(words[2 * best :])


def parse_work_model(text: str) -> str:
    low = (text or "").lower()
    for pat, value in WORK_MODEL_PATTERNS:
        if re.search(pat, low, flags=re.IGNORECASE):
            return value
    return "unspecified"


def split_company_location(tail: str):
    """Split 'Acme Technologies India (Remote)' into company + location.

    Company = words before the LAST location word (so 'Cummins India Pune
    Division' keeps 'Cummins India' as company and 'Pune Division' as location);
    location = from that word on.
    """
    tail = collapse(strip_inline_cruft(tail))
    if not tail:
        return "", ""
    # Work-model parens are handled separately; drop them before scanning.
    tail = collapse(re.sub(r"\(\s*(?:Remote|Hybrid|On-?site)\s*\)", " ", tail, flags=re.IGNORECASE))
    if not tail:
        return "", ""
    words = tail.split(" ")
    anchors = [i for i, w in enumerate(words)
               if re.sub(r"[^a-z]", "", w.lower()) in LOCATION_WORDS]
    if not anchors:
        # No location word: treat everything as company, no location claimed.
        return tail, ""
    loc_start = anchors[0]
    # A bare 'India' before a real city usually belongs to the company name
    # (e.g. 'Cummins India Pune Division'), so anchor at the city instead.
    first_word = re.sub(r"[^a-z]", "", words[loc_start].lower())
    if first_word == "india":
        later = [i for i in anchors if re.sub(r"[^a-z]", "", words[i].lower()) not in ("india",)]
        if later:
            loc_start = later[0]
    if loc_start == 0:
        return "", tail
    return " ".join(words[:loc_start]), " ".join(words[loc_start:])


CITY_SUFFIXES = {
    "division", "urban", "rural", "east", "west", "north", "south", "city",
    "district", "taluk", "mandal", "region", "metropolitan",
}


def extract_city(location: str) -> str:
    if not location:
        return ""
    first = collapse(location).split(",")[0].split("(")[0].strip()
    first = re.split(r"\d", first)[0].strip()  # cut salary text like '2.5M INR/yr'
    words = first.split()
    while len(words) > 1 and words[-1].lower() in CITY_SUFFIXES:
        words.pop()
    return " ".join(words) or first


def normalize_card(card_text: str):
    """Parse a LinkedIn card block (alert details) into structured fields.

    Cards come in two shapes: multi-line (title / company / location on
    separate lines) and single-line (the same content run together, like the
    ledger titles). Posted/applicant markers may appear inline in either.
    """
    text = card_text or ""
    posted = ""
    m = re.search(r"Posted\s+\d+\s+(?:minute|hour|day|week)s?\s+ago", text, flags=re.IGNORECASE)
    if m:
        posted = collapse(m.group(0))
    applicants = parse_applicants(text)

    def is_pure_meta(ln: str) -> bool:
        """A line that is only posted-time / applicants / apply-state chrome."""
        residue = re.sub(
            r"Posted\s+\d+\s+\w+\s+ago|\d+\s+applicants?|Easy Apply|Viewed|Verified|[·,\-|\s]",
            "", ln, flags=re.IGNORECASE,
        )
        return residue == ""

    lines = [collapse(ln) for ln in text.splitlines() if collapse(ln)]
    content = [ln for ln in lines if not is_pure_meta(ln)]

    if len(content) >= 1:
        # Try the single-line form first: line 0 may contain the whole
        # '{title}{title}{company}{location}{meta}' run-in even in multi-line
        # cards, with lines below merely repeating the title.
        rep_title, rest = split_repeated_prefix(content[0])
        if rep_title is not None:
            title = strip_inline_cruft(rep_title)
            company, location = split_company_location(truncate_at_cruft(rest))
            work_model = parse_work_model(rest)
            if not company and len(content) > 1:
                company = strip_inline_cruft(content[1])
            if not location and len(content) > 2:
                location = strip_inline_cruft(content[2])
        else:
            # Classic multi-line card. Line 0 may still be a run-in
            # 'title company location' string, so scan it too.
            scan_title, scan_tail = split_title_runin(content[0])
            if scan_tail:
                title = strip_inline_cruft(scan_title)
                s_company, s_location = split_company_location(scan_tail)
            else:
                title = strip_inline_cruft(content[0])
                s_company, s_location = "", ""
            company = strip_inline_cruft(content[1]) if len(content) > 1 else s_company
            location = strip_inline_cruft(content[2]) if len(content) > 2 else s_location
            work_model = parse_work_model(content[0] + " " + (location or ""))
    else:
        title = company = location = ""
        work_model = "unspecified"

    if location:
        location = collapse(re.sub(r"\(\s*(?:Remote|Hybrid|On-?site)\s*\)", "", location, flags=re.IGNORECASE)) or location
    return {
        "title": title,
        "company": company,
        "location": location,
        "city": extract_city(location),
        "work_model": work_model,
        "posted_text": posted,
        "applicants": applicants,
    }


def _is_roleish(word: str) -> bool:
    token = re.sub(r"[^a-z0-9/+&-]", "", word.lower())
    if not token or token in ROLE_TOKENS or token.isdigit() or re.fullmatch(r"[ivx]+", token):
        return True
    # 'Python/Django/GenAI' is title material if any slash segment is a role
    # word; hyphenated compounds like 'Acme-Data-Systems' need all segments
    # role-ish before they count as title rather than company.
    if "/" in token:
        return any(seg in ROLE_TOKENS for seg in token.split("/") if seg)
    if "-" in token:
        return all(seg in ROLE_TOKENS for seg in token.split("-") if seg)
    return False


def split_title_runin(text: str):
    """Split a run-in 'Software Engineer Acme India' string where title and
    company are jammed together with no separator: the title is the leading
    run of role-ish tokens. Returns (title, tail) — tail empty when the whole
    string looks like a title.
    """
    words = collapse(truncate_at_cruft(text)).split(" ")
    k = 0
    for i, w in enumerate(words):
        if _is_roleish(w):
            k = i + 1
        else:
            break
    if k == 0 or k >= len(words):
        return collapse(text), ""
    return " ".join(words[:k]), " ".join(words[k:])


def normalize_ledger_title(raw_title: str):
    """Parse a messy single-line ledger title into structured fields.

    Shapes seen in job_history.jsonl, in the order we try them:
    1. '{title} {title} {company} {location} {cruft}'   (alert cards)
    2. 'Selected, {title} {title} {company} ...'          (alert cards + badge)
    3. '{company} — {role}'                               (feed posts)
    4. '{title} {company} {location} {cruft}'             (run-in, no repeat)
    """
    empty = {"title": "", "company": "", "location": "", "city": "",
             "work_model": "unspecified", "posted_text": "", "applicants": None}
    raw = collapse(raw_title or "")
    if not raw:
        return empty

    title, rest = split_repeated_prefix(raw)
    if title is not None:
        title = strip_inline_cruft(title)
        work_model = parse_work_model(rest)
        rest_clean = re.sub(r"\(\s*(?:Remote|Hybrid|On-?site)\s*\)", " ",
                            truncate_at_cruft(rest), flags=re.IGNORECASE)
        company, location = split_company_location(rest_clean)
        return {"title": title, "company": company, "location": location,
                "city": extract_city(location), "work_model": work_model,
                "posted_text": "", "applicants": None}

    # Feed-post style 'Company — Role'.
    if " — " in raw or " – " in raw:
        sep = " — " if " — " in raw else " – "
        head, _, tail = raw.partition(sep)
        head, tail = collapse(head), collapse(truncate_at_cruft(tail))
        if len(head) >= 2 and len(tail) >= 4:
            return {"title": tail, "company": strip_inline_cruft(head),
                    "location": "", "city": "", "work_model": "unspecified",
                    "posted_text": "", "applicants": None}

    # Run-in title + company + location with no separator.
    work_model = parse_work_model(raw)
    title, tail = split_title_runin(raw)
    company, location = ("", "")
    if tail:
        tail_clean = re.sub(r"\(\s*(?:Remote|Hybrid|On-?site)\s*\)", " ", tail, flags=re.IGNORECASE)
        company, location = split_company_location(tail_clean)
    return {"title": strip_inline_cruft(title), "company": company,
            "location": location, "city": extract_city(location),
            "work_model": work_model, "posted_text": "", "applicants": None}


def parse_experience(*texts):
    """Extract (exp_min, exp_max, stated) from title + description text.

    Trusted sources, in order: explicit ranges ('1-2 years'), 'N+ years',
    entry-level keywords. A bare year count is accepted only when 'experience'
    sits within ~45 chars of the match or the match is in the title itself —
    random prose digits are ignored.
    """
    title = (texts[0] if texts else "") or ""
    body = " | ".join(t for t in texts if t)
    if not body:
        return None, None, None
    low = body.lower()
    title_end = len(title.lower())

    def context_ok(m) -> bool:
        ctx = low[max(0, m.start() - 45):m.end() + 45]
        return "experienc" in ctx or m.start() <= title_end

    m = re.search(r"(\d(?:\.\d)?)\s*(?:-|–|to)\s*(\d(?:\.\d)?)\s*\+?\s*years?", low)
    if m and context_ok(m):
        return float(m.group(1)), float(m.group(2)), collapse(m.group(0))

    m = re.search(r"(\d(?:\.\d)?)\s*\+\s*years?", low)
    if m and context_ok(m):
        return float(m.group(1)), None, collapse(m.group(0))

    if re.search(r"entry[ -]level|fresher|graduate\b|0[ -]1 year", low):
        return 0.0, 1.0, "entry-level"

    m = re.search(r"(\d)\s+years?\s+of\s+experience", low)
    if m:
        return float(m.group(1)), None, collapse(m.group(0))

    m = re.search(r"experience[:\s]*(?:of\s*)?(\d)\s+years?", low)
    if m:
        return float(m.group(1)), None, collapse(m.group(0))
    return None, None, None


def parse_salary(text: str) -> str:
    if not text:
        return ""
    m = re.search(
        r"(?:₹|\brs\.?\b|\binr\b)\s?\d[\d.,]*\s*(?:-|–|to)?\s*[\d.,]*\s*(?:lpa|lakh|lakhs|crore|cr\b|k\b)?",
        text, flags=re.IGNORECASE,
    )
    return collapse(m.group(0)) if m else ""


def parse_applicants(text: str):
    if not text:
        return None
    m = re.search(r"(\d+)\s+applicants?", text, flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
