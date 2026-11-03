from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException, status
from app.api.deps import verify_api_key
from app.models.metrics import (
    ConnectionMetrics,
    DatabaseSizeMetrics,
    CacheHitRatioMetrics,
    SlowQueriesResponse,
    DeadlocksResponse,
)
from app.services.multi_db_manager import multi_db_manager
from app.services.migration_service import migration_history_service

router = APIRouter(prefix="/metrics", tags=["Health & Performance Metrics"])


def _raise_metric_error(exc: Exception):
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, NotImplementedError):
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.get("/migrations", dependencies=[Depends(verify_api_key)])
async def get_migration_history(db_id: Optional[str] = Query(None, description="Registered database ID")):
    """Read recognized migration ledger tables without running migration commands."""
    try:
        return await migration_history_service.get_history(db_id)
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not read migration history. Check connectivity and database read permissions.")


@router.get("/connections", response_model=ConnectionMetrics, dependencies=[Depends(verify_api_key)])
async def get_connections(db_id: Optional[str] = Query(None, description="Registered database ID")):
    """
    Active vs idle vs maximum allowed connections for the target database.
    """
    try:
        return await multi_db_manager.get_connections(db_id)
    except Exception as e:
        _raise_metric_error(e)


@router.get("/database-size", response_model=DatabaseSizeMetrics, dependencies=[Depends(verify_api_key)])
async def get_database_size(db_id: Optional[str] = Query(None, description="Registered database ID")):
    """
    Current disk/memory space consumed and individual table or collection sizes.
    """
    try:
        return await multi_db_manager.get_database_size(db_id)
    except Exception as e:
        _raise_metric_error(e)


@router.get("/cache-hit-ratio", response_model=CacheHitRatioMetrics, dependencies=[Depends(verify_api_key)])
async def get_cache_hit_ratio(db_id: Optional[str] = Query(None, description="Registered database ID")):
    """
    Buffer cache hit ratio (should ideally be > 95% for healthy DB memory).
    """
    try:
        return await multi_db_manager.get_cache_hit_ratio(db_id)
    except Exception as e:
        _raise_metric_error(e)


@router.get("/slow-queries", response_model=SlowQueriesResponse, dependencies=[Depends(verify_api_key)])
async def get_slow_queries(
    db_id: Optional[str] = Query(None, description="Registered database ID"),
    threshold_seconds: Optional[float] = Query(None, description="Query duration threshold in seconds")
):
    """
    Identifies queries or operations currently running longer than a threshold (e.g., > 5 seconds).
    """
    try:
        return await multi_db_manager.get_slow_queries(db_id, threshold_seconds)
    except Exception as e:
        _raise_metric_error(e)


@router.get("/deadlocks", response_model=DeadlocksResponse, dependencies=[Depends(verify_api_key)])
async def get_deadlocks(db_id: Optional[str] = Query(None, description="Registered database ID")):
    """
    Counts of conflict errors, deadlocks, and rollbacks.
    """
    try:
        return await multi_db_manager.get_deadlocks(db_id)
    except Exception as e:
        _raise_metric_error(e)
