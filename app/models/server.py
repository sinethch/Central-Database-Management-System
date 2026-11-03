from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class ServerRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    host: str = Field(..., min_length=1, max_length=253)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=512)
    database_root: str = Field(default="/srv/dbops/databases", min_length=1, max_length=300)
    backup_root: str = Field(default="/srv/dbops/snapshots", min_length=1, max_length=300)


class ServerNameUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class ServerPathsUpdateRequest(BaseModel):
    database_root: str = Field(..., min_length=1, max_length=300)
    backup_root: str = Field(..., min_length=1, max_length=300)


class ServerRecord(BaseModel):
    id: str
    name: str
    host: str
    ssh_port: int
    username: str
    database_root: str
    backup_root: str
    status: str = "unknown"
    created_at: str


class ServerListResponse(BaseModel):
    total: int
    servers: List[ServerRecord]


class ServerHealthResponse(BaseModel):
    server_id: str
    status: str
    checked_at: str
    ssh_connected: bool
    docker_version: Optional[str] = None
    uptime_seconds: Optional[float] = None
    load_average: List[float] = Field(default_factory=list)
    memory_total_bytes: Optional[int] = None
    memory_available_bytes: Optional[int] = None
    disk_total_bytes: Optional[int] = None
    disk_used_bytes: Optional[int] = None
    disk_available_bytes: Optional[int] = None
    disk_used_percent: Optional[int] = None
    managed_containers_running: Optional[int] = None
    managed_containers_stopped: Optional[int] = None
    error: Optional[str] = None


class ContainerCreateRequest(BaseModel):
    server_id: str
    engine: Literal["postgres", "mongodb", "mysql", "redis"] = "postgres"
    name: str = Field(..., min_length=1, max_length=63, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    container_name: str = Field(..., min_length=1, max_length=63, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    database_name: str = Field(default="appdb", min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    database_user: str = Field(default="dbops", min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    database_password: str = Field(..., min_length=16, max_length=128)
    host_port: int = Field(default=5432, ge=1024, le=65535)
    image: Optional[str] = Field(default=None, max_length=160, pattern=r"^[A-Za-z0-9._/:@-]+$")


class ContainerCreateResponse(BaseModel):
    database_id: str
    server_id: str
    container_name: str
    database_name: str
    connection_url: str
    data_directory: str
    status: str
    message: Optional[str] = None
