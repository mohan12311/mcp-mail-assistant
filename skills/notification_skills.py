"""
MCP Mail Assistant - 알림 관련 Skills
send_slack_notification
"""
from typing import Optional
from datetime import datetime


# 글로벌 설정 참조
_config = None


def init_config(config) -> None:
    """설정 초기화"""
    global _config
    _config = config


def get_config():
    """현재 설정 반환"""
    if _config is None:
        from skills.mail_skills import get_config as mail_get_config
        return mail_get_config()
    return _config


def send_slack_notification(
    message: str,
    channel: Optional[str] = None,
    blocks: Optional[list[dict]] = None,
) -> dict:
    """
    Slack으로 알림 전송
    
    Args:
        message: 메시지 텍스트 (blocks가 없을 때 fallback으로 사용)
        channel: 채널명 (기본: 설정의 SLACK_CHANNEL)
        blocks: Slack Block Kit 형식의 메시지 블록 (선택)
    
    Returns:
        {success: bool, timestamp?: str, error?: str}
    """
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        return {
            "success": False,
            "error": "slack-sdk 패키지가 필요합니다: pip install slack-sdk"
        }
    
    config = get_config()
    
    if not config.slack_bot_token:
        return {
            "success": False,
            "error": "SLACK_BOT_TOKEN이 설정되지 않았습니다."
        }
    
    client = WebClient(token=config.slack_bot_token)
    target_channel = channel or config.slack_channel
    
    try:
        if blocks:
            response = client.chat_postMessage(
                channel=target_channel,
                text=message,  # fallback text
                blocks=blocks
            )
        else:
            response = client.chat_postMessage(
                channel=target_channel,
                text=message
            )
        
        return {
            "success": True,
            "timestamp": response["ts"],
            "channel": response["channel"],
        }
    
    except SlackApiError as e:
        return {
            "success": False,
            "error": str(e.response["error"])
        }


def format_inbox_summary_for_slack(summary: dict) -> tuple[str, list[dict]]:
    """
    받은편지함 요약을 Slack 메시지 형식으로 변환
    
    Args:
        summary: summarize_inbox의 반환값
    
    Returns:
        (text, blocks) 튜플
    """
    today = datetime.now().strftime("%m월 %d일")
    
    # 통계
    total = summary.get("total_count", 0)
    analyzed = summary.get("analyzed_count", 0)
    urgent_count = len(summary.get("urgent_items", []))
    attachment_count = len(summary.get("attachments_to_review", []))
    
    # 기본 텍스트 (fallback)
    text = f"📬 메일 요약 리포트 ({today}) - {analyzed}통 분석 완료"
    
    # Blocks 구성
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🔔 메일 요약 리포트 ({today})",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📬 *새 메일 {analyzed}통* | ⚠️ 긴급 {urgent_count}건 | 📎 첨부파일 {attachment_count}건"
            }
        },
        {"type": "divider"}
    ]
    
    # 메일 요약 추가
    for i, email_summary in enumerate(summary.get("summaries", [])[:5], 1):
        urgency_emoji = "🔴" if email_summary.get("urgency") == "urgent" else "🟢"
        response_emoji = "📩" if email_summary.get("requires_response") else ""
        
        attachments = email_summary.get("attachments", [])
        attachment_text = f"\n📎 {', '.join(attachments)}" if attachments else ""
        
        email_block = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{i}️⃣ {urgency_emoji} *{email_summary.get('sender', '')}*\n"
                    f"_{email_summary.get('subject', '')}_\n"
                    f"{email_summary.get('summary', '')}"
                    f"{attachment_text}"
                    f"{' ' + response_emoji + ' 답장 필요' if response_emoji else ''}"
                )
            }
        }
        blocks.append(email_block)
    
    # Todo 섹션
    todos = summary.get("all_todos", [])
    if todos:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*✅ 오늘의 Todo*"
            }
        })
        
        todo_texts = []
        for todo in todos[:10]:  # 최대 10개
            priority_emoji = "🔴" if todo.get("priority") == "high" else "⚪"
            deadline = f" (기한: {todo.get('deadline')})" if todo.get("deadline") else ""
            todo_texts.append(f"{priority_emoji} {todo.get('description', '')}{deadline}")
        
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join(todo_texts)
            }
        })
    
    return text, blocks


def send_inbox_summary_to_slack(
    summary: dict,
    channel: Optional[str] = None
) -> dict:
    """
    받은편지함 요약을 Slack으로 전송
    
    Args:
        summary: summarize_inbox의 반환값
        channel: 채널명 (선택)
    
    Returns:
        send_slack_notification의 반환값
    """
    text, blocks = format_inbox_summary_for_slack(summary)
    return send_slack_notification(message=text, channel=channel, blocks=blocks)
