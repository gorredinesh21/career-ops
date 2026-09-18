"""SQLite schema and connection helpers for Career-Ops.

Domain model follows the BRD section 16.1 (Jobs + Community + AI/ops objects):
canonical Job + JobSource provenance + versioned JobSnapshot + structured
JobSkill requirements + RoleFamily taxonomy + role requests/votes + feedback
+ ingest run audit.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "careerops.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS role_family (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    tagline TEXT,
    sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS job (
    id INTEGER PRIMARY KEY,
    canonical_key TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    city TEXT,
    work_model TEXT DEFAULT 'unspecified',   -- remote | hybrid | onsite | unspecified
    exp_min REAL,
    exp_max REAL,
    exp_stated TEXT,
    role_family_id INTEGER REFERENCES role_family(id),
    family_confidence REAL,
    family_reason TEXT,
    salary_text TEXT,
    applicants INTEGER,
    posted_text TEXT,
    description TEXT,
    curator_notes TEXT,
    apply_url TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    freshness TEXT NOT NULL DEFAULT 'active', -- active | likely_active | stale | closed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_first_seen ON job(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_job_family ON job(role_family_id);
CREATE INDEX IF NOT EXISTS idx_job_company ON job(company);

CREATE TABLE IF NOT EXISTS job_source (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES job(id),
    source_type TEXT NOT NULL,               -- alert | feed | curated | ledger
    external_id TEXT,
    raw_title TEXT,
    url TEXT,
    easy_apply INTEGER DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(source_type, external_id)
);
CREATE INDEX IF NOT EXISTS idx_source_job ON job_source(job_id);

CREATE TABLE IF NOT EXISTS job_snapshot (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES job(id),
    version INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(job_id, version)
);

CREATE TABLE IF NOT EXISTS job_skill (
    job_id INTEGER NOT NULL REFERENCES job(id),
    skill TEXT NOT NULL,
    requirement TEXT NOT NULL,               -- required | preferred | inferred
    UNIQUE(job_id, skill)
);
CREATE INDEX IF NOT EXISTS idx_skill_job ON job_skill(job_id);

CREATE TABLE IF NOT EXISTS role_request (
    id INTEGER PRIMARY KEY,
    role_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_vote (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES role_request(id),
    voter_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(request_id, voter_hash)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES job(id),
    kind TEXT NOT NULL,                      -- stale | wrong_role | wrong_data
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_run (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sources TEXT,
    jobs_seen INTEGER DEFAULT 0,
    jobs_new INTEGER DEFAULT 0,
    jobs_updated INTEGER DEFAULT 0,
    sources_merged INTEGER DEFAULT 0,
    notes TEXT
);
"""


def connect(db_path: Path = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    from app.classify import FAMILIES

    for sort, fam in enumerate(FAMILIES):
        conn.execute(
            "INSERT INTO role_family (slug, name, tagline, sort) VALUES (?,?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET name=excluded.name, tagline=excluded.tagline",
            (fam["slug"], fam["name"], fam["tagline"], sort),
        )
    conn.commit()


def get_db() -> sqlite3.Connection:
    conn = connect()
    init_db(conn)
    return conn
