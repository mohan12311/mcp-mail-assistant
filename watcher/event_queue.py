"""
이벤트 큐 관리

메일 이벤트를 thread-safe하게 관리합니다.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from queue import Queue, Empty
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EmailEvent:
    """메일 이벤트"""
    event_type: str  # "new_email", "email_updated"
    email_id: str
    email_data: dict
    timestamp: datetime = field(default_factory=datetime.now)
    retry_count: int = 0
    
    def __repr__(self) -> str:
        return f"EmailEvent(type={self.event_type}, id={self.email_id}, retry={self.retry_count})"


class EventQueue:
    """Thread-safe 이벤트 큐"""
    
    def __init__(self, maxsize: int = 0):
        """
        이벤트 큐 초기화
        
        Args:
            maxsize: 큐 최대 크기 (0=무제한)
        """
        self._queue = Queue(maxsize=maxsize)
        logger.info(f"EventQueue 초기화 완료 (maxsize={maxsize})")
    
    def put_event(self, event: EmailEvent) -> None:
        """
        이벤트를 큐에 추가합니다.
        
        Args:
            event: 추가할 이벤트
        """
        self._queue.put(event)
        logger.debug(f"이벤트 추가: {event}")
    
    def get_event(self, timeout: Optional[float] = None) -> Optional[EmailEvent]:
        """
        큐에서 이벤트를 가져옵니다 (제거).
        
        Args:
            timeout: 타임아웃 (초), None=블로킹
            
        Returns:
            EmailEvent | None: 이벤트 또는 None (타임아웃 시)
        """
        try:
            event = self._queue.get(timeout=timeout)
            logger.debug(f"이벤트 가져옴: {event}")
            return event
        except Empty:
            return None
    
    def peek(self) -> Optional[EmailEvent]:
        """
        큐의 첫 이벤트를 확인합니다 (제거하지 않음).
        
        Returns:
            EmailEvent | None: 이벤트 또는 None (비어있으면)
        """
        try:
            # Queue는 peek를 직접 지원하지 않으므로 get 후 다시 put
            event = self._queue.get_nowait()
            self._queue.put(event)
            return event
        except Empty:
            return None
    
    def size(self) -> int:
        """
        큐의 현재 크기를 반환합니다.
        
        Returns:
            int: 큐에 있는 이벤트 수
        """
        return self._queue.qsize()
    
    def clear(self) -> None:
        """큐를 비웁니다."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Empty:
                break
        logger.info("EventQueue 초기화됨")
    
    def is_empty(self) -> bool:
        """
        큐가 비어있는지 확인합니다.
        
        Returns:
            bool: 비어있으면 True
        """
        return self._queue.empty()
