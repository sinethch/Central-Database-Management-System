from typing import Optional
from fastapi import APIRouter, Query, Depends
from app.models.metrics import HealthResponse
from app.services.multi_db_manager import multi_db_manager

router = APIRouter(prefix="", tags=["Health & Status"])


@router.get("/health", response_model=HealthResponse)
async def get_health(db_id: Optional[str] = Query(None, description="Optional registered database ID")):
    """
    Ping test, uptime, database version, and connectivity latency for any registered target database.
    """
    return await multi_db_manager.get_health(db_id)
