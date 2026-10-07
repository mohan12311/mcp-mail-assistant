"""
MCP Mail Assistant - Email Watcher 패키지

메일 폴링 및 이벤트 처리를 담당합니다.
"""
from watcher.email_watcher import EmailWatcher
from watcher.event_queue import EmailEvent, EventQueue
from watcher.dedupe import DedupeChecker
from watcher.processor import EventProcessor

__all__ = [
    "EmailWatcher",
    "EmailEvent", 
    "EventQueue",
    "DedupeChecker",
    "EventProcessor",
]
