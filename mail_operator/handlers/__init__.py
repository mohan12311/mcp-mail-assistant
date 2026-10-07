"""
Operator 이벤트 핸들러

각 이벤트 타입별 처리 로직을 정의합니다.
"""

from mail_operator.handlers.new_email import handle_new_email
from mail_operator.handlers.approval import handle_approval_granted, handle_approval_denied

__all__ = [
    "handle_new_email",
    "handle_approval_granted",
    "handle_approval_denied",
]
