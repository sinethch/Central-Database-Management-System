import os
import sys
import gzip
import json
import uuid
import time
import shutil
import hashlib
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

from cryptography.fernet import Fernet
import base64

from app.config import settings
from app.models.backup import BackupMetadata
from app.services.alert_service import alert_service
from app.services.notification_service import notification_service

logger = logging.getLogger("dbops.backup")


def _human_size(bytes_val: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.2f} PB"


class BackupService:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.metadata_file = Path(settings.BACKUP_STORAGE_DIR) / "backup_metadata.json"
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        path = settings.get_backup_path()
        if not self.metadata_file.exists():
            with open(self.metadata_file, "w", encoding="utf-8") as f:
                json.dump([], f)

    def _get_encryption_key(self) -> bytes:
        """Derives a 32-byte urlsafe base64 key suitable for Fernet AES-256."""
        raw_key = (settings.BACKUP_ENCRYPTION_KEY or "").strip()
        if len(raw_key) < 32 or raw_key in {
            "dbops-control-plane-default-key-32b!",
            "replace-with-a-separate-long-random-secret",
        }:
            raise RuntimeError("Set BACKUP_ENCRYPTION_KEY to a stable random secret of at least 32 characters before encrypting snapshots.")
        digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    def _load_metadata(self) -> List[Dict[str, Any]]:
        self._ensure_storage()
        try:
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list):
                raise ValueError("metadata root must be a list")
            return records
        except Exception as e:
            raise RuntimeError("Could not read backup metadata. Restore or repair the metadata file before continuing.") from e

    def _save_metadata(self, records: List[Dict[str, Any]]) -> None:
        self._ensure_storage()
        temp_path = self.metadata_file.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
        temp_path.replace(self.metadata_file)

    def list_backups(self, db_id: Optional[str] = None) -> List[BackupMetadata]:
        records = self._load_metadata()
        now = datetime.now(timezone.utc)
        results = []
        for r in records:
            if db_id and r.get("db_id") and r.get("db_id") != db_id:
                continue
            # Calculate dynamic age
            try:
                created_dt = datetime.fromisoformat(r["created_at"])
                age_days = round((now - created_dt).total_seconds() / 86400.0, 2)
            except Exception:
                age_days = 0.0
            r["age_days"] = age_days
            results.append(BackupMetadata(**r))
        # Sort newest first
        results.sort(key=lambda x: x.created_at, reverse=True)
        return results

    def get_backup(self, backup_id: str) -> Optional[BackupMetadata]:
        records = self._load_metadata()
        for r in records:
            if r["backup_id"] == backup_id:
                now = datetime.now(timezone.utc)
                try:
                    created_dt = datetime.fromisoformat(r["created_at"])
                    r["age_days"] = round((now - created_dt).total_seconds() / 86400.0, 2)
                except Exception:
                    r["age_days"] = 0.0
                return BackupMetadata(**r)
        return None

    async def delete_backup(self, backup_id: str) -> bool:
        """Remove a snapshot's local archive and registry entry only.

        Repository replicas and remote-server copies are intentionally left untouched.
        """
        async with self.lock:
            records = self._load_metadata()
            record = next((item for item in records if item.get("backup_id") == backup_id), None)
            if record is None:
                return False
            if record.get("status") == "in_progress":
                raise RuntimeError("An in-progress snapshot cannot be deleted.")

            storage_root = settings.get_backup_path().resolve()
            archive_path = Path(record.get("file_path", "")).resolve()
            try:
                archive_path.relative_to(storage_root)
            except (ValueError, OSError):
                raise ValueError("Snapshot archive is outside the application's local backup directory.")

            # Only unlink the app-managed local archive. Never remove repository or
            # remote copies referenced by replicas/remote_copies in metadata.
            if archive_path.exists():
                if not archive_path.is_file():
                    raise ValueError("Snapshot archive path is not a regular file.")
                archive_path.unlink()
            records.remove(record)
            self._save_metadata(records)
            return True

    def _calculate_sha256(self, file_path: Path) -> str:
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                sha.update(chunk)
        return sha.hexdigest()

    async def create_backup(
        self,
        db_id: Optional[str] = None,
        format_type: str = "custom",
        compress: bool = True,
        encrypt: bool = False,
        description: Optional[str] = None
    ) -> BackupMetadata:
        """
        Executes a PostgreSQL MVCC snapshot via pg_dump. Snapshot creation can add
        database and storage load; it does not guarantee zero operational impact.
        """
        from app.services.multi_db_manager import multi_db_manager
        target = multi_db_manager.get_database_info(db_id) if db_id else multi_db_manager.get_default_db()
        if not target:
            raise FileNotFoundError("Backup target database was not found.")
        target_url = target.get("connection_url", settings.TARGET_DATABASE_URL)
        target_engine = target.get("engine", "postgres").lower()
        actual_db_id = target.get("id", "db-primary")
        if target_engine not in {"postgres", "postgresql"}:
            raise ValueError(f"Backup creation currently supports PostgreSQL targets only (selected: '{target_engine}').")
        if format_type not in {"custom", "plain"}:
            raise ValueError("Backup format must be 'custom' or 'plain'.")

        if self.lock.locked():
            raise RuntimeError("Another backup is currently in progress. Please wait for it to complete.")

        async with self.lock:
            backup_id = str(uuid.uuid4())[:8]
            timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            storage_path = settings.get_backup_path()

            # Determine file extension
            if target_engine == "mongodb":
                ext = ".archive.gz" if compress else ".archive"
            elif format_type == "custom":
                ext = ".dump"
            else:
                ext = ".sql.gz" if compress else ".sql"

            if encrypt:
                ext += ".enc"

            filename = f"{target_engine}_backup_{timestamp_str}_{backup_id}{ext}"
            target_file = storage_path / filename
            temp_file = storage_path / f"temp_{filename}"

            start_time = time.perf_counter()
            start_iso = datetime.now(timezone.utc).isoformat()

            # Initial metadata entry
            initial_record = {
                "backup_id": backup_id,
                "db_id": actual_db_id,
                "filename": filename,
                "file_path": str(target_file),
                "format": format_type,
                "size_bytes": 0,
                "size_human": "0 B",
                "compressed": compress,
                "encrypted": encrypt,
                "status": "in_progress",
                "created_at": start_iso,
                "completed_at": None,
                "duration_seconds": None,
                "age_days": 0.0,
                "description": description,
                "error": None,
                "checksum_sha256": None,
            }

            records = self._load_metadata()
            records.append(initial_record)
            self._save_metadata(records)

            try:
                # Build command according to engine
                if target_engine in ["postgres", "postgresql"]:
                    cmd = [
                        "pg_dump",
                        "--dbname", target_url,
                        "--no-owner",
                        "--no-privileges",
                    ]

                if format_type == "custom":
                    cmd.extend(["--format=custom", "--file", str(temp_file)])
                    if compress:
                        cmd.extend(["--compress=6"])
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await proc.communicate()

                else:
                    # Write plain dumps to disk directly rather than buffering the
                    # entire database dump in application memory.
                    cmd.extend(["--format=plain", "--file", str(temp_file)])
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await proc.communicate()

                if proc.returncode != 0:
                    err_msg = stderr.decode().strip() or "pg_dump failed without stderr"
                    raise RuntimeError(f"pg_dump error (code {proc.returncode}): {err_msg}")

                if format_type == "plain" and compress:
                    compressed_temp = storage_path / f"compressed_{filename}"
                    with open(temp_file, "rb") as source, gzip.open(compressed_temp, "wb") as destination:
                        shutil.copyfileobj(source, destination)
                    temp_file.unlink(missing_ok=True)
                    temp_file = compressed_temp

                # Optional AES-256 Encryption
                if encrypt:
                    fernet = Fernet(self._get_encryption_key())
                    with open(temp_file, "rb") as f_in:
                        raw_bytes = f_in.read()
                    encrypted_data = fernet.encrypt(raw_bytes)
                    with open(target_file, "wb") as f_out:
                        f_out.write(encrypted_data)
                    temp_file.unlink(missing_ok=True)
                else:
                    temp_file.rename(target_file)

                duration = round(time.perf_counter() - start_time, 2)
                size_bytes = target_file.stat().st_size
                checksum = self._calculate_sha256(target_file)
                completed_iso = datetime.now(timezone.utc).isoformat()

                # Update metadata record
                for r in records:
                    if r["backup_id"] == backup_id:
                        r["status"] = "completed"
                        r["size_bytes"] = size_bytes
                        r["size_human"] = _human_size(size_bytes)
                        r["completed_at"] = completed_iso
                        r["duration_seconds"] = duration
                        r["checksum_sha256"] = checksum
                        break
                self._save_metadata(records)

                # Run automatic retention cleanup
                self.enforce_retention()

                logger.info(f"Backup {backup_id} completed successfully in {duration}s ({_human_size(size_bytes)})")
                await notification_service.notify(
                    "backup_success",
                    "Database Backup Completed",
                    f"Snapshot `{backup_id}` captured successfully.",
                    details={
                        "database": actual_db_id,
                        "size": _human_size(size_bytes),
                        "duration": f"{duration}s",
                        "format": format_type,
                        "encrypted": encrypt,
                    },
                    severity="SUCCESS",
                )
                return BackupMetadata(**[r for r in records if r["backup_id"] == backup_id][0])

            except Exception as e:
                temp_file.unlink(missing_ok=True)
                target_file.unlink(missing_ok=True)
                err_str = str(e)
                logger.error(f"Backup {backup_id} failed: {err_str}")

                for r in records:
                    if r["backup_id"] == backup_id:
                        r["status"] = "failed"
                        r["error"] = err_str
                        r["completed_at"] = datetime.now(timezone.utc).isoformat()
                        break
                self._save_metadata(records)

                # Telegram notification (dedicated backup_failure event)
                await notification_service.notify(
                    "backup_failure",
                    "Database Backup Failed",
                    f"Backup `{backup_id}` failed.",
                    details={"database": actual_db_id, "error": err_str},
                    severity="CRITICAL",
                )

                # Alert on backup failure (webhook/email; Telegram handled above)
                await alert_service.send_alert(
                    title="Database Backup Failed",
                    message=f"Backup ID: {backup_id}\nError: {err_str}",
                    severity="CRITICAL",
                    details={"backup_id": backup_id, "error": err_str},
                    send_telegram=False,
                )

                raise RuntimeError(err_str)

    async def verify_backup(self, backup_id: str) -> Dict[str, Any]:
        """
        Validates backup archive integrity without restoring to production.
        Checks checksum, archive format, and uses pg_restore --list on custom dumps.
        """
        record = self.get_backup(backup_id)
        if not record:
            raise FileNotFoundError(f"Backup {backup_id} not found.")

        file_path = Path(record.file_path)
        if not file_path.exists():
            return {
                "backup_id": backup_id,
                "valid": False,
                "format": record.format,
                "details": "Backup file is missing from storage disk.",
                "error": "File not found",
                "verified_at": datetime.now(timezone.utc).isoformat()
            }

        # Step 1: Verify sha256 checksum
        current_sha = self._calculate_sha256(file_path)
        if record.checksum_sha256 and current_sha != record.checksum_sha256:
            return {
                "backup_id": backup_id,
                "valid": False,
                "format": record.format,
                "details": "Checksum mismatch. Archive may be corrupted or altered.",
                "error": "Checksum verification failed",
                "verified_at": datetime.now(timezone.utc).isoformat()
            }

        # If encrypted, test decryption first
        test_file = file_path
        decrypted_temp = None
        if record.encrypted:
            try:
                fernet = Fernet(self._get_encryption_key())
                with open(file_path, "rb") as f:
                    decrypted_bytes = fernet.decrypt(f.read())
                decrypted_temp = file_path.parent / f"verify_{backup_id}.tmp"
                with open(decrypted_temp, "wb") as f_out:
                    f_out.write(decrypted_bytes)
                test_file = decrypted_temp
            except Exception as e:
                return {
                    "backup_id": backup_id,
                    "valid": False,
                    "format": record.format,
                    "details": f"AES-256 Decryption failed. Invalid encryption key: {e}",
                    "error": str(e),
                    "verified_at": datetime.now(timezone.utc).isoformat()
                }

        try:
            # Step 2: Validate archive structure
            if record.format == "custom":
                # pg_restore --list inspects the Table of Contents of a custom dump without modifying anything
                proc = await asyncio.create_subprocess_exec(
                    "pg_restore", "--list", str(test_file),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    return {
                        "backup_id": backup_id,
                        "valid": False,
                        "format": record.format,
                        "details": f"pg_restore verification failed: {stderr.decode()}",
                        "error": stderr.decode(),
                        "verified_at": datetime.now(timezone.utc).isoformat()
                    }
                toc_lines = len(stdout.decode().splitlines())
                details = f"Valid custom PostgreSQL archive. TOC entries verified: {toc_lines} catalog objects."
            else:
                # Plain format
                if record.compressed:
                    with gzip.open(test_file, "rb") as gz:
                        # read chunks to test gzip stream integrity
                        bytes_read = 0
                        while chunk := gz.read(65536):
                            bytes_read += len(chunk)
                    details = f"Valid gzipped SQL script. Decompressed payload size: {_human_size(bytes_read)}."
                else:
                    with open(test_file, "r", encoding="utf-8", errors="ignore") as f:
                        lines = sum(1 for _ in f)
                    details = f"Valid plain SQL script. Line count: {lines} lines."

            return {
                "backup_id": backup_id,
                "valid": True,
                "format": record.format,
                "details": details,
                "error": None,
                "verified_at": datetime.now(timezone.utc).isoformat()
            }
        finally:
            if decrypted_temp:
                decrypted_temp.unlink(missing_ok=True)

    def enforce_retention(self) -> int:
        """
        Deletes backups older than BACKUP_RETENTION_DAYS.
        Returns count of purged backups.
        """
        records = self._load_metadata()
        retention_days = settings.BACKUP_RETENTION_DAYS
        now = datetime.now(timezone.utc)
        survivors = []
        deleted_count = 0

        for r in records:
            try:
                created_dt = datetime.fromisoformat(r["created_at"])
                age = (now - created_dt).total_seconds() / 86400.0
                if age > retention_days:
                    # Retention may only remove local archives owned by this app.
                    root = settings.get_backup_path().resolve()
                    path = Path(r["file_path"]).resolve()
                    try:
                        path.relative_to(root)
                    except (ValueError, OSError):
                        logger.error("Retention skipped backup %s because its path is outside local storage.", r.get("backup_id"))
                        survivors.append(r)
                        continue
                    if path.exists() and path.is_file():
                        path.unlink()
                    deleted_count += 1
                    logger.info(f"Retention policy: deleted expired backup {r['backup_id']} (Age: {age:.1f} days)")
                else:
                    survivors.append(r)
            except Exception as e:
                logger.warning(f"Error checking retention for {r.get('backup_id')}: {e}")
                survivors.append(r)

        if deleted_count > 0:
            self._save_metadata(survivors)
        return deleted_count


backup_service = BackupService()

