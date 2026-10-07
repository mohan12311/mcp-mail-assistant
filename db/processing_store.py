"""Atomic new-email commit and durable local/notification outbox operations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from db.connection import get_connection


OUTBOX_PENDING = "pending"
OUTBOX_DISPATCHING = "dispatching"
OUTBOX_DISPATCHED = "dispatched"
OUTBOX_DISCARDED = "discarded"
OUTBOX_DELIVERY_UNKNOWN = "delivery_unknown"

TERMINAL_OUTBOX_STATUSES = frozenset(
    {OUTBOX_DISPATCHED, OUTBOX_DISCARDED, OUTBOX_DELIVERY_UNKNOWN}
)


def _now() -> datetime:
    return datetime.now()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _task_result(row: Any) -> dict[str, Any]:
    return {
        "task_id": int(row["id"]),
        "email_id": row["email_id"],
        "task_type": row["task_type"],
        "title": row["title"],
        "description": row["description"] or "",
        "requires_approval": row["approval_status"] == "pending",
        "send_state": row["send_state"],
        "send_payload": (
            json.loads(row["send_payload"])
            if row["send_payload"]
            else None
        ),
    }


def _load_commit_result(conn: Any, result_json: str) -> dict[str, Any]:
    result = json.loads(result_json)
    task_ids = [int(task_id) for task_id in result.get("task_ids", [])]
    tasks = []
    for task_id in task_ids:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is not None:
            tasks.append(_task_result(row))
    return {
        "already_committed": True,
        "adopted_legacy_completion": bool(result.get("adopted_legacy_completion")),
        "tasks": tasks,
    }


def _find_legacy_equivalent_task(
    conn: Any,
    *,
    spec: dict[str, Any],
    claimed_ids: set[int],
) -> Any | None:
    rows = conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE email_id = ?
          AND task_type = ?
          AND title = ?
          AND COALESCE(description, '') = ?
          AND COALESCE(deadline, '') = ?
          AND priority = ?
          AND approval_status = ?
          AND idempotency_key IS NULL
        ORDER BY id
        """,
        (
            spec["email_id"],
            spec["task_type"],
            spec["title"],
            spec.get("description") or "",
            spec.get("deadline") or "",
            spec.get("priority", "medium"),
            spec.get("approval_status", "none"),
        ),
    ).fetchall()
    return next((row for row in rows if int(row["id"]) not in claimed_ids), None)


def _upsert_task(
    conn: Any,
    *,
    spec: dict[str, Any],
    claimed_ids: set[int],
) -> Any:
    existing = conn.execute(
        "SELECT * FROM tasks WHERE idempotency_key = ?",
        (spec["idempotency_key"],),
    ).fetchone()
    if existing is not None:
        return existing

    legacy = _find_legacy_equivalent_task(conn, spec=spec, claimed_ids=claimed_ids)
    if legacy is not None:
        conn.execute(
            "UPDATE tasks SET idempotency_key = ? WHERE id = ? AND idempotency_key IS NULL",
            (spec["idempotency_key"], legacy["id"]),
        )
        return conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (legacy["id"],)
        ).fetchone()

    cursor = conn.execute(
        """
        INSERT INTO tasks (
            email_id, task_type, title, description, deadline,
            priority, status, approval_status, idempotency_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            spec["email_id"],
            spec["task_type"],
            spec["title"],
            spec.get("description"),
            spec.get("deadline"),
            spec.get("priority", "medium"),
            spec.get("status", "pending"),
            spec.get("approval_status", "none"),
            spec["idempotency_key"],
        ),
    )
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()


def _insert_outbox(
    conn: Any,
    *,
    aggregate_id: str,
    topic: str,
    idempotency_key: str,
    payload: dict[str, Any],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO operator_outbox (
            aggregate_type, aggregate_id, topic, idempotency_key,
            payload_json, status, created_at, updated_at
        ) VALUES ('email', ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (aggregate_id, topic, idempotency_key, _json(payload), now, now),
    )


def commit_new_email_processing(
    *,
    source_event_id: str,
    email_id: str,
    analysis: dict[str, Any],
    task_specs: Iterable[dict[str, Any]],
    slack_enabled: bool,
) -> dict[str, Any]:
    """Commit analysis, tasks, processed state, and all follow-up intents once."""
    if not source_event_id or not email_id:
        raise ValueError("source_event_id and email_id are required")

    specs = [dict(spec) for spec in task_specs]
    now = _now().isoformat()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            "SELECT result_json FROM email_processing_commits WHERE email_id = ?",
            (email_id,),
        ).fetchone()
        if prior is not None:
            return _load_commit_result(conn, prior["result_json"])

        email = conn.execute(
            "SELECT id, is_processed FROM emails WHERE id = ?", (email_id,)
        ).fetchone()
        if email is None:
            raise ValueError("email does not exist")

        if bool(email["is_processed"]):
            existing_ids = [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE email_id = ? ORDER BY id", (email_id,)
                ).fetchall()
            ]
            result = {
                "task_ids": existing_ids,
                "adopted_legacy_completion": True,
            }
            conn.execute(
                """
                INSERT INTO email_processing_commits (
                    email_id, source_event_id, result_json, committed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (email_id, source_event_id, _json(result), now),
            )
            loaded = _load_commit_result(conn, _json(result))
            loaded["already_committed"] = False
            return loaded

        conn.execute(
            """
            UPDATE emails
            SET body_summary = ?, priority_score = ?, priority_level = ?,
                urgency = ?, requires_response = ?
            WHERE id = ?
            """,
            (
                analysis.get("summary"),
                analysis.get("priority_score"),
                analysis.get("priority_level"),
                analysis.get("urgency"),
                bool(analysis.get("requires_response", False)),
                email_id,
            ),
        )

        claimed_ids: set[int] = set()
        task_rows = []
        for spec in specs:
            if spec.get("email_id") != email_id or not spec.get("idempotency_key"):
                raise ValueError("task spec has invalid email_id or idempotency_key")
            row = _upsert_task(conn, spec=spec, claimed_ids=claimed_ids)
            claimed_ids.add(int(row["id"]))
            task_rows.append(row)

        for row in task_rows:
            task_id = int(row["id"])
            if row["task_type"] == "reply_email":
                _insert_outbox(
                    conn,
                    aggregate_id=email_id,
                    topic="reply_draft",
                    idempotency_key=f"new-email:{email_id}:task:{task_id}:reply-draft",
                    payload={"task_id": task_id},
                    now=now,
                )

        if slack_enabled:
            _insert_outbox(
                conn,
                aggregate_id=email_id,
                topic="slack_email_summary",
                idempotency_key=f"new-email:{email_id}:slack:summary",
                payload={"email_id": email_id},
                now=now,
            )
            for row in task_rows:
                if row["approval_status"] != "pending":
                    continue
                task_id = int(row["id"])
                topic = (
                    "slack_reply_button"
                    if row["task_type"] == "reply_email"
                    else "slack_task_approval"
                )
                _insert_outbox(
                    conn,
                    aggregate_id=email_id,
                    topic=topic,
                    idempotency_key=f"new-email:{email_id}:slack:task:{task_id}:{topic}",
                    payload={"task_id": task_id},
                    now=now,
                )

        conn.execute("UPDATE emails SET is_processed = 1 WHERE id = ?", (email_id,))
        result = {
            "task_ids": [int(row["id"]) for row in task_rows],
            "adopted_legacy_completion": False,
        }
        conn.execute(
            """
            INSERT INTO email_processing_commits (
                email_id, source_event_id, result_json, committed_at
            ) VALUES (?, ?, ?, ?)
            """,
            (email_id, source_event_id, _json(result), now),
        )
        return {
            "already_committed": False,
            "adopted_legacy_completion": False,
            "tasks": [_task_result(row) for row in task_rows],
        }


def get_new_email_processing(email_id: str) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT result_json FROM email_processing_commits WHERE email_id = ?",
            (email_id,),
        ).fetchone()
        return _load_commit_result(conn, row["result_json"]) if row else None


def list_outbox_for_email(email_id: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM operator_outbox
            WHERE aggregate_type = 'email' AND aggregate_id = ?
            ORDER BY id
            """,
            (email_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result


def claim_outbox(
    outbox_id: int,
    *,
    owner: str,
    lease_seconds: int,
) -> Optional[dict[str, Any]]:
    now = _now()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE operator_outbox
            SET status = 'dispatching', lease_owner = ?, lease_until = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE id = ? AND status = 'pending'
              AND (available_at IS NULL OR available_at <= ?)
            """,
            (
                owner,
                (now + timedelta(seconds=lease_seconds)).isoformat(),
                now.isoformat(),
                outbox_id,
                now.isoformat(),
            ),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM operator_outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item


def finish_outbox(outbox_id: int, *, owner: str, discarded: bool = False) -> bool:
    status = OUTBOX_DISCARDED if discarded else OUTBOX_DISPATCHED
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE operator_outbox
            SET status = ?, lease_owner = NULL, lease_until = NULL,
                available_at = NULL, last_error_code = NULL, updated_at = ?
            WHERE id = ? AND status = 'dispatching' AND lease_owner = ?
            """,
            (status, _now().isoformat(), outbox_id, owner),
        )
        return cursor.rowcount == 1


def retry_outbox(
    outbox_id: int,
    *,
    owner: str,
    error_code: str,
    base_delay_seconds: int = 5,
    max_delay_seconds: int = 3600,
) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT attempts FROM operator_outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
        if row is None:
            return False
        base_delay_seconds = max(1, int(base_delay_seconds))
        max_delay_seconds = max(base_delay_seconds, int(max_delay_seconds))
        delay = min(
            max_delay_seconds,
            base_delay_seconds * (2 ** max(0, int(row["attempts"]) - 1)),
        )
        now = _now()
        cursor = conn.execute(
            """
            UPDATE operator_outbox
            SET status = 'pending', lease_owner = NULL, lease_until = NULL,
                available_at = ?, last_error_code = ?, updated_at = ?
            WHERE id = ? AND status = 'dispatching' AND lease_owner = ?
            """,
            (
                (now + timedelta(seconds=delay)).isoformat(),
                error_code[:64],
                now.isoformat(),
                outbox_id,
                owner,
            ),
        )
        return cursor.rowcount == 1


def mark_outbox_delivery_unknown(
    outbox_id: int,
    *,
    owner: str,
    error_code: str,
) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE operator_outbox
            SET status = 'delivery_unknown', lease_owner = NULL, lease_until = NULL,
                available_at = NULL, last_error_code = ?, updated_at = ?
            WHERE id = ? AND status = 'dispatching' AND lease_owner = ?
            """,
            (error_code[:64], _now().isoformat(), outbox_id, owner),
        )
        return cursor.rowcount == 1


def recover_expired_outbox(email_id: str) -> dict[str, int]:
    """Recover only locally-verifiable drafts; quarantine external side effects."""
    now = _now().isoformat()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        recovered = conn.execute(
            """
            UPDATE operator_outbox
            SET status = 'pending', lease_owner = NULL, lease_until = NULL,
                available_at = NULL, last_error_code = 'expired_local_claim',
                updated_at = ?
            WHERE aggregate_type = 'email' AND aggregate_id = ?
              AND status = 'dispatching' AND lease_until < ?
              AND topic = 'reply_draft'
            """,
            (now, email_id, now),
        ).rowcount
        quarantined = conn.execute(
            """
            UPDATE operator_outbox
            SET status = 'delivery_unknown', lease_owner = NULL, lease_until = NULL,
                available_at = NULL, last_error_code = 'expired_external_claim',
                updated_at = ?
            WHERE aggregate_type = 'email' AND aggregate_id = ?
              AND status = 'dispatching' AND lease_until < ?
              AND topic != 'reply_draft'
            """,
            (now, email_id, now),
        ).rowcount
        return {"recovered": recovered, "quarantined": quarantined}


def has_retryable_outbox(email_id: str) -> bool:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT 1 FROM operator_outbox
            WHERE aggregate_type = 'email' AND aggregate_id = ?
              AND status IN ('pending', 'dispatching')
            LIMIT 1
            """,
            (email_id,),
        ).fetchone() is not None
