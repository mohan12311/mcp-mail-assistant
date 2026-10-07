"""Versioned SQLite schema migrations for MCP Mail Assistant."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe SQLite identifier: {value!r}")
    return value


@dataclass(frozen=True)
class Operation:
    """One idempotent schema operation with an explicit presence check."""

    kind: Literal["table", "column", "index", "data"]
    name: str
    sql: str
    table: str | None = None
    check_sql: str | None = None

    def is_present(self, conn: sqlite3.Connection) -> bool:
        if self.kind in {"table", "index"}:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
                (self.kind, self.name),
            ).fetchone()
            return row is not None

        if self.kind == "column":
            table = _identifier(self.table or "")
            column_name = _identifier(self.name)
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            return any(row[1] == column_name for row in rows)

        if not self.check_sql:
            raise ValueError(f"data migration {self.name!r} requires check_sql")
        row = conn.execute(self.check_sql).fetchone()
        return bool(row and row[0])


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    operations: tuple[Operation, ...]

    @property
    def checksum(self) -> str:
        payload = {
            "version": self.version,
            "name": self.name,
            "operations": [
                {
                    "kind": operation.kind,
                    "name": operation.name,
                    "table": operation.table,
                    "sql": operation.sql.strip(),
                    **(
                        {"check_sql": operation.check_sql.strip()}
                        if operation.check_sql is not None
                        else {}
                    ),
                }
                for operation in self.operations
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def table(name: str, sql: str) -> Operation:
    return Operation("table", _identifier(name), sql)


def column(table_name: str, name: str, definition: str) -> Operation:
    table_name = _identifier(table_name)
    name = _identifier(name)
    return Operation(
        "column",
        name,
        f'ALTER TABLE "{table_name}" ADD COLUMN "{name}" {definition}',
        table=table_name,
    )


def index(name: str, sql: str) -> Operation:
    return Operation("index", _identifier(name), sql)


def data(name: str, sql: str, *, check_sql: str) -> Operation:
    """Return an idempotent data-repair operation with an explicit invariant."""
    return Operation("data", _identifier(name), sql, check_sql=check_sql)


from db.migrations.registry import MIGRATIONS  # noqa: E402  (definitions use helpers)


__all__ = [
    "MIGRATIONS",
    "Migration",
    "Operation",
    "column",
    "data",
    "index",
    "table",
]
