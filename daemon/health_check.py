"""
헬스체크 모듈
"""
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    """시스템 헬스 상태"""
    timestamp: datetime
    healthy: bool
    components: dict[str, bool] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        """딕셔너리로 변환"""
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        return data
    
    def to_json(self, indent: int = 2) -> str:
        """JSON 문자열로 변환"""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


class HealthChecker:
    """
    시스템 헬스 체커
    
    각 컴포넌트의 상태를 모니터링하고 메트릭을 수집합니다.
    """
    
    def __init__(self):
        self.start_time = time.time()
        self.last_poll_time: Optional[datetime] = None
        self.emails_processed_today = 0
        self.errors_last_hour: list[tuple[datetime, str]] = []
        self._component_checkers: dict[str, callable] = {}
        
    def register_component(self, name: str, checker: callable) -> None:
        """
        컴포넌트 체커 등록
        
        Args:
            name: 컴포넌트 이름
            checker: 상태 확인 함수 (반환값: bool)
        """
        self._component_checkers[name] = checker
        logger.debug(f"Registered health checker for component: {name}")
    
    def update_poll_time(self) -> None:
        """마지막 폴링 시간 갱신"""
        self.last_poll_time = datetime.now()
    
    def increment_processed_count(self, count: int = 1) -> None:
        """처리된 메일 수 증가"""
        self.emails_processed_today += count
    
    def log_error(self, error: str) -> None:
        """에러 로그"""
        self.errors_last_hour.append((datetime.now(), error))
        # 1시간 이상 된 에러 제거
        cutoff = time.time() - 3600
        self.errors_last_hour = [
            (ts, err) for ts, err in self.errors_last_hour
            if ts.timestamp() > cutoff
        ]
    
    def check_all(self) -> HealthStatus:
        """모든 컴포넌트 상태 확인"""
        timestamp = datetime.now()
        components = {}
        errors = []
        
        # 각 컴포넌트 체크
        for name, checker in self._component_checkers.items():
            try:
                is_healthy = checker()
                components[name] = is_healthy
                if not is_healthy:
                    errors.append(f"{name} is unhealthy")
            except Exception as e:
                logger.error(f"Health check failed for {name}: {e}")
                components[name] = False
                errors.append(f"{name} check failed: {str(e)}")
        
        # 메트릭 수집
        uptime = int(time.time() - self.start_time)
        metrics = {
            'uptime_seconds': uptime,
            'uptime_human': self._format_uptime(uptime),
            'last_poll_time': self.last_poll_time.isoformat() if self.last_poll_time else None,
            'emails_processed_today': self.emails_processed_today,
            'errors_last_hour': len(self.errors_last_hour),
        }
        
        # 최근 에러 추가
        if self.errors_last_hour:
            recent_errors = [err for _, err in self.errors_last_hour[-5:]]
            errors.extend(recent_errors)
        
        # 전체 상태 판단
        healthy = all(components.values()) and len(self.errors_last_hour) < 10
        
        return HealthStatus(
            timestamp=timestamp,
            healthy=healthy,
            components=components,
            metrics=metrics,
            errors=errors
        )
    
    def is_healthy(self) -> bool:
        """시스템이 건강한지 확인"""
        status = self.check_all()
        return status.healthy
    
    def get_status_json(self) -> str:
        """상태를 JSON 문자열로 반환"""
        status = self.check_all()
        return status.to_json()
    
    def log_status(self) -> None:
        """현재 상태를 로그에 기록"""
        status = self.check_all()
        
        if status.healthy:
            logger.info("🟢 System healthy")
        else:
            logger.warning("🟡 System unhealthy")
        
        # 컴포넌트 상태
        for name, is_healthy in status.components.items():
            emoji = "✅" if is_healthy else "❌"
            logger.info(f"  {emoji} {name}")
        
        # 메트릭
        logger.info(f"  ⏱️  Uptime: {status.metrics['uptime_human']}")
        if status.metrics['last_poll_time']:
            logger.info(f"  📧 Last poll: {status.metrics['last_poll_time']}")
        logger.info(f"  📊 Processed today: {status.metrics['emails_processed_today']}")
        logger.info(f"  ⚠️  Errors (1h): {status.metrics['errors_last_hour']}")
        
        # 에러
        if status.errors:
            logger.warning(f"  Errors: {', '.join(status.errors[:3])}")
    
    def _format_uptime(self, seconds: int) -> str:
        """업타임을 읽기 쉬운 형식으로 포맷"""
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        
        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if secs > 0 or not parts:
            parts.append(f"{secs}s")
        
        return " ".join(parts)


# 전역 인스턴스
_health_checker: Optional[HealthChecker] = None


def get_health_checker() -> HealthChecker:
    """전역 HealthChecker 인스턴스 반환"""
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker()
    return _health_checker

