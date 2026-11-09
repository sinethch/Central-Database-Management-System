import asyncio
import json

import pytest

import app.services.notification_service as ns_mod
from app.config import settings
from app.services.notification_service import notification_service


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the notification settings file at a throwaway path for every test."""
    monkeypatch.setattr(ns_mod, "SETTINGS_FILE", tmp_path / "notification_settings.json")
    monkeypatch.setattr(settings, "DATABASE_CREDENTIAL_ENCRYPTION_KEY", "unit-test-credential-encryption-key")
    return tmp_path


def test_config_defaults_and_persistence(isolated):
    cfg = notification_service.get_config()
    assert {"enabled", "telegram_configured", "bot_token_masked", "chat_id", "silent", "events"}.issubset(cfg)
    for event in ns_mod.DEFAULT_EVENTS:
        assert event in cfg["events"]

    updated = notification_service.update_config({
        "chat_id": "-100123",
        "bot_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWX",
        "enabled": True,
        "events": {"health": True},
    })
    assert updated["chat_id"] == "-100123"
    assert updated["telegram_configured"] is True
    assert "…" in updated["bot_token_masked"]
    assert "ABCDEFGHIJKLMNOPQRSTUVWX" not in updated["bot_token_masked"]
    assert updated["events"]["health"] is True

    saved = json.loads((isolated / "notification_settings.json").read_text())
    assert "bot_token" not in saved
    assert saved["bot_token_enc"]
    assert notification_service._load()["bot_token"] == "123456789:ABCDEFGHIJKLMNOPQRSTUVWX"
    assert saved["enabled"] is True


def test_masked_token_is_not_written_back(isolated):
    notification_service.update_config({
        "bot_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWX",
        "chat_id": "-100999",
    })
    masked = notification_service.get_config()["bot_token_masked"]

    notification_service.update_config({"bot_token": masked})
    cfg = notification_service.get_config()
    assert cfg["telegram_configured"] is True
    assert cfg["bot_token_masked"] == masked


def test_send_test_when_disabled(isolated):
    notification_service.update_config({"enabled": False})
    res = asyncio.run(notification_service.send_test("hello"))
    assert res["success"] is False
    assert res["status"] == "disabled"


def test_notify_respects_event_toggles(isolated, monkeypatch):
    notification_service.update_config({
        "enabled": True,
        "bot_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWX",
        "chat_id": "-100999",
        "events": {"backup_success": False},
    })

    muted = asyncio.run(notification_service.notify("backup_success", "Title", "Body"))
    assert muted["status"] == "muted"

    captured = {}

    async def fake_send(text):
        captured["text"] = text
        return {"success": True, "status": "sent", "message": "ok"}

    monkeypatch.setattr(notification_service, "send_message", fake_send)
    ok = asyncio.run(notification_service.notify("backup_failure", "Backup down", "boom", {"k": "v"}, "CRITICAL"))
    assert ok["success"] is True
    assert "Backup down" in captured["text"]
    assert "k" in captured["text"]


def test_notify_disabled_short_circuits(isolated):
    notification_service.update_config({"enabled": False})
    res = asyncio.run(notification_service.notify("alert", "t", "m"))
    assert res["status"] == "disabled"
