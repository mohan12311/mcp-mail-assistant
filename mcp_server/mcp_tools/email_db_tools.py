"""
Email DB Tools - MCP Tools for DB-based email operations
Operator가 IMAP이 아닌 DB(raw store)에서 메일/본문/첨부 메타를 가져오도록 합니다.

KIRA식 분리: Daemon이 쌓은 raw data를 Operator가 MCP 도구로 읽습니다.
"""
import json
from mcp.types import Tool, TextContent

from db import email_store, attachment_store


def get_email_db_tools() -> list[Tool]:
    """이메일 DB 도구 목록 반환"""
    return [
        Tool(
            name="email_db_get",
            description="""DB에서 메일 상세 정보를 조회합니다.

Daemon이 저장한 메일 데이터를 가져옵니다.
이벤트 처리 시 email_id로 메일 정보를 확인할 때 사용합니다.

반환 정보: 제목, 발신자, 수신일시, 본문, 첨부파일 여부 등""",
            inputSchema={
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "메일 ID"
                    }
                },
                "required": ["email_id"]
            }
        ),
        Tool(
            name="email_db_get_body",
            description="""DB에서 메일 본문만 조회합니다.

메일 분석 시 본문 내용만 필요할 때 사용합니다.
max_length로 본문 길이를 제한할 수 있습니다.

반환 정보: 본문 텍스트 (길이 제한 가능)""",
            inputSchema={
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "메일 ID"
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "본문 최대 길이 (기본: 5000)",
                        "default": 5000,
                        "minimum": 500,
                        "maximum": 20000
                    }
                },
                "required": ["email_id"]
            }
        ),
        Tool(
            name="email_db_list_unprocessed",
            description="""DB에서 미처리 메일 목록을 조회합니다.

아직 분석/처리되지 않은 메일들을 확인할 때 사용합니다.
Operator가 직접 폴링하는 대신 이벤트 기반으로 처리하는 것이 권장됩니다.

반환 정보: 미처리 메일 목록 (ID, 제목, 발신자, 수신일시)""",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "최대 조회 개수 (기본: 20)",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 100
                    }
                }
            }
        ),
        Tool(
            name="email_db_mark_processed",
            description="""메일을 처리 완료로 표시합니다.

분석 및 태스크 생성이 완료된 메일을 표시할 때 사용합니다.
이후 email_db_list_unprocessed에서 제외됩니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "메일 ID"
                    }
                },
                "required": ["email_id"]
            }
        ),
        Tool(
            name="email_db_get_attachments",
            description="""DB에서 메일의 첨부파일 메타데이터를 조회합니다.

첨부파일 분석 전에 어떤 파일들이 있는지 확인할 때 사용합니다.

반환 정보: 첨부파일 목록 (파일명, 타입, 크기, 처리 여부)""",
            inputSchema={
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "메일 ID"
                    }
                },
                "required": ["email_id"]
            }
        ),
        Tool(
            name="email_db_get_with_body",
            description="""DB에서 메일 정보와 본문을 함께 조회합니다.

메일 분석 시 메타데이터와 본문을 한 번에 가져올 때 사용합니다.
Agent 분석에 최적화된 형태로 데이터를 반환합니다.

반환 정보: 
- 메일 메타: 제목, 발신자, 수신일시, 첨부파일 여부
- 본문 텍스트
- 첨부파일 목록 (있는 경우)

Agent는 이 데이터를 분석하여:
- 요약 생성 (2-3문장)
- 긴급도 판단: urgent / normal / low
- Action Items 추출: ToDo, 리마인더, 답장 필요 등
- 일본어 간접 표현 주의 (「ご検討いただければ」= 의뢰)""",
            inputSchema={
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "메일 ID"
                    },
                    "max_body_length": {
                        "type": "integer",
                        "description": "본문 최대 길이 (기본: 5000)",
                        "default": 5000,
                        "minimum": 500,
                        "maximum": 20000
                    }
                },
                "required": ["email_id"]
            }
        ),
        Tool(
            name="email_db_update_analysis",
            description="""메일의 분석 결과를 저장합니다.

Agent가 생성한 요약, 우선순위, 긴급도 등을 DB에 저장합니다.
분석 완료 후 호출하세요.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "메일 ID"
                    },
                    "body_summary": {
                        "type": "string",
                        "description": "본문 요약 (2-3문장)"
                    },
                    "priority_score": {
                        "type": "integer",
                        "description": "우선순위 점수 (0-100)",
                        "minimum": 0,
                        "maximum": 100
                    },
                    "priority_level": {
                        "type": "string",
                        "description": "우선순위 레벨",
                        "enum": ["low", "medium", "high", "urgent"]
                    },
                    "urgency": {
                        "type": "string",
                        "description": "긴급도",
                        "enum": ["low", "normal", "urgent"]
                    },
                    "requires_response": {
                        "type": "boolean",
                        "description": "답장 필요 여부"
                    }
                },
                "required": ["email_id"]
            }
        )
    ]


async def handle_email_db_tool(name: str, arguments: dict) -> list[TextContent]:
    """이메일 DB 도구 실행"""
    
    if name == "email_db_get":
        return await _email_db_get(email_id=arguments["email_id"])
    
    elif name == "email_db_get_body":
        return await _email_db_get_body(
            email_id=arguments["email_id"],
            max_length=arguments.get("max_length", 5000)
        )
    
    elif name == "email_db_list_unprocessed":
        return await _email_db_list_unprocessed(
            limit=arguments.get("limit", 20)
        )
    
    elif name == "email_db_mark_processed":
        return await _email_db_mark_processed(email_id=arguments["email_id"])
    
    elif name == "email_db_get_attachments":
        return await _email_db_get_attachments(email_id=arguments["email_id"])
    
    elif name == "email_db_get_with_body":
        return await _email_db_get_with_body(
            email_id=arguments["email_id"],
            max_body_length=arguments.get("max_body_length", 5000)
        )
    
    elif name == "email_db_update_analysis":
        return await _email_db_update_analysis(
            email_id=arguments["email_id"],
            body_summary=arguments.get("body_summary"),
            priority_score=arguments.get("priority_score"),
            priority_level=arguments.get("priority_level"),
            urgency=arguments.get("urgency"),
            requires_response=arguments.get("requires_response")
        )
    
    else:
        raise ValueError(f"Unknown email_db tool: {name}")


async def _email_db_get(email_id: str) -> list[TextContent]:
    """DB에서 메일 조회"""
    try:
        email = email_store.get_email(email_id)
        
        if not email:
            return [TextContent(type="text", text=f"⚠️ 메일을 찾을 수 없습니다: {email_id}")]
        
        result_text = f"📧 메일 상세\n\n"
        result_text += f"ID: {email['id']}\n"
        result_text += f"제목: {email['subject']}\n"
        result_text += f"발신자: {email['sender']} <{email['sender_email']}>\n"
        result_text += f"수신일시: {email['received_at']}\n"
        result_text += f"폴더: {email.get('folder', 'INBOX')}\n"
        result_text += f"첨부파일: {'있음' if email.get('has_attachments') else '없음'}\n"
        result_text += f"처리 상태: {'완료' if email.get('is_processed') else '미처리'}\n"
        result_text += f"Slack 알림: {'완료' if email.get('slack_notified') else '미완료'}\n"
        
        if email.get('body_summary'):
            result_text += f"\n요약: {email['body_summary']}\n"
        
        if email.get('priority_level') and email.get('priority_level') != 'low':
            result_text += f"우선순위: {email['priority_level']} (점수: {email.get('priority_score', 0)})\n"
        
        # JSON 데이터 포함
        result_text += f"\n---\n[RAW_DATA]\n{json.dumps(email, ensure_ascii=False, indent=2)}"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 메일 조회 실패: {str(e)}")]


async def _email_db_get_body(email_id: str, max_length: int) -> list[TextContent]:
    """DB에서 메일 본문 조회"""
    try:
        email = email_store.get_email(email_id)
        
        if not email:
            return [TextContent(type="text", text=f"⚠️ 메일을 찾을 수 없습니다: {email_id}")]
        
        body = email.get('body_text') or ""
        original_length = len(body)
        
        if len(body) > max_length:
            body = body[:max_length] + f"\n\n... (생략됨: 전체 {original_length}자 중 {max_length}자 표시)"
        
        result_text = f"📄 메일 본문 ({email_id})\n"
        result_text += f"제목: {email['subject']}\n"
        result_text += f"길이: {original_length}자\n"
        result_text += "-" * 40 + "\n"
        result_text += body
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 본문 조회 실패: {str(e)}")]


async def _email_db_list_unprocessed(limit: int) -> list[TextContent]:
    """DB에서 미처리 메일 목록 조회"""
    try:
        emails = email_store.get_unprocessed_emails(limit=limit)
        
        if not emails:
            return [TextContent(type="text", text="✅ 미처리 메일이 없습니다.")]
        
        result_text = f"📋 미처리 메일 목록 (총 {len(emails)}건)\n\n"
        
        for i, email in enumerate(emails, 1):
            attachment = " 📎" if email.get('has_attachments') else ""
            result_text += f"{i}. [{email['id'][:8]}...] {email['subject']}{attachment}\n"
            result_text += f"   발신자: {email['sender']} <{email['sender_email']}>\n"
            result_text += f"   수신: {email['received_at']}\n\n"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 미처리 메일 조회 실패: {str(e)}")]


async def _email_db_mark_processed(email_id: str) -> list[TextContent]:
    """메일을 처리 완료로 표시"""
    try:
        success = email_store.mark_as_processed(email_id)
        
        if success:
            return [TextContent(type="text", text=f"✅ 메일 처리 완료 표시: {email_id}")]
        else:
            return [TextContent(type="text", text=f"⚠️ 메일을 찾을 수 없습니다: {email_id}")]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 처리 완료 표시 실패: {str(e)}")]


async def _email_db_get_attachments(email_id: str) -> list[TextContent]:
    """DB에서 첨부파일 메타데이터 조회"""
    try:
        attachments = attachment_store.get_attachments_for_email(email_id)
        
        if not attachments:
            return [TextContent(type="text", text=f"📎 첨부파일이 없습니다: {email_id}")]
        
        result_text = f"📎 첨부파일 목록 ({email_id})\n\n"
        
        for i, att in enumerate(attachments, 1):
            size_str = _format_size(att.get('file_size', 0))
            processed = "✅" if att.get('is_processed') else "⏳"
            
            result_text += f"{i}. {processed} {att['filename']}\n"
            result_text += f"   타입: {att.get('file_type', '알 수 없음')}\n"
            result_text += f"   크기: {size_str}\n"
            
            if att.get('local_path'):
                result_text += f"   경로: {att['local_path']}\n"
            
            result_text += "\n"
        
        # JSON 데이터 포함
        result_text += f"\n---\n[RAW_DATA]\n{json.dumps(attachments, ensure_ascii=False, indent=2)}"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 첨부파일 조회 실패: {str(e)}")]


async def _email_db_get_with_body(email_id: str, max_body_length: int) -> list[TextContent]:
    """DB에서 메일 정보와 본문 함께 조회 (Agent 분석용)"""
    try:
        email = email_store.get_email(email_id)
        
        if not email:
            return [TextContent(type="text", text=f"⚠️ 메일을 찾을 수 없습니다: {email_id}")]
        
        # 첨부파일 정보
        attachments = attachment_store.get_attachments_for_email(email_id)
        
        # 본문 길이 제한
        body = email.get('body_text') or ""
        original_length = len(body)
        truncated = False
        
        if len(body) > max_body_length:
            body = body[:max_body_length]
            truncated = True
        
        result_text = "📧 메일 분석 데이터\n"
        result_text += "=" * 50 + "\n\n"
        
        result_text += "⚠️ Agent 분석 가이드:\n"
        result_text += "- 요약을 생성하세요 (2-3문장)\n"
        result_text += "- 긴급도를 판단하세요: urgent / normal / low\n"
        result_text += "- Action Items를 추출하세요\n"
        result_text += "- 일본어 간접 표현에 주의하세요\n"
        result_text += "=" * 50 + "\n\n"
        
        # 메타데이터
        result_text += f"[메타데이터]\n"
        result_text += f"ID: {email['id']}\n"
        result_text += f"제목: {email['subject']}\n"
        result_text += f"발신자: {email['sender']} <{email['sender_email']}>\n"
        result_text += f"수신일시: {email['received_at']}\n"
        result_text += f"첨부파일: {'있음' if email.get('has_attachments') else '없음'}\n\n"
        
        # 첨부파일 정보
        if attachments:
            result_text += f"[첨부파일 - {len(attachments)}개]\n"
            for att in attachments:
                size_str = _format_size(att.get('file_size', 0))
                result_text += f"- {att['filename']} ({size_str})\n"
            result_text += "\n"
        
        # 본문
        result_text += f"[본문 - {original_length}자"
        if truncated:
            result_text += f", {max_body_length}자까지 표시"
        result_text += "]\n"
        result_text += "-" * 40 + "\n"
        result_text += body
        
        if truncated:
            result_text += f"\n\n... (생략됨)"
        
        # JSON 데이터 포함
        raw_data = {
            "email_id": email['id'],
            "subject": email['subject'],
            "sender": email['sender'],
            "sender_email": email['sender_email'],
            "received_at": email['received_at'],
            "has_attachments": email.get('has_attachments', False),
            "body_length": original_length,
            "attachments": [
                {"filename": a['filename'], "file_type": a.get('file_type'), "file_size": a.get('file_size')}
                for a in attachments
            ] if attachments else []
        }
        result_text += f"\n\n---\n[RAW_DATA]\n{json.dumps(raw_data, ensure_ascii=False, indent=2)}"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 메일 조회 실패: {str(e)}")]


async def _email_db_update_analysis(
    email_id: str,
    body_summary: str | None,
    priority_score: int | None,
    priority_level: str | None,
    urgency: str | None,
    requires_response: bool | None
) -> list[TextContent]:
    """메일 분석 결과 저장"""
    try:
        from db.connection import get_connection
        
        updates = []
        params = []
        
        if body_summary is not None:
            updates.append("body_summary = ?")
            params.append(body_summary)
        
        if priority_score is not None:
            updates.append("priority_score = ?")
            params.append(priority_score)
        
        if priority_level is not None:
            updates.append("priority_level = ?")
            params.append(priority_level)
        
        if urgency is not None:
            updates.append("urgency = ?")
            params.append(urgency)
        
        if requires_response is not None:
            updates.append("requires_response = ?")
            params.append(requires_response)
        
        if not updates:
            return [TextContent(type="text", text="⚠️ 업데이트할 항목이 없습니다.")]
        
        params.append(email_id)
        
        with get_connection() as conn:
            cursor = conn.cursor()
            query = f"UPDATE emails SET {', '.join(updates)} WHERE id = ?"
            cursor.execute(query, params)
            
            if cursor.rowcount > 0:
                return [TextContent(type="text", text=f"✅ 메일 분석 결과 저장 완료: {email_id}")]
            else:
                return [TextContent(type="text", text=f"⚠️ 메일을 찾을 수 없습니다: {email_id}")]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 분석 결과 저장 실패: {str(e)}")]


def _format_size(size_bytes: int) -> str:
    """파일 크기를 사람이 읽기 쉬운 형식으로 변환"""
    if size_bytes <= 0:
        return "0B"
    
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    
    return f"{size_bytes:.1f}TB"
