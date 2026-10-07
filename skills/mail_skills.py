"""
MCP Mail Assistant - 메일 관련 Skills
list_emails, read_email, list_emails_with_body (권장)
"""
import hashlib
from typing import Optional

from core import Config
from core.mail_adapter import create_mail_adapter
from tools.filter import strip_reply_and_signature


# 글로벌 설정 (초기화 시 설정)
_config: Optional[Config] = None


def init_config(config: Config) -> None:
    """설정 초기화"""
    global _config
    _config = config


def get_config() -> Config:
    """현재 설정 반환"""
    if _config is None:
        raise RuntimeError("Config not initialized. Call init_config() first.")
    return _config


def list_emails(
    folder: str = "INBOX",
    limit: int = 10,
    only_unseen: bool = True
) -> list[dict]:
    """
    받은 메일 목록 조회
    
    Args:
        folder: 메일함 이름 (기본: INBOX)
        limit: 조회할 메일 수 (기본: 10)
        only_unseen: 안 읽은 메일만 (기본: True)
    
    Returns:
        메일 목록 [{id, subject, sender, date, has_attachment}, ...]
    """
    config = get_config()
    adapter = create_mail_adapter(config)
    emails = adapter.fetch_new_emails(limit=limit)

    return [
        {
            "id": e.uid,
            "subject": e.subject,
            "sender": e.sender,
            "sender_email": e.sender_email,
            "message_id": e.message_id,
            "in_reply_to": e.in_reply_to,
            "references": e.references,
            "thread_topic": e.thread_topic,
            "uidl": e.uidl,
            "date": e.date,
            "has_attachment": False,
            "is_read": not only_unseen,
        }
        for e in emails
    ]


def read_email(email_id: str, folder: str = "INBOX") -> dict:
    """
    특정 메일 상세 읽기
    
    Args:
        email_id: 메일 ID
        folder: 메일함 이름 (기본: INBOX)
    
    Returns:
        메일 상세 정보 {subject, sender, date, body, attachments[], thread_info}
    """
    config = get_config()
    adapter = create_mail_adapter(config)
    content = adapter.get_email_body(email_id)

    # 본문에서 인용/서명 제거
    clean_body = strip_reply_and_signature(content.body)

    return {
        "id": content.uid,
        "subject": content.subject,
        "sender": content.sender,
        "sender_email": content.sender_email,
        "message_id": content.message_id,
        "in_reply_to": content.in_reply_to,
        "references": content.references,
        "thread_topic": content.thread_topic,
        "uidl": content.uidl,
        "date": content.date,
        "body": clean_body,
        "body_full": content.body,  # 원본 본문
        "attachments": [
            _attachment_dict(a, include_content=False)
            for a in content.attachments
        ],
        "thread_info": {
            "message_id": content.message_id,
            "in_reply_to": content.in_reply_to,
            "references": content.references,
            "thread_topic": content.thread_topic,
        }
    }


def delete_email_after_processed(email_id: str, uidl: Optional[str] = None) -> bool:
    """Delete a source message only after the processor has saved it locally."""
    config = get_config()
    adapter = create_mail_adapter(config)
    return adapter.delete_email(email_id, uidl=uidl)


def _storage_email_id(config: Config, email_meta, email_content) -> str:
    """POP message number와 분리된 재현 가능한 로컬 저장 ID를 만듭니다."""
    if not config.pop_host:
        return str(email_meta.uid)
    identity = (
        email_content.uidl
        or email_meta.uidl
        or email_content.message_id
        or email_meta.message_id
        or "|".join((
            email_content.sender_email or email_meta.sender_email or "",
            email_content.date or email_meta.date or "",
            email_content.subject or email_meta.subject or "",
            email_content.body or "",
        ))
    )
    digest = hashlib.sha256(str(identity).encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"pop-{digest}"


def _attachment_dict(attachment, include_content: bool) -> dict:
    """Serialize attachment metadata, keeping payload bytes on the internal POP path."""
    result = {
        "filename": getattr(attachment, "filename", None),
        "content_type": getattr(attachment, "content_type", None),
        "size": getattr(attachment, "size", None),
        "ingest_status": getattr(attachment, "ingest_status", "pending"),
        "ingest_error": getattr(attachment, "ingest_error", None),
        "part_index": getattr(attachment, "part_index", 0),
    }
    if include_content:
        content = getattr(attachment, "content", None)
        if isinstance(content, bytes):
            result["content"] = content
    return result


def list_emails_with_body(
    folder: str = "INBOX",
    limit: int = 10,
    only_unseen: bool = True,
    max_body_length: int = 3000
) -> list[dict]:
    """
    메일 목록과 본문을 함께 조회 (Agent 분석용)
    
    Args:
        folder: 메일함 이름 (기본: INBOX)
        limit: 조회할 메일 수 (기본: 10)
        only_unseen: 안 읽은 메일만 (기본: True)
        max_body_length: 본문 최대 길이 (기본: 3000)
    
    Returns:
        메일 목록 [{id, subject, sender, sender_email, date, body, has_attachment, attachments[], is_newsletter}, ...]
        
    Agent는 반환된 데이터를 직접 분석하여:
    - 각 메일의 요약 생성
    - 긴급도 판단 (urgent/normal/low)
    - Action Items 추출
    - 답장 필요 여부 판단
    """
    config = get_config()
    adapter = create_mail_adapter(config)

    results = []
    email_list = adapter.fetch_new_emails(limit=limit)

    for email_meta in email_list:
        # 상세 정보 읽기
        email_content = adapter.get_email_body(email_meta.uid)
        storage_email_id = _storage_email_id(config, email_meta, email_content)

        # 본문에서 인용/서명 제거
        clean_body = strip_reply_and_signature(email_content.body)

        # 본문 길이 제한
        truncated_body = clean_body[:max_body_length]
        if len(clean_body) > max_body_length:
            truncated_body += f"\n\n... (이하 {len(clean_body) - max_body_length}자 생략)"

        # 뉴스레터 여부 체크
        is_newsletter = _is_likely_newsletter(clean_body)

        results.append({
            "id": storage_email_id,
            "source_uid": email_content.uid or email_meta.uid,
            "subject": email_content.subject or email_meta.subject,
            "sender": email_content.sender or email_meta.sender,
            "sender_email": email_content.sender_email or email_meta.sender_email,
            "message_id": email_content.message_id or email_meta.message_id,
            "in_reply_to": email_content.in_reply_to or email_meta.in_reply_to,
            "references": email_content.references or email_meta.references,
            "thread_topic": email_content.thread_topic or email_meta.thread_topic,
            "uidl": email_content.uidl or email_meta.uidl,
            "date": email_content.date or email_meta.date,
            "body": truncated_body,
            "body_length": len(clean_body),
            "has_attachment": len(email_content.attachments) > 0,
            "attachments": [
                _attachment_dict(a, include_content=bool(config.pop_host))
                for a in email_content.attachments
            ],
            "is_read": not only_unseen,
            "is_newsletter": is_newsletter,
            "source_protocol": "pop3" if config.pop_host else "imap",
            "delete_after_processed": bool(config.pop_host and config.pop_delete_after_download),
        })

    return results


def _is_likely_newsletter(body: str) -> bool:
    """간단한 뉴스레터 체크"""
    keywords = [
        "unsubscribe", "配信停止", "配信解除", "メルマガ", 
        "ニュースレター", "このメールは", "advertisement"
    ]
    body_lower = body.lower()
    return any(k.lower() in body_lower for k in keywords)
