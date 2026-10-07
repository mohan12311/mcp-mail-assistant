"""
DB 연결 관리자

SQLite 연결을 관리하고 context manager를 제공합니다.
"""

import os
import sqlite3
import logging
from contextlib import contextmanager
from typing import Generator

logger = logging.getLogger(__name__)


def get_db_path() -> str:
    """
    DB 파일 경로를 반환합니다.
    
    Returns:
        str: DB 파일 절대 경로
    """
    db_path = os.environ.get("DB_PATH", "./db/mail_agent.db")
    return os.path.abspath(db_path)


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """
    SQLite 연결을 제공하는 context manager입니다.
    
    사용 예:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM emails")
    
    Yields:
        sqlite3.Connection: SQLite 연결 객체
    """
    db_path = get_db_path()
    conn = None
    
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row  # dict-like access
        logger.debug(f"DB 연결 성공: {db_path}")
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"DB 연결 오류: {e}")
        raise
    finally:
        if conn:
            conn.close()
            logger.debug("DB 연결 종료")
