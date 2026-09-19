"""Boot-time job synchronization for the hosted deployment.

The container image carries a jobs-only snapshot (deploy/careerops.db). At
startup we merge it into the live persistent DB:
- brand-new jobs are copied in full (with sources, skills, capabilities),
- existing jobs get freshness/date updates and richer fields backfilled.

User data — accounts, profiles, applications, votes, reviews, saved searches,
feedback — lives only in the persistent DB and is never written by the sync.
"""
import shutil
import sqlite3

JOB_UPDATE_FIELDS = {
    "title": "CASE WHEN excluded.title <> '' THEN excluded.title ELSE job.title END",
    "company": "CASE WHEN excluded.company <> '' THEN excluded.company ELSE job.company END",
    "location": "CASE WHEN excluded.location <> '' THEN excluded.location ELSE job.location END",
    "city": "CASE WHEN excluded.city <> '' THEN excluded.city ELSE job.city END",
    "work_model": "CASE WHEN excluded.work_model <> 'unspecified' THEN excluded.work_model ELSE job.work_model END",
    "exp_min": "COALESCE(job.exp_min, excluded.exp_min)",
    "exp_max": "COALESCE(job.exp_max, excluded.exp_max)",
    "exp_stated": "COALESCE(job.exp_stated, excluded.exp_stated)",
    "role_family_id": "COALESCE(job.role_family_id, excluded.role_family_id)",
    "family_confidence": "COALESCE(job.family_confidence, excluded.family_confidence)",
    "family_reason": "CASE WHEN excluded.family_reason <> '' THEN excluded.family_reason ELSE job.family_reason END",
    "salary_text": "COALESCE(NULLIF(excluded.salary_text, ''), job.salary_text)",
    "applicants": "COALESCE(job.applicants, excluded.applicants)",
    "posted_text": "COALESCE(NULLIF(excluded.posted_text, ''), job.posted_text)",
    "description": "COALESCE(NULLIF(excluded.description, ''), job.description)",
    "apply_url": "COALESCE(NULLIF(excluded.apply_url, ''), job.apply_url)",
    "last_seen": "MAX(job.last_seen, excluded.last_seen)",
    "first_seen": "MIN(job.first_seen, excluded.first_seen)",
}

JOB_COLS = ["canonical_key", "title", "company", "location", "city", "work_model",
            "exp_min", "exp_max", "exp_stated", "role_family_id", "family_confidence",
            "family_reason", "salary_text", "applicants", "posted_text",
            "description", "curator_notes", "apply_url", "first_seen", "last_seen",
            "freshness", "created_at", "updated_at"]


def ensure_db(db_path, seed_path) -> bool:
    """Create the live DB from the baked snapshot on first boot. Returns True
    when a fresh DB was seeded."""
    if db_path.exists():
        return False
    if not seed_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return False  # local dev: no seed, schema init will create it
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(seed_path, db_path)
    return True


def sync_jobs_from_seed(conn: sqlite3.Connection, seed_path) -> int:
    """Merge job rows from the baked snapshot into the live DB. Returns the
    number of jobs processed. No-op (0) when the seed is absent (local dev)."""
    if not seed_path.exists():
        return 0
    conn.execute("ATTACH DATABASE ? AS seed", (str(seed_path),))
    try:
        cols = ", ".join(JOB_COLS)
        updates = ", ".join(f"{k} = {v}" for k, v in JOB_UPDATE_FIELDS.items())
        conn.execute(
            f"INSERT INTO job ({cols}) SELECT {cols} FROM seed.job WHERE true "
            f"ON CONFLICT(canonical_key) DO UPDATE SET {updates}"
        )
        # Re-point per-job child rows for jobs that pre-existed (ids differ).
        for table in ("job_skill", "job_capability"):
            conn.execute(
                f"DELETE FROM {table} WHERE job_id IN "
                "(SELECT j.id FROM job j JOIN seed.job s ON s.canonical_key = j.canonical_key)"
            )
            if table == "job_skill":
                conn.execute(
                    "INSERT INTO job_skill (job_id, skill, requirement) "
                    "SELECT j.id, s.skill, s.requirement FROM seed.job_skill s "
                    "JOIN job j ON j.canonical_key = "
                    "(SELECT canonical_key FROM seed.job WHERE id = s.job_id)"
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO job_capability (job_id, capability, evidence) "
                    "SELECT j.id, s.capability, s.evidence FROM seed.job_capability s "
                    "JOIN job j ON j.canonical_key = "
                    "(SELECT canonical_key FROM seed.job WHERE id = s.job_id)"
                )
        conn.execute(
            "INSERT OR IGNORE INTO job_source (job_id, source_type, external_id, raw_title, "
            "url, easy_apply, first_seen, last_seen) "
            "SELECT j.id, s.source_type, s.external_id, s.raw_title, s.url, s.easy_apply, "
            "s.first_seen, s.last_seen FROM seed.job_source s "
            "JOIN job j ON j.canonical_key = "
            "(SELECT canonical_key FROM seed.job WHERE id = s.job_id)"
        )
        n = conn.execute("SELECT COUNT(*) AS n FROM seed.job").fetchone()["n"]
        conn.commit()
        return n
    finally:
        conn.execute("DETACH DATABASE seed")
