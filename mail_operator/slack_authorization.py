"""Fail-closed Slack approver authorization without identifier logging."""
from __future__ import annotations

import hmac
import os


class SlackAuthorizationError(RuntimeError):
    """The Slack actor or action is not authorized."""


def configured_approvers() -> tuple[str, ...]:
    raw = os.environ.get("SLACK_APPROVER_USER_IDS", "")
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not values:
        raise SlackAuthorizationError("Slack approver allowlist is not configured")
    return values


def require_authorized_user(user_id: object) -> None:
    candidate = user_id if isinstance(user_id, str) else ""
    if not candidate:
        raise SlackAuthorizationError("Slack approval actor is missing")
    allowed = configured_approvers()
    if not any(hmac.compare_digest(candidate, expected) for expected in allowed):
        raise SlackAuthorizationError("Slack approval actor is not authorized")


def require_action(action: object, expected_action_id: str) -> None:
    if not isinstance(action, dict) or action.get("action_id") != expected_action_id:
        raise SlackAuthorizationError("Slack approval action is invalid")


def require_view(body: object, expected_callback_id: str) -> None:
    if not isinstance(body, dict):
        raise SlackAuthorizationError("Slack approval view is invalid")
    view = body.get("view")
    if not isinstance(view, dict) or view.get("callback_id") != expected_callback_id:
        raise SlackAuthorizationError("Slack approval view is invalid")
