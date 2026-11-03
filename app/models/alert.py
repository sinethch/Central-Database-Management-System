from typing import Optional
from pydantic import BaseModel, Field


class AlertConfigRequest(BaseModel):
    webhook_url: Optional[str] = Field(default=None, description="Generic Webhook or Slack/Discord incoming webhook URL")
    telegram_bot_token: Optional[str] = Field(default=None, description="Telegram Bot API Token")
    telegram_chat_id: Optional[str] = Field(default=None, description="Telegram Chat ID")
    max_connection_warning_percent: Optional[float] = Field(default=None, ge=0, le=100, description="Connection alert threshold")
    slow_query_threshold_seconds: Optional[float] = Field(default=None, gt=0, description="Slow query alert threshold")
    buffer_cache_hit_warning_percent: Optional[float] = Field(default=None, ge=0, le=100, description="Cache hit ratio warning threshold")
    email_enabled: Optional[bool] = Field(default=None, description="Enable email alerts")
    smtp_host: Optional[str] = Field(default=None, description="SMTP Server Host")
    smtp_port: Optional[int] = Field(default=587, description="SMTP Server Port")
    smtp_user: Optional[str] = Field(default=None, description="SMTP Username")
    smtp_password: Optional[str] = Field(default=None, description="SMTP Password")
    email_to: Optional[str] = Field(default=None, description="Destination Email Address")
    email_from: Optional[str] = Field(default=None, description="Sender Email Address")


class AlertConfigResponse(BaseModel):
    webhook_configured: bool
    telegram_configured: bool
    email_configured: bool
    max_connection_warning_percent: float
    slow_query_threshold_seconds: float
    buffer_cache_hit_warning_percent: float
    message: str


class AlertTriggerTestRequest(BaseModel):
    message: str = Field(default="Test notification from DBOps Control Plane")
    severity: str = Field(default="INFO", description="INFO, WARNING, or CRITICAL")


class AlertTriggerTestResponse(BaseModel):
    success: bool
    details: dict

