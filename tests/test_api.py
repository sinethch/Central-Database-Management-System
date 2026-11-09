import asyncio
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app import auth

import app.services.database_registry as registry_module
import app.services.notification_service as notification_module
from app.services.backup_service import backup_service
from app.services.scheduler_service import scheduler_service

client = TestClient(app)
VALID_HEADERS = {"X-API-Key": settings.API_KEY}
INVALID_HEADERS = {"X-API-Key": "wrong-key"}

def test_main_admin_can_reset_and_delete_another_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_STORAGE_DIR", str(tmp_path))
    auth._sessions.clear()
    auth._mfa_challenges.clear()
    try:
        auth.initialize_users()
        if not any(user["username"].casefold() == "admin" for user in auth.list_users()):
            auth.create_user("Admin", "main-admin-test-password", role="admin")
        auth.create_user("target_admin", "initial-test-password", role="admin")
        token = auth.issue_session({"username": "Admin", "role": "admin", "password_change_required": False})
        headers = {"Authorization": f"Bearer {token}"}

        reset = client.post(
            "/api/v1/auth/users/target_admin/password",
            headers=headers,
            json={"new_password": "temporary-test-password"},
        )
        assert reset.status_code == 200
        assert auth.authenticate("target_admin", "temporary-test-password")["password_change_required"] is True

        deleted = client.delete("/api/v1/auth/users/target_admin", headers=headers)
        assert deleted.status_code == 200
        assert not any(user["username"] == "target_admin" for user in auth.list_users())
    finally:
        auth._sessions.clear()
        auth._mfa_challenges.clear()


def test_authenticated_websocket_broadcasts_activity_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_STORAGE_DIR", str(tmp_path))
    auth._sessions.clear()
    auth._mfa_challenges.clear()
    try:
        auth.initialize_users()
        token = auth.issue_session({"username": "Admin", "role": "admin", "password_change_required": False})
        with client.websocket_connect("/api/v1/ws") as websocket:
            websocket.send_json({"token": token})
            assert websocket.receive_json() == {"type": "ready"}

            response = client.post(
                "/api/v1/auth/activity",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "client_id": "websocket-test-client-01",
                    "kind": "user",
                    "title": "Real-time test event",
                    "detail": "",
                    "ts": 1794240000000,
                },
            )
            assert response.status_code == 201
            assert websocket.receive_json() == {"type": "update", "topic": "activity"}
    finally:
        auth._sessions.clear()
        auth._mfa_challenges.clear()


def test_backups_page_includes_snapshot_section():
    """The Backups workspace keeps its table and trend without other workspace content."""
    response = client.get("/backups")

    assert response.status_code == 200
    assert '<div class="flex-row split-row backups-split">' in response.text
    assert 'id="backupsTableBody"' in response.text
    assert 'id="backupSizeTrend"' in response.text
    assert 'id="restoresTableBody"' not in response.text
    assert 'id="slowQueriesTableBody"' not in response.text


def test_console_uses_original_hero_and_has_separate_post_login_welcome():
    response = client.get("/console")

    assert response.status_code == 200
    assert 'class="hero reveal" id="command-center"' in response.text
    assert 'class="tunnel-hero reveal" id="command-center"' not in response.text
    assert 'class="console-workspace"' in response.text
    assert 'class="db-pill-list db-target-carousel" role="region"' in response.text
    assert 'id="activeDbTitle"' in response.text
    assert 'id="remoteServers"' in response.text
    assert response.text.index('id="target-selector"') < response.text.index('id="activeDbTitle"')
    assert response.text.index('id="activeDbTitle"') < response.text.index('id="remoteServers"')
    welcome = response.text.split('<section id="welcomeExperience"', 1)[1].split("</section>", 1)[0]
    assert 'id="welcomeTunnelCards"' in welcome
    assert 'id="welcomeUser"' in welcome
    assert 'id="welcomeContinue"' in welcome
    assert 'id="heroStatusText"' not in welcome
    assert 'data-action="open-backup"' not in welcome
    assert 'href="#target-selector"' not in welcome
    assert "images.unsplash.com" not in response.text
    assert "cdn.21st.dev" not in response.text
    provision_dialog = response.text.split('<dialog id="provisionDbDialog"', 1)[1].split("</dialog>", 1)[0]
    assert 'id="provisionNotice"' in provision_dialog
    assert 'aria-live="polite"' in provision_dialog


@pytest.mark.parametrize(
    ("path", "page", "included", "excluded"),
    [
        ("/console", "console", 'id="remoteServers"', 'id="backupsTableBody"'),
        ("/overview", "overview", 'id="slowQueriesTableBody"', 'id="remoteServers"'),
        ("/restore", "restore", 'id="restoresTableBody"', 'id="backupsTableBody"'),
        ("/restored-databases", "restored-databases", 'id="restoredDatabasesBody"', 'id="restoresTableBody"'),
        ("/storage", "storage", 'id="tablesTableBody"', 'id="slowQueriesTableBody"'),
        ("/activity", "activity", 'id="activityList"', 'id="restoresTableBody"'),
    ],
)
def test_workspace_pages_only_include_relevant_sections(path, page, included, excluded):
    response = client.get(path)

    assert response.status_code == 200
    assert f'data-page="{page}"' in response.text
    assert included in response.text
    assert excluded not in response.text
    assert "<!-- workspace:" not in response.text


@pytest.fixture(autouse=True)
def isolate_persistent_services(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_CREDENTIAL_ENCRYPTION_KEY", "unit-test-credential-encryption-key")
    monkeypatch.setattr(backup_service, "metadata_file", tmp_path / "backup_metadata.json")
    monkeypatch.setattr(registry_module, "DATABASES_FILE", tmp_path / "registered_databases.json")
    monkeypatch.setattr(notification_module, "SETTINGS_FILE", tmp_path / "notification_settings.json")
    monkeypatch.setattr(scheduler_service, "settings_file", tmp_path / "backup_schedule.json")


def test_health_endpoint():
    """Health endpoint returns status and structure without authentication required."""
    with patch("app.services.multi_db_manager.multi_db_manager.get_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = {
            "timestamp": "2026-10-14T10:00:00Z",
            "connected": True,
            "latency_ms": 1.25,
            "version": "PostgreSQL 16.2",
            "uptime": "2 days 04:12:00",
            "started_at": "2026-10-14T10:00:00Z",
            "error": None,
            "status": "healthy",
        }
        response = client.get("/api/v1/health", headers=VALID_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["connected"] is True
        assert data["latency_ms"] == 1.25
        assert "PostgreSQL 16.2" in data["version"]


def test_auth_rejection():
    """Protected endpoints reject requests with missing or invalid API keys."""
    resp_no_key = client.get("/api/v1/backups")
    assert resp_no_key.status_code == 401

    resp_wrong_key = client.get("/api/v1/backups", headers=INVALID_HEADERS)
    assert resp_wrong_key.status_code == 401


def test_list_backups_authenticated():
    """Listing backups succeeds with valid API key."""
    response = client.get("/api/v1/backups", headers=VALID_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert "backups" in data
    assert "storage_directory" in data
    assert "retention_days" in data


def test_backup_schedule_endpoints():
    """Get and update backup schedule endpoints work correctly."""
    # GET schedule
    get_res = client.get("/api/v1/backups/schedule", headers=VALID_HEADERS)
    assert get_res.status_code == 200
    assert "cron" in get_res.json()

    # POST new schedule
    payload = {
        "cron": "30 3 * * *",
        "enabled": True,
        "format": "custom"
    }
    post_res = client.post("/api/v1/backups/schedule", json=payload, headers=VALID_HEADERS)
    assert post_res.status_code == 200
    data = post_res.json()
    assert data["cron"] == "30 3 * * *"
    assert data["enabled"] is True


def test_alert_config_endpoints():
    """Alerts configuration retrieval and updates work."""
    get_res = client.get("/api/v1/alerts/config", headers=VALID_HEADERS)
    assert get_res.status_code == 200
    assert "webhook_configured" in get_res.json()

    # Update threshold
    update_res = client.post(
        "/api/v1/alerts/config",
        json={"max_connection_warning_percent": 85.0},
        headers=VALID_HEADERS
    )
    assert update_res.status_code == 200
    assert update_res.json()["max_connection_warning_percent"] == 85.0


def test_metrics_connections_mocked():
    """Connection metrics return expected structure and calculations."""
    with patch("app.services.multi_db_manager.multi_db_manager.get_connections", new_callable=AsyncMock) as mock_connections:
        mock_connections.return_value = {
            "total_connections": 30,
            "max_connections": 100,
            "connection_usage_percent": 30.0,
            "warning_threshold_percent": 80.0,
            "is_warning": False,
            "states": {"active": 10, "idle": 20},
            "by_database": {"demo_db": 30},
            "by_user": {"app_user": 30},
        }

        res = client.get("/api/v1/metrics/connections", headers=VALID_HEADERS)
        assert res.status_code == 200
        data = res.json()
        assert data["total_connections"] == 30
        assert data["max_connections"] == 100
        assert data["connection_usage_percent"] == 30.0
        assert data["is_warning"] is False


def test_database_registry_endpoints():
    """Registering and listing external databases in centralized DevOps registry."""
    # List databases
    list_res = client.get("/api/v1/databases", headers=VALID_HEADERS)
    assert list_res.status_code == 200
    assert "databases" in list_res.json()

    # Register MongoDB database
    payload = {
        "name": "DevOps Mongo Analytics",
        "engine": "mongodb",
        "connection_url": "mongodb://telemetry_user:secret123@mongo-host:27017/analytics",
        "verify_connectivity": False
    }
    with patch.object(registry_module.database_registry, "verify_connection", new_callable=AsyncMock) as verify:
        verify.return_value = {"connected": True, "latency_ms": 1.0, "version": "MongoDB test", "error": None}
        create_res = client.post("/api/v1/databases", json=payload, headers=VALID_HEADERS)
    assert create_res.status_code == 200
    db_data = create_res.json()
    assert db_data["name"] == "DevOps Mongo Analytics"
    assert db_data["engine"] == "mongodb"
    assert "secret123" not in db_data["connection_url"], "Password must be masked in API output"


def test_admin_can_rename_target_display_name_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry_module, "DATABASES_FILE", tmp_path / "registered_databases.json")
    auth._sessions.clear()
    auth._mfa_challenges.clear()
    try:
        auth.initialize_users()
        target = asyncio.run(registry_module.database_registry.add_database(
            name="Original target name",
            engine="postgres",
            connection_url="postgresql://dbuser:dbpassword@localhost:5432/appdb",
            verify=False,
        ))
        admin_token = auth.issue_session({"username": "Admin", "role": "admin", "password_change_required": False})
        response = client.patch(
            f"/api/v1/databases/{target.id}/name",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "QA target"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "QA target"
        saved = next(item for item in registry_module.database_registry._load() if item["id"] == target.id)
        assert saved["name"] == "QA target"
        assert saved["connection_url"] == "postgresql://dbuser:dbpassword@localhost:5432/appdb"

        auth.create_user("regular_user", "a-secure-test-password")
        user_token = auth.issue_session({"username": "regular_user", "role": "user", "password_change_required": False})
        denied = client.patch(
            f"/api/v1/databases/{target.id}/name",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"name": "Unauthorized rename"},
        )
        assert denied.status_code == 403
    finally:
        auth._sessions.clear()
        auth._mfa_challenges.clear()


def test_actions_endpoints():
    """Testing DevOps action endpoints: ping and kill-query."""
    with patch("app.services.multi_db_manager.multi_db_manager.get_health", new_callable=AsyncMock) as mock_health, \
         patch("app.services.multi_db_manager.multi_db_manager.kill_query", new_callable=AsyncMock) as mock_kill:
        mock_health.return_value = {
            "connected": True,
            "latency_ms": 1.5,
            "version": "PostgreSQL 16",
            "uptime": "1 day",
            "started_at": None,
            "error": None,
            "status": "healthy",
            "timestamp": "2026-10-14T10:00:00Z",
        }
        mock_kill.return_value = {"success": True}
        ping_res = client.post("/api/v1/actions/ping", json={"db_id": "db-primary"}, headers=VALID_HEADERS)
        assert ping_res.status_code == 200
        assert ping_res.json()["connected"] is True

        kill_res = client.post("/api/v1/actions/kill-query", json={"db_id": "db-primary", "pid": 12345}, headers=VALID_HEADERS)
        assert kill_res.status_code == 200
        assert kill_res.json()["success"] is True


def test_notification_config_endpoint():
    """Telegram notification config is exposed with a masked token and auth enforced."""
    assert client.get("/api/v1/notifications/config").status_code == 401

    res = client.get("/api/v1/notifications/config", headers=VALID_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert "events" in data
    assert "bot_token_masked" in data
    assert "telegram_configured" in data


def test_restores_endpoints():
    """Restore listing is auth-protected and restore creation requires confirmation."""
    assert client.get("/api/v1/restores").status_code == 401

    list_res = client.get("/api/v1/restores", headers=VALID_HEADERS)
    assert list_res.status_code == 200
    assert "restores" in list_res.json()

    bad = client.post(
        "/api/v1/restores",
        json={"backup_id": "does-not-exist", "confirm": "wrong", "mode": "overwrite"},
        headers=VALID_HEADERS,
    )
    assert bad.status_code == 400
