import re
import json
import time
import gzip
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

from cryptography.fernet import Fernet

from app.config import settings
from app.models.restore import RestoreJob
from app.services.backup_service import backup_service
from app.services.notification_service import notification_service
from app.services.storage_service import storage_service

logger = logging.getLogger("dbops.restore")

CONFIRM_PHRASE = "RESTORE"
EXISTING_CONFIRM_PHRASE = "RESTORE EXISTING"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_UNSUPPORTED_PG16_SETTING_RE = re.compile(rb"^\s*SET\s+transaction_timeout\s*=", re.IGNORECASE)
_EXISTING_RESTORE_RESET_SQL = (
    b"DROP SCHEMA public CASCADE;\n"
    b"CREATE SCHEMA public;\n"
)


def _write_compatible_restore_sql(
    source: BinaryIO,
    destination: BinaryIO,
    reset_existing: bool = False,
) -> None:
    """Filter settings unsupported by PostgreSQL 16 and optionally clear public."""
    if reset_existing:
        destination.write(_EXISTING_RESTORE_RESET_SQL)
    for line in source:
        if not _UNSUPPORTED_PG16_SETTING_RE.match(line):
            destination.write(line)


def _with_database(url: str, dbname: str) -> str:
    """Returns `url` with its path (database) replaced by `dbname`."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{dbname}"))


def _database_of(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.path or "/").lstrip("/") or "postgres"


def _mask_url(url: str) -> str:
    return re.sub(r":([^@/]+)@", ":****@", url)


class RestoreService:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.metadata_file = Path(settings.BACKUP_STORAGE_DIR) / "restore_metadata.json"
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        settings.get_backup_path()
        if not self.metadata_file.exists():
            with open(self.metadata_file, "w", encoding="utf-8") as f:
                json.dump([], f)

    def _load(self) -> List[Dict[str, Any]]:
        self._ensure_storage()
        try:
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("metadata root must be a list")
            return data
        except Exception as exc:
            raise RuntimeError("Could not read restore history. Repair the restore metadata file before continuing.") from exc

    def _save(self, records: List[Dict[str, Any]]) -> None:
        self._ensure_storage()
        temp_file = self.metadata_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(records[:100], f, indent=2)
        temp_file.replace(self.metadata_file)

    def list_restores(self, limit: int = 30) -> List[RestoreJob]:
        records = self._load()
        records = records[:limit]
        return [RestoreJob(**r) for r in records]

    def get_restore(self, restore_id: str) -> Optional[RestoreJob]:
        for r in self._load():
            if r.get("restore_id") == restore_id:
                return RestoreJob(**r)
        return None

    async def register_restored_database(self, restore_id: str) -> RestoreJob:
        """Add a successful clone restore to the monitored database registry."""
        job = next((r for r in self._load() if r.get("restore_id") == restore_id), None)
        if not job:
            raise FileNotFoundError(f"Restore '{restore_id}' not found.")
        if job.get("status") != "completed" or job.get("mode") != "clone":
            raise ValueError("Only completed clone restores can be added to Targets.")
        await self._register_restored_database(job)
        records = self._load()
        for index, record in enumerate(records):
            if record.get("restore_id") == restore_id:
                records[index] = job
                break
        self._save(records)
        return RestoreJob(**job)

    def unregister_restored_database(self, restore_id: str) -> RestoreJob:
        """Remove the clone from this app's Targets without dropping its database."""
        records = self._load()
        job = next((r for r in records if r.get("restore_id") == restore_id), None)
        if not job:
            raise FileNotFoundError(f"Restore '{restore_id}' not found.")
        if job.get("status") != "completed" or job.get("mode") != "clone":
            raise ValueError("Only completed clone restores can be removed from Targets.")
        database_id = job.get("restored_database_id")
        if not database_id:
            raise ValueError("This restored database is not currently registered in Targets.")
        from app.services.database_registry import database_registry
        if not database_registry.remove_database(database_id):
            raise FileNotFoundError("The restored database was already removed from Targets.")
        job["restored_database_id"] = None
        self._save(records)
        return RestoreJob(**job)

    def clear_registered_database_reference(self, database_id: str) -> None:
        """Keep restore history in sync when a restored target is removed elsewhere."""
        records = self._load()
        changed = False
        for record in records:
            if record.get("restored_database_id") == database_id:
                record["restored_database_id"] = None
                changed = True
        if changed:
            self._save(records)

    async def _register_restored_database(self, job: Dict[str, Any]) -> None:
        from app.services.multi_db_manager import multi_db_manager
        from app.services.database_registry import database_registry

        if job.get("restored_database_id"):
            if any(item.get("id") == job["restored_database_id"] for item in database_registry._load()):
                return
            # Repair old restore records whose target was removed directly from
            # the database registry before cross-interface synchronization.
            job["restored_database_id"] = None

        destination = multi_db_manager.get_database_info(job.get("db_id"))
        if not destination:
            raise FileNotFoundError("The registered destination server is no longer available.")
        connection_url = _with_database(destination.get("connection_url", ""), job["target_database"])
        # Recover safely if registration succeeded but saving restore metadata did not.
        for item in database_registry._load():
            if item.get("connection_url") == connection_url:
                job["restored_database_id"] = item.get("id")
                return
        instance = await database_registry.add_database(
            name=f"{job['target_database']} (restored)",
            engine=job.get("engine", "postgres"),
            connection_url=connection_url,
            verify=True,
            server_id=destination.get("server_id"),
            container_id=destination.get("container_id"),
            read_only_verified=False,
        )
        job["restored_database_id"] = instance.id

    def _decrypt_if_needed(self, file_path: Path, record) -> Path:
        if not record.encrypted:
            return file_path
        fernet = Fernet(backup_service._get_encryption_key())
        with open(file_path, "rb") as f:
            decrypted = fernet.decrypt(f.read())
        temp = file_path.parent / f"restore_{uuid.uuid4().hex[:8]}.tmp"
        with open(temp, "wb") as f_out:
            f_out.write(decrypted)
        return temp

    async def _create_database(self, target_url: str, dbname: str) -> str:
        import asyncpg
        admin_url = _with_database(target_url, "postgres")
        conn = await asyncpg.connect(dsn=admin_url, timeout=10)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1;", dbname)
            if exists:
                raise RuntimeError(f"Database '{dbname}' already exists. Choose another name.")
            await conn.execute(f'CREATE DATABASE "{dbname}";')
        finally:
            await conn.close()
        return _with_database(target_url, dbname)

    async def _run(self, cmd: List[str], stdin_data: Optional[bytes] = None) -> Dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=stdin_data)
        return {
            "returncode": proc.returncode,
            "stdout": (stdout or b"").decode(errors="replace"),
            "stderr": (stderr or b"").decode(errors="replace"),
        }

    async def create_restore(
        self,
        backup_id: str,
        db_id: Optional[str] = None,
        mode: str = "clone",
        new_db_name: Optional[str] = None,
        confirm: str = "",
        source_repository: Optional[str] = None,
        source_server_id: Optional[str] = None,
    ) -> RestoreJob:
        mode = (mode or "clone").lower()
        if mode not in {"clone", "existing"}:
            raise ValueError("Restore mode must be 'clone' or 'existing'.")
        expected_confirmation = EXISTING_CONFIRM_PHRASE if mode == "existing" else CONFIRM_PHRASE
        if (confirm or "").strip() != expected_confirmation:
            raise ValueError(f"Confirmation failed. Type '{expected_confirmation}' to proceed.")

        record = backup_service.get_backup(backup_id)
        if not record:
            raise FileNotFoundError(f"Backup '{backup_id}' not found.")
        if record.status != "completed":
            raise ValueError(f"Backup '{backup_id}' is not in a restorable state (status: {record.status}).")

        remote_copy = None
        if source_server_id:
            remote_copy = next((copy for copy in (record.remote_copies or []) if copy.get("server_id") == source_server_id), None)
            if not remote_copy or not remote_copy.get("path") or not remote_copy.get("checksum_sha256"):
                raise FileNotFoundError("No checksum-verified snapshot copy is recorded on that remote server.")
            if record.checksum_sha256 and remote_copy["checksum_sha256"] != record.checksum_sha256:
                raise ValueError("Remote snapshot checksum does not match the local snapshot metadata.")
            file_path = None
            resolved_repository = f"server:{source_server_id}"
        else:
            file_path = storage_service.resolve_replica(record, source_repository)
            resolved_repository = source_repository
            if not resolved_repository:
                if file_path.resolve() == Path(record.file_path).resolve():
                    resolved_repository = "local"
                else:
                    resolved_repository = next((r.get("repository_id") for r in (record.replicas or []) if Path(r.get("file_path", "")).resolve() == file_path.resolve()), "verified-replica")

        from app.services.multi_db_manager import multi_db_manager
        target = multi_db_manager.get_database_info(db_id) if db_id else multi_db_manager.get_default_db()
        if not target:
            raise FileNotFoundError("Restore target database was not found.")
        if source_server_id and mode == "existing":
            raise ValueError("Remote snapshot copies can only be restored into a new database on a provisioned DBOps container.")
        if source_server_id and not (target.get("server_id") and target.get("container_id")):
            raise ValueError("Remote snapshot restore is limited to a newly provisioned DBOps container, so existing server databases remain untouched.")
        target_url = target.get("connection_url", settings.TARGET_DATABASE_URL)
        engine = (target.get("engine", "postgres") or "postgres").lower()
        actual_db_id = target.get("id", "db-primary")

        if engine not in ("postgres", "postgresql"):
            raise RuntimeError(f"Restore currently supports PostgreSQL targets only (requested '{engine}').")

        if self.lock.locked():
            raise RuntimeError("Another restore is already in progress. Please wait for it to finish.")

        if mode == "clone":
            new_db_name = (new_db_name or "").strip()
            if not _IDENTIFIER_RE.match(new_db_name):
                raise ValueError("Provide a valid new database name (letters, digits, underscore; must not start with a digit).")
            target_database = new_db_name
            restore_url = _with_database(target_url, target_database)
        else:
            target_database = _database_of(target_url)
            if target_database.lower() in {"postgres", "template0", "template1"}:
                raise ValueError("Choose a registered application database; system databases cannot be restore targets.")
            restore_url = target_url

        restore_id = uuid.uuid4().hex[:8]
        started_iso = datetime.now(timezone.utc).isoformat()
        job = {
            "restore_id": restore_id,
            "backup_id": backup_id,
            "db_id": actual_db_id,
            "engine": engine,
            "mode": mode,
            "target_database": target_database,
            "target_url_masked": _mask_url(restore_url),
            "status": "running",
            "started_at": started_iso,
            "completed_at": None,
            "duration_seconds": None,
            "message": None,
            "error": None,
            "log": None,
            "source_repository": resolved_repository,
        }

        async with self.lock:
            records = self._load()
            records.insert(0, job)
            self._save(records)

            await notification_service.notify(
                "restore_started",
                "Database Restore Started",
                f"Restoring backup `{backup_id}` into *{target_database}* ({mode}).",
                details={"restore_id": restore_id, "mode": mode, "target": target_database},
                severity="WARNING",
            )

            start = time.perf_counter()
            work_file = file_path
            temp_files: List[Path] = []
            try:
                if source_server_id:
                    from app.services.server_service import server_service
                    remote_stage = settings.get_backup_path() / f"remote_source_{restore_id}_{record.filename}"
                    await asyncio.to_thread(
                        server_service.download_snapshot, source_server_id, remote_copy["path"],
                        remote_stage, remote_copy["checksum_sha256"],
                    )
                    temp_files.append(remote_stage)
                    work_file = remote_stage
                # Stage remote copies locally so restore/decryption never writes to a repository.
                staged = settings.get_backup_path() / f"restore_source_{restore_id}_{record.filename}"
                if work_file != staged:
                    import shutil
                    shutil.copyfile(work_file, staged)
                    if record.checksum_sha256 and storage_service._sha256(staged) != record.checksum_sha256:
                        raise IOError("Staged snapshot checksum verification failed.")
                    temp_files.append(staged)
                    work_file = staged
                decrypted = self._decrypt_if_needed(work_file, record)
                if decrypted != work_file:
                    work_file = decrypted
                    temp_files.append(work_file)

                if mode == "clone":
                    # Clone mode creates a fresh database and never targets the registered source database.
                    restore_url = await self._create_database(target_url, new_db_name)
                    job["target_url_masked"] = _mask_url(restore_url)

                if record.format == "custom":
                    # Render custom archives to SQL, removing the PostgreSQL 17-only
                    # transaction_timeout setting so archives from newer pg_dump
                    # clients can be restored to PostgreSQL 16 targets. The setting
                    # only controls statement timeouts; omitting it preserves the
                    # schema and data while avoiding an unsupported SET error.
                    rendered_sql = settings.get_backup_path() / f"restore_{restore_id}.sql"
                    filtered_sql = settings.get_backup_path() / f"restore_{restore_id}_compatible.sql"
                    temp_files.extend([rendered_sql, filtered_sql])
                    render_cmd = [
                        "pg_restore", "--no-owner", "--no-privileges",
                        "--file", str(rendered_sql), str(work_file),
                    ]
                    render_result = await self._run(render_cmd)
                    if render_result["returncode"] != 0:
                        tail = (render_result["stderr"] or render_result["stdout"] or "unknown error").strip()
                        raise RuntimeError(f"Could not read PostgreSQL archive (code {render_result['returncode']}): {tail[-1500:]}")

                    with open(rendered_sql, "rb") as source, open(filtered_sql, "wb") as destination:
                        _write_compatible_restore_sql(
                            source,
                            destination,
                            reset_existing=mode == "existing",
                        )

                    cmd = ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1"]
                    if mode == "existing":
                        # Clear and restore public atomically so failures preserve existing data.
                        cmd.append("--single-transaction")
                    cmd += ["--dbname", restore_url, "--file", str(filtered_sql)]
                    result = await self._run(cmd)
                else:
                    sql_file = settings.get_backup_path() / f"restore_{restore_id}_compatible.sql"
                    temp_files.append(sql_file)
                    source_opener = gzip.open if record.compressed else open
                    with source_opener(work_file, "rb") as source, open(sql_file, "wb") as destination:
                        _write_compatible_restore_sql(
                            source,
                            destination,
                            reset_existing=mode == "existing",
                        )
                    cmd = ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1"]
                    if mode == "existing":
                        cmd.append("--single-transaction")
                    cmd += ["--dbname", restore_url, "--file", str(sql_file)]
                    result = await self._run(cmd)

                # pg_restore reports non-fatal warnings via a non-zero code in some cases;
                # treat code 0 strictly, otherwise surface the stderr tail.
                if result["returncode"] != 0:
                    tail = (result["stderr"] or result["stdout"] or "unknown error").strip()
                    raise RuntimeError(f"Restore command failed (code {result['returncode']}): {tail[-1500:]}")

                duration = round(time.perf_counter() - start, 2)
                job.update({
                    "status": "completed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "duration_seconds": duration,
                    "message": f"Restored into '{target_database}' successfully in {duration}s.",
                    "log": (result["stderr"] or result["stdout"] or "")[-4000:],
                })
                if mode == "clone":
                    try:
                        await self._register_restored_database(job)
                    except Exception as registry_error:
                        # The restore itself succeeded. Keep it completed and allow
                        # the user to retry adding it to Targets from restore history.
                        job["message"] += f" Target registration is pending: {registry_error}"
                        logger.warning("Restore %s completed but target registration failed: %s", restore_id, registry_error)
                logger.info(f"Restore {restore_id} completed in {duration}s into '{target_database}'.")

                await notification_service.notify(
                    "restore_success",
                    "Database Restore Completed",
                    f"Backup `{backup_id}` restored into *{target_database}* in {duration}s.",
                    details={"restore_id": restore_id, "mode": mode, "target": target_database},
                    severity="SUCCESS",
                )
            except Exception as e:
                err_str = str(e)
                job.update({
                    "status": "failed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "duration_seconds": round(time.perf_counter() - start, 2),
                    "error": err_str,
                    "message": f"Restore failed: {err_str}",
                })
                logger.error(f"Restore {restore_id} failed: {err_str}")
                await notification_service.notify(
                    "restore_failure",
                    "Database Restore Failed",
                    f"Restore of `{backup_id}` failed.\n\nError: {err_str}",
                    details={"restore_id": restore_id, "target": target_database},
                    severity="CRITICAL",
                )
            finally:
                for tmp in temp_files:
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
                records = self._load()
                for i, r in enumerate(records):
                    if r.get("restore_id") == restore_id:
                        records[i] = job
                        break
                self._save(records)

        return RestoreJob(**job)


restore_service = RestoreService()
