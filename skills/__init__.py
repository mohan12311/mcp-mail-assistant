"""
MCP Mail Assistant - Agent Skills
Claude Agent가 사용할 수 있는 도구 함수들
"""
from .mail_skills import list_emails, read_email, list_emails_with_body
from .attachment_skills import get_attachments, parse_attachment
from .notification_skills import send_slack_notification, send_inbox_summary_to_slack

__all__ = [
    "list_emails",
    "read_email",
    "list_emails_with_body",
    "get_attachments",
    "parse_attachment",
    "send_slack_notification",
    "send_inbox_summary_to_slack",
]
