"""
MCP Mail Assistant - Daemon 패키지

메인 이벤트 루프 및 데몬 관리 기능을 제공합니다.
"""
__all__ = [
    'MailDaemon',
    'HealthChecker',
    'HealthStatus',
    'get_health_checker',
    'ShutdownHandler',
    'get_shutdown_handler',
    'is_shutdown_requested',
    'setup_logging',
]

__version__ = '0.1.0'


def __getattr__(name):
    if name == 'MailDaemon':
        from daemon.main_loop import MailDaemon
        return MailDaemon
    if name in {'HealthChecker', 'HealthStatus', 'get_health_checker'}:
        from daemon import health_check
        return getattr(health_check, name)
    if name in {'ShutdownHandler', 'get_shutdown_handler', 'is_shutdown_requested'}:
        from daemon import graceful_shutdown
        return getattr(graceful_shutdown, name)
    if name == 'setup_logging':
        from daemon.logging_config import setup_logging
        return setup_logging
    raise AttributeError(f"module 'daemon' has no attribute {name!r}")
