import os
import asyncio
from typing import Optional
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import FileResponse
from app.api.deps import verify_api_key
from app.config import settings
from app.models.backup import (
    BackupCreateRequest,
    BackupMetadata,
    BackupListResponse,
    BackupScheduleRequest,
    BackupScheduleResponse,
    BackupVerifyResponse,
)
from app.services.backup_service import backup_service
from app.services.scheduler_service import scheduler_service
from app.services.storage_service import storage_service
from app.services.server_service import server_service
from app.services.restore_service import restore_service
from app.services.database_registry import database_registry

router = APIRouter(prefix="/backups", tags=["Backup Management"])


@router.get("", response_model=BackupListResponse, dependencies=[Depends(verify_api_key)])
async def list_backups(db_id: Optional[str] = Query(None, description="Filter backups by registered database ID")):
    """
    List all historical backups (timestamp, file size, status, retention age).
    """
    backups = backup_service.list_backups(db_id=db_id)
    return {
        "total": len(backups),
        "storage_directory": settings.BACKUP_STORAGE_DIR,
        "retention_days": settings.BACKUP_RETENTION_DAYS,
        "backups": backups
    }


@router.post("/create", response_model=BackupMetadata, dependencies=[Depends(verify_api_key)])
async def create_backup(payload: BackupCreateRequest):
    """
    Trigger an immediate manual non-blocking snapshot backup.
    Accepts format ('custom' or 'plain'), compression, AES-256 encryption, and target db_id.
    """
    try:
        record = await backup_service.create_backup(
            db_id=payload.db_id,
            format_type=payload.format,
            compress=payload.compress,
            encrypt=payload.encrypt,
            description=payload.description
        )
        for repository_id in payload.repository_ids:
            storage_service.replicate(record.backup_id, repository_id)
        remote_errors = []
        for server_id in payload.destination_server_ids:
            try:
                await asyncio.to_thread(server_service.copy_snapshot, server_id, record.backup_id)
            except Exception as exc:
                remote_errors.append({"server_id": server_id, "error": str(exc)})
        if payload.repository_ids or payload.destination_server_ids:
            record = backup_service.get_backup(record.backup_id) or record
        result = record.model_dump()
        result["remote_copy_errors"] = remote_errors
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Backup failed: {str(e)}")


@router.get("/schedule", response_model=BackupScheduleResponse, dependencies=[Depends(verify_api_key)])
async def get_schedule():
    """
    Get the current automated backup schedule and next scheduled execution.
    """
    return scheduler_service.get_schedule_status()


@router.post("/schedule", response_model=BackupScheduleResponse, dependencies=[Depends(verify_api_key)])
async def configure_schedule(payload: BackupScheduleRequest):
    """
    Configure or update automated backup schedules (e.g., cron: '0 2 * * *' for daily at 2 AM).
    """
    try:
        databases = database_registry.list_databases()
        if payload.db_id and not any(db.id == payload.db_id for db in databases):
            raise ValueError("Selected database is no longer registered.")
        selected_db = next((db for db in databases if db.id == payload.db_id), None) if payload.db_id else (databases[0] if databases else None)
        if selected_db and selected_db.engine not in {"postgres", "postgresql"}:
            raise ValueError("Scheduled backups currently support PostgreSQL targets only.")
        if payload.format not in {"custom", "plain"}:
            raise ValueError("Backup format must be 'custom' or 'plain'.")
        for repository_id in payload.repository_ids:
            repository = storage_service.get(repository_id)
            if not repository or not Path(repository.get("path", "")).is_dir():
                raise ValueError(f"Selected snapshot repository '{repository_id}' is unavailable.")
        for server_id in payload.destination_server_ids:
            if not server_service.get(server_id):
                raise ValueError(f"Selected remote server '{server_id}' is no longer registered.")
        res = scheduler_service.update_schedule(
            cron=payload.cron,
            enabled=payload.enabled,
            format_type=payload.format,
            encrypt=payload.encrypt,
            db_id=payload.db_id,
            repository_ids=payload.repository_ids,
            destination_server_ids=payload.destination_server_ids,
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/{backupId}", dependencies=[Depends(verify_api_key)])
async def delete_backup(backupId: str):
    """Delete only the local archive and its app registry entry.

    Copies in mounted repositories and on remote servers are intentionally preserved.
    """
    if restore_service.lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A restore is currently running. Wait for it to finish before deleting snapshots.",
        )
    try:
        deleted = await backup_service.delete_backup(backupId)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Backup '{backupId}' not found.")
    return {
        "deleted": True,
        "backup_id": backupId,
        "message": "Local snapshot archive and app entry deleted. Repository and remote-server copies were left untouched.",
    }


@router.get("/{backupId}", response_model=BackupMetadata, dependencies=[Depends(verify_api_key)])
async def get_backup_metadata(backupId: str):
    """
    Get metadata for a specific backup.
    """
    record = backup_service.get_backup(backupId)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Backup '{backupId}' not found.")
    return record


@router.get("/{backupId}/download", dependencies=[Depends(verify_api_key)])
async def download_backup(backupId: str):
    """
    Securely download the backup archive (.dump, .sql.gz, or .enc).
    """
    record = backup_service.get_backup(backupId)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Backup '{backupId}' not found.")

    file_path = Path(record.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup file is missing on server storage.")

    media_type = "application/octet-stream"
    if record.filename.endswith(".sql.gz"):
        media_type = "application/gzip"
    elif record.filename.endswith(".sql"):
        media_type = "text/plain"

    return FileResponse(
        path=str(file_path),
        filename=record.filename,
        media_type=media_type
    )


@router.post("/verify/{backupId}", response_model=BackupVerifyResponse, dependencies=[Depends(verify_api_key)])
async def verify_backup(backupId: str):
    """
    Validates backup archive integrity without restoring to production.
    Checks SHA256, Table-of-Contents via pg_restore or gzip stream validity.
    """
    try:
        res = await backup_service.verify_backup(backupId)
        return res
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

