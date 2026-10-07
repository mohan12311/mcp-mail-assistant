"""
DB 초기화 스크립트

SQLite DB 파일과 테이블을 생성합니다.
"""

import os
import logging
from db.connection import get_connection, get_db_path
from db.migration_runner import run_migrations

logger = logging.getLogger(__name__)


def initialize() -> None:
    """
    DB를 초기화합니다.
    
    - db/ 디렉토리가 없으면 생성
    - mail_agent.db 파일이 없으면 생성
    - 버전형 migration을 순서대로 적용
    - migration 이름/checksum과 적용 시각 기록
    - 기존 비버전 DB는 실제 스키마 객체를 확인해 안전하게 채택
    
    Raises:
        Exception: DB 초기화 실패 시
    """
    try:
        db_path = get_db_path()
        db_dir = os.path.dirname(db_path)
        
        # db/ 디렉토리 생성
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)
            logger.info(f"DB 디렉토리 생성: {db_dir}")
        
        # DB 파일 생성 및 migration 적용
        with get_connection() as conn:
            applied = run_migrations(conn)
            logger.info(
                "DB 초기화 완료: %s (applied migrations=%s)",
                db_path,
                applied or "none",
            )

            # 테이블 확인
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            logger.info(f"생성된 테이블: {', '.join(tables)}")
            
    except Exception as e:
        logger.error(f"DB 초기화 실패: {e}")
        raise


if __name__ == "__main__":
    # 직접 실행 시 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    initialize()
    print("✅ DB 초기화 완료")
