"""
감사 로깅 모듈

모든 Operator의 도구 호출, 결정, DB 업데이트를 기록합니다.
운영 필수 기능으로, 이벤트 추적과 디버깅에 사용됩니다.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any, List
from enum import Enum

logger = logging.getLogger(__name__)


class AuditAction(Enum):
    """감사 액션 타입"""
    EVENT_RECEIVED = "event_received"
    EVENT_PROCESSED = "event_processed"
    EVENT_FAILED = "event_failed"
    
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    
    POLICY_CHECK = "policy_check"
    POLICY_DECISION = "policy_decision"
    
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RECEIVED = "approval_received"
    SLACK_REQUEST = "slack_request"
    SLACK_RESPONSE = "slack_response"
    
    TASK_CREATED = "task_created"
    TASK_EXECUTED = "task_executed"
    TASK_FAILED = "task_failed"
    
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"


@dataclass
class AuditEntry:
    """감사 로그 엔트리"""
    timestamp: str
    action: str
    event_id: Optional[str] = None
    task_id: Optional[str] = None
    details: dict = field(default_factory=dict)
    operator_instance: Optional[str] = None
    duration_ms: Optional[int] = None
    success: bool = True
    error: Optional[str] = None


class AuditLogger:
    """감사 로거"""
    
    def __init__(
        self, 
        operator_instance: str,
        enabled: bool = True,
        log_to_file: bool = False,
        file_path: Optional[str] = None
    ):
        """
        감사 로거 초기화
        
        Args:
            operator_instance: Operator 인스턴스 ID
            enabled: 로깅 활성화 여부
            log_to_file: 파일 로깅 여부
            file_path: 로그 파일 경로
        """
        self.operator_instance = operator_instance
        self.enabled = enabled
        self.log_to_file = log_to_file
        self.file_path = file_path or f"audit_{operator_instance}.log"
        
        # 현재 이벤트 컨텍스트
        self._current_event_id: Optional[str] = None
        self._event_start_time: Optional[datetime] = None
        
        # 메모리 내 로그 (최근 100개)
        self._recent_logs: List[AuditEntry] = []
        self._max_recent_logs = 100
        
        logger.info(f"AuditLogger 초기화: {operator_instance}")
    
    def set_event_context(self, event_id: str):
        """현재 처리 중인 이벤트 컨텍스트 설정"""
        self._current_event_id = event_id
        self._event_start_time = datetime.now()
    
    def clear_event_context(self):
        """이벤트 컨텍스트 초기화"""
        self._current_event_id = None
        self._event_start_time = None
    
    def log(
        self,
        action: AuditAction,
        details: Optional[dict] = None,
        event_id: Optional[str] = None,
        task_id: Optional[str] = None,
        success: bool = True,
        error: Optional[str] = None
    ) -> None:
        """
        감사 로그 기록
        
        Args:
            action: 감사 액션 타입
            details: 상세 정보 (민감 정보 제외)
            event_id: 이벤트 ID (없으면 컨텍스트에서 가져옴)
            task_id: 태스크 ID
            success: 성공 여부
            error: 에러 메시지
        """
        if not self.enabled:
            return
        
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            action=action.value,
            event_id=event_id or self._current_event_id,
            task_id=task_id,
            details=self._sanitize_details(details or {}),
            operator_instance=self.operator_instance,
            success=success,
            error=error
        )
        
        # 로그 출력
        self._log_entry(entry)
        
        # 메모리에 저장
        self._recent_logs.append(entry)
        if len(self._recent_logs) > self._max_recent_logs:
            self._recent_logs.pop(0)
        
        # 파일에 저장
        if self.log_to_file:
            self._write_to_file(entry)
    
    def log_tool_call(
        self,
        tool_name: str,
        arguments: dict,
        event_id: Optional[str] = None
    ) -> None:
        """도구 호출 로그"""
        self.log(
            action=AuditAction.TOOL_CALL,
            details={
                "tool_name": tool_name,
                "arguments": self._sanitize_details(arguments)
            },
            event_id=event_id
        )
    
    def log_tool_result(
        self,
        tool_name: str,
        result_summary: str,
        success: bool = True,
        error: Optional[str] = None,
        event_id: Optional[str] = None
    ) -> None:
        """도구 결과 로그"""
        self.log(
            action=AuditAction.TOOL_RESULT,
            details={
                "tool_name": tool_name,
                "result_summary": result_summary[:200]  # 결과 요약 (200자 제한)
            },
            event_id=event_id,
            success=success,
            error=error
        )
    
    def log_policy_decision(
        self,
        task_type: str,
        risk_level: str,
        policy: str,
        reason: str,
        task_id: Optional[str] = None
    ) -> None:
        """정책 결정 로그"""
        self.log(
            action=AuditAction.POLICY_DECISION,
            details={
                "task_type": task_type,
                "risk_level": risk_level,
                "policy": policy,
                "reason": reason
            },
            task_id=task_id
        )
    
    def log_llm_interaction(
        self,
        prompt_summary: str,
        response_summary: str,
        model: str,
        tokens_used: Optional[int] = None
    ) -> None:
        """LLM 상호작용 로그"""
        self.log(
            action=AuditAction.LLM_REQUEST,
            details={
                "prompt_summary": prompt_summary[:100],  # 100자 요약
                "model": model
            }
        )
        self.log(
            action=AuditAction.LLM_RESPONSE,
            details={
                "response_summary": response_summary[:100],
                "tokens_used": tokens_used
            }
        )
    
    def get_recent_logs(self, limit: int = 50) -> List[dict]:
        """최근 로그 조회"""
        logs = self._recent_logs[-limit:]
        return [
            {
                "timestamp": log.timestamp,
                "action": log.action,
                "event_id": log.event_id,
                "task_id": log.task_id,
                "success": log.success,
                "error": log.error,
                "details": log.details
            }
            for log in logs
        ]
    
    def get_event_trace(self, event_id: str) -> List[dict]:
        """특정 이벤트의 전체 추적 로그"""
        return [
            {
                "timestamp": log.timestamp,
                "action": log.action,
                "details": log.details,
                "success": log.success,
                "error": log.error
            }
            for log in self._recent_logs
            if log.event_id == event_id
        ]
    
    def _sanitize_details(self, details: dict) -> dict:
        """민감 정보 제거"""
        sanitized = {}
        
        # 민감한 키 목록
        sensitive_keys = {
            "password", "token", "secret", "key", "auth",
            "body_text", "body_full", "email_body", "attachment_content",
            "subject", "sender", "recipient", "message"
        }
        
        for key, value in details.items():
            key_lower = key.lower()
            
            # 민감한 키는 마스킹
            if any(s in key_lower for s in sensitive_keys):
                if isinstance(value, str) and len(value) > 0:
                    sanitized[key] = f"[REDACTED: {len(value)} chars]"
                else:
                    sanitized[key] = "[REDACTED]"
            # 긴 문자열은 잘라냄
            elif isinstance(value, str) and len(value) > 500:
                sanitized[key] = value[:500] + f"... ({len(value)} chars total)"
            # dict는 재귀적으로 처리
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_details(value)
            # 리스트는 길이만
            elif isinstance(value, list) and len(value) > 10:
                sanitized[key] = f"[list: {len(value)} items]"
            else:
                sanitized[key] = value
        
        return sanitized
    
    def _log_entry(self, entry: AuditEntry) -> None:
        """로그 엔트리 출력"""
        log_msg = (
            f"[AUDIT] {entry.action} | "
            f"event={entry.event_id or 'N/A'} | "
            f"success={entry.success}"
        )
        
        if entry.error:
            log_msg += f" | error={entry.error}"
        
        if entry.details:
            log_msg += f" | details={json.dumps(entry.details, ensure_ascii=False)}"
        
        if entry.success:
            logger.info(log_msg)
        else:
            logger.warning(log_msg)
    
    def _write_to_file(self, entry: AuditEntry) -> None:
        """파일에 로그 기록"""
        try:
            log_line = json.dumps({
                "timestamp": entry.timestamp,
                "action": entry.action,
                "event_id": entry.event_id,
                "task_id": entry.task_id,
                "operator": entry.operator_instance,
                "success": entry.success,
                "error": entry.error,
                "details": entry.details
            }, ensure_ascii=False) + "\n"
            
            with open(self.file_path, "a", encoding="utf-8") as f:
                f.write(log_line)
        except Exception as e:
            logger.error(f"감사 로그 파일 기록 실패: {e}")
