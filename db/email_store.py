"""
emails 테이블 CRUD 함수

메일 정보를 저장하고 조회합니다.
"""

import logging
from typing import Optional, List
from datetime import datetime
from db.connection import get_connection

logger = logging.getLogger(__name__)


def save_email(email: dict) -> str:
    """
    메일을 저장합니다.
    
    Args:
        email: 메일 정보 dict
            - id (str, required): 메일 고유 ID
            - message_id (str): Message-ID 헤더
            - in_reply_to (str): In-Reply-To 헤더
            - references_header (str): References 헤더
            - thread_topic (str): Outlook Thread-Topic 헤더
            - subject (str, required): 제목
            - sender (str, required): 발신자 이름
            - sender_email (str, required): 발신자 이메일
            - received_at (str, required): 수신 시각 (ISO 8601)
            - body_text (str): 본문
            - body_summary (str): 본문 요약
            - priority_score (int): 우선순위 점수
            - priority_level (str): 우선순위 레벨
            - urgency (str): 긴급도
            - has_attachments (bool): 첨부파일 여부
            - requires_response (bool): 답장 필요 여부
            - folder (str): 폴더명
    
    Returns:
        str: 저장된 메일 ID
        
    Raises:
        Exception: DB 저장 실패 시
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO emails (
                    id, message_id, in_reply_to, references_header, thread_topic,
                    uidl, subject, sender, sender_email, received_at,
                    body_text, body_summary, priority_score, priority_level, urgency,
                    has_attachments, requires_response, folder
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                email["id"],
                email.get("message_id"),
                email.get("in_reply_to"),
                email.get("references_header"),
                email.get("thread_topic"),
                email.get("uidl"),
                email["subject"],
                email["sender"],
                email["sender_email"],
                email["received_at"],
                email.get("body_text"),
                email.get("body_summary"),
                email.get("priority_score", 0),
                email.get("priority_level", "low"),
                email.get("urgency", "normal"),
                email.get("has_attachments", False),
                email.get("requires_response", False),
                email.get("folder", "INBOX")
            ))
            
            logger.info(f"메일 저장 완료: {email['id']}")
            return email["id"]
            
    except Exception as e:
        logger.error(f"메일 저장 실패: {e}")
        raise


def get_email(email_id: str) -> Optional[dict]:
    """
    메일을 ID로 조회합니다.
    
    Args:
        email_id: 메일 ID
        
    Returns:
        dict | None: 메일 정보 또는 None
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM emails WHERE id = ?", (email_id,))
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            return None
            
    except Exception as e:
        logger.error(f"메일 조회 실패: {e}")
        raise


def get_unprocessed_emails(limit: int = 20) -> List[dict]:
    """
    미처리 메일 목록을 조회합니다.
    
    Args:
        limit: 최대 조회 개수
        
    Returns:
        list[dict]: 미처리 메일 목록 (수신 시각 내림차순)
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM emails 
                WHERE is_processed = 0 
                ORDER BY received_at DESC 
                LIMIT ?
            """, (limit,))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
    except Exception as e:
        logger.error(f"미처리 메일 조회 실패: {e}")
        raise


def mark_as_processed(email_id: str) -> bool:
    """
    메일을 처리 완료로 표시합니다.
    
    Args:
        email_id: 메일 ID
        
    Returns:
        bool: 성공 여부
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE emails 
                SET is_processed = 1 
                WHERE id = ?
            """, (email_id,))
            
            success = cursor.rowcount > 0
            if success:
                logger.info(f"메일 처리 완료 표시: {email_id}")
            return success
            
    except Exception as e:
        logger.error(f"메일 처리 완료 표시 실패: {e}")
        raise


def mark_as_notified(email_id: str, slack_ts: str) -> bool:
    """
    메일을 Slack 알림 완료로 표시합니다.
    
    Args:
        email_id: 메일 ID
        slack_ts: Slack 메시지 타임스탬프
        
    Returns:
        bool: 성공 여부
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE emails 
                SET slack_notified = 1, slack_ts = ? 
                WHERE id = ?
            """, (slack_ts, email_id))
            
            success = cursor.rowcount > 0
            if success:
                logger.info(f"메일 알림 완료 표시: {email_id}")
            return success
            
    except Exception as e:
        logger.error(f"메일 알림 완료 표시 실패: {e}")
        raise


def exists(email_id: str) -> bool:
    """
    메일이 이미 존재하는지 확인합니다 (중복 체크용).
    
    Args:
        email_id: 메일 ID
        
    Returns:
        bool: 존재 여부
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM emails WHERE id = ? LIMIT 1", (email_id,))
            return cursor.fetchone() is not None
            
    except Exception as e:
        logger.error(f"메일 존재 확인 실패: {e}")
        raise


def get_emails_since(since: datetime, limit: int = 100) -> List[dict]:
    """
    특정 시각 이후의 메일 목록을 조회합니다.
    
    Args:
        since: 시작 시각
        limit: 최대 조회 개수
        
    Returns:
        list[dict]: 메일 목록 (수신 시각 내림차순)
    """
    try:
        since_str = since.isoformat()
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM emails 
                WHERE received_at >= ? 
                ORDER BY received_at DESC 
                LIMIT ?
            """, (since_str, limit))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
    except Exception as e:
        logger.error(f"시각 이후 메일 조회 실패: {e}")
        raise


class EmailStore:
    """
    호환용 Store 래퍼.

    일부 코드에서 `EmailStore().get_connection()` 패턴을 기대하므로,
    모듈 함수 기반 CRUD를 유지하면서 최소 래퍼 클래스를 제공합니다.
    """

    def get_connection(self):
        return get_connection()
