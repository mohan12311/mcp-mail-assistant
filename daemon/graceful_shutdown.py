"""
정상 종료 처리 모듈
"""
import logging
import signal
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ShutdownHandler:
    """
    정상 종료 핸들러
    
    시그널을 처리하고 정상 종료 프로세스를 관리합니다.
    """
    
    def __init__(self):
        self.shutdown_flag = threading.Event()
        self.shutdown_timeout = 30  # 초
        self.force_timeout = 60  # 초
        self.cleanup_callbacks: list[Callable[[], None]] = []
        
    def setup_handlers(self) -> None:
        """시그널 핸들러 등록"""
        # SIGINT (Ctrl+C)
        signal.signal(signal.SIGINT, self._signal_handler)
        
        # SIGTERM (Windows에서는 제한적 지원)
        try:
            signal.signal(signal.SIGTERM, self._signal_handler)
        except (AttributeError, ValueError):
            logger.warning("SIGTERM not available on this platform")
        
        logger.info("Shutdown handlers registered")
    
    def _signal_handler(self, signum: int, frame) -> None:
        """시그널 핸들러"""
        signal_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else f"Signal {signum}"
        logger.info(f"Received {signal_name}, initiating graceful shutdown...")
        self.request_shutdown()
    
    def request_shutdown(self) -> None:
        """종료 요청"""
        if not self.shutdown_flag.is_set():
            logger.info("🛑 Graceful shutdown initiated")
            self.shutdown_flag.set()
        else:
            logger.warning("Shutdown already in progress")
    
    def is_shutdown_requested(self) -> bool:
        """종료 요청 여부 확인"""
        return self.shutdown_flag.is_set()
    
    def register_cleanup(self, callback: Callable[[], None]) -> None:
        """
        정리 작업 콜백 등록
        
        Args:
            callback: 종료 시 호출할 함수
        """
        self.cleanup_callbacks.append(callback)
    
    def execute_cleanup(self) -> None:
        """등록된 정리 작업 실행"""
        logger.info(f"Executing {len(self.cleanup_callbacks)} cleanup callbacks...")
        
        for i, callback in enumerate(self.cleanup_callbacks, 1):
            try:
                logger.debug(f"Cleanup {i}/{len(self.cleanup_callbacks)}: {callback.__name__}")
                callback()
            except Exception as e:
                logger.error(f"Cleanup callback {callback.__name__} failed: {e}", exc_info=True)
        
        logger.info("Cleanup complete")
    
    def wait_for_shutdown(self, timeout: Optional[int] = None) -> bool:
        """
        종료 대기
        
        Args:
            timeout: 대기 시간(초), None이면 무한 대기
            
        Returns:
            타임아웃 내 종료되었으면 True, 아니면 False
        """
        if timeout is None:
            timeout = self.shutdown_timeout
        
        logger.info(f"Waiting for shutdown (timeout={timeout}s)...")
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.shutdown_flag.is_set():
                return True
            time.sleep(0.1)
        
        logger.warning(f"Shutdown timeout ({timeout}s) reached")
        return False
    
    def perform_shutdown(self, cleanup_timeout: int = 30) -> int:
        """
        실제 종료 프로세스 실행
        
        Args:
            cleanup_timeout: 정리 작업 타임아웃(초)
            
        Returns:
            종료 코드 (0: 정상, 1: 타임아웃)
        """
        logger.info("=" * 60)
        logger.info("Starting shutdown sequence")
        logger.info("=" * 60)
        
        # 1. 종료 플래그 설정
        self.request_shutdown()
        
        # 2. 진행 중인 작업 완료 대기
        logger.info(f"Waiting for in-flight operations to complete (max {cleanup_timeout}s)...")
        time.sleep(2)  # 짧은 대기로 현재 작업 완료 기회 제공
        
        # 3. 정리 작업 실행
        cleanup_start = time.time()
        try:
            self.execute_cleanup()
            cleanup_time = time.time() - cleanup_start
            logger.info(f"Cleanup completed in {cleanup_time:.2f}s")
        except Exception as e:
            logger.error(f"Cleanup failed: {e}", exc_info=True)
        
        # 4. 타임아웃 체크
        total_time = time.time() - cleanup_start
        if total_time > cleanup_timeout:
            logger.warning(f"Cleanup exceeded timeout ({total_time:.2f}s > {cleanup_timeout}s)")
            return 1
        
        logger.info("=" * 60)
        logger.info("✅ Shutdown complete")
        logger.info("=" * 60)
        
        return 0


# 전역 인스턴스
_shutdown_handler: Optional[ShutdownHandler] = None


def get_shutdown_handler() -> ShutdownHandler:
    """전역 ShutdownHandler 인스턴스 반환"""
    global _shutdown_handler
    if _shutdown_handler is None:
        _shutdown_handler = ShutdownHandler()
    return _shutdown_handler


def is_shutdown_requested() -> bool:
    """종료 요청 여부 확인 (편의 함수)"""
    return get_shutdown_handler().is_shutdown_requested()

