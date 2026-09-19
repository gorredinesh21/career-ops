"""Read-model queries for the web layer."""
import re
from collections import Counter

BANDS = {
    "0-1": (0.0, 1.0),
    "1-2": (1.0, 2.0),
    "2-3": (2.0, 3.0),
    "3-4": (3.0, 4.0),
    "4+": (4.0, 99.0),
}


def query_jobs(conn, *, family=None, band=None, work=None, loc=None, q=None,
               freshness=None, page=1, per_page=25):
    where, params = [], []
    selects = """
        SELECT j.*, f.slug AS family_slug, f.name AS family_name,
               (SELECT COUNT(*) FROM job_source s WHERE s.job_id = j.id) AS source_count
        FROM job j LEFT JOIN role_family f ON f.id = j.role_family_id
    """
    if family:
        where.append("f.slug = ?")
        params.append(family)
    if band and band in BANDS:
        bmin, bmax = BANDS[band]
        # Overlap test; unspecified experience is kept visible in every band.
        where.append("(j.exp_min IS NULL OR j.exp_min <= ?) AND (j.exp_max IS NULL OR j.exp_max >= ?)")
        params.extend([bmax, bmin])
    if work and work in ("remote", "hybrid", "onsite"):
        where.append("j.work_model = ?")
        params.append(work)
    if freshness in ("active", "likely_active", "stale", "closed"):
        where.append("j.freshness = ?")
        params.append(freshness)
    if loc:
        where.append("(j.location LIKE ? OR j.city LIKE ? OR j.company LIKE ?)")
        like = f"%{loc}%"
        params.extend([like, like, like])
    if q:
        where.append("(j.title LIKE ? OR j.company LIKE ? OR j.description LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM job j LEFT JOIN role_family f ON f.id = j.role_family_id" + clause,
        params,
    ).fetchone()["n"]
    rows = conn.execute(
        selects + clause + " ORDER BY j.first_seen DESC, j.id DESC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    return rows, total


def get_job(conn, job_id: int):
    return conn.execute(
        "SELECT j.*, f.slug AS family_slug, f.name AS family_name "
        "FROM job j LEFT JOIN role_family f ON f.id = j.role_family_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()


def job_sources(conn, job_id: int):
    return conn.execute(
        "SELECT * FROM job_source WHERE job_id = ? ORDER BY first_seen", (job_id,)
    ).fetchall()


def job_skills(conn, job_id: int):
    return conn.execute(
        "SELECT * FROM job_skill WHERE job_id = ? ORDER BY requirement, skill", (job_id,)
    ).fetchall()


def job_feedback(conn, job_id: int):
    return conn.execute(
        "SELECT * FROM feedback WHERE job_id = ? ORDER BY created_at DESC LIMIT 20",
        (job_id,),
    ).fetchall()


def snapshot_count(conn, job_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM job_snapshot WHERE job_id = ?", (job_id,)
    ).fetchone()["n"]


def families_with_counts(conn):
    return conn.execute(
        "SELECT f.*, (SELECT COUNT(*) FROM job j WHERE j.role_family_id = f.id) AS n "
        "FROM role_family f ORDER BY f.sort"
    ).fetchall()


def platform_stats(conn):
    one = lambda sql, p=(): conn.execute(sql, p).fetchone()["n"]  # noqa: E731
    return {
        "jobs": one("SELECT COUNT(*) AS n FROM job"),
        "active": one("SELECT COUNT(*) AS n FROM job WHERE freshness IN ('active','likely_active')"),
        "companies": one("SELECT COUNT(DISTINCT lower(company)) AS n FROM job WHERE company <> ''"),
        "with_description": one("SELECT COUNT(*) AS n FROM job WHERE description <> ''"),
        "added_today": one("SELECT COUNT(*) AS n FROM job WHERE first_seen = date('now','localtime')"),
        "sources": one("SELECT COUNT(*) AS n FROM job_source"),
        "merged_jobs": one(
            "SELECT COUNT(*) AS n FROM job WHERE id IN "
            "(SELECT job_id FROM job_source GROUP BY job_id HAVING COUNT(*) > 1)"),
    }


def ops_dashboard(conn):
    fam_counts = conn.execute(
        "SELECT f.slug, f.name, COUNT(j.id) AS n, AVG(j.family_confidence) AS conf "
        "FROM role_family f LEFT JOIN job j ON j.role_family_id = f.id "
        "GROUP BY f.id ORDER BY n DESC"
    ).fetchall()
    freshness_counts = conn.execute(
        "SELECT freshness, COUNT(*) AS n FROM job GROUP BY freshness"
    ).fetchall()
    unclassified = conn.execute(
        "SELECT id, title, company FROM job WHERE role_family_id IS NULL "
        "ORDER BY first_seen DESC LIMIT 30"
    ).fetchall()
    runs = conn.execute(
        "SELECT * FROM ingest_run ORDER BY id DESC LIMIT 10"
    ).fetchall()
    feedback_rows = conn.execute(
        "SELECT fb.*, j.title FROM feedback fb LEFT JOIN job j ON j.id = fb.job_id "
        "ORDER BY fb.created_at DESC LIMIT 20"
    ).fetchall()
    city_top = conn.execute(
        "SELECT COALESCE(NULLIF(city,''),'(unknown)') AS city, COUNT(*) AS n "
        "FROM job GROUP BY city ORDER BY n DESC LIMIT 10"
    ).fetchall()
    skill_top = conn.execute(
        "SELECT skill, COUNT(*) AS n FROM job_skill GROUP BY skill ORDER BY n DESC LIMIT 15"
    ).fetchall()
    return {
        "families": fam_counts,
        "freshness": freshness_counts,
        "unclassified": unclassified,
        "runs": runs,
        "feedback": feedback_rows,
        "cities": city_top,
        "skills": skill_top,
    }


def role_requests(conn):
    return conn.execute(
        "SELECT r.id, r.role_name, r.created_at, COUNT(v.id) AS votes "
        "FROM role_request r LEFT JOIN role_vote v ON v.request_id = r.id "
        "GROUP BY r.id ORDER BY votes DESC, r.created_at"
    ).fetchall()


# ---------------------------------------------------------------- candidate


def latest_profile(conn):
    return conn.execute(
        "SELECT * FROM candidate_profile ORDER BY id DESC LIMIT 1"
    ).fetchone()


def profile_skills(conn, profile_id: int):
    return conn.execute(
        "SELECT * FROM candidate_skill WHERE profile_id = ? ORDER BY skill", (profile_id,)
    ).fetchall()


def profile_repos(conn, profile_id: int):
    return conn.execute(
        "SELECT * FROM candidate_github_repo WHERE profile_id = ? "
        "ORDER BY pushed_at DESC LIMIT 50", (profile_id,)
    ).fetchall()


def profile_family_affinity(conn, profile_id: int):
    """Which role families this profile's skills show up in most, measured as
    demand for the profile's skills among live postings of that family."""
    return conn.execute(
        """
        SELECT f.name, f.slug,
               COUNT(DISTINCT js.skill) AS matched_skills,
               (SELECT COUNT(DISTINCT skill) FROM job_skill
                 WHERE job_id IN (SELECT id FROM job WHERE role_family_id = f.id)) AS family_skills
        FROM role_family f
        CROSS JOIN candidate_skill cs ON cs.profile_id = ?
        JOIN job_skill js ON lower(js.skill) = lower(cs.skill)
        JOIN job j ON j.id = js.job_id AND j.role_family_id = f.id
        GROUP BY f.id
        ORDER BY matched_skills DESC
        """,
        (profile_id,),
    ).fetchall()


def job_fit_for_profile(conn, job_id: int, profile_id: int):
    """Requirement-by-requirement evidence comparison for one job vs the
    profile. Returns list of {skill, requirement, status, depth, evidence}."""
    reqs = conn.execute(
        "SELECT skill, requirement FROM job_skill WHERE job_id = ? ORDER BY requirement, skill",
        (job_id,),
    ).fetchall()
    skills = conn.execute(
        "SELECT skill, depth, evidence, confidence FROM candidate_skill WHERE profile_id = ?",
        (profile_id,),
    ).fetchall()
    by_name = {}
    for s in skills:
        cur = by_name.get(s["skill"])
        if cur is None or (s["confidence"] or 0) > (cur["confidence"] or 0):
            by_name[s["skill"]] = s
    out = []
    for r in reqs:
        hit = by_name.get(r["skill"])
        if hit:
            status = "match" if hit["confidence"] and hit["confidence"] >= 0.55 else "weak"
            out.append({"skill": r["skill"], "requirement": r["requirement"],
                        "status": status, "depth": hit["depth"], "evidence": hit["evidence"]})
        else:
            out.append({"skill": r["skill"], "requirement": r["requirement"],
                        "status": "missing", "depth": "", "evidence": ""})
    return out
