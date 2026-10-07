"""
위험도 기반 정책 엔진

태스크의 위험도를 평가하고 실행 정책을 결정합니다.
- auto: 자동 실행 (승인 불필요)
- confirm: 승인 필요
- deny: 실행 차단
"""
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class RiskLevel(Enum):
    """위험도 레벨"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ExecutionPolicy(Enum):
    """실행 정책"""
    AUTO = "auto"  # 자동 실행
    CONFIRM = "confirm"  # 승인 필요
    DENY = "deny"  # 실행 차단


@dataclass
class PolicyDecision:
    """정책 결정 결과"""
    risk_level: RiskLevel
    policy: ExecutionPolicy
    reason: str
    requires_approval: bool = False


# 안전한 액션 allowlist
SAFE_ACTIONS = {
    # 읽기 전용 액션
    "read_email",
    "summarize_email",
    "classify_email",
    "extract_tasks",
    "analyze_attachment",
    # 알림 액션
    "notify_slack",
    "create_reminder",
    # 내부 데이터 업데이트
    "update_priority",
    "add_label",
    "mark_read",
}

# 승인이 필요한 액션
APPROVAL_REQUIRED_ACTIONS = {
    # 외부 전송 (구현됨)
    "reply_email",
    # 데이터 변경
    "move_email",
    "archive_email",
    "create_task_external",
}

# 차단된 액션 (구현되지 않았거나 항상 금지)
BLOCKED_ACTIONS = {
    "delete_email",
    "delete_all",
    "export_contacts",
    "share_external",
    # 미구현 — 현재 지원하지 않음
    "send_email",
    "forward_email",
    "create_meeting",
    "update_meeting",
    "cancel_meeting",
}


def evaluate_risk(task_type: str, task_data: dict) -> PolicyDecision:
    """
    태스크의 위험도를 평가하고 실행 정책을 결정합니다.
    
    Args:
        task_type: 태스크 타입
        task_data: 태스크 데이터 (제목, 설명, 대상 등)
        
    Returns:
        PolicyDecision: 정책 결정 결과
    """
    # 1. 차단된 액션 체크
    if task_type in BLOCKED_ACTIONS:
        return PolicyDecision(
            risk_level=RiskLevel.CRITICAL,
            policy=ExecutionPolicy.DENY,
            reason=f"차단된 액션: {task_type}",
            requires_approval=False
        )
    
    # 2. 안전한 액션 체크
    if task_type in SAFE_ACTIONS:
        return PolicyDecision(
            risk_level=RiskLevel.LOW,
            policy=ExecutionPolicy.AUTO,
            reason=f"안전한 액션: {task_type}",
            requires_approval=False
        )
    
    # 3. 승인 필요 액션 체크
    if task_type in APPROVAL_REQUIRED_ACTIONS:
        # 위험도 세분화
        risk_level = _evaluate_action_risk(task_type, task_data)
        
        return PolicyDecision(
            risk_level=risk_level,
            policy=ExecutionPolicy.CONFIRM,
            reason=f"승인 필요 액션: {task_type}",
            requires_approval=True
        )
    
    # 4. 알 수 없는 액션은 기본적으로 차단 (defense-in-depth)
    logger.warning(f"알 수 없는 액션 타입 — 차단: {task_type}")
    return PolicyDecision(
        risk_level=RiskLevel.HIGH,
        policy=ExecutionPolicy.DENY,
        reason=f"지원하지 않는 액션: {task_type}",
        requires_approval=False
    )


def _evaluate_action_risk(task_type: str, task_data: dict) -> RiskLevel:
    """액션의 세부 위험도를 평가합니다."""
    
    # 이메일 전송 관련
    if task_type in {"send_email", "reply_email", "forward_email"}:
        # 외부 수신자가 있으면 높은 위험도
        recipients = task_data.get("recipients", [])
        if any(_is_external_email(r) for r in recipients):
            return RiskLevel.HIGH
        
        # 첨부파일이 있으면 중간 위험도
        if task_data.get("has_attachments"):
            return RiskLevel.MEDIUM
        
        return RiskLevel.MEDIUM
    
    # 일정 관련
    if task_type in {"create_meeting", "update_meeting", "cancel_meeting"}:
        # 외부 참석자가 있으면 높은 위험도
        attendees = task_data.get("attendees", [])
        if any(_is_external_email(a) for a in attendees):
            return RiskLevel.HIGH
        
        return RiskLevel.MEDIUM
    
    return RiskLevel.MEDIUM


def _is_external_email(email: str) -> bool:
    """외부 이메일인지 확인합니다."""
    _raw = os.environ.get("INTERNAL_DOMAINS", "")
    internal_domains = {d.strip().lower() for d in _raw.split(",") if d.strip()}

    if "@" not in email:
        return False

    domain = email.split("@")[1].lower()
    return domain not in internal_domains


def is_action_allowed(task_type: str) -> bool:
    """액션이 허용되는지 확인합니다."""
    return task_type not in BLOCKED_ACTIONS


def requires_approval(task_type: str) -> bool:
    """액션이 승인을 필요로 하는지 확인합니다."""
    if task_type in BLOCKED_ACTIONS:
        return False  # 차단된 것은 승인으로도 실행 불가
    
    if task_type in SAFE_ACTIONS:
        return False
    
    return True  # 기본적으로 승인 필요


def get_risk_summary(task_type: str, task_data: dict) -> str:
    """위험도 요약 문자열을 반환합니다."""
    decision = evaluate_risk(task_type, task_data)
    
    risk_emoji = {
        RiskLevel.LOW: "🟢",
        RiskLevel.MEDIUM: "🟡",
        RiskLevel.HIGH: "🟠",
        RiskLevel.CRITICAL: "🔴",
    }
    
    policy_text = {
        ExecutionPolicy.AUTO: "자동 실행",
        ExecutionPolicy.CONFIRM: "승인 필요",
        ExecutionPolicy.DENY: "차단됨",
    }
    
    return (
        f"{risk_emoji.get(decision.risk_level, '❓')} "
        f"위험도: {decision.risk_level.value} | "
        f"정책: {policy_text.get(decision.policy, '알 수 없음')} | "
        f"사유: {decision.reason}"
    )
