"""PII-minimal event queue operations CLI.

This command never sends mail or calls Slack. Generic DLQ retry deliberately
rejects SEND_CONFIRMED; operators must use the separately reviewed send retry
workflow for send_failed/delivery_unknown tasks.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence, TextIO

from db import event_store
from db.connection import get_connection
from db.migration_runner import MigrationError, check_migrations


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mail_operator.events",
        description="Inspect mail-agent event health and explicitly re-arm safe DLQ events.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    metrics = commands.add_parser("metrics", help="print count-only queue metrics")
    metrics.add_argument("--json", action="store_true")

    dlq = commands.add_parser("dlq", help="inspect or re-arm the failed-event queue")
    dlq_commands = dlq.add_subparsers(dest="dlq_command", required=True)
    dlq_list = dlq_commands.add_parser("list", help="list DLQ metadata without payloads")
    dlq_list.add_argument("--limit", type=int, default=50)
    dlq_list.add_argument("--json", action="store_true")
    retry = dlq_commands.add_parser("retry", help="explicitly re-arm one safe event")
    retry.add_argument("event_id")
    retry.add_argument(
        "--confirm",
        required=True,
        help="re-enter the exact event ID after reviewing the DLQ entry",
    )
    return parser


def _print_metrics(metrics: dict, *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(metrics, ensure_ascii=False, sort_keys=True), file=out)
        return
    events = metrics["events"]
    outbox = metrics["outbox"]
    print(
        "events "
        f"pending={events['pending']} leased={events['leased']} "
        f"done={events['done']} failed={events['failed']} "
        f"retry_due={events['retry_due']} expired_leases={events['expired_leases']}",
        file=out,
    )
    print(
        "outbox "
        f"pending={outbox['pending']} dispatching={outbox['dispatching']} "
        f"dispatched={outbox['dispatched']} discarded={outbox['discarded']} "
        f"delivery_unknown={outbox['delivery_unknown']}",
        file=out,
    )
    if events["dlq_by_type"]:
        print(
            "dlq_by_type "
            + " ".join(
                f"{event_type}={count}"
                for event_type, count in sorted(events["dlq_by_type"].items())
            ),
            file=out,
        )


def _print_dlq(rows: list[dict], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, sort_keys=True), file=out)
        return
    if not rows:
        print("DLQ empty", file=out)
        return
    for row in rows:
        print(
            f"{row['id']} type={row['event_type']} "
            f"attempts={row['attempts']}/{row['max_attempts']} "
            f"updated_at={row['updated_at']}",
            file=out,
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    try:
        with get_connection() as conn:
            check_migrations(conn)

        if args.command == "metrics":
            _print_metrics(
                event_store.get_operational_metrics(),
                as_json=args.json,
                out=out,
            )
            return 0

        if args.dlq_command == "list":
            limit = max(1, min(int(args.limit), 500))
            _print_dlq(
                event_store.list_dlq_summary(limit=limit),
                as_json=args.json,
                out=out,
            )
            return 0

        if args.confirm != args.event_id:
            print("confirmation does not match the event ID", file=err)
            return 2
        if not event_store.retry_event(args.event_id):
            print("event is not in the DLQ", file=err)
            return 1
        print(f"event re-armed: {args.event_id}", file=out)
        return 0
    except event_store.UnsafeEventRetry:
        print(
            "send_confirmed cannot be retried here; use the reviewed send retry workflow",
            file=err,
        )
        return 2
    except MigrationError as exc:
        print(f"migration check failed: {exc}", file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
