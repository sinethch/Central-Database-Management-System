import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database to monitor & backup
    TARGET_DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/postgres"

    # Management API Settings
    MANAGEMENT_HOST: str = "0.0.0.0"
    MANAGEMENT_PORT: int = 8080
    API_KEY: str = "dbops-control-plane-api-key"
    ADMIN_USERNAME: str = "Admin"
    ADMIN_PASSWORD: str = "AdminBackup"
    APP_NAME: str = "DBOps Control Plane"
    APP_VERSION: str = "1.0.0"

    # Backup Settings
    BACKUP_SCHEDULE: str = "0 2 * * *"  # Cron format: min hour day month dow
    BACKUP_RETENTION_DAYS: int = 14
    BACKUP_STORAGE_DIR: str = os.getenv("BACKUP_STORAGE_DIR", "./backups")
    BACKUP_ENCRYPTION_KEY: Optional[str] = None  # 32-byte urlsafe base64 string or passphrase
    DATABASE_CREDENTIAL_ENCRYPTION_KEY: Optional[str] = None
    AUTH_TOTP_ENCRYPTION_KEY: Optional[str] = None  # Stable key used to encrypt authenticator seeds
    SERVER_CREDENTIAL_ENCRYPTION_KEY: Optional[str] = None
    SSH_KNOWN_HOSTS_PATH: str = "~/.ssh/known_hosts"

    # Health & Alerting Thresholds
    SLOW_QUERY_THRESHOLD_SECONDS: float = 5.0
    MAX_CONNECTION_WARNING_PERCENT: float = 80.0
    BUFFER_CACHE_HIT_WARNING_PERCENT: float = 95.0

    # Notification Channels
    ALERT_WEBHOOK_URL: Optional[str] = None
    ALERT_TELEGRAM_BOT_TOKEN: Optional[str] = None
    ALERT_TELEGRAM_CHAT_ID: Optional[str] = None
    ALERT_EMAIL_ENABLED: bool = False
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    ALERT_EMAIL_TO: Optional[str] = None
    ALERT_EMAIL_FROM: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def get_backup_path(self) -> Path:
        path = Path(self.BACKUP_STORAGE_DIR)
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()

