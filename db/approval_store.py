"""Server-authoritative approval requests, decisions, and event validation."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from db.connection import get_connection
from db.task_state import TaskStateError, transition_task


LOCAL_CLI = "local_cli"
LOCAL_MCP = "local_mcp"
SLACK = "slack"
SEND_CONFIRMED = "send_confirmed"

_SOURCE_REF = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_SOURCE_ACTIONS = {
    LOCAL_CLI: frozenset({"tasks.approve", "retry_send.confirm"}),
    LOCAL_MCP: frozenset({"task_approve", "task_deny"}),
    SLACK: frozenset(
        {"task_approve", "task_deny", "reply_send", "reply_send_modal"}
    ),
}


class ApprovalAuthorityError(RuntimeError):
    """An approval request/event has no current server authority."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None = None) -> datetime:
    current = value or utc_now()
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return as_utc(value).isoformat()


def parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(parsed)


def approval_ttl_seconds() -> int:
    raw = os.environ.get("APPROVAL_TTL_SECONDS", "900")
    try:
        seconds = int(raw)
    except (TypeError, ValueError) as exc:
        raise ApprovalAuthorityError("APPROVAL_TTL_SECONDS must be an integer") from exc
    if seconds < 30 or seconds > 86400:
        raise ApprovalAuthorityError(
            "APPROVAL_TTL_SECONDS must be between 30 and 86400"
        )
    return seconds


def payload_sha256(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _active_request_error(
    task: dict[str, Any],
    *,
    now: datetime,
    require_pending: bool,
) -> str | None:
    if task.get("status") != "pending":
        return "task is not pending"
    expected_status = "pending" if require_pending else "approved"
    if task.get("approval_status") != expected_status:
        return "approval status has no current authority"
    if int(task.get("approval_version") or 0) < 1:
        return "legacy approval requires a new review"
    requested_at = parse_utc(task.get("approval_requested_at"))
    expires_at = parse_utc(task.get("approval_expires_at"))
    if requested_at is None or expires_at is None:
        return "approval window is missing"
    if now >= expires_at:
        return "approval request expired"
    return None


def _expire_task(conn: sqlite3.Connection, task: dict[str, Any]) -> None:
    if task.get("approval_status") not in {"pending", "approved"}:
        return
    send_state = task.get("send_state")
    target_send: str | None | object = ...
    if send_state == "pending_send_confirmation":
        target_send = "draft_generated"
    transition_task(
        conn,
        int(task["id"]),
        approval_status="expired",
        send_state=target_send,
        fields={"approval_source": None, "approved_at": None},
    )


def assert_active_request(
    task: dict[str, Any],
    *,
    now: datetime | None = None,
    require_pending: bool = True,
    payload_hash: str | None = None,
) -> None:
    current = as_utc(now)
    error = _active_request_error(
        task, now=current, require_pending=require_pending
    )
    if error:
        raise ApprovalAuthorityError(error)
    if payload_hash is not None:
        stored_hash = task.get("approval_payload_sha256")
        if not isinstance(stored_hash, str) or not hmac.compare_digest(
            stored_hash, payload_hash
        ):
            raise ApprovalAuthorityError("approval payload changed")


def request_approval(
    task_id: int,
    *,
    payload_hash: str | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
    send_state: str | None | object = ...,
) -> dict[str, Any]:
    """Create a new bounded approval generation for one pending task."""
    owns_connection = conn is None
    context = get_connection() if owns_connection else None
    db = context.__enter__() if context is not None else conn
    assert db is not None
    try:
        if owns_connection:
            db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ApprovalAuthorityError("task not found")
        task = dict(row)
        if task.get("status") != "pending":
            raise ApprovalAuthorityError("only pending tasks can request approval")
        if task.get("approval_status") in {"denied", "cancelled"}:
            raise ApprovalAuthorityError("terminal approval cannot be reopened")
        if task.get("send_state") in {"sending", "sent", "cancelled"}:
            raise ApprovalAuthorityError("send state cannot request approval")
        seconds = approval_ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
        if seconds < 30 or seconds > 86400:
            raise ApprovalAuthorityError("approval ttl is outside the safe range")
        requested = as_utc(now)
        expires = requested + timedelta(seconds=seconds)
        target = transition_task(
            db,
            task_id,
            approval_status="pending",
            send_state=send_state,
            fields={
                "approval_requested_at": iso_utc(requested),
                "approval_expires_at": iso_utc(expires),
                "approval_payload_sha256": payload_hash,
                "approval_source": None,
                "approval_version": int(task.get("approval_version") or 0) + 1,
                "approved_at": None,
            },
        )
        return target
    except Exception:
        if owns_connection:
            db.rollback()
        raise
    finally:
        if context is not None:
            context.__exit__(None, None, None)


def _validate_source(source: str, action: str, source_ref: str) -> None:
    if source not in _SOURCE_ACTIONS or action not in _SOURCE_ACTIONS[source]:
        raise ApprovalAuthorityError("invalid approval source action")
    if not _SOURCE_REF.fullmatch(source_ref or ""):
        raise ApprovalAuthorityError("invalid approval source reference")


def _source_id(
    *,
    event_type: str,
    task_id: int,
    version: int,
    source: str,
    action: str,
    source_ref: str,
) -> str:
    prefix = {LOCAL_CLI: "local-cli", LOCAL_MCP: "local-mcp", SLACK: "slack"}[source]
    return (
        f"{prefix}:{event_type}:task:{task_id}:request:{version}:"
        f"action:{action}:ref:{source_ref}"
    )


def _insert_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    source_id: str,
    payload: dict[str, Any],
    expires_at: str,
    now: datetime,
) -> tuple[str, bool]:
    event_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"mcp-mail-assistant:event:{event_type}:source:{source_id}",
        )
    )
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO events (
            id, event_type, payload_json, status, source_id, max_attempts,
            expires_at, created_at, updated_at
        ) VALUES (?, ?, ?, 'pending', ?, 3, ?, ?, ?)
        """,
        (
            event_id,
            event_type,
            json.dumps(payload, ensure_ascii=False),
            source_id,
            expires_at,
            iso_utc(now),
            iso_utc(now),
        ),
    )
    return event_id, cursor.rowcount == 1


def create_send_confirmation(
    *,
    task_id: int,
    source: str,
    action: str,
    source_ref: str,
    expected_payload_hash: str | None = None,
    expected_approval_version: int | None = None,
    allowed_send_states: frozenset[str] = frozenset(
        {"draft_generated", "pending_send_confirmation"}
    ),
    now: datetime | None = None,
) -> tuple[str, bool]:
    """Atomically consume one active review and queue its bounded event."""
    _validate_source(source, action, source_ref)
    if not (
        (source == LOCAL_CLI and action in {"tasks.approve", "retry_send.confirm"})
        or (source == SLACK and action == "reply_send_modal")
    ):
        raise ApprovalAuthorityError("source cannot authorize SMTP confirmation")
    current = as_utc(now)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ApprovalAuthorityError("task not found")
        task = dict(row)
        raw_payload = task.get("send_payload")
        if task.get("task_type") != "reply_email" or not isinstance(raw_payload, str):
            raise ApprovalAuthorityError("task has no reviewable reply payload")
        current_hash = payload_sha256(raw_payload)
        if expected_payload_hash is not None and not hmac.compare_digest(
            expected_payload_hash, current_hash
        ):
            raise ApprovalAuthorityError("reviewed payload changed")
        version = int(task.get("approval_version") or 0)
        if (
            expected_approval_version is not None
            and version != int(expected_approval_version)
        ):
            raise ApprovalAuthorityError("reviewed approval request changed")
        source_id = _source_id(
            event_type=SEND_CONFIRMED,
            task_id=task_id,
            version=version,
            source=source,
            action=action,
            source_ref=source_ref,
        )
        existing = conn.execute(
            "SELECT id, expires_at FROM events WHERE source_id = ? AND event_type = ?",
            (source_id, SEND_CONFIRMED),
        ).fetchone()
        if existing is not None and task.get("approval_status") == "approved":
            error = _active_request_error(task, now=current, require_pending=False)
            event_expiry = parse_utc(existing["expires_at"])
            if error or event_expiry is None or current >= event_expiry:
                _expire_task(conn, task)
                conn.commit()
                raise ApprovalAuthorityError(error or "approval event expired")
            return str(existing["id"]), False

        error = _active_request_error(task, now=current, require_pending=True)
        if error:
            if error == "approval request expired":
                _expire_task(conn, task)
                conn.commit()
            raise ApprovalAuthorityError(error)
        assert_active_request(task, now=current, payload_hash=current_hash)
        if task.get("send_state") not in allowed_send_states:
            raise ApprovalAuthorityError("send state is not approvable")

        version = int(task["approval_version"])
        event_payload = {
            "task_id": task_id,
            "approval_source": source,
            "approval_action": action,
            "approval_version": version,
            "send_payload_sha256": current_hash,
        }
        event_id, created = _insert_event(
            conn,
            event_type=SEND_CONFIRMED,
            source_id=source_id,
            payload=event_payload,
            expires_at=str(task["approval_expires_at"]),
            now=current,
        )
        if not created:
            return event_id, False
        transition_task(
            conn,
            task_id,
            approval_status="approved",
            send_state="pending_send_confirmation",
            fields={"approval_source": source, "approved_at": iso_utc(current)},
        )
        return event_id, True


def create_approval_decision(
    *,
    task_id: int,
    approved: bool,
    source: str,
    action: str,
    source_ref: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> tuple[str, bool]:
    """Atomically resolve a non-SMTP approval request and queue its event."""
    _validate_source(source, action, source_ref)
    if source not in {LOCAL_MCP, SLACK}:
        raise ApprovalAuthorityError("source cannot authorize a task decision")
    expected_action = "task_approve" if approved else "task_deny"
    if action != expected_action:
        raise ApprovalAuthorityError("approval decision action mismatch")
    current = as_utc(now)
    event_type = "approval_granted" if approved else "approval_denied"
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ApprovalAuthorityError("task not found")
        task = dict(row)
        version = int(task.get("approval_version") or 0)
        source_id = _source_id(
            event_type=event_type,
            task_id=task_id,
            version=version,
            source=source,
            action=action,
            source_ref=source_ref,
        )
        existing = conn.execute(
            "SELECT id, expires_at FROM events WHERE source_id = ? AND event_type = ?",
            (source_id, event_type),
        ).fetchone()
        resolved_status = "approved" if approved else "denied"
        if existing is not None and task.get("approval_status") == resolved_status:
            event_expiry = parse_utc(existing["expires_at"])
            if event_expiry is None or current >= event_expiry:
                raise ApprovalAuthorityError("approval event expired")
            return str(existing["id"]), False
        error = _active_request_error(task, now=current, require_pending=True)
        if error:
            if error == "approval request expired":
                _expire_task(conn, task)
                conn.commit()
            raise ApprovalAuthorityError(error)
        version = int(task["approval_version"])
        payload = {
            "task_id": task_id,
            "approval_source": source,
            "approval_action": action,
            "approval_version": version,
            "decision": "approved" if approved else "denied",
        }
        if reason:
            payload["reason"] = "user_denied"
        event_id, created = _insert_event(
            conn,
            event_type=event_type,
            source_id=source_id,
            payload=payload,
            expires_at=str(task["approval_expires_at"]),
            now=current,
        )
        if not created:
            return event_id, False
        fields = {"approval_source": source}
        if approved:
            fields["approved_at"] = iso_utc(current)
            transition_task(
                conn, task_id, approval_status="approved", fields=fields
            )
        else:
            target_send: str | None | object = ...
            if task.get("send_state") not in {None, "sent", "cancelled"}:
                target_send = "cancelled"
            transition_task(
                conn,
                task_id,
                status="cancelled",
                approval_status="denied",
                send_state=target_send,
                fields={
                    **fields,
                    "execution_result": "Denied by an authorized approval path",
                },
            )
        return event_id, True


def create_retry_send_confirmation(
    *,
    task_id: int,
    expected_payload_hash: str,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Atomically reissue and approve a failed/unknown send request."""
    current = as_utc(now)
    confirmation_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ApprovalAuthorityError("task not found")
        task = dict(row)
        raw_payload = task.get("send_payload")
        if (
            task.get("task_type") != "reply_email"
            or task.get("status") != "pending"
            or task.get("approval_status") not in {"pending", "approved", "expired"}
            or task.get("send_state") not in {"send_failed", "delivery_unknown"}
            or not isinstance(raw_payload, str)
        ):
            raise ApprovalAuthorityError("task is not explicitly retryable")
        current_hash = payload_sha256(raw_payload)
        if not hmac.compare_digest(current_hash, expected_payload_hash):
            raise ApprovalAuthorityError("reviewed payload changed")
        task = request_approval(
            task_id,
            payload_hash=current_hash,
            now=current,
            conn=conn,
        )
        version = int(task["approval_version"])
        source_id = _source_id(
            event_type=SEND_CONFIRMED,
            task_id=task_id,
            version=version,
            source=LOCAL_CLI,
            action="retry_send.confirm",
            source_ref=confirmation_id,
        )
        payload = {
            "task_id": task_id,
            "approval_source": LOCAL_CLI,
            "approval_action": "retry_send.confirm",
            "approval_version": version,
            "send_payload_sha256": current_hash,
        }
        event_id, created = _insert_event(
            conn,
            event_type=SEND_CONFIRMED,
            source_id=source_id,
            payload=payload,
            expires_at=str(task["approval_expires_at"]),
            now=current,
        )
        if not created:
            raise ApprovalAuthorityError("retry confirmation collision")
        transition_task(
            conn,
            task_id,
            approval_status="approved",
            send_state="pending_send_confirmation",
            fields={
                "approval_source": LOCAL_CLI,
                "approved_at": iso_utc(current),
                "send_claimed_at": None,
                "send_claim_event_id": None,
            },
        )
        return event_id, confirmation_id


def validate_stored_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    task: dict[str, Any],
    expected_type: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reload and validate the authoritative event row inside a DB transaction."""
    current = as_utc(now)
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise ApprovalAuthorityError("approval event not found")
    event = dict(row)
    if event.get("event_type") != expected_type:
        raise ApprovalAuthorityError("approval event type mismatch")
    if event.get("status") not in {"pending", "leased"}:
        raise ApprovalAuthorityError("approval event is not active")
    event_expiry = parse_utc(event.get("expires_at"))
    task_expiry = parse_utc(task.get("approval_expires_at"))
    if event_expiry is None or task_expiry is None:
        raise ApprovalAuthorityError("legacy approval event requires a new review")
    if current >= event_expiry or current >= task_expiry:
        _expire_task(conn, task)
        raise ApprovalAuthorityError("approval event expired")
    if event_expiry > task_expiry:
        raise ApprovalAuthorityError("approval event exceeds request expiry")
    try:
        payload = json.loads(event["payload_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ApprovalAuthorityError("approval event payload is invalid") from exc
    if payload.get("task_id") != int(task["id"]):
        raise ApprovalAuthorityError("approval event task mismatch")
    version = int(task.get("approval_version") or 0)
    if version < 1 or payload.get("approval_version") != version:
        raise ApprovalAuthorityError("approval event request version mismatch")
    source = payload.get("approval_source")
    action = payload.get("approval_action")
    source_id = event.get("source_id")
    if source not in _SOURCE_ACTIONS or action not in _SOURCE_ACTIONS[source]:
        raise ApprovalAuthorityError("approval event source is invalid")
    if task.get("approval_source") != source:
        raise ApprovalAuthorityError("approval event source does not own the task")
    if expected_type == SEND_CONFIRMED:
        if not (
            (source == LOCAL_CLI and action in {"tasks.approve", "retry_send.confirm"})
            or (source == SLACK and action == "reply_send_modal")
        ):
            raise ApprovalAuthorityError("approval event action cannot authorize SMTP")
    elif expected_type == "approval_granted" and action != "task_approve":
        raise ApprovalAuthorityError("approval grant action mismatch")
    elif expected_type == "approval_denied" and action != "task_deny":
        raise ApprovalAuthorityError("approval denial action mismatch")
    marker = (
        f":{expected_type}:task:{task['id']}:request:{version}:"
        f"action:{action}:ref:"
    )
    prefix = {LOCAL_CLI: "local-cli", LOCAL_MCP: "local-mcp", SLACK: "slack"}[source]
    if not isinstance(source_id, str) or not source_id.startswith(prefix + marker):
        raise ApprovalAuthorityError("approval event source state mismatch")
    source_ref = source_id.split(":ref:", 1)[-1]
    if not _SOURCE_REF.fullmatch(source_ref):
        raise ApprovalAuthorityError("approval event source reference is invalid")
    event["payload"] = payload
    return event


__all__ = [
    "ApprovalAuthorityError",
    "LOCAL_CLI",
    "LOCAL_MCP",
    "SLACK",
    "approval_ttl_seconds",
    "assert_active_request",
    "create_approval_decision",
    "create_retry_send_confirmation",
    "create_send_confirmation",
    "iso_utc",
    "payload_sha256",
    "request_approval",
    "validate_stored_event",
]
