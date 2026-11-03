from typing import Optional, List
from pydantic import BaseModel, Field


class RestoreRequest(BaseModel):
    backup_id: str = Field(..., description="Backup archive to restore")
    db_id: Optional[str] = Field(default=None, description="Registered database ID to restore into")
    mode: str = Field(default="clone", description="clone creates a new database; existing applies the snapshot to a selected registered database")
    new_db_name: Optional[str] = Field(default=None, description="Required only when mode is clone")
    source_repository: Optional[str] = Field(default=None, description="Registered repository ID; omitted selects a verified available copy")
    source_server_id: Optional[str] = Field(default=None, description="Registered remote server containing a verified snapshot copy")
    confirm: str = Field(..., description="Type RESTORE for clone or RESTORE EXISTING for existing-target restore")


class RestoreJob(BaseModel):
    restore_id: str
    backup_id: str
    db_id: Optional[str] = None
    engine: str
    mode: str
    target_database: str
    target_url_masked: str
    status: str  # running, completed, failed
    started_at: str
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    message: Optional[str] = None
    error: Optional[str] = None
    log: Optional[str] = None
    source_repository: Optional[str] = None
    restored_database_id: Optional[str] = None


class RestoreListResponse(BaseModel):
    total: int
    restores: List[RestoreJob]
