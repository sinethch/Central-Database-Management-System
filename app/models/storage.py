from typing import List, Optional
from pydantic import BaseModel, Field


class StorageTargetCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=120)
    path: str = Field(..., min_length=1, max_length=500, description="Mounted POSIX/NFS/SMB path visible to the control plane")
    primary: bool = False


class StorageTarget(BaseModel):
    id: str
    name: str
    path: str
    primary: bool = False
    available: bool = False


class StorageTargetList(BaseModel):
    total: int
    targets: List[StorageTarget]


class ReplicationResult(BaseModel):
    backup_id: str
    repository_id: str
    status: str
    checksum_sha256: Optional[str] = None
    error: Optional[str] = None
