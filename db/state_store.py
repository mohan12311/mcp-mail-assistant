"""
processing_state 테이블 CRUD 함수

처리 상태 및 커서 정보를 저장하고 조회합니다.
"""

import logging
from typing import Optional
from datetime import datetime
from db.connection import get_connection

logger = logging.getLogger(__name__)


# 예약된 키 상수
KEY_LAST_PROCESSED_EMAIL_ID = "last_processed_email_id"
KEY_LAST_POLL_TIME = "last_poll_time"
KEY_WATCHER_STATUS = "watcher_status"


def set_state(key: str, value: str) -> bool:
    """
    상태 값을 설정합니다.
    
    Args:
        key: 상태 키
        value: 상태 값
        
    Returns:
        bool: 성공 여부
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO processing_state (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            """, (key, value))
            
            success = cursor.rowcount > 0
            if success:
                logger.debug(f"상태 저장: {key} = {value}")
            return success
            
    except Exception as e:
        logger.error(f"상태 저장 실패: {e}")
        raise


def get_state(key: str) -> Optional[str]:
    """
    상태 값을 조회합니다.
    
    Args:
        key: 상태 키
        
    Returns:
        str | None: 상태 값 또는 None
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value FROM processing_state 
                WHERE key = ?
            """, (key,))
            
            row = cursor.fetchone()
            return row["value"] if row else None
            
    except Exception as e:
        logger.error(f"상태 조회 실패: {e}")
        raise


def get_last_processed_email_id() -> Optional[str]:
    """
    마지막으로 처리한 메일 ID를 조회합니다.
    
    Returns:
        str | None: 마지막 메일 ID 또는 None
    """
    return get_state(KEY_LAST_PROCESSED_EMAIL_ID)


def set_last_processed_email_id(email_id: str) -> bool:
    """
    마지막으로 처리한 메일 ID를 저장합니다.
    
    Args:
        email_id: 메일 ID
        
    Returns:
        bool: 성공 여부
    """
    return set_state(KEY_LAST_PROCESSED_EMAIL_ID, email_id)


def get_last_poll_time() -> Optional[datetime]:
    """
    마지막 폴링 시각을 조회합니다.
    
    Returns:
        datetime | None: 마지막 폴링 시각 또는 None
    """
    value = get_state(KEY_LAST_POLL_TIME)
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError as e:
            logger.error(f"폴링 시각 파싱 실패: {e}")
            return None
    return None


def set_last_poll_time(dt: datetime) -> bool:
    """
    마지막 폴링 시각을 저장합니다.
    
    Args:
        dt: 폴링 시각
        
    Returns:
        bool: 성공 여부
    """
    return set_state(KEY_LAST_POLL_TIME, dt.isoformat())


def get_watcher_status() -> Optional[str]:
    """
    Watcher 상태를 조회합니다.
    
    Returns:
        str | None: 상태 (running/stopped/error) 또는 None
    """
    return get_state(KEY_WATCHER_STATUS)


def set_watcher_status(status: str) -> bool:
    """
    Watcher 상태를 저장합니다.
    
    Args:
        status: 상태 (running/stopped/error)
        
    Returns:
        bool: 성공 여부
    """
    return set_state(KEY_WATCHER_STATUS, status)

