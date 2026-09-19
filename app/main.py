"""Career-Ops web application — jobs, candidate intelligence, matching,
applications, alerts, agent, accounts."""
import hashlib
import json
import time
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import queries
from app.auth import login as auth_login
from app.auth import logout as auth_logout
from app.auth import register as auth_register
from app.auth import user_for_token
from app.config import TELEGRAM_URL, VOTE_REVIEW_THRESHOLD
from app.db import get_db, write_txn
from app.fit import band_of, capability_readiness, evaluate, typed_gaps
from app.freshness import freshness_explanation
from app.normalize import collapse
from app.resume import extract_pdf_text

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Experience-aware role model (BRD Module D).
EXPERIENCE_MATRIX = [
    ("Execution", "Small tasks with guidance", "Own small/medium work",
     "Independently deliver meaningful components", "Own larger ambiguous work"),
    ("Technical depth", "Fundamentals + role basics", "Solid applied skills",
     "Deeper system understanding", "Architecture/trade-off awareness"),
    ("Ownership", "Task-level", "Feature/component-level",
     "Service/domain-level", "Cross-component/team-level signals"),
    ("Debugging", "Known failure modes", "Independent troubleshooting",
     "Complex incident/problem solving", "Prevention and systemic improvements"),
    ("Communication", "Clear status/reasoning", "Technical communication within team",
     "Cross-functional clarity", "Stakeholder influence and technical direction"),
    ("Mentoring", "Learning orientation", "May help peers informally",
     "Regular peer support", "Coaching/technical leadership signals"),
]

app = FastAPI(title="Career-Ops", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def startup() -> None:
    """One-time boot: restore the newest healthy GCS backup (or seed a fresh
    DB), create schema, refresh job data from the baked snapshot (user data
    is never touched), then start the async backup loop."""
    from app import storage
    from app.config import DB_PATH, GCS_BUCKET, SEED_DB_PATH
    from app.db import connect, init_db
    from app.sync import ensure_db, sync_jobs_from_seed

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    backup = storage.configure(DB_PATH, GCS_BUCKET)
    restored = backup.restore() if backup else False
    if not restored and DB_PATH.exists() and not storage.db_is_healthy(DB_PATH):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        DB_PATH.rename(DB_PATH.parent / f"{DB_PATH.name}.bad-{stamp}")
        print(f"[startup] local db unhealthy; moved aside", flush=True)
    if not restored and not DB_PATH.exists():
        ensure_db(DB_PATH, SEED_DB_PATH)
    conn = connect()
    init_db(conn)
    synced = sync_jobs_from_seed(conn, SEED_DB_PATH)
    conn.close()
    if backup:
        backup.start()
        backup.mark_dirty()  # persist the post-sync state immediately
    print(f"[startup] db ready at {DB_PATH} (restored_from_backup={restored}); "
          f"jobs synced from seed: {synced}", flush=True)


@app.on_event("shutdown")
def shutdown() -> None:
    from app import storage

    if storage.active():
        storage.active().flush_now()


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Any unhandled failure renders the honest error page (with the reason)
    instead of an opaque 500 — errors stay visible, never silent."""
    import traceback

    print("".join(traceback.format_exception(exc)), flush=True)
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "code": 500,
         "message": f"Something broke on this page ({type(exc).__name__}). "
                    "The error was logged — try again in a minute."},
        status_code=500,
    )


def db():
    """Per-request connection (FastAPI runs sync routes in a threadpool)."""
    return get_db()


@app.middleware("http")
async def cookies(request: Request, call_next):
    response = await call_next(request)
    if not request.cookies.get("voter_id"):
        response.set_cookie("voter_id", uuid.uuid4().hex, max_age=60 * 60 * 24 * 365)
    return response


def current_user(request: Request):
    conn = db()
    return user_for_token(conn, request.cookies.get("co_session", ""))


def user_key_for(request: Request) -> str:
    user = current_user(request)
    return user["username"] if user else "local"


def voter_hash(request: Request) -> str:
    vid = request.cookies.get("voter_id", request.client.host if request.client else "anon")
    return hashlib.sha256(vid.encode()).hexdigest()[:16]


def ctx(request: Request, **kw) -> dict:
    user = current_user(request)
    base = {
        "request": request,
        "today": date.today().isoformat(),
        "bands": list(queries.BANDS.keys()),
        "telegram_url": TELEGRAM_URL,
        "user": user,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- errors


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return templates.TemplateResponse(
        request=request, name="error.html",
        context=ctx(request, code=404, message="That page does not exist."),
        status_code=404,
    )


@app.exception_handler(500)
async def server_error(request: Request, exc):
    return templates.TemplateResponse(
        request=request, name="error.html",
        context=ctx(request, code=500,
                    message="Something broke on our side. The error is real, not a spinner."),
        status_code=500,
    )


# ---------------------------------------------------------------- jobs


@app.get("/healthz")
def healthz():
    stats = queries.platform_stats(db())
    return JSONResponse({"ok": True, **stats})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = db()
    stats = queries.platform_stats(conn)
    families = queries.families_with_counts(conn)
    latest, _ = queries.query_jobs(conn, page=1, per_page=5)
    return templates.TemplateResponse(
        request=request, name="index.html",
        context=ctx(request, stats=stats, families=families, latest=latest),
    )


@app.get("/jobs", response_class=HTMLResponse)
def jobs(
    request: Request,
    family: str = "", band: str = "", work: str = "", loc: str = "",
    q: str = "", freshness: str = "", page: int = 1,
):
    page = max(1, page)
    conn = db()
    rows, total = queries.query_jobs(
        conn, family=family or None, band=band or None, work=work or None,
        loc=loc or None, q=q or None, freshness=freshness or None, page=page,
    )
    query_params = {k: v for k, v in dict(
        family=family, band=band, work=work, loc=loc, q=q, freshness=freshness
    ).items() if v}
    pages = max(1, -(-total // 25))
    return templates.TemplateResponse(
        request=request, name="jobs.html",
        context=ctx(
            request, jobs=rows, total=total, families=queries.families_with_counts(conn),
            filters=query_params, page=page, pages=pages,
        ),
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    conn = db()
    job = queries.get_job(conn, job_id)
    if job is None:
        return templates.TemplateResponse(
            request=request, name="error.html",
            context=ctx(request, code=404, message="No such job."),
            status_code=404,
        )
    with write_txn() as wconn:
        wconn.execute(
            "INSERT INTO page_event (kind, job_id, created_at) VALUES ('job_view', ?, datetime('now','localtime'))",
            (job_id,),
        )

    ukey = user_key_for(request)
    prof = queries.latest_profile(conn, ukey)
    skills = fit_rows = fit_band = fit_why = gaps = caps = None
    alignment = prefs_mismatch = None
    profile_skills = []
    if prof:
        profile_skills = queries.profile_skills(conn, prof["id"])
        ev = evaluate(conn, job, profile_skills,
                      reviews=queries.profile_reviews(conn, prof["id"]),
                      user_band=queries.profile_band(conn, prof),
                      user_prefs=queries.profile_prefs(conn, prof))
        fit_rows, fit_band, fit_why = ev["rows"], ev["band"], ev["why"]
        alignment = ev["alignment"]
        prefs_mismatch = ev["preference_mismatches"]
        gaps = typed_gaps(ev, profile_skills)
        caps = capability_readiness(conn, job_id, [s["evidence"] for s in profile_skills])
    return templates.TemplateResponse(
        request=request, name="job_detail.html",
        context=ctx(
            request,
            job=job,
            sources=queries.job_sources(conn, job_id),
            skills=queries.job_skills(conn, job_id),
            feedback=queries.job_feedback(conn, job_id),
            snapshots=queries.snapshot_count(conn, job_id),
            freshness_why=freshness_explanation(job["freshness"]),
            fit=fit_rows, fit_band=fit_band, fit_why=fit_why,
            gap_actions=gaps, alignment=alignment, pref_mismatches=prefs_mismatch,
            capabilities=caps,
            tracked=queries.current_application_status(conn, ukey, job_id),
        ),
    )


@app.post("/jobs/{job_id}/feedback")
def job_feedback(job_id: int, kind: str = Form(...), note: str = Form("")):
    if kind not in ("stale", "wrong_role", "wrong_data"):
        kind = "wrong_data"
    with write_txn() as conn:
        conn.execute(
            "INSERT INTO feedback (job_id, kind, note, created_at) VALUES (?,?,?,datetime('now'))",
            (job_id, kind, collapse(note)[:500]),
        )
    return RedirectResponse(f"/jobs/{job_id}?feedback=thanks", status_code=303)


@app.post("/jobs/{job_id}/track")
def track(request: Request, job_id: int, status: str = Form(...), note: str = Form(""), resume_version: str = Form("")):
    conn = db()
    if queries.get_job(conn, job_id) is None:
        return RedirectResponse("/jobs", status_code=303)
    try:
        with write_txn() as wconn:
            queries.track_job(wconn, user_key_for(request), job_id, status, note, resume_version, commit=False)
    except ValueError:
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}?tracked={status}", status_code=303)


@app.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request):
    conn = db()
    ukey = user_key_for(request)
    jobs, timelines = queries.applications(conn, ukey)
    order = {s: i for i, s in enumerate(["offer", "interview", "assessment",
                                         "applied", "saved", "withdrawn",
                                         "rejected", "closed"])}
    jobs = sorted(jobs, key=lambda j: (order.get(j["status"], 9), -(j["id"])))
    obs = queries.progress_observations(conn, ukey)
    return templates.TemplateResponse(
        request=request, name="applications.html",
        context=ctx(request, jobs=jobs, timelines=timelines,
                    statuses=queries.STATUSES, observations=obs),
    )


# ---------------------------------------------------------------- roles / voting


@app.get("/roles", response_class=HTMLResponse)
def roles(request: Request):
    conn = db()
    return templates.TemplateResponse(
        request=request, name="roles.html",
        context=ctx(request, families=queries.families_with_counts(conn),
                    requests=queries.role_requests(conn),
                    threshold=VOTE_REVIEW_THRESHOLD,
                    matrix=EXPERIENCE_MATRIX),
    )


@app.post("/roles/request")
def role_request(role_name: str = Form(...)):
    name = collapse(role_name)[:80]
    if name:
        with write_txn() as conn:
            conn.execute(
                "INSERT INTO role_request (role_name, created_at) VALUES (?, datetime('now')) "
                "ON CONFLICT(role_name) DO NOTHING",
                (name,),
            )
    return RedirectResponse("/roles", status_code=303)


@app.post("/roles/{request_id}/vote")
def role_vote(request: Request, request_id: int):
    conn = db()
    exists = conn.execute(
        "SELECT 1 FROM role_request WHERE id = ?", (request_id,)
    ).fetchone()
    if exists is None:
        return RedirectResponse("/roles", status_code=303)  # stale vote target: no-op
    with write_txn() as conn:
        conn.execute(
            "INSERT INTO role_vote (request_id, voter_hash, created_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(request_id, voter_hash) DO NOTHING",
            (request_id, voter_hash(request)),
        )
    return RedirectResponse("/roles", status_code=303)


# ---------------------------------------------------------------- accounts (Phase 2)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, msg: str = ""):
    return templates.TemplateResponse(request=request, name="login.html",
                                      context=ctx(request, msg=msg))


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = db()
    token, err = auth_login(conn, username, password)
    if err:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context=ctx(request, msg=err), status_code=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("co_session", token, max_age=60 * 60 * 24 * 30, httponly=True)
    return response


@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = db()
    uid, err = auth_register(conn, username, password)
    if err:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context=ctx(request, msg=err), status_code=422)
    token, _ = auth_login(conn, username, password)
    response = RedirectResponse("/profile", status_code=303)
    response.set_cookie("co_session", token, max_age=60 * 60 * 24 * 30, httponly=True)
    return response


@app.post("/logout")
def do_logout(request: Request):
    conn = db()
    auth_logout(conn, request.cookies.get("co_session", ""))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("co_session")
    return response


# ---------------------------------------------------------------- candidate profile (Module C)


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    conn = db()
    ukey = user_key_for(request)
    prof = queries.latest_profile(conn, ukey)
    skills = repos = []
    affinity = []
    repo_flags = {}
    education = reviews = portfolios = prefs = recency = []
    if prof:
        skills = queries.profile_skills(conn, prof["id"])
        repos = queries.profile_repos(conn, prof["id"])
        affinity = queries.profile_family_affinity(conn, prof["id"])
        education = queries.profile_education(conn, prof["id"])
        reviews = queries.profile_reviews(conn, prof["id"])
        portfolios = queries.profile_portfolios(conn, prof["id"])
        prefs = queries.profile_prefs(conn, prof) or {}
        recency = queries.profile_recency(conn, prof["id"])
        for r in repos:
            repo_flags[r["name"]] = _repo_flag(r)
    return templates.TemplateResponse(
        request=request, name="profile.html",
        context=ctx(request, profile=prof, skills=skills, repos=repos,
                    affinity=affinity, repo_flags=repo_flags,
                    education=education, reviews=reviews, portfolios=portfolios,
                    prefs=prefs, recency=recency,
                    depth_order=["production use", "independent build",
                                 "working use", "repo evidence",
                                 "listed in skills section", "mentioned", "exposure"]),
    )


def _repo_flag(repo) -> str:
    if repo["fork"]:
        return "fork"
    pushed = repo["pushed_at"] or ""
    recent = pushed >= "2026-03"
    if recent and repo["description"]:
        return "active-original"
    if not recent:
        return "stale"
    return "original"


def _store_profile(conn, ukey: str, raw_text: str, source: str, github_user: str = ""):
    from app.resume import (fetch_portfolio, github_repos_for, parse_education,
                            parse_resume, skills_from_repos)

    # children first — deleting the parent row while skills/repos/etc. still
    # reference it fails the FK constraint (re-upload used to 500 here)
    for child in ("candidate_skill", "candidate_github_repo", "candidate_education",
                  "candidate_review", "candidate_portfolio"):
        conn.execute(
            f"DELETE FROM {child} WHERE profile_id IN "
            f"(SELECT id FROM candidate_profile WHERE user_key = ?)", (ukey,))
    conn.execute("DELETE FROM candidate_profile WHERE user_key = ?", (ukey,))
    now = datetime.now().isoformat(timespec="seconds")
    github_json, github_error, repos = None, None, []
    if github_user:
        try:
            data = github_repos_for(github_user)
            repos = data["repos"]
            github_json = json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            github_error = str(exc)
    cur = conn.execute(
        "INSERT INTO candidate_profile (user_key, raw_text, source, github_user, github_json, "
        "github_error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (ukey, raw_text, source, github_user, github_json, github_error, now, now),
    )
    pid = cur.lastrowid
    claims = parse_resume(raw_text)
    for skill, items in claims.items():
        for c in items:
            conn.execute(
                "INSERT OR IGNORE INTO candidate_skill (profile_id, skill, section, "
                "evidence, depth, confidence, origin) VALUES (?,?,?,?,?,?, 'resume')",
                (pid, skill, c["section"], c["evidence"], c["depth"], c["confidence"]),
            )
    from app.resume import skill_recency
    for skill, year in skill_recency(claims).items():
        conn.execute(
            "UPDATE candidate_skill SET section = section || CASE WHEN section LIKE '%last evidenced%' "
            "THEN '' ELSE ' · last evidenced ' || ? END WHERE profile_id = ? AND skill = ?",
            (str(year), pid, skill),
        )
    for edu in parse_education(raw_text):
        conn.execute(
            "INSERT OR IGNORE INTO candidate_education (profile_id, degree, institution, "
            "dates, years, classification) VALUES (?,?,?,?,?,?)",
            (pid, edu["degree"], edu["institution"], edu["years"], edu["years"], edu["classification"]),
        )
    for skill, items in skills_from_repos(repos).items():
        for c in items:
            conn.execute(
                "INSERT OR IGNORE INTO candidate_skill (profile_id, skill, section, "
                "evidence, depth, confidence, origin) VALUES (?,?,?,?,?,?, 'github')",
                (pid, skill, "github", c["evidence"], c["depth"], c["confidence"]),
            )
    for repo in repos:
        conn.execute(
            "INSERT OR IGNORE INTO candidate_github_repo (profile_id, name, description, "
            "language, stars, pushed_at, url, topics, fork) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, repo["name"], repo["description"], repo["language"], repo["stars"],
             repo["pushed_at"], repo["url"], json.dumps(repo["topics"]), 1 if repo["fork"] else 0),
        )
    conn.commit()
    return pid


@app.post("/profile/upload")
async def profile_upload(
    request: Request,
    resume_file: UploadFile = None,
    resume_text: str = Form(""),
    github_user: str = Form(""),
    band: str = Form(""),
    work_pref: str = Form(""),
    loc_pref: str = Form(""),
):
    raw = (resume_text or "").strip()
    source = "paste"
    if resume_file is not None and resume_file.filename:
        blob = await resume_file.read()
        name = resume_file.filename.lower()
        if name.endswith(".pdf"):
            try:
                raw = extract_pdf_text(blob)
            except Exception as exc:
                return templates.TemplateResponse(
                    request=request, name="error.html",
                    context=ctx(request, code=422, message=f"Could not read that PDF: {exc}"),
                    status_code=422)
            source = f"upload:{resume_file.filename}"
        elif name.endswith((".txt", ".md", ".tex")):
            raw = blob.decode("utf-8", errors="replace")
            source = f"upload:{resume_file.filename}"
        else:
            return templates.TemplateResponse(
                request=request, name="error.html",
                context=ctx(request, code=422,
                            message="Unsupported file type. Paste the text, or upload .pdf / .txt / .md / .tex."),
                status_code=422)
    ukey = user_key_for(request)
    if not raw:
        # No resume given: still allow preference-only updates on an existing profile.
        conn = db()
        prof = queries.latest_profile(conn, ukey)
        if prof is None and not github_user:
            return templates.TemplateResponse(
                request=request, name="error.html",
                context=ctx(request, code=422,
                            message="Nothing to build a profile from — paste your resume text or upload a file."),
                status_code=422)
        if prof is not None:
            _save_prefs(conn, prof, band, work_pref, loc_pref)
            return RedirectResponse("/profile", status_code=303)
    with write_txn() as wconn:
        _store_profile(wconn, ukey, raw, source, (github_user or "").strip().lstrip("@"))
    conn = db()
    prof = queries.latest_profile(conn, ukey)
    _save_prefs(conn, prof, band, work_pref, loc_pref)
    return RedirectResponse("/profile", status_code=303)


def _save_prefs(conn, prof, band: str, work_pref: str, loc_pref: str):
    if prof is None:
        return
    import json as _json
    band_vals = None
    if band in queries.BANDS:
        band_vals = list(queries.BANDS[band])
    works = [w.strip().lower() for w in (work_pref or "").split(",") if w.strip()]
    locs = [l.strip() for l in (loc_pref or "").split(",") if l.strip()]
    prefs = {"band": band_vals, "work_models": works, "locations": locs}
    # Preferences live in the review table under a reserved key — visible in export.
    conn.execute("DELETE FROM candidate_review WHERE profile_id = ? AND skill = '__prefs__'", (prof["id"],))
    conn.execute(
        "INSERT INTO candidate_review (profile_id, skill, verdict, note, created_at) "
        "VALUES (?, '__prefs__', 'prefs', ?, datetime('now'))",
        (prof["id"], _json.dumps(prefs)),
    )
    conn.commit()


@app.post("/profile/review")
def profile_review(request: Request, skill: str = Form(...), verdict: str = Form(...), note: str = Form("")):
    conn = db()
    ukey = user_key_for(request)
    prof = queries.latest_profile(conn, ukey)
    if prof is None or verdict not in ("confirmed", "rejected"):
        return RedirectResponse("/profile", status_code=303)
    with write_txn() as wconn:
        wconn.execute(
            "INSERT INTO candidate_review (profile_id, skill, verdict, note, created_at) "
            "VALUES (?,?,?,?,datetime('now')) "
            "ON CONFLICT(profile_id, skill) DO UPDATE SET verdict = excluded.verdict, note = excluded.note",
            (prof["id"], skill, verdict, collapse(note)[:300]),
        )
    return RedirectResponse("/profile#review", status_code=303)


@app.post("/profile/portfolio")
def add_portfolio(request: Request, url: str = Form(...)):
    from app.resume import fetch_portfolio

    conn = db()
    ukey = user_key_for(request)
    prof = queries.latest_profile(conn, ukey)
    if prof is None:
        return RedirectResponse("/profile", status_code=303)
    error, title, skills_json = "", "", "[]"
    try:
        data = fetch_portfolio(url.strip())
        title = data["title"]
        skills_json = json.dumps(data["skills"])
        with write_txn() as wconn:
            for skill in data["skills"]:
                wconn.execute(
                    "INSERT OR IGNORE INTO candidate_skill (profile_id, skill, section, evidence, "
                    "depth, confidence, origin) VALUES (?,?,?,?,?,?, 'portfolio')",
                    (prof["id"], skill, "portfolio", url.strip(), "portfolio mention", 0.45),
                )
    except Exception as exc:
        error = str(exc)
    with write_txn() as wconn:
        wconn.execute(
            "INSERT INTO candidate_portfolio (profile_id, url, title, skills_json, error, fetched_at) "
            "VALUES (?,?,?,?,?,datetime('now'))",
            (prof["id"], url.strip()[:300], title[:160], skills_json, error[:300]),
        )
    return RedirectResponse("/profile#portfolio", status_code=303)


@app.get("/profile/export")
def profile_export(request: Request):
    """DPDP-style data export: everything we hold about this user, as JSON."""
    conn = db()
    ukey = user_key_for(request)
    prof = queries.latest_profile(conn, ukey)
    if prof is None:
        return JSONResponse({"user": ukey, "profile": None})
    jobs, timelines = queries.applications(conn, ukey)
    payload = {
        "user": ukey,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "profile": {
            "source": prof["source"], "github_user": prof["github_user"],
            "created_at": prof["created_at"],
        },
        "skills": [dict(s) for s in queries.profile_skills(conn, prof["id"])],
        "education": [dict(e) for e in queries.profile_education(conn, prof["id"])],
        "github_repos": [dict(r) for r in queries.profile_repos(conn, prof["id"])],
        "reviews": [dict(r) for r in queries.profile_reviews(conn, prof["id"])],
        "applications": [dict(j) for j in jobs],
        "application_timelines": {str(k): [dict(e) for e in v] for k, v in timelines.items()},
        "saved_searches": [dict(s) for s in queries.saved_searches(conn, ukey)],
    }
    return JSONResponse(payload)


@app.post("/profile/delete")
def profile_delete(request: Request):
    conn = db()
    ukey = user_key_for(request)
    prof = queries.latest_profile(conn, ukey)
    with write_txn() as wconn:
        if prof:
            # children before parent — FK constraint fails otherwise
            for child in ("candidate_skill", "candidate_github_repo", "candidate_education",
                          "candidate_review", "candidate_portfolio"):
                wconn.execute(f"DELETE FROM {child} WHERE profile_id = ?", (prof["id"],))
            wconn.execute("DELETE FROM candidate_profile WHERE id = ?", (prof["id"],))
        wconn.execute("DELETE FROM application_event WHERE user_key = ?", (ukey,))
    return RedirectResponse("/profile", status_code=303)


# ---------------------------------------------------------------- alerts (Module A leftover)


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request):
    conn = db()
    ukey = user_key_for(request)
    items = []
    for s in queries.saved_searches(conn, ukey):
        params = dict(p.split("=", 1) for p in s["query_string"].split("&") if "=" in p)
        rows, total = queries.query_jobs(
            conn, family=params.get("family") or None, band=params.get("band") or None,
            work=params.get("work") or None, loc=params.get("loc") or None,
            q=params.get("q") or None, page=1, per_page=200)
        since = s["last_checked"] or "2000-01-01"
        fresh = [r for r in rows if r["first_seen"] > since]
        items.append({"search": s, "total": total, "new": len(fresh),
                      "new_jobs": fresh[:5]})
    return templates.TemplateResponse(
        request=request, name="alerts.html",
        context=ctx(request, items=items, telegram_url=TELEGRAM_URL),
    )


@app.post("/alerts/save")
def save_alert(request: Request, name: str = Form(...), query_string: str = Form(...)):
    conn = db()
    ukey = user_key_for(request)
    with write_txn() as wconn:
        wconn.execute(
            "INSERT INTO saved_search (user_key, name, query_string, created_at) VALUES (?,?,?,datetime('now'))",
            (ukey, collapse(name)[:60], query_string[:300]),
        )
    return RedirectResponse("/alerts", status_code=303)


@app.post("/alerts/{search_id}/check")
def check_alert(search_id: int):
    with write_txn() as wconn:
        wconn.execute(
            "UPDATE saved_search SET last_checked = date('now','localtime') WHERE id = ?",
            (search_id,),
        )
    return RedirectResponse("/alerts", status_code=303)


@app.post("/alerts/{search_id}/delete")
def delete_alert(search_id: int):
    with write_txn() as wconn:
        wconn.execute("DELETE FROM saved_search WHERE id = ?", (search_id,))
    return RedirectResponse("/alerts", status_code=303)


# ---------------------------------------------------------------- agent (Module H, deterministic)


@app.get("/agent", response_class=HTMLResponse)
def agent_page(request: Request, q: str = ""):
    conn = db()
    ukey = user_key_for(request)
    answer = None
    if q:
        with write_txn() as wconn:
            wconn.execute(
                "INSERT INTO page_event (kind, detail, created_at) VALUES ('agent_query', ?, datetime('now','localtime'))",
                (q[:200],),
            )
        answer = answer_query(conn, ukey, q)
    return templates.TemplateResponse(
        request=request, name="agent.html",
        context=ctx(request, q=q, answer=answer),
    )


def answer_query(conn, ukey: str, q: str) -> dict:
    """Deterministic structured agent: routes a question to a real query over
    the platform's data and returns a cited, evidence-level answer. It never
    predicts hiring outcomes and says so when asked."""
    from app import queries as Q
    low = q.lower()
    prof = Q.latest_profile(conn, ukey)

    def needs_profile():
        return None if prof else {"title": "Build your profile first",
                                  "body": ["This answer needs a candidate profile. Add your resume on the Profile page — it takes one upload."],
                                  "links": [("/profile", "Build profile")]}

    # family gap question
    fam_slug, fam_name = None, None
    for f in Q.families_with_counts(conn):
        if f["name"].lower().split()[0].rstrip(",") in low or f["slug"].replace("-", " ") in low:
            fam_slug, fam_name = f["slug"], f["name"]
            break
    if fam_slug and any(w in low for w in ("gap", "missing", "lack", "need", "improve")):
        p = needs_profile()
        if p:
            return p
        skills = Q.profile_skills(conn, prof["id"])
        owned = {s["skill"] for s in skills}
        demand = conn.execute(
            "SELECT js.skill, COUNT(DISTINCT js.job_id) AS n FROM job_skill js "
            "JOIN job j ON j.id = js.job_id "
            "WHERE j.role_family_id = (SELECT id FROM role_family WHERE slug = ?) "
            "AND j.freshness IN ('active','likely_active') GROUP BY js.skill ORDER BY n DESC LIMIT 40",
            (fam_slug,),
        ).fetchall()
        total_jobs = conn.execute(
            "SELECT COUNT(*) AS n FROM job WHERE role_family_id = "
            "(SELECT id FROM role_family WHERE slug = ?) AND freshness IN ('active','likely_active')",
            (fam_slug,),
        ).fetchone()["n"] or 1
        missing = [(r["skill"], round(100 * r["n"] / total_jobs))
                   for r in demand if r["skill"] not in owned][:8]
        covered = [(r["skill"], round(100 * r["n"] / total_jobs))
                   for r in demand if r["skill"] in owned][:5]
        body = []
        if covered:
            body.append(f"Your evidence already covers the market's most-demanded skills: "
                        + ", ".join(f"{s} ({p}% of postings)" for s, p in covered[:5]) + ".")
        if missing:
            body.append("Biggest gaps, ranked by how often live postings demand them: "
                        + ", ".join(f"{s} ({p}%)" for s, p in missing[:6]) + ".")
            body.append("Advice: close the high-percentage gaps first — they unlock the "
                        "largest share of postings per unit of effort. A small real project "
                        "per skill beats any keyword change.")
        else:
            body.append("You have no gaps among the family's top-demanded skills.")
        return {"title": f"Your gaps for {fam_name}",
                "body": body,
                "links": [(f"/jobs?family={fam_slug}", f"Browse {fam_name} jobs"),
                          ("/profile", "Your evidence")]}

    if any(w in low for w in ("fit", "best job", "match", "recommend")):
        p = needs_profile()
        if p:
            return p
        from app import queries as Q2
        from app.fit import evaluate as ev2
        skills = Q2.profile_skills(conn, prof["id"])
        rows, _ = Q2.query_jobs(conn, page=1, per_page=60,
                                freshness="active")
        scored = []
        for j in rows:
            if not j["role_family_id"]:
                continue
            e = ev2(conn, j, skills, user_band=Q2.profile_band(conn, prof))
            req = e["counts"]["required"] or 1
            scored.append((e["counts"]["matched"] / req, e["band"], j))
        scored.sort(key=lambda t: -t[0])
        body = [f"{j['title']} at {j['company']} — {band} ({m.get('why', '')})"
                for m, band, j in scored[:5]]
        return {"title": "Best current fits for your evidence",
                "body": body or ["No active jobs to evaluate yet — run ingestion first."],
                "links": [("/jobs", "Browse the feed")]}

    if any(w in low for w in ("application", "pipeline", "status", "tracker", "progress")):
        jobs, timelines = Q.applications(conn, ukey)
        if not jobs:
            return {"title": "Application status",
                    "body": ["Nothing tracked yet. Open a job and use Track this job."],
                    "links": [("/jobs", "Browse jobs"), ("/applications", "Tracker")]}
        by = {}
        for j in jobs:
            by.setdefault(j["status"], []).append(j["title"])
        body = [f"{len(titles)} at {stage}: " + "; ".join(titles[:3]) for stage, titles in by.items()]
        return {"title": "Your application status",
                "body": body, "links": [("/applications", "Open tracker")]}

    if any(w in low for w in ("week", "do next", "action", "plan", "priority")):
        p = needs_profile()
        if p:
            return p
        skills = Q.profile_skills(conn, prof["id"])
        demand = conn.execute(
            "SELECT js.skill, COUNT(DISTINCT js.job_id) AS n FROM job_skill js "
            "JOIN job j ON j.id = js.job_id "
            "WHERE j.freshness IN ('active','likely_active') GROUP BY js.skill ORDER BY n DESC LIMIT 25"
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM job WHERE freshness IN ('active','likely_active')"
        ).fetchone()["n"] or 1
        owned = {s["skill"] for s in skills}
        top = next(((r["skill"], round(100 * r["n"] / total)) for r in demand if r["skill"] not in owned), None)
        body = []
        if top:
            body.append(f"1. Highest-leverage gap market-wide: {top[0]} — asked by {top[1]}% of all live postings. Build one real project around it.")
        body.append("2. Check /alerts for new jobs matching your saved searches since your last visit.")
        jobs, _ = Q.applications(conn, ukey)
        stale = [j for j in jobs if j["status"] in ("applied", "assessment") and j["status_at"] < "2026-09-05"]
        if stale:
            body.append(f"3. {len(stale)} application(s) have sat without movement for ~2 weeks: "
                        + "; ".join(j["title"] for j in stale[:3]) + ". A polite follow-up is reasonable.")
        if len(body) == 1:
            body.append("2. Track your next application in the tracker so progress observations can start.")
        return {"title": "What to do this week",
                "body": body, "links": [("/jobs", "Browse jobs"), ("/alerts", "Alerts")]}

    if any(w in low for w in ("hire", "chance", "probability", "will i get")):
        return {"title": "I don't predict hiring outcomes",
                "body": ["No system can honestly tell you whether you'll be hired — it depends "
                         "on the applicant pool, the process and factors nobody outside can see.",
                         "What I can do: show which requirements your evidence covers, which "
                         "gaps are worth closing, and how common each ask is across live postings."],
                "links": [("/jobs", "Try a job's evidence view")]}

    return {"title": "I can answer questions like these",
            "body": ["What are my gaps for GenAI Engineer / Data Engineer / … ?",
                     "What are the best current fits for me?",
                     "What is my application status?",
                     "What should I do this week?",
                     "Will I get hired? (ask me why the answer is no)"],
            "links": [("/jobs", "Browse jobs"), ("/profile", "Build profile")]}


# ---------------------------------------------------------------- ops (metrics)


@app.get("/ops", response_class=HTMLResponse)
def ops(request: Request):
    conn = db()
    return templates.TemplateResponse(
        request=request, name="ops.html",
        context=ctx(request, ops=queries.ops_dashboard(conn),
                    metrics=queries.product_metrics(conn),
                    stats=queries.platform_stats(conn)),
    )
