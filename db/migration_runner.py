"""Transactional migration runner with persisted version/checksum history."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from db.migrations import MIGRATIONS, Migration


MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """Base error for schema migration failures."""


class MigrationDriftError(MigrationError):
    """Recorded history is newer than or different from the checked-in registry."""


class SchemaValidationError(MigrationError):
    """A recorded migration no longer has its required schema objects."""


def _migration_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone() is not None


def _applied_rows(conn: sqlite3.Connection) -> list[sqlite3.Row | tuple[Any, ...]]:
    if not _migration_table_exists(conn):
        return []
    return conn.execute(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()


def _row_value(row: sqlite3.Row | tuple[Any, ...], key: str, index: int) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key]
    return row[index]


def _validate_recorded_history(conn: sqlite3.Connection) -> dict[int, dict[str, str]]:
    known = {migration.version: migration for migration in MIGRATIONS}
    applied: dict[int, dict[str, str]] = {}
    for row in _applied_rows(conn):
        version = int(_row_value(row, "version", 0))
        name = str(_row_value(row, "name", 1))
        checksum = str(_row_value(row, "checksum", 2))
        applied_at = str(_row_value(row, "applied_at", 3))
        migration = known.get(version)
        if migration is None:
            raise MigrationDriftError(
                f"database migration {version} is newer than this application"
            )
        if name != migration.name or checksum != migration.checksum:
            raise MigrationDriftError(
                f"migration {version} history does not match the checked-in definition"
            )
        applied[version] = {
            "name": name,
            "checksum": checksum,
            "applied_at": applied_at,
        }
    return applied


def validate_schema(conn: sqlite3.Connection) -> None:
    """Verify that every operation in the recorded migration graph is present."""
    applied = _validate_recorded_history(conn)
    for migration in MIGRATIONS:
        if migration.version not in applied:
            continue
        missing = [
            f"{operation.kind}:{operation.table or ''}:{operation.name}"
            for operation in migration.operations
            if not operation.is_present(conn)
        ]
        if missing:
            raise SchemaValidationError(
                f"migration {migration.version} is recorded but schema objects are missing: "
                + ", ".join(missing)
            )


def _apply_one(conn: sqlite3.Connection, migration: Migration) -> bool:
    """Apply or adopt one migration under an immediate write transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = ?",
            (migration.version,),
        ).fetchone()
        if existing is not None:
            name = _row_value(existing, "name", 0)
            checksum = _row_value(existing, "checksum", 1)
            if name != migration.name or checksum != migration.checksum:
                raise MigrationDriftError(
                    f"migration {migration.version} changed while another process applied it"
                )
            conn.commit()
            return False

        for operation in migration.operations:
            if not operation.is_present(conn):
                conn.execute(operation.sql)
            if not operation.is_present(conn):
                raise SchemaValidationError(
                    f"migration {migration.version} did not create "
                    f"{operation.kind} {operation.name}"
                )

        conn.execute(
            """
            INSERT INTO schema_migrations (version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                migration.version,
                migration.name,
                migration.checksum,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def run_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration and return versions newly recorded."""
    conn.execute(MIGRATION_TABLE_SQL)
    conn.commit()
    _validate_recorded_history(conn)

    applied_now = []
    for migration in MIGRATIONS:
        if _apply_one(conn, migration):
            applied_now.append(migration.version)

    validate_schema(conn)
    return applied_now


def migration_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return read-only migration status without creating schema objects."""
    applied = _validate_recorded_history(conn)
    latest = MIGRATIONS[-1].version if MIGRATIONS else 0
    pending = [
        migration.version
        for migration in MIGRATIONS
        if migration.version not in applied
    ]
    return {
        "current": max(applied, default=0),
        "latest": latest,
        "pending": pending,
        "applied": [
            {
                "version": version,
                "name": applied[version]["name"],
                "checksum": applied[version]["checksum"],
                "applied_at": applied[version]["applied_at"],
            }
            for version in sorted(applied)
        ],
    }


def check_migrations(conn: sqlite3.Connection) -> dict[str, Any]:
    """Fail when migration history is pending, divergent, or structurally incomplete."""
    status = migration_status(conn)
    if status["pending"]:
        raise MigrationError(
            "pending migrations: " + ", ".join(map(str, status["pending"]))
        )
    validate_schema(conn)
    return status
