from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field


class BackupCreateRequest(BaseModel):
    db_id: Optional[str] = Field(default=None, description="Registered database ID to back up")
    format: str = Field(default="custom", description="Backup format: 'custom' (.dump) or 'plain' (.sql.gz)")
    compress: bool = Field(default=True, description="Whether to compress the backup archive")
    encrypt: bool = Field(default=False, description="Whether to encrypt the backup with AES-256")
    description: Optional[str] = Field(default=None, description="Optional label or note for this backup")
    repository_ids: List[str] = Field(default_factory=list, description="Registered storage targets to receive verified copies")
    destination_server_ids: List[str] = Field(default_factory=list, description="Remote servers to receive a verified snapshot file copy")


class BackupMetadata(BaseModel):
    backup_id: str
    db_id: Optional[str] = None
    filename: str
    file_path: str
    format: str
    size_bytes: int
    size_human: str
    compressed: bool
    encrypted: bool
    status: str  # in_progress, completed, failed
    created_at: str
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    age_days: Optional[float] = None
    description: Optional[str] = None
    error: Optional[str] = None
    checksum_sha256: Optional[str] = None
    replicas: List[dict] = Field(default_factory=list)
    remote_copies: List[dict] = Field(default_factory=list)
    remote_copy_errors: List[dict] = Field(default_factory=list)


class BackupListResponse(BaseModel):
    total: int
    storage_directory: str
    retention_days: int
    backups: List[BackupMetadata]


class BackupScheduleRequest(BaseModel):
    cron: str = Field(default="0 2 * * *", description="Cron expression (e.g. '0 2 * * *' for daily at 2 AM)")
    enabled: bool = Field(default=True, description="Whether the schedule is active")
    format: str = Field(default="custom", description="Backup format: 'custom' or 'plain'")
    encrypt: bool = Field(default=False, description="Encrypt automated backup")
    db_id: Optional[str] = Field(default=None, description="Registered database to snapshot")
    repository_ids: List[str] = Field(default_factory=list, description="Mounted repositories to receive verified copies")
    destination_server_ids: List[str] = Field(default_factory=list, description="Remote servers to receive verified copies")


class BackupScheduleResponse(BaseModel):
    cron: str
    enabled: bool
    format: str
    encrypt: bool
    db_id: Optional[str] = None
    repository_ids: List[str] = Field(default_factory=list)
    destination_server_ids: List[str] = Field(default_factory=list)
    next_run_time: Optional[str] = None
    message: str


class BackupVerifyResponse(BaseModel):
    backup_id: str
    valid: bool
    format: str
    details: str
    error: Optional[str] = None
    verified_at: str

