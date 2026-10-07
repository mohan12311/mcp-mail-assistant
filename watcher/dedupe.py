"""
중복 처리 방지

LRU 캐시와 DB 조회를 통해 메일 중복 처리를 방지합니다.
"""
import logging
from collections import OrderedDict
from threading import Lock
from typing import Optional

from db import email_store

logger = logging.getLogger(__name__)


class DedupeChecker:
    """메일 중복 체크"""
    
    def __init__(self, cache_size: int = 1000):
        """
        중복 체커 초기화
        
        Args:
            cache_size: LRU 캐시 크기
        """
        self._cache: OrderedDict[str, bool] = OrderedDict()
        self._cache_size = cache_size
        self._lock = Lock()
        logger.info(f"DedupeChecker 초기화 완료 (cache_size={cache_size})")
    
    def is_duplicate(self, email_id: str) -> bool:
        """
        메일 ID로 중복 체크 (캐시 + DB).
        
        Args:
            email_id: 메일 ID
            
        Returns:
            bool: 이미 처리된 메일이면 True
        """
        with self._lock:
            # 1. 캐시 확인
            if email_id in self._cache:
                logger.debug(f"캐시 히트: {email_id}")
                # LRU 업데이트 (최근 사용으로 이동)
                self._cache.move_to_end(email_id)
                return True
            
            # 2. DB 확인
            try:
                exists = email_store.exists(email_id)
                if exists:
                    logger.debug(f"DB 히트: {email_id}")
                    # 캐시에 추가
                    self._add_to_cache(email_id)
                    return True
                
                logger.debug(f"신규 메일: {email_id}")
                return False
                
            except Exception as e:
                logger.error(f"중복 체크 실패 (email_id={email_id}): {e}")
                # 에러 시 안전하게 중복으로 처리 (재처리 방지)
                return True
    
    def is_duplicate_by_message_id(self, message_id: str) -> bool:
        """
        Message-ID 헤더로 중복 체크.
        
        Args:
            message_id: Message-ID 헤더 값
            
        Returns:
            bool: 이미 처리된 메일이면 True
        """
        if not message_id:
            return False
        
        with self._lock:
            # Message-ID 기반 캐시 키
            cache_key = f"msg:{message_id}"
            
            # 1. 캐시 확인
            if cache_key in self._cache:
                logger.debug(f"Message-ID 캐시 히트: {message_id}")
                self._cache.move_to_end(cache_key)
                return True
            
            # 2. DB 확인 (message_id 필드로 조회)
            try:
                from db.connection import get_connection
                with get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT 1 FROM emails WHERE message_id = ? LIMIT 1", 
                        (message_id,)
                    )
                    exists = cursor.fetchone() is not None
                
                if exists:
                    logger.debug(f"Message-ID DB 히트: {message_id}")
                    self._add_to_cache(cache_key)
                    return True
                
                return False
                
            except Exception as e:
                logger.error(f"Message-ID 중복 체크 실패 ({message_id}): {e}")
                return True
    
    def is_duplicate_by_uidl(self, uidl: str) -> bool:
        """
        POP3 UIDL 기반 중복 체크. emails.uidl 컬럼 체크.

        Args:
            uidl: POP3 UIDL 값

        Returns:
            bool: 이미 처리된 메일이면 True
        """
        if not uidl:
            return False

        with self._lock:
            cache_key = f"uidl:{uidl}"

            if cache_key in self._cache:
                logger.debug(f"UIDL 캐시 히트: {uidl}")
                self._cache.move_to_end(cache_key)
                return True

            try:
                from db.connection import get_connection
                with get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT 1 FROM emails WHERE uidl = ? LIMIT 1",
                        (uidl,)
                    )
                    exists = cursor.fetchone() is not None

                if exists:
                    logger.debug(f"UIDL DB 히트: {uidl}")
                    self._add_to_cache(cache_key)
                    return True

                return False

            except Exception as e:
                logger.error(f"UIDL 중복 체크 실패 ({uidl}): {e}")
                return True

    def mark_seen(self, email_id: str) -> None:
        """
        메일을 캐시에 추가 (처리됨으로 표시).
        
        Args:
            email_id: 메일 ID
        """
        with self._lock:
            self._add_to_cache(email_id)
            logger.debug(f"메일 처리 완료 표시: {email_id}")
    
    def _add_to_cache(self, key: str) -> None:
        """
        캐시에 키를 추가 (LRU 관리).
        
        Args:
            key: 캐시 키
        """
        self._cache[key] = True
        self._cache.move_to_end(key)
        
        # LRU 크기 제한
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
    
    def clear_cache(self) -> None:
        """캐시를 초기화합니다."""
        with self._lock:
            self._cache.clear()
            logger.info("DedupeChecker 캐시 초기화됨")
    
    def cache_stats(self) -> dict:
        """
        캐시 통계를 반환합니다.
        
        Returns:
            dict: {"size": int, "capacity": int}
        """
        with self._lock:
            return {
                "size": len(self._cache),
                "capacity": self._cache_size,
            }
