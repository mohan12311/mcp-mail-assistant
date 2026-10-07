"""
메인 이벤트 루프 - 모든 컴포넌트 통합
"""
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from core.config import Config
from core.imap_client import IMAPClient
from db import initialize as db_initialize
from db import email_store, task_store
from watcher import EmailWatcher, EventProcessor, DedupeChecker
from daemon.health_check import get_health_checker
from daemon.graceful_shutdown import get_shutdown_handler

logger = logging.getLogger(__name__)


class MailDaemon:
    """
    MCP Mail Assistant 메인 데몬
    
    모든 컴포넌트를 통합하여 24/7 이메일 감시 및 처리를 수행합니다.
    """
    
    def __init__(self, config: Optional[Config] = None, verbose: bool = False):
        """
        초기화
        
        Args:
            config: 설정 객체 (None이면 환경변수에서 로드)
            verbose: 상세 로깅 활성화
        """
        self.config = config or Config.from_env()
        self.verbose = verbose
        
        # 핸들러 인스턴스
        self.shutdown_handler = get_shutdown_handler()
        self.health_checker = get_health_checker()
        
        # 워커 스레드
        self.watcher_thread: Optional[threading.Thread] = None
        
        # 컴포넌트
        self.imap_client: Optional[IMAPClient] = None
        self.email_watcher: Optional[EmailWatcher] = None
        self.event_processor: Optional[EventProcessor] = None
        self.dedupe_checker: Optional[DedupeChecker] = None
        
        # DB 스토어
        self.email_store: Optional[email_store] = None
        self.task_store: Optional[task_store] = None
        
        # 상태
        self._initialized = False
        self._running = False
        
        logger.info("MailDaemon instance created")
    
    def initialize(self) -> None:
        """컴포넌트 초기화"""
        if self._initialized:
            logger.warning("Already initialized")
            return
        
        logger.info("=" * 60)
        logger.info("Initializing MailDaemon")
        logger.info("=" * 60)
        
        try:
            # 1. DB 초기화
            logger.info("Initializing database...")
            db_path = os.environ.get('DB_PATH', './db/mail_agent.db')
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            db_initialize()
            logger.info(f"✅ Database initialized: {db_path}")
            
            # 2. IMAP 클라이언트 생성
            logger.info("Initializing IMAP client...")
            self.imap_client = IMAPClient(self.config)
            logger.info("✅ IMAP client created")
            
            # 3. EmailWatcher 생성
            logger.info("Initializing EmailWatcher...")
            poll_interval = int(os.environ.get('EMAIL_POLL_INTERVAL_SECONDS', '300'))
            batch_size = int(os.environ.get('EMAIL_BATCH_SIZE', '20'))
            self.email_watcher = EmailWatcher(
                poll_interval=poll_interval,
                batch_size=batch_size,
                config=self.config,
                process_inline=False,
            )
            logger.info(
                "✅ EmailWatcher created "
                f"(poll_interval={poll_interval}s, batch_size={batch_size}, mode=queue-only)"
            )
            
            # 4. DedupeChecker 생성
            logger.info("Initializing DedupeChecker...")
            cache_size = int(os.environ.get('DEDUP_CACHE_SIZE', '1000'))
            self.dedupe_checker = DedupeChecker(cache_size=cache_size)
            logger.info(f"✅ DedupeChecker created (cache_size={cache_size})")
            
            # 5. DB 스토어 생성
            logger.info("Initializing DB stores...")
            self.email_store = email_store
            self.task_store = task_store
            logger.info("✅ DB stores created")
            
            # 6. EventProcessor 생성
            logger.info("Initializing EventProcessor...")
            self.event_processor = EventProcessor(self.dedupe_checker)
            logger.info("✅ EventProcessor created")
            
            # 7. 헬스체크 등록
            self._register_health_checks()
            logger.info("✅ Health checkers registered")
            
            # 8. 종료 핸들러 등록
            self.shutdown_handler.setup_handlers()
            self.shutdown_handler.register_cleanup(self._cleanup)
            logger.info("✅ Shutdown handlers registered")
            
            self._initialized = True
            logger.info("=" * 60)
            logger.info("✅ Initialization complete")
            logger.info("=" * 60)
            
        except Exception as e:
            logger.error(f"Initialization failed: {e}", exc_info=True)
            raise
    
    def _register_health_checks(self) -> None:
        """헬스체크 등록"""
        # DB 연결 체크
        def check_db() -> bool:
            try:
                from db import get_connection
                with get_connection() as conn:
                    conn.execute("SELECT 1").fetchone()
                    return True
            except Exception as e:
                logger.error(f"DB health check failed: {e}")
                return False
        
        # 선택된 메일 adapter가 초기화되었는지 확인
        def check_mail_adapter() -> bool:
            try:
                return self.email_watcher is not None
            except Exception as e:
                logger.error(f"Mail adapter health check failed: {e}")
                return False
        
        # Slack Socket 체크
        def check_slack() -> bool:
            # 개인용 DB-only 모드에서는 Slack이 선택 사항입니다.
            return True
        
        self.health_checker.register_component('db', check_db)
        self.health_checker.register_component('mail_adapter', check_mail_adapter)
        self.health_checker.register_component('slack_optional', check_slack)
    
    def start(self) -> None:
        """데몬 시작"""
        if not self._initialized:
            raise RuntimeError("Not initialized. Call initialize() first.")
        
        if self._running:
            logger.warning("Already running")
            return
        
        logger.info("=" * 60)
        logger.info("🚀 Starting MailDaemon")
        logger.info("=" * 60)
        
        self._running = True
        
        # Watcher 스레드 시작
        self.watcher_thread = threading.Thread(
            target=self._run_watcher,
            name="EmailWatcher",
            daemon=False
        )
        self.watcher_thread.start()
        logger.info("✅ EmailWatcher thread started")
        
        # 메인 루프 시작
        self._main_loop()
    
    def _run_watcher(self) -> None:
        """Watcher 스레드 실행"""
        try:
            logger.info("EmailWatcher thread running")
            self.email_watcher.start()
        except Exception as e:
            logger.error(f"EmailWatcher thread error: {e}", exc_info=True)
            self.health_checker.log_error(f"Watcher error: {e}")
    
    def _main_loop(self) -> None:
        """메인 이벤트 루프"""
        logger.info("Main loop started")
        
        health_check_interval = int(os.environ.get('HEALTH_CHECK_INTERVAL', '60'))
        last_health_check = 0
        
        while not self.shutdown_handler.is_shutdown_requested():
            try:
                # 1. 이벤트 처리
                events = []
                if self.email_watcher:
                    events = self.email_watcher.get_new_events(timeout=1.0)
                
                if events:
                    logger.info(f"Processing {len(events)} event(s)")
                    for event in events:
                        try:
                            if self.event_processor:
                                if self.event_processor.process_event(event):
                                    self.health_checker.increment_processed_count()
                            else:
                                logger.error("EventProcessor not initialized")
                        except Exception as e:
                            logger.error(f"Event processing error: {e}", exc_info=True)
                            self.health_checker.log_error(f"Event error: {e}")
                    self.health_checker.update_poll_time()
                
                # 2. 주기적 헬스체크
                now = time.time()
                if now - last_health_check >= health_check_interval:
                    self.health_checker.log_status()
                    last_health_check = now
                
                # 3. 짧은 sleep
                time.sleep(0.1)
                
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received")
                self.shutdown_handler.request_shutdown()
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                self.health_checker.log_error(f"Main loop error: {e}")
                time.sleep(1)  # 에러 시 잠시 대기
        
        logger.info("Main loop exited")
    
    def stop(self) -> None:
        """데몬 정지"""
        if not self._running:
            logger.warning("Not running")
            return
        
        logger.info("Stopping MailDaemon...")
        self.shutdown_handler.request_shutdown()

        # Watcher 중지
        if self.email_watcher:
            self.email_watcher.stop()
        
        # Watcher 스레드 종료 대기
        if self.watcher_thread and self.watcher_thread.is_alive():
            logger.info("Waiting for EmailWatcher thread to finish...")
            self.watcher_thread.join(timeout=10)
            if self.watcher_thread.is_alive():
                logger.warning("EmailWatcher thread did not finish in time")
        
        self._running = False
        logger.info("✅ MailDaemon stopped")
    
    def _cleanup(self) -> None:
        """정리 작업"""
        logger.info("Performing cleanup...")
        
        try:
            # 1. EventProcessor 정리
            if self.event_processor:
                logger.debug("Cleaning up EventProcessor")
                # 필요시 정리 작업
            
            # 2. IMAP 클라이언트 정리
            if self.imap_client:
                logger.debug("Cleaning up IMAP client")
                # 필요시 정리 작업
            
            # 3. 로그 플러시
            for handler in logging.getLogger().handlers:
                handler.flush()
            
            logger.info("Cleanup complete")
        except Exception as e:
            logger.error(f"Cleanup error: {e}", exc_info=True)
    
    def run(self) -> int:
        """
        데몬 실행 (블로킹)
        
        Returns:
            종료 코드 (0: 정상, 1: 에러)
        """
        try:
            self.initialize()
            self.start()
            
            # 종료 신호 대기
            while not self.shutdown_handler.is_shutdown_requested():
                time.sleep(1)
            
            self.stop()
            return 0
            
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            return 1
    
    def poll_once(self, dry_run: bool = False, limit: int = 10) -> int:
        """
        단일 폴링만 실행 (테스트용)
        
        Args:
            dry_run: True면 실제 처리 없이 시뮬레이션
            
        Returns:
            종료 코드
        """
        try:
            logger.info("=" * 60)
            logger.info("Running single poll (poll-once mode)")
            logger.info("=" * 60)
            
            # DB 초기화
            db_initialize()
            
            if dry_run:
                from core.mail_adapter import create_mail_adapter

                adapter = create_mail_adapter(self.config)
                logger.info("Fetching email metadata through configured adapter...")
                emails = adapter.fetch_new_emails(limit=limit)
                protocol = "POP3" if self.config.pop_host else "IMAP"
                logger.info(
                    "DRY RUN: %s adapter에서 %d개 메일 메타데이터 확인 (본문/DB 처리/삭제 없음)",
                    protocol,
                    len(emails),
                )
            else:
                # 실제 단일 처리는 상시 daemon과 같은 adapter/processor 경로를 사용합니다.
                watcher = EmailWatcher(
                    poll_interval=0,
                    batch_size=limit,
                    config=self.config,
                    process_inline=True,
                )
                processed = watcher.poll_once()
                logger.info("Processed %d email(s)", processed)
            
            logger.info("=" * 60)
            logger.info("✅ Poll-once complete")
            logger.info("=" * 60)
            return 0
            
        except Exception as e:
            logger.error(f"Poll-once failed: {e}", exc_info=True)
            return 1
