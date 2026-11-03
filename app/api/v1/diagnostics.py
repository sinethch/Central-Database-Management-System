from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from app.api.deps import verify_api_key
from app.models.diagnostics import (
    LocksDiagnosticResponse,
    TableBloatResponse,
)
from app.services.diagnostics_service import diagnostics_service
from app.services.multi_db_manager import multi_db_manager

router = APIRouter(prefix="/diagnostics", tags=["Diagnostics & Troubleshooting"])


def _raise_diagnostic_error(exc: Exception):
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, NotImplementedError):
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.get("/locks", response_model=LocksDiagnosticResponse, dependencies=[Depends(verify_api_key)])
async def get_locks(db_id: Optional[str] = Query(None, description="Registered database ID")):
    """
    Shows if any transactions are waiting on locks held by another process.
    Lists blocked PID, blocking PID, wait duration, and SQL statements.
    """
    try:
        return await multi_db_manager.get_waiting_locks(db_id)
    except Exception as e:
        _raise_diagnostic_error(e)


@router.get("/table-bloat", response_model=TableBloatResponse, dependencies=[Depends(verify_api_key)])
async def get_table_bloat(db_id: Optional[str] = Query(None, description="Registered database ID")):
    """
    Estimates dead tuples in tables to determine if autovacuum is keeping up.
    Provides bloat ratios and vacuum recommendations.
    """
    try:
        return await multi_db_manager.get_table_bloat(db_id)
    except Exception as e:
        _raise_diagnostic_error(e)

