import json
import uuid
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

from app.config import settings
from app.crypto_utils import decrypt_persisted_secret, encrypt_persisted_secret
from app.models.database_registry import DatabaseInstance

DATABASES_FILE = Path(settings.BACKUP_STORAGE_DIR) / "registered_databases.json"


def mask_url(url: str) -> str:
    """Masks database passwords in connection URLs for secure display."""
    try:
        # Match :password@ pattern
        return re.sub(r":([^@/]+)@", ":****@", url)
    except Exception:
        return url


class DatabaseRegistryService:
    def __init__(self):
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        settings.get_backup_path()
        if not DATABASES_FILE.exists():
            # Seed with primary TARGET_DATABASE_URL from environment
            primary_db = {
                "id": "db-primary",
                "name": "Primary PostgreSQL (Production)",
                "engine": "postgres",
                "connection_url": settings.TARGET_DATABASE_URL,
                "read_only_verified": False,
                "status": "online",
                "latency_ms": 1.2,
                "version": "PostgreSQL 16",
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            with open(DATABASES_FILE, "w", encoding="utf-8") as f:
                json.dump([primary_db], f, indent=2)

    def _load(self) -> List[Dict[str, Any]]:
        self._ensure_storage()
        try:
            with open(DATABASES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, list):
                    return []
                for item in data:
                    saved_url = item.get("connection_url", "")
                    if saved_url.startswith("enc:v1:"):
                        item["connection_url"] = decrypt_persisted_secret(saved_url)
                    # Connectivity checks do not establish that the account is
                    # read-only, so never expose an unverified record as verified.
                    item["read_only_verified"] = False
                    if item.get("id") == "db-primary":
                        if item.get("connection_url") != settings.TARGET_DATABASE_URL:
                            item["connection_url"] = settings.TARGET_DATABASE_URL
                return data
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("Could not read the registered database file. Fix or restore its JSON before continuing.")

    def _save(self, items: List[Dict[str, Any]]) -> None:
        self._ensure_storage()
        temp_path = DATABASES_FILE.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            persisted = []
            for row in items:
                saved = dict(row)
                if saved.get("connection_url"):
                    saved["connection_url"] = encrypt_persisted_secret(saved["connection_url"])
                persisted.append(saved)
            json.dump(persisted, f, indent=2)
        temp_path.replace(DATABASES_FILE)

    def list_databases(self) -> List[DatabaseInstance]:
        raw = self._load()
        results = []
        for r in raw:
            item = dict(r)
            # Mask the connection url before returning to client
            item["connection_url"] = mask_url(item["connection_url"])
            results.append(DatabaseInstance(**item))
        return results

    async def add_database(self, name: str, engine: str, connection_url: str, verify: bool = True, server_id: Optional[str] = None, container_id: Optional[str] = None, read_only_verified: bool = False) -> DatabaseInstance:
        engine = engine.lower()
        if engine == "postgresql":
            engine = "postgres"
        if engine not in ["postgres", "mongodb", "mysql", "redis"]:
            raise ValueError(f"Unsupported database engine '{engine}'. Supported engines: postgres, mongodb, mysql, redis.")

        name = (name or "").strip()
        connection_url = (connection_url or "").strip()
        if not name:
            raise ValueError("A database label is required.")
        if not connection_url:
            raise ValueError("A connection URL is required.")

        db_id = f"db-{str(uuid.uuid4())[:8]}"
        created_at = datetime.now(timezone.utc).isoformat()

        # Real, non-invasive connectivity test against the provided URL.
        status = "online"
        latency: Optional[float] = None
        version = f"{engine.title()} Server"
        last_error: Optional[str] = None
        if verify:
            result = await self.verify_connection(engine, connection_url)
            status = "online" if result.get("connected") else "offline"
            latency = result.get("latency_ms")
            version = result.get("version") or version
            last_error = result.get("error")

        new_entry = {
            "id": db_id,
            "name": name,
            "engine": engine,
            "connection_url": connection_url,
            "read_only_verified": read_only_verified,
            "server_id": server_id,
            "container_id": container_id,
            "status": status,
            "latency_ms": latency,
            "version": version,
            "last_error": last_error,
            "last_checked_at": created_at,
            "created_at": created_at,
        }

        records = self._load()
        records.append(new_entry)
        self._save(records)

        # Return masked version
        res_entry = dict(new_entry)
        res_entry["connection_url"] = mask_url(connection_url)
        return DatabaseInstance(**res_entry)

    def rename_database(self, db_id: str, name: str) -> DatabaseInstance:
        name = (name or "").strip()
        if not name:
            raise ValueError("A target display name is required.")
        if len(name) > 120:
            raise ValueError("Target display names must be 120 characters or fewer.")

        records = self._load()
        target = next((record for record in records if record.get("id") == db_id), None)
        if target is None:
            raise LookupError(f"Database '{db_id}' not found.")
        target["name"] = name
        self._save(records)

        result = dict(target)
        result["connection_url"] = mask_url(result.get("connection_url", ""))
        return DatabaseInstance(**result)

    async def verify_connection(self, engine: str, connection_url: str) -> Dict[str, Any]:
        """Attempts a short, read-only connection to the target and reports the result."""
        engine = (engine or "").lower()
        if engine == "postgresql":
            engine = "postgres"
        start = time.perf_counter()

        def elapsed() -> float:
            return round((time.perf_counter() - start) * 1000, 2)

        try:
            if engine == "postgres":
                import asyncpg
                conn = await asyncpg.connect(dsn=connection_url, timeout=6)
                try:
                    raw = await conn.fetchval("SELECT version();")
                finally:
                    await conn.close()
                return {"connected": True, "latency_ms": elapsed(),
                        "version": (raw or "PostgreSQL").split(" on ")[0].strip(), "error": None}

            if engine == "mongodb":
                import pymongo
                client = pymongo.MongoClient(connection_url, serverSelectionTimeoutMS=5000)
                try:
                    info = client.server_info()
                finally:
                    client.close()
                return {"connected": True, "latency_ms": elapsed(),
                        "version": f"MongoDB {info.get('version', '')}".strip(), "error": None}

            if engine == "mysql":
                import pymysql
                parsed = urlparse(connection_url)
                conn = pymysql.connect(
                    host=parsed.hostname or "localhost",
                    port=parsed.port or 3306,
                    user=parsed.username or "root",
                    password=parsed.password or "",
                    database=(parsed.path.lstrip("/") or None),
                    connect_timeout=5,
                )
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT VERSION();")
                        row = cursor.fetchone()
                finally:
                    conn.close()
                return {"connected": True, "latency_ms": elapsed(),
                        "version": f"MySQL {row[0] if row else ''}".strip(), "error": None}

            if engine == "redis":
                import redis
                client = redis.from_url(connection_url, socket_timeout=5)
                try:
                    info = client.info()
                finally:
                    client.close()
                return {"connected": True, "latency_ms": elapsed(),
                        "version": f"Redis {info.get('redis_version', '')}".strip(), "error": None}

            return {"connected": False, "latency_ms": None, "version": None,
                    "error": f"Unsupported engine '{engine}'."}
        except Exception as e:
            return {"connected": False, "latency_ms": None, "version": None,
                    "error": str(e) or e.__class__.__name__}

    def remove_database(self, db_id: str) -> bool:
        records = self._load()
        filtered = [r for r in records if r["id"] != db_id]
        if len(filtered) != len(records):
            self._save(filtered)
            return True
        return False

    def remove_container_registration(self, server_id: str, container_id: str) -> bool:
        records = self._load()
        filtered = [r for r in records if not (r.get("server_id") == server_id and r.get("container_id") == container_id)]
        if len(filtered) != len(records):
            self._save(filtered)
            return True
        return False


database_registry = DatabaseRegistryService()
