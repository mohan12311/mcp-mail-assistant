"""
MCP Mail Assistant - 파일 파서 베이스
파일 확장자에 따라 적절한 파서 호출
"""
import os
from typing import Optional
from core.models import AttachmentAnalysis


SUPPORTED_EXTENSIONS = {
    ".docx": "word",
    ".doc": "word",
    ".xlsx": "excel",
    ".xls": "excel",
    ".pptx": "ppt",
    ".ppt": "ppt",
    ".pdf": "pdf",
}


def get_supported_extensions() -> list[str]:
    """지원하는 파일 확장자 목록"""
    return list(SUPPORTED_EXTENSIONS.keys())


def get_file_type(filename: str) -> str:
    """파일명에서 파일 타입 추출"""
    ext = os.path.splitext(filename)[1].lower()
    return SUPPORTED_EXTENSIONS.get(ext, "unknown")


def parse_file(filepath: str) -> Optional[AttachmentAnalysis]:
    """파일 경로로 적절한 파서 호출"""
    from .word_parser import parse_word
    from .excel_parser import parse_excel
    from .ppt_parser import parse_ppt
    from .pdf_parser import parse_pdf
    
    file_type = get_file_type(filepath)
    
    parsers = {
        "word": parse_word,
        "excel": parse_excel,
        "ppt": parse_ppt,
        "pdf": parse_pdf,
    }
    
    parser = parsers.get(file_type)
    if parser:
        return parser(filepath)
    
    return None
