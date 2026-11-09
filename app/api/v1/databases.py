from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from app.api.deps import verify_api_key
from app.auth import current_user, require_admin
from app.models.database_registry import (
    DatabaseInstance,
    DatabaseRegistrationRequest,
    DatabaseListResponse,
    DatabaseTestRequest,
    DatabaseTestResponse,
)
from app.services.database_registry import database_registry
from app.services.database_browser import database_browser

router = APIRouter(prefix="/databases", tags=["Centralized Database Registry"])


class DatabaseNameUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


@router.get("/{db_id}/browse/tables")
async def browse_database_tables(db_id: str, user=Depends(current_user)):
    """List table/collection names using read-only access to a registered target."""
    try:
        return await database_browser.list_tables(db_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except NotImplementedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Could not read database metadata: {exc}")


@router.get("/{db_id}/browse/rows")
async def browse_database_rows(
    db_id: str,
    table: str = Query(..., min_length=1, max_length=512),
    offset: int = Query(0, ge=0, le=10_000_000),
    limit: int = Query(100, ge=1, le=200),
    user=Depends(current_user),
):
    """Read a bounded page of rows/documents. No write statements are accepted."""
    try:
        return await database_browser.read_rows(db_id, table, offset, limit)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except NotImplementedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Could not read database rows: {exc}")


@router.get("", response_model=DatabaseListResponse, dependencies=[Depends(verify_api_key)])
async def list_registered_databases():
    """
    List all databases monitored by the centralized DevOps control plane.
    Supports PostgreSQL, MongoDB, MySQL, and Redis.
    """
    dbs = database_registry.list_databases()
    return {
        "total": len(dbs),
        "databases": dbs
    }


@router.post("", response_model=DatabaseInstance, dependencies=[Depends(verify_api_key)])
async def register_database(payload: DatabaseRegistrationRequest):
    """
    Register a database connection for monitoring. A connectivity test does not
    verify that the supplied account has read-only database permissions.
    """
    try:
        instance = await database_registry.add_database(
            name=payload.name,
            engine=payload.engine,
            connection_url=payload.connection_url,
            verify=payload.verify_connectivity
        )
        return instance
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Registration failed: {str(e)}")


@router.post("/test", response_model=DatabaseTestResponse, dependencies=[Depends(verify_api_key)])
async def test_database_connection(payload: DatabaseTestRequest):
    """
    Perform a short, read-only connectivity test against an arbitrary database URL
    without registering it. Used by the UI "Test Connection" button.
    """
    return await database_registry.verify_connection(payload.engine, payload.connection_url)


@router.patch("/{db_id}/name", response_model=DatabaseInstance)
async def rename_database(db_id: str, payload: DatabaseNameUpdate, user=Depends(current_user)):
    """Update only a registered target's display name."""
    await require_admin(user)
    try:
        return database_registry.rename_database(db_id, payload.name)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/{db_id}/connection-url")
async def get_database_connection_url(db_id: str, user=Depends(current_user)):
    """Return a registered database URL only to administrators, for explicit copying."""
    await require_admin(user)
    target = next((item for item in database_registry._load() if item.get("id") == db_id), None)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Database '{db_id}' not found.")
    return {"connection_url": target.get("connection_url", ""), "database_name": target.get("name", "Database")}


@router.delete("/{db_id}", dependencies=[Depends(verify_api_key)])
async def unregister_database(db_id: str):
    """
    Remove a database from active DevOps monitoring. Does not touch or modify the target database.
    """
    success = database_registry.remove_database(db_id)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Database '{db_id}' not found.")
    from app.services.restore_service import restore_service
    restore_service.clear_registered_database_reference(db_id)
    return {"message": f"Database '{db_id}' unregistered successfully."}
