from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class DatabaseInstance(BaseModel):
    id: str = Field(description="Unique identifier for the database instance")
    name: str = Field(description="Human-friendly label e.g. 'Production Aurora', 'Cluster Mongo-01'")
    engine: str = Field(description="Database engine: 'postgres', 'mongodb', 'mysql', 'redis'")
    connection_url: str = Field(description="Connection string (masked in public responses)")
    read_only_verified: bool = Field(default=False, description="Whether an administrator independently verified that this account has no write privileges")
    status: str = Field(default="online", description="'online', 'degraded', 'offline'")
    latency_ms: Optional[float] = None
    version: Optional[str] = None
    last_error: Optional[str] = Field(default=None, description="Reason the last connectivity check failed, if any")
    last_checked_at: Optional[str] = None
    created_at: str
    server_id: Optional[str] = None
    container_id: Optional[str] = None


class DatabaseRegistrationRequest(BaseModel):
    name: str = Field(..., description="Descriptive name for the database instance")
    engine: str = Field(..., description="Engine type: 'postgres', 'mongodb', 'mysql', 'redis'")
    connection_url: str = Field(..., description="Connection string")
    verify_connectivity: bool = Field(default=True, description="Test connection immediately")


class DatabaseTestRequest(BaseModel):
    engine: str = Field(..., description="Engine type: 'postgres', 'mongodb', 'mysql', 'redis'")
    connection_url: str = Field(..., description="Connection string to test")


class DatabaseTestResponse(BaseModel):
    connected: bool
    latency_ms: Optional[float] = None
    version: Optional[str] = None
    error: Optional[str] = None


class DatabaseListResponse(BaseModel):
    total: int
    databases: List[DatabaseInstance]

