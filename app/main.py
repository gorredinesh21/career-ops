"""Career-Ops web application (Phase 1: job intelligence + discovery)."""
import hashlib
import json
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import queries
from app.db import get_db
from app.freshness import freshness_explanation
from app.normalize import collapse
from app.resume import extract_pdf_text

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

VOTE_REVIEW_THRESHOLD = 25  # transparent per BRD Module B

# Experience-aware role model (BRD Module D): product-level reference
# dimensions, not an industry standard. Bands are context, not proof.
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


def db():
    """Per-request connection (FastAPI runs sync routes in a threadpool)."""
    return get_db()


@app.middleware("http")
async def voter_cookie(request: Request, call_next):
    response = await call_next(request)
    if not request.cookies.get("voter_id"):
        response.set_cookie("voter_id", uuid.uuid4().hex, max_age=60 * 60 * 24 * 365)
    return response


def voter_hash(request: Request) -> str:
    vid = request.cookies.get("voter_id", request.client.host if request.client else "anon")
    return hashlib.sha256(vid.encode()).hexdigest()[:16]


def ctx(request: Request, **kw) -> dict:
    base = {
        "request": request,
        "today": date.today().isoformat(),
        "bands": list(queries.BANDS.keys()),
    }
    base.update(kw)
    return base


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


@app.get("/healthz")
def healthz():
    stats = queries.platform_stats(db())
    return JSONResponse({"ok": True, **stats})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    stats = queries.platform_stats(db())
    families = queries.families_with_counts(db())
    latest, _ = queries.query_jobs(db(), page=1, per_page=5)
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
    rows, total = queries.query_jobs(
        db(), family=family or None, band=band or None, work=work or None,
        loc=loc or None, q=q or None, freshness=freshness or None, page=page,
    )
    families = queries.families_with_counts(db())
    query_params = {k: v for k, v in dict(
        family=family, band=band, work=work, loc=loc, q=q, freshness=freshness
    ).items() if v}
    pages = max(1, -(-total // 25))
    return templates.TemplateResponse(
        request=request, name="jobs.html",
        context=ctx(
            request, jobs=rows, total=total, families=families,
            filters=query_params, page=page, pages=pages,
        ),
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    conn = db()
    job = queries.get_job(conn, job_id)
    prof = queries.latest_profile(conn)
    if job is None:
        return templates.TemplateResponse(
            request=request, name="error.html",
            context=ctx(request, code=404, message="No such job."),
            status_code=404,
        )
    fit = None
    fit_band = fit_why = None
    gap_actions = None
    profile_skills = []
    if prof:
        profile_skills = queries.profile_skills(conn, prof["id"])
        fit = queries.job_fit_for_profile(db(), job_id, prof["id"])
        if fit:
            fit_band, fit_why = queries.fit_band(fit)
            gap_actions = queries.gap_actions(fit, profile_skills)
    return templates.TemplateResponse(
        request=request, name="job_detail.html",
        context=ctx(
            request,
            job=job,
            sources=queries.job_sources(db(), job_id),
            skills=queries.job_skills(db(), job_id),
            feedback=queries.job_feedback(db(), job_id),
            snapshots=queries.snapshot_count(db(), job_id),
            freshness_why=freshness_explanation(job["freshness"]),
            fit=fit,
            fit_band=fit_band,
            fit_why=fit_why,
            gap_actions=gap_actions,
            profile_skills=profile_skills,
        ),
    )


@app.post("/jobs/{job_id}/track")
def track(job_id: int, status: str = Form(...), note: str = Form(""), resume_version: str = Form("")):
    conn = db()
    if queries.get_job(conn, job_id) is None:
        return RedirectResponse("/jobs", status_code=303)
    try:
        queries.track_job(conn, job_id, status, note, resume_version)
    except ValueError:
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}?tracked={status}", status_code=303)


@app.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request):
    conn = db()
    jobs, timelines = queries.applications(conn)
    # newest-relevant first: offers/interviews above rejections
    order = {s: i for i, s in enumerate(["offer", "interview", "assessment",
                                         "applied", "saved", "withdrawn",
                                         "rejected", "closed"])}
    jobs = sorted(jobs, key=lambda j: (order.get(j["status"], 9), -(j["id"])))
    return templates.TemplateResponse(
        request=request, name="applications.html",
        context=ctx(request, jobs=jobs, timelines=timelines,
                    statuses=queries.STATUSES),
    )


@app.post("/jobs/{job_id}/feedback")
def job_feedback(job_id: int, kind: str = Form(...), note: str = Form("")):
    if kind not in ("stale", "wrong_role", "wrong_data"):
        kind = "wrong_data"
    conn = db()
    conn.execute(
        "INSERT INTO feedback (job_id, kind, note, created_at) VALUES (?,?,?,datetime('now'))",
        (job_id, kind, collapse(note)[:500]),
    )
    conn.commit()
    return RedirectResponse(f"/jobs/{job_id}?feedback=thanks", status_code=303)


@app.get("/roles", response_class=HTMLResponse)
def roles(request: Request):
    conn = db()
    families = queries.families_with_counts(conn)
    requests_list = queries.role_requests(conn)
    return templates.TemplateResponse(
        request=request, name="roles.html",
        context=ctx(request, families=families, requests=requests_list,
                    threshold=VOTE_REVIEW_THRESHOLD,
                    matrix=EXPERIENCE_MATRIX),
    )


@app.post("/roles/request")
def role_request(role_name: str = Form(...)):
    name = collapse(role_name)[:80]
    if name:
        conn = db()
        conn.execute(
            "INSERT INTO role_request (role_name, created_at) VALUES (?, datetime('now')) "
            "ON CONFLICT(role_name) DO NOTHING",
            (name,),
        )
        conn.commit()
    return RedirectResponse("/roles", status_code=303)


@app.post("/roles/{request_id}/vote")
def role_vote(request: Request, request_id: int):
    conn = db()
    conn.execute(
        "INSERT INTO role_vote (request_id, voter_hash, created_at) VALUES (?,?,datetime('now')) "
        "ON CONFLICT(request_id, voter_hash) DO NOTHING",
        (request_id, voter_hash(request)),
    )
    conn.commit()
    return RedirectResponse("/roles", status_code=303)


@app.get("/ops", response_class=HTMLResponse)
def ops(request: Request):
    return templates.TemplateResponse(
        request=request, name="ops.html",
        context=ctx(request, ops=queries.ops_dashboard(db()),
                    stats=queries.platform_stats(db())),
    )


# ---------------------------------------------------------------- candidate profile (Module C v1)


def _repo_flag(repo) -> str:
    """Deterministic originality signal (BRD 8.2): forks flagged, recency and
    description noted. Stars are never a proficiency signal."""
    if repo["fork"]:
        return "fork"
    pushed = repo["pushed_at"] or ""
    recent = pushed >= "2026-03"  # ~6 months
    if recent and repo["description"]:
        return "active-original"
    if not recent:
        return "stale"
    return "original"


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    conn = db()
    prof = queries.latest_profile(conn)
    skills = repos = []
    affinity = []
    repo_flags = {}
    if prof:
        skills = queries.profile_skills(conn, prof["id"])
        repos = queries.profile_repos(conn, prof["id"])
        affinity = queries.profile_family_affinity(conn, prof["id"])
        for r in repos:
            repo_flags[r["name"]] = _repo_flag(r)
    return templates.TemplateResponse(
        request=request, name="profile.html",
        context=ctx(request, profile=prof, skills=skills, repos=repos,
                    affinity=affinity, repo_flags=repo_flags,
                    depth_order=["production use", "independent build",
                                 "working use", "repo evidence",
                                 "listed in skills section", "mentioned", "exposure"]),
    )


def _store_profile(conn, raw_text: str, source: str, github_user: str = ""):
    from app.resume import parse_resume, github_repos_for, skills_from_repos

    conn.execute("DELETE FROM candidate_profile")
    conn.execute("DELETE FROM candidate_skill")
    conn.execute("DELETE FROM candidate_github_repo")
    now = datetime.now().isoformat(timespec="seconds")
    github_json, github_error = None, None
    repos = []
    if github_user:
        try:
            data = github_repos_for(github_user)
            repos = data["repos"]
            github_json = json.dumps(data, ensure_ascii=False)
        except Exception as exc:  # visible, honest failure — no silent skip
            github_error = str(exc)
    cur = conn.execute(
        "INSERT INTO candidate_profile (raw_text, source, github_user, github_json, "
        "github_error, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (raw_text, source, github_user, github_json, github_error, now, now),
    )
    pid = cur.lastrowid
    for skill, claims in parse_resume(raw_text).items():
        for c in claims:
            conn.execute(
                "INSERT OR IGNORE INTO candidate_skill (profile_id, skill, section, "
                "evidence, depth, confidence, origin) VALUES (?,?,?,?,?,?, 'resume')",
                (pid, skill, c["section"], c["evidence"], c["depth"], c["confidence"]),
            )
    for skill, claims in skills_from_repos(repos).items():
        for c in claims:
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


@app.post("/profile/upload")
async def profile_upload(
    request: Request,
    resume_file: UploadFile = None,
    resume_text: str = Form(""),
    github_user: str = Form(""),
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
                    context=ctx(request, code=422,
                                message=f"Could not read that PDF: {exc}"),
                    status_code=422,
                )
            source = f"upload:{resume_file.filename}"
        elif name.endswith((".txt", ".md", ".tex")):
            raw = blob.decode("utf-8", errors="replace")
            source = f"upload:{resume_file.filename}"
        else:
            return templates.TemplateResponse(
                request=request, name="error.html",
                context=ctx(request, code=422,
                            message="Unsupported file type. Paste the text, or upload .pdf / .txt / .md / .tex."),
                status_code=422,
            )
    if not raw and not github_user:
        return templates.TemplateResponse(
            request=request, name="error.html",
            context=ctx(request, code=422,
                        message="Nothing to build a profile from — paste your resume text or upload a file."),
            status_code=422,
        )
    conn = db()
    _store_profile(conn, raw, source, (github_user or "").strip().lstrip("@"))
    return RedirectResponse("/profile", status_code=303)


@app.post("/profile/delete")
def profile_delete():
    conn = db()
    conn.execute("DELETE FROM candidate_profile")
    conn.execute("DELETE FROM candidate_skill")
    conn.execute("DELETE FROM candidate_github_repo")
    conn.commit()
    return RedirectResponse("/profile", status_code=303)
