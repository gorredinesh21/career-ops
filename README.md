# Career-Ops

A technical job-search control room — Phase 1 (Job Intelligence + Discovery).

Jobs are ingested from the real daily pipelines (LinkedIn alert detail files,
the cross-day ledger, curated feed-scan jobs), normalized into one canonical
listing per real job, freshness-tracked by first-seen/last-seen evidence, and
structured into a controlled taxonomy of nine role families. No LLM anywhere:
every extraction is deterministic and explainable.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate   # optional; system python3 works if deps present
pip install -r requirements.txt
python3 -m app.ingest        # (re)build data/careerops.db from the pipelines; idempotent
uvicorn app.main:app --port 8765
# open http://127.0.0.1:8765
```

## Test

```bash
python3 -m pytest tests/ -q
```

## Pages

| Page | What it does |
|---|---|
| `/` | Landing: live counts, 3-step how-to, role families, latest sightings |
| `/jobs` | Feed with filters: family, experience band, work model, freshness, location, search |
| `/jobs/{id}` | Structured requirements, preserved original posting, classification reason, provenance, report buttons |
| `/roles` | Taxonomy + community role requests with transparent 25-vote review threshold |
| `/ops` | Ingestion health: freshness distribution, classification confidence, unclassified review queue, ingest runs, user reports |
| `/healthz` | JSON liveness + counts |

## Data sources (defaults, overridable via CLI args)

1. `~/Desktop/LinkedIn Daily/*/alert*_details.jsonl` — full JDs per LinkedIn job id
2. `~/Desktop/LinkedIn Daily/job_history.jsonl` — cross-day ledger; drives first-seen/last-seen freshness
3. `~/.zcode/workspace/default/linkedin-feed-jobs-2026-09-18/qualifying_jobs.json` — curated feed jobs (stored with curator notes, never as posting text)

## Design rules (from the BRD)

- One canonical job per real opening; sources/provenance expandable, duplicates merged with explainable reasons.
- Freshness = pipeline sighting evidence, never "posted today" marketing claims.
- Nothing invented: skills only appear when they match the posting text; missing fields stay visibly empty; curator notes are labeled separately from posting text.
- Unclassified jobs go to the review queue instead of being forced into a family.
- Applications happen at the original source; Career-Ops never auto-applies.

## Layout

```
app/          FastAPI app — db, normalize, classify, freshness, ingest, queries, main
templates/    Jinja pages (server-rendered, no JS framework, no spinners)
static/       plain CSS
tests/        deterministic pytest suite for the parsing/classification pipeline
data/         careerops.db (generated; gitignored)
```
