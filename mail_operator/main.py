"""
Operator 메인 모듈

LLM Operator 프로세스의 엔트리포인트입니다.
이벤트를 소비하고 처리하는 메인 루프를 실행합니다.
"""
import asyncio
import logging
import signal
import sys
import os

# 프로젝트 루트를 Python 경로에 추가
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from typing import Optional

from mail_operator.config import OperatorConfig
from mail_operator.cli_runner import configure_provider
from mail_operator.audit import AuditLogger, AuditAction
from mail_operator.mcp_client import MCPClientWrapper
from db import event_store, initialize as db_initialize
from db.event_store import EventType, EventStatus

logger = logging.getLogger(__name__)


class Operator:
    """
    Mail LLM Operator
    
    이벤트 큐에서 이벤트를 가져와 처리하는 메인 루프를 실행합니다.
    """
    
    def __init__(self, config: Optional[OperatorConfig] = None):
        """
        Operator 초기화
        
        Args:
            config: Operator 설정 (None이면 환경변수에서 로드)
        """
        self.config = config or OperatorConfig.from_env()
        configure_provider(
            provider_name=self.config.llm_provider,
            allow_fallback=self.config.llm_allow_fallback,
            model=self.config.model,
        )
        self.audit = AuditLogger(
            operator_instance=self.config.instance_id,
            enabled=self.config.audit_log_enabled
        )
        self.mcp_client: Optional[MCPClientWrapper] = None
        
        self._running = False
        self._shutdown_event = asyncio.Event()
        
        logger.info(f"Operator 초기화: {self.config.instance_id}")
    
    async def start(self):
        """Operator 시작"""
        logger.info("=" * 60)
        logger.info(f"🚀 Mail Operator 시작")
        logger.info(f"   Instance ID: {self.config.instance_id}")
        logger.info(f"   LLM Provider: {self.config.llm_provider}")
        logger.info(f"   Model: {self.config.model or 'CLI account default'}")
        logger.info("=" * 60)
        
        # DB 초기화
        db_initialize()
        
        # MCP 클라이언트 초기화
        self.mcp_client = MCPClientWrapper(self.config)
        await self.mcp_client.connect()
        
        self._running = True
        
        # 시그널 핸들러 등록
        self._setup_signal_handlers()
        
        # 메인 루프 실행
        await self._main_loop()
    
    async def stop(self):
        """Operator 중지"""
        logger.info("Operator 중지 중...")
        self._running = False
        self._shutdown_event.set()
        
        if self.mcp_client:
            await self.mcp_client.disconnect()
        
        logger.info("✅ Operator 중지 완료")

    async def run_once(self) -> int:
        """대기하지 않고 현재 pending 이벤트를 설정 개수만큼 한 번 처리합니다."""
        db_initialize()
        self.mcp_client = MCPClientWrapper(self.config)
        await self.mcp_client.connect()
        try:
            events = event_store.lease_next_event(
                owner=self.config.instance_id,
                lease_seconds=self.config.event_lease_seconds,
                max_count=self.config.max_events_per_poll,
            )
            for event in events:
                await self._process_event(event)
            logger.info("Operator one-shot 처리: %d event(s)", len(events))
            return len(events)
        finally:
            await self.mcp_client.disconnect()
    
    def _setup_signal_handlers(self):
        """시그널 핸들러 설정"""
        try:
            loop = asyncio.get_running_loop()
            
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._handle_signal)
        except (NotImplementedError, RuntimeError):
            # Windows에서는 add_signal_handler가 지원되지 않음
            pass
    
    def _handle_signal(self):
        """종료 시그널 처리"""
        logger.info("종료 시그널 수신")
        asyncio.create_task(self.stop())
    
    async def _main_loop(self):
        """메인 이벤트 처리 루프"""
        logger.info("메인 루프 시작")
        
        while self._running:
            try:
                # 1. 이벤트 가져오기
                events = event_store.lease_next_event(
                    owner=self.config.instance_id,
                    lease_seconds=self.config.event_lease_seconds,
                    max_count=self.config.max_events_per_poll
                )
                
                if events:
                    # 2. 이벤트 처리
                    for event in events:
                        await self._process_event(event)
                else:
                    # 이벤트가 없으면 대기
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(),
                            timeout=self.config.event_poll_interval_seconds
                        )
                        break  # 종료 요청
                    except asyncio.TimeoutError:
                        pass  # 타임아웃 - 다시 폴링
                
            except Exception as e:
                logger.error(f"메인 루프 에러: {e}", exc_info=True)
                await asyncio.sleep(self.config.retry_delay_seconds)
        
        logger.info("메인 루프 종료")
    
    async def _process_event(self, event: dict):
        """단일 이벤트 처리"""
        event_id = event['id']
        event_type = event['event_type']
        lease_owner = event.get("lease_owner") or self.config.instance_id
        
        logger.info(f"이벤트 처리: {event_id} ({event_type})")
        
        self.audit.log(
            AuditAction.EVENT_RECEIVED,
            details={"event_type": event_type},
            event_id=event_id
        )
        
        try:
            # 이벤트 타입별 핸들러 호출
            if event_type == EventType.NEW_EMAIL:
                success, error = await self._handle_new_email(event)
            elif event_type == EventType.APPROVAL_GRANTED:
                success, error = await self._handle_approval_granted(event)
            elif event_type == EventType.APPROVAL_DENIED:
                success, error = await self._handle_approval_denied(event)
            elif event_type == EventType.SEND_CONFIRMED:
                success, error = await self._handle_send_confirmed(event)
            elif event_type == EventType.SLACK_CONVERSATION:
                success, error = await self._handle_slack_conversation(event)
            else:
                logger.warning(f"알 수 없는 이벤트 타입: {event_type}")
                success, error = False, f"Unknown event type: {event_type}"
            
            # 결과에 따라 ack 또는 fail
            if success:
                event_store.ack_event(event_id, owner=lease_owner)
                logger.info(f"이벤트 처리 완료: {event_id}")
            else:
                event_store.fail_event(
                    event_id,
                    error or "Unknown error",
                    retryable=True,
                    owner=lease_owner,
                    base_delay_seconds=self.config.event_retry_base_seconds,
                    max_delay_seconds=self.config.event_retry_max_seconds,
                )
                logger.warning(f"이벤트 처리 실패: {event_id} - {error}")
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"이벤트 처리 에러: {event_id} - {error_msg}", exc_info=True)
            event_store.fail_event(
                event_id,
                error_msg,
                retryable=True,
                owner=lease_owner,
                base_delay_seconds=self.config.event_retry_base_seconds,
                max_delay_seconds=self.config.event_retry_max_seconds,
            )
    
    async def _handle_new_email(self, event: dict):
        """new_email 이벤트 처리"""
        from mail_operator.handlers.new_email import handle_new_email
        return await handle_new_email(event, self.mcp_client, self.audit, self.config)
    
    async def _handle_approval_granted(self, event: dict):
        """approval_granted 이벤트 처리"""
        from mail_operator.handlers.approval import handle_approval_granted
        return await handle_approval_granted(event, self.mcp_client, self.audit, self.config)
    
    async def _handle_approval_denied(self, event: dict):
        """approval_denied 이벤트 처리"""
        from mail_operator.handlers.approval import handle_approval_denied
        return await handle_approval_denied(event, self.mcp_client, self.audit, self.config)

    async def _handle_send_confirmed(self, event: dict):
        """send_confirmed 이벤트 처리"""
        from mail_operator.handlers.approval import handle_send_confirmed
        return await handle_send_confirmed(
            event=event,
            mcp_client=self.mcp_client,
            audit=self.audit,
            config=self.config,
        )

    async def _handle_slack_conversation(self, event: dict):
        """Persisted Slack conversation request processing."""
        from mail_operator.handlers.slack_conversation import handle_slack_conversation

        return await handle_slack_conversation(
            event=event,
            mcp_client=self.mcp_client,
            audit=self.audit,
            config=self.config,
        )


async def run_operator(config: Optional[OperatorConfig] = None):
    """Operator 실행"""
    operator = Operator(config)
    
    try:
        await operator.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    finally:
        await operator.stop()


async def run_operator_once(config: Optional[OperatorConfig] = None) -> int:
    """CLI one-shot 실행 진입점."""
    operator = Operator(config)
    return await operator.run_once()


def main():
    """CLI 엔트리포인트"""
    import argparse

    # Windows PowerShell/콘솔의 기본 코드페이지(cp932 등)에서
    # 한글/일본어 help 메시지 출력 시 UnicodeEncodeError가 발생할 수 있어
    # 가능하면 stdout/stderr를 UTF-8로 재설정합니다.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    
    parser = argparse.ArgumentParser(description="Mail LLM Operator")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="로그 레벨"
    )
    parser.add_argument(
        "--instance-id",
        default=None,
        help="Operator 인스턴스 ID"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="현재 pending 이벤트를 한 번만 처리하고 종료"
    )

    args = parser.parse_args()

    # 로깅 설정
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 설정 로드
    config = OperatorConfig.from_env()
    if args.instance_id:
        config.instance_id = args.instance_id
    config.log_level = args.log_level

    # 이벤트 루프 실행
    if args.once:
        processed = asyncio.run(run_operator_once(config))
        logger.info("one-shot 완료: %d event(s)", processed)
    else:
        asyncio.run(run_operator(config))


def run():
    """`python -m mail_operator` 호환 엔트리포인트."""
    main()


if __name__ == "__main__":
    main()
