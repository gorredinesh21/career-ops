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
            fit=queries.job_fit_for_profile(db(), job_id, prof["id"]) if prof else None,
        ),
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
    families = queries.families_with_counts(db())
    requests_list = queries.role_requests(db())
    return templates.TemplateResponse(
        request=request, name="roles.html",
        context=ctx(request, families=families, requests=requests_list,
                    threshold=VOTE_REVIEW_THRESHOLD),
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


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    conn = db()
    prof = queries.latest_profile(conn)
    skills = repos = []
    affinity = []
    if prof:
        skills = queries.profile_skills(conn, prof["id"])
        repos = queries.profile_repos(conn, prof["id"])
        affinity = queries.profile_family_affinity(conn, prof["id"])
    return templates.TemplateResponse(
        request=request, name="profile.html",
        context=ctx(request, profile=prof, skills=skills, repos=repos,
                    affinity=affinity,
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
