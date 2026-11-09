import hashlib
import hmac
import base64
import secrets
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
import qrcode
from qrcode.image.svg import SvgPathFillImage
from fastapi import HTTPException, Request, status
from app.config import settings

_sessions = {}
_mfa_challenges = {}
_lock = threading.Lock()
_SESSION_TTL_SECONDS = 12 * 60 * 60


def _db_path() -> Path:
    path = Path(settings.BACKUP_STORAGE_DIR) / "users.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def initialize_users():
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY COLLATE NOCASE, password_hash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','user')), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "totp_secret_enc" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN totp_secret_enc TEXT")
        if "totp_enabled" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0")
        if "totp_confirmed" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN totp_confirmed INTEGER NOT NULL DEFAULT 0")
        if "password_change_required" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN password_change_required INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE, role TEXT NOT NULL, expires_at REAL NOT NULL)")
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
        if conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is None:
            _create_user(conn, settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD or "AdminBackup", "admin")


def _totp_cipher() -> Fernet:
    secret = (settings.AUTH_TOTP_ENCRYPTION_KEY or settings.SERVER_CREDENTIAL_ENCRYPTION_KEY
              or settings.BACKUP_ENCRYPTION_KEY or settings.API_KEY or "dbops-totp-key").encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


def _totp_code(secret: str, at_time: Optional[float] = None) -> str:
    counter = int((at_time if at_time is not None else time.time()) // 30)
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _verify_totp(secret: str, code: str) -> bool:
    if len(code) != 6 or not code.isdigit():
        return False
    now = time.time()
    return any(hmac.compare_digest(_totp_code(secret, now + offset * 30), code) for offset in (-1, 0, 1))


def _mfa_setup_details(username: str, secret: str) -> dict:
    label = quote(f"DBOps Control Plane:{username}", safe="")
    issuer = quote("DBOps Control Plane", safe="")
    otpauth_uri = f"otpauth://totp/{label}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_Q, border=4)
    qr.add_data(otpauth_uri)
    qr.make(fit=True)
    svg = qr.make_image(image_factory=SvgPathFillImage).to_string(encoding="unicode")
    return {
        "manual_secret": secret,
        "otpauth_uri": otpauth_uri,
        "qr_data_url": "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii"),
    }


def _create_user(conn, username, password, role):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()
    conn.execute("INSERT INTO users(username,password_hash,salt,role) VALUES(?,?,?,?)", (username, digest, salt, role))


def authenticate(username: str, password: str) -> Optional[dict]:
    initialize_users()
    with sqlite3.connect(_db_path()) as conn:
        row = conn.execute("SELECT username,password_hash,salt,role,totp_secret_enc,totp_enabled,totp_confirmed,password_change_required FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    if not row:
        return None
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(row[2]), 310000).hex()
    if not hmac.compare_digest(candidate, row[1]):
        return None
    return {"username": row[0], "role": row[3], "totp_secret_enc": row[4], "totp_enabled": bool(row[5]), "totp_confirmed": bool(row[6]), "password_change_required": bool(row[7])}


def begin_mfa_challenge(user: dict) -> dict:
    is_enrolled = bool(user.get("totp_enabled") and user.get("totp_secret_enc"))
    if not is_enrolled:
        session_user = {"username": user["username"], "role": user["role"],
                        "password_change_required": user.get("password_change_required", False)}
        return {
            "mfa_required": False,
            "token": issue_session(session_user),
            "user": session_user,
        }

    challenge = secrets.token_urlsafe(32)
    setup_required = not user.get("totp_confirmed", False)
    try:
        secret = (_totp_cipher().decrypt(user["totp_secret_enc"].encode()).decode()
                  if setup_required else None)
    except (InvalidToken, ValueError):
        raise HTTPException(500, "Could not read the saved authenticator setup. Check that AUTH_TOTP_ENCRYPTION_KEY has not changed.")
    with _lock:
        now = time.time()
        for key, value in list(_mfa_challenges.items()):
            if value["expires_at"] < now:
                _mfa_challenges.pop(key, None)
        _mfa_challenges[challenge] = {
            "username": user["username"], "role": user["role"], "mode": "enroll" if setup_required else "verify",
            "secret": secret if setup_required else user["totp_secret_enc"],
            "password_change_required": user.get("password_change_required", False),
            "expires_at": now + 300, "attempts": 0,
        }
    response = {
        "mfa_required": True,
        "challenge": challenge,
        "username": user["username"],
        "setup_required": setup_required,
    }
    if setup_required:
        response.update(_mfa_setup_details(user["username"], secret))
    return response


def complete_mfa_challenge(challenge: str, code: str) -> tuple[str, dict]:
    with _lock:
        pending = _mfa_challenges.get(challenge)
        if not pending or pending["expires_at"] < time.time():
            _mfa_challenges.pop(challenge, None)
            raise HTTPException(401, "Authenticator challenge expired. Sign in again.")
        pending["attempts"] += 1
        mode, username = pending["mode"], pending["username"]
        secret_value = pending["secret"]
        attempts = pending["attempts"]
    if attempts > 5:
        with _lock:
            _mfa_challenges.pop(challenge, None)
        raise HTTPException(429, "Too many verification attempts. Sign in again.")
    try:
        secret = secret_value if mode == "enroll" else _totp_cipher().decrypt(secret_value.encode()).decode()
    except (InvalidToken, ValueError):
        raise HTTPException(500, "Could not read the saved authenticator setup. Check that AUTH_TOTP_ENCRYPTION_KEY has not changed.")
    if not _verify_totp(secret, code.strip()):
        raise HTTPException(401, "That authenticator code is incorrect or expired.")
    with _lock:
        _mfa_challenges.pop(challenge, None)
    if mode == "enroll":
        initialize_users()
        with sqlite3.connect(_db_path()) as conn:
            conn.execute("UPDATE users SET totp_secret_enc=?, totp_enabled=1, totp_confirmed=1 WHERE username=? COLLATE NOCASE",
                         (_totp_cipher().encrypt(secret.encode()).decode(), username))
    user = {"username": pending["username"], "role": pending["role"],
            "password_change_required": pending.get("password_change_required", False)}
    return issue_session(user), user


def create_user(username: str, password: str, enable_mfa: bool = False, role: str = "user"):
    if role not in {"user", "admin"}:
        raise HTTPException(400, "Role must be user or admin.")
    initialize_users()
    result = {"mfa_enabled": False}
    with sqlite3.connect(_db_path()) as conn:
        try:
            _create_user(conn, username, password, role)
            conn.execute("UPDATE users SET password_change_required=1 WHERE username=? COLLATE NOCASE", (username,))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That username already exists.")
    if enable_mfa:
        secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
        encrypted = _totp_cipher().encrypt(secret.encode()).decode()
        with sqlite3.connect(_db_path()) as conn:
            conn.execute("UPDATE users SET totp_secret_enc=?, totp_enabled=1 WHERE username=? COLLATE NOCASE",
                         (encrypted, username))
        result = {
            "mfa_enabled": True,
            **_mfa_setup_details(username, secret),
        }
    return result


def change_password(username: str, current_password: str, new_password: str) -> None:
    """Verify the current credential, rotate it, and clear forced-change state."""
    initialize_users()
    with sqlite3.connect(_db_path()) as conn:
        row = conn.execute(
            "SELECT password_hash,salt FROM users WHERE username=? COLLATE NOCASE", (username,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "User account not found.")
        candidate = hashlib.pbkdf2_hmac("sha256", current_password.encode(), bytes.fromhex(row[1]), 310000).hex()
        if not hmac.compare_digest(candidate, row[0]):
            raise HTTPException(401, "Current password is incorrect.")
        if hmac.compare_digest(current_password, new_password):
            raise HTTPException(400, "Choose a new password different from the current password.")
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", new_password.encode(), bytes.fromhex(salt), 310000).hex()
        conn.execute(
            "UPDATE users SET password_hash=?,salt=?,password_change_required=0 WHERE username=? COLLATE NOCASE",
            (digest, salt, username),
        )


def list_users():
    initialize_users()
    with sqlite3.connect(_db_path()) as conn:
        return [{"username": r[0], "role": r[1], "created_at": r[2]} for r in conn.execute("SELECT username,role,created_at FROM users ORDER BY username")]


def is_main_admin(username: str) -> bool:
    return username.casefold() == "admin"


def _clear_cached_user_access(username: str) -> None:
    with _lock:
        for token, session in list(_sessions.items()):
            if session["username"].casefold() == username.casefold():
                _sessions.pop(token, None)
        for challenge, pending in list(_mfa_challenges.items()):
            if pending["username"].casefold() == username.casefold():
                _mfa_challenges.pop(challenge, None)


def delete_user(username: str, requester_username: str):
    initialize_users()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT role FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        if username.casefold() == requester_username.casefold():
            raise HTTPException(400, "You cannot delete your own account.")
        if row[0] == "admin":
            if not is_main_admin(requester_username):
                raise HTTPException(403, "Only the Admin account can delete administrator accounts.")
            admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
            if admin_count <= 1:
                raise HTTPException(400, "The last administrator account cannot be deleted.")
        conn.execute("DELETE FROM users WHERE username=? COLLATE NOCASE", (username,))
        conn.execute("DELETE FROM sessions WHERE username=? COLLATE NOCASE", (username,))
    _clear_cached_user_access(username)


def reset_user_password(username: str, new_password: str, requester_username: str) -> None:
    if not is_main_admin(requester_username):
        raise HTTPException(403, "Only the Admin account can reset other users' passwords.")
    if username.casefold() == requester_username.casefold():
        raise HTTPException(400, "Use Change password to update your own password.")
    initialize_users()
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", new_password.encode(), bytes.fromhex(salt), 310000).hex()
    with sqlite3.connect(_db_path()) as conn:
        row = conn.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        conn.execute(
            "UPDATE users SET password_hash=?,salt=?,password_change_required=1 WHERE username=? COLLATE NOCASE",
            (digest, salt, username),
        )
        conn.execute("DELETE FROM sessions WHERE username=? COLLATE NOCASE", (username,))
    _clear_cached_user_access(username)


def issue_session(user: dict) -> str:
    token = secrets.token_urlsafe(36)
    expires_at = time.time() + _SESSION_TTL_SECONDS
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    initialize_users()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("INSERT INTO sessions(token_hash,username,role,expires_at) VALUES(?,?,?,?)",
                     (token_hash, user["username"], user["role"], expires_at))
    with _lock:
        _sessions[token] = {**user, "expires_at": expires_at}
    return token


def revoke_session(token: str) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
    with _lock:
        _sessions.pop(token, None)


async def current_user(request: Request):
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        initialize_users()
        with sqlite3.connect(_db_path()) as conn:
            row = conn.execute("SELECT s.username,s.role,s.expires_at,u.password_change_required FROM sessions s JOIN users u ON u.username=s.username WHERE s.token_hash=?", (token_hash,)).fetchone()
            if row and row[2] > time.time():
                user = {"username": row[0], "role": row[1], "password_change_required": bool(row[3])}
                if user["password_change_required"] and request.url.path not in {
                    "/api/v1/auth/me", "/api/v1/auth/change-password", "/api/v1/auth/logout"
                }:
                    raise HTTPException(403, "Change your temporary password before using DBOps.")
                return user
            if row:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        with _lock:
            _sessions.pop(token, None)
    # Keep existing API-key integrations working as an administrative credential.
    key = request.headers.get("X-API-Key")
    if not key and authorization.startswith("Bearer "):
        key = authorization[7:].strip()
    if key and settings.API_KEY and hmac.compare_digest(key, settings.API_KEY):
        return {"username": "api-key", "role": "admin"}
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in with a valid account.", headers={"WWW-Authenticate": "Bearer"})


async def require_admin(user= None):
    if user is None or user.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator access required.")
    return user
