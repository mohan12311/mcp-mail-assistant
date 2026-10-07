"""
Mail Operator - LLM 오퍼레이터

KIRA 스타일의 3-프로세스 아키텍처에서 LLM 판단을 담당합니다.
- 이벤트 소비: DB 기반 이벤트 큐에서 이벤트를 가져와 처리
- 메일 분석: 분류, 요약, 태스크 생성
- 승인 흐름: 위험도에 따라 자동 실행 또는 승인 요청
- 실행 및 감사: 승인된 작업 실행 및 로그 기록

Daemon(raw data 공급자)과 분리되어 독립적으로 동작합니다.
MCP 도구를 통해 DB/Slack/이메일/태스크를 조작합니다.

Note: Python 기본 라이브러리 `operator`와 충돌을 피하기 위해
      패키지명을 `mail_operator`로 지정했습니다.
"""

from mail_operator.config import OperatorConfig
from mail_operator.main import run_operator

__all__ = [
    "OperatorConfig",
    "run_operator",
]
