"""Ingestion pipeline: raw pipeline files -> canonical Job Store.

Sources (all real, from the existing daily job pipelines):
1. Alert detail JSONL files   ~/Desktop/LinkedIn Daily/<date>/alert*_details.jsonl
   (cardText + full panel description per LinkedIn job id)
2. Cross-day ledger           ~/Desktop/LinkedIn Daily/job_history.jsonl
   (every job ever seen with firstSeen/lastSeen — drives freshness)
3. Curated feed jobs          qualifying_jobs.json from the feed scan
   (company/role/loc/exp/ctc/apply + curator notes; description stays empty,
   notes are stored separately and rendered as curator notes, not as the
   original posting — BRD: never invent requirements)

Idempotent: re-running updates last_seen, merges duplicate sources, appends
job snapshots when the captured text changed.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from app.classify import classify, extract_skills
from app.db import connect, init_db
from app.freshness import compute_freshness
from app.normalize import (
    collapse, extract_city, normalize_card, normalize_ledger_title,
    parse_applicants, parse_experience, parse_salary, parse_work_model,
    slugify,
)

DEFAULT_LEDGER = Path.home() / "Desktop" / "LinkedIn Daily" / "job_history.jsonl"
DEFAULT_DAILY_GLOB = Path.home() / "Desktop" / "LinkedIn Daily"
DEFAULT_QUALIFYING = (
    Path.home() / ".zcode" / "workspace" / "default"
    / "linkedin-feed-jobs-2026-09-18" / "qualifying_jobs.json"
)

FUZZY_TITLE_JACCARD = 0.6


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def canonical_key(company: str, title: str, city: str, external_id: str = "") -> str:
    if company and title:
        return f"{slugify(company)}|{slugify(title)}|{slugify(city)}"
    # Not enough structure to claim a canonical identity — key by external id.
    return f"ext|{external_id or slugify(title)}"


def title_tokens(title: str) -> set:
    stop = {"engineer", "developer", "senior", "junior", "software", "the", "and", "of", "a"}
    return {t for t in slugify(title).split("-") if t and t not in stop}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class Ingestor:
    def __init__(self, conn):
        self.conn = conn
        self.stats = {
            "jobs_seen": 0, "jobs_new": 0, "jobs_updated": 0,
            "sources_merged": 0, "snapshots_added": 0, "errors": [],
        }

    # -- low-level helpers ---------------------------------------------------

    def find_by_key(self, key: str):
        return self.conn.execute(
            "SELECT * FROM job WHERE canonical_key = ?", (key,)
        ).fetchone()

    def find_fuzzy(self, company: str, title: str, city: str):
        """Same company + city + overlapping title tokens >= threshold."""
        if not company:
            return None
        rows = self.conn.execute(
            "SELECT * FROM job WHERE lower(company) = ?", (company.lower(),)
        ).fetchall()
        target = title_tokens(title)
        best, best_score = None, 0.0
        for row in rows:
            if city and row["city"] and slugify(city) != slugify(row["city"]):
                continue
            score = jaccard(target, title_tokens(row["title"]))
            if score > best_score:
                best, best_score = row, score
        if best is not None and best_score >= FUZZY_TITLE_JACCARD:
            return best
        return None

    def find_by_external(self, source_type: str, external_id: str):
        if not external_id:
            return None
        row = self.conn.execute(
            "SELECT j.* FROM job j JOIN job_source s ON s.job_id = j.id "
            "WHERE s.source_type = ? AND s.external_id = ?",
            (source_type, external_id),
        ).fetchone()
        return row

    def add_snapshot_if_changed(self, job_id: int, raw: dict, text_field: str) -> None:
        payload = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        seen = self.conn.execute(
            "SELECT raw_json FROM job_snapshot WHERE job_id = ?", (job_id,)
        ).fetchall()
        if any(row["raw_json"] == payload for row in seen):
            return  # same captured state already recorded
        version = len(seen) + 1
        self.conn.execute(
            "INSERT INTO job_snapshot (job_id, version, captured_at, raw_json) VALUES (?,?,?,?)",
            (job_id, version, now_iso(), payload),
        )
        self.stats["snapshots_added"] += 1
        # keep the canonical job's description in sync with the newest snapshot
        if text_field and raw.get(text_field):
            self.conn.execute(
                "UPDATE job SET description = ? WHERE id = ?", (raw[text_field], job_id)
            )

    def upsert_source(self, job_id: int, source_type: str, external_id: str,
                      raw_title: str, url: str, easy_apply: bool,
                      seen_date: str) -> bool:
        """Insert or refresh a provenance row. Returns True when merged into an
        existing job (dedup evidence) vs attached to a brand-new job."""
        existing = self.conn.execute(
            "SELECT * FROM job_source WHERE source_type = ? AND external_id = ?",
            (source_type, external_id),
        ).fetchone() if external_id else None
        if existing:
            self.conn.execute(
                "UPDATE job_source SET last_seen = MAX(last_seen, ?), url = COALESCE(?, url) "
                "WHERE id = ?",
                (seen_date, url, existing["id"]),
            )
            return True
        self.conn.execute(
            "INSERT INTO job_source (job_id, source_type, external_id, raw_title, url, "
            "easy_apply, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, source_type, external_id, raw_title, url, 1 if easy_apply else 0,
             seen_date, seen_date),
        )
        return False

    def touch_job_dates(self, job_id: int, first: str, last: str) -> None:
        self.conn.execute(
            "UPDATE job SET first_seen = MIN(first_seen, ?), last_seen = MAX(last_seen, ?), "
            "updated_at = ? WHERE id = ?",
            (first, last, now_iso(), job_id),
        )

    def refresh_skills(self, job_id: int, description: str) -> None:
        skills = extract_skills(description or "")
        self.conn.execute("DELETE FROM job_skill WHERE job_id = ?", (job_id,))
        for name, req in sorted(skills.items()):
            self.conn.execute(
                "INSERT OR IGNORE INTO job_skill (job_id, skill, requirement) VALUES (?,?,?)",
                (job_id, name, req),
            )

    # -- record-level ingestion ------------------------------------------------

    def ingest_record(self, *, source_type: str, external_id: str, title: str,
                      company: str, location: str, city: str, work_model: str,
                      description: str, apply_url: str, seen_date: str,
                      salary_text: str = "", applicants=None, posted_text: str = "",
                      curator_notes: str = "", easy_apply: bool = False,
                      raw: dict = None, raw_title: str = ""):
        self.stats["jobs_seen"] += 1
        title = collapse(title)
        company = collapse(company)
        if not title:
            self.stats["errors"].append(f"skip (no title): {external_id}")
            return None

        exp_min, exp_max, exp_stated = parse_experience(title, description)
        fam_slug, confidence, reason = classify(title, description)

        job = self.find_by_external(source_type, external_id)
        key = canonical_key(company, title, city, external_id)
        if job is None:
            job = self.find_by_key(key)
        if job is None:
            job = self.find_fuzzy(company, title, city)
            if job is not None:
                self.stats["sources_merged"] += 1

        fam_id = None
        if fam_slug:
            row = self.conn.execute(
                "SELECT id FROM role_family WHERE slug = ?", (fam_slug,)
            ).fetchone()
            fam_id = row["id"] if row else None

        if job is None:
            cur = self.conn.execute(
                "INSERT INTO job (canonical_key, title, company, location, city, work_model, "
                "exp_min, exp_max, exp_stated, role_family_id, family_confidence, family_reason, "
                "salary_text, applicants, posted_text, description, curator_notes, apply_url, "
                "first_seen, last_seen, freshness, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, title, company, location, city, work_model,
                 exp_min, exp_max, exp_stated, fam_id, confidence, reason,
                 salary_text, applicants, posted_text, description, curator_notes,
                 apply_url, seen_date, seen_date, "active", now_iso(), now_iso()),
            )
            job_id = cur.lastrowid
            self.stats["jobs_new"] += 1
            merged = self.upsert_source(job_id, source_type, external_id, raw_title,
                                        apply_url, easy_apply, seen_date)
            merged_any = False
        else:
            job_id = job["id"]
            # Prefer richer values but never overwrite good data with emptier data.
            self.conn.execute(
                "UPDATE job SET "
                "title = CASE WHEN ? <> '' THEN ? ELSE title END, "
                "company = CASE WHEN ? <> '' THEN ? ELSE company END, "
                "location = CASE WHEN ? <> '' THEN ? ELSE location END, "
                "city = CASE WHEN ? <> '' THEN ? ELSE city END, "
                "work_model = CASE WHEN ? <> 'unspecified' THEN ? ELSE work_model END, "
                "exp_min = COALESCE(exp_min, ?), exp_max = COALESCE(exp_max, ?), "
                "exp_stated = COALESCE(exp_stated, ?), "
                "role_family_id = COALESCE(role_family_id, ?), "
                "family_confidence = COALESCE(family_confidence, ?), "
                "family_reason = CASE WHEN ? <> '' THEN ? ELSE family_reason END, "
                "salary_text = COALESCE(NULLIF(?, ''), salary_text), "
                "applicants = COALESCE(?, applicants), "
                "posted_text = COALESCE(NULLIF(?, ''), posted_text), "
                "curator_notes = COALESCE(NULLIF(?, ''), curator_notes), "
                "description = COALESCE(NULLIF(?, ''), description), "
                "apply_url = COALESCE(NULLIF(?, ''), apply_url), "
                "updated_at = ? "
                "WHERE id = ?",
                (title, title, company, company, location, location, city, city,
                 work_model, work_model, exp_min, exp_max, exp_stated,
                 fam_id, confidence, reason, reason, salary_text, applicants,
                 posted_text, curator_notes, description, apply_url, now_iso(), job_id),
            )
            merged_any = self.upsert_source(job_id, source_type, external_id, raw_title,
                                            apply_url, easy_apply, seen_date)
            if merged_any:
                self.stats["sources_merged"] += 1
            self.stats["jobs_updated"] += 1

        self.touch_job_dates(job_id, seen_date, seen_date)
        if raw:
            self.add_snapshot_if_changed(job_id, raw, "panelText")
        self.refresh_skills(job_id, description)
        return job_id

    # -- source files ----------------------------------------------------------

    def ingest_alert_details(self, daily_root: Path) -> None:
        files = sorted(daily_root.glob("*/alert*_details.jsonl"))
        for fp in files:
            run_date = fp.parent.name  # folder is YYYY-MM-DD
            with open(fp, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        self.stats["errors"].append(f"bad json in {fp.name}")
                        continue
                    card = normalize_card(rec.get("cardText", ""))
                    desc = collapse(rec.get("panelText", ""))
                    if not card["title"]:
                        self.stats["errors"].append(
                            f"no parsable title for jobId {rec.get('jobId')}")
                        continue
                    self.ingest_record(
                        source_type="alert",
                        external_id=str(rec.get("jobId", "")),
                        title=card["title"],
                        company=card["company"],
                        location=card["location"],
                        city=card["city"],
                        work_model=card["work_model"],
                        description=desc,
                        apply_url=f"https://www.linkedin.com/jobs/view/{rec.get('jobId')}",
                        seen_date=run_date,
                        salary_text=parse_salary(desc),
                        applicants=card["applicants"] or parse_applicants(rec.get("insights", "")),
                        posted_text=card["posted_text"],
                        easy_apply=bool(rec.get("easyApply")),
                        raw=rec,
                        raw_title=collapse(rec.get("cardText", "").splitlines()[0] if rec.get("cardText") else ""),
                    )

    def ingest_ledger(self, ledger_path: Path) -> None:
        """Ledger drives freshness dates for known jobs; unknown ids become
        minimal title-only job records (honest: no description claimed)."""
        if not ledger_path.exists():
            self.stats["errors"].append(f"ledger missing: {ledger_path}")
            return
        with open(ledger_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ext = str(rec.get("key", ""))
                src = rec.get("source", "alert")
                first, last = rec.get("firstSeen", ""), rec.get("lastSeen", "")
                job = self.find_by_external(src, ext)
                if job is not None:
                    self.touch_job_dates(job["id"], first, last)
                    self.upsert_source(job["id"], src, ext, rec.get("title", ""),
                                       rec.get("applyUrl", ""), False, last)
                    continue
                parsed = normalize_ledger_title(rec.get("title", ""))
                if not parsed["title"]:
                    self.stats["errors"].append(f"ledger unparsable: {ext}")
                    continue
                self.ingest_record(
                    source_type=src,
                    external_id=ext,
                    title=parsed["title"],
                    company=parsed["company"],
                    location=parsed["location"],
                    city=parsed["city"],
                    work_model=parsed["work_model"],
                    description="",
                    apply_url=rec.get("applyUrl", ""),
                    seen_date=first or last,
                    raw=rec,
                )

    def ingest_qualifying(self, path: Path) -> None:
        if not path.exists():
            self.stats["errors"].append(f"qualifying missing: {path}")
            return
        items = json.loads(path.read_text(encoding="utf-8"))
        seen_date = "2026-09-18"  # date of the feed scan run that produced this file
        for it in items:
            role = collapse(it.get("role", ""))
            company = collapse(it.get("company", ""))
            loc = collapse(it.get("loc", ""))
            notes = "\n\n".join(
                f"{label}: {collapse(it.get(k, ''))}"
                for label, k in [
                    ("Why it qualifies", "why"), ("How to apply", "how"),
                    ("Poster", "poster"), ("Research", "research"), ("Notes", "notes"),
                ]
                if collapse(it.get(k, ""))
            )
            self.ingest_record(
                source_type="curated",
                external_id=slugify(f"{company}-{role}"),
                title=role,
                company=company,
                location=loc,
                city=extract_city(loc),
                work_model="unspecified" if "remote" not in loc.lower() and "(hybrid)" not in loc.lower() else ("remote" if "remote" in loc.lower() else "hybrid"),
                description="",
                apply_url=it.get("apply", ""),
                seen_date=seen_date,
                salary_text=parse_salary(it.get("ctc", "")) or collapse(it.get("ctc", "")),
                curator_notes=notes,
                raw=it,
                raw_title=role,
            )

    # -- Excel sources ----------------------------------------------------------

    def _apply_scored_category(self, job_id: int, category: str) -> None:
        """The scored Excels carry a human-curated category column; it outranks
        the keyword classifier when the name maps to a taxonomy family."""
        name = collapse(category)
        if not name:
            return
        row = self.conn.execute(
            "SELECT id FROM role_family WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE job SET role_family_id = ?, family_confidence = 0.95, "
                "family_reason = 'category from scored Excel (daily pipeline)' WHERE id = ?",
                (row["id"], job_id),
            )

    def ingest_alert_excel(self, path: Path) -> None:
        """Scored-alert Excel: 'All jobs' sheet (the Shortlist sheet is a
        subset). Shares LinkedIn Job IDs with the alert-details source, so
        rows merge into existing canonical jobs and enrich them."""
        import openpyxl

        seen_date = "2026-09-18" if "18" in path.stem else "2026-09-19"
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        if "All jobs" not in wb.sheetnames:
            self.stats["errors"].append(f"{path.name}: no 'All jobs' sheet")
            wb.close()
            return
        ws = wb["All jobs"]
        rows = ws.iter_rows(values_only=True)
        header = [collapse(str(h or "")) for h in next(rows, ())]
        idx = {name: i for i, name in enumerate(header)}

        def cell(row, col):
            i = idx.get(col)
            if i is None or i >= len(row) or row[i] is None:
                return ""
            return str(row[i]).strip()

        for row in rows:
            job_id = cell(row, "Job ID")
            title = cell(row, "Title")
            if not job_id or not title:
                continue
            desc = cell(row, "Job description (full)")
            loc = cell(row, "Location")
            applicants = None
            try:
                applicants = int(float(cell(row, "Applicants"))) or None
            except ValueError:
                applicants = None
            raw = {
                "excel": path.name, "jobId": job_id, "title": title,
                "company": cell(row, "Company"), "category": cell(row, "Category"),
                "fit": cell(row, "Fit"), "why": cell(row, "Why it fits"),
                "score": cell(row, "Score"), "skillsMatched": cell(row, "Skills matched"),
            }
            job_db_id = self.ingest_record(
                source_type="alert",
                external_id=job_id,
                title=title,
                company=cell(row, "Company"),
                location=loc,
                city=extract_city(loc),
                work_model=parse_work_model(loc),
                description=desc,
                apply_url=cell(row, "Job link"),
                seen_date=seen_date,
                salary_text=parse_salary(desc),
                applicants=applicants,
                posted_text=cell(row, "Posted"),
                easy_apply=cell(row, "Easy Apply").lower().startswith("y"),
                raw=raw,
                raw_title=title,
            )
            if job_db_id:
                self._apply_scored_category(job_db_id, cell(row, "Category"))
        wb.close()

    def ingest_feed_excel(self, path: Path) -> None:
        """Feed-scan Excel: curated 1-2 YOE jobs with live status and the
        official job description where captured. Supersedes the older
        qualifying_jobs.json with richer data."""
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet = next((s for s in wb.sheetnames if s.lower().startswith("jobs from feed")), None)
        if sheet is None:
            self.stats["errors"].append(f"{path.name}: no feed jobs sheet")
            wb.close()
            return
        rows = wb[sheet].iter_rows(values_only=True)
        header = [collapse(str(h or "")) for h in next(rows, ())]
        idx = {name: i for i, name in enumerate(header)}

        def col_index(col: str) -> int:
            if col in idx:
                return idx[col]
            # Excel headers carry run-specific suffixes (e.g. 'Live status
            # (checked 18 Sep ~7:30am)') — fall back to prefix matching.
            for name, i in idx.items():
                if name.startswith(col):
                    return i
            return -1

        def cell(row, col):
            i = col_index(col)
            if i < 0 or i >= len(row) or row[i] is None:
                return ""
            return str(row[i]).strip()

        for row in rows:
            company = cell(row, "Company")
            role = cell(row, "Role / Title")
            if not company or not role:
                continue
            loc = cell(row, "Location")
            live = cell(row, "Live status")
            notes = "\n\n".join(
                f"{label}: {cell(row, col)}"
                for label, col in [
                    ("Why it fits you", "Why it fits you"),
                    ("How to apply / referral", "How to apply / Referral"),
                    ("Poster", "Posted by (connection)"),
                    ("Research", "About the company (research)"),
                    ("Notes / cautions", "Notes / Cautions"),
                    ("Live status", "Live status"),
                ]
                if cell(row, col)
            )
            raw = {
                "excel": path.name, "company": company, "role": role,
                "liveStatus": live, "postAge": cell(row, "Post age"),
            }
            job_db_id = self.ingest_record(
                source_type="curated",
                external_id=slugify(f"{company}-{role}"),
                title=role,
                company=company,
                location=loc,
                city=extract_city(loc),
                work_model=parse_work_model(loc),
                description=cell(row, "Job description (official"),
                apply_url=cell(row, "Apply link"),
                seen_date="2026-09-18",
                salary_text=parse_salary(cell(row, "Salary / CTC")) or cell(row, "Salary / CTC"),
                curator_notes=notes,
                raw=raw,
                raw_title=role,
            )
            if job_db_id and live:
                if live.upper().startswith("CLOSED"):
                    self.conn.execute(
                        "UPDATE job SET freshness = 'closed' WHERE id = ?", (job_db_id,)
                    )
                elif live.upper().startswith("OPEN"):
                    self.conn.execute(
                        "UPDATE job SET freshness = 'active' WHERE id = ?", (job_db_id,)
                    )
        wb.close()

    def backfill_capabilities(self) -> None:
        """Extract seniority/scope signals (Module E) for every job with a
        description. Deterministic; runs on every ingest (cheap, idempotent)."""
        from app.jobexpect import extract_capabilities

        for row in self.conn.execute(
                "SELECT id, description FROM job WHERE description <> ''").fetchall():
            caps = extract_capabilities(row["description"])
            self.conn.execute("DELETE FROM job_capability WHERE job_id = ?", (row["id"],))
            for cap, evidence in caps.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO job_capability (job_id, capability, evidence) "
                    "VALUES (?,?,?)", (row["id"], cap, evidence[:400]))

    def recompute_freshness(self) -> None:
        rows = self.conn.execute("SELECT id, first_seen, last_seen FROM job").fetchall()
        for row in rows:
            state = compute_freshness(row["first_seen"], row["last_seen"])
            self.conn.execute(
                "UPDATE job SET freshness = ? WHERE id = ?", (state, row["id"])
            )

    def run(self, ledger: Path, daily_root: Path, qualifying: Path,
            alert_excels=None, feed_excel=None) -> dict:
        cur = self.conn.execute(
            "INSERT INTO ingest_run (started_at, sources) VALUES (?, ?)",
            (now_iso(), f"ledger={ledger}; daily={daily_root}; qualifying={qualifying}; "
                        f"alert_excels={list(map(str, alert_excels or []))}; feed_excel={feed_excel}"),
        )
        run_id = cur.lastrowid
        self.ingest_alert_details(daily_root)
        self.ingest_ledger(ledger)
        self.ingest_qualifying(qualifying)
        for xl in alert_excels or []:
            if Path(xl).exists():
                self.ingest_alert_excel(Path(xl))
            else:
                self.stats["errors"].append(f"alert excel missing: {xl}")
        self.recompute_freshness()
        self.backfill_capabilities()
        # Feed excel runs last so its human-verified live status (OPEN/CLOSED)
        # is not overwritten by the date-based freshness recomputation.
        if feed_excel and Path(feed_excel).exists():
            self.ingest_feed_excel(Path(feed_excel))
        elif feed_excel:
            self.stats["errors"].append(f"feed excel missing: {feed_excel}")
        self.conn.execute(
            "UPDATE ingest_run SET finished_at = ?, jobs_seen = ?, jobs_new = ?, "
            "jobs_updated = ?, sources_merged = ?, notes = ? WHERE id = ?",
            (now_iso(), self.stats["jobs_seen"], self.stats["jobs_new"],
             self.stats["jobs_updated"], self.stats["sources_merged"],
             json.dumps(self.stats["errors"][:50]), run_id),
        )
        self.conn.commit()
        return self.stats


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    ledger = Path(argv[0]) if len(argv) > 0 else DEFAULT_LEDGER
    daily = Path(argv[1]) if len(argv) > 1 else DEFAULT_DAILY_GLOB
    qualifying = Path(argv[2]) if len(argv) > 2 else DEFAULT_QUALIFYING
    excel_dir = Path.home() / "Desktop" / "Job Hunt Excels"
    alert_excels = sorted(excel_dir.glob("linkedin_alerts_jobs_scored*.xlsx"))
    feed_excel = excel_dir / "linkedin_feed_jobs_1-2YOE.xlsx"
    conn = connect()
    init_db(conn)
    stats = Ingestor(conn).run(ledger, daily, qualifying, alert_excels, feed_excel)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
