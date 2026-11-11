import logging
import json
import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.config import settings
from app.services.backup_service import backup_service
from app.realtime import realtime_hub
from app.services.metrics_service import metrics_service
from app.services.storage_service import storage_service
from app.services.server_service import server_service
from app.services.migration_service import migration_history_service
from app.services.database_registry import database_registry
from app.services.notification_service import notification_service

logger = logging.getLogger("dbops.scheduler")


class SchedulerService:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.job_id = "automated_backup_job"
        self.retention_job_id = "retention_pruning_job"
        self.current_cron = settings.BACKUP_SCHEDULE
        self.is_enabled = bool(settings.BACKUP_SCHEDULE)
        self.current_format = "custom"
        self.current_encrypt = False
        self.current_db_id: Optional[str] = None
        self.current_repository_ids = []
        self.current_destination_server_ids = []
        self.settings_file = Path(settings.BACKUP_STORAGE_DIR) / "backup_schedule.json"
        self.migration_state_file = Path(settings.BACKUP_STORAGE_DIR) / "migration_notification_state.json"
        self._load_settings()

    def _load_migration_state(self) -> Dict[str, list]:
        try:
            data = json.loads(self.migration_state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_migration_state(self, data: Dict[str, list]) -> None:
        settings.get_backup_path()
        temp = self.migration_state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(self.migration_state_file)

    @staticmethod
    def _migration_signature(item: Dict[str, Any]) -> str:
        stable_fields = {
            key: item.get(key)
            for key in ("framework", "version", "name", "applied_at", "status")
        }
        return hashlib.sha256(
            json.dumps(stable_fields, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    async def _check_migration_changes(self):
        """Poll recognized migration ledgers with read-only SELECTs and alert on new rows."""
        state = self._load_migration_state()
        state_changed = False
        try:
            targets = database_registry._load()
        except Exception:
            logger.exception("Could not load registered targets for migration monitoring.")
            return

        for target in targets:
            if str(target.get("engine", "")).lower() not in {"postgres", "postgresql"}:
                continue
            target_id = str(target.get("id") or "")
            if not target_id:
                continue
            try:
                history = await migration_history_service.get_history(target_id)
                if not history.get("supported"):
                    continue
                entries = history.get("migrations") or []
                current = {self._migration_signature(item) for item in entries}
                if target_id not in state:
                    # Newly registered targets have no history yet. Treat their
                    # first successful scan as discovery so existing migration
                    # records are not silently missed. Persist even an empty
                    # baseline to distinguish a target with no migrations from
                    # one that has never been scanned successfully.
                    new_entries_by_signature = {
                        self._migration_signature(item): item for item in entries
                    }
                    new_entries = list(new_entries_by_signature.values())
                    target_name = history.get("database_name") or target.get("name") or target_id
                    if new_entries:
                        migration_lines = [
                            f"• `{item.get('framework') or 'Unknown'}` · "
                            f"`{item.get('version') or 'Not recorded'}` · "
                            f"{item.get('name') or 'Not recorded'} · "
                            f"Status: {item.get('status') or 'Unknown'}"
                            for item in new_entries
                        ]
                        await notification_service.notify(
                            "migration_detected",
                            "Database migrations detected",
                            f"{len(new_entries)} migration record(s) were found on {target_name} during initial monitoring.",
                            {"Target": target_name, "Migrations": "\n".join(migration_lines)},
                            "WARNING",
                        )
                        logger.info("Discovered %d migration record(s) for newly monitored target %s.", len(new_entries), target_id)
                    state[target_id] = sorted(current)[-5000:]
                    state_changed = True
                    continue

                known = set(state.get(target_id) or [])
                # Deduplicate rows within the current scan, then send one message
                # per target containing every newly discovered migration.
                new_entries_by_signature = {}
                for item in entries:
                    signature = self._migration_signature(item)
                    if signature not in known:
                        new_entries_by_signature.setdefault(signature, item)
                new_entries = list(new_entries_by_signature.values())
                if new_entries:
                    target_name = history.get("database_name") or target.get("name") or target_id
                    migration_lines = [
                        f"• `{item.get('framework') or 'Unknown'}` · "
                        f"`{item.get('version') or 'Not recorded'}` · "
                        f"{item.get('name') or 'Not recorded'} · "
                        f"Status: {item.get('status') or 'Unknown'}"
                        for item in new_entries
                    ]
                    # Persist fingerprints before sending so delivery failures or
                    # the next polling run cannot emit these records repeatedly.
                    state[target_id] = sorted(known | current)[-5000:]
                    self._save_migration_state(state)
                    state_changed = False
                    await notification_service.notify(
                        "migration_detected",
                        "Database migrations detected",
                        f"{len(new_entries)} new migration record(s) were found on {target_name}.",
                        {"Target": target_name, "Migrations": "\n".join(migration_lines)},
                        "WARNING",
                    )
                if new_entries:
                    logger.info("Detected %d new migration record(s) for target %s.", len(new_entries), target_id)
                combined = known | current
                # Bound local state if a ledger contains a very long history.
                state[target_id] = sorted(combined)[-5000:]
                state_changed = True
            except Exception:
                # Failed or inaccessible reads do not update that target's baseline.
                logger.warning("Migration polling failed for target %s; it will retry next interval.", target_id, exc_info=True)

        if state_changed:
            try:
                self._save_migration_state(state)
            except OSError:
                logger.exception("Could not save migration notification baseline.")

    def _load_settings(self) -> None:
        try:
            saved = json.loads(self.settings_file.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.current_cron = saved.get("cron", self.current_cron)
                self.is_enabled = bool(saved.get("enabled", self.is_enabled))
                self.current_format = saved.get("format", self.current_format)
                self.current_encrypt = bool(saved.get("encrypt", self.current_encrypt))
                self.current_db_id = saved.get("db_id")
                self.current_repository_ids = list(saved.get("repository_ids", []))
                self.current_destination_server_ids = list(saved.get("destination_server_ids", []))
        except (OSError, ValueError, TypeError):
            pass

    def _save_settings(self) -> None:
        settings.get_backup_path()
        data = {
            "cron": self.current_cron,
            "enabled": self.is_enabled,
            "format": self.current_format,
            "encrypt": self.current_encrypt,
            "db_id": self.current_db_id,
            "repository_ids": self.current_repository_ids,
            "destination_server_ids": self.current_destination_server_ids,
        }
        temp = self.settings_file.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(self.settings_file)

    async def _scheduled_backup_task(self):
        logger.info("Executing scheduled automated database backup...")
        db_id = self.current_db_id
        format_type = self.current_format
        encrypt = self.current_encrypt
        repository_ids = list(self.current_repository_ids)
        destination_server_ids = list(self.current_destination_server_ids)
        try:
            record = await backup_service.create_backup(
                db_id=db_id,
                format_type=format_type,
                compress=True,
                encrypt=encrypt,
                description="Automated scheduled backup"
            )
            if record:
                for repository_id in repository_ids:
                    try:
                        await asyncio.to_thread(storage_service.replicate, record.backup_id, repository_id)
                    except Exception as exc:
                        logger.error("Scheduled snapshot %s repository copy to %s failed: %s", record.backup_id, repository_id, exc)
                for server_id in destination_server_ids:
                    try:
                        await asyncio.to_thread(server_service.copy_snapshot, server_id, record.backup_id)
                    except Exception as exc:
                        logger.error("Scheduled snapshot %s remote copy to %s failed: %s", record.backup_id, server_id, exc)
                await realtime_hub.publish({"type": "update", "topic": "backups"})
        except Exception as e:
            logger.error(f"Scheduled backup failed: {e}")

    async def _retention_task(self):
        logger.info("Running routine backup retention pruning...")
        try:
            backup_service.enforce_retention()
            await realtime_hub.publish({"type": "update", "topic": "backups"})
        except Exception as e:
            logger.error(f"Routine retention task error: {e}")

    async def _routine_health_monitoring(self):
        """Periodically checks health to trigger threshold alerts if needed."""
        try:
            await metrics_service.get_connections()
        except Exception:
            pass

    def start(self):
        # 1. Automated Backup Job
        if self.is_enabled and self.current_cron:
            self._schedule_cron_job(self.current_cron)

        # 2. Hourly Retention Job
        self.scheduler.add_job(
            self._retention_task,
            "interval",
            hours=6,
            id=self.retention_job_id,
            replace_existing=True
        )

        # 3. Connection check every 2 minutes
        self.scheduler.add_job(
            self._routine_health_monitoring,
            "interval",
            minutes=2,
            id="routine_connection_check",
            replace_existing=True
        )

        # Read migration ledgers every two minutes. The migration-history service
        # enforces PostgreSQL read-only transactions and only executes SELECTs.
        self.scheduler.add_job(
            self._check_migration_changes,
            "interval",
            minutes=2,
            id="routine_migration_monitoring",
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
        )

        self.scheduler.start()
        logger.info("DBOps Background Scheduler started.")

    def _schedule_cron_job(self, cron_expr: str):
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: '{cron_expr}'. Expected 5 space-separated fields (min hour day month dow).")

        minute, hour, day, month, day_of_week = parts
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone="UTC",
        )
        self.scheduler.add_job(
            self._scheduled_backup_task,
            trigger=trigger,
            id=self.job_id,
            replace_existing=True
        )
        logger.info(f"Scheduled backup job registered with cron: '{cron_expr}'")

    def update_schedule(self, cron: str, enabled: bool, format_type: str = "custom", encrypt: bool = False,
                        db_id: Optional[str] = None, repository_ids=None, destination_server_ids=None) -> Dict[str, Any]:
        if not enabled:
            if self.scheduler.get_job(self.job_id):
                self.scheduler.remove_job(self.job_id)
            self.current_format = format_type
            self.current_encrypt = encrypt
            self.current_db_id = db_id
            self.current_repository_ids = list(dict.fromkeys(repository_ids or []))
            self.current_destination_server_ids = list(dict.fromkeys(destination_server_ids or []))
            self.current_cron = cron
            self.is_enabled = False
            self._save_settings()
            return {
                "cron": cron,
                "enabled": False,
                "format": format_type,
                "encrypt": encrypt,
                "db_id": self.current_db_id,
                "repository_ids": self.current_repository_ids,
                "destination_server_ids": self.current_destination_server_ids,
                "next_run_time": None,
                "message": "Automated backup schedule disabled."
            }

        self._schedule_cron_job(cron)
        self.current_format = format_type
        self.current_encrypt = encrypt
        self.current_db_id = db_id
        self.current_repository_ids = list(dict.fromkeys(repository_ids or []))
        self.current_destination_server_ids = list(dict.fromkeys(destination_server_ids or []))
        self.current_cron = cron
        self.is_enabled = True
        self._save_settings()

        job = self.scheduler.get_job(self.job_id)
        next_run_val = getattr(job, "next_run_time", None) if job else None
        if not next_run_val and job and getattr(job, "trigger", None):
            from datetime import datetime, timezone
            try:
                next_run_val = job.trigger.get_next_fire_time(None, datetime.now(timezone.utc))
            except Exception:
                next_run_val = None
        next_run = next_run_val.isoformat() if next_run_val else None

        return {
            "cron": cron,
            "enabled": True,
            "format": format_type,
            "encrypt": encrypt,
            "db_id": self.current_db_id,
            "repository_ids": self.current_repository_ids,
            "destination_server_ids": self.current_destination_server_ids,
            "next_run_time": next_run,
            "message": f"Backup schedule updated successfully. Next execution: {next_run}"
        }

    def get_schedule_status(self) -> Dict[str, Any]:
        job = self.scheduler.get_job(self.job_id)
        next_run_val = getattr(job, "next_run_time", None) if job else None
        if not next_run_val and job and getattr(job, "trigger", None):
            from datetime import datetime, timezone
            try:
                next_run_val = job.trigger.get_next_fire_time(None, datetime.now(timezone.utc))
            except Exception:
                next_run_val = None
        next_run = next_run_val.isoformat() if next_run_val else None

        return {
            "cron": self.current_cron,
            "enabled": self.is_enabled and job is not None,
            "format": self.current_format,
            "encrypt": self.current_encrypt,
            "db_id": self.current_db_id,
            "repository_ids": self.current_repository_ids,
            "destination_server_ids": self.current_destination_server_ids,
            "next_run_time": next_run,
            "message": "Current automated backup schedule"
        }

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("DBOps Background Scheduler stopped.")


scheduler_service = SchedulerService()
