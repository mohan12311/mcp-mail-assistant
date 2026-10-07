"""Mail input adapter abstraction for MCP Mail Assistant.

Provides a protocol-neutral interface over IMAP and POP3.
Only create_mail_adapter() knows which protocol is used.
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import logging
import poplib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import TYPE_CHECKING, Optional

from core.email_headers import (
    UnsafeEmailHeaderError,
    normalize_message_id,
    normalize_references,
    normalize_thread_topic,
)

if TYPE_CHECKING:
    from core.config import Config
    from core.imap_client import IMAPClient


logger = logging.getLogger(__name__)


def _safe_thread_headers(
    *,
    message_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    thread_topic: Optional[str] = None,
) -> dict[str, Optional[str]]:
    """Validate protocol header values before they enter the event/DB path."""
    validators = {
        "message_id": lambda value: normalize_message_id(value, name="Message-ID"),
        "in_reply_to": lambda value: normalize_message_id(value, name="In-Reply-To"),
        "references": normalize_references,
        "thread_topic": normalize_thread_topic,
    }
    result: dict[str, Optional[str]] = {}
    for name, value in {
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "thread_topic": thread_topic,
    }.items():
        if value is None or value == "":
            result[name] = None
            continue
        try:
            result[name] = validators[name](str(value))
        except UnsafeEmailHeaderError:
            logger.warning("Unsafe inbound thread header omitted: %s", name)
            result[name] = None
    return result


@dataclass
class FetchedMailMeta:
    """Lightweight mail header returned by fetch_new_emails(). Protocol-neutral."""
    uid: str
    message_id: Optional[str]
    uidl: Optional[str]       # POP3 UIDL; None for IMAP
    subject: str
    sender: str
    date: str
    sender_email: str = ""
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
    thread_topic: Optional[str] = None


@dataclass
class FetchedMailContent:
    """Full mail body returned by get_email_body()."""
    uid: str
    body: str
    attachments: list = field(default_factory=list)
    subject: str = ""
    sender: str = ""
    sender_email: str = ""
    date: str = ""
    message_id: Optional[str] = None
    uidl: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
    thread_topic: Optional[str] = None


@dataclass
class FetchedAttachment:
    """MIME attachment carried in memory until durable local processing."""
    filename: str
    content_type: str
    size: int
    content: Optional[bytes] = field(default=None, repr=False)
    ingest_status: str = "ready"
    ingest_error: Optional[str] = None
    part_index: int = 0


class MailAdapter(ABC):
    """Abstract mail input adapter. Concrete implementations: IMAPAdapter, POP3Adapter."""

    @abstractmethod
    def fetch_new_emails(self, limit: int = 10) -> list[FetchedMailMeta]:
        """Return metadata for new/unread messages."""

    @abstractmethod
    def get_email_body(self, uid: str) -> FetchedMailContent:
        """Return full content for a message by uid."""

    def delete_email(self, uid: str, uidl: Optional[str] = None) -> bool:
        """Delete a source message after durable local processing, if supported."""
        return False


class IMAPAdapter(MailAdapter):
    """Wraps the existing IMAPClient. Sets uidl=None (IMAP has no UIDL concept)."""

    def __init__(self, imap_client: "IMAPClient"):
        self._client = imap_client

    def fetch_new_emails(self, limit: int = 10) -> list[FetchedMailMeta]:
        # IMAPClient.list_emails() returns list[EmailInfo] (Pydantic models)
        # EmailInfo fields: email_id, subject, sender, sender_email, received_at,
        #                   has_attachment, is_read, snippet
        with self._client.connection():
            summaries = self._client.list_emails(limit=limit, only_unseen=True)
        result = []
        for summary in summaries:
            headers = _safe_thread_headers(
                message_id=getattr(summary, "message_id", None),
                in_reply_to=getattr(summary, "in_reply_to", None),
                references=getattr(summary, "references", None),
                thread_topic=getattr(summary, "thread_topic", None),
            )
            result.append(FetchedMailMeta(
                uid=summary.email_id,
                message_id=headers["message_id"],
                uidl=None,
                subject=summary.subject,
                sender=summary.sender,
                sender_email=getattr(summary, "sender_email", ""),
                date=str(summary.received_at),
                in_reply_to=headers["in_reply_to"],
                references=headers["references"],
                thread_topic=headers["thread_topic"],
            ))
        return result

    def get_email_body(self, uid: str) -> FetchedMailContent:
        # IMAPClient.read_email() returns EmailSummary (Pydantic model)
        # EmailSummary fields: email_id, subject, sender, body, attachments, message_id, ...
        with self._client.connection():
            content = self._client.read_email(uid)
        headers = _safe_thread_headers(
            message_id=getattr(content, "message_id", None),
            in_reply_to=getattr(content, "in_reply_to", None),
            references=getattr(content, "references", None),
            thread_topic=getattr(content, "thread_topic", None),
        )
        return FetchedMailContent(
            uid=uid,
            body=content.body,
            attachments=list(content.attachments),
            subject=content.subject,
            sender=content.sender,
            sender_email=content.sender_email,
            date=str(content.received_at),
            message_id=headers["message_id"],
            uidl=None,
            in_reply_to=headers["in_reply_to"],
            references=headers["references"],
            thread_topic=headers["thread_topic"],
        )


class POP3Adapter(MailAdapter):
    """POP3-based mail adapter. Never calls DELE unless config.pop_delete_after_download."""

    def __init__(self, config: "Config"):
        self._config = config

    def _connect(self):
        if self._config.pop_ssl:
            conn = poplib.POP3_SSL(self._config.pop_host, self._config.pop_port)
        else:
            conn = poplib.POP3(self._config.pop_host, self._config.pop_port)
        conn.user(self._config.pop_user)
        conn.pass_(self._config.pop_pass)
        return conn

    @staticmethod
    def _compute_hash(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def _parse_sender(raw_sender: str) -> tuple[str, str]:
        name, address = parseaddr(raw_sender or "")
        display_name = name or address or raw_sender or ""
        return display_name, address or ""

    @staticmethod
    def _extract_uidl(conn, message_num: int) -> Optional[str]:
        uidl_resp = conn.uidl(message_num).decode()
        return uidl_resp.split(" ", 2)[-1].strip() if " " in uidl_resp else None

    def _attachment_limit(self, name: str, default: int) -> int:
        value = getattr(self._config, name, default)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return default

    def _extract_attachments(self, msg) -> list[FetchedAttachment]:
        """Decode MIME attachments while enforcing bounded in-memory payloads."""
        max_file_bytes = self._attachment_limit(
            "attachment_max_file_bytes", 10 * 1024 * 1024
        )
        max_mail_bytes = self._attachment_limit(
            "attachment_max_mail_bytes", 25 * 1024 * 1024
        )
        attachments: list[FetchedAttachment] = []
        total_attachment_bytes = 0

        for part_index, part in enumerate(msg.walk(), 1):
            if part.is_multipart():
                continue
            filename = part.get_filename()
            disposition = part.get_content_disposition()
            if disposition != "attachment" and not filename:
                continue

            try:
                payload = part.get_payload(decode=True) or b""
            except Exception as exc:
                attachments.append(FetchedAttachment(
                    filename=str(filename or f"attachment-{part_index}"),
                    content_type=str(part.get_content_type() or "application/octet-stream"),
                    size=0,
                    content=None,
                    ingest_status="storage_failed",
                    ingest_error=f"MIME payload decode failed: {str(exc)[:500]}",
                    part_index=part_index,
                ))
                continue
            size = len(payload)
            total_attachment_bytes += size
            status = "ready"
            error = None
            content: Optional[bytes] = payload

            if size > max_file_bytes:
                status = "rejected_size"
                error = f"attachment exceeds per-file limit ({max_file_bytes} bytes)"
                content = None
            elif total_attachment_bytes > max_mail_bytes:
                status = "rejected_size"
                error = f"email attachments exceed per-mail limit ({max_mail_bytes} bytes)"
                content = None

            attachments.append(FetchedAttachment(
                filename=str(filename or f"attachment-{part_index}"),
                content_type=str(part.get_content_type() or "application/octet-stream"),
                size=size,
                content=content,
                ingest_status=status,
                ingest_error=error,
                part_index=part_index,
            ))

        return attachments

    def fetch_new_emails(self, limit: int = 10) -> list[FetchedMailMeta]:
        conn = self._connect()
        try:
            count, _ = conn.stat()
            msgs = []
            for i in range(max(1, count - limit + 1), count + 1):
                raw_lines = conn.retr(i)[1]
                raw = b"\r\n".join(raw_lines)
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                uidl = self._extract_uidl(conn, i)
                headers = _safe_thread_headers(
                    message_id=msg.get("Message-ID"),
                    in_reply_to=msg.get("In-Reply-To"),
                    references=msg.get("References"),
                    thread_topic=msg.get("Thread-Topic"),
                )
                sender, sender_email = self._parse_sender(str(msg.get("From", "")))
                msgs.append(FetchedMailMeta(
                    uid=str(i),
                    message_id=headers["message_id"],
                    uidl=uidl,
                    subject=str(msg.get("Subject", "")),
                    sender=sender,
                    sender_email=sender_email,
                    date=str(msg.get("Date", "")),
                    in_reply_to=headers["in_reply_to"],
                    references=headers["references"],
                    thread_topic=headers["thread_topic"],
                ))
            return msgs
        finally:
            try:
                conn.quit()
            except Exception:
                pass

    def get_email_body(self, uid: str) -> FetchedMailContent:
        conn = self._connect()
        try:
            raw_lines = conn.retr(int(uid))[1]
            raw = b"\r\n".join(raw_lines)
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            uidl = self._extract_uidl(conn, int(uid))
            headers = _safe_thread_headers(
                message_id=msg.get("Message-ID"),
                in_reply_to=msg.get("In-Reply-To"),
                references=msg.get("References"),
                thread_topic=msg.get("Thread-Topic"),
            )
            sender, sender_email = self._parse_sender(str(msg.get("From", "")))
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if (
                        part.get_content_type() == "text/plain"
                        and part.get_content_disposition() != "attachment"
                        and not part.get_filename()
                    ):
                        body = (part.get_payload(decode=True) or b"").decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                        break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode(
                        msg.get_content_charset() or "utf-8", errors="replace"
                    )
            content = FetchedMailContent(
                uid=uid,
                body=body,
                attachments=self._extract_attachments(msg),
                subject=str(msg.get("Subject", "")),
                sender=sender,
                sender_email=sender_email,
                date=str(msg.get("Date", "")),
                message_id=headers["message_id"],
                uidl=uidl,
                in_reply_to=headers["in_reply_to"],
                references=headers["references"],
                thread_topic=headers["thread_topic"],
            )
            return content
        finally:
            try:
                conn.quit()
            except Exception:
                pass

    def delete_email(self, uid: str, uidl: Optional[str] = None) -> bool:
        conn = self._connect()
        try:
            message_num = int(uid)
            if uidl:
                _resp, listings, _octets = conn.uidl()
                for item in listings:
                    text = item.decode() if isinstance(item, bytes) else str(item)
                    parts = text.split(" ", 1)
                    if len(parts) == 2 and parts[1].strip() == uidl:
                        message_num = int(parts[0])
                        break
                else:
                    return False
            conn.dele(message_num)
            return True
        finally:
            try:
                conn.quit()
            except Exception:
                pass


def create_mail_adapter(config: "Config") -> MailAdapter:
    """Factory: returns POP3Adapter when POP_HOST is set, else IMAPAdapter."""
    if config.pop_host:
        return POP3Adapter(config)
    from core.imap_client import IMAPClient
    return IMAPAdapter(IMAPClient(config))
