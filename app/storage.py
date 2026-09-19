"""Durable storage without SQLite-on-FUSE fragility.

The database is a plain file on local disk (real fsync semantics; SQLite over
a network filesystem is unsupported and corrupted our first deployment).
Every committed write schedules an asynchronous backup of the file to a GCS
object; on boot the newest backup is downloaded and integrity-checked before
use. A container that dies between a commit and the next flush loses at most
a few seconds of writes — the file itself is never again opened through a
network mount.

Disabled entirely when CAREEROPS_GCS_BUCKET is unset (local development).
"""
import sqlite3
import threading
import time
from pathlib import Path

FLUSH_INTERVAL = 5.0  # minimum seconds between backup uploads


def db_is_healthy(path: Path) -> bool:
    """quick_check is fast on a 2 MB database and catches corruption."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


class GcsBackup:
    """Asynchronous file-level backup of the SQLite database (and session
    secret) to GCS. The uploads run in a daemon thread so request latency is
    never affected; failures re-queue with backoff."""

    def __init__(self, db_path: Path, bucket_name: str, prefix: str = ""):
        self.db_path = Path(db_path)
        self.secret_path = self.db_path.parent / ".secret"
        self.bucket_name = bucket_name
        self.prefix = prefix
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._last_flush = 0.0
        self._client = None
        self._thread = None
        self._enabled = bool(bucket_name)

    # -- low-level -------------------------------------------------------
    def _bucket(self):
        if self._client is None:
            from google.cloud import storage  # lazy: not needed on localhost

            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def _upload(self, local: Path, object_name: str) -> None:
        self._bucket().blob(self.prefix + object_name).upload_from_filename(local)

    # -- public API ------------------------------------------------------
    def restore(self) -> bool:
        """Download the backup into db_path (and .secret). True when a healthy
        backup was restored; any unhealthy object is moved aside, not used."""
        if not self._enabled:
            return False
        try:
            blob = self._bucket().blob(self.prefix + self.db_path.name)
            if not blob.exists():
                print(f"[storage] no backup object in gs://{self.bucket_name}", flush=True)
                return False
            tmp = self.db_path.with_suffix(".download")
            blob.download_to_filename(tmp)
            if not db_is_healthy(tmp):
                stamp = time.strftime("%Y%m%d-%H%M%S")
                bad = self.db_path.parent / f"{self.db_path.name}.bad-{stamp}"
                tmp.rename(bad)
                print(f"[storage] backup object UNHEALTHY, kept aside as {bad.name}; "
                      "falling back to seed", flush=True)
                return False
            tmp.rename(self.db_path)
            secret = self._bucket().blob(self.prefix + ".secret")
            if secret.exists():
                secret.download_to_filename(self.secret_path)
            print(f"[storage] restored db from gs://{self.bucket_name}/"
                  f"{self.prefix}{self.db_path.name}", flush=True)
            return True
        except Exception as exc:  # network hiccup: boot from seed, keep going
            print(f"[storage] restore failed ({exc}); falling back to seed", flush=True)
            return False

    def mark_dirty(self) -> None:
        if self._enabled:
            self._dirty.set()

    def start(self) -> None:
        if self._enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="gcs-backup", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._dirty.wait()
            wait = FLUSH_INTERVAL - (time.monotonic() - self._last_flush)
            if wait > 0 and not self._stop.wait(wait):
                continue  # interval not reached yet; loop and re-check
            self._dirty.clear()
            self._flush()

    def _flush(self) -> None:
        try:
            # checkpoint WAL into the main file so the uploaded object is
            # self-contained
            conn = sqlite3.connect(self.db_path, timeout=10)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            self._upload(self.db_path, self.db_path.name)
            if self.secret_path.exists():
                self._upload(self.secret_path, ".secret")
            self._last_flush = time.monotonic()
        except Exception as exc:
            print(f"[storage] backup upload failed ({exc}); will retry", flush=True)
            self._dirty.set()
            self._stop.wait(15)  # back off before the retry

    def flush_now(self) -> None:
        """Final synchronous flush on shutdown so a graceful redeploy loses
        nothing."""
        if self._enabled and self._dirty.is_set():
            self._dirty.clear()
            self._flush()


_backup: GcsBackup | None = None


def configure(db_path: Path, bucket_name: str | None) -> GcsBackup | None:
    global _backup
    _backup = GcsBackup(db_path, bucket_name or "") if bucket_name else None
    return _backup


def active() -> GcsBackup | None:
    return _backup


def mark_dirty() -> None:
    if _backup is not None:
        _backup.mark_dirty()
