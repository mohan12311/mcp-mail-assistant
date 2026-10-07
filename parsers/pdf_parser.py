"""
MCP Mail Assistant - PDF 파서
.pdf 파일에서 텍스트와 표 추출
"""
import os
from typing import Optional
from core.models import AttachmentAnalysis


def parse_pdf(filepath: str) -> Optional[AttachmentAnalysis]:
    """PDF 파일 파싱"""
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("pdfplumber 패키지가 필요합니다: pip install pdfplumber")
    
    if not os.path.exists(filepath):
        return None
    
    filename = os.path.basename(filepath)
    
    text_parts = []
    tables = []
    page_count = 0
    
    with pdfplumber.open(filepath) as pdf:
        page_count = len(pdf.pages)
        
        for i, page in enumerate(pdf.pages, 1):
            # 텍스트 추출
            text = page.extract_text()
            if text:
                text_parts.append(f"[페이지 {i}]")
                text_parts.append(text)
                text_parts.append("")
            
            # 표 추출 (최대 5개까지)
            page_tables = page.extract_tables()
            for j, table in enumerate(page_tables[:5]):
                if table:
                    # 첫 행을 헤더로 가정
                    headers = table[0] if table else []
                    rows = table[1:] if len(table) > 1 else []
                    
                    # None 값 빈 문자열로 변환
                    headers = [str(h) if h else "" for h in headers]
                    rows = [[str(c) if c else "" for c in row] for row in rows]
                    
                    tables.append({
                        "page": i,
                        "table_index": j + 1,
                        "headers": headers,
                        "rows": rows,
                    })
    
    extracted_text = "\n".join(text_parts)
    
    return AttachmentAnalysis(
        filename=filename,
        file_type="pdf",
        extracted_text=extracted_text,
        tables=tables if tables else None,
        page_count=page_count,
        key_points=[],
    )
