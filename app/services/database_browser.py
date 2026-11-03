"""Read-only table and row browsing for registered database targets."""
import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import asyncpg
import pymongo
import pymysql
import redis

from app.services.database_registry import database_registry


class DatabaseBrowser:
    PAGE_MAX = 200
    TABLE_MAX = 500

    def _target(self, db_id: str) -> dict:
        target = next((item for item in database_registry._load() if item.get("id") == db_id), None)
        if not target:
            raise LookupError("Registered database not found.")
        return target

    @staticmethod
    def _engine(target: dict) -> str:
        engine = str(target.get("engine", "")).lower()
        return "postgres" if engine == "postgresql" else engine

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, (dict, list)):
            return json.loads(json.dumps(value, default=str))
        return value

    async def list_tables(self, db_id: str) -> dict:
        target = self._target(db_id)
        engine, url = self._engine(target), target.get("connection_url", "")
        if engine == "postgres":
            conn = await asyncpg.connect(dsn=url, timeout=6, server_settings={"default_transaction_read_only": "on", "statement_timeout": "15000"})
            try:
                rows = await conn.fetch("""SELECT table_schema AS schema, table_name AS name
                    FROM information_schema.tables WHERE table_type = 'BASE TABLE'
                    AND table_schema NOT IN ('pg_catalog', 'information_schema')
                    ORDER BY table_schema, table_name LIMIT $1""", self.TABLE_MAX)
                return {"engine": engine, "tables": [dict(row) for row in rows], "truncated": len(rows) == self.TABLE_MAX}
            finally:
                await conn.close()
        if engine in ("mysql", "mariadb"):
            parsed = urlparse(url)
            conn = pymysql.connect(host=parsed.hostname or "localhost", port=parsed.port or 3306, user=parsed.username or "root", password=parsed.password or "", database=parsed.path.lstrip("/") or None, connect_timeout=5, read_timeout=10, write_timeout=10, autocommit=False)
            try:
                conn.begin(readonly=True)
                with conn.cursor() as cursor:
                    cursor.execute("SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.tables WHERE TABLE_TYPE='BASE TABLE' AND TABLE_SCHEMA = COALESCE(%s, DATABASE()) ORDER BY TABLE_NAME LIMIT %s", (parsed.path.lstrip("/") or None, self.TABLE_MAX))
                    tables = [{"schema": schema, "name": name} for schema, name in cursor.fetchall()]
                conn.rollback()
                return {"engine": engine, "tables": tables, "truncated": len(tables) == self.TABLE_MAX}
            finally:
                conn.close()
        if engine == "mongodb":
            client = pymongo.MongoClient(url, serverSelectionTimeoutMS=5000, readPreference="secondaryPreferred")
            try:
                db_name = urlparse(url).path.lstrip("/").split("?")[0] or "admin"
                names = client[db_name].list_collection_names()[:self.TABLE_MAX]
                return {"engine": engine, "tables": [{"schema": db_name, "name": name} for name in names], "truncated": len(names) == self.TABLE_MAX}
            finally:
                client.close()
        if engine == "redis":
            raise NotImplementedError("Redis stores keys rather than tables; table browsing is not available for this target.")
        raise NotImplementedError(f"Browsing is not supported for '{engine}'.")

    async def read_rows(self, db_id: str, table: str, offset: int, limit: int) -> dict:
        target = self._target(db_id)
        engine, url = self._engine(target), target.get("connection_url", "")
        limit = min(limit, self.PAGE_MAX)
        if engine == "postgres":
            conn = await asyncpg.connect(dsn=url, timeout=6, server_settings={"default_transaction_read_only": "on", "statement_timeout": "15000"})
            try:
                async with conn.transaction(readonly=True):
                    exists = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_schema=$1 AND table_name=$2 AND table_schema NOT IN ('pg_catalog','information_schema'))""", *self._split_table(table))
                    if not exists:
                        raise ValueError("Choose a table from the registered table list.")
                    schema, name = self._split_table(table)
                    quoted = f'"{schema.replace(chr(34), chr(34)*2)}"."{name.replace(chr(34), chr(34)*2)}"'
                    rows = await conn.fetch(f"SELECT * FROM {quoted} LIMIT $1 OFFSET $2", limit, offset)
                    columns = await self._postgres_columns(conn, schema, name)
                    result_rows = [
                        {column: self._json_value(row[index]) for index, column in enumerate(columns)}
                        for row in rows
                    ]
                    return {"columns": columns, "rows": result_rows, "limit": limit, "offset": offset, "has_more": len(rows) == limit}
            finally:
                await conn.close()
        if engine in ("mysql", "mariadb"):
            parsed = urlparse(url)
            default_schema = parsed.path.lstrip("/")
            schema, name = self._split_table(table)
            if schema != default_schema:
                raise ValueError("Choose a table from the selected database.")
            conn = pymysql.connect(host=parsed.hostname or "localhost", port=parsed.port or 3306, user=parsed.username or "root", password=parsed.password or "", database=default_schema or None, connect_timeout=5, read_timeout=10, write_timeout=10, autocommit=False)
            try:
                conn.begin(readonly=True)
                with conn.cursor() as cursor:
                    cursor.execute("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s AND table_type='BASE TABLE')", (schema, name))
                    if not cursor.fetchone()[0]:
                        raise ValueError("Choose a table from the registered table list.")
                    quoted = f"`{schema.replace('`', '``')}`.`{name.replace('`', '``')}`"
                    cursor.execute(f"SELECT * FROM {quoted} LIMIT %s OFFSET %s", (limit, offset))
                    columns = [item[0] for item in cursor.description]
                    rows = [{column: self._json_value(value) for column, value in zip(columns, row)} for row in cursor.fetchall()]
                conn.rollback()
                return {"columns": columns, "rows": rows, "limit": limit, "offset": offset, "has_more": len(rows) == limit}
            finally:
                conn.rollback()
                conn.close()
        if engine == "mongodb":
            client = pymongo.MongoClient(url, serverSelectionTimeoutMS=5000, readPreference="secondaryPreferred")
            try:
                db_name = urlparse(url).path.lstrip("/").split("?")[0] or "admin"
                schema, name = self._split_table(table)
                if schema != db_name or name not in client[db_name].list_collection_names():
                    raise ValueError("Choose a collection from the registered collection list.")
                docs = list(client[db_name][name].find({}).skip(offset).limit(limit))
                columns = list(dict.fromkeys(key for doc in docs for key in doc.keys()))
                rows = [{key: self._json_value(value) for key, value in doc.items()} for doc in docs]
                return {"columns": columns, "rows": rows, "limit": limit, "offset": offset, "has_more": len(rows) == limit}
            finally:
                client.close()
        raise NotImplementedError(f"Row browsing is not supported for '{engine}'.")

    @staticmethod
    def _split_table(table: str) -> tuple[str, str]:
        parts = table.split(".", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError("Invalid table reference.")
        return parts[0], parts[1]

    @staticmethod
    async def _postgres_columns(conn, schema: str, name: str) -> list[str]:
        rows = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema=$1 AND table_name=$2 ORDER BY ordinal_position", schema, name)
        return [row["column_name"] for row in rows]


database_browser = DatabaseBrowser()
