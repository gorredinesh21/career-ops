"""Central product configuration. Placeholder values are clearly marked so
they can be swapped without hunting through the code."""
import os
import secrets
from pathlib import Path

# Where the SQLite database lives. ALWAYS local disk — SQLite must never sit
# on a network filesystem (gcsfuse corrupted our first deployment). On Cloud
# Run this is ephemeral container disk; durability comes from the GCS backup
# below, which restores on boot and uploads after every write.
DB_PATH = Path(os.environ.get("CAREEROPS_DB",
                              Path(__file__).resolve().parent.parent / "data" / "careerops.db"))

# When set, every committed write is backed up to
# gs://<bucket>/<db filename> asynchronously and restored on boot.
GCS_BUCKET = os.environ.get("CAREEROPS_GCS_BUCKET", "").strip()

# Jobs-only snapshot baked into the container image; merged into the live DB
# at startup so redeploying refreshes jobs without touching user data.
SEED_DB_PATH = Path(os.environ.get("CAREEROPS_SEED",
                                   Path(__file__).resolve().parent.parent / "deploy" / "careerops.db"))

# Community distribution (BRD Module J). Dinesh will supply the real channel
# link later — the button renders this value everywhere it appears.
TELEGRAM_URL = "https://t.me/career_ops"  # PLACEHOLDER — replace with the real channel link

# Session signing for accounts. Persisted so logins survive restarts.
_SECRET_PATH = DB_PATH.parent / ".secret"
SESSION_TTL_DAYS = 30

# Community voting (BRD Module B): transparent review threshold.
VOTE_REVIEW_THRESHOLD = 25

# Freshness thresholds (days since last sighting).
ACTIVE_DAYS, LIKELY_DAYS, STALE_DAYS = 7, 21, 45


def session_secret() -> bytes:
    _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _SECRET_PATH.exists():
        return _SECRET_PATH.read_bytes().strip()
    value = secrets.token_bytes(32)
    _SECRET_PATH.write_bytes(value)
    return value
