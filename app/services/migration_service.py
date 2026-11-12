"""Read-only discovery of common PostgreSQL migration history tables."""

from datetime import date, datetime
from typing import Any, Dict, List

import asyncpg

from app.services.multi_db_manager import multi_db_manager


MIGRATION_SOURCES = [
    ("Flyway", "flyway_schema_history", """
        SELECT COALESCE(version, ''), COALESCE(description, script, ''), installed_on,
               CASE WHEN success THEN 'success' ELSE 'failed' END
        FROM flyway_schema_history ORDER BY installed_rank DESC LIMIT 100
    """),
    ("Liquibase", "databasechangelog", """
        SELECT COALESCE(id, ''), COALESCE(author, '') || ' · ' || COALESCE(filename, ''),
               dateexecuted, COALESCE(exectype, 'executed')
        FROM databasechangelog ORDER BY orderexecuted DESC LIMIT 100
    """),
    ("Prisma", "_prisma_migrations", """
        SELECT migration_name, migration_name, started_at,
               CASE WHEN rolled_back_at IS NOT NULL THEN 'rolled back'
                    WHEN finished_at IS NULL THEN 'incomplete/failed' ELSE 'success' END
        FROM _prisma_migrations ORDER BY started_at DESC LIMIT 100
    """),
    ("Django", "django_migrations", """
        SELECT app || '.' || name, app || '.' || name, applied, 'applied'
        FROM django_migrations ORDER BY applied DESC LIMIT 100
    """),
    # Alembic stores only the current head(s), not an append-only history.
    # It still must be monitored because it is a common migration framework.
    ("Alembic", "alembic_version", """
        SELECT version_num, version_num, NULL::timestamptz, 'current revision'
        FROM alembic_version ORDER BY version_num LIMIT 100
    """),
    ("Rails", "schema_migrations", """
        SELECT version, version, NULL::timestamptz, 'applied'
        FROM schema_migrations ORDER BY version DESC LIMIT 100
    """),
    ("Sequelize", '"SequelizeMeta"', """
        SELECT name, name, NULL::timestamptz, 'applied'
        FROM "SequelizeMeta" ORDER BY name DESC LIMIT 100
    """),
]


def _iso(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value) if value is not None else None


class MigrationHistoryService:
    async def get_history(self, db_id: str | None = None) -> Dict[str, Any]:
        target = multi_db_manager.get_database_info(db_id) if db_id else multi_db_manager.get_default_db()
        if not target:
            raise LookupError("Database not found in registry")

        engine = str(target.get("engine", "")).lower()
        if engine not in {"postgres", "postgresql"}:
            return {
                "supported": False,
                "status": "unsupported",
                "database_name": target.get("name", "Database"),
                "frameworks": [], "migrations": [],
                "message": "Migration history discovery currently supports PostgreSQL targets only.",
            }

        conn = await asyncpg.connect(
            dsn=target.get("connection_url", ""), timeout=5,
            server_settings={"default_transaction_read_only": "on", "statement_timeout": "5000"},
        )
        found: List[str] = []
        migrations: List[Dict[str, Any]] = []
        inaccessible: List[str] = []
        try:
            database_name = await conn.fetchval("SELECT current_database()")
            for framework, relation, query in MIGRATION_SOURCES:
                exists = await conn.fetchval("SELECT to_regclass($1)", relation)
                if not exists:
                    continue
                found.append(framework)
                try:
                    # Keep each ledger read isolated. A table may exist but
                    # have a schema variant or permissions that prevent reading;
                    # continue monitoring the other recognized frameworks.
                    rows = await conn.fetch(query)
                    migrations.extend({
                        "framework": framework,
                        "version": row[0],
                        "name": row[1],
                        "applied_at": _iso(row[2]),
                        "status": row[3],
                    } for row in rows)
                except Exception:
                    # Metadata table exists, but the monitoring role may not be
                    # allowed to read its rows. Do not expose DB error details.
                    inaccessible.append(framework)
        finally:
            await conn.close()

        if found:
            status = "partial" if inaccessible else "detected"
            message = "Migration history is read from existing migration ledger tables."
            if inaccessible:
                message += " Some detected ledgers are not readable by this database user."
        else:
            status = "not_detected"
            message = (
                "No recognized migration history table was found. This does not prove that "
                "migrations were never run; the application may use a custom or external ledger."
            )
        migrations.sort(key=lambda item: item["applied_at"] or "", reverse=True)
        return {
            "supported": True, "status": status, "database_name": database_name,
            "frameworks": found, "inaccessible": inaccessible,
            "migrations": migrations[:200], "message": message,
        }


migration_history_service = MigrationHistoryService()
