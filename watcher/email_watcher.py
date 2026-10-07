"""
Email Watcher - 메일 폴링 루프

주기적으로 선택된 POP3/IMAP adapter를 폴링하여 새 메일을 감지하고 처리합니다.
"""
import os
import sys
import logging
import signal
import time
import argparse
from threading import Thread, Event
from typing import Optional, List
from datetime import datetime

from core.config import Config
from skills.mail_skills import init_config, list_emails_with_body
from watcher.event_queue import EmailEvent, EventQueue
from watcher.dedupe import DedupeChecker
from watcher.processor import EventProcessor
from db import state_store

logger = logging.getLogger(__name__)


class EmailWatcher:
    """메일 폴링 감시자"""
    
    def __init__(
        self, 
        poll_interval: int = 300,
        batch_size: int = 20,
        config: Optional[Config] = None,
        process_inline: bool = True,
    ):
        """
        Email Watcher 초기화
        
        Args:
            poll_interval: 폴링 간격 (초)
            batch_size: 한 번에 처리할 메일 수
            config: Config 인스턴스 (None이면 자동 로드)
            process_inline: True이면 자체적으로 이벤트까지 처리,
                False이면 큐에 적재만 하고 외부 소비자에게 맡김
        """
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.process_inline = process_inline
        
        # Config 초기화
        if config is None:
            config = Config.from_env()
        self.config = config
        init_config(config)
        
        # 컴포넌트 초기화
        self.event_queue = EventQueue(maxsize=1000)
        self.dedupe: Optional[DedupeChecker] = None
        self.processor: Optional[EventProcessor] = None
        if self.process_inline:
            self.dedupe = DedupeChecker(cache_size=1000)
            self.processor = EventProcessor(self.dedupe)
        
        # 종료 플래그
        self._stop_event = Event()
        self._thread: Optional[Thread] = None
        
        # 통계
        self.stats = {
            "total_polls": 0,
            "total_emails": 0,
            "total_enqueued": 0,
            "total_processed": 0,
            "total_skipped": 0,
            "total_failed": 0,
        }
        
        mode = "inline-processing" if self.process_inline else "queue-only"
        logger.info(
            f"EmailWatcher 초기화 완료 "
            f"(poll_interval={poll_interval}s, batch_size={batch_size}, mode={mode})"
        )
    
    def start(self) -> None:
        """백그라운드 스레드에서 폴링을 시작합니다."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("이미 실행 중입니다")
            return
        
        self._stop_event.clear()
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        
        # 상태 업데이트
        state_store.set_watcher_status("running")
        logger.info("EmailWatcher 시작됨 (백그라운드)")
    
    def stop(self) -> None:
        """폴링을 정상 종료합니다."""
        logger.info("EmailWatcher 종료 중...")
        self._stop_event.set()
        
        if self._thread is not None:
            self._thread.join(timeout=10)
        
        # 상태 업데이트
        state_store.set_watcher_status("stopped")
        logger.info("EmailWatcher 종료됨")
    
    def run_forever(self) -> None:
        """메인 스레드에서 실행 (블로킹)."""
        logger.info("EmailWatcher 실행 (메인 스레드)")
        state_store.set_watcher_status("running")
        
        try:
            self._run_loop()
        except KeyboardInterrupt:
            logger.info("사용자에 의해 중단됨")
        finally:
            state_store.set_watcher_status("stopped")
    
    def poll_once(self) -> int:
        """
        단일 폴링을 실행합니다 (테스트용).
        
        Returns:
            int: 처리된 메일 수
        """
        logger.info("단일 폴링 실행")
        count = self._do_poll()
        if self.process_inline:
            logger.info(f"단일 폴링 완료: {count}개 메일 처리")
        else:
            logger.info(f"단일 폴링 완료: {count}개 이벤트 큐에 적재")
        return count

    def get_new_events(
        self,
        timeout: float = 0.0,
        max_events: int = 50,
    ) -> List[EmailEvent]:
        """
        큐에서 새 이벤트를 가져옵니다.
        
        Args:
            timeout: 첫 이벤트 대기 타임아웃 (초)
            max_events: 한 번에 가져올 최대 이벤트 수
        
        Returns:
            list[EmailEvent]: 이벤트 목록
        """
        events: List[EmailEvent] = []
        wait_timeout = timeout
        
        while len(events) < max_events:
            event = self.event_queue.get_event(timeout=wait_timeout)
            if event is None:
                break
            events.append(event)
            # 이후 호출에서는 논블로킹으로 전환
            wait_timeout = 0
        
        if events:
            logger.debug(f"{len(events)}개 이벤트를 큐에서 수신")
        return events
    
    def _run_loop(self) -> None:
        """폴링 루프 (내부 메서드)."""
        retry_count = 0
        max_retries = 3
        retry_delay = 30  # 30초
        
        while not self._stop_event.is_set():
            try:
                # 폴링 실행
                count = self._do_poll()
                self.stats["total_polls"] += 1
                
                # 성공 시 재시도 카운터 리셋
                retry_count = 0
                
                # 다음 폴링까지 대기
                logger.debug(f"{self.poll_interval}초 대기 중...")
                self._stop_event.wait(timeout=self.poll_interval)
                
            except KeyboardInterrupt:
                logger.info("사용자에 의해 중단됨")
                break
                
            except Exception as e:
                retry_count += 1
                logger.error(
                    f"폴링 실패 ({retry_count}/{max_retries}): {e}",
                    exc_info=True
                )
                
                if retry_count >= max_retries:
                    logger.error("최대 재시도 횟수 초과, 종료합니다")
                    state_store.set_watcher_status("error")
                    break
                
                # 재시도 대기
                logger.info(f"{retry_delay}초 후 재시도...")
                self._stop_event.wait(timeout=retry_delay)
    
    def _do_poll(self) -> int:
        """
        실제 폴링을 수행합니다.
        
        Returns:
            int: 처리된 메일 수
        """
        logger.info("메일 폴링 시작")
        start_time = time.time()
        
        try:
            # 1. 메일 조회
            emails = list_emails_with_body(
                folder="INBOX",
                limit=self.batch_size,
                only_unseen=True
            )
            
            total = len(emails)
            self.stats["total_emails"] += total
            logger.info(f"{total}개 메일 조회됨")
            
            if not emails:
                logger.info("새 메일 없음")
                self._update_poll_time()
                return 0
            
            # 2. 이벤트 생성 및 큐에 추가
            events = []
            for email_data in emails:
                event = EmailEvent(
                    event_type="new_email",
                    email_id=email_data["id"],
                    email_data=email_data,
                    timestamp=datetime.now()
                )
                events.append(event)
                self.event_queue.put_event(event)
            
            enqueued = len(events)
            self.stats["total_enqueued"] += enqueued
            
            # 3. 이벤트 처리 (옵션)
            if self.process_inline and self.processor:
                result = self.processor.process_batch(events)
                
                self.stats["total_processed"] += result["success"]
                self.stats["total_skipped"] += result["skipped"]
                self.stats["total_failed"] += result["failed"]
                
                processed_count = result["success"]
                log_message = (
                    f"폴링 완료: "
                    f"{result['success']}개 처리, "
                    f"{result['skipped']}개 스킵, "
                    f"{result['failed']}개 실패"
                )
            else:
                processed_count = enqueued
                log_message = f"폴링 완료: {enqueued}개 이벤트 큐에 적재 (외부 처리 대기)"
            
            # 4. 폴링 시각 업데이트
            self._update_poll_time()
            
            elapsed = time.time() - start_time
            logger.info(f"{log_message} ({elapsed:.2f}초)")
            
            return processed_count
            
        except Exception as e:
            logger.error(f"폴링 실행 중 에러: {e}", exc_info=True)
            raise
    
    def _update_poll_time(self) -> None:
        """마지막 폴링 시각을 업데이트합니다."""
        try:
            state_store.set_last_poll_time(datetime.now())
        except Exception as e:
            logger.error(f"폴링 시각 업데이트 실패: {e}")
    
    def get_stats(self) -> dict:
        """
        통계 정보를 반환합니다.
        
        Returns:
            dict: 통계 정보
        """
        return {
            **self.stats,
            "queue_size": self.event_queue.size(),
            "cache_stats": self.dedupe.cache_stats() if self.dedupe else None,
            "last_poll_time": state_store.get_last_poll_time(),
        }


def main():
    """CLI 진입점"""
    parser = argparse.ArgumentParser(description="MCP Mail Assistant - Email Watcher")
    parser.add_argument(
        "--once", 
        action="store_true",
        help="단일 폴링 실행 후 종료"
    )
    parser.add_argument(
        "--daemon", 
        action="store_true",
        help="데몬 모드로 실행 (무한 루프)"
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        help="폴링 간격 (초)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="배치 크기"
    )
    args = parser.parse_args()
    
    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
        ]
    )
    
    # 환경변수에서 설정 로드
    poll_interval = args.poll_interval or int(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "300"))
    batch_size = args.batch_size or int(os.getenv("EMAIL_BATCH_SIZE", "20"))
    
    # Watcher 초기화
    try:
        watcher = EmailWatcher(
            poll_interval=poll_interval,
            batch_size=batch_size
        )
    except Exception as e:
        logger.error(f"Watcher 초기화 실패: {e}", exc_info=True)
        sys.exit(1)
    
    # 실행 모드
    if args.once:
        # 단일 폴링
        try:
            count = watcher.poll_once()
            print(f"\nProcessed emails: {count}")
            print("\n=== Statistics ===")
            for key, value in watcher.get_stats().items():
                print(f"{key}: {value}")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Polling failed: {e}", exc_info=True)
            sys.exit(1)
    
    elif args.daemon:
        # 데몬 모드
        def signal_handler(signum, frame):
            logger.info(f"시그널 수신: {signum}")
            watcher.stop()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            watcher.run_forever()
        except Exception as e:
            logger.error(f"실행 중 에러: {e}", exc_info=True)
            sys.exit(1)
    
    else:
        # 기본: 도움말 표시
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
