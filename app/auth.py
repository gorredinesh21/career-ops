"""Accounts (BRD Phase 2): register / login / logout with PBKDF2-hashed
passwords and DB-backed sessions. Anonymous use keeps working under the
'local' user key — signup is a conversion moment, never a wall."""
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta

from app.config import SESSION_TTL_DAYS


def _hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return salt.hex() + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 120_000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def register(conn: sqlite3.Connection, username: str, password: str):
    username = (username or "").strip().lower()
    if not (3 <= len(username) <= 30) or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        return None, "Username must be 3-30 chars: letters, numbers, . _ -"
    if len(password or "") < 8:
        return None, "Password must be at least 8 characters."
    if conn.execute("SELECT 1 FROM co_user WHERE username = ?", (username,)).fetchone():
        return None, "That username is taken."
    cur = conn.execute(
        "INSERT INTO co_user (username, password_hash, created_at) VALUES (?,?,?)",
        (username, _hash_password(password), datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return cur.lastrowid, None


def login(conn: sqlite3.Connection, username: str, password: str):
    row = conn.execute(
        "SELECT id, password_hash FROM co_user WHERE username = ?",
        ((username or "").strip().lower(),),
    ).fetchone()
    if row is None or not verify_password(password or "", row["password_hash"]):
        return None, "Wrong username or password."
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_TTL_DAYS)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO session (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (token, row["id"], datetime.now().isoformat(timespec="seconds"), expires),
    )
    conn.commit()
    return token, None


def logout(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM session WHERE token = ?", (token,))
    conn.commit()


def user_for_token(conn: sqlite3.Connection, token: str):
    if not token:
        return None
    row = conn.execute(
        "SELECT u.id, u.username, s.expires_at FROM session s JOIN co_user u ON u.id = s.user_id "
        "WHERE s.token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < datetime.now().isoformat(timespec="seconds"):
        logout(conn, token)
        return None
    return row
