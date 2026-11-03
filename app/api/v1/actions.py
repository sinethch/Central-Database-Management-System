from typing import Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status
from app.api.deps import verify_api_key
from app.services.multi_db_manager import multi_db_manager
from app.services.alert_service import alert_service

router = APIRouter(prefix="/actions", tags=["DevOps Database Actions"])


class KillQueryRequest(BaseModel):
    db_id: Optional[str] = Field(default=None, description="Registered database ID")
    pid: int = Field(..., description="Process ID (Postgres/MySQL) or Operation ID (MongoDB)")


class PingRequest(BaseModel):
    db_id: Optional[str] = Field(default=None, description="Registered database ID")


@router.post("/kill-query", dependencies=[Depends(verify_api_key)])
async def kill_query_action(payload: KillQueryRequest):
    """
    Safely terminates a rogue, hung, or long-running query or operation on the selected database.
    """
    try:
        res = await multi_db_manager.kill_query(payload.db_id, payload.pid)
        return res
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to kill query: {str(e)}")


@router.post("/ping", dependencies=[Depends(verify_api_key)])
async def ping_database_action(payload: PingRequest):
    """
    Executes a real-time non-invasive ping and latency measurement on the target database.
    """
    try:
        return await multi_db_manager.get_health(payload.db_id)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Ping failed: {str(e)}")

