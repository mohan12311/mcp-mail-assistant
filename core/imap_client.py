"""
MCP Mail Assistant - IMAP 클라이언트
메일 서버 연결 및 메일 읽기 기능
"""
from __future__ import annotations

import imaplib
import email
import email.message
import os
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime
from typing import Optional
from contextlib import contextmanager

from .config import Config
from .models import EmailInfo, EmailSummary, Attachment


def decode_mime_header(header: str | None) -> str:
    """MIME 인코딩된 헤더를 디코딩"""
    if not header:
        return ""
    parts = decode_header(header)
    result = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            result += text.decode(enc or "utf-8", errors="replace")
        else:
            result += text
    return result


def parse_email_address(header: str) -> tuple[str, str]:
    """이메일 헤더에서 이름과 주소 추출"""
    decoded = decode_mime_header(header)
    # "Name <email@example.com>" 형식 파싱
    if "<" in decoded and ">" in decoded:
        name = decoded.split("<")[0].strip().strip('"')
        email_addr = decoded.split("<")[1].split(">")[0].strip()
    else:
        name = ""
        email_addr = decoded.strip()
    return name or email_addr, email_addr


class IMAPClient:
    """IMAP 메일 클라이언트"""
    
    def __init__(self, config: Config):
        self.config = config
        self._connection: Optional[imaplib.IMAP4_SSL] = None
    
    def connect(self) -> None:
        """IMAP 서버에 연결"""
        self._connection = imaplib.IMAP4_SSL(
            self.config.imap_host, 
            self.config.imap_port
        )
        self._connection.login(self.config.imap_user, self.config.imap_pass)
    
    def disconnect(self) -> None:
        """연결 종료"""
        if self._connection:
            try:
                self._connection.logout()
            except:
                pass
            self._connection = None
    
    @contextmanager
    def connection(self):
        """컨텍스트 매니저로 연결 관리"""
        self.connect()
        try:
            yield self
        finally:
            self.disconnect()
    
    @property
    def conn(self) -> imaplib.IMAP4_SSL:
        """현재 연결 반환"""
        if not self._connection:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._connection
    
    def list_emails(
        self, 
        folder: str = "INBOX",
        limit: int = 10,
        only_unseen: bool = True
    ) -> list[EmailInfo]:
        """메일 목록 조회"""
        self.conn.select(folder)
        
        criteria = "UNSEEN" if only_unseen else "ALL"
        _, data = self.conn.search(None, criteria)
        
        ids = data[0].split()
        # 최신순으로 limit개
        ids = ids[-limit:][::-1]
        
        results = []
        for mid in ids:
            _, msg_data = self.conn.fetch(mid, "(RFC822.HEADER FLAGS)")
            
            # 헤더만 파싱
            header_data = msg_data[0][1]
            msg = email.message_from_bytes(header_data)
            
            # FLAGS에서 읽음 여부 확인
            flags_data = msg_data[0][0].decode()
            is_read = "\\Seen" in flags_data
            
            sender_name, sender_email = parse_email_address(msg.get("From"))
            
            date_raw = msg.get("Date")
            try:
                received_at = parsedate_to_datetime(date_raw) if date_raw else datetime.now()
            except:
                received_at = datetime.now()
            
            results.append(EmailInfo(
                email_id=mid.decode(),
                subject=decode_mime_header(msg.get("Subject")),
                sender=sender_name,
                sender_email=sender_email,
                received_at=received_at,
                has_attachment=False,  # 헤더만으로는 정확히 알 수 없음
                is_read=is_read,
                message_id=msg.get("Message-ID"),
                in_reply_to=msg.get("In-Reply-To"),
                references=msg.get("References"),
                thread_topic=decode_mime_header(msg.get("Thread-Topic")),
            ))
        
        return results
    
    def read_email(self, email_id: str, folder: str = "INBOX") -> EmailSummary:
        """특정 메일 상세 읽기"""
        self.conn.select(folder)
        
        _, msg_data = self.conn.fetch(email_id.encode(), "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        
        sender_name, sender_email = parse_email_address(msg.get("From"))
        
        date_raw = msg.get("Date")
        try:
            received_at = parsedate_to_datetime(date_raw) if date_raw else datetime.now()
        except:
            received_at = datetime.now()
        
        # 본문 추출
        body = self._extract_body(msg)
        
        # 첨부파일 목록
        attachments = self._extract_attachments(msg)
        
        return EmailSummary(
            email_id=email_id,
            subject=decode_mime_header(msg.get("Subject")),
            sender=sender_name,
            sender_email=sender_email,
            received_at=received_at,
            body=body,
            attachments=attachments,
            message_id=msg.get("Message-ID"),
            in_reply_to=msg.get("In-Reply-To"),
            references=msg.get("References"),
            thread_topic=decode_mime_header(msg.get("Thread-Topic")),
        )
    
    def _extract_body(self, msg: email.message.Message) -> str:
        """메일 본문 추출 (text/plain 우선)"""
        if msg.is_multipart():
            # text/plain 찾기
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition", ""))
                
                if "attachment" in disp.lower():
                    continue
                    
                if ctype == "text/plain":
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace") if payload else ""
            
            # text/plain 없으면 text/html
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    # HTML 태그 간단히 제거
                    import re
                    html = payload.decode(charset, errors="replace") if payload else ""
                    return re.sub(r'<[^>]+>', '', html)
            
            return ""
        
        # 단일 파트
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else ""
    
    def _extract_attachments(self, msg: email.message.Message) -> list[Attachment]:
        """첨부파일 정보 추출"""
        attachments = []
        
        if not msg.is_multipart():
            return attachments
        
        for part in msg.walk():
            disp = str(part.get("Content-Disposition", ""))
            
            if "attachment" in disp.lower() or "inline" in disp.lower():
                filename = part.get_filename()
                if filename:
                    filename = decode_mime_header(filename)
                    content_type = part.get_content_type()
                    payload = part.get_payload(decode=True)
                    size = len(payload) if payload else 0
                    
                    attachments.append(Attachment(
                        filename=filename,
                        content_type=content_type,
                        size=size,
                    ))
        
        return attachments
    
    def download_attachment(
        self, 
        email_id: str, 
        filename: str, 
        save_dir: str | None = None,
        folder: str = "INBOX"
    ) -> str | None:
        """첨부파일 다운로드"""
        save_dir = save_dir or self.config.attachment_dir
        os.makedirs(save_dir, exist_ok=True)
        
        self.conn.select(folder)
        _, msg_data = self.conn.fetch(email_id.encode(), "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        
        for part in msg.walk():
            part_filename = part.get_filename()
            if part_filename:
                decoded_filename = decode_mime_header(part_filename)
                if decoded_filename == filename:
                    payload = part.get_payload(decode=True)
                    if payload:
                        # 안전한 파일명 생성
                        safe_filename = "".join(
                            c for c in filename 
                            if c.isalnum() or c in "._- "
                        )
                        save_path = os.path.join(save_dir, safe_filename)
                        
                        with open(save_path, "wb") as f:
                            f.write(payload)
                        
                        return save_path
        
        return None
