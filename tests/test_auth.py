import pytest
from fastapi import HTTPException

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
    assert result["user"] == {
        "username": "no_mfa_user", "role": "user", "password_change_required": True,
    }
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
    assert session_user == {
        "username": "mfa_user", "role": "user", "password_change_required": True,
    }

    verified = auth.begin_mfa_challenge(auth.authenticate("mfa_user", "a-secure-test-password"))
    assert verified["setup_required"] is False


def test_each_mfa_user_gets_a_distinct_authenticator_secret(isolated_auth_db):
    first = auth.create_user("admin_one", "a-secure-test-password", enable_mfa=True, role="admin")
    second = auth.create_user("admin_two", "another-secure-password", enable_mfa=True, role="admin")

    assert first["manual_secret"] != second["manual_secret"]
    assert first["otpauth_uri"] != second["otpauth_uri"]


def test_main_admin_can_delete_another_admin_but_not_their_own_account(isolated_auth_db):
    auth.create_user("admin_one", "a-secure-test-password", role="admin")

    auth.delete_user("admin_one", "Admin")

    assert [user["username"] for user in auth.list_users()] == ["Admin"]
    with pytest.raises(HTTPException, match="cannot delete your own account"):
        auth.delete_user("admin", "ADMIN")


def test_other_admin_cannot_delete_an_admin_account(isolated_auth_db):
    auth.create_user("admin_two", "another-secure-password", role="admin")
    auth.create_user("admin_three", "third-secure-password", role="admin")

    with pytest.raises(HTTPException, match="Only the Admin account"):
        auth.delete_user("admin_two", "admin_three")

    assert any(user["username"] == "admin_two" for user in auth.list_users())


def test_main_admin_password_reset_forces_change_and_revokes_sessions(isolated_auth_db):
    auth.create_user("account_user", "initial-secure-password", role="admin")
    token = auth.issue_session({"username": "account_user", "role": "admin"})
    auth._mfa_challenges["test-challenge"] = {"username": "account_user"}

    auth.reset_user_password("account_user", "new-temporary-password", "Admin")

    reset_user = auth.authenticate("account_user", "new-temporary-password")
    assert reset_user["password_change_required"] is True
    assert auth.authenticate("account_user", "initial-secure-password") is None
    assert token not in auth._sessions
    assert "test-challenge" not in auth._mfa_challenges


def test_password_reset_is_limited_to_main_admin_and_other_accounts(isolated_auth_db):
    auth.create_user("account_user", "initial-secure-password")

    with pytest.raises(HTTPException, match="Only the Admin account"):
        auth.reset_user_password("account_user", "new-temporary-password", "admin_two")
    with pytest.raises(HTTPException, match="Use Change password"):
        auth.reset_user_password("Admin", "new-temporary-password", "Admin")
