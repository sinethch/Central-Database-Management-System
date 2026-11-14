import asyncio

import app.services.scheduler_service as scheduler_module
from app.services.scheduler_service import SchedulerService


def test_migration_monitor_notifies_new_entries_and_saves_baseline(tmp_path, monkeypatch):
    service = SchedulerService()
    monkeypatch.setattr(service, "migration_state_file", tmp_path / "migration_state.json")
    monkeypatch.setattr(scheduler_module.database_registry, "_load", lambda: [
        {"id": "db-1", "name": "Production", "engine": "postgres"},
    ])
    monkeypatch.setattr(scheduler_module.migration_history_service, "get_history", lambda db_id: asyncio.sleep(0, result={
        "supported": True,
        "status": "detected",
        "database_name": "appdb",
        "migrations": [{"framework": "Flyway", "version": "42", "name": "add_index", "applied_at": "2026-11-12T00:00:00", "status": "success"}],
    }))
    sent = []

    async def notify(*args):
        sent.append(args)
        return {"success": True}

    monkeypatch.setattr(scheduler_module.notification_service, "notify", notify)
    asyncio.run(service._check_migration_changes())

    assert len(sent) == 1
    assert sent[0][0] == "migration_detected"
    assert "appdb" in sent[0][2]
    assert "Flyway" in sent[0][3]["Migrations"]
    assert "db-1" in service._load_migration_state()


def test_partial_migration_read_does_not_advance_baseline(tmp_path, monkeypatch):
    service = SchedulerService()
    monkeypatch.setattr(service, "migration_state_file", tmp_path / "migration_state.json")
    monkeypatch.setattr(scheduler_module.database_registry, "_load", lambda: [
        {"id": "db-1", "name": "Production", "engine": "postgres"},
    ])
    monkeypatch.setattr(scheduler_module.migration_history_service, "get_history", lambda db_id: asyncio.sleep(0, result={
        "supported": True,
        "status": "partial",
        "database_name": "appdb",
        "migrations": [],
    }))
    sent = []

    async def notify(*args):
        sent.append(args)
        return {"success": True}

    monkeypatch.setattr(scheduler_module.notification_service, "notify", notify)
    asyncio.run(service._check_migration_changes())

    assert not sent
    assert service._load_migration_state() == {}


def test_unrecognized_ledger_does_not_advance_baseline(tmp_path, monkeypatch):
    service = SchedulerService()
    monkeypatch.setattr(service, "migration_state_file", tmp_path / "migration_state.json")
    monkeypatch.setattr(scheduler_module.database_registry, "_load", lambda: [
        {"id": "db-1", "name": "Production", "engine": "postgres"},
    ])
    monkeypatch.setattr(scheduler_module.migration_history_service, "get_history", lambda db_id: asyncio.sleep(0, result={
        "supported": True,
        "status": "not_detected",
        "database_name": "appdb",
        "migrations": [],
    }))
    sent = []

    async def notify(*args):
        sent.append(args)
        return {"success": True}

    monkeypatch.setattr(scheduler_module.notification_service, "notify", notify)
    asyncio.run(service._check_migration_changes())

    assert not sent
    assert service._load_migration_state() == {}


def test_failed_migration_notification_does_not_advance_baseline(tmp_path, monkeypatch):
    service = SchedulerService()
    monkeypatch.setattr(service, "migration_state_file", tmp_path / "migration_state.json")
    monkeypatch.setattr(scheduler_module.database_registry, "_load", lambda: [
        {"id": "db-1", "name": "Production", "engine": "postgres"},
    ])
    monkeypatch.setattr(scheduler_module.migration_history_service, "get_history", lambda db_id: asyncio.sleep(0, result={
        "supported": True,
        "status": "detected",
        "database_name": "appdb",
        "migrations": [{"framework": "Flyway", "version": "43", "name": "add_column", "applied_at": "2026-11-13T00:00:00", "status": "success"}],
    }))

    async def notify(*args):
        return {"success": False, "status": "disabled"}

    monkeypatch.setattr(scheduler_module.notification_service, "notify", notify)
    asyncio.run(service._check_migration_changes())

    assert service._load_migration_state() == {}

