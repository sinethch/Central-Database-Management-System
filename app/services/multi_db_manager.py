import time
import re
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from urllib.parse import urlparse

import asyncpg
import pymysql
import redis
import pymongo

from app.config import settings
from app.services.backup_service import _human_size

logger = logging.getLogger("dbops.multidb")


class MultiDatabaseManager:
    def __init__(self):
        self._pg_pools: Dict[str, asyncpg.Pool] = {}

    async def _with_postgres(self, url: str, operation, *args, **kwargs):
        """Run a read-only metrics operation against the selected PostgreSQL URL."""
        conn = await asyncpg.connect(
            dsn=url,
            timeout=6,
            server_settings={"default_transaction_read_only": "on", "statement_timeout": "15000"},
        )
        try:
            return await operation(*args, conn=conn, **kwargs)
        finally:
            await conn.close()

    def get_database_info(self, db_id: str) -> Optional[Dict[str, Any]]:
        from app.services.database_registry import database_registry
        dbs = database_registry._load()
        for d in dbs:
            if d["id"] == db_id:
                return d
        return None

    def get_default_db(self) -> Dict[str, Any]:
        from app.services.database_registry import database_registry
        dbs = database_registry._load()
        if dbs:
            return dbs[0]
        return {
            "id": "db-primary",
            "name": "Primary Database",
            "engine": "postgres",
            "connection_url": settings.TARGET_DATABASE_URL
        }

    def _require_target(self, db_id: Optional[str]) -> Dict[str, Any]:
        target = self.get_database_info(db_id) if db_id else self.get_default_db()
        if not target:
            raise LookupError("Database not found in registry")
        return target

    # =========================================================================
    # 1. Health & Ping
    # =========================================================================
    async def get_health(self, db_id: Optional[str] = None) -> Dict[str, Any]:
        target = self.get_database_info(db_id) if db_id else self.get_default_db()
        if not target:
            return {
                "status": "unhealthy",
                "connected": False,
                "latency_ms": None,
                "version": None,
                "uptime": None,
                "started_at": None,
                "error": "Database not found in registry",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "engine": "unknown"
            }

        engine = target.get("engine", "postgres").lower()
        url = target.get("connection_url", "")

        start = time.perf_counter()
        try:
            if engine in ["postgres", "postgresql"]:
                return await self._get_health_postgres(url)
            elif engine == "mongodb":
                return self._get_health_mongodb(url, start)
            elif engine in ["mysql", "mariadb"]:
                return self._get_health_mysql(url, start)
            elif engine == "redis":
                return self._get_health_redis(url, start)
            else:
                return {
                    "status": "healthy",
                    "connected": True,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                    "version": f"{engine.title()} (Custom Adapter)",
                    "uptime": "Active",
                    "started_at": None,
                    "error": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "engine": engine
                }
        except Exception as e:
            return {
                "status": "unhealthy",
                "connected": False,
                "latency_ms": None,
                "version": None,
                "uptime": None,
                "started_at": None,
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "engine": engine
            }

    async def _get_health_postgres(self, url: str) -> Dict[str, Any]:
        start = time.perf_counter()
        conn = await asyncpg.connect(dsn=url, timeout=6)
        try:
            version = await conn.fetchval("SELECT version();")
            uptime = await conn.fetchrow(
                "SELECT pg_postmaster_start_time() AS started_at, "
                "now() - pg_postmaster_start_time() AS uptime;"
            )
            return {
                "status": "healthy",
                "connected": True,
                "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                "version": version,
                "uptime": str(uptime["uptime"]) if uptime else None,
                "started_at": uptime["started_at"].isoformat() if uptime else None,
                "error": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "engine": "postgres"
            }
        finally:
            await conn.close()

    def _get_health_mongodb(self, url: str, start: float) -> Dict[str, Any]:
        client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
        try:
            status = client.admin.command('serverStatus')
            latency = round((time.perf_counter() - start) * 1000, 2)
            uptime_secs = status.get("uptime", 0)
            uptime_str = f"{int(uptime_secs // 86400)}d {int((uptime_secs % 86400) // 3600)}h {int((uptime_secs % 3600) // 60)}m"
            return {
                "status": "healthy",
                "connected": True,
                "latency_ms": latency,
                "version": f"MongoDB {status.get('version', '')}",
                "uptime": uptime_str,
                "started_at": None,
                "error": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "engine": "mongodb"
            }
        finally:
            client.close()

    def _get_health_mysql(self, url: str, start: float) -> Dict[str, Any]:
        parsed = urlparse(url)
        conn = pymysql.connect(
            host=parsed.hostname or 'localhost',
            port=parsed.port or 3306,
            user=parsed.username or 'root',
            password=parsed.password or '',
            database=parsed.path.lstrip('/') if parsed.path else None,
            connect_timeout=3
        )
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT VERSION(), @@uptime;")
                ver, uptime = cursor.fetchone()
                latency = round((time.perf_counter() - start) * 1000, 2)
                uptime_str = f"{int(uptime // 86400)}d {int((uptime % 86400) // 3600)}h"
                return {
                    "status": "healthy",
                    "connected": True,
                    "latency_ms": latency,
                    "version": f"MySQL {ver}",
                    "uptime": uptime_str,
                    "started_at": None,
                    "error": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "engine": "mysql"
                }
        finally:
            conn.close()

    def _get_health_redis(self, url: str, start: float) -> Dict[str, Any]:
        r = redis.from_url(url, socket_timeout=3)
        try:
            info = r.info()
            latency = round((time.perf_counter() - start) * 1000, 2)
            uptime_days = info.get("uptime_in_days", 0)
            return {
                "status": "healthy",
                "connected": True,
                "latency_ms": latency,
                "version": f"Redis {info.get('redis_version', '')}",
                "uptime": f"{uptime_days} days",
                "started_at": None,
                "error": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "engine": "redis"
            }
        finally:
            r.close()

    # =========================================================================
    # 2. Connections Metrics
    # =========================================================================
    async def get_connections(self, db_id: Optional[str] = None) -> Dict[str, Any]:
        target = self._require_target(db_id)
        engine = target.get("engine", "postgres").lower()
        url = target.get("connection_url", "")

        if engine in ["postgres", "postgresql"]:
            from app.services.metrics_service import metrics_service
            return await self._with_postgres(url, metrics_service.get_connections)

        elif engine == "mongodb":
            try:
                client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
                status = client.admin.command('serverStatus')
                conn_info = status.get("connections", {})
                current = conn_info.get("current", 1)
                available = conn_info.get("available", 100)
                total_max = current + available
                usage_pct = round((current / total_max) * 100.0, 2) if total_max else 0.0
                client.close()
                return {
                    "total_connections": current,
                    "max_connections": total_max,
                    "connection_usage_percent": usage_pct,
                    "warning_threshold_percent": settings.MAX_CONNECTION_WARNING_PERCENT,
                    "is_warning": usage_pct >= settings.MAX_CONNECTION_WARNING_PERCENT,
                    "states": {"active": conn_info.get("active", current), "available": available},
                    "by_database": {"admin": current},
                    "by_user": {"authenticated": current}
                }
            except Exception as e:
                logger.warning(f"Mongo connections error: {e}")
                raise RuntimeError("Could not read MongoDB connection metrics.") from e

        elif engine in ["mysql", "mariadb"]:
            try:
                parsed = urlparse(url)
                conn = pymysql.connect(
                    host=parsed.hostname or 'localhost',
                    port=parsed.port or 3306,
                    user=parsed.username or 'root',
                    password=parsed.password or '',
                    database=parsed.path.lstrip('/') if parsed.path else None,
                    connect_timeout=3
                )
                with conn.cursor() as cursor:
                    cursor.execute("SHOW STATUS LIKE 'Threads_connected';")
                    threads = int(cursor.fetchone()[1])
                    cursor.execute("SHOW VARIABLES LIKE 'max_connections';")
                    max_c = int(cursor.fetchone()[1])
                    usage_pct = round((threads / max_c) * 100.0, 2)
                    conn.close()
                    return {
                        "total_connections": threads,
                        "max_connections": max_c,
                        "connection_usage_percent": usage_pct,
                        "warning_threshold_percent": settings.MAX_CONNECTION_WARNING_PERCENT,
                        "is_warning": usage_pct >= settings.MAX_CONNECTION_WARNING_PERCENT,
                        "states": {"connected": threads},
                        "by_database": {parsed.path.lstrip('/') or 'mysql': threads},
                        "by_user": {parsed.username or 'root': threads}
                    }
            except Exception as e:
                logger.warning(f"MySQL connections error: {e}")
                raise RuntimeError("Could not read MySQL connection metrics.") from e

        elif engine == "redis":
            try:
                r = redis.from_url(url, socket_timeout=3)
                info = r.info()
                curr = info.get("connected_clients", 1)
                max_c = info.get("maxclients", 10000)
                usage_pct = round((curr / max_c) * 100.0, 2)
                r.close()
                return {
                    "total_connections": curr,
                    "max_connections": max_c,
                    "connection_usage_percent": usage_pct,
                    "warning_threshold_percent": settings.MAX_CONNECTION_WARNING_PERCENT,
                    "is_warning": False,
                    "states": {"connected_clients": curr},
                    "by_database": {"db0": curr},
                    "by_user": {"default": curr}
                }
            except Exception as e:
                logger.warning(f"Redis connections error: {e}")
                raise RuntimeError("Could not read Redis connection metrics.") from e

        raise NotImplementedError(f"Connection metrics are not supported for '{engine}'.")

    # =========================================================================
    # 3. Database Size & Top Relations / Collections
    # =========================================================================
    async def get_database_size(self, db_id: Optional[str] = None) -> Dict[str, Any]:
        target = self._require_target(db_id)
        engine = target.get("engine", "postgres").lower()
        url = target.get("connection_url", "")

        if engine in ["postgres", "postgresql"]:
            from app.services.metrics_service import metrics_service
            return await self._with_postgres(url, metrics_service.get_database_size)

        elif engine == "mongodb":
            try:
                client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
                parsed = urlparse(url)
                db_name = parsed.path.lstrip('/') or 'admin'
                mdb = client[db_name]
                stats = mdb.command("dbStats")
                total_bytes = stats.get("storageSize", 0)

                collections_data = []
                for coll_name in mdb.list_collection_names()[:25]:
                    cstats = mdb.command("collStats", coll_name)
                    c_size = cstats.get("storageSize", 0)
                    c_idx = cstats.get("totalIndexSize", 0)
                    collections_data.append({
                        "schema_name": db_name,
                        "table_name": coll_name,
                        "total_bytes": c_size + c_idx,
                        "total_human": _human_size(c_size + c_idx),
                        "table_bytes": c_size,
                        "table_human": _human_size(c_size),
                        "index_bytes": c_idx,
                        "index_human": _human_size(c_idx),
                    })
                client.close()
                return {
                    "database_name": db_name,
                    "total_bytes": total_bytes,
                    "total_human": _human_size(total_bytes),
                    "tables": collections_data
                }
            except Exception as e:
                logger.warning(f"MongoDB size error: {e}")
                raise RuntimeError("Could not read MongoDB database size.") from e

        elif engine in ["mysql", "mariadb"]:
            try:
                parsed = urlparse(url)
                db_name = parsed.path.lstrip('/') or 'mysql'
                conn = pymysql.connect(
                    host=parsed.hostname or 'localhost',
                    port=parsed.port or 3306,
                    user=parsed.username or 'root',
                    password=parsed.password or '',
                    database=db_name,
                    connect_timeout=3
                )
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COALESCE(SUM(data_length + index_length), 0) "
                        "FROM information_schema.tables WHERE table_schema = %s;",
                        (db_name,),
                    )
                    total_bytes = int(cursor.fetchone()[0] or 0)
                    query = """
                        SELECT table_schema, table_name,
                               (data_length + index_length) as total_b,
                               data_length as table_b,
                               index_length as idx_b
                        FROM information_schema.tables
                        WHERE table_schema = %s
                        ORDER BY total_b DESC LIMIT 25;
                    """
                    cursor.execute(query, (db_name,))
                    rows = cursor.fetchall()
                    tables = []
                    for r in rows:
                        tot = int(r[2] or 0)
                        dat = int(r[3] or 0)
                        idx = int(r[4] or 0)
                        tables.append({
                            "schema_name": r[0],
                            "table_name": r[1],
                            "total_bytes": tot,
                            "total_human": _human_size(tot),
                            "table_bytes": dat,
                            "table_human": _human_size(dat),
                            "index_bytes": idx,
                            "index_human": _human_size(idx),
                        })
                    conn.close()
                    return {
                        "database_name": db_name,
                        "total_bytes": total_bytes,
                        "total_human": _human_size(total_bytes),
                        "tables": tables
                    }
            except Exception as e:
                logger.warning(f"MySQL size error: {e}")
                raise RuntimeError("Could not read MySQL database size.") from e

        elif engine == "redis":
            try:
                r = redis.from_url(url, socket_timeout=3)
                info = r.info()
                used_bytes = info.get("used_memory", 0)
                max_memory_bytes = info.get("maxmemory", 0)
                keyspace = info.get("keyspace", {})
                db_key_counts = {}
                for db_name, db_stats in keyspace.items():
                    try:
                        db_key_counts[db_name] = int(db_stats.get("keys", 0))
                    except (AttributeError, TypeError, ValueError):
                        continue
                dbsize = db_key_counts.get("db0", 0)
                eviction_policy = info.get("maxmemory_policy", "unknown")
                r.close()
                return {
                    "database_name": "redis_db0",
                    "total_bytes": used_bytes,
                    "total_human": _human_size(used_bytes),
                    "memory_limit_bytes": max_memory_bytes,
                    "key_count": dbsize,
                    "total_key_count": sum(db_key_counts.values()),
                    "db_key_counts": db_key_counts,
                    "memory_fragmentation_ratio": info.get("mem_fragmentation_ratio"),
                    "evicted_keys": info.get("evicted_keys", 0),
                    "eviction_policy": eviction_policy,
                    "tables": [{
                        "schema_name": "keyspace",
                        "table_name": f"db0 ({dbsize} keys)",
                        "total_bytes": used_bytes,
                        "total_human": _human_size(used_bytes),
                        "table_bytes": used_bytes,
                        "table_human": _human_size(used_bytes),
                        "index_bytes": 0,
                        "index_human": "0 B"
                    }]
                }
            except Exception as e:
                logger.warning(f"Redis size error: {e}")
                raise RuntimeError("Could not read Redis memory metrics.") from e

        raise NotImplementedError(f"Database size metrics are not supported for '{engine}'.")

    # =========================================================================
    # 4. Cache Hit Ratio
    # =========================================================================
    async def get_cache_hit_ratio(self, db_id: Optional[str] = None) -> Dict[str, Any]:
        target = self._require_target(db_id)
        engine = target.get("engine", "postgres").lower()
        url = target.get("connection_url", "")

        if engine in ["postgres", "postgresql"]:
            from app.services.metrics_service import metrics_service
            return await self._with_postgres(url, metrics_service.get_cache_hit_ratio)

        raise NotImplementedError(f"Buffer cache hit metrics are only available for PostgreSQL targets (selected: '{engine}').")

    # =========================================================================
    # 5. Slow Queries & Deadlocks
    # =========================================================================
    async def get_slow_queries(self, db_id: Optional[str] = None, threshold_seconds: Optional[float] = None) -> Dict[str, Any]:
        target = self._require_target(db_id)
        engine = target.get("engine", "postgres").lower()
        url = target.get("connection_url", "")

        if engine in ["postgres", "postgresql"]:
            from app.services.metrics_service import metrics_service
            return await self._with_postgres(url, metrics_service.get_slow_queries, threshold_seconds=threshold_seconds)

        elif engine == "mongodb":
            try:
                client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
                ops = client.admin.command("currentOp", {"secs_running": {"$gt": int(threshold_seconds or 5)}})
                slow = []
                for op in ops.get("inprog", []):
                    slow.append({
                        "pid": op.get("opid", 0),
                        "duration_seconds": float(op.get("secs_running", 0)),
                        "query": str(op.get("command", {})),
                        "state": op.get("active", True) and "active" or "idle",
                        "user": op.get("effectiveUsers", [{}])[0].get("user", "admin"),
                        "client_addr": op.get("client", "local"),
                        "query_start": None
                    })
                client.close()
                return {
                    "threshold_seconds": threshold_seconds or 5.0,
                    "count": len(slow),
                    "slow_queries": slow
                }
            except Exception as e:
                logger.warning(f"MongoDB slow queries error: {e}")
                raise RuntimeError("Could not read MongoDB current operations.") from e

        raise NotImplementedError(f"Slow query inspection is not supported for '{engine}'.")

    async def get_deadlocks(self, db_id: Optional[str] = None) -> Dict[str, Any]:
        target = self._require_target(db_id)
        engine = target.get("engine", "postgres").lower()
        url = target.get("connection_url", "")

        if engine in ["postgres", "postgresql"]:
            from app.services.metrics_service import metrics_service
            return await self._with_postgres(url, metrics_service.get_deadlocks)

        raise NotImplementedError(f"Deadlock statistics are not supported for '{engine}'.")

    async def get_waiting_locks(self, db_id: Optional[str] = None) -> Dict[str, Any]:
        target = self._require_target(db_id)
        engine = (target.get("engine") or "postgres").lower()
        if engine not in {"postgres", "postgresql"}:
            raise NotImplementedError(f"Lock diagnostics are not supported for '{engine}'.")
        from app.services.diagnostics_service import diagnostics_service
        return await self._with_postgres(target.get("connection_url", ""), diagnostics_service.get_waiting_locks)

    async def get_table_bloat(self, db_id: Optional[str] = None) -> Dict[str, Any]:
        target = self._require_target(db_id)
        engine = (target.get("engine") or "postgres").lower()
        if engine not in {"postgres", "postgresql"}:
            raise NotImplementedError(f"Table bloat diagnostics are not supported for '{engine}'.")
        from app.services.diagnostics_service import diagnostics_service
        return await self._with_postgres(target.get("connection_url", ""), diagnostics_service.get_table_bloat)

    # =========================================================================
    # 6. Actions: Kill Query / Operation
    # =========================================================================
    async def kill_query(self, db_id: Optional[str], pid: int) -> Dict[str, Any]:
        target = self._require_target(db_id)
        engine = target.get("engine", "postgres").lower()
        url = target.get("connection_url", "")

        if engine in ["postgres", "postgresql"]:
            async def terminate(conn):
                return await conn.fetchval("SELECT pg_terminate_backend($1);", pid)
            success = await self._with_postgres(url, terminate)
            return {
                "success": bool(success),
                "pid": pid,
                "message": f"Terminated PostgreSQL process PID {pid}." if success else f"Could not terminate PID {pid} (may have already finished)."
            }
        elif engine == "mongodb":
            try:
                client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
                res = client.admin.command("killOp", op=pid)
                client.close()
                return {
                    "success": True,
                    "pid": pid,
                    "message": f"Killed MongoDB Operation ID {pid}: {res}"
                }
            except Exception as e:
                return {"success": False, "pid": pid, "message": f"Failed to kill op {pid}: {str(e)}"}

        elif engine in ["mysql", "mariadb"]:
            try:
                parsed = urlparse(url)
                conn = pymysql.connect(
                    host=parsed.hostname or 'localhost',
                    port=parsed.port or 3306,
                    user=parsed.username or 'root',
                    password=parsed.password or '',
                    database=parsed.path.lstrip('/') if parsed.path else None,
                    connect_timeout=3
                )
                with conn.cursor() as cursor:
                    cursor.execute(f"KILL {pid};")
                    conn.close()
                    return {"success": True, "pid": pid, "message": f"Killed MySQL thread ID {pid}."}
            except Exception as e:
                return {"success": False, "pid": pid, "message": f"Failed to kill thread {pid}: {str(e)}"}

        elif engine == "redis":
            try:
                r = redis.from_url(url, socket_timeout=3)
                r.client_kill_filter(_id=pid)
                r.close()
                return {"success": True, "pid": pid, "message": f"Terminated Redis client ID {pid}."}
            except Exception as e:
                return {"success": False, "pid": pid, "message": f"Failed to kill client {pid}: {str(e)}"}

        return {"success": False, "pid": pid, "message": f"Unsupported engine '{engine}' for kill operation"}


multi_db_manager = MultiDatabaseManager()

