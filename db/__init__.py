"""
MCP Mail Assistant DB Layer

DB Layer 공개 API를 export합니다.
단일 SQLite DB 파일 (db/mail_agent.db)을 사용하여 모든 데이터를 관리합니다.
"""

from db.init_db import initialize
from db.connection import get_connection
from db import email_store
from db import task_store
from db import attachment_store
from db import state_store
from db import event_store
from db import processing_store
from db import slack_conversation_store

__all__ = [
    "initialize",
    "get_connection",
    "email_store",
    "task_store",
    "attachment_store",
    "state_store",
    "event_store",
    "processing_store",
    "slack_conversation_store",
]
