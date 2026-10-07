"""
Operator 설정

Operator 프로세스의 런타임 설정을 관리합니다.
"""
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv


@dataclass
class OperatorConfig:
    """Operator 설정"""
    
    # 인스턴스 ID (lease 소유자 식별용)
    instance_id: str = field(default_factory=lambda: f"operator-{uuid.uuid4().hex[:8]}")
    
    # LLM CLI 설정. model=None이면 선택한 CLI의 로그인 계정 기본 모델을 사용합니다.
    llm_provider: str = "claude"
    llm_allow_fallback: bool = False
    model: Optional[str] = None
    
    # 이벤트 처리 설정
    event_poll_interval_seconds: float = 5.0  # 이벤트 폴링 간격
    event_lease_seconds: int = 300  # 이벤트 lease 시간 (5분)
    max_events_per_poll: int = 1  # 한 번에 처리할 최대 이벤트 수
    
    # 재시도 설정
    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    event_retry_base_seconds: int = 5
    event_retry_max_seconds: int = 3600
    
    # MCP 서버 설정
    mcp_server_command: str = "python"
    mcp_server_args: list = field(default_factory=lambda: ["mcp_server/server.py"])
    
    # Slack 설정 (환경변수에서 로드)
    slack_bot_token: Optional[str] = None
    slack_app_token: Optional[str] = None
    slack_channel_id: Optional[str] = None
    
    # 로깅 설정
    log_level: str = "INFO"
    audit_log_enabled: bool = True
    
    # 안전 설정
    max_body_length: int = 10000  # 본문 최대 길이
    max_attachment_text_length: int = 5000  # 첨부 텍스트 최대 길이
    
    @classmethod
    def from_env(cls) -> "OperatorConfig":
        """환경변수에서 설정 로드"""
        load_dotenv()
        return cls(
            instance_id=os.environ.get(
                "OPERATOR_INSTANCE_ID", 
                f"operator-{uuid.uuid4().hex[:8]}"
            ),
            llm_provider=os.environ.get("LLM_PROVIDER", "claude").lower(),
            llm_allow_fallback=os.environ.get(
                "LLM_ALLOW_FALLBACK", "false"
            ).lower() == "true",
            model=os.environ.get("LLM_MODEL") or os.environ.get("OPERATOR_MODEL"),
            event_poll_interval_seconds=float(
                os.environ.get("OPERATOR_POLL_INTERVAL", "5.0")
            ),
            event_lease_seconds=int(
                os.environ.get("OPERATOR_LEASE_SECONDS", "300")
            ),
            max_events_per_poll=int(
                os.environ.get("OPERATOR_MAX_EVENTS", "1")
            ),
            max_retries=int(os.environ.get("OPERATOR_MAX_RETRIES", "3")),
            retry_delay_seconds=float(
                os.environ.get("OPERATOR_RETRY_DELAY", "1.0")
            ),
            event_retry_base_seconds=int(
                os.environ.get("OPERATOR_EVENT_RETRY_BASE_SECONDS", "5")
            ),
            event_retry_max_seconds=int(
                os.environ.get("OPERATOR_EVENT_RETRY_MAX_SECONDS", "3600")
            ),
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN"),
            slack_app_token=os.environ.get("SLACK_APP_TOKEN"),
            slack_channel_id=os.environ.get("SLACK_CHANNEL_ID"),
            log_level=os.environ.get("OPERATOR_LOG_LEVEL", "INFO"),
            audit_log_enabled=os.environ.get(
                "OPERATOR_AUDIT_LOG", "true"
            ).lower() == "true",
            max_body_length=int(
                os.environ.get("OPERATOR_MAX_BODY_LENGTH", "10000")
            ),
            max_attachment_text_length=int(
                os.environ.get("OPERATOR_MAX_ATTACHMENT_TEXT", "5000")
            ),
        )
