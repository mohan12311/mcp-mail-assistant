"""Durable, user-scoped behavior rules for email triage and reply drafting."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any

from db.connection import get_connection


RULE_KINDS = {"processing_exclusion", "reply_style"}
SCOPE_TYPES = {
    "default",
    "sender_email",
    "sender_domain",
    "sender_name",
    "subject_contains",
}
_IDENTITY = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_SPECIFICITY = {
    "default": 0,
    "subject_contains": 1,
    "sender_name": 2,
    "sender_domain": 3,
    "sender_email": 4,
}


class PreferenceAuthorityError(RuntimeError):
    """A preference proposal or mutation failed its authority checks."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ttl_seconds() -> int:
    try:
        seconds = int(os.environ.get("PREFERENCE_CONFIRMATION_TTL_SECONDS", "900"))
    except (TypeError, ValueError) as exc:
        raise PreferenceAuthorityError(
            "PREFERENCE_CONFIRMATION_TTL_SECONDS must be an integer"
        ) from exc
    if not 60 <= seconds <= 86400:
        raise PreferenceAuthorityError(
            "PREFERENCE_CONFIRMATION_TTL_SECONDS must be between 60 and 86400"
        )
    return seconds


def _environment_scope() -> tuple[str, str]:
    tenant = os.environ.get("MAIL_AGENT_TENANT_ID", "local").strip()
    mailbox = os.environ.get("MAIL_AGENT_MAILBOX_ID", "primary").strip()
    if not _IDENTITY.fullmatch(tenant) or not _IDENTITY.fullmatch(mailbox):
        raise PreferenceAuthorityError("invalid preference tenant or mailbox id")
    return tenant, mailbox


def _identity(name: str, value: object) -> str:
    candidate = value if isinstance(value, str) else ""
    if not _IDENTITY.fullmatch(candidate):
        raise PreferenceAuthorityError(f"invalid preference {name}")
    return candidate


def _normalize_scope(scope_type: str, scope_value: object) -> tuple[str, str]:
    if scope_type not in SCOPE_TYPES:
        raise ValueError("unsupported preference scope type")
    raw = str(scope_value or "").strip()
    if any(character in raw for character in ("\x00", "\r", "\n")):
        raise ValueError("preference scope contains control characters")
    if scope_type == "default":
        return scope_type, "*"
    if scope_type == "sender_email":
        _, address = parseaddr(raw)
        normalized = address.casefold()
        if normalized != raw.casefold() or normalized.count("@") != 1:
            raise ValueError("sender_email scope requires one exact email address")
        local, domain = normalized.rsplit("@", 1)
        if not local or not _DOMAIN.fullmatch(domain):
            raise ValueError("sender_email scope is invalid")
        return scope_type, normalized
    if scope_type == "sender_domain":
        normalized = raw.removeprefix("@").casefold()
        if not _DOMAIN.fullmatch(normalized):
            raise ValueError("sender_domain scope is invalid")
        return scope_type, normalized
    normalized = " ".join(raw.split()).casefold()
    if not 2 <= len(normalized) <= 200:
        raise ValueError(f"{scope_type} scope must be between 2 and 200 characters")
    return scope_type, normalized


def _normalize_instruction(rule_kind: str, instruction: object) -> str | None:
    if rule_kind not in RULE_KINDS:
        raise ValueError("unsupported preference rule kind")
    if rule_kind == "processing_exclusion":
        return None
    normalized = " ".join(str(instruction or "").split())
    if not normalized or len(normalized) > 1000 or "\x00" in normalized:
        raise ValueError("reply_style requires an instruction of at most 1000 characters")
    return normalized


def _skill(rule_kind: str) -> str:
    return "email_triage" if rule_kind == "processing_exclusion" else "reply_drafting"


def _effects(rule_kind: str) -> dict[str, Any]:
    if rule_kind == "processing_exclusion":
        return {
            "mode": "archive_only",
            "no_action": True,
            "no_draft": True,
            "no_notification": True,
        }
    return {"apply_to": "reply_draft"}


def propose_rule(
    *,
    user_id: str,
    channel_id: str,
    thread_ts: str,
    source_interaction_id: str,
    rule_kind: str,
    scope_type: str,
    scope_value: object,
    instruction: object = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist an inert proposal that requires a later explicit confirmation."""
    user = _identity("user id", user_id)
    channel = _identity("channel id", channel_id)
    thread = _identity("thread timestamp", thread_ts)
    source = _identity("source interaction id", source_interaction_id)
    scope_type, scope_value = _normalize_scope(scope_type, scope_value)
    instruction = _normalize_instruction(rule_kind, instruction)
    tenant, mailbox = _environment_scope()
    current = (now or _now()).astimezone(timezone.utc)
    expires = current + timedelta(seconds=_ttl_seconds())
    effects_json = json.dumps(
        _effects(rule_kind),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    canonical = (user, channel, thread, rule_kind, scope_type, scope_value, instruction)

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM user_preferences WHERE source_interaction_id=?", (source,)
        ).fetchone()
        if row is not None:
            item = dict(row)
            existing = (
                item["user_id"],
                item["channel_id"],
                item["thread_ts"],
                item["rule_kind"],
                item["scope_type"],
                item["scope_value"],
                item.get("instruction"),
            )
            if existing != canonical:
                raise PreferenceAuthorityError(
                    "preference interaction was reused with different rule data"
                )
            return item

        timestamp = _iso(current)
        cursor = conn.execute(
            """
            INSERT INTO user_preferences (
                tenant_id, mailbox_id, user_id, skill_name, rule_kind,
                scope_type, scope_value, instruction, effects_json, status,
                source_interaction_id, channel_id, thread_ts, expires_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant,
                mailbox,
                user,
                _skill(rule_kind),
                rule_kind,
                scope_type,
                scope_value,
                instruction,
                effects_json,
                source,
                channel,
                thread,
                _iso(expires),
                timestamp,
                timestamp,
            ),
        )
        return dict(
            conn.execute(
                "SELECT * FROM user_preferences WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        )


def get_pending_rule(
    *,
    user_id: str,
    channel_id: str,
    thread_ts: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    user = _identity("user id", user_id)
    channel = _identity("channel id", channel_id)
    thread = _identity("thread timestamp", thread_ts)
    tenant, mailbox = _environment_scope()
    current = (now or _now()).astimezone(timezone.utc)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM user_preferences
            WHERE tenant_id=? AND mailbox_id=? AND user_id=?
              AND channel_id=? AND thread_ts=? AND status='pending'
            ORDER BY id DESC
            """,
            (tenant, mailbox, user, channel, thread),
        ).fetchall()
        for row in rows:
            item = dict(row)
            expiry = _parse_time(item.get("expires_at"))
            if expiry is not None and current < expiry:
                return item
            conn.execute(
                "UPDATE user_preferences SET status='expired', updated_at=? WHERE id=?",
                (_iso(current), item["id"]),
            )
    return None


def confirm_rule(
    preference_id: int,
    *,
    user_id: str,
    channel_id: str,
    thread_ts: str,
    confirmation_interaction_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Activate one same-user, same-thread, unexpired proposal."""
    user = _identity("user id", user_id)
    channel = _identity("channel id", channel_id)
    thread = _identity("thread timestamp", thread_ts)
    confirmation = _identity("confirmation interaction id", confirmation_interaction_id)
    tenant, mailbox = _environment_scope()
    current = (now or _now()).astimezone(timezone.utc)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM user_preferences WHERE id=?", (int(preference_id),)
        ).fetchone()
        if row is None:
            raise PreferenceAuthorityError("preference proposal not found")
        item = dict(row)
        expected = (tenant, mailbox, user, channel, thread)
        actual = tuple(
            item[key]
            for key in ("tenant_id", "mailbox_id", "user_id", "channel_id", "thread_ts")
        )
        if actual != expected:
            raise PreferenceAuthorityError("preference proposal context does not match")
        if item["status"] == "active":
            if item.get("confirmation_interaction_id") == confirmation:
                return item
            raise PreferenceAuthorityError("preference proposal is already active")
        if item["status"] != "pending":
            raise PreferenceAuthorityError("preference proposal is not pending")
        expiry = _parse_time(item.get("expires_at"))
        if expiry is None or current >= expiry:
            conn.execute(
                "UPDATE user_preferences SET status='expired', updated_at=? WHERE id=?",
                (_iso(current), item["id"]),
            )
            raise PreferenceAuthorityError("preference proposal expired")

        maximum = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS value FROM user_preferences
            WHERE tenant_id=? AND mailbox_id=? AND user_id=? AND rule_kind=?
              AND scope_type=? AND scope_value=? AND id != ?
            """,
            (
                tenant,
                mailbox,
                user,
                item["rule_kind"],
                item["scope_type"],
                item["scope_value"],
                item["id"],
            ),
        ).fetchone()["value"]
        timestamp = _iso(current)
        conn.execute(
            """
            UPDATE user_preferences SET status='disabled', disabled_at=?, updated_at=?
            WHERE tenant_id=? AND mailbox_id=? AND user_id=? AND rule_kind=?
              AND scope_type=? AND scope_value=? AND status='active'
            """,
            (
                timestamp,
                timestamp,
                tenant,
                mailbox,
                user,
                item["rule_kind"],
                item["scope_type"],
                item["scope_value"],
            ),
        )
        conn.execute(
            """
            UPDATE user_preferences
            SET status='active', confirmation_interaction_id=?, version=?,
                confirmed_at=?, expires_at=NULL, updated_at=?
            WHERE id=? AND status='pending'
            """,
            (confirmation, int(maximum) + 1, timestamp, timestamp, item["id"]),
        )
        return dict(
            conn.execute(
                "SELECT * FROM user_preferences WHERE id=?", (item["id"],)
            ).fetchone()
        )


def disable_rule(
    preference_id: int,
    *,
    user_id: str,
    interaction_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    user = _identity("user id", user_id)
    interaction = _identity("interaction id", interaction_id)
    tenant, mailbox = _environment_scope()
    current = (now or _now()).astimezone(timezone.utc)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM user_preferences WHERE id=?", (int(preference_id),)
        ).fetchone()
        if row is None:
            raise PreferenceAuthorityError("preference rule not found")
        item = dict(row)
        if (item["tenant_id"], item["mailbox_id"], item["user_id"]) != (
            tenant,
            mailbox,
            user,
        ):
            raise PreferenceAuthorityError("preference rule owner does not match")
        if item["status"] == "disabled":
            return item
        if item["status"] not in {"active", "pending"}:
            raise PreferenceAuthorityError("preference rule cannot be disabled")
        timestamp = _iso(current)
        conn.execute(
            """
            UPDATE user_preferences
            SET status='disabled', disabled_by_interaction_id=?,
                disabled_at=?, updated_at=? WHERE id=?
            """,
            (interaction, timestamp, timestamp, item["id"]),
        )
        return dict(
            conn.execute(
                "SELECT * FROM user_preferences WHERE id=?", (item["id"],)
            ).fetchone()
        )


def list_rules(*, user_id: str, include_inactive: bool = False) -> list[dict[str, Any]]:
    user = _identity("user id", user_id)
    tenant, mailbox = _environment_scope()
    statuses = (
        ("active", "pending", "disabled", "expired")
        if include_inactive
        else ("active",)
    )
    placeholders = ",".join("?" for _ in statuses)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM user_preferences
            WHERE tenant_id=? AND mailbox_id=? AND user_id=?
              AND status IN ({placeholders})
            ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                     id DESC
            """,
            (tenant, mailbox, user, *statuses),
        ).fetchall()
    return [dict(row) for row in rows]


def _runtime_user_id() -> str | None:
    configured = os.environ.get("MAIL_AGENT_PREFERENCE_USER_ID", "").strip()
    if configured:
        return _identity("runtime user id", configured)
    tenant, mailbox = _environment_scope()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT user_id FROM user_preferences
            WHERE tenant_id=? AND mailbox_id=? AND status='active'
            ORDER BY user_id LIMIT 2
            """,
            (tenant, mailbox),
        ).fetchall()
    return str(rows[0]["user_id"]) if len(rows) == 1 else None


def _matches(rule: dict[str, Any], email_data: dict[str, Any]) -> bool:
    scope_type = rule["scope_type"]
    expected = str(rule["scope_value"])
    sender_email = str(email_data.get("sender_email") or "").strip().casefold()
    sender_name = " ".join(str(email_data.get("sender") or "").split()).casefold()
    subject = " ".join(str(email_data.get("subject") or "").split()).casefold()
    if scope_type == "default":
        return True
    if scope_type == "sender_email":
        return sender_email == expected
    if scope_type == "sender_domain":
        return "@" in sender_email and sender_email.rsplit("@", 1)[1] == expected
    if scope_type == "sender_name":
        return expected in sender_name
    if scope_type == "subject_contains":
        return expected in subject
    return False


def resolve_rules(
    email_data: dict[str, Any],
    *,
    rule_kind: str,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    if rule_kind not in RULE_KINDS:
        raise ValueError("unsupported preference rule kind")
    owner = user_id or _runtime_user_id()
    if owner is None:
        return []
    tenant, mailbox = _environment_scope()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM user_preferences
            WHERE tenant_id=? AND mailbox_id=? AND user_id=?
              AND rule_kind=? AND status='active'
            """,
            (tenant, mailbox, _identity("user id", owner), rule_kind),
        ).fetchall()
    matches = [dict(row) for row in rows if _matches(dict(row), email_data)]
    matches.sort(key=lambda item: (_SPECIFICITY[item["scope_type"]], int(item["id"])))
    for item in matches:
        try:
            item["effects"] = json.loads(item["effects_json"])
        except (TypeError, json.JSONDecodeError):
            item["effects"] = {}
    return matches


def resolve_processing_policy(
    email_data: dict[str, Any], *, user_id: str | None = None
) -> dict[str, Any]:
    rules = resolve_rules(
        email_data, rule_kind="processing_exclusion", user_id=user_id
    )
    return {
        "suppress_processing": any(
            rule.get("effects", {}).get("mode") == "archive_only" for rule in rules
        ),
        "rules": rules,
    }


def resolve_reply_preferences(
    email_data: dict[str, Any], *, user_id: str | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "id": int(rule["id"]),
            "version": int(rule["version"]),
            "instruction": rule["instruction"],
        }
        for rule in resolve_rules(email_data, rule_kind="reply_style", user_id=user_id)
        if rule.get("instruction")
    ]


__all__ = [
    "PreferenceAuthorityError",
    "confirm_rule",
    "disable_rule",
    "get_pending_rule",
    "list_rules",
    "propose_rule",
    "resolve_processing_policy",
    "resolve_reply_preferences",
    "resolve_rules",
]
