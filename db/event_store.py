"""
events 테이블 CRUD 함수 (KIRA식 영속 이벤트 큐)

이벤트를 DB 기반으로 저장하고 lease 기반으로 처리합니다.
at-least-once + idempotent handler를 기본으로 합니다.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Any

from db.connection import get_connection

logger = logging.getLogger(__name__)


DEFAULT_RETRY_BASE_SECONDS = 5
DEFAULT_RETRY_MAX_SECONDS = 3600


class EventSourceConflict(RuntimeError):
    """A globally unique source_id was reused for a different event intent."""


class UnsafeEventRetry(RuntimeError):
    """Generic retry was requested for an event requiring explicit authorization."""


# 이벤트 타입 상수
class EventType:
    NEW_EMAIL = "new_email"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    TASK_CREATED = "task_created"
    TASK_EXECUTED = "task_executed"
    SEND_CONFIRMED = "send_confirmed"
    SLACK_CONVERSATION = "slack_conversation"
    ERROR = "error"


# 이벤트 상태 상수
class EventStatus:
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"


def create_event(
    event_type: str,
    payload: dict,
    source_id: Optional[str] = None,
    max_attempts: int = 3,
    expires_at: Optional[str] = None,
) -> str:
    """
    새 이벤트를 생성합니다.
    
    Args:
        event_type: 이벤트 타입 (new_email, approval_granted 등)
        payload: 이벤트 페이로드 (dict)
        source_id: 원본 ID (email_id, task_id 등) - idempotency 체크용
        max_attempts: 최대 재시도 횟수
        
    Returns:
        str: 생성된 이벤트 ID
        
    Raises:
        Exception: DB 저장 실패 시
    """
    event_id = str(uuid.uuid4())
    payload_json = json.dumps(payload, ensure_ascii=False)
    now = datetime.now().isoformat()
    
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            
            if source_id:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO events (
                        id, event_type, payload_json, status,
                        source_id, max_attempts, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        payload_json,
                        EventStatus.PENDING,
                        source_id,
                        max_attempts,
                        expires_at,
                        now,
                        now,
                    ),
                )
                if cursor.rowcount == 0:
                    existing = cursor.execute(
                        "SELECT id, event_type FROM events WHERE source_id = ?",
                        (source_id,),
                    ).fetchone()
                    if existing is None:
                        raise EventSourceConflict("source_id conflict could not be resolved")
                    if existing["event_type"] != event_type:
                        raise EventSourceConflict(
                            "source_id is already bound to a different event_type"
                        )
                    logger.info(
                        "원자적 이벤트 중복 무시: %s (type=%s, source=%s)",
                        existing["id"],
                        event_type,
                        source_id,
                    )
                    return str(existing["id"])
            else:
                cursor.execute(
                    """
                    INSERT INTO events (
                        id, event_type, payload_json, status,
                        source_id, max_attempts, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        payload_json,
                        EventStatus.PENDING,
                        max_attempts,
                        expires_at,
                        now,
                        now,
                    ),
                )
            logger.info(f"이벤트 생성: {event_id} (type={event_type}, source={source_id})")
            return event_id
            
    except Exception as e:
        logger.error(f"이벤트 생성 실패: {e}")
        raise


def create_idempotent_event(
    event_type: str,
    payload: dict,
    source_id: str,
    max_attempts: int = 3,
    expires_at: Optional[str] = None,
) -> tuple[str, bool]:
    """결정적 ID로 이벤트를 한 번만 생성한다.

    Returns:
        tuple[str, bool]: (event_id, 이번 호출에서 새로 생성했는지 여부)
    """
    if not source_id:
        raise ValueError("source_id is required for an idempotent event")

    event_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"mcp-mail-assistant:event:{event_type}:source:{source_id}",
        )
    )
    payload_json = json.dumps(payload, ensure_ascii=False)
    now = datetime.now().isoformat()

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO events (
                    id, event_type, payload_json, status,
                    source_id, max_attempts, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    payload_json,
                    EventStatus.PENDING,
                    source_id,
                    max_attempts,
                    expires_at,
                    now,
                    now,
                ),
            )
            created = cursor.rowcount > 0
            if created:
                logger.info(
                    "결정적 이벤트 생성: %s (type=%s, source=%s)",
                    event_id,
                    event_type,
                    source_id,
                )
            else:
                cursor.execute(
                    """
                    SELECT id, event_type, source_id
                    FROM events WHERE source_id = ?
                    """,
                    (source_id,),
                )
                existing = cursor.fetchone()
                if (
                    not existing
                    or existing["event_type"] != event_type
                    or existing["source_id"] != source_id
                ):
                    raise EventSourceConflict(
                        "source_id is already bound to a different event intent"
                    )
                event_id = str(existing["id"])
                logger.info(
                    "결정적 이벤트 중복 무시: %s (type=%s, source=%s)",
                    event_id,
                    event_type,
                    source_id,
                )
            return event_id, created
    except Exception as e:
        logger.error(f"결정적 이벤트 생성 실패: {e}")
        raise


def lease_next_event(
    owner: str,
    lease_seconds: int = 300,
    event_types: Optional[List[str]] = None,
    max_count: int = 1
) -> List[dict]:
    """
    다음 처리할 이벤트를 lease(임대)합니다.
    
    - pending 상태이거나
    - leased 상태이지만 lease_until이 지난 이벤트를 대상으로 합니다.
    - attempts < max_attempts 조건을 만족해야 합니다.
    
    Args:
        owner: lease 소유자 ID (operator instance id)
        lease_seconds: lease 유효 시간 (초)
        event_types: 처리할 이벤트 타입 목록 (None이면 전체)
        max_count: 한 번에 lease할 최대 이벤트 수
        
    Returns:
        list[dict]: lease된 이벤트 목록
    """
    now = datetime.now()
    lease_until = now + timedelta(seconds=lease_seconds)
    
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()

            # A worker that disappears on its final ordinary attempt must not
            # leave an unleaseable zombie. SEND_CONFIRMED gets one bounded
            # recovery lease so its durable SMTP ledger can quarantine an
            # interrupted send instead of silently remaining in "sending".
            cursor.execute(
                """
                UPDATE events
                SET status = ?, lease_owner = NULL, lease_until = NULL,
                    next_attempt_at = NULL,
                    last_error = COALESCE(last_error, 'lease_expired_at_attempt_limit'),
                    updated_at = ?
                WHERE status = ? AND lease_until < ?
                  AND attempts >= max_attempts
                  AND NOT (event_type = ? AND attempts = max_attempts)
                """,
                (
                    EventStatus.FAILED,
                    now.isoformat(),
                    EventStatus.LEASED,
                    now.isoformat(),
                    EventType.SEND_CONFIRMED,
                ),
            )
            
            # 조건에 맞는 이벤트 조회
            query = """
                SELECT id, event_type, payload_json, status, 
                       attempts, max_attempts, source_id, expires_at, created_at
                FROM events
                WHERE (
                    (
                        status = ?
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                        AND attempts < max_attempts
                    )
                    OR (
                        status = ? AND lease_until < ?
                        AND (
                            attempts < max_attempts
                            OR (event_type = ? AND attempts = max_attempts)
                        )
                    )
                )
            """
            params: List[Any] = [
                EventStatus.PENDING,
                now.isoformat(),
                EventStatus.LEASED,
                now.isoformat(),
                EventType.SEND_CONFIRMED,
            ]
            
            if event_types:
                placeholders = ','.join(['?' for _ in event_types])
                query += f" AND event_type IN ({placeholders})"
                params.extend(event_types)
            
            query += " ORDER BY created_at ASC LIMIT ?"
            params.append(max_count)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            if not rows:
                return []
            
            # lease 업데이트
            leased_events = []
            for row in rows:
                event_id = row['id']
                lease_token = f"{owner}:{uuid.uuid4().hex}"
                
                cursor.execute("""
                    UPDATE events
                    SET status = ?,
                        lease_owner = ?,
                        lease_until = ?,
                        attempts = attempts + 1,
                        next_attempt_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                """, (
                    EventStatus.LEASED,
                    lease_token,
                    lease_until.isoformat(),
                    now.isoformat(),
                    event_id
                ))
                
                event = dict(row)
                event["status"] = EventStatus.LEASED
                event["lease_owner"] = lease_token
                event["lease_until"] = lease_until.isoformat()
                event["attempts"] = int(event["attempts"]) + 1
                event['payload'] = json.loads(event['payload_json'])
                del event['payload_json']
                leased_events.append(event)
                
                logger.debug(
                    "이벤트 lease: %s (instance=%s, until=%s)",
                    event_id,
                    owner,
                    lease_until,
                )
            
            return leased_events
            
    except Exception as e:
        logger.error(f"이벤트 lease 실패: {e}")
        raise


def ack_event(event_id: str, owner: Optional[str] = None) -> bool:
    """
    이벤트 처리 완료를 확인(acknowledge)합니다.
    
    Args:
        event_id: 이벤트 ID
        
    Returns:
        bool: 성공 여부
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            
            query = """
                UPDATE events
                SET status = ?,
                    lease_owner = NULL,
                    lease_until = NULL,
                    next_attempt_at = NULL,
                    updated_at = ?
                WHERE id = ?
            """
            params: list[Any] = [
                EventStatus.DONE,
                datetime.now().isoformat(),
                event_id,
            ]
            if owner is not None:
                query += " AND status = ? AND lease_owner = ?"
                params.extend([EventStatus.LEASED, owner])
            cursor.execute(query, params)
            
            success = cursor.rowcount > 0
            if success:
                logger.info(f"이벤트 ack 완료: {event_id}")
            else:
                logger.warning(f"이벤트 ack 실패 - 존재하지 않음: {event_id}")
            return success
            
    except Exception as e:
        logger.error(f"이벤트 ack 실패: {e}")
        raise


def fail_event(
    event_id: str,
    error: str,
    retryable: bool = True,
    *,
    owner: Optional[str] = None,
    base_delay_seconds: int = DEFAULT_RETRY_BASE_SECONDS,
    max_delay_seconds: int = DEFAULT_RETRY_MAX_SECONDS,
) -> bool:
    """
    이벤트 처리 실패를 기록합니다.
    
    Args:
        event_id: 이벤트 ID
        error: 에러 메시지
        retryable: 재시도 가능 여부 (False면 바로 failed 상태로)
        
    Returns:
        bool: 성공 여부
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            
            # 현재 상태 조회
            cursor.execute("""
                SELECT event_type, status, lease_owner, attempts, max_attempts
                FROM events WHERE id = ?
            """, (event_id,))
            row = cursor.fetchone()
            
            if not row:
                logger.warning(f"이벤트 fail 실패 - 존재하지 않음: {event_id}")
                return False
            
            attempts = row['attempts']
            max_attempts = row['max_attempts']
            if owner is not None and (
                row["status"] != EventStatus.LEASED or row["lease_owner"] != owner
            ):
                return False

            # SEND_CONFIRMED is never re-armed by the generic automatic retry path.
            # A process interruption still re-leases the same durable claim so the
            # outbound ledger can quarantine uncertainty without another SMTP call.
            if row["event_type"] == EventType.SEND_CONFIRMED:
                retryable = False
            
            # 재시도 불가능하거나 최대 시도 횟수 초과 시 failed로
            if not retryable or attempts >= max_attempts:
                new_status = EventStatus.FAILED
                next_attempt_at = None
            else:
                new_status = EventStatus.PENDING  # 다시 pending으로 (재시도 대기)
                base_delay_seconds = max(1, int(base_delay_seconds))
                max_delay_seconds = max(base_delay_seconds, int(max_delay_seconds))
                delay = min(
                    max_delay_seconds,
                    base_delay_seconds * (2 ** max(0, int(attempts) - 1)),
                )
                next_attempt_at = (
                    datetime.now() + timedelta(seconds=delay)
                ).isoformat()
            
            cursor.execute("""
                UPDATE events
                SET status = ?,
                    lease_owner = NULL,
                    lease_until = NULL,
                    last_error = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                new_status,
                error,
                next_attempt_at,
                datetime.now().isoformat(),
                event_id
            ))
            
            logger.info(f"이벤트 fail 기록: {event_id} (status={new_status}, error={error[:100]}...)")
            return True
            
    except Exception as e:
        logger.error(f"이벤트 fail 기록 실패: {e}")
        raise


def get_event(event_id: str) -> Optional[dict]:
    """
    이벤트를 ID로 조회합니다.
    
    Args:
        event_id: 이벤트 ID
        
    Returns:
        dict | None: 이벤트 정보 또는 None
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = cursor.fetchone()
            
            if row:
                event = dict(row)
                event['payload'] = json.loads(event['payload_json'])
                del event['payload_json']
                return event
            return None
            
    except Exception as e:
        logger.error(f"이벤트 조회 실패: {e}")
        raise


def list_events(
    status: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 50
) -> List[dict]:
    """
    이벤트 목록을 조회합니다.
    
    Args:
        status: 필터링할 상태 (None이면 전체)
        event_type: 필터링할 이벤트 타입 (None이면 전체)
        limit: 최대 조회 개수
        
    Returns:
        list[dict]: 이벤트 목록
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM events WHERE 1=1"
            params: List[Any] = []
            
            if status:
                query += " AND status = ?"
                params.append(status)
            
            if event_type:
                query += " AND event_type = ?"
                params.append(event_type)
            
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            events = []
            for row in rows:
                event = dict(row)
                event['payload'] = json.loads(event['payload_json'])
                del event['payload_json']
                events.append(event)
            
            return events
            
    except Exception as e:
        logger.error(f"이벤트 목록 조회 실패: {e}")
        raise


def list_failed_events(limit: int = 50) -> List[dict]:
    """
    실패한 이벤트 목록을 조회합니다 (DLQ 역할).
    
    Args:
        limit: 최대 조회 개수
        
    Returns:
        list[dict]: 실패한 이벤트 목록
    """
    return list_events(status=EventStatus.FAILED, limit=limit)


def retry_event(event_id: str) -> bool:
    """
    실패한 이벤트를 재시도 대기 상태로 변경합니다.
    
    Args:
        event_id: 이벤트 ID
        
    Returns:
        bool: 성공 여부
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            
            row = cursor.execute(
                "SELECT event_type FROM events WHERE id = ? AND status = ?",
                (event_id, EventStatus.FAILED),
            ).fetchone()
            if row is not None and row["event_type"] == EventType.SEND_CONFIRMED:
                raise UnsafeEventRetry(
                    "send_confirmed requires a new explicit reviewed confirmation"
                )

            cursor.execute("""
                UPDATE events
                SET status = ?,
                    attempts = 0,
                    last_error = NULL,
                    next_attempt_at = NULL,
                    lease_owner = NULL,
                    lease_until = NULL,
                    updated_at = ?
                WHERE id = ? AND status = ?
            """, (
                EventStatus.PENDING,
                datetime.now().isoformat(),
                event_id,
                EventStatus.FAILED
            ))
            
            success = cursor.rowcount > 0
            if success:
                logger.info(f"이벤트 재시도 설정: {event_id}")
            return success
            
    except Exception as e:
        logger.error(f"이벤트 재시도 설정 실패: {e}")
        raise


def event_exists_for_source(source_id: str, event_type: str) -> bool:
    """
    특정 source_id로 생성된 이벤트가 있는지 확인합니다 (idempotency 체크).
    
    Args:
        source_id: 원본 ID
        event_type: 이벤트 타입
        
    Returns:
        bool: 존재 여부
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM events 
                WHERE source_id = ? AND event_type = ?
                LIMIT 1
            """, (source_id, event_type))
            return cursor.fetchone() is not None
            
    except Exception as e:
        logger.error(f"이벤트 존재 확인 실패: {e}")
        raise


def get_event_stats() -> dict:
    """
    이벤트 통계를 반환합니다.
    
    Returns:
        dict: 상태별 이벤트 수
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status, COUNT(*) as count
                FROM events
                GROUP BY status
            """)
            
            stats = {
                EventStatus.PENDING: 0,
                EventStatus.LEASED: 0,
                EventStatus.DONE: 0,
                EventStatus.FAILED: 0
            }
            
            for row in cursor.fetchall():
                stats[row['status']] = row['count']
            
            return stats
            
    except Exception as e:
        logger.error(f"이벤트 통계 조회 실패: {e}")
        raise


def get_operational_metrics() -> dict[str, Any]:
    """Return count/timing-only health signals with no payload, IDs, or errors."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        event_counts = {
            row["status"]: int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM events GROUP BY status"
            ).fetchall()
        }
        outbox_counts = {
            row["status"]: int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM operator_outbox GROUP BY status"
            ).fetchall()
        }
        event_types = {
            row["event_type"]: int(row["count"])
            for row in conn.execute(
                """
                SELECT event_type, COUNT(*) AS count
                FROM events WHERE status = 'failed'
                GROUP BY event_type ORDER BY event_type
                """
            ).fetchall()
        }
        return {
            "events": {
                "pending": event_counts.get(EventStatus.PENDING, 0),
                "leased": event_counts.get(EventStatus.LEASED, 0),
                "done": event_counts.get(EventStatus.DONE, 0),
                "failed": event_counts.get(EventStatus.FAILED, 0),
                "retry_due": int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM events
                        WHERE status = 'pending' AND next_attempt_at IS NOT NULL
                          AND next_attempt_at <= ?
                        """,
                        (now,),
                    ).fetchone()[0]
                ),
                "expired_leases": int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM events
                        WHERE status = 'leased' AND lease_until < ?
                        """,
                        (now,),
                    ).fetchone()[0]
                ),
                "dlq_by_type": event_types,
            },
            "outbox": {
                "pending": outbox_counts.get("pending", 0),
                "dispatching": outbox_counts.get("dispatching", 0),
                "dispatched": outbox_counts.get("dispatched", 0),
                "discarded": outbox_counts.get("discarded", 0),
                "delivery_unknown": outbox_counts.get("delivery_unknown", 0),
            },
        }


def list_dlq_summary(limit: int = 50) -> List[dict[str, Any]]:
    """Return DLQ operator fields only; payloads and error text are excluded."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, event_type, attempts, max_attempts, created_at, updated_at
            FROM events WHERE status = 'failed'
            ORDER BY updated_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
