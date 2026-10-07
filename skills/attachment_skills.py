"""
MCP Mail Assistant - 첨부파일 관련 Skills
get_attachments, download_attachment, parse_attachment
"""
import os
from typing import Optional

from core import Config, IMAPClient
from parsers import parse_file, get_supported_extensions


# 글로벌 설정 참조
_config: Optional[Config] = None


def init_config(config: Config) -> None:
    """설정 초기화"""
    global _config
    _config = config


def get_config() -> Config:
    """현재 설정 반환"""
    if _config is None:
        # mail_skills에서 초기화된 설정 사용 시도
        from skills.mail_skills import get_config as mail_get_config
        return mail_get_config()
    return _config


def download_attachment(
    email_id: str,
    filename: str,
    folder: str = "INBOX"
) -> dict:
    """
    특정 첨부파일 다운로드 (파싱 없이)
    
    Args:
        email_id: 메일 ID
        filename: 다운로드할 첨부파일명
        folder: 메일함 이름 (기본: INBOX)
    
    Returns:
        {filename, size, size_readable, content_type, local_path}
    """
    config = get_config()
    client = IMAPClient(config)
    
    with client.connection():
        email = client.read_email(email_id, folder=folder)
        
        # 해당 파일 찾기
        target_att = None
        for att in email.attachments:
            if att.filename == filename:
                target_att = att
                break
        
        if not target_att:
            return {"error": f"첨부파일 '{filename}'을 찾을 수 없습니다."}
        
        # 다운로드
        local_path = client.download_attachment(
            email_id,
            filename,
            save_dir=config.attachment_dir,
            folder=folder
        )
        
        if not local_path:
            return {"error": f"첨부파일 다운로드에 실패했습니다."}
        
        return {
            "filename": target_att.filename,
            "size": target_att.size,
            "size_readable": _format_size(target_att.size),
            "content_type": target_att.content_type,
            "local_path": local_path,
            "is_supported": _is_supported_file(target_att.filename),
        }


def get_attachments(
    email_id: str,
    download: bool = False,
    folder: str = "INBOX"
) -> list[dict]:
    """
    메일의 첨부파일 목록 조회 및 다운로드
    
    Args:
        email_id: 메일 ID
        download: 다운로드 여부 (기본: False)
        folder: 메일함 이름 (기본: INBOX)
    
    Returns:
        첨부파일 목록 [{filename, size, content_type, local_path?}, ...]
    """
    config = get_config()
    client = IMAPClient(config)
    
    with client.connection():
        email = client.read_email(email_id, folder=folder)
        
        results = []
        for att in email.attachments:
            item = {
                "filename": att.filename,
                "size": att.size,
                "size_readable": _format_size(att.size),
                "content_type": att.content_type,
                "is_supported": _is_supported_file(att.filename),
                "local_path": None,
            }
            
            if download:
                local_path = client.download_attachment(
                    email_id, 
                    att.filename,
                    save_dir=config.attachment_dir
                )
                item["local_path"] = local_path
            
            results.append(item)
    
    return results


def parse_attachment(
    email_id: str,
    filename: str,
    folder: str = "INBOX"
) -> Optional[dict]:
    """
    첨부파일 다운로드 및 내용 추출 (AI 분석 없음)
    
    Word, Excel, PowerPoint, PDF 파일의 내용을 추출합니다.
    Agent가 직접 extracted_text를 분석하여 핵심 포인트를 추출하세요.
    
    Args:
        email_id: 메일 ID
        filename: 첨부파일명
        folder: 메일함 이름 (기본: INBOX)
    
    Returns:
        {filename, file_type, extracted_text, text_length, tables, page_count, sheet_names, local_path}
        또는 지원하지 않는 파일인 경우 error
    """
    config = get_config()
    
    if not _is_supported_file(filename):
        return {
            "error": f"지원하지 않는 파일 형식입니다. 지원 형식: {', '.join(get_supported_extensions())}"
        }
    
    client = IMAPClient(config)
    
    with client.connection():
        # 첨부파일 다운로드
        local_path = client.download_attachment(
            email_id,
            filename,
            save_dir=config.attachment_dir,
            folder=folder
        )
        
        if not local_path:
            return {"error": f"첨부파일 '{filename}'을 찾을 수 없습니다."}
    
    # 파일 파싱
    try:
        analysis = parse_file(local_path)
        
        if analysis is None:
            return {"error": "파일 분석에 실패했습니다."}
        
        return {
            "filename": analysis.filename,
            "file_type": analysis.file_type,
            "extracted_text": analysis.extracted_text[:5000],  # 길이 제한
            "text_length": len(analysis.extracted_text),
            "tables": analysis.tables,
            "page_count": analysis.page_count,
            "sheet_names": analysis.sheet_names,
            "local_path": local_path,
        }
    
    except Exception as e:
        return {"error": f"파일 분석 중 오류: {str(e)}"}


# 이전 함수명과의 호환성을 위한 별칭
analyze_attachment = parse_attachment


def _is_supported_file(filename: str) -> bool:
    """지원하는 파일 형식인지 확인"""
    ext = os.path.splitext(filename)[1].lower()
    return ext in get_supported_extensions()


def _format_size(size: int) -> str:
    """파일 크기를 읽기 쉬운 형식으로 변환"""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
