import asyncio
import gzip
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.services.notification_service as ns_mod
from app.services.multi_db_manager import multi_db_manager
from app.services.restore_service import (
    restore_service,
    _with_database,
    _database_of,
    _mask_url,
    _write_compatible_restore_sql,
)
from app.services.backup_service import backup_service
from app.config import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Isolate metadata + reset the asyncio lock (loop-bound on first await)."""
    monkeypatch.setattr(settings, "BACKUP_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(restore_service, "metadata_file", tmp_path / "restore_metadata.json")
    monkeypatch.setattr(backup_service, "metadata_file", tmp_path / "backup_metadata.json")
    monkeypatch.setattr(
        multi_db_manager,
        "get_default_db",
        lambda: {
            "id": "db-primary",
            "engine": "postgres",
            "connection_url": "postgresql://test:test@localhost:5432/app_db",
        },
    )
    restore_service.lock = asyncio.Lock()
    (tmp_path / "backup_metadata.json").write_text("[]", encoding="utf-8")
    return tmp_path


def _seed_backup(tmp_path, backup_id="bk123456", fmt="custom", encrypt=False, compress=True):
    extension = ".dump" if fmt == "custom" else (".sql.gz" if compress else ".sql")
    archive = tmp_path / f"{backup_id}{extension}"
    payload = b"PGDMP mock archive payload"
    if fmt == "plain":
        sql = b"SET transaction_timeout = 0;\nCREATE TABLE public.snapshot_probe (id integer);\n"
        payload = gzip.compress(sql) if compress else sql
    archive.write_bytes(payload)
    record = {
        "backup_id": backup_id,
        "db_id": "db-primary",
        "filename": archive.name,
        "file_path": str(archive),
        "format": fmt,
        "size_bytes": archive.stat().st_size,
        "size_human": "24 B",
        "compressed": compress,
        "encrypted": encrypt,
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": 0.4,
        "age_days": 0.0,
        "description": "test seed",
        "error": None,
        "checksum_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    backup_service._save_metadata([record])
    return record


def test_url_helpers():
    assert _database_of("postgresql://u:p@host:5432/demo_db") == "demo_db"
    assert _database_of("postgresql://u:p@host:5432") == "postgres"
    assert _with_database("postgresql://u:p@host:5432/demo_db", "clone_x") == "postgresql://u:p@host:5432/clone_x"
    masked = _mask_url("postgresql://user:secret@host:5432/demo_db")
    assert "secret" not in masked and "****" in masked


def test_confirm_phrase_is_mandatory():
    with pytest.raises(ValueError, match="Confirmation failed"):
        asyncio.run(restore_service.create_restore(backup_id="anything", confirm="nope"))


def test_invalid_mode_rejected():
    with pytest.raises(ValueError, match="mode must be"):
        asyncio.run(restore_service.create_restore(backup_id="anything", confirm="RESTORE", mode="destroy"))


def test_unknown_backup_returns_not_found():
    with pytest.raises(FileNotFoundError):
        asyncio.run(restore_service.create_restore(backup_id="missing", confirm="RESTORE", mode="clone"))


def test_clone_requires_valid_database_name(tmp_path):
    _seed_backup(tmp_path)
    with pytest.raises(ValueError, match="valid new database name"):
        asyncio.run(restore_service.create_restore(
            backup_id="bk123456", confirm="RESTORE", mode="clone", new_db_name="1bad-name"))


def test_existing_restore_clears_public_schema_atomically(tmp_path, monkeypatch):
    _seed_backup(tmp_path, fmt="custom")
    commands = []
    executed_sql = None

    async def fake_run(cmd, stdin_data=None):
        nonlocal executed_sql
        commands.append(cmd)
        if cmd[0] == "pg_restore":
            sql_path = cmd[cmd.index("--file") + 1]
            Path(sql_path).write_bytes(b"CREATE TABLE public.snapshot_probe (id integer);\n")
        elif cmd[0] == "psql":
            executed_sql = Path(cmd[cmd.index("--file") + 1]).read_bytes()
        return {"returncode": 0, "stdout": "restore complete", "stderr": ""}

    async def fake_notify(*args, **kwargs):
        return {"success": True, "status": "sent"}

    monkeypatch.setattr(restore_service, "_run", fake_run)
    monkeypatch.setattr(ns_mod.notification_service, "notify", fake_notify)

    job = asyncio.run(restore_service.create_restore(
        backup_id="bk123456", confirm="RESTORE EXISTING", mode="existing"))

    assert job.status == "completed"
    assert job.mode == "existing"
    assert job.error is None
    assert job.duration_seconds is not None
    assert "*" in job.target_url_masked
    assert executed_sql is not None
    assert executed_sql.startswith(b"DROP SCHEMA public CASCADE;\nCREATE SCHEMA public;\n")
    assert "--single-transaction" in commands[1]

    jobs = restore_service.list_restores()
    assert len(jobs) == 1
    assert jobs[0].restore_id == job.restore_id


def test_failed_restore_is_recorded(tmp_path, monkeypatch):
    _seed_backup(tmp_path, fmt="custom")
    command_count = 0

    async def failing_run(cmd, stdin_data=None):
        nonlocal command_count
        command_count += 1
        if cmd[0] == "pg_restore":
            sql_path = cmd[cmd.index("--file") + 1]
            Path(sql_path).write_bytes(b"CREATE TABLE public.snapshot_probe (id integer);\n")
            return {"returncode": 0, "stdout": "", "stderr": ""}
        return {"returncode": 1, "stdout": "", "stderr": "permission denied for schema public"}

    async def fake_notify(*args, **kwargs):
        return {"success": True, "status": "sent"}

    monkeypatch.setattr(restore_service, "_run", failing_run)
    monkeypatch.setattr(ns_mod.notification_service, "notify", fake_notify)

    job = asyncio.run(restore_service.create_restore(
        backup_id="bk123456", confirm="RESTORE EXISTING", mode="existing"))

    assert command_count == 2
    assert job.status == "failed"
    assert "permission denied" in (job.error or "")
    assert restore_service.get_restore(job.restore_id) is not None


def test_plain_restore_filters_postgres_17_setting_and_clears_existing_schema():
    import io

    source = io.BytesIO(
        b"SET statement_timeout = 0;\n"
        b"SET transaction_timeout = 0;\n"
        b"CREATE TABLE public.snapshot_probe (id integer);\n"
    )
    destination = io.BytesIO()

    _write_compatible_restore_sql(source, destination, reset_existing=True)

    sql = destination.getvalue()
    assert sql.startswith(b"DROP SCHEMA public CASCADE;\nCREATE SCHEMA public;\n")
    assert b"SET statement_timeout = 0;" in sql
    assert b"SET transaction_timeout" not in sql
    assert b"CREATE TABLE public.snapshot_probe" in sql


def test_plain_existing_restore_uses_filtered_script(tmp_path, monkeypatch):
    record = _seed_backup(tmp_path, fmt="plain", compress=True)
    commands = []
    executed_sql = None

    async def fake_run(cmd, stdin_data=None):
        nonlocal executed_sql
        commands.append(cmd)
        executed_sql = Path(cmd[cmd.index("--file") + 1]).read_bytes()
        return {"returncode": 0, "stdout": "restore complete", "stderr": ""}

    async def fake_notify(*args, **kwargs):
        return {"success": True, "status": "sent"}

    monkeypatch.setattr(restore_service, "_run", fake_run)
    monkeypatch.setattr(ns_mod.notification_service, "notify", fake_notify)

    job = asyncio.run(restore_service.create_restore(
        backup_id=record["backup_id"],
        confirm="RESTORE EXISTING",
        mode="existing",
    ))

    assert job.status == "completed"
    assert commands[0][0] == "psql"
    assert executed_sql is not None
    assert b"DROP SCHEMA public CASCADE;" in executed_sql
    assert b"SET transaction_timeout" not in executed_sql
    assert b"CREATE TABLE public.snapshot_probe" in executed_sql
    assert "--single-transaction" in commands[0]
