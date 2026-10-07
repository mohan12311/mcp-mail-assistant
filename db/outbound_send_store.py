"""Durable SQLite ledger for approval-bound SMTP side effects.

SQLite cannot participate in the SMTP transaction. The safe recovery rule is
therefore conservative: a ledger row observed in ``sending`` is quarantined as
``delivery_unknown`` and is never automatically claimed again.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from db.connection import get_connection
from db.approval_store import (
    ApprovalAuthorityError,
    SEND_CONFIRMED,
    create_retry_send_confirmation,
    iso_utc,
    validate_stored_event,
)
from db.task_state import TaskStateError, transition_task

logger = logging.getLogger(__name__)

CLAIMED = "claimed"
TERMINAL = "terminal"
QUARANTINED = "quarantined"
REQUIRES_CONFIRMATION = "requires_confirmation"
REJECTED = "rejected"

TERMINAL_LEDGER_STATES = frozenset({"sent", "send_failed", "delivery_unknown"})
RETRYABLE_TASK_STATES = frozenset({"send_failed", "delivery_unknown"})


class OutboundLedgerConflict(RuntimeError):
    """Raised when a finalize operation no longer owns a sending row."""


def _now() -> str:
    return iso_utc()


def _safe_error(value: object) -> str:
    return " ".join(str(value).split())[:1000]


def get_ledger_entry(ledger_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM outbound_send_ledger WHERE id = ?", (ledger_id,)
        ).fetchone()
        return dict(row) if row else None


def list_ledger_for_task(task_id: int) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM outbound_send_ledger
            WHERE task_id = ?
            ORDER BY id ASC
            """,
            (task_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def resolve_outbound_message_id(
    *,
    task_id: int,
    approval_payload_hash: str,
    fallback_message_id: str,
) -> str:
    """Reuse the first ledger Message-ID for every explicit retry of a draft."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT outbound_message_id FROM outbound_send_ledger
            WHERE task_id = ? AND approval_payload_hash = ?
            ORDER BY id ASC LIMIT 1
            """,
            (task_id, approval_payload_hash),
        ).fetchone()
        return str(row["outbound_message_id"]) if row else fallback_message_id


def _quarantine_sending(
    conn: Any,
    *,
    task_id: int,
    ledger_id: int,
    reason: str,
) -> None:
    now = _now()
    conn.execute(
        """
        UPDATE outbound_send_ledger
        SET status = 'delivery_unknown',
            uncertainty_reason = ?,
            finalized_at = ?,
            updated_at = ?
        WHERE id = ? AND status = 'sending'
        """,
        (_safe_error(reason), now, now, ledger_id),
    )
    transition_task(
        conn,
        task_id,
        send_state="delivery_unknown",
        fields={
            "send_claimed_at": None,
            "send_claim_event_id": None,
            "execution_result": "SMTP delivery result is unknown; explicit review is required",
        },
    )


def claim_outbound_send(
    *,
    task_id: int,
    event_id: str,
    approval_payload_hash: str,
    outbound_message_id: str,
) -> dict[str, Any]:
    """Atomically claim one approved attempt before any SMTP call.

    An existing ``sending`` row, including one owned by the same re-leased
    event, is evidence of an interrupted/overlapping SMTP attempt. It is moved
    to ``delivery_unknown`` instead of being sent again.
    """
    if not event_id or not approval_payload_hash or not outbound_message_id:
        raise ValueError("event id, approval hash, and Message-ID are required")

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task_row:
            return {"disposition": REJECTED, "reason": "task_not_found"}
        task = dict(task_row)

        existing = conn.execute(
            "SELECT * FROM outbound_send_ledger WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing:
            existing = dict(existing)
            if existing["status"] == "sending":
                _quarantine_sending(
                    conn,
                    task_id=task_id,
                    ledger_id=int(existing["id"]),
                    reason="sending attempt was observed again after claim",
                )
                return {
                    "disposition": QUARANTINED,
                    "ledger_id": int(existing["id"]),
                    "status": "delivery_unknown",
                }
            return {
                "disposition": TERMINAL,
                "ledger_id": int(existing["id"]),
                "status": existing["status"],
            }

        if task.get("send_state") == "sent" or task.get("status") == "completed":
            return {"disposition": TERMINAL, "status": "sent"}

        active = conn.execute(
            """
            SELECT id FROM outbound_send_ledger
            WHERE task_id = ? AND status = 'sending'
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if active:
            _quarantine_sending(
                conn,
                task_id=task_id,
                ledger_id=int(active["id"]),
                reason="overlapping event observed an in-flight SMTP attempt",
            )
            return {
                "disposition": QUARANTINED,
                "ledger_id": int(active["id"]),
                "status": "delivery_unknown",
            }

        if task.get("send_state") == "sending":
            # Compatibility recovery for a pre-ledger claim. There is no safe
            # evidence that SMTP was not reached, so synthesize a quarantined
            # ledger row instead of leaving the task stuck or re-sending it.
            now = _now()
            cursor = conn.execute(
                """
                INSERT INTO outbound_send_ledger (
                    task_id, event_id, approval_payload_hash,
                    outbound_message_id, status, claimed_at, finalized_at,
                    uncertainty_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'delivery_unknown', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    event_id,
                    approval_payload_hash,
                    outbound_message_id,
                    now,
                    now,
                    "pre-ledger sending state recovered conservatively",
                    now,
                    now,
                ),
            )
            transition_task(
                conn,
                task_id,
                send_state="delivery_unknown",
                fields={
                    "send_claimed_at": None,
                    "send_claim_event_id": None,
                    "execution_result": "SMTP delivery result is unknown; explicit review is required",
                },
            )
            return {
                "disposition": QUARANTINED,
                "ledger_id": int(cursor.lastrowid),
                "status": "delivery_unknown",
            }

        if task.get("send_state") in RETRYABLE_TASK_STATES:
            return {
                "disposition": REQUIRES_CONFIRMATION,
                "status": task.get("send_state"),
            }
        if (
            task.get("task_type") != "reply_email"
            or task.get("status") != "pending"
            or task.get("approval_status") != "approved"
            or task.get("send_state") != "pending_send_confirmation"
        ):
            return {
                "disposition": REJECTED,
                "reason": "task_not_claimable",
                "status": task.get("send_state"),
            }

        try:
            authoritative_event = validate_stored_event(
                conn,
                event_id=event_id,
                task=task,
                expected_type=SEND_CONFIRMED,
            )
        except (ApprovalAuthorityError, TaskStateError) as exc:
            return {
                "disposition": REJECTED,
                "reason": str(exc),
                "status": task.get("send_state"),
            }
        approved_hash = authoritative_event["payload"].get("send_payload_sha256")
        current_hash = hashlib.sha256(
            (task.get("send_payload") or "").encode("utf-8")
        ).hexdigest()
        task_hash = task.get("approval_payload_sha256")
        if not (
            isinstance(approved_hash, str)
            and isinstance(task_hash, str)
            and hmac.compare_digest(approved_hash, approval_payload_hash)
            and hmac.compare_digest(approved_hash, current_hash)
            and hmac.compare_digest(approved_hash, task_hash)
        ):
            return {
                "disposition": REJECTED,
                "reason": "approval payload changed",
                "status": task.get("send_state"),
            }

        now = _now()
        cursor = conn.execute(
            """
            INSERT INTO outbound_send_ledger (
                task_id, event_id, approval_payload_hash,
                outbound_message_id, status, claimed_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'sending', ?, ?, ?)
            """,
            (
                task_id,
                event_id,
                approval_payload_hash,
                outbound_message_id,
                now,
                now,
                now,
            ),
        )
        transition_task(
            conn,
            task_id,
            send_state="sending",
            fields={"send_claimed_at": now, "send_claim_event_id": event_id},
        )
        return {
            "disposition": CLAIMED,
            "ledger_id": int(cursor.lastrowid),
            "status": "sending",
            "outbound_message_id": outbound_message_id,
        }


def finalize_outbound_send(
    ledger_id: int,
    *,
    execution_result: str,
) -> None:
    """Record ledger=sent and task=sent/completed in one SQLite transaction."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT task_id, status FROM outbound_send_ledger WHERE id = ?",
            (ledger_id,),
        ).fetchone()
        if not row or row["status"] != "sending":
            raise OutboundLedgerConflict("outbound ledger is no longer sending")
        now = _now()
        ledger_update = conn.execute(
            """
            UPDATE outbound_send_ledger
            SET status = 'sent', smtp_completed_at = ?, finalized_at = ?,
                last_error = NULL, uncertainty_reason = NULL, updated_at = ?
            WHERE id = ? AND status = 'sending'
            """,
            (now, now, now, ledger_id),
        )
        transition_task(
            conn,
            int(row["task_id"]),
            status="completed",
            send_state="sent",
            fields={
                "executed_at": now,
                "execution_result": execution_result,
                "send_claimed_at": None,
                "send_claim_event_id": None,
            },
        )
        if ledger_update.rowcount != 1:
            raise OutboundLedgerConflict("task changed while finalizing outbound send")


def mark_send_failed(ledger_id: int, error: object) -> bool:
    """Atomically record a known non-delivery failure."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT task_id FROM outbound_send_ledger WHERE id = ? AND status = 'sending'",
            (ledger_id,),
        ).fetchone()
        if not row:
            return False
        now = _now()
        conn.execute(
            """
            UPDATE outbound_send_ledger
            SET status = 'send_failed', last_error = ?, finalized_at = ?, updated_at = ?
            WHERE id = ? AND status = 'sending'
            """,
            (_safe_error(error), now, now, ledger_id),
        )
        transition_task(
            conn,
            int(row["task_id"]),
            send_state="send_failed",
            fields={
                "send_claimed_at": None,
                "send_claim_event_id": None,
                "execution_result": "SMTP send failed; explicit review is required",
            },
        )
        return True


def mark_delivery_unknown(
    ledger_id: int,
    reason: object,
    *,
    smtp_returned_success: bool = False,
) -> bool:
    """Quarantine an attempt whose SMTP acceptance cannot be proven locally."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT task_id, status FROM outbound_send_ledger WHERE id = ?",
            (ledger_id,),
        ).fetchone()
        if not row or row["status"] not in {"sending", "delivery_unknown"}:
            return False
        if row["status"] == "sending":
            _quarantine_sending(
                conn,
                task_id=int(row["task_id"]),
                ledger_id=ledger_id,
                reason=_safe_error(reason),
            )
        if smtp_returned_success:
            now = _now()
            conn.execute(
                """
                UPDATE outbound_send_ledger
                SET smtp_completed_at = COALESCE(smtp_completed_at, ?),
                    updated_at = ?
                WHERE id = ? AND status = 'delivery_unknown'
                """,
                (now, now, ledger_id),
            )
        return True


def create_retry_confirmation(
    *,
    task_id: int,
    approval_payload_hash: str,
    approval_source: str,
) -> tuple[str, str]:
    """Re-arm a failed/unknown task and enqueue a new explicit approval atomically."""
    if approval_source != "local_cli":
        raise OutboundLedgerConflict("retry approval source is not authorized")
    try:
        event_id, confirmation_id = create_retry_send_confirmation(
            task_id=task_id,
            expected_payload_hash=approval_payload_hash,
        )
    except ApprovalAuthorityError as exc:
        raise OutboundLedgerConflict(str(exc)) from exc
    logger.info("새 발송 재확인 이벤트 생성: task=%s event=%s", task_id, event_id)
    return event_id, confirmation_id
