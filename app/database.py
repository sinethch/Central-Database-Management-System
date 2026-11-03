import time
import logging
from typing import Any, Dict, List, Optional
import asyncpg
from app.config import settings

logger = logging.getLogger("dbops.database")


class DatabaseManager:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def init_pool(self) -> None:
        if self.pool is not None:
            return

        logger.info("Initializing read-only asyncpg connection pool to target database...")
        try:
            # Enforce read-only transaction on every connection established
            async def init_connection(conn: asyncpg.Connection):
                await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;")
                await conn.execute("SET statement_timeout = '15s';")

            self.pool = await asyncpg.create_pool(
                dsn=settings.TARGET_DATABASE_URL,
                min_size=1,
                max_size=5,
                timeout=10.0,
                command_timeout=15.0,
                init=init_connection,
            )
            logger.info("Connection pool successfully established.")
        except Exception as e:
            logger.error(f"Failed to initialize database connection pool: {e}")
            # Do not raise here so the app can start up and report degraded health
            self.pool = None

    async def close_pool(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
            logger.info("Database connection pool closed.")

    async def check_connectivity(self) -> Dict[str, Any]:
        """Ping database, calculate latency and fetch version."""
        start = time.perf_counter()
        if not self.pool:
            try:
                await self.init_pool()
            except Exception as e:
                return {
                    "connected": False,
                    "error": str(e),
                    "latency_ms": None,
                    "version": None,
                }

        if not self.pool:
            return {
                "connected": False,
                "error": "Database pool is not available",
                "latency_ms": None,
                "version": None,
            }

        try:
            async with self.pool.acquire() as conn:
                version = await conn.fetchval("SELECT version();")
                uptime_data = await conn.fetchrow(
                    "SELECT pg_postmaster_start_time() as start_time, "
                    "now() - pg_postmaster_start_time() as uptime;"
                )
                latency = round((time.perf_counter() - start) * 1000, 2)
                return {
                    "connected": True,
                    "latency_ms": latency,
                    "version": version,
                    "started_at": uptime_data["start_time"].isoformat() if uptime_data else None,
                    "uptime": str(uptime_data["uptime"]) if uptime_data else None,
                    "error": None,
                }
        except Exception as e:
            return {
                "connected": False,
                "error": str(e),
                "latency_ms": None,
                "version": None,
            }

    async def fetch(self, query: str, *args) -> List[Dict[str, Any]]:
        if not self.pool:
            await self.init_pool()
        if not self.pool:
            raise ConnectionError("Database pool is offline")

        async with self.pool.acquire() as conn:
            records = await conn.fetch(query, *args)
            return [dict(record) for record in records]

    async def fetchrow(self, query: str, *args) -> Optional[Dict[str, Any]]:
        if not self.pool:
            await self.init_pool()
        if not self.pool:
            raise ConnectionError("Database pool is offline")

        async with self.pool.acquire() as conn:
            record = await conn.fetchrow(query, *args)
            return dict(record) if record else None

    async def fetchval(self, query: str, *args) -> Any:
        if not self.pool:
            await self.init_pool()
        if not self.pool:
            raise ConnectionError("Database pool is offline")

        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args)


db = DatabaseManager()

