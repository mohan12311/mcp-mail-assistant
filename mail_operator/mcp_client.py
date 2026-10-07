"""
MCP 클라이언트 래퍼

Operator가 MCP 서버의 도구를 호출하기 위한 클라이언트입니다.
내부적으로 DB store를 직접 호출하여 MCP 서버 없이도 동작합니다.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from mail_operator.config import OperatorConfig

logger = logging.getLogger(__name__)


def _slack_client_msg_id(arguments: dict) -> str | None:
    key = arguments.get("idempotency_key")
    if not key:
        return None
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mcp-mail-assistant:slack:{key}"))


def _slack_post_arguments(arguments: dict, **kwargs: Any) -> dict[str, Any]:
    client_msg_id = _slack_client_msg_id(arguments)
    if client_msg_id:
        kwargs["client_msg_id"] = client_msg_id
    return kwargs


def _record_smoke_evidence(entry: dict) -> None:
    """Optionally append smoke evidence without affecting normal runtime."""
    path = os.environ.get("MAIL_AGENT_SMOKE_EVIDENCE_PATH")
    if not path:
        return
    try:
        evidence_path = Path(path)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            **entry,
        }
        with evidence_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.debug("Smoke evidence write skipped: %s", exc)


class MCPClientWrapper:
    """
    MCP 클라이언트 래퍼
    
    실제 MCP 프로토콜 대신 내부 함수를 직접 호출합니다.
    이는 Operator가 같은 프로세스/DB를 공유할 때 사용됩니다.
    
    추후 실제 MCP stdio 클라이언트로 교체 가능합니다.
    """
    
    def __init__(self, config: OperatorConfig):
        """
        초기화
        
        Args:
            config: Operator 설정
        """
        self.config = config
        self._connected = False
        
        # 도구 핸들러 매핑
        self._tool_handlers = {}
        
        logger.info("MCPClientWrapper 초기화")
    
    async def connect(self):
        """연결 (내부 핸들러 등록)"""
        if self._connected:
            return
        
        # 내부 도구 핸들러 등록
        self._register_handlers()
        self._connected = True
        logger.info("MCPClientWrapper 연결 완료")
    
    async def disconnect(self):
        """연결 해제"""
        self._connected = False
        logger.info("MCPClientWrapper 연결 해제")
    
    def _register_handlers(self):
        """도구 핸들러 등록"""
        # 이메일 DB 도구
        self._tool_handlers.update({
            "email_db_get": self._email_db_get,
            "email_db_get_body": self._email_db_get_body,
            "email_db_get_with_body": self._email_db_get_with_body,
            "email_db_get_attachments": self._email_db_get_attachments,
            "email_db_update_analysis": self._email_db_update_analysis,
            "email_db_mark_processed": self._email_db_mark_processed,
            "email_db_list_unprocessed": self._email_db_list_unprocessed,
        })
        
        # 이벤트 도구
        self._tool_handlers.update({
            "event_create": self._event_create,
            "event_get": self._event_get,
            "event_list": self._event_list,
        })
        
        # 태스크 도구
        self._tool_handlers.update({
            "task_create": self._task_create,
            "task_detail": self._task_detail,
            "task_update_status": self._task_update_status,
            "task_list": self._task_list,
        })
        
        # Slack 알림 도구
        self._tool_handlers.update({
            "slack_notify": self._slack_notify,
            "slack_notify_with_approval": self._slack_notify_with_approval,
            "slack_notify_with_send_button": self._slack_notify_with_send_button,
        })
    
    async def call_tool(self, name: str, arguments: dict) -> str:
        """
        도구 호출
        
        Args:
            name: 도구 이름
            arguments: 도구 인자
            
        Returns:
            str: 도구 결과 (텍스트)
        """
        if not self._connected:
            raise RuntimeError("MCPClient가 연결되지 않았습니다")
        
        handler = self._tool_handlers.get(name)
        if not handler:
            raise ValueError(f"알 수 없는 도구: {name}")
        
        # Slack messages, email bodies, and draft text must never be copied into
        # debug logs.  Argument names are sufficient for tool-path diagnostics.
        logger.debug("도구 호출: %s (argument_keys=%s)", name, sorted(arguments))
        
        try:
            result = await handler(arguments)
            return result
        except Exception as e:
            logger.error(f"도구 호출 실패: {name} - {e}")
            raise
    
    # ===== Email DB Tools =====
    
    async def _email_db_get(self, args: dict) -> str:
        from db import email_store
        
        email_id = args["email_id"]
        email = email_store.get_email(email_id)
        
        if not email:
            return f"⚠️ 메일을 찾을 수 없습니다: {email_id}"
        
        return f"📧 메일: {email['subject']}\n---\n[RAW_DATA]\n{json.dumps(email, ensure_ascii=False)}"
    
    async def _email_db_get_body(self, args: dict) -> str:
        from db import email_store
        
        email_id = args["email_id"]
        max_length = args.get("max_length", 5000)
        
        email = email_store.get_email(email_id)
        if not email:
            return f"⚠️ 메일을 찾을 수 없습니다: {email_id}"
        
        body = (email.get("body_text") or "")[:max_length]
        return f"📄 본문:\n{body}"
    
    async def _email_db_get_with_body(self, args: dict) -> str:
        from db import email_store, attachment_store
        
        email_id = args["email_id"]
        max_body_length = args.get("max_body_length", 5000)
        
        email = email_store.get_email(email_id)
        if not email:
            return f"⚠️ 메일을 찾을 수 없습니다: {email_id}"
        
        attachments = attachment_store.get_attachments_for_email(email_id)
        
        body = (email.get("body_text") or "")[:max_body_length]
        
        raw_data = {
            "email_id": email["id"],
            "subject": email.get("subject"),
            "sender": email.get("sender"),
            "sender_email": email.get("sender_email"),
            "received_at": email.get("received_at"),
            "has_attachments": email.get("has_attachments", False),
            "body_text": body,
            "attachments": attachments
        }
        
        result = f"📧 메일 분석 데이터\n제목: {email.get('subject')}\n발신자: {email.get('sender')}\n"
        result += f"\n---\n[RAW_DATA]\n{json.dumps(raw_data, ensure_ascii=False)}"
        
        return result
    
    async def _email_db_get_attachments(self, args: dict) -> str:
        from db import attachment_store
        
        email_id = args["email_id"]
        
        attachments = attachment_store.get_attachments_for_email(email_id)
        
        if not attachments:
            return f"📎 첨부파일 없음: {email_id}"
        
        result = f"📎 첨부파일 {len(attachments)}개\n"
        result += f"\n---\n[RAW_DATA]\n{json.dumps(attachments, ensure_ascii=False)}"
        
        return result
    
    async def _email_db_update_analysis(self, args: dict) -> str:
        from db.connection import get_connection
        
        email_id = args["email_id"]
        
        updates = []
        params = []
        
        for field in ["body_summary", "priority_score", "priority_level", "urgency", "requires_response"]:
            if field in args and args[field] is not None:
                updates.append(f"{field} = ?")
                params.append(args[field])
        
        if not updates:
            return "⚠️ 업데이트할 항목 없음"
        
        params.append(email_id)
        
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE emails SET {', '.join(updates)} WHERE id = ?", params)
            
            if cursor.rowcount > 0:
                return f"✅ 분석 결과 저장: {email_id}"
            return f"⚠️ 메일 없음: {email_id}"
    
    async def _email_db_mark_processed(self, args: dict) -> str:
        from db import email_store
        
        email_id = args["email_id"]
        success = email_store.mark_as_processed(email_id)
        
        if success:
            return f"✅ 처리 완료: {email_id}"
        return f"⚠️ 메일 없음: {email_id}"
    
    async def _email_db_list_unprocessed(self, args: dict) -> str:
        from db import email_store
        
        limit = args.get("limit", 20)
        emails = email_store.get_unprocessed_emails(limit=limit)
        
        if not emails:
            return "✅ 미처리 메일 없음"
        
        result = f"📋 미처리 메일 {len(emails)}건\n"
        for e in emails:
            result += f"- [{e['id'][:8]}] {e.get('subject', '(제목 없음)')}\n"
        
        return result
    
    # ===== Event Tools =====
    
    async def _event_create(self, args: dict) -> str:
        from db import event_store
        
        event_id = event_store.create_event(
            event_type=args["event_type"],
            payload=args["payload"],
            source_id=args.get("source_id"),
            max_attempts=args.get("max_attempts", 3)
        )
        
        return f"✅ 이벤트 생성: {event_id}"
    
    async def _event_get(self, args: dict) -> str:
        from db import event_store
        
        event_id = args["event_id"]
        event = event_store.get_event(event_id)
        
        if not event:
            return f"⚠️ 이벤트 없음: {event_id}"
        
        return f"📋 이벤트: {event['event_type']}\n---\n[RAW_DATA]\n{json.dumps(event, ensure_ascii=False)}"
    
    async def _event_list(self, args: dict) -> str:
        from db import event_store
        
        events = event_store.list_events(
            status=args.get("status"),
            event_type=args.get("event_type"),
            limit=args.get("limit", 20)
        )
        
        if not events:
            return "📭 이벤트 없음"
        
        result = f"📋 이벤트 {len(events)}건\n"
        for e in events:
            result += f"- [{e['status']}] {e['event_type']} ({e['id'][:8]})\n"
        
        return result
    
    # ===== Task Tools =====
    
    async def _task_create(self, args: dict) -> str:
        from db import task_store
        
        requires_approval = bool(args.get("requires_approval"))
        task_id = task_store.create_task(
            email_id=args["email_id"],
            task_type=args["task_type"],
            title=args["title"],
            description=args.get("description"),
            deadline=args.get("deadline"),
            priority=args.get("priority", "medium"),
            approval_status="pending" if requires_approval else "none",
        )
        
        raw_data = {
            "task_id": task_id,
            "requires_approval": requires_approval,
            "approval_status": "pending" if requires_approval else "none",
        }
        return f"✅ 태스크 생성: {task_id}\n---\n[RAW_DATA]\n{json.dumps(raw_data, ensure_ascii=False)}"
    
    async def _task_detail(self, args: dict) -> str:
        from db import task_store
        
        task_id = args["task_id"]
        task = task_store.get_task(task_id)
        
        if not task:
            return f"⚠️ 태스크 없음: {task_id}"
        
        return f"📋 태스크: {task.get('title')}\n---\n[RAW_DATA]\n{json.dumps(task, ensure_ascii=False)}"
    
    async def _task_update_status(self, args: dict) -> str:
        from db import task_store
        
        task_id = args["task_id"]
        status = args["status"]
        
        success = task_store.update_task_status(
            task_id=task_id,
            status=status,
            execution_result=args.get("execution_result")
        )
        
        if success:
            return f"✅ 태스크 상태 업데이트: {task_id} → {status}"
        return f"⚠️ 태스크 없음: {task_id}"
    
    async def _task_list(self, args: dict) -> str:
        from db import task_store
        
        tasks = task_store.list_tasks(
            status=args.get("status"),
            limit=args.get("limit", 20)
        )
        
        if not tasks:
            return "📭 태스크 없음"
        
        result = f"📋 태스크 {len(tasks)}건\n"
        for t in tasks:
            result += f"- [{t.get('status')}] {t.get('title')} (ID: {t.get('id')})\n"
        
        return result
    
    # ===== Slack Tools =====
    
    async def _slack_notify(self, args: dict) -> str:
        """Slack 알림 전송 (AsyncWebClient)"""
        message = args.get("message", "")
        channel = args.get("channel") or os.environ.get("SLACK_CHANNEL", "#mail-alerts")
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            logger.error("SLACK_BOT_TOKEN 환경변수 미설정")
            return "❌ SLACK_BOT_TOKEN 없음"
        from slack_sdk.errors import SlackApiError
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=token)
        try:
            post_args = {"channel": channel, "text": message}
            if args.get("thread_ts"):
                post_args["thread_ts"] = args["thread_ts"]
            response = await client.chat_postMessage(
                **_slack_post_arguments(args, **post_args)
            )
            logger.info(f"Slack 알림 전송 성공: ts={response['ts']}")
            _record_smoke_evidence({
                "kind": "slack_tool",
                "tool": "slack_notify",
                "channel": channel,
                "ts": response.get("ts"),
                "message_length": len(message),
            })
            return f"✅ Slack 알림 전송: ts={response['ts']}"
        except SlackApiError as e:
            logger.error(f"Slack 알림 실패: {e.response['error']}")
            return f"❌ Slack 오류: {e.response['error']}"

    async def _slack_notify_with_approval(self, args: dict) -> str:
        """Slack 승인 요청 알림 (버튼 포함)"""
        message = args.get("message", "")
        task_id = args.get("task_id")
        channel = args.get("channel") or os.environ.get("APPROVAL_CHANNEL", "#task-approvals")
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            return "❌ SLACK_BOT_TOKEN 없음"
        from slack_sdk.errors import SlackApiError
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=token)
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": message}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ 승인"},
                 "style": "primary", "value": f"approve_{task_id}", "action_id": "task_approve"},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ 거부"},
                 "style": "danger", "value": f"deny_{task_id}", "action_id": "task_deny"},
            ]}
        ]
        try:
            response = await client.chat_postMessage(**_slack_post_arguments(
                args, channel=channel, text=message, blocks=blocks
            ))
            _record_smoke_evidence({
                "kind": "slack_tool",
                "tool": "slack_notify_with_approval",
                "channel": channel,
                "ts": response.get("ts"),
                "task_id": task_id,
                "message_length": len(message),
            })
            return f"✅ Slack 승인 요청 전송: ts={response['ts']}"
        except SlackApiError as e:
            return f"❌ Slack 오류: {e.response['error']}"

    async def _slack_notify_with_send_button(self, args: dict) -> str:
        """reply_email용: DB 재검증 후 최종 확인 modal을 여는 버튼."""
        message = args.get("message", "")
        task_id = args.get("task_id")
        channel = args.get("channel") or os.environ.get("APPROVAL_CHANNEL", "#task-approvals")
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            return "❌ SLACK_BOT_TOKEN 없음"
        from slack_sdk.errors import SlackApiError
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=token)
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": message}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🔐 최종 확인 열기"},
                        "action_id": "reply_send",
                        "value": str(task_id),
                        "style": "primary",
                    }
                ],
            },
        ]
        try:
            post_args = {"channel": channel, "text": message, "blocks": blocks}
            if args.get("thread_ts"):
                post_args["thread_ts"] = args["thread_ts"]
            response = await client.chat_postMessage(
                **_slack_post_arguments(args, **post_args)
            )
            _record_smoke_evidence({
                "kind": "slack_tool",
                "tool": "slack_notify_with_send_button",
                "channel": channel,
                "ts": response.get("ts"),
                "task_id": task_id,
                "message_length": len(message),
            })
            return f"✅ 답장 보내기 버튼 알림 전송: ts={response['ts']}"
        except SlackApiError as e:
            return f"❌ Slack 오류: {e.response['error']}"
