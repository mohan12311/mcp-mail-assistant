"""
Notification Tools - MCP Tools for Slack notifications
skills/notification_skills.py의 함수들을 MCP Tool로 래핑
"""
import json
from mcp.types import Tool, TextContent

from skills.notification_skills import send_inbox_summary_to_slack, send_slack_notification


def get_notification_tools() -> list[Tool]:
    """알림 도구 목록 반환"""
    return [
            Tool(
                name="slack_send_summary",
                description="""받은편지함 요약을 Slack으로 전송

email_inbox_summary 도구로 생성된 요약을 Slack 채널에 전송합니다.
Block Kit 형식의 보기 좋은 메시지로 변환하여 전송합니다.

사용 순서:
1. email_inbox_summary 실행
2. 결과의 [RAW_DATA] 섹션에서 JSON 추출
3. slack_send_summary에 전달

참고: SLACK_BOT_TOKEN 환경변수가 필요합니다.""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "object",
                            "description": "email_inbox_summary의 결과 JSON 객체"
                        },
                        "channel": {
                            "type": "string",
                            "description": "Slack 채널명 (선택, 기본: 환경변수 SLACK_CHANNEL)"
                        }
                    },
                    "required": ["summary"]
                }
            ),
            Tool(
                name="slack_send_message",
                description="""Slack으로 커스텀 메시지 전송

지정된 채널에 텍스트 메시지를 전송합니다.
간단한 알림이나 메모를 전송할 때 사용합니다.

참고: SLACK_BOT_TOKEN 환경변수가 필요합니다.""",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "전송할 메시지 텍스트"
                        },
                        "channel": {
                            "type": "string",
                            "description": "Slack 채널명 (선택, 기본: 환경변수 SLACK_CHANNEL)"
                        }
                    },
                    "required": ["message"]
                }
            )
        ]


async def handle_notification_tool(name: str, arguments: dict) -> list[TextContent]:
    """알림 도구 실행"""
    
    if name == "slack_send_summary":
        return await _slack_send_summary(
            summary=arguments["summary"],
            channel=arguments.get("channel")
        )
    
    elif name == "slack_send_message":
        return await _slack_send_message(
            message=arguments["message"],
            channel=arguments.get("channel")
        )
    
    else:
        raise ValueError(f"Unknown notification tool: {name}")


async def _slack_send_summary(summary: dict, channel: str = None) -> list[TextContent]:
    """받은편지함 요약을 Slack으로 전송"""
    try:
        # summary가 문자열로 전달된 경우 파싱
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except json.JSONDecodeError:
                error_text = "❌ summary는 유효한 JSON 객체여야 합니다."
                return [TextContent(type="text", text=error_text)]
        
        result = send_inbox_summary_to_slack(summary, channel=channel)
        
        if result['success']:
            result_text = f"✅ Slack 알림 전송 성공!\n\n"
            result_text += f"채널: {result.get('channel', 'N/A')}\n"
            result_text += f"타임스탬프: {result.get('timestamp', 'N/A')}\n"
        else:
            result_text = f"❌ Slack 알림 전송 실패\n\n"
            result_text += f"오류: {result.get('error', 'Unknown error')}\n"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        error_text = f"❌ Slack 알림 전송 중 오류: {str(e)}"
        return [TextContent(type="text", text=error_text)]


async def _slack_send_message(message: str, channel: str = None) -> list[TextContent]:
    """Slack으로 커스텀 메시지 전송"""
    try:
        result = send_slack_notification(message=message, channel=channel)
        
        if result['success']:
            result_text = f"✅ Slack 메시지 전송 성공!\n\n"
            result_text += f"채널: {result.get('channel', 'N/A')}\n"
            result_text += f"타임스탬프: {result.get('timestamp', 'N/A')}\n"
        else:
            result_text = f"❌ Slack 메시지 전송 실패\n\n"
            result_text += f"오류: {result.get('error', 'Unknown error')}\n"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        error_text = f"❌ Slack 메시지 전송 중 오류: {str(e)}"
        return [TextContent(type="text", text=error_text)]

