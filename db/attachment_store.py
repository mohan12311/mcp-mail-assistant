"""
attachments 테이블 CRUD 함수

첨부파일 정보를 저장하고 조회합니다.
"""

import logging
from typing import Optional, List
from db.connection import get_connection

logger = logging.getLogger(__name__)


def save_attachment(attachment: dict) -> int:
    """
    첨부파일 정보를 저장합니다.
    
    Args:
        attachment: 첨부파일 정보 dict
            - email_id (str, required): 연관된 메일 ID
            - filename (str, required): 파일명
            - file_type (str): 파일 타입
            - file_size (int): 파일 크기 (bytes)
            - local_path (str): 로컬 저장 경로
            - extracted_text (str): 추출된 텍스트
            - key_points (str): 핵심 포인트
            - is_processed (bool): 처리 완료 여부
            - detected_mime (str): 파일 시그니처로 판별한 MIME
            - content_sha256 (str): 저장 content 해시
            - processing_status (str): 저장/추출 처리 상태
            - processing_error (str): 격리된 처리 오류
    
    Returns:
        int: 생성된 첨부파일 ID
        
    Raises:
        Exception: DB 저장 실패 시
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO attachments (
                    email_id, filename, file_type, file_size, local_path,
                    extracted_text, key_points, is_processed, detected_mime,
                    content_sha256, processing_status, processing_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                attachment["email_id"],
                attachment["filename"],
                attachment.get("file_type"),
                attachment.get("file_size"),
                attachment.get("local_path"),
                attachment.get("extracted_text"),
                attachment.get("key_points"),
                attachment.get("is_processed", False),
                attachment.get("detected_mime"),
                attachment.get("content_sha256"),
                attachment.get("processing_status", "pending"),
                attachment.get("processing_error"),
            ))
            
            attachment_id = cursor.lastrowid
            logger.info("첨부파일 저장 완료: %s", attachment_id)
            return attachment_id
            
    except Exception as e:
        logger.error(f"첨부파일 저장 실패: {e}")
        raise


def get_attachments_by_email(email_id: str) -> List[dict]:
    """
    특정 메일의 첨부파일 목록을 조회합니다.
    
    Args:
        email_id: 메일 ID
        
    Returns:
        list[dict]: 첨부파일 목록
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM attachments 
                WHERE email_id = ? 
                ORDER BY created_at DESC
            """, (email_id,))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
    except Exception as e:
        logger.error(f"첨부파일 조회 실패: {e}")
        raise


def get_attachments_for_email(email_id: str) -> List[dict]:
    """호환용 alias: 기존 MCP/Operator 호출명을 유지합니다."""
    return get_attachments_by_email(email_id)


def update_processing_result(attachment_id: int, result: dict) -> bool:
    """Persist the terminal storage/extraction state for one attachment."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE attachments
            SET filename = ?, local_path = ?, extracted_text = ?, key_points = ?,
                is_processed = ?, detected_mime = ?, content_sha256 = ?,
                processing_status = ?, processing_error = ?
            WHERE id = ?
        """, (
            result["filename"],
            result.get("local_path"),
            result.get("extracted_text"),
            result.get("key_points"),
            result.get("is_processed", False),
            result.get("detected_mime"),
            result.get("content_sha256"),
            result.get("processing_status", "pending"),
            result.get("processing_error"),
            attachment_id,
        ))
        return cursor.rowcount > 0


def mark_as_processed(
    attachment_id: int, 
    extracted_text: str, 
    key_points: str
) -> bool:
    """
    첨부파일을 처리 완료로 표시하고 추출 결과를 저장합니다.
    
    Args:
        attachment_id: 첨부파일 ID
        extracted_text: 추출된 텍스트
        key_points: 핵심 포인트
        
    Returns:
        bool: 성공 여부
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE attachments 
                SET is_processed = 1,
                    extracted_text = ?,
                    key_points = ?,
                    processing_status = 'extracted',
                    processing_error = NULL
                WHERE id = ?
            """, (extracted_text, key_points, attachment_id))
            
            success = cursor.rowcount > 0
            if success:
                logger.info(f"첨부파일 처리 완료 표시: {attachment_id}")
            return success
            
    except Exception as e:
        logger.error(f"첨부파일 처리 완료 표시 실패: {e}")
        raise


def get_unprocessed_attachments() -> List[dict]:
    """
    미처리 첨부파일 목록을 조회합니다.
    
    Returns:
        list[dict]: 미처리 첨부파일 목록
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM attachments 
                WHERE is_processed = 0 
                ORDER BY created_at DESC
            """)
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
    except Exception as e:
        logger.error(f"미처리 첨부파일 조회 실패: {e}")
        raise
