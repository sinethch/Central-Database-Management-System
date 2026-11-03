"""Encryption helpers for secrets persisted in the control-plane storage volume."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_PREFIX = "enc:v1:"


def _cipher() -> Fernet:
    secret = (getattr(settings, "DATABASE_CREDENTIAL_ENCRYPTION_KEY", None)
              or settings.SERVER_CREDENTIAL_ENCRYPTION_KEY
              or settings.BACKUP_ENCRYPTION_KEY or "").strip()
    placeholders = {"replace-with-a-separate-long-random-secret", "dbops-control-plane-default-key-32b!"}
    if len(secret) < 32 or secret in placeholders:
        raise RuntimeError(
            "Set a stable, unique DATABASE_CREDENTIAL_ENCRYPTION_KEY or "
            "SERVER_CREDENTIAL_ENCRYPTION_KEY (at least 32 characters) before storing application secrets."
        )
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()))


def encrypt_persisted_secret(value: str) -> str:
    if value.startswith(_PREFIX):
        return value
    return _PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_persisted_secret(value: str) -> str:
    if not value.startswith(_PREFIX):
        return value
    try:
        return _cipher().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(
            "Could not decrypt a saved application secret. Keep DATABASE_CREDENTIAL_ENCRYPTION_KEY stable."
        ) from exc
