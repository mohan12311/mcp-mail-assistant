"""Canonical task state transition contract.

Every runtime path that changes ``status``, ``approval_status`` or
``send_state`` uses this module.  The database transaction remains the final
authority; callers cannot validate a stale task and then update it separately.
"""
from __future__ import annotations

import sqlite3
from typing import Any


class TaskStateError(RuntimeError):
    """The stored state is unknown or violates a transition contract."""


TASK_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "failed", "cancelled"}
)
APPROVAL_STATUSES = frozenset(
    {"none", "pending", "approved", "denied", "expired", "cancelled"}
)
SEND_STATES = frozenset(
    {
        None,
        "draft_generated",
        "draft_failed",
        "pending_send_confirmation",
        "sending",
        "sent",
        "send_failed",
        "delivery_unknown",
        "cancelled",
    }
)

_TASK_TRANSITIONS = {
    "pending": frozenset({"in_progress", "completed", "failed", "cancelled"}),
    "in_progress": frozenset({"pending", "completed", "failed", "cancelled"}),
    "failed": frozenset({"pending", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}
_APPROVAL_TRANSITIONS = {
    "none": frozenset({"pending", "cancelled"}),
    "pending": frozenset({"approved", "denied", "expired", "cancelled"}),
    "approved": frozenset({"pending", "expired", "cancelled"}),
    "expired": frozenset({"pending", "cancelled"}),
    "denied": frozenset(),
    "cancelled": frozenset(),
}
_SEND_TRANSITIONS = {
    # ``None -> pending_send_confirmation`` is retained only for pre-v6 data
    # adoption.  SMTP authority still requires a v6 approved request/event.
    None: frozenset(
        {"draft_generated", "draft_failed", "pending_send_confirmation", "cancelled"}
    ),
    "draft_generated": frozenset(
        {"draft_generated", "draft_failed", "pending_send_confirmation", "cancelled"}
    ),
    "draft_failed": frozenset({"draft_generated", "draft_failed", "cancelled"}),
    "pending_send_confirmation": frozenset(
        {"sending", "draft_generated", "cancelled"}
    ),
    "sending": frozenset({"sent", "send_failed", "delivery_unknown"}),
    "send_failed": frozenset({"pending_send_confirmation", "cancelled"}),
    "delivery_unknown": frozenset({"pending_send_confirmation", "cancelled"}),
    "sent": frozenset(),
    "cancelled": frozenset(),
}


def _check_known(task: dict[str, Any]) -> None:
    if task.get("status") not in TASK_STATUSES:
        raise TaskStateError("unknown task status")
    if task.get("approval_status") not in APPROVAL_STATUSES:
        raise TaskStateError("unknown approval status")
    if task.get("send_state") not in SEND_STATES:
        raise TaskStateError("unknown send state")


def _check_edge(name: str, current: Any, target: Any, graph: dict[Any, frozenset]) -> None:
    if current == target:
        return
    if target not in graph[current]:
        raise TaskStateError(f"disallowed {name} transition: {current!r} -> {target!r}")


def validate_transition(
    current: dict[str, Any],
    *,
    status: str | None = None,
    approval_status: str | None = None,
    send_state: str | None | object = ...,
) -> tuple[str, str, str | None]:
    """Validate one three-axis transition and return its target state."""
    _check_known(current)
    target_status = current["status"] if status is None else status
    target_approval = (
        current["approval_status"] if approval_status is None else approval_status
    )
    target_send = current.get("send_state") if send_state is ... else send_state
    target = {
        "status": target_status,
        "approval_status": target_approval,
        "send_state": target_send,
    }
    _check_known(target)
    _check_edge("task status", current["status"], target_status, _TASK_TRANSITIONS)
    _check_edge(
        "approval status",
        current["approval_status"],
        target_approval,
        _APPROVAL_TRANSITIONS,
    )
    _check_edge("send state", current.get("send_state"), target_send, _SEND_TRANSITIONS)

    if target_send == "sending" and not (
        target_status == "pending" and target_approval == "approved"
    ):
        raise TaskStateError("sending requires a pending task with approved authority")
    if target_send == "sent" and not (
        target_status == "completed" and target_approval == "approved"
    ):
        raise TaskStateError("sent requires completed task and approved authority")
    if target_status == "cancelled" and target_send in {
        "pending_send_confirmation",
        "sending",
        "sent",
    }:
        raise TaskStateError("cancelled task cannot retain an active or completed send state")
    if target_approval in {"denied", "expired", "cancelled"} and target_send in {
        "pending_send_confirmation",
        "sending",
    }:
        raise TaskStateError("inactive approval cannot retain an active send state")
    if current.get("send_state") == "sending" and target_send == "sending" and (
        target_status != current["status"]
        or target_approval != current["approval_status"]
    ):
        raise TaskStateError("in-flight send authority is immutable")
    return target_status, target_approval, target_send  # type: ignore[return-value]


def transition_task(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    status: str | None = None,
    approval_status: str | None = None,
    send_state: str | None | object = ...,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and update a task using the caller's transaction."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskStateError("task not found")
    current = dict(row)
    target_status, target_approval, target_send = validate_transition(
        current,
        status=status,
        approval_status=approval_status,
        send_state=send_state,
    )
    updates: dict[str, Any] = dict(fields or {})
    forbidden = {"id", "status", "approval_status", "send_state"} & updates.keys()
    if forbidden:
        raise TaskStateError("state columns must use the transition arguments")
    updates.update(
        {
            "status": target_status,
            "approval_status": target_approval,
            "send_state": target_send,
        }
    )
    assignments = ", ".join(f'"{name}" = ?' for name in updates)
    params = [*updates.values(), task_id]
    cursor = conn.execute(f'UPDATE tasks SET {assignments} WHERE id = ?', params)
    if cursor.rowcount != 1:
        raise TaskStateError("task changed during transition")
    updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(updated)
