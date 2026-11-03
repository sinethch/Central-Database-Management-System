from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import verify_api_key
from app.models.notification import (
    NotificationConfigRequest,
    NotificationConfigResponse,
    NotificationTestRequest,
    NotificationTestResponse,
)
from app.services.notification_service import notification_service

router = APIRouter(prefix="/notifications", tags=["Telegram Notifications"])


@router.get("/config", response_model=NotificationConfigResponse, dependencies=[Depends(verify_api_key)])
async def get_notification_config():
    """
    View the persisted Telegram notification configuration (bot token masked).
    """
    cfg = notification_service.get_config()
    cfg["message"] = "Current Telegram notification configuration."
    return cfg


@router.post("/config", response_model=NotificationConfigResponse, dependencies=[Depends(verify_api_key)])
async def update_notification_config(payload: NotificationConfigRequest):
    """
    Persist Telegram bot token, chat ID, master switch and per-event delivery toggles.
    """
    cfg = notification_service.update_config(payload.model_dump(exclude_unset=True))
    cfg["message"] = "Telegram notification configuration saved."
    return cfg


@router.post("/test", response_model=NotificationTestResponse, dependencies=[Depends(verify_api_key)])
async def send_test_notification(payload: NotificationTestRequest):
    """
    Send an immediate test message through the configured Telegram bot.
    """
    return await notification_service.send_test(payload.message)
