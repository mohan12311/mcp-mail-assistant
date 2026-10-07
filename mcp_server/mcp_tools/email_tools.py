"""
Email Tools - MCP Tools for email operations
skills/mail_skills.py의 함수들을 MCP Tool로 래핑
"""
import json
from mcp.types import Tool, TextContent

from skills.mail_skills import list_emails, read_email, list_emails_with_body


def get_email_tools() -> list[Tool]:
    """이메일 도구 목록 반환"""
    return [
            Tool(
                name="email_list",
                description="""메일 목록 조회

받은편지함 또는 지정된 폴더의 메일 목록을 조회합니다.
기본적으로 최신 10개의 읽지 않은 메일을 반환합니다.

반환 정보: 메일 ID, 제목, 발신자, 날짜, 첨부파일 유무, 읽음 여부""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "folder": {
                            "type": "string",
                            "description": "메일함 이름 (기본: INBOX)",
                            "default": "INBOX"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "조회할 메일 수 (1-50, 기본: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50
                        },
                        "only_unseen": {
                            "type": "boolean",
                            "description": "읽지 않은 메일만 조회 (기본: true)",
                            "default": True
                        }
                    }
                }
            ),
            Tool(
                name="email_read",
                description="""특정 메일 상세 읽기

메일 ID를 사용하여 메일의 상세 내용을 조회합니다.
본문, 발신자 정보, 첨부파일 목록 등을 반환합니다.

email_list로 먼저 메일 ID를 확인한 후 사용하세요.""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "email_id": {
                            "type": "string",
                            "description": "메일 ID (email_list에서 확인)"
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
                name="email_list_with_body",
                description="""메일 목록과 본문 일괄 조회 (Agent 분석용)

未読メールの一覧と本文を一括で取得します。
Agentはこのデータを直接分析して以下を判断してください:

- 各メールの要約（2-3文）
- 緊急度: urgent（至急対応）/ normal / low
- ActionItems: ToDo、リマインダー、返信必要など
- 日本語の間接表現に注意（「ご検討いただければ」= 依頼）

返信が必要なメールは明示的にマークしてください。

반환 정보: 메일 ID, 제목, 발신자, 날짜, 본문, 첨부파일 목록, 뉴스레터 여부""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "folder": {
                            "type": "string",
                            "description": "메일함 이름 (기본: INBOX)",
                            "default": "INBOX"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "조회할 메일 수 (1-20, 기본: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 20
                        },
                        "only_unseen": {
                            "type": "boolean",
                            "description": "읽지 않은 메일만 조회 (기본: true)",
                            "default": True
                        },
                        "max_body_length": {
                            "type": "integer",
                            "description": "본문 최대 길이 (기본: 3000)",
                            "default": 3000,
                            "minimum": 500,
                            "maximum": 10000
                        }
                    }
                }
            )
        ]


async def handle_email_tool(name: str, arguments: dict) -> list[TextContent]:
    """이메일 도구 실행"""
    
    if name == "email_list":
        return await _email_list(
            folder=arguments.get("folder", "INBOX"),
            limit=arguments.get("limit", 10),
            only_unseen=arguments.get("only_unseen", True)
        )
    
    elif name == "email_read":
        return await _email_read(
            email_id=arguments["email_id"],
            folder=arguments.get("folder", "INBOX")
        )
    
    elif name == "email_list_with_body":
        return await _email_list_with_body(
            folder=arguments.get("folder", "INBOX"),
            limit=arguments.get("limit", 10),
            only_unseen=arguments.get("only_unseen", True),
            max_body_length=arguments.get("max_body_length", 3000)
        )
    
    else:
        raise ValueError(f"Unknown email tool: {name}")


async def _email_list(folder: str, limit: int, only_unseen: bool) -> list[TextContent]:
    """메일 목록 조회"""
    try:
        emails = list_emails(folder=folder, limit=limit, only_unseen=only_unseen)
        
        if not emails:
            result_text = "📭 조회 조건에 맞는 메일이 없습니다."
            return [TextContent(type="text", text=result_text)]
        
        # 결과 포맷팅
        result_text = f"📬 메일 목록 (총 {len(emails)}통)\n\n"
        
        for i, email in enumerate(emails, 1):
            status = "📩" if not email["is_read"] else "📭"
            attachment = " 📎" if email["has_attachment"] else ""
            
            result_text += f"{i}. {status} {email['sender']}\n"
            result_text += f"   제목: {email['subject']}{attachment}\n"
            result_text += f"   날짜: {email['date']}\n"
            result_text += f"   ID: {email['id']}\n\n"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        error_text = f"❌ 메일 목록 조회 실패: {str(e)}"
        return [TextContent(type="text", text=error_text)]


async def _email_read(email_id: str, folder: str) -> list[TextContent]:
    """특정 메일 상세 읽기"""
    try:
        email = read_email(email_id, folder=folder)
        
        result_text = f"📖 메일 상세\n\n"
        result_text += f"발신자: {email['sender']} <{email['sender_email']}>\n"
        result_text += f"제목: {email['subject']}\n"
        result_text += f"날짜: {email['date']}\n\n"
        
        if email['attachments']:
            result_text += f"📎 첨부파일:\n"
            for att in email['attachments']:
                size_kb = att['size'] / 1024
                if size_kb > 1024:
                    size_str = f"{size_kb/1024:.2f}MB"
                else:
                    size_str = f"{size_kb:.1f}KB"
                result_text += f"   - {att['filename']} ({size_str})\n"
            result_text += "\n"
        
        result_text += f"본문:\n{'-'*60}\n"
        result_text += email['body']
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        error_text = f"❌ 메일 읽기 실패: {str(e)}"
        return [TextContent(type="text", text=error_text)]


async def _email_list_with_body(folder: str, limit: int, only_unseen: bool, max_body_length: int) -> list[TextContent]:
    """메일 목록과 본문 일괄 조회 (Agent 분석용)"""
    try:
        emails = list_emails_with_body(
            folder=folder,
            limit=limit,
            only_unseen=only_unseen,
            max_body_length=max_body_length
        )
        
        if not emails:
            result_text = "📭 조회 조건에 맞는 메일이 없습니다."
            return [TextContent(type="text", text=result_text)]
        
        # 결과 포맷팅
        result_text = f"📬 메일 목록 + 본문 (총 {len(emails)}통)\n"
        result_text += "=" * 60 + "\n\n"
        result_text += "⚠️ Agent 분석 가이드:\n"
        result_text += "- 각 메일의 요약을 생성하세요 (2-3문장)\n"
        result_text += "- 긴급도를 판단하세요: urgent / normal / low\n"
        result_text += "- Action Items를 추출하세요\n"
        result_text += "- 일본어 간접 표현에 주의하세요 (「ご検討いただければ」= 의뢰)\n"
        result_text += "=" * 60 + "\n\n"
        
        for i, email in enumerate(emails, 1):
            status = "📩" if not email["is_read"] else "📭"
            attachment = " 📎" if email["has_attachment"] else ""
            newsletter = " [뉴스레터]" if email.get("is_newsletter") else ""
            
            result_text += f"━━━━━ [{i}/{len(emails)}] ━━━━━\n"
            result_text += f"{status} 발신자: {email['sender']} <{email['sender_email']}>\n"
            result_text += f"제목: {email['subject']}{attachment}{newsletter}\n"
            result_text += f"날짜: {email['date']}\n"
            result_text += f"ID: {email['id']}\n"
            
            if email['attachments']:
                result_text += f"첨부파일:\n"
                for att in email['attachments']:
                    size_kb = att['size'] / 1024
                    size_str = f"{size_kb/1024:.2f}MB" if size_kb > 1024 else f"{size_kb:.1f}KB"
                    result_text += f"  - {att['filename']} ({size_str})\n"
            
            result_text += f"\n본문 ({email['body_length']}자):\n"
            result_text += "-" * 40 + "\n"
            result_text += email['body']
            result_text += "\n\n"
        
        # JSON 데이터도 포함 (후속 도구 사용을 위해)
        result_text += f"\n---\n[RAW_DATA]\n{json.dumps(emails, ensure_ascii=False, indent=2)}"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        error_text = f"❌ 메일 목록 조회 실패: {str(e)}"
        return [TextContent(type="text", text=error_text)]

