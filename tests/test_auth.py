import pytest

from app import auth
from app.config import settings


@pytest.fixture
def isolated_auth_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_STORAGE_DIR", str(tmp_path))
    auth._sessions.clear()
    auth._mfa_challenges.clear()
    yield
    auth._sessions.clear()
    auth._mfa_challenges.clear()


def test_login_without_enrolled_mfa_issues_session(isolated_auth_db):
    auth.create_user("no_mfa_user", "a-secure-test-password", enable_mfa=False)
    user = auth.authenticate("no_mfa_user", "a-secure-test-password")

    result = auth.begin_mfa_challenge(user)

    assert result["mfa_required"] is False
    assert result["token"]
    assert result["user"] == {"username": "no_mfa_user", "role": "user"}
    assert not auth._mfa_challenges


def test_login_with_enrolled_mfa_requires_verification(isolated_auth_db):
    auth.create_user("mfa_user", "a-secure-test-password", enable_mfa=True)
    user = auth.authenticate("mfa_user", "a-secure-test-password")

    result = auth.begin_mfa_challenge(user)

    assert result["mfa_required"] is True
    assert result["setup_required"] is True
    assert result["username"] == "mfa_user"
    assert result["qr_data_url"].startswith("data:image/svg+xml;base64,")
    assert result["manual_secret"]
    assert result["challenge"]
    assert "token" not in result

    code = auth._totp_code(result["manual_secret"])
    token, session_user = auth.complete_mfa_challenge(result["challenge"], code)
    assert token
    assert session_user == {"username": "mfa_user", "role": "user"}

    verified = auth.begin_mfa_challenge(auth.authenticate("mfa_user", "a-secure-test-password"))
    assert verified["setup_required"] is False


def test_each_mfa_user_gets_a_distinct_authenticator_secret(isolated_auth_db):
    first = auth.create_user("admin_one", "a-secure-test-password", enable_mfa=True, role="admin")
    second = auth.create_user("admin_two", "another-secure-password", enable_mfa=True, role="admin")

    assert first["manual_secret"] != second["manual_secret"]
    assert first["otpauth_uri"] != second["otpauth_uri"]
