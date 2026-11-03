from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class NotificationConfigResponse(BaseModel):
    enabled: bool
    telegram_configured: bool
    bot_token_masked: str
    chat_id: str
    telegram_recipients: list[str] = Field(default_factory=list)
    silent: bool
    events: Dict[str, bool]
    updated_at: Optional[str] = None
    message: str


class NotificationConfigRequest(BaseModel):
    enabled: Optional[bool] = Field(default=None, description="Master switch for Telegram notifications")
    bot_token: Optional[str] = Field(default=None, description="Telegram Bot API token")
    chat_id: Optional[str] = Field(default=None, description="Telegram chat / channel ID")
    silent: Optional[bool] = Field(default=None, description="Send messages without a notification sound")
    events: Optional[Dict[str, bool]] = Field(default=None, description="Per-event delivery toggles")


class NotificationTestRequest(BaseModel):
    message: str = Field(default="Test notification from DBOps Control Plane")


class NotificationTestResponse(BaseModel):
    success: bool
    status: str
    details: Dict[str, Any]
