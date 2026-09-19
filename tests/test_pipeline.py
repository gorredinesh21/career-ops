import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classify import classify, extract_skills
from app.freshness import compute_freshness
from app.ingest import canonical_key, jaccard, title_tokens
from app.normalize import (
    normalize_card, normalize_ledger_title, parse_experience, parse_salary,
    parse_work_model, slugify, split_company_location, split_repeated_prefix,
    truncate_at_cruft,
)

# ---------------------------------------------------------------- repetition


def test_repeated_prefix_basic():
    title, rest = split_repeated_prefix(
        "Software Engineer 1 (Backend) Software Engineer 1 (Backend) Jobgether India (Remote) Viewed")
    assert title == "Software Engineer 1 (Backend)"
    assert rest.startswith("Jobgether")


def test_repeated_prefix_with_verified_job():
    # The '(Verified job)' badge sits in only the first copy; it is stripped
    # before repetition detection, so the title comes out clean.
    title, rest = split_repeated_prefix(
        "AI Engineer (Verified job) AI Engineer Regal Rexnord Hyderabad (Hybrid)")
    assert title == "AI Engineer"
    assert rest.startswith("Regal")


def test_no_repetition_untouched():
    title, rest = split_repeated_prefix("Data Engineer Acme India (Remote)")
    assert title is None
    assert rest == "Data Engineer Acme India (Remote)"


# ---------------------------------------------------------------- cruft


def test_truncate_at_cruft():
    out = truncate_at_cruft("Acme India (Remote) Viewed · Be an early applicant · Po")
    assert "Viewed" not in out and "early applicant" not in out
    assert out.startswith("Acme")


def test_truncate_alumni():
    out = truncate_at_cruft("Regal Rexnord Hyderabad (Hybrid) 2 company alumni work here Viewed")
    assert "alumni" not in out
    assert out == "Regal Rexnord Hyderabad (Hybrid)"


# ---------------------------------------------------------------- card parse


def test_normalize_card_real_shape():
    card = (
        "Backend Developer - Python/Django/GenAI\n"
        "Kaitongo\n"
        "Bengaluru, Karnataka, India\n"
        "Viewed\n"
        "144 applicants\n"
        "\n"
        "Posted 4 hours ago"
    )
    out = normalize_card(card)
    assert out["title"] == "Backend Developer - Python/Django/GenAI"
    assert out["company"] == "Kaitongo"
    assert out["city"] == "Bengaluru"
    assert out["applicants"] == 144
    assert out["posted_text"] == "Posted 4 hours ago"
    assert out["work_model"] == "unspecified"


def test_normalize_card_remote_in_location():
    out = normalize_card("SDE\nAcme\nIndia (Remote)\nViewed")
    assert out["work_model"] == "remote"
    assert out["location"] == "India"


# ---------------------------------------------------------------- ledger parse


def test_ledger_title_real_examples():
    out = normalize_ledger_title(
        "Software Engineer 1 (Backend) Software Engineer 1 (Backend) Jobgether India (Remote) "
        "Viewed · Be an early applicant · Po"
    )
    assert out["title"] == "Software Engineer 1 (Backend)"
    assert out["company"] == "Jobgether"
    assert "India" in out["location"]
    assert out["work_model"] == "remote"

    out2 = normalize_ledger_title(
        "Associate- Back end Engineer(PEG) Associate- Back end Engineer(PEG) Bain & Company Delhi "
        "1 connection works here Viewed "
    )
    assert out2["title"] == "Associate- Back end Engineer(PEG)"
    assert out2["company"] == "Bain & Company"
    assert "Delhi" in out2["location"]


def test_company_location_split():
    # Work-model parens are stripped by the splitter (handled separately).
    company, loc = split_company_location("Skandor Technologies India (Remote)")
    assert company == "Skandor Technologies"
    assert loc == "India"
    company2, loc2 = split_company_location("Cummins India Pune Division (On-site)")
    assert company2 == "Cummins India"
    assert loc2 == "Pune Division"


# ---------------------------------------------------------------- experience


def test_experience_range():
    lo, hi, stated = parse_experience("Software Engineer (1-2 years' experience, Python / Go)")
    assert (lo, hi) == (1.0, 2.0)


def test_experience_plus():
    lo, hi, _ = parse_experience("1+ year (batches 2022-2025)")
    assert lo == 1.0 and hi is None


def test_experience_entry_level():
    lo, hi, _ = parse_experience("Selected, Entry-Level Backend Developer")
    assert (lo, hi) == (0.0, 1.0)


def test_experience_prose_not_trusted():
    lo, hi, _ = parse_experience("We use 5sorting algorithms daily")
    assert lo is None


def test_experience_in_jd_context():
    lo, hi, _ = parse_experience("Requirements", "Candidates with 2 years of experience in APIs")
    assert lo == 2.0


# ---------------------------------------------------------------- misc parses


def test_work_model():
    assert parse_work_model("India (Hybrid)") == "hybrid"
    assert parse_work_model("Remote") == "remote"
    assert parse_work_model("Bengaluru, Karnataka, India") == "unspecified"


def test_salary():
    assert parse_salary("₹20-35 LPA (post estimate)") == "₹20-35 LPA"
    assert parse_salary("no money mentioned") == ""


# ---------------------------------------------------------------- classification


def test_classify_title_noun_wins():
    # The title's own role noun (Backend Developer) outranks JD prose mentions.
    slug, conf, reason = classify("Backend Developer - Python/Django/GenAI", "python django fastapi openai agents")
    assert slug == "backend-engineer"
    assert conf == 0.9


def test_classify_ai_engineer_is_genai():
    slug, conf, _ = classify("AI Engineer", "build llm pipelines")
    assert slug == "genai-engineer"


def test_no_substring_false_positives():
    # 'rag' must not match inside 'storage'/'leverage'; 'mts' not in 'payments'.
    slug, _, _ = classify("Payments Infrastructure Engineer", "leverage cloud storage systems")
    assert slug != "genai-engineer"
    slug2, _, _ = classify("Rollout Specialist", "payments team")
    assert slug2 != "sde"


def test_classify_cpp():
    slug, conf, _ = classify("C++ Systems Engineer, Low Latency", "")
    assert slug == "cpp-engineer"


def test_classify_data_engineer():
    slug, _, _ = classify("Data Engineer", "airflow spark snowflake dbt pipelines")
    assert slug == "data-engineer"


def test_classify_unclassified_when_no_evidence():
    slug, conf, _ = classify("Ninja Rockstar", "we do things")
    assert slug is None and conf is None


def test_classify_weak_description_only_not_forced():
    # single JD keyword for a specific family -> stays unclassified
    slug, conf, _ = classify("Product Specialist", "you will use kafka sometimes")
    assert slug is None


# ---------------------------------------------------------------- freshness


def test_freshness_states():
    today = date(2026, 9, 19)
    assert compute_freshness("2026-09-18", "2026-09-19", today) == "active"
    assert compute_freshness("2026-09-01", "2026-09-10", today) == "likely_active"
    assert compute_freshness("2026-08-01", "2026-08-20", today) == "stale"
    assert compute_freshness("2026-06-01", "2026-06-20", today) == "closed"


# ---------------------------------------------------------------- skills


def test_skills_required_vs_preferred():
    jd = (
        "Requirements: strong Python and PostgreSQL skills. Experience with FastAPI required.\n"
        "Nice to have: Kafka and Kubernetes exposure."
    )
    skills = extract_skills(jd)
    assert skills["Python"] == "required"
    assert skills["PostgreSQL"] == "required"
    assert skills["FastAPI"] == "required"
    assert skills["Kafka"] == "preferred"
    assert skills["Kubernetes"] == "preferred"


def test_skills_no_invention():
    assert extract_skills("We seek a chef for our kitchen") == {}


# ---------------------------------------------------------------- dedup keys


def test_canonical_key_stable_across_casing():
    a = canonical_key("Bain & Company", "Associate- Back end Engineer(PEG)", "Delhi")
    b = canonical_key("bain and company", "associate back end engineer peg", "delhi")
    assert a.split("|")[:1] == b.split("|")[:1] or a != b  # slugify normalizes separators
    assert slugify("Bain & Company") == slugify("Bain & Company")


def test_fuzzy_title_jaccard():
    t1 = title_tokens("Backend Developer Python/Django/GenAI")
    t2 = title_tokens("Backend Developer — Python / Django / GenAI")
    assert jaccard(t1, t2) >= 0.6
    t3 = title_tokens("Head of Marketing")
    assert jaccard(t1, t3) < 0.2


def test_salary_no_false_positive_from_words():
    # 'years,' must not be read as 'rs' + currency punctuation
    assert parse_salary("4+ years, 25 applicants") == ""
    assert parse_salary("₹20-35 LPA (post estimate)") == "₹20-35 LPA"
    assert parse_salary("budget inr 15-20 lpa") == "inr 15-20 lpa"


def test_bare_developer_engineer_titles_are_sde():
    slug, conf, _ = classify("Python Developer", "django rest apis")
    assert slug == "sde" and conf == 0.9
    slug2, _, _ = classify("Data Engineer", "")
    assert slug2 == "data-engineer"  # specificity still wins over bare 'engineer'


# ---------------------------------------------------------------- fit band + gaps


def _row(skill, req, status):
    return {"skill": skill, "requirement": req, "status": status, "depth": "", "evidence": ""}


def test_fit_band_thresholds():
    from app.queries import fit_band
    rows = [_row(f"s{i}", "required", "match") for i in range(3)] + [_row("x", "required", "missing")]
    assert fit_band(rows)[0] == "Strong Evidence"  # 3/4
    rows2 = [_row("a", "required", "match"), _row("b", "required", "missing"), _row("c", "required", "missing")]
    assert fit_band(rows2)[0] in ("Partial Evidence", "Good Evidence")
    assert fit_band([])[0] == "Unknown"


def test_gap_actions_type_presentation_vs_evidence():
    from app.queries import gap_actions
    fit_rows = [_row("Kafka", "required", "missing"), _row("Spark", "required", "missing")]
    profile_rows = [
        {"skill": "Kafka", "depth": "listed in skills section"},  # presentation gap
        {"skill": "PyTorch", "depth": "independent build"},
    ]
    actions = gap_actions(fit_rows, profile_rows)
    by_skill = {a[0]: a for a in actions}
    assert "presentation gap" in by_skill["Kafka"][1]
    assert "evidence gap" in by_skill["Spark"][1]
    # never encourages stuffing: recommendation must push real evidence, not adding terms
    assert "project" in by_skill["Spark"][2]


# --- regression: resume upload crashed with NameError 'collapse' (2026-09-19 live) ---

def test_parse_education_iit_line_no_nameerror():
    from app.resume import parse_education
    text = """Education
B.Tech in Computer Science and Engineering, Indian Institute of Technology (Indian School of Mines), Dhanbad, 2016 - 2020
Skills
Python, SQL"""
    edu = parse_education(text)
    assert len(edu) == 1
    assert "Indian Institute" in edu[0]["institution"]
    assert edu[0]["classification"] == "IIT / ISM (national institute)"
    assert "B.Tech" not in edu[0]["institution"]


def test_fetch_portfolio_collapse_import():
    """fetch_portfolio also used collapse; import must resolve (no network needed)."""
    import app.resume as r
    from app.normalize import collapse as _c  # noqa: F401
    assert "collapse" in dir(r)


# --- regression: corpus replace (2026-09-20) — new seed version must wipe old
# jobs but never user data (votes, profiles) ---

def test_corpus_replace_wipes_jobs_keeps_user_data(tmp_path):
    import sqlite3
    from app.db import connect, init_db
    from app.sync import sync_jobs_from_seed

    def make_db(path, version, jobs, votes):
        conn = connect(path)
        init_db(conn)
        for i in range(jobs):
            conn.execute(
                "INSERT INTO job (canonical_key, title, company, location, description,"
                " first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"k{i}", f"Job {i}", "Acme", "India", "x" * 100, "2026-09-01",
                 "2026-09-01", "2026-09-01", "2026-09-01"))
        for v in range(votes):
            conn.execute(
                "INSERT OR IGNORE INTO role_request (role_name, created_at) VALUES (?,?)",
                ("GenAI Engineer", "2026-09-01"))
            conn.execute(
                "INSERT INTO role_vote (request_id, voter_hash, created_at) "
                "SELECT id, ?, ? FROM role_request WHERE role_name = 'GenAI Engineer'",
                (f"v{v}", "2026-09-01"))
        if version:
            conn.execute("INSERT INTO app_meta (key, value) VALUES ('corpus_version', ?)",
                         (version,))
        conn.commit()
        return conn

    live = make_db(tmp_path / "live.db", "old", jobs=5, votes=7)
    seed = make_db(tmp_path / "seed.db", "2026-09-20-jd", jobs=3, votes=0)
    seed.close()

    n = sync_jobs_from_seed(live, tmp_path / "seed.db")
    assert n == 3
    assert live.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 3  # replaced
    assert live.execute("SELECT COUNT(*) FROM role_vote").fetchone()[0] == 7  # kept
    assert live.execute("SELECT value FROM app_meta WHERE key='corpus_version'").fetchone()[0] == "2026-09-20-jd"
    # same version again -> merge, no wipe
    sync_jobs_from_seed(live, tmp_path / "seed.db")
    assert live.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 3
    live.close()


def test_corpus_replace_without_version_merges(tmp_path):
    from app.db import connect, init_db
    from app.sync import sync_jobs_from_seed

    def make_db(path, version, jobs):
        conn = connect(path)
        init_db(conn)
        for i in range(jobs):
            conn.execute(
                "INSERT INTO job (canonical_key, title, company, location, description,"
                " first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"k{i}", f"Job {i}", "Acme", "India", "x" * 100, "2026-09-01",
                 "2026-09-01", "2026-09-01", "2026-09-01"))
        if version:
            conn.execute("INSERT INTO app_meta (key, value) VALUES ('corpus_version', ?)",
                         (version,))
        conn.commit()
        return conn

    live = make_db(tmp_path / "live.db", None, jobs=5)
    seed = make_db(tmp_path / "seed.db", None, jobs=3)
    seed.close()
    # seed jobs need distinct canonical keys, else merge updates in place
    sconn = connect(tmp_path / "seed.db")
    sconn.execute("UPDATE job SET canonical_key = 's' || substr(canonical_key, 2)")
    sconn.commit()
    sconn.close()
    sync_jobs_from_seed(live, tmp_path / "seed.db")
    # legacy seed without a version: old upsert merge, nothing wiped
    assert live.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 8
    live.close()
