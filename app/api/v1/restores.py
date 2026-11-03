from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query

from app.api.deps import verify_api_key
from app.auth import current_user, require_admin
from app.models.restore import RestoreRequest, RestoreJob, RestoreListResponse
from app.services.database_registry import database_registry
from app.services.restore_service import restore_service

router = APIRouter(prefix="/restores", tags=["Restore / Disaster Recovery"])


@router.get("", response_model=RestoreListResponse, dependencies=[Depends(verify_api_key)])
async def list_restores(limit: int = Query(30, ge=1, le=100)):
    """
    List historical restore jobs (target database, mode, status, duration).
    """
    jobs = restore_service.list_restores(limit=limit)
    return {"total": len(jobs), "restores": jobs}


@router.get("/{restoreId}", response_model=RestoreJob, dependencies=[Depends(verify_api_key)])
async def get_restore(restoreId: str):
    """
    Get the status and log of a specific restore job.
    """
    job = restore_service.get_restore(restoreId)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Restore '{restoreId}' not found.")
    return job


@router.post("/{restoreId}/register-target", response_model=RestoreJob, dependencies=[Depends(verify_api_key)])
async def register_restored_target(restoreId: str):
    """Register a completed clone restore in the monitored Targets list."""
    try:
        return await restore_service.register_restored_database(restoreId)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Could not add restored database to Targets: {str(e)}")


@router.delete("/{restoreId}/target", response_model=RestoreJob, dependencies=[Depends(verify_api_key)])
async def unregister_restored_target(restoreId: str):
    """Remove the restored database from this app's monitoring Targets only."""
    try:
        return restore_service.unregister_restored_database(restoreId)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Could not remove restored database from Targets: {str(e)}")


@router.get("/{restoreId}/connection-url")
async def get_restored_connection_url(restoreId: str, user=Depends(current_user)):
    """Return a restored database's credential-bearing URL to administrators only."""
    await require_admin(user)
    job = restore_service.get_restore(restoreId)
    if not job or job.status != "completed" or job.mode != "clone":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Completed clone restore not found.")
    if not job.restored_database_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Add this restored database to Targets first.")
    target = next((item for item in database_registry._load() if item.get("id") == job.restored_database_id), None)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restored database is no longer registered in Targets.")
    return {"connection_url": target.get("connection_url", ""), "database_name": job.target_database}


@router.post("", response_model=RestoreJob, dependencies=[Depends(verify_api_key)])
async def create_restore(payload: RestoreRequest):
    """
    Restore a snapshot into a new database or, with explicit confirmation, a selected existing database.
    """
    try:
        return await restore_service.create_restore(
            backup_id=payload.backup_id,
            db_id=payload.db_id,
            mode=payload.mode,
            new_db_name=payload.new_db_name,
            confirm=payload.confirm,
            source_repository=payload.source_repository,
            source_server_id=payload.source_server_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Restore failed: {str(e)}")
