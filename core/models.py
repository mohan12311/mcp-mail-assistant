"""
MCP Mail Assistant - 데이터 모델 정의
Pydantic을 사용한 타입 안전한 데이터 구조
"""
from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel, Field


class Attachment(BaseModel):
    """메일 첨부파일 정보"""
    filename: str
    content_type: str
    size: int
    local_path: Optional[str] = None


class AttachmentAnalysis(BaseModel):
    """첨부파일 분석 결과"""
    filename: str
    file_type: str  # "word", "excel", "ppt", "pdf", "unknown"
    extracted_text: str
    tables: Optional[list[dict]] = None  # Excel/Word 표 데이터
    key_points: list[str] = Field(default_factory=list)  # Claude가 추출한 핵심 포인트
    page_count: Optional[int] = None  # PDF/PPT 페이지/슬라이드 수
    sheet_names: Optional[list[str]] = None  # Excel 시트명 목록


class ActionItem(BaseModel):
    """메일에서 추출된 할 일 항목"""
    type: str  # "todo", "reminder", "reply_needed", "review_attachment", "fyi"
    description: str
    deadline: Optional[date] = None
    priority: str = "medium"  # "high", "medium", "low"
    related_attachment: Optional[str] = None


class EmailInfo(BaseModel):
    """메일 기본 정보 (목록 조회용)"""
    email_id: str
    subject: str
    sender: str
    sender_email: str
    received_at: datetime
    has_attachment: bool
    is_read: bool
    snippet: Optional[str] = None  # 본문 미리보기 (200자)
    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
    thread_topic: Optional[str] = None


class EmailSummary(BaseModel):
    """메일 상세 정보 및 분석 결과"""
    email_id: str
    subject: str
    sender: str
    sender_email: str
    received_at: datetime
    body: str
    summary: Optional[str] = None  # Claude가 생성한 요약
    action_items: list[ActionItem] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    attachment_analyses: list[AttachmentAnalysis] = Field(default_factory=list)
    requires_response: bool = False
    urgency: str = "normal"  # "urgent", "normal", "low"
    
    # 스레드 정보
    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
    thread_topic: Optional[str] = None


class InboxSummary(BaseModel):
    """받은편지함 전체 요약"""
    generated_at: datetime
    total_count: int
    unread_count: int
    emails: list[EmailSummary] = Field(default_factory=list)
    urgent_items: list[ActionItem] = Field(default_factory=list)
    all_todos: list[ActionItem] = Field(default_factory=list)
    attachments_to_review: list[str] = Field(default_factory=list)
