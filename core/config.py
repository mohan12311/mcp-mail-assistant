"""
MCP Mail Assistant - 설정 관리
환경 변수에서 설정을 로드
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_INT_FIELDS: frozenset[str] = frozenset({
    "imap_port",
    "pipeline_interval_seconds",
    "pipeline_batch_size",
    "pop_port",
    "smtp_port",
    "smtp_timeout_seconds",
    "attachment_max_file_bytes",
    "attachment_max_mail_bytes",
})
_FLOAT_FIELDS: frozenset[str] = frozenset({"min_confidence_threshold"})
_BOOL_FIELDS: frozenset[str] = frozenset({"pop_ssl", "pop_delete_after_download", "smtp_tls"})


@dataclass
class Config:
    """애플리케이션 설정"""
    # IMAP 설정
    imap_host: str
    imap_port: int
    imap_user: str
    imap_pass: str

    # Anthropic API
    anthropic_api_key: str

    # Slack 설정
    slack_bot_token: str
    slack_app_token: str  # Socket Mode용
    slack_channel: str
    approval_channel: str

    # 첨부파일 저장 경로
    attachment_dir: str

    # Pipeline 설정
    pipeline_interval_seconds: int
    pipeline_batch_size: int
    min_confidence_threshold: float

    # POP3 설정 (선택적 — pop_host가 비어있으면 IMAP 사용)
    pop_host: str = ""
    pop_port: int = 995
    pop_user: str = ""
    pop_pass: str = ""
    pop_ssl: bool = True
    pop_delete_after_download: bool = False

    # POP3 첨부파일 보호 한도
    attachment_max_file_bytes: int = 10 * 1024 * 1024
    attachment_max_mail_bytes: int = 25 * 1024 * 1024

    # SMTP 설정
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_tls: bool = True
    smtp_timeout_seconds: int = 30
    
    @classmethod
    def from_env(cls, env_path: str | None = None) -> "Config":
        """환경 변수에서 설정 로드"""
        if env_path:
            load_dotenv(env_path)
        else:
            load_dotenv()

        return cls(
            imap_host=os.getenv("IMAP_HOST", ""),
            imap_port=int(os.getenv("IMAP_PORT", "993")),
            # IMAP_USER/IMAP_PASS는 "DB 전용 모드(MCP/Operator)"에서도 실행 가능하도록 선택값으로 둡니다.
            # 실제 IMAP 접근은 IMAPClient에서 실패 처리됩니다.
            imap_user=os.getenv("IMAP_USER", ""),
            imap_pass=os.getenv("IMAP_PASS", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
            slack_app_token=os.getenv("SLACK_APP_TOKEN", ""),
            slack_channel=os.getenv("SLACK_CHANNEL", "#general"),
            approval_channel=os.getenv("APPROVAL_CHANNEL", "#task-approvals"),
            attachment_dir=os.getenv("ATTACHMENT_DIR", "./attachments"),
            pipeline_interval_seconds=int(os.getenv("PIPELINE_INTERVAL_SECONDS", "600")),
            pipeline_batch_size=int(os.getenv("PIPELINE_BATCH_SIZE", "10")),
            min_confidence_threshold=float(os.getenv("MIN_CONFIDENCE_THRESHOLD", "0.5")),
            pop_host=os.getenv("POP_HOST", ""),
            pop_port=int(os.getenv("POP_PORT", "995")),
            pop_user=os.getenv("POP_USER", ""),
            pop_pass=os.getenv("POP_PASS", ""),
            pop_ssl=os.getenv("POP_SSL", "true").lower() == "true",
            pop_delete_after_download=os.getenv("POP_DELETE_AFTER_DOWNLOAD", "false").lower() == "true",
            attachment_max_file_bytes=int(os.getenv("ATTACHMENT_MAX_FILE_BYTES", str(10 * 1024 * 1024))),
            attachment_max_mail_bytes=int(os.getenv("ATTACHMENT_MAX_MAIL_BYTES", str(25 * 1024 * 1024))),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_pass=os.getenv("SMTP_PASS", ""),
            smtp_tls=os.getenv("SMTP_TLS", "true").lower() == "true",
            smtp_timeout_seconds=int(os.getenv("SMTP_TIMEOUT_SECONDS", "30")),
        )

    @classmethod
    def from_file(cls, path: str) -> "Config":
        """JSON 파일 값 우선 로드; 파일에 없는 필드는 env 기본값 사용 (파일 > env > 하드코딩 기본값)."""
        data = json.loads(Path(path).read_text())
        base = cls.from_env()
        for key, value in data.items():
            if hasattr(base, key):
                if key in _INT_FIELDS:
                    value = int(value)
                elif key in _FLOAT_FIELDS:
                    value = float(value)
                elif key in _BOOL_FIELDS and isinstance(value, str):
                    value = value.lower() == "true"
                setattr(base, key, value)
        return base
