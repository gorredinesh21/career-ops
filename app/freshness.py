"""Freshness state machine (BRD Module A).

discovered -> active -> likely_active -> stale -> closed, driven purely by
first_seen / last_seen evidence from the ingestion ledger. Thresholds are
explicit so the UI can explain any badge it renders.
"""
from datetime import date

ACTIVE_DAYS = 7       # seen within the last week
LIKELY_DAYS = 21      # not seen for 1-3 weeks
STALE_DAYS = 45       # not seen for over a month -> assume closed

EXPLANATIONS = {
    "active": "Seen in a pipeline run within the last 7 days.",
    "likely_active": "Not seen for 1–3 weeks; probably still open but unverified.",
    "stale": "Not seen for over 3 weeks; treat as possibly closed.",
    "closed": "Not seen for over 6 weeks; assumed closed.",
}


def compute_freshness(first_seen: str, last_seen: str, today: date = None) -> str:
    today = today or date.today()
    try:
        last = date.fromisoformat(last_seen)
    except (TypeError, ValueError):
        return "stale"
    days = (today - last).days
    if days <= ACTIVE_DAYS:
        return "active"
    if days <= LIKELY_DAYS:
        return "likely_active"
    if days <= STALE_DAYS:
        return "stale"
    return "closed"


def freshness_explanation(state: str) -> str:
    return EXPLANATIONS.get(state, "")
