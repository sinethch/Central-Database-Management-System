import logging
from typing import Dict, Any, List
from app.database import db

logger = logging.getLogger("dbops.diagnostics")


class DiagnosticsService:
    @staticmethod
    async def get_waiting_locks(conn=None) -> Dict[str, Any]:
        """
        Detects transactions waiting on locks held by another process.
        Returns the blocked queries, blocking queries, and lock modes.
        """
        query = """
            SELECT
                blocked_locks.pid     AS blocked_pid,
                blocked_activity.usename  AS blocked_user,
                blocked_activity.query    AS blocked_statement,
                blocking_locks.pid    AS blocking_pid,
                blocking_activity.usename AS blocking_user,
                blocking_activity.query   AS blocking_statement,
                ROUND(EXTRACT(EPOCH FROM (now() - blocked_activity.query_start))::numeric, 2)::float AS waiting_duration_seconds,
                blocked_locks.locktype AS lock_type,
                blocked_locks.mode     AS mode
            FROM  pg_catalog.pg_locks         blocked_locks
            JOIN pg_catalog.pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
            JOIN pg_catalog.pg_locks         blocking_locks
                ON blocking_locks.locktype = blocked_locks.locktype
                AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
                AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
                AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
                AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
                AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
                AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
                AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
                AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
                AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
                AND blocking_locks.pid != blocked_locks.pid
            JOIN pg_catalog.pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
            WHERE NOT blocked_locks.granted
            ORDER BY waiting_duration_seconds DESC;
        """
        query_db = conn or db
        records = await query_db.fetch(query)
        locks = []
        for r in records:
            locks.append({
                "blocked_pid": r["blocked_pid"],
                "blocked_user": r["blocked_user"],
                "blocked_statement": r["blocked_statement"],
                "blocking_pid": r["blocking_pid"],
                "blocking_user": r["blocking_user"],
                "blocking_statement": r["blocking_statement"],
                "waiting_duration_seconds": r["waiting_duration_seconds"],
                "lock_type": r["lock_type"],
                "mode": r["mode"],
            })

        return {
            "total_waiting_locks": len(locks),
            "has_conflicts": len(locks) > 0,
            "locks": locks
        }

    @staticmethod
    async def get_table_bloat(conn=None) -> Dict[str, Any]:
        """
        Estimates dead tuples in user tables to verify autovacuum efficiency.
        """
        query = """
            SELECT
                schemaname as schema_name,
                relname as table_name,
                n_live_tup::bigint as live_tuples,
                n_dead_tup::bigint as dead_tuples,
                last_vacuum::text,
                last_autovacuum::text,
                autovacuum_count::int
            FROM pg_catalog.pg_stat_user_tables
            ORDER BY n_dead_tup DESC
            LIMIT 50;
        """
        query_db = conn or db
        records = await query_db.fetch(query)
        tables = []
        needing_vacuum = 0

        for r in records:
            live = r["live_tuples"] or 0
            dead = r["dead_tuples"] or 0
            total = live + dead
            dead_ratio = round((dead / total * 100.0), 2) if total > 0 else 0.0

            if dead_ratio > 20.0 and dead > 1000:
                recommendation = "High dead tuples detected (>20%). Manual VACUUM or autovacuum tuning advised."
                needing_vacuum += 1
            elif dead_ratio > 10.0:
                recommendation = "Moderate bloat. Autovacuum is monitoring."
            else:
                recommendation = "Healthy tuple density. Autovacuum is keeping up."

            tables.append({
                "schema_name": r["schema_name"],
                "table_name": r["table_name"],
                "live_tuples": live,
                "dead_tuples": dead,
                "dead_ratio_percent": dead_ratio,
                "last_vacuum": r["last_vacuum"],
                "last_autovacuum": r["last_autovacuum"],
                "autovacuum_count": r["autovacuum_count"] or 0,
                "recommendation": recommendation,
            })

        return {
            "total_tables_checked": len(tables),
            "tables_needing_vacuum": needing_vacuum,
            "tables": tables
        }


diagnostics_service = DiagnosticsService()

