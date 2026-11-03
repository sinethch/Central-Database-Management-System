import logging
from typing import Dict, Any, List
from datetime import datetime, timezone
from app.config import settings
from app.database import db
from app.services.backup_service import _human_size
from app.services.alert_service import alert_service

logger = logging.getLogger("dbops.metrics")


class MetricsService:
    @staticmethod
    async def get_health() -> Dict[str, Any]:
        result = await db.check_connectivity()
        status = "healthy" if result["connected"] else "unhealthy"
        return {
            "status": status,
            "connected": result["connected"],
            "latency_ms": result["latency_ms"],
            "version": result["version"],
            "uptime": result["uptime"],
            "started_at": result["started_at"],
            "error": result["error"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    @staticmethod
    async def get_connections(conn=None) -> Dict[str, Any]:
        """
        Calculates active vs idle vs max connections.
        Checks against MAX_CONNECTION_WARNING_PERCENT.
        """
        # Fetch max_connections setting
        query_db = conn or db
        max_conn_row = await query_db.fetchrow("SHOW max_connections;")
        max_conn = int(max_conn_row["max_connections"]) if max_conn_row else 100

        # Fetch connection breakdown by state
        states_records = await query_db.fetch("""
            SELECT COALESCE(state, 'unknown') as state, COUNT(*)::int as count
            FROM pg_stat_activity
            WHERE pid <> pg_backend_pid()
            GROUP BY state;
        """)
        states_dict = {r["state"]: r["count"] for r in states_records}
        total_connections = sum(states_dict.values())

        # Breakdown by DB
        db_records = await query_db.fetch("""
            SELECT datname, COUNT(*)::int as count
            FROM pg_stat_activity
            WHERE pid <> pg_backend_pid() AND datname IS NOT NULL
            GROUP BY datname;
        """)
        by_database = {r["datname"]: r["count"] for r in db_records}

        # Breakdown by User
        user_records = await query_db.fetch("""
            SELECT usename, COUNT(*)::int as count
            FROM pg_stat_activity
            WHERE pid <> pg_backend_pid() AND usename IS NOT NULL
            GROUP BY usename;
        """)
        by_user = {r["usename"]: r["count"] for r in user_records}

        usage_pct = round((total_connections / max_conn) * 100.0, 2)
        warning_threshold = settings.MAX_CONNECTION_WARNING_PERCENT
        is_warning = usage_pct >= warning_threshold

        if is_warning:
            # Trigger alert
            await alert_service.send_alert(
                title="High Database Connection Usage",
                message=(
                    f"PostgreSQL connections reached {usage_pct}% of capacity "
                    f"({total_connections}/{max_conn}). Threshold: {warning_threshold}%."
                ),
                severity="WARNING",
                details={
                    "total": total_connections,
                    "max": max_conn,
                    "usage_percent": usage_pct,
                    "states": states_dict
                }
            )

        return {
            "total_connections": total_connections,
            "max_connections": max_conn,
            "connection_usage_percent": usage_pct,
            "warning_threshold_percent": warning_threshold,
            "is_warning": is_warning,
            "states": states_dict,
            "by_database": by_database,
            "by_user": by_user
        }

    @staticmethod
    async def get_database_size(conn=None) -> Dict[str, Any]:
        """
        Current disk space consumed by database and top individual tables.
        """
        query_db = conn or db
        current_db = await query_db.fetchval("SELECT current_database();")
        total_size_bytes = await query_db.fetchval(
            "SELECT pg_database_size(current_database());"
        ) or 0

        # Query top tables with table size and index size
        tables_query = """
            SELECT
                schemaname as schema_name,
                relname as table_name,
                pg_total_relation_size(relid)::bigint as total_bytes,
                pg_relation_size(relid)::bigint as table_bytes,
                pg_indexes_size(relid)::bigint as index_bytes
            FROM pg_catalog.pg_stat_user_tables
            ORDER BY total_bytes DESC
            LIMIT 50;
        """
        records = await query_db.fetch(tables_query)
        tables = []
        for r in records:
            tables.append({
                "schema_name": r["schema_name"],
                "table_name": r["table_name"],
                "total_bytes": r["total_bytes"],
                "total_human": _human_size(r["total_bytes"]),
                "table_bytes": r["table_bytes"],
                "table_human": _human_size(r["table_bytes"]),
                "index_bytes": r["index_bytes"],
                "index_human": _human_size(r["index_bytes"]),
            })

        return {
            "database_name": current_db or "postgres",
            "total_bytes": total_size_bytes,
            "total_human": _human_size(total_size_bytes),
            "tables": tables
        }

    @staticmethod
    async def get_cache_hit_ratio(conn=None) -> Dict[str, Any]:
        """
        Buffer cache hit ratio (should ideally be > 95% for healthy DB memory).
        """
        query_db = conn or db
        current_db = await query_db.fetchval("SELECT current_database();")

        # Database buffer cache hit ratio
        cache_row = await query_db.fetchrow("""
            SELECT
                sum(blks_hit)::bigint as blks_hit,
                sum(blks_read)::bigint as blks_read
            FROM pg_stat_database
            WHERE datname = current_database();
        """)

        blks_hit = cache_row["blks_hit"] or 0
        blks_read = cache_row["blks_read"] or 0
        total_blocks = blks_hit + blks_read

        if total_blocks > 0:
            buffer_ratio = round((blks_hit / total_blocks) * 100.0, 2)
        else:
            buffer_ratio = 100.0

        # Index cache hit ratio
        idx_row = await query_db.fetchrow("""
            SELECT
                sum(idx_blks_hit)::bigint as idx_hit,
                sum(idx_blks_read)::bigint as idx_read
            FROM pg_statio_user_tables;
        """)
        idx_hit = (idx_row["idx_hit"] if idx_row else 0) or 0
        idx_read = (idx_row["idx_read"] if idx_row else 0) or 0
        idx_total = idx_hit + idx_read

        if idx_total > 0:
            index_ratio = round((idx_hit / idx_total) * 100.0, 2)
        else:
            index_ratio = 100.0

        threshold = settings.BUFFER_CACHE_HIT_WARNING_PERCENT
        is_healthy = buffer_ratio >= threshold

        recommendation = (
            "Cache performance is optimal (> 95%). Queries are serving efficiently from RAM."
            if is_healthy else
            f"Buffer cache hit ratio is below {threshold}%. Consider increasing shared_buffers or tuning heavy scan queries."
        )

        return {
            "database_name": current_db or "postgres",
            "buffer_cache_hit_ratio": buffer_ratio,
            "index_cache_hit_ratio": index_ratio,
            "blocks_read": blks_read,
            "blocks_hit": blks_hit,
            "is_healthy": is_healthy,
            "threshold_percent": threshold,
            "recommendation": recommendation
        }

    @staticmethod
    async def get_slow_queries(threshold_seconds: float = None, conn=None) -> Dict[str, Any]:
        """
        Identifies active queries currently running longer than threshold.
        """
        threshold = threshold_seconds or settings.SLOW_QUERY_THRESHOLD_SECONDS
        query = """
            SELECT
                pid,
                ROUND(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 2)::float as duration_seconds,
                query,
                state,
                usename as "user",
                client_addr::text,
                query_start::text
            FROM pg_stat_activity
            WHERE state != 'idle'
              AND pid <> pg_backend_pid()
              AND query NOT ILIKE '%pg_stat_activity%'
              AND (now() - query_start) > ($1 * interval '1 second')
            ORDER BY duration_seconds DESC;
        """
        query_db = conn or db
        records = await query_db.fetch(query, threshold)
        slow_queries = []
        for r in records:
            slow_queries.append({
                "pid": r["pid"],
                "duration_seconds": r["duration_seconds"],
                "query": r["query"],
                "state": r["state"],
                "user": r["user"],
                "client_addr": r["client_addr"],
                "query_start": r["query_start"]
            })

        return {
            "threshold_seconds": threshold,
            "count": len(slow_queries),
            "slow_queries": slow_queries
        }

    @staticmethod
    async def get_deadlocks(conn=None) -> Dict[str, Any]:
        """
        Counts of conflict errors, deadlocks, and rollbacks.
        """
        query_db = conn or db
        current_db = await query_db.fetchval("SELECT current_database();")
        row = await query_db.fetchrow("""
            SELECT
                COALESCE(deadlocks, 0)::int as deadlocks,
                COALESCE(conflicts, 0)::int as conflicts,
                COALESCE(xact_rollback, 0)::bigint as rollbacks,
                COALESCE(xact_commit, 0)::bigint as commits
            FROM pg_stat_database
            WHERE datname = current_database();
        """)

        deadlocks = row["deadlocks"] if row else 0
        conflicts = row["conflicts"] if row else 0
        rollbacks = row["rollbacks"] if row else 0
        commits = row["commits"] if row else 0
        total_xacts = rollbacks + commits
        rollback_ratio = round((rollbacks / total_xacts * 100.0), 2) if total_xacts > 0 else 0.0

        return {
            "database_name": current_db or "postgres",
            "deadlocks_count": deadlocks,
            "conflicts_count": conflicts,
            "rollbacks_count": rollbacks,
            "commits_count": commits,
            "rollback_ratio_percent": rollback_ratio
        }


metrics_service = MetricsService()

