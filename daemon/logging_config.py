"""
로깅 설정 모듈
"""
import logging
import logging.handlers
import os
from pathlib import Path


def setup_logging(verbose: bool = False) -> None:
    """
    로깅 설정
    
    Args:
        verbose: 상세 로깅 활성화 여부
    """
    level = logging.DEBUG if verbose else logging.INFO
    
    # 콘솔 핸들러
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    
    # 파일 핸들러 (로테이션)
    log_dir = os.environ.get('LOG_DIR', './logs')
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    
    log_file = os.path.join(log_dir, 'mail-agent.log')
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    
    # 루트 로거 설정
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    
    # 기존 핸들러 제거 (중복 방지)
    root.handlers.clear()
    
    root.addHandler(console)
    root.addHandler(file_handler)
    
    logging.info(f"Logging initialized (level={'DEBUG' if verbose else 'INFO'}, file={log_file})")

