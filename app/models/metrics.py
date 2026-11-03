from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    connected: bool
    latency_ms: Optional[float]
    version: Optional[str]
    uptime: Optional[str]
    started_at: Optional[str]
    error: Optional[str] = None
    timestamp: str


class ConnectionMetrics(BaseModel):
    total_connections: int
    max_connections: int
    connection_usage_percent: float
    warning_threshold_percent: float
    is_warning: bool
    states: Dict[str, int]
    by_database: Dict[str, int]
    by_user: Dict[str, int]


class TableSizeInfo(BaseModel):
    schema_name: str
    table_name: str
    total_bytes: int
    total_human: str
    table_bytes: int
    table_human: str
    index_bytes: int
    index_human: str


class DatabaseSizeMetrics(BaseModel):
    database_name: str
    total_bytes: int
    total_human: str
    tables: List[TableSizeInfo]


class CacheHitRatioMetrics(BaseModel):
    database_name: str
    buffer_cache_hit_ratio: float
    index_cache_hit_ratio: float
    blocks_read: int
    blocks_hit: int
    is_healthy: bool
    threshold_percent: float
    recommendation: str


class SlowQueryInfo(BaseModel):
    pid: int
    duration_seconds: float
    query: str
    state: str
    user: Optional[str]
    client_addr: Optional[str]
    query_start: Optional[str]


class SlowQueriesResponse(BaseModel):
    threshold_seconds: float
    count: int
    slow_queries: List[SlowQueryInfo]


class DeadlocksResponse(BaseModel):
    database_name: str
    deadlocks_count: int
    conflicts_count: int
    rollbacks_count: int
    commits_count: int
    rollback_ratio_percent: float

