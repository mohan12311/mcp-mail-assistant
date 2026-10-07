"""Command-line interface for MCP Mail Assistant SQLite migrations."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence, TextIO

from db.connection import get_connection, get_db_path
from db.init_db import initialize
from db.migration_runner import MigrationError, check_migrations, migration_status


def _print_status(status: dict, out: TextIO, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2), file=out)
        return
    print(f"Database: {get_db_path()}", file=out)
    print(f"Current migration: {status['current']}", file=out)
    print(f"Latest migration: {status['latest']}", file=out)
    pending = ", ".join(map(str, status["pending"])) or "none"
    print(f"Pending migrations: {pending}", file=out)


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(description="Manage MCP Mail Assistant SQLite migrations")
    parser.add_argument("command", choices=("status", "upgrade", "check"))
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    args = parser.parse_args(argv)

    try:
        if args.command == "upgrade":
            initialize()

        with get_connection() as conn:
            status = (
                check_migrations(conn)
                if args.command == "check"
                else migration_status(conn)
            )
        _print_status(status, out, as_json=args.json)
        return 0
    except MigrationError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=err)
        else:
            print(f"Migration error: {exc}", file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
