"""
CLI 인터페이스
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def cmd_start(args) -> int:
    """데몬 시작"""
    from daemon.logging_config import setup_logging
    setup_logging(args.verbose)
    
    logger.info("Starting MCP Mail Assistant daemon...")
    
    # PID 파일 체크
    if args.pid_file:
        pid_file = Path(args.pid_file)
        if pid_file.exists():
            logger.warning(f"PID file already exists: {pid_file}")
            try:
                with open(pid_file, 'r') as f:
                    old_pid = f.read().strip()
                logger.warning(f"Previous PID: {old_pid}")
                logger.warning("If the process is not running, delete the PID file manually.")
            except Exception as e:
                logger.error(f"Failed to read PID file: {e}")
        
        # 현재 PID 저장
        try:
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            with open(pid_file, 'w') as f:
                f.write(str(os.getpid()))
            logger.info(f"PID file created: {pid_file}")
        except Exception as e:
            logger.error(f"Failed to create PID file: {e}")
    
    try:
        # Config 로드
        from core.config import Config
        from daemon.main_loop import MailDaemon

        config = None
        if args.config:
            logger.info(f"Loading config from: {args.config}")
            config = Config.from_file(args.config)
        else:
            config = Config.from_env()
        
        # Daemon 실행
        daemon = MailDaemon(config, verbose=args.verbose)
        exit_code = daemon.run()
        
        return exit_code
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 0
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1
    finally:
        # PID 파일 삭제
        if args.pid_file:
            pid_file = Path(args.pid_file)
            if pid_file.exists():
                try:
                    pid_file.unlink()
                    logger.info(f"PID file removed: {pid_file}")
                except Exception as e:
                    logger.error(f"Failed to remove PID file: {e}")


def cmd_stop(args) -> int:
    """실행 중인 데몬 종료"""
    from daemon.logging_config import setup_logging
    setup_logging(args.verbose)
    
    pid_file = Path(args.pid_file)
    
    if not pid_file.exists():
        logger.error(f"PID file not found: {pid_file}")
        logger.error("Is the daemon running?")
        return 1
    
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        
        logger.info(f"Stopping daemon (PID: {pid})...")
        
        # Windows/Linux 모두 지원
        import signal
        try:
            if sys.platform == "win32":
                os.kill(pid, signal.CTRL_C_EVENT)
            else:
                os.kill(pid, signal.SIGTERM)
            logger.info("SIGTERM sent")
        except ProcessLookupError:
            logger.warning(f"Process {pid} not found")
            # PID 파일 삭제
            pid_file.unlink()
            return 1
        except Exception as e:
            logger.error(f"Failed to send signal: {e}")
            return 1
        
        # 종료 대기
        import time
        for i in range(30):
            try:
                os.kill(pid, 0)  # 프로세스 존재 확인
                time.sleep(1)
            except ProcessLookupError:
                logger.info("Daemon stopped")
                # PID 파일 삭제
                if pid_file.exists():
                    pid_file.unlink()
                return 0
        
        logger.warning("Daemon did not stop in 30 seconds")
        return 1
        
    except Exception as e:
        logger.error(f"Failed to stop daemon: {e}", exc_info=True)
        return 1


def cmd_status(args) -> int:
    """현재 상태 출력"""
    from daemon.logging_config import setup_logging
    setup_logging(args.verbose)
    
    pid_file = Path(args.pid_file)
    
    # PID 파일 체크
    if pid_file.exists():
        try:
            with open(pid_file, 'r') as f:
                pid = int(f.read().strip())
            
            # 프로세스 존재 확인
            try:
                os.kill(pid, 0)
                print(f"[OK] Daemon is RUNNING (PID: {pid})")
                print(f"     PID file: {pid_file}")
                return 0
            except ProcessLookupError:
                print(f"[WARN] Daemon is NOT RUNNING (stale PID file)")
                print(f"       Stale PID: {pid}")
                print(f"       PID file: {pid_file}")
                print(f"       Cleanup: Delete the PID file manually")
                return 1
        except Exception as e:
            print(f"[WARN] Error reading PID file: {e}")
            return 1
    else:
        print(f"[INFO] Daemon is NOT RUNNING")
        print(f"       No PID file found: {pid_file}")
        return 1


def cmd_health(args) -> int:
    """헬스체크 실행"""
    from daemon.logging_config import setup_logging
    setup_logging(args.verbose)
    
    try:
        from daemon.health_check import get_health_checker
        from core.config import Config
        from db import initialize as db_initialize

        runtime_config = Config.from_env()

        # DB 초기화 (연결 테스트용)
        db_initialize()
        
        # 헬스체크 실행
        health_checker = get_health_checker()
        
        # 기본 컴포넌트 체크 등록
        def check_db() -> bool:
            try:
                from db import get_connection
                with get_connection() as conn:
                    conn.execute("SELECT 1").fetchone()
                    return True
            except Exception:
                return False
        
        def check_slack() -> bool:
            # 개인용 DB-only 모드에서는 Slack이 선택 사항입니다.
            return True

        def check_mail_config() -> bool:
            pop_ready = all((
                runtime_config.pop_host,
                runtime_config.pop_user,
                runtime_config.pop_pass,
            ))
            imap_ready = all((
                runtime_config.imap_host,
                runtime_config.imap_user,
                runtime_config.imap_pass,
            ))
            return bool(pop_ready or imap_ready)

        def check_llm_cli() -> bool:
            from mail_operator.cli_runner import CliProvider

            provider = CliProvider(
                provider_name=os.environ.get("LLM_PROVIDER", "claude"),
                allow_fallback=False,
            )
            if not provider.cli:
                return False
            if not getattr(args, "live_llm", False):
                return True
            result = provider.analyze(
                "Return a JSON object with ok=true. Do nothing else.",
                timeout=60,
                schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            )
            if not result["success"]:
                logger.error("LLM live health check failed: %s", result["error"])
            return bool(result["success"])
        
        health_checker.register_component('db', check_db)
        health_checker.register_component('mail_config', check_mail_config)
        llm_component = 'llm_runtime' if getattr(args, "live_llm", False) else 'llm_cli_installed'
        health_checker.register_component(llm_component, check_llm_cli)
        health_checker.register_component('slack_optional', check_slack)
        
        # 상태 확인
        status = health_checker.check_all()
        
        # 콘솔 출력
        print("=" * 60)
        print("MCP Mail Assistant Health Check")
        print("=" * 60)
        print(f"Timestamp: {status.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Status: {'[HEALTHY]' if status.healthy else '[UNHEALTHY]'}")
        print()
        
        print("Components:")
        for name, is_healthy in status.components.items():
            status_mark = "[OK]" if is_healthy else "[FAIL]"
            print(f"  {status_mark} {name}")
        print()
        
        print("Metrics:")
        for key, value in status.metrics.items():
            if value is not None:
                print(f"  {key}: {value}")
        print()
        
        if status.errors:
            print("Errors:")
            for error in status.errors:
                print(f"  [WARN] {error}")
            print()
        
        # JSON 출력 (옵션)
        if args.json:
            print("=" * 60)
            print("JSON Output:")
            print("=" * 60)
            print(status.to_json())
        
        return 0 if status.healthy else 1
        
    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        print(f"❌ Health check error: {e}")
        return 1


def cmd_poll_once(args) -> int:
    """단일 폴링만 실행 (테스트용)"""
    from daemon.logging_config import setup_logging
    setup_logging(args.verbose)
    
    logger.info("Running poll-once...")
    
    try:
        from core.config import Config
        from daemon.main_loop import MailDaemon

        config = Config.from_env()
        daemon = MailDaemon(config, verbose=args.verbose)
        
        return daemon.poll_once(
            dry_run=args.dry_run,
            limit=max(1, min(args.limit, 100)),
        )
    
    except Exception as e:
        logger.error(f"Poll-once failed: {e}", exc_info=True)
        return 1


def cmd_init_db(args) -> int:
    """DB 초기화만 실행"""
    from daemon.logging_config import setup_logging
    setup_logging(args.verbose)
    
    try:
        from db import initialize as db_initialize

        logger.info("Initializing database...")
        
        # DB 경로 확인
        db_path = os.environ.get('DB_PATH', './db/mail_agent.db')
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # 초기화
        db_initialize()
        
        logger.info(f"Database initialized: {db_path}")
        print(f"[OK] Database initialized successfully")
        print(f"     Path: {db_path}")
        
        return 0
    
    except Exception as e:
        logger.error(f"DB initialization failed: {e}", exc_info=True)
        print(f"[ERROR] DB initialization failed: {e}")
        return 1


def main() -> int:
    """CLI 메인 함수"""
    parser = argparse.ArgumentParser(
        prog='daemon.py',
        description='MCP Mail Assistant - AI-powered email assistant daemon',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 daemon.py start                    Start daemon
  python3 daemon.py start --verbose          Start with verbose logging
  python3 daemon.py stop                     Stop daemon
  python3 daemon.py status                   Check daemon status
  python3 daemon.py health                   Run health check
  python3 daemon.py poll-once --verbose      Run single poll (test mode)
  python3 daemon.py init-db                  Initialize database
        """
    )
    
    # 전역 옵션
    parser.add_argument('--config', '-c', help='Config file path')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')
    parser.add_argument('--pid-file', default='./mail-agent.pid', help='PID file path')
    
    # 서브커맨드
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # start
    parser_start = subparsers.add_parser('start', help='Start daemon')
    parser_start.set_defaults(func=cmd_start)
    
    # stop
    parser_stop = subparsers.add_parser('stop', help='Stop daemon')
    parser_stop.set_defaults(func=cmd_stop)
    
    # status
    parser_status = subparsers.add_parser('status', help='Show daemon status')
    parser_status.set_defaults(func=cmd_status)
    
    # health
    parser_health = subparsers.add_parser('health', help='Run health check')
    parser_health.add_argument('--json', action='store_true', help='Output JSON')
    parser_health.add_argument(
        '--live-llm',
        action='store_true',
        help='Make one minimal structured LLM call to verify authentication',
    )
    parser_health.set_defaults(func=cmd_health)
    
    # poll-once
    parser_poll = subparsers.add_parser('poll-once', help='Run single poll (test mode)')
    parser_poll.add_argument('--dry-run', action='store_true', help='Simulate without processing')
    parser_poll.add_argument('--limit', type=int, default=10, help='Maximum emails to inspect/process')
    parser_poll.set_defaults(func=cmd_poll_once)
    
    # init-db
    parser_init = subparsers.add_parser('init-db', help='Initialize database')
    parser_init.set_defaults(func=cmd_init_db)
    
    # 파싱
    args = parser.parse_args()
    
    # 커맨드 없으면 help 출력
    if not args.command:
        parser.print_help()
        return 0
    
    # 커맨드 실행
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
