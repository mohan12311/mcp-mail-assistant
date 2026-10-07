"""
Attachment Tools - MCP Tools for attachment operations
skills/attachment_skills.py의 함수들을 MCP Tool로 래핑
"""
from mcp.types import Tool, TextContent

from skills.attachment_skills import get_attachments, parse_attachment, download_attachment


def get_attachment_tools() -> list[Tool]:
    """첨부파일 도구 목록 반환"""
    return [
            Tool(
                name="attachment_list",
                description="""메일의 첨부파일 목록 조회

지정된 메일의 첨부파일 목록을 조회하고, 
선택적으로 로컬에 다운로드합니다.

반환 정보: 파일명, 크기, 타입, 분석 가능 여부, 다운로드 경로

먼저 email_list로 메일 ID를 확인하세요.""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "email_id": {
                            "type": "string",
                            "description": "메일 ID (email_list에서 확인)"
                        },
                        "download": {
                            "type": "boolean",
                            "description": "첨부파일 다운로드 여부 (기본: false)",
                            "default": False
                        },
                        "folder": {
                            "type": "string",
                            "description": "메일함 이름 (기본: INBOX)",
                            "default": "INBOX"
                        }
                    },
                    "required": ["email_id"]
                }
            ),
            Tool(
                name="attachment_parse",
                description="""첨부파일 내용 추출 (Agent 분석용)

Word, Excel, PowerPoint, PDF 파일의 텍스트 내용을 추출합니다.
Agentが直接 extracted_text を分析して核心ポイントを抽出してください。

지원 형식: .docx, .xlsx, .pptx, .pdf

반환 정보: 파일명, 파일 타입, 추출된 텍스트, 테이블 데이터, 페이지 수

Agent 분석 가이드:
- 추출된 텍스트에서 핵심 포인트 3-5개를 식별하세요
- 비즈니스 맥락에서 중요한 내용, 수치, 날짜, 요청사항을 우선 추출
- Excel 파일은 테이블 데이터도 참고하세요

먼저 attachment_list로 정확한 파일명을 확인하세요.""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "email_id": {
                            "type": "string",
                            "description": "메일 ID (email_list에서 확인)"
                        },
                        "filename": {
                            "type": "string",
                            "description": "파싱할 첨부파일명 (attachment_list에서 확인)"
                        },
                        "folder": {
                            "type": "string",
                            "description": "메일함 이름 (기본: INBOX)",
                            "default": "INBOX"
                        }
                    },
                    "required": ["email_id", "filename"]
                }
            ),
            Tool(
                name="attachment_download",
                description="""첨부파일 다운로드 (파싱 없이)

지정된 첨부파일을 로컬에 다운로드합니다.
파일 내용 분석 없이 단순 다운로드만 수행합니다.

다운로드 후 파일 경로가 반환되며, 
파일 내용 추출이 필요하면 attachment_parse를 사용하세요.

먼저 attachment_list로 정확한 파일명을 확인하세요.""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "email_id": {
                            "type": "string",
                            "description": "메일 ID (email_list에서 확인)"
                        },
                        "filename": {
                            "type": "string",
                            "description": "다운로드할 첨부파일명 (attachment_list에서 확인)"
                        },
                        "folder": {
                            "type": "string",
                            "description": "메일함 이름 (기본: INBOX)",
                            "default": "INBOX"
                        }
                    },
                    "required": ["email_id", "filename"]
                }
            )
        ]


async def handle_attachment_tool(name: str, arguments: dict) -> list[TextContent]:
    """첨부파일 도구 실행"""
    
    if name == "attachment_list":
        return await _attachment_list(
            email_id=arguments["email_id"],
            download=arguments.get("download", False),
            folder=arguments.get("folder", "INBOX")
        )
    
    elif name == "attachment_parse":
        return await _attachment_parse(
            email_id=arguments["email_id"],
            filename=arguments["filename"],
            folder=arguments.get("folder", "INBOX")
        )
    
    elif name == "attachment_download":
        return await _attachment_download(
            email_id=arguments["email_id"],
            filename=arguments["filename"],
            folder=arguments.get("folder", "INBOX")
        )
    
    else:
        raise ValueError(f"Unknown attachment tool: {name}")


async def _attachment_list(email_id: str, download: bool, folder: str) -> list[TextContent]:
    """메일의 첨부파일 목록 조회"""
    try:
        attachments = get_attachments(email_id, download=download, folder=folder)
        
        if not attachments:
            result_text = "📎 첨부파일이 없습니다."
            return [TextContent(type="text", text=result_text)]
        
        result_text = f"📎 첨부파일 목록 (총 {len(attachments)}개)\n\n"
        
        for i, att in enumerate(attachments, 1):
            supported = "✅" if att["is_supported"] else "❌"
            result_text += f"{i}. {att['filename']}\n"
            result_text += f"   크기: {att['size_readable']}\n"
            result_text += f"   타입: {att['content_type']}\n"
            result_text += f"   분석 가능: {supported}\n"
            
            if att.get("local_path"):
                result_text += f"   저장 위치: {att['local_path']}\n"
            
            result_text += "\n"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        error_text = f"❌ 첨부파일 목록 조회 실패: {str(e)}"
        return [TextContent(type="text", text=error_text)]


async def _attachment_parse(email_id: str, filename: str, folder: str) -> list[TextContent]:
    """첨부파일 내용 추출 (Agent 분석용)"""
    try:
        result = parse_attachment(email_id, filename, folder=folder)
        
        if result is None:
            error_text = "❌ 파일 파싱에 실패했습니다."
            return [TextContent(type="text", text=error_text)]
        
        if "error" in result:
            error_text = f"❌ {result['error']}"
            return [TextContent(type="text", text=error_text)]
        
        result_text = f"📄 첨부파일 파싱 완료: {result['filename']}\n\n"
        result_text += f"파일 타입: {result['file_type']}\n"
        
        if result.get('page_count'):
            result_text += f"페이지 수: {result['page_count']}\n"
        
        if result.get('sheet_names'):
            result_text += f"Excel 시트: {', '.join(result['sheet_names'])}\n"
        
        result_text += f"추출된 텍스트 길이: {result['text_length']:,}자\n\n"
        
        # 테이블 정보
        if result.get('tables'):
            result_text += f"📊 테이블: {len(result['tables'])}개 발견\n\n"
        
        result_text += "=" * 60 + "\n"
        result_text += "⚠️ Agent 분석 가이드:\n"
        result_text += "- 아래 텍스트에서 핵심 포인트 3-5개를 식별하세요\n"
        result_text += "- 중요한 수치, 날짜, 요청사항을 우선 추출\n"
        result_text += "=" * 60 + "\n\n"
        
        # 추출된 텍스트
        result_text += f"📄 추출된 내용:\n{'-'*60}\n"
        preview = result['extracted_text'][:3000]
        result_text += preview
        
        if result['text_length'] > 3000:
            result_text += f"\n\n... (이하 {result['text_length'] - 3000:,}자 생략)"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        error_text = f"❌ 첨부파일 파싱 실패: {str(e)}"
        return [TextContent(type="text", text=error_text)]


async def _attachment_download(email_id: str, filename: str, folder: str) -> list[TextContent]:
    """첨부파일 다운로드 (파싱 없이)"""
    try:
        result = download_attachment(email_id, filename, folder=folder)
        
        if "error" in result:
            error_text = f"❌ {result['error']}"
            return [TextContent(type="text", text=error_text)]
        
        result_text = f"📥 첨부파일 다운로드 완료\n\n"
        result_text += f"파일명: {result['filename']}\n"
        result_text += f"크기: {result['size_readable']}\n"
        result_text += f"타입: {result['content_type']}\n"
        result_text += f"저장 위치: {result['local_path']}\n"
        
        if result['is_supported']:
            result_text += f"\n✅ 이 파일은 attachment_parse로 내용 추출이 가능합니다."
        else:
            result_text += f"\n⚠️ 이 파일 형식은 내용 추출을 지원하지 않습니다."
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        error_text = f"❌ 첨부파일 다운로드 실패: {str(e)}"
        return [TextContent(type="text", text=error_text)]

