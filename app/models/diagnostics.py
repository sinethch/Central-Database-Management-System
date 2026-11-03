from typing import Optional, List
from pydantic import BaseModel


class LockInfo(BaseModel):
    blocked_pid: int
    blocked_user: Optional[str]
    blocked_statement: Optional[str]
    blocking_pid: int
    blocking_user: Optional[str]
    blocking_statement: Optional[str]
    waiting_duration_seconds: Optional[float]
    lock_type: Optional[str]
    mode: Optional[str]


class LocksDiagnosticResponse(BaseModel):
    total_waiting_locks: int
    has_conflicts: bool
    locks: List[LockInfo]


class TableBloatInfo(BaseModel):
    schema_name: str
    table_name: str
    live_tuples: int
    dead_tuples: int
    dead_ratio_percent: float
    last_vacuum: Optional[str]
    last_autovacuum: Optional[str]
    autovacuum_count: int
    recommendation: str


class TableBloatResponse(BaseModel):
    total_tables_checked: int
    tables_needing_vacuum: int
    tables: List[TableBloatInfo]

