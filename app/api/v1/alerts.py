from fastapi import APIRouter, Depends, HTTPException, status
from app.api.deps import verify_api_key
from app.config import settings
from app.models.alert import (
    AlertConfigRequest,
    AlertConfigResponse,
    AlertTriggerTestRequest,
    AlertTriggerTestResponse,
)
from app.services.alert_service import alert_service

router = APIRouter(prefix="/alerts", tags=["Alerting & Notifications"])


@router.get("/config", response_model=AlertConfigResponse, dependencies=[Depends(verify_api_key)])
async def get_alert_config():
    """
    View currently configured notification channels and warning thresholds.
    """
    return {
        "webhook_configured": bool(settings.ALERT_WEBHOOK_URL),
        "telegram_configured": bool(settings.ALERT_TELEGRAM_BOT_TOKEN and settings.ALERT_TELEGRAM_CHAT_ID),
        "email_configured": bool(settings.ALERT_EMAIL_ENABLED and settings.SMTP_HOST and settings.ALERT_EMAIL_TO),
        "max_connection_warning_percent": settings.MAX_CONNECTION_WARNING_PERCENT,
        "slow_query_threshold_seconds": settings.SLOW_QUERY_THRESHOLD_SECONDS,
        "buffer_cache_hit_warning_percent": settings.BUFFER_CACHE_HIT_WARNING_PERCENT,
        "message": "Current notification and threshold configuration."
    }


@router.post("/config", response_model=AlertConfigResponse, dependencies=[Depends(verify_api_key)])
async def update_alert_config(payload: AlertConfigRequest):
    """
    Configure Webhook, Telegram, or Email notifications and update threshold limits.
    """
    if payload.webhook_url is not None:
        settings.ALERT_WEBHOOK_URL = payload.webhook_url
    if payload.telegram_bot_token is not None:
        settings.ALERT_TELEGRAM_BOT_TOKEN = payload.telegram_bot_token
    if payload.telegram_chat_id is not None:
        settings.ALERT_TELEGRAM_CHAT_ID = payload.telegram_chat_id
    if payload.max_connection_warning_percent is not None:
        settings.MAX_CONNECTION_WARNING_PERCENT = payload.max_connection_warning_percent
    if payload.slow_query_threshold_seconds is not None:
        settings.SLOW_QUERY_THRESHOLD_SECONDS = payload.slow_query_threshold_seconds
    if payload.buffer_cache_hit_warning_percent is not None:
        settings.BUFFER_CACHE_HIT_WARNING_PERCENT = payload.buffer_cache_hit_warning_percent
    if payload.email_enabled is not None:
        settings.ALERT_EMAIL_ENABLED = payload.email_enabled
    if payload.smtp_host is not None:
        settings.SMTP_HOST = payload.smtp_host
    if payload.smtp_port is not None:
        settings.SMTP_PORT = payload.smtp_port
    if payload.smtp_user is not None:
        settings.SMTP_USER = payload.smtp_user
    if payload.smtp_password is not None:
        settings.SMTP_PASSWORD = payload.smtp_password
    if payload.email_to is not None:
        settings.ALERT_EMAIL_TO = payload.email_to
    if payload.email_from is not None:
        settings.ALERT_EMAIL_FROM = payload.email_from

    return {
        "webhook_configured": bool(settings.ALERT_WEBHOOK_URL),
        "telegram_configured": bool(settings.ALERT_TELEGRAM_BOT_TOKEN and settings.ALERT_TELEGRAM_CHAT_ID),
        "email_configured": bool(settings.ALERT_EMAIL_ENABLED and settings.SMTP_HOST and settings.ALERT_EMAIL_TO),
        "max_connection_warning_percent": settings.MAX_CONNECTION_WARNING_PERCENT,
        "slow_query_threshold_seconds": settings.SLOW_QUERY_THRESHOLD_SECONDS,
        "buffer_cache_hit_warning_percent": settings.BUFFER_CACHE_HIT_WARNING_PERCENT,
        "message": "Alert configuration updated successfully."
    }


@router.post("/test", response_model=AlertTriggerTestResponse, dependencies=[Depends(verify_api_key)])
async def trigger_test_alert(payload: AlertTriggerTestRequest):
    """
    Send an immediate test notification across all configured channels.
    """
    details = await alert_service.send_alert(
        title="DBOps Test Notification",
        message=payload.message,
        severity=payload.severity,
        details={"sender": "API Test Endpoint", "timestamp": "now"}
    )
    delivered = any(str(value).startswith("sent") for value in details.values())
    return {
        "success": delivered,
        "details": details
    }

