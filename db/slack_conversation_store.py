"""Durable, server-authoritative Slack conversation request state."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from db.connection import get_connection
from db.event_store import EventStatus, EventType


_SLACK_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
MAX_REQUEST_TEXT = 2000


class SlackInteractionAuthorityError(RuntimeError):
    """A persisted Slack interaction is invalid, expired, or inconsistent."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def interaction_ttl_seconds() -> int:
    raw = os.environ.get("SLACK_INTERACTION_TTL_SECONDS", "300")
    try:
        seconds = int(raw)
    except (TypeError, ValueError) as exc:
        raise SlackInteractionAuthorityError(
            "SLACK_INTERACTION_TTL_SECONDS must be an integer"
        ) from exc
    if seconds < 30 or seconds > 3600:
        raise SlackInteractionAuthorityError(
            "SLACK_INTERACTION_TTL_SECONDS must be between 30 and 3600"
        )
    return seconds


def context_ttl_seconds() -> int:
    raw = os.environ.get("SLACK_CONTEXT_TTL_SECONDS", "86400")
    try:
        seconds = int(raw)
    except (TypeError, ValueError) as exc:
        raise SlackInteractionAuthorityError(
            "SLACK_CONTEXT_TTL_SECONDS must be an integer"
        ) from exc
    if seconds < 300 or seconds > 2592000:
        raise SlackInteractionAuthorityError(
            "SLACK_CONTEXT_TTL_SECONDS must be between 300 and 2592000"
        )
    return seconds


def _require_id(name: str, value: object) -> str:
    candidate = value if isinstance(value, str) else ""
    if not _SLACK_ID.fullmatch(candidate):
        raise SlackInteractionAuthorityError(f"invalid Slack {name}")
    return candidate


def canonical_request_sha256(
    *,
    slack_event_id: str,
    actor_id: str,
    channel_id: str,
    thread_ts: str,
    message_ts: str,
    request_text: str,
) -> str:
    encoded = json.dumps(
        {
            "slack_event_id": slack_event_id,
            "actor_id": actor_id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "message_ts": message_ts,
            "request_text": request_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_interaction_event(
    *,
    slack_event_id: object,
    actor_id: object,
    channel_id: object,
    thread_ts: object,
    message_ts: object,
    request_text: object,
    now: datetime | None = None,
) -> tuple[str, str, bool]:
    """Atomically persist one Slack request and its idempotent queue event."""
    slack_event = _require_id("event id", slack_event_id)
    actor = _require_id("actor id", actor_id)
    channel = _require_id("channel id", channel_id)
    thread = _require_id("thread timestamp", thread_ts)
    message = _require_id("message timestamp", message_ts)
    text = request_text.strip() if isinstance(request_text, str) else ""
    if not text or len(text) > MAX_REQUEST_TEXT:
        raise SlackInteractionAuthorityError("Slack request text is empty or too long")

    current = (now or _now()).astimezone(timezone.utc)
    expires = current + timedelta(seconds=interaction_ttl_seconds())
    digest = canonical_request_sha256(
        slack_event_id=slack_event,
        actor_id=actor,
        channel_id=channel,
        thread_ts=thread,
        message_ts=message,
        request_text=text,
    )
    interaction_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"mcp-mail-assistant:slack-interaction:{slack_event}")
    )
    source_id = f"slack-conversation:{slack_event}"
    event_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"mcp-mail-assistant:event:{EventType.SLACK_CONVERSATION}:source:{source_id}",
        )
    )
    timestamp = _iso(current)
    expiry = _iso(expires)

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, request_sha256 FROM slack_interactions WHERE slack_event_id = ?",
            (slack_event,),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["request_sha256"]), digest):
                raise SlackInteractionAuthorityError(
                    "Slack event id was reused with different request data"
                )
            event = conn.execute(
                "SELECT id, event_type FROM events WHERE source_id = ?", (source_id,)
            ).fetchone()
            if event is None or event["event_type"] != EventType.SLACK_CONVERSATION:
                raise SlackInteractionAuthorityError(
                    "Slack interaction event state is inconsistent"
                )
            return str(event["id"]), str(existing["id"]), False

        conn.execute(
            """
            INSERT INTO slack_interactions (
                id, slack_event_id, actor_id, channel_id, thread_ts,
                message_ts, request_text, request_sha256, status,
                expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                interaction_id,
                slack_event,
                actor,
                channel,
                thread,
                message,
                text,
                digest,
                expiry,
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO events (
                id, event_type, payload_json, status, source_id, max_attempts,
                expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 3, ?, ?, ?)
            """,
            (
                event_id,
                EventType.SLACK_CONVERSATION,
                json.dumps({"interaction_id": interaction_id}),
                EventStatus.PENDING,
                source_id,
                expiry,
                timestamp,
                timestamp,
            ),
        )
        return event_id, interaction_id, True


def load_authoritative_interaction(
    event_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reload and verify the DB request; the queue payload is only a locator."""
    current = (now or _now()).astimezone(timezone.utc)
    with get_connection() as conn:
        event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if event is None or event["event_type"] != EventType.SLACK_CONVERSATION:
            raise SlackInteractionAuthorityError("Slack conversation event not found")
        if event["status"] not in {EventStatus.PENDING, EventStatus.LEASED}:
            raise SlackInteractionAuthorityError("Slack conversation event is not active")
        try:
            locator = json.loads(event["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SlackInteractionAuthorityError("Slack event locator is invalid") from exc
        interaction = conn.execute(
            "SELECT * FROM slack_interactions WHERE id = ?",
            (locator.get("interaction_id"),),
        ).fetchone()
        if interaction is None:
            raise SlackInteractionAuthorityError("Slack interaction not found")
        item = dict(interaction)
        if event["source_id"] != f"slack-conversation:{item['slack_event_id']}":
            raise SlackInteractionAuthorityError("Slack event source is inconsistent")
        expiry = _parse(item.get("expires_at"))
        event_expiry = _parse(event["expires_at"])
        if expiry is None or event_expiry is None or event_expiry != expiry:
            raise SlackInteractionAuthorityError("Slack interaction expiry is invalid")
        if current >= expiry:
            conn.execute(
                "UPDATE slack_interactions SET status='expired', updated_at=? WHERE id=?",
                (_iso(current), item["id"]),
            )
            raise SlackInteractionAuthorityError("Slack interaction expired")
        digest = canonical_request_sha256(
            slack_event_id=item["slack_event_id"],
            actor_id=item["actor_id"],
            channel_id=item["channel_id"],
            thread_ts=item["thread_ts"],
            message_ts=item["message_ts"],
            request_text=item["request_text"],
        )
        if not hmac.compare_digest(digest, str(item["request_sha256"])):
            raise SlackInteractionAuthorityError("Slack interaction payload changed")
        if item["status"] not in {"pending", "processed"}:
            raise SlackInteractionAuthorityError("Slack interaction is not processable")
        return item


def save_response(
    interaction_id: str,
    *,
    response_text: str,
    response_kind: str = "message",
    response_task_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if response_kind not in {"message", "draft_with_confirmation"}:
        raise ValueError("invalid Slack response kind")
    current = (now or _now()).astimezone(timezone.utc)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM slack_interactions WHERE id = ?", (interaction_id,)
        ).fetchone()
        if row is None:
            raise SlackInteractionAuthorityError("Slack interaction not found")
        existing = dict(row)
        if existing["status"] == "processed":
            return existing
        if existing["status"] != "pending":
            raise SlackInteractionAuthorityError("Slack interaction is not pending")
        timestamp = _iso(current)
        conn.execute(
            """
            UPDATE slack_interactions
            SET status='processed', response_text=?, response_kind=?,
                response_task_id=?, processed_at=?, updated_at=?
            WHERE id=?
            """,
            (
                response_text,
                response_kind,
                response_task_id,
                timestamp,
                timestamp,
                interaction_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM slack_interactions WHERE id = ?", (interaction_id,)
        ).fetchone()
        return dict(updated)


def get_active_task(channel_id: str, thread_ts: str, *, now: datetime | None = None) -> int | None:
    current = (now or _now()).astimezone(timezone.utc)
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT active_task_id, expires_at FROM slack_conversation_contexts
            WHERE channel_id = ? AND thread_ts = ?
            """,
            (channel_id, thread_ts),
        ).fetchone()
        if row is None:
            return None
        expiry = _parse(row["expires_at"])
        if expiry is None or current >= expiry:
            conn.execute(
                "DELETE FROM slack_conversation_contexts WHERE channel_id=? AND thread_ts=?",
                (channel_id, thread_ts),
            )
            return None
        return int(row["active_task_id"]) if row["active_task_id"] is not None else None


def set_active_task(
    channel_id: str,
    thread_ts: str,
    task_id: int,
    *,
    now: datetime | None = None,
) -> None:
    current = (now or _now()).astimezone(timezone.utc)
    expiry = current + timedelta(seconds=context_ttl_seconds())
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO slack_conversation_contexts (
                channel_id, thread_ts, active_task_id, expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                active_task_id=excluded.active_task_id,
                expires_at=excluded.expires_at,
                updated_at=excluded.updated_at
            """,
            (channel_id, thread_ts, task_id, _iso(expiry), _iso(current)),
        )


__all__ = [
    "MAX_REQUEST_TEXT",
    "SlackInteractionAuthorityError",
    "canonical_request_sha256",
    "context_ttl_seconds",
    "create_interaction_event",
    "get_active_task",
    "interaction_ttl_seconds",
    "load_authoritative_interaction",
    "save_response",
    "set_active_task",
]
