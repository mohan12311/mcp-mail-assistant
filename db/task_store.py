"""
tasks 테이블 CRUD 함수

태스크 정보를 저장하고 조회합니다.
"""

import hashlib
import json
import logging
from typing import Optional, List
from datetime import datetime, timedelta, timezone
from db.connection import get_connection
from db.approval_store import approval_ttl_seconds, iso_utc, request_approval, utc_now
from db.task_state import (
    APPROVAL_STATUSES,
    SEND_STATES,
    TASK_STATUSES,
    TaskStateError,
    transition_task,
)

logger = logging.getLogger(__name__)


def save_task(task: dict) -> int:
    """
    태스크를 저장합니다.
    
    Args:
        task: 태스크 정보 dict
            - email_id (str, required): 연관된 메일 ID
            - task_type (str, required): 태스크 타입
            - title (str, required): 제목
            - description (str): 설명
            - deadline (str): 기한 (날짜 문자열)
            - priority (str): 우선순위 (urgent/high/medium/low)
            - status (str): 상태 (pending/in_progress/completed)
            - approval_status (str): 승인 상태 (none/pending/approved/denied)
    
    Returns:
        int: 생성된 태스크 ID
        
    Raises:
        Exception: DB 저장 실패 시
    """
    try:
        status = task.get("status", "pending")
        approval_status = task.get("approval_status", "none")
        send_state = task.get("send_state")
        if status not in TASK_STATUSES:
            raise TaskStateError("unknown initial task status")
        if approval_status not in APPROVAL_STATUSES:
            raise TaskStateError("unknown initial approval status")
        if send_state not in SEND_STATES:
            raise TaskStateError("unknown initial send state")
        requested_at = None
        expires_at = None
        approval_version = 0
        if approval_status == "pending":
            requested = utc_now()
            requested_at = iso_utc(requested)
            expires_at = iso_utc(
                requested + timedelta(seconds=approval_ttl_seconds())
            )
            approval_version = 1

        with get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO tasks (
                    email_id, task_type, title, description, deadline,
                    priority, status, approval_status, send_state,
                    approval_requested_at, approval_expires_at, approval_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task["email_id"],
                task["task_type"],
                task["title"],
                task.get("description"),
                task.get("deadline"),
                task.get("priority", "medium"),
                status,
                approval_status,
                send_state,
                requested_at,
                expires_at,
                approval_version,
            ))
            
            task_id = cursor.lastrowid
            logger.info(f"태스크 저장 완료: {task_id}")
            return task_id
            
    except Exception as e:
        logger.error(f"태스크 저장 실패: {e}")
        raise


def get_task(task_id: int) -> Optional[dict]:
    """
    태스크를 ID로 조회합니다.
    
    Args:
        task_id: 태스크 ID
        
    Returns:
        dict | None: 태스크 정보 또는 None
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            return None
            
    except Exception as e:
        logger.error(f"태스크 조회 실패: {e}")
        raise


def get_tasks_by_email(email_id: str) -> List[dict]:
    """
    특정 메일과 연관된 태스크 목록을 조회합니다.
    
    Args:
        email_id: 메일 ID
        
    Returns:
        list[dict]: 태스크 목록
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM tasks 
                WHERE email_id = ? 
                ORDER BY created_at DESC
            """, (email_id,))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
    except Exception as e:
        logger.error(f"메일 태스크 조회 실패: {e}")
        raise


def get_pending_tasks() -> List[dict]:
    """
    대기 중인 태스크 목록을 조회합니다.
    
    Returns:
        list[dict]: 대기 중인 태스크 목록
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM tasks 
                WHERE status = 'pending' 
                ORDER BY created_at DESC
            """)
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
    except Exception as e:
        logger.error(f"대기 중인 태스크 조회 실패: {e}")
        raise


def get_tasks_awaiting_approval() -> List[dict]:
    """
    승인 대기 중인 태스크 목록을 조회합니다.
    
    Returns:
        list[dict]: 승인 대기 중인 태스크 목록
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM tasks 
                WHERE approval_status = 'pending' 
                ORDER BY created_at DESC
            """)
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
    except Exception as e:
        logger.error(f"승인 대기 태스크 조회 실패: {e}")
        raise


def update_approval_status(
    task_id: int, 
    status: str, 
    slack_ts: Optional[str] = None
) -> bool:
    """
    태스크의 승인 상태를 업데이트합니다.
    
    Args:
        task_id: 태스크 ID
        status: 승인 상태 (pending/approved/denied)
        slack_ts: Slack 메시지 타임스탬프 (선택)
        
    Returns:
        bool: 성공 여부
    """
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fields = {"approval_slack_ts": slack_ts}
            if status == "approved":
                fields["approved_at"] = iso_utc()
            transition_task(
                conn,
                task_id,
                approval_status=status,
                fields=fields,
            )
            success = True
            if success:
                logger.info(f"태스크 승인 상태 업데이트: {task_id} -> {status}")
            return success
            
    except Exception as e:
        logger.error(f"태스크 승인 상태 업데이트 실패: {e}")
        raise


def mark_as_executed(task_id: int, result: str) -> bool:
    """
    태스크를 실행 완료로 표시합니다.
    
    Args:
        task_id: 태스크 ID
        result: 실행 결과
        
    Returns:
        bool: 성공 여부
    """
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            transition_task(
                conn,
                task_id,
                status="completed",
                fields={
                    "executed_at": iso_utc(),
                    "execution_result": result,
                },
            )
            success = True
            if success:
                logger.info(f"태스크 실행 완료 표시: {task_id}")
            return success
            
    except Exception as e:
        logger.error(f"태스크 실행 완료 표시 실패: {e}")
        raise


def get_overdue_tasks() -> List[dict]:
    """
    기한이 지난 태스크 목록을 조회합니다.
    
    Returns:
        list[dict]: 기한 초과 태스크 목록
    """
    try:
        today = datetime.now().date().isoformat()
        
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM tasks 
                WHERE deadline < ? 
                  AND status != 'completed'
                  AND deadline IS NOT NULL
                ORDER BY deadline ASC
            """, (today,))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
    except Exception as e:
        logger.error(f"기한 초과 태스크 조회 실패: {e}")
        raise



class TaskStore:
    """
    호환용 Store 래퍼.

    과거/외부 코드에서 `TaskStore().get_connection()` 패턴을 기대하는 경우가 있어,
    모듈 함수 기반 CRUD를 유지하면서 최소 래퍼 클래스를 제공합니다.
    """

    def get_connection(self):
        return get_connection()


def create_task(
    email_id: str,
    task_type: str,
    title: str,
    description: Optional[str] = None,
    deadline: Optional[str] = None,
    priority: str = "medium",
    status: str = "pending",
    approval_status: str = "none",
) -> int:
    """
    태스크를 생성합니다 (save_task의 편의 함수).
    
    Args:
        email_id: 연관된 메일 ID
        task_type: 태스크 타입
        title: 제목
        description: 설명
        deadline: 기한 (YYYY-MM-DD)
        priority: 우선순위
        status: 초기 태스크 상태
        approval_status: 초기 승인 상태
        
    Returns:
        int: 생성된 태스크 ID
    """
    return save_task({
        "email_id": email_id,
        "task_type": task_type,
        "title": title,
        "description": description,
        "deadline": deadline,
        "priority": priority,
        "status": status,
        "approval_status": approval_status
    })


def update_task_status(
    task_id: int,
    status: str,
    execution_result: Optional[str] = None
) -> bool:
    """
    태스크의 상태를 업데이트합니다.
    
    Args:
        task_id: 태스크 ID
        status: 상태 (pending/in_progress/completed/failed/cancelled)
        execution_result: 실행 결과 (완료/실패 시)
        
    Returns:
        bool: 성공 여부
    """
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return False
            task = dict(row)
            fields = {"execution_result": execution_result}
            approval_status = None
            send_state: str | None | object = ...
            if status == "completed":
                fields["executed_at"] = iso_utc()
            elif status == "cancelled":
                if task.get("approval_status") not in {"denied", "cancelled"}:
                    approval_status = "cancelled"
                if task.get("send_state") not in {None, "sent", "cancelled"}:
                    send_state = "cancelled"
            transition_task(
                conn,
                task_id,
                status=status,
                approval_status=approval_status,
                send_state=send_state,
                fields=fields,
            )
            success = True
            if success:
                logger.info(f"태스크 상태 업데이트: {task_id} -> {status}")
            return success
            
    except Exception as e:
        logger.error(f"태스크 상태 업데이트 실패: {e}")
        raise


def list_tasks(
    status: Optional[str] = None,
    approval_status: Optional[str] = None,
    limit: int = 50
) -> List[dict]:
    """
    태스크 목록을 조회합니다.
    
    Args:
        status: 필터링할 상태 (None이면 전체)
        approval_status: 필터링할 승인 상태 (None이면 전체)
        limit: 최대 조회 개수
        
    Returns:
        list[dict]: 태스크 목록
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM tasks WHERE 1=1"
            params = []
            
            if status:
                query += " AND status = ?"
                params.append(status)
            
            if approval_status:
                query += " AND approval_status = ?"
                params.append(approval_status)
            
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
    except Exception as e:
        logger.error(f"태스크 목록 조회 실패: {e}")
        raise


def update_send_state(task_id: int, send_state: str) -> bool:
    """Update send state through the canonical transition contract."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        fields = {
            "send_claimed_at": iso_utc() if send_state == "sending" else None,
            "send_claim_event_id": None,
        }
        if send_state == "sending":
            row = conn.execute("SELECT approval_status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None or row["approval_status"] != "approved":
                raise TaskStateError("sending requires approved authority")
        transition_task(conn, task_id, send_state=send_state, fields=fields)
        return True


def claim_send_pending(task_id: int, event_id: Optional[str] = None) -> bool:
    """Atomically claim a send task before SMTP side effects.

    A pending task can be claimed by any event. A task already in "sending" can
    only be re-claimed by the same event, which lets leased-event retries recover
    without allowing a second Slack submission to send the same message.
    """
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return False
        task = dict(row)
        if (
            task.get("task_type") != "reply_email"
            or task.get("status") != "pending"
            or task.get("approval_status") != "approved"
            or task.get("send_state") != "pending_send_confirmation"
        ):
            return False
        transition_task(
            conn,
            task_id,
            send_state="sending",
            fields={
                "send_claimed_at": iso_utc(),
                "send_claim_event_id": event_id,
            },
        )
        return True


def update_send_payload(task_id: int, payload_json: str) -> bool:
    """Store a draft and issue a new version-bound approval request."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return False
        task = dict(row)
        if task.get("status") != "pending" or task.get("task_type") != "reply_email":
            raise TaskStateError("send payload can change only on a pending reply task")
        conn.execute(
            "UPDATE tasks SET send_payload = ? WHERE id = ?",
            (payload_json, task_id),
        )
        next_send_state: str | None | object = ...
        if task.get("send_state") == "pending_send_confirmation":
            next_send_state = "draft_generated"
        request_approval(
            task_id,
            payload_hash=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            conn=conn,
            send_state=next_send_state,
        )
        return True


def list_actionable_tasks(limit: int = 20, *, now: Optional[datetime] = None) -> List[dict]:
    """List pending work whose snooze window has elapsed."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'pending'
              AND (snoozed_until IS NULL OR snoozed_until <= ?)
            ORDER BY
                CASE priority
                    WHEN 'urgent' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    ELSE 3
                END,
                CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                deadline,
                created_at DESC
            LIMIT ?
            """,
            (current.astimezone(timezone.utc).isoformat(), limit),
        ).fetchall()
        return [dict(row) for row in rows]


def snooze_task(
    task_id: int,
    until: datetime,
    *,
    interaction_id: Optional[str] = None,
) -> dict:
    """Hide pending work until ``until`` and invalidate an old review window."""
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    target_until = until.astimezone(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskStateError("task not found")
        task = dict(row)
        if interaction_id and task.get("last_slack_interaction_id") == interaction_id:
            return task
        if task.get("status") != "pending":
            raise TaskStateError("only pending tasks can be snoozed")
        if task.get("send_state") in {"sending", "sent"}:
            raise TaskStateError("an in-flight or sent reply cannot be snoozed")
        approval_status = None
        send_state: str | None | object = ...
        fields = {
            "snoozed_until": target_until,
            "last_slack_interaction_id": interaction_id,
        }
        if task.get("approval_status") in {"pending", "approved"}:
            approval_status = "expired"
            fields.update({"approval_source": None, "approved_at": None})
        if task.get("send_state") == "pending_send_confirmation":
            send_state = "draft_generated"
        return transition_task(
            conn,
            task_id,
            approval_status=approval_status,
            send_state=send_state,
            fields=fields,
        )


def complete_task_without_send(
    task_id: int,
    *,
    interaction_id: Optional[str] = None,
) -> dict:
    """Complete a task while explicitly cancelling any unsent reply authority."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskStateError("task not found")
        task = dict(row)
        if interaction_id and task.get("last_slack_interaction_id") == interaction_id:
            return task
        if task.get("status") == "completed":
            return task
        if task.get("status") not in {"pending", "in_progress"}:
            raise TaskStateError("task cannot be completed from its current state")
        if task.get("send_state") in {"sending", "sent"}:
            raise TaskStateError("an in-flight or sent reply cannot be completed manually")
        approval_status = None
        if task.get("approval_status") in {"pending", "approved", "expired"}:
            approval_status = "cancelled"
        send_state: str | None | object = ...
        if task.get("send_state") is not None:
            send_state = "cancelled"
        return transition_task(
            conn,
            task_id,
            status="completed",
            approval_status=approval_status,
            send_state=send_state,
            fields={
                "snoozed_until": None,
                "executed_at": iso_utc(),
                "execution_result": "Completed by authorized Slack conversation without SMTP",
                "approval_source": None,
                "approved_at": None,
                "last_slack_interaction_id": interaction_id,
            },
        )


def replace_reply_draft(
    task_id: int,
    *,
    subject: str,
    body: str,
    interaction_id: Optional[str] = None,
) -> dict:
    """Replace only draft text, preserving DB-owned recipient/thread metadata."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskStateError("task not found")
        task = dict(row)
        if interaction_id and task.get("last_slack_interaction_id") == interaction_id:
            return task
        if (
            task.get("task_type") != "reply_email"
            or task.get("status") != "pending"
            or task.get("approval_status") not in {"pending", "expired"}
            or task.get("send_state")
            not in {"draft_generated", "draft_failed", "pending_send_confirmation"}
            or not isinstance(task.get("send_payload"), str)
        ):
            raise TaskStateError("reply draft is not editable")
        try:
            payload = json.loads(task["send_payload"])
        except json.JSONDecodeError as exc:
            raise TaskStateError("reply draft payload is invalid") from exc
        payload["subject"] = subject
        payload["body"] = body
        payload.pop("error", None)
        payload_json = json.dumps(payload, ensure_ascii=False)
        conn.execute(
            """
            UPDATE tasks
            SET send_payload = ?, snoozed_until = NULL,
                last_slack_interaction_id = ?
            WHERE id = ?
            """,
            (payload_json, interaction_id, task_id),
        )
        return request_approval(
            task_id,
            payload_hash=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            conn=conn,
            send_state="draft_generated",
        )


def refresh_reply_review(task_id: int) -> dict:
    """Return a reviewable draft, reissuing expired authority from SQLite state."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskStateError("task not found")
        task = dict(row)
        if (
            task.get("task_type") != "reply_email"
            or task.get("status") != "pending"
            or task.get("send_state")
            not in {"draft_generated", "pending_send_confirmation"}
            or not isinstance(task.get("send_payload"), str)
        ):
            raise TaskStateError("reply draft is not reviewable")
        if task.get("approval_status") == "approved":
            return task
        expiry = task.get("approval_expires_at")
        try:
            expiry_dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            expiry_dt = datetime.min.replace(tzinfo=timezone.utc)
        current = datetime.now(timezone.utc)
        conn.execute("UPDATE tasks SET snoozed_until = NULL WHERE id = ?", (task_id,))
        if task.get("approval_status") != "pending" or current >= expiry_dt:
            payload_hash = hashlib.sha256(task["send_payload"].encode("utf-8")).hexdigest()
            task = request_approval(
                task_id,
                payload_hash=payload_hash,
                conn=conn,
                send_state="draft_generated",
            )
        return task


def prepare_local_send_confirmation(task_id: int) -> bool:
    """Deprecated: authority and event creation must be one atomic operation."""
    raise TaskStateError(
        "use approval_store.create_send_confirmation to avoid an authority gap"
    )
