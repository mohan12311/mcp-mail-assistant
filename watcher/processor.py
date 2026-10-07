"""
이벤트 처리기

메일 이벤트를 받아 DB에 저장하고 상태를 업데이트합니다.
"""
import logging
from typing import List, Dict, Optional
from datetime import datetime

from watcher.event_queue import EmailEvent
from watcher.dedupe import DedupeChecker
from db import email_store, attachment_store, state_store, event_store
from db.event_store import EventType

logger = logging.getLogger(__name__)


class EventProcessor:
    """메일 이벤트 처리"""
    
    def __init__(self, dedupe_checker: DedupeChecker):
        """
        이벤트 처리기 초기화
        
        Args:
            dedupe_checker: 중복 체커 인스턴스
        """
        self.dedupe = dedupe_checker
        logger.info("EventProcessor 초기화 완료")
    
    def process_event(self, event: EmailEvent) -> bool:
        """
        단일 이벤트를 처리합니다.
        
        Args:
            event: 처리할 이벤트
            
        Returns:
            bool: 성공 여부
        """
        email_data = event.email_data
        try:
            email_id = event.email_id
            
            # 1. 중복 체크
            if self.dedupe.is_duplicate(email_id):
                logger.info(f"중복 메일 스킵: {email_id}")
                return False
            
            # Message-ID / UIDL로도 중복 체크
            message_id = email_data.get("message_id") or email_data.get("thread_info", {}).get("message_id")
            uidl = email_data.get("uidl")
            if message_id and self.dedupe.is_duplicate_by_message_id(message_id):
                logger.info(f"중복 메일 스킵 (Message-ID): {email_id}")
                return False
            if uidl and self.dedupe.is_duplicate_by_uidl(uidl):
                logger.info(f"중복 메일 스킵 (UIDL): {email_id}")
                return False
            
            # 2. 메일 데이터 변환
            email_record = self._convert_to_email_record(email_data)
            
            # 3. DB에 메일 저장
            email_store.save_email(email_record)
            logger.info(f"메일 저장 완료: {email_id}")
            
            # 4. 첨부파일 메타데이터 저장
            attachments_durable = True
            if email_data.get("has_attachment") and "attachments" in email_data:
                attachments_durable = self._save_attachment_metadata(
                    email_id,
                    email_data["attachments"],
                    source_protocol=email_data.get("source_protocol"),
                )

            # 4.5. Operator 이벤트는 첨부 상태/추출문이 DB에 기록된 뒤 생성한다.
            event_payload = {
                "email_id": email_id,
                "subject": email_record.get("subject"),
                "sender": email_record.get("sender"),
                "sender_email": email_record.get("sender_email"),
                "has_attachments": email_record.get("has_attachments", False),
                "received_at": email_record.get("received_at"),
            }
            event_store.create_event(
                event_type=EventType.NEW_EMAIL,
                payload=event_payload,
                source_id=email_id,
            )
            logger.info(f"new_email 이벤트 확인: {email_id}")
            
            # 5. 중복 체크 캐시에 추가
            self.dedupe.mark_seen(email_id)
            if message_id:
                self.dedupe.mark_seen(f"msg:{message_id}")
            if uidl:
                self.dedupe.mark_seen(f"uidl:{uidl}")
            
            # 6. 상태 커서 업데이트
            state_store.set_last_processed_email_id(email_id)

            # 7. POP 삭제 옵션은 로컬 저장/이벤트 생성이 끝난 뒤에만 실행한다.
            if attachments_durable:
                self._delete_source_after_processed(email_data)
            elif email_data.get("delete_after_processed"):
                logger.warning(
                    "첨부파일이 안전하게 보존되지 않아 원본 POP 메일 삭제를 건너뜁니다: %s",
                    email_id,
                )
            
            return True
            
        except Exception as e:
            logger.error(f"이벤트 처리 실패: {event} - {e}", exc_info=True)
            return False
        finally:
            for attachment in email_data.get("attachments", []):
                if isinstance(attachment, dict):
                    attachment.pop("content", None)
    
    def process_batch(self, events: List[EmailEvent]) -> Dict[str, int]:
        """
        여러 이벤트를 일괄 처리합니다.
        
        Args:
            events: 처리할 이벤트 리스트
            
        Returns:
            dict: {"success": int, "skipped": int, "failed": int}
        """
        result = {"success": 0, "skipped": 0, "failed": 0}
        
        for event in events:
            try:
                # process_event가 중복 확인과 POP payload 해제를 함께 담당한다.
                if self.process_event(event):
                    result["success"] += 1
                else:
                    result["skipped"] += 1
                    
            except Exception as e:
                logger.error(f"배치 처리 중 에러: {event} - {e}")
                result["failed"] += 1
        
        logger.info(
            f"배치 처리 완료: "
            f"success={result['success']}, "
            f"skipped={result['skipped']}, "
            f"failed={result['failed']}"
        )
        
        return result
    
    def _convert_to_email_record(self, email_data: dict) -> dict:
        """
        메일 데이터를 DB 레코드 형식으로 변환합니다.
        
        Args:
            email_data: 원본 메일 데이터
            
        Returns:
            dict: DB 레코드
        """
        # thread_info에서 message_id 추출
        thread_info = email_data.get("thread_info", {})
        message_id = thread_info.get("message_id") or email_data.get("message_id")
        in_reply_to = thread_info.get("in_reply_to") or email_data.get("in_reply_to")
        references = thread_info.get("references") or email_data.get("references")
        thread_topic = thread_info.get("thread_topic") or email_data.get("thread_topic")
        
        # 날짜 변환
        received_at = email_data.get("date")
        if isinstance(received_at, str):
            # ISO 형식 유지
            pass
        elif isinstance(received_at, datetime):
            received_at = received_at.isoformat()
        else:
            # 기본값: 현재 시각
            received_at = datetime.now().isoformat()
        
        return {
            "id": email_data["id"],
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "references_header": references,
            "thread_topic": thread_topic,
            "uidl": email_data.get("uidl"),
            "subject": email_data.get("subject", "(제목 없음)"),
            "sender": email_data.get("sender", ""),
            "sender_email": email_data.get("sender_email") or "",
            "received_at": received_at,
            "body_text": email_data.get("body") or email_data.get("body_full"),
            "body_summary": None,  # Phase 3에서 Agent가 생성
            "priority_score": 0,
            "priority_level": "low",
            "urgency": "normal",
            "has_attachments": email_data.get("has_attachment", False),
            "requires_response": False,
            "folder": "INBOX",
        }
    
    def _save_attachment_metadata(
        self,
        email_id: str,
        attachments: List[dict],
        source_protocol: Optional[str] = None,
    ) -> bool:
        """
        첨부파일 메타데이터를 저장합니다.
        
        Args:
            email_id: 메일 ID
            attachments: 첨부파일 목록
        """
        all_durable = True
        for att in attachments:
            try:
                filename = att.get("filename", "unknown")
                if source_protocol == "pop3":
                    from core.attachment_pipeline import sanitize_attachment_filename

                    filename = sanitize_attachment_filename(
                        filename,
                        int(att.get("part_index") or 0),
                    )
                attachment_record = {
                    "email_id": email_id,
                    "filename": filename,
                    "file_type": att.get("content_type"),
                    "file_size": att.get("size"),
                    "local_path": None,
                    "extracted_text": None,
                    "key_points": None,
                    "is_processed": False,
                    "processing_status": (
                        "pending"
                        if att.get("ingest_status", "ready") == "ready"
                        else att.get("ingest_status")
                    ),
                    "processing_error": att.get("ingest_error"),
                }

                attachment_id = attachment_store.save_attachment(attachment_record)
                if source_protocol == "pop3":
                    from core.attachment_pipeline import process_pop3_attachment
                    from skills.mail_skills import get_config

                    result = process_pop3_attachment(
                        email_id,
                        att,
                        get_config().attachment_dir,
                    )
                    if not attachment_store.update_processing_result(attachment_id, result):
                        raise RuntimeError("첨부파일 처리 결과 DB 업데이트 대상이 없습니다")
                    all_durable = all_durable and bool(result.get("local_path"))
                logger.debug("첨부파일 메타데이터 저장: id=%s", attachment_id)
                
            except Exception as e:
                all_durable = False
                logger.error(
                    "첨부파일 처리 실패: email_id=%s error=%s",
                    email_id,
                    e,
                )
        return all_durable

    def _delete_source_after_processed(self, email_data: dict) -> None:
        if not email_data.get("delete_after_processed"):
            return

        try:
            from skills.mail_skills import delete_email_after_processed
            deleted = delete_email_after_processed(
                email_data.get("source_uid") or email_data["id"],
                uidl=email_data.get("uidl"),
            )
            if deleted:
                logger.info(f"원본 POP 메일 삭제 완료: {email_data['id']}")
            else:
                logger.warning(f"원본 POP 메일 삭제 대상 없음: {email_data['id']}")
        except Exception as e:
            logger.warning(f"원본 POP 메일 삭제 실패: {email_data.get('id')} - {e}", exc_info=True)
