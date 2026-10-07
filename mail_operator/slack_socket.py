"""
Slack Socket Mode handler for approval buttons.

This module receives Block Kit button clicks and writes approval events into the
SQLite event queue. It intentionally does not execute tasks directly; the
Operator remains the single place that performs LLM-driven decisions/execution.
"""
import argparse
import asyncio
import html
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv

from db import initialize as db_initialize, task_store
from db.approval_store import (
    ApprovalAuthorityError,
    SLACK,
    assert_active_request,
    create_approval_decision,
    create_send_confirmation,
    payload_sha256,
)
from mail_operator.slack_authorization import (
    configured_approvers,
    require_action,
    require_authorized_user,
    require_view,
)

logger = logging.getLogger(__name__)


def _escape_slack(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def _reply_confirmation_blocks(
    send_payload: dict[str, Any],
    email_data: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a confirmation view without raw email or attachment content."""
    email_data = email_data or {}
    original_summary = (email_data.get("body_summary") or "요약 없음")[:1000]
    original_subject = email_data.get("subject") or ""
    original_sender = email_data.get("sender") or ""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*수신자:* {_escape_slack(send_payload.get('recipient'))}\n"
                    f"*제목:* {_escape_slack(send_payload.get('subject'))}"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*답장 내용:*\n```"
                    + _escape_slack(send_payload.get("body", "")[:2000]).replace(
                        "```", "``\u200b`"
                    )
                    + "```"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*원문 메타데이터:* {_escape_slack(original_sender)} — "
                    f"{_escape_slack(original_subject)}\n"
                    f"*저장된 요약:* {_escape_slack(original_summary)}\n"
                    "_원문 본문과 첨부 추출문은 Slack에 표시하지 않습니다._"
                ),
            },
        },
    ]


def _create_conversation_event(
    *,
    slack_event_id: object,
    user_id: object,
    channel_id: object,
    thread_ts: object,
    message_ts: object,
    text: object,
) -> Optional[str]:
    """Authorize ingress, then atomically persist a DB-owned Operator event."""
    from db.slack_conversation_store import create_interaction_event

    require_authorized_user(user_id)
    event_id, _interaction_id, created = create_interaction_event(
        slack_event_id=slack_event_id,
        actor_id=user_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        message_ts=message_ts,
        request_text=text,
    )
    return event_id if created else None


def _extract_task_id(action: dict[str, Any]) -> int:
    value = str(action.get("value") or "")
    if "_" in value:
        value = value.rsplit("_", 1)[-1]
    return int(value)


def _create_approval_event(
    task_id: int,
    approved: bool,
    user_id: Optional[str],
    action_ts: Optional[str],
    action_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[str]:
    expected_action = "task_approve" if approved else "task_deny"
    require_authorized_user(user_id)
    require_action({"action_id": action_id}, expected_action)
    if not isinstance(action_ts, str) or not action_ts:
        raise ApprovalAuthorityError("Slack approval source timestamp is missing")
    event_id, created = create_approval_decision(
        task_id=task_id,
        approved=approved,
        source=SLACK,
        action=expected_action,
        source_ref=action_ts,
        reason=reason,
    )
    return event_id if created else None


async def _update_message(client: Any, body: dict[str, Any], text: str) -> None:
    channel = body.get("channel", {}).get("id")
    message_ts = body.get("message", {}).get("ts")
    if not channel or not message_ts:
        return
    await client.chat_update(
        channel=channel,
        ts=message_ts,
        text=text,
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }
        ],
    )


def create_app():
    from slack_bolt.async_app import AsyncApp

    # Startup itself is fail-closed.  The returned identifiers are deliberately
    # ignored so they can never be included in logs.
    configured_approvers()
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN 환경변수가 필요합니다.")

    app = AsyncApp(token=token)

    async def persist_conversation(body: dict[str, Any], event: dict[str, Any]) -> None:
        if event.get("subtype") or event.get("bot_id"):
            return
        try:
            message_ts = event.get("ts")
            event_id = _create_conversation_event(
                slack_event_id=body.get("event_id"),
                user_id=event.get("user"),
                channel_id=event.get("channel"),
                thread_ts=event.get("thread_ts") or message_ts,
                message_ts=message_ts,
                text=event.get("text"),
            )
            logger.info(
                "Slack conversation request persisted (created=%s)",
                bool(event_id),
            )
        except Exception:
            # Actor identifiers and message text are intentionally excluded.
            logger.error("Slack conversation request rejected", exc_info=True)

    @app.event("app_mention")
    async def handle_app_mention(body, event):
        await persist_conversation(body, event)

    @app.event("message")
    async def handle_direct_message(body, event):
        if event.get("channel_type") != "im":
            return
        await persist_conversation(body, event)

    @app.action("task_approve")
    async def handle_task_approve(ack, body, action, client):
        await ack()
        try:
            task_id = _extract_task_id(action)
            user_id = body.get("user", {}).get("id")
            action_ts = body.get("actions", [{}])[0].get("action_ts")

            event_id = _create_approval_event(
                task_id=task_id,
                approved=True,
                user_id=user_id,
                action_ts=action_ts,
                action_id=action.get("action_id"),
            )
            suffix = f"\n이벤트: `{event_id}`" if event_id else "\n이미 처리된 요청입니다."
            await _update_message(client, body, f"✅ 승인됨: task `{task_id}`{suffix}")
        except Exception:
            logger.error("Slack approval 처리 실패", exc_info=True)
            await _update_message(client, body, "⚠️ 승인 처리 실패: 권한 또는 요청 상태를 확인하세요.")

    @app.action("task_deny")
    async def handle_task_deny(ack, body, action, client):
        await ack()
        try:
            task_id = _extract_task_id(action)
            user_id = body.get("user", {}).get("id")
            action_ts = body.get("actions", [{}])[0].get("action_ts")

            event_id = _create_approval_event(
                task_id=task_id,
                approved=False,
                user_id=user_id,
                action_ts=action_ts,
                action_id=action.get("action_id"),
                reason="Slack 버튼으로 거부",
            )
            suffix = f"\n이벤트: `{event_id}`" if event_id else "\n이미 처리된 요청입니다."
            await _update_message(client, body, f"❌ 거부됨: task `{task_id}`{suffix}")
        except Exception:
            logger.error("Slack denial 처리 실패", exc_info=True)
            await _update_message(client, body, "⚠️ 거부 처리 실패: 권한 또는 요청 상태를 확인하세요.")

    @app.action("reply_send")
    async def handle_reply_send_button(ack, body, client):
        """답장 보내기 버튼 클릭 → 즉시 ack + modal 열기."""
        await ack()

        trigger_id = body.get("trigger_id")
        action = body["actions"][0]
        require_authorized_user(body.get("user", {}).get("id"))
        require_action(action, "reply_send")
        task_id = action.get("value")

        import json as _json
        from db import email_store as _email_store
        task = task_store.get_task(int(task_id))
        send_payload = {}
        if task and task.get("send_payload"):
            send_payload = _json.loads(task["send_payload"])

        # 원문은 Slack에 내보내지 않고 저장된 요약/메타데이터만 사용합니다.
        email_data = None
        email_id = task.get("email_id") if task else None
        if email_id:
            email_data = _email_store.get_email(email_id)
        blocks = _reply_confirmation_blocks(send_payload, email_data)

        send_payload_raw = task.get("send_payload") if task else None
        if not send_payload_raw:
            raise ValueError("답장 초안이 없어 발송 확인 창을 열 수 없습니다.")
        if task.get("send_state") not in {"draft_generated", "pending_send_confirmation"}:
            raise ApprovalAuthorityError("답장 요청 상태가 검토 가능하지 않습니다.")
        current_hash = payload_sha256(send_payload_raw)
        assert_active_request(task, payload_hash=current_hash)

        private_metadata = _json.dumps(
            {
                "task_id": int(task_id),
                "send_payload_sha256": current_hash,
                "approval_version": int(task["approval_version"]),
            },
            ensure_ascii=False,
        )

        await client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "reply_send_modal",
                "private_metadata": private_metadata,
                "title": {"type": "plain_text", "text": "답장 확인"},
                "submit": {"type": "plain_text", "text": "최종 발송 확인"},
                "close": {"type": "plain_text", "text": "취소"},
                "blocks": blocks,
            },
        )

    @app.view("reply_send_modal")
    async def handle_reply_send_modal_submit(ack, body):
        """modal 최종 확인 → SEND_CONFIRMED 이벤트 생성."""
        await ack()

        import json as _json
        require_authorized_user(body.get("user", {}).get("id"))
        require_view(body, "reply_send_modal")

        metadata = _json.loads(body["view"]["private_metadata"])
        task_id = int(metadata["task_id"])
        approved_digest = metadata["send_payload_sha256"]
        view_id = body["view"]["id"]

        event_id, created = create_send_confirmation(
            task_id=task_id,
            source=SLACK,
            action="reply_send_modal",
            source_ref=view_id,
            expected_payload_hash=approved_digest,
            expected_approval_version=int(metadata["approval_version"]),
        )
        if created:
            logger.info("SEND_CONFIRMED 이벤트 생성: task %s", task_id)
        else:
            logger.info("SEND_CONFIRMED 중복 요청 무시: task %s", task_id)

    return app


async def run_socket_mode() -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    # launchd receives no secrets in its plist. Load the project-local .env in
    # the child process, just as the Daemon and Operator configuration do.
    load_dotenv()
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise RuntimeError("SLACK_APP_TOKEN 환경변수가 필요합니다.")

    db_initialize()
    app = create_app()
    handler = AsyncSocketModeHandler(app, app_token)
    logger.info("Slack Socket Mode handler 시작")
    await handler.start_async()


def main() -> None:
    parser = argparse.ArgumentParser(description="Mail Assistant Slack approval Socket Mode handler")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(run_socket_mode())


if __name__ == "__main__":
    main()
