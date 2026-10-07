"""Helpers that bind a final-send event to the draft the user reviewed."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from typing import Any, Optional

from db.approval_store import as_utc, parse_utc


LOCAL_APPROVAL_SOURCE = "local_cli"
SLACK_APPROVAL_SOURCE = "slack"


def send_payload_sha256(payload_json: str) -> str:
    """Return a deterministic digest for the exact stored send payload."""
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def stable_outbound_message_id(
    task_id: int,
    approval_payload_hash: str,
    sender_address: str,
) -> str:
    """Return the same RFC-style Message-ID for the same task and draft.

    Explicit retries deliberately retain this ID. SMTP servers are not required
    to deduplicate it, but it gives operators and providers a stable delivery
    correlation key without storing recipient/body data in the ledger.
    """
    domain = str(sender_address or "").rsplit("@", 1)[-1].lower().strip()
    if not re.fullmatch(r"[a-z0-9.-]+", domain or ""):
        domain = "mcp-mail-assistant.local"
    token = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"mcp-mail-assistant:outbound:{int(task_id)}:{approval_payload_hash}",
    ).hex
    return f"<mail-agent-{token}@{domain}>"


def build_confirmation_payload(
    task_id: int,
    approval_source: str,
    send_payload_json: str,
) -> dict[str, Any]:
    """Build the event payload that proves which draft was approved."""
    return {
        "task_id": int(task_id),
        "approval_source": approval_source,
        "send_payload_sha256": send_payload_sha256(send_payload_json),
    }


def validate_send_confirmation(
    event: dict[str, Any],
    task: dict[str, Any],
    send_payload_json: str,
) -> Optional[str]:
    """Return a failure reason when an event is not valid for this task/draft."""
    task_id = task.get("id") or task.get("task_id")
    if task.get("task_type") != "reply_email":
        return "task is not reply_email"
    if task.get("status") != "pending":
        return f"task status is not pending: {task.get('status')!r}"
    if task.get("approval_status") != "approved":
        return f"task approval is not approved: {task.get('approval_status')!r}"

    payload = event.get("payload") or {}
    source = payload.get("approval_source")
    action = payload.get("approval_action")
    version = int(task.get("approval_version") or 0)
    if version < 1 or payload.get("approval_version") != version:
        return "approval request version mismatch"
    now = as_utc()
    event_expiry = parse_utc(event.get("expires_at"))
    task_expiry = parse_utc(task.get("approval_expires_at"))
    if event_expiry is None or task_expiry is None:
        return "approval expiry is missing"
    if now >= event_expiry or now >= task_expiry:
        return "approval expired"
    source_id = event.get("source_id") or ""
    if source == LOCAL_APPROVAL_SOURCE:
        if action not in {"tasks.approve", "retry_send.confirm"}:
            return "local approval action mismatch"
        expected_prefix = (
            f"local-cli:send_confirmed:task:{task_id}:request:{version}:"
            f"action:{action}:ref:"
        )
        if not source_id.startswith(expected_prefix) or source_id == expected_prefix:
            return "local approval source_id mismatch"
    elif source == SLACK_APPROVAL_SOURCE:
        if action != "reply_send_modal":
            return "Slack approval action mismatch"
        expected_prefix = (
            f"slack:send_confirmed:task:{task_id}:request:{version}:"
            "action:reply_send_modal:ref:"
        )
        if not source_id.startswith(expected_prefix) or source_id == expected_prefix:
            return "Slack approval source_id mismatch"
    else:
        return "unknown approval source"

    approved_digest = payload.get("send_payload_sha256")
    current_digest = send_payload_sha256(send_payload_json)
    if not isinstance(approved_digest, str) or not hmac.compare_digest(
        approved_digest, current_digest
    ):
        return "approved draft no longer matches stored send payload"
    return None
