"""
승인 이벤트 핸들러

approval_granted / approval_denied 이벤트를 처리합니다.
"""
import logging
from typing import Optional, Tuple, Any

from mail_operator.audit import AuditLogger, AuditAction
from mail_operator.policy import is_action_allowed

logger = logging.getLogger(__name__)


async def handle_approval_granted(
    event: dict,
    mcp_client: Any,
    audit: AuditLogger,
    config: Any
) -> Tuple[bool, Optional[str]]:
    """
    approval_granted 이벤트를 처리합니다.
    
    승인된 태스크를 실행합니다.
    
    Args:
        event: 이벤트 데이터
        mcp_client: MCP 클라이언트
        audit: 감사 로거
        config: Operator 설정
        
    Returns:
        Tuple[bool, Optional[str]]: (성공 여부, 에러 메시지)
    """
    event_id = event['id']
    payload = event['payload']
    task_id = payload.get('task_id')
    
    audit.set_event_context(event_id)
    audit.log(
        AuditAction.APPROVAL_RECEIVED,
        details={"task_id": task_id, "approved": True},
        event_id=event_id,
        task_id=str(task_id) if task_id else None
    )
    authority_validated = False
    try:
        # 1. 태스크 정보 조회
        logger.info(f"승인된 태스크 조회: {task_id}")
        task = await _fetch_task(mcp_client, task_id)
        
        if not task:
            raise ValueError(f"태스크를 찾을 수 없습니다: {task_id}")

        from db.approval_store import assert_active_request, validate_stored_event
        from db.connection import get_connection
        from db.task_state import transition_task

        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (int(task_id),)
            ).fetchone()
            if current_row is None:
                raise ValueError(f"태스크를 찾을 수 없습니다: {task_id}")
            current_task = dict(current_row)
            validate_stored_event(
                conn,
                event_id=event_id,
                task=current_task,
                expected_type="approval_granted",
            )
            assert_active_request(current_task, require_pending=False)
            if current_task.get("task_type") != "reply_email":
                current_task = transition_task(
                    conn, int(task_id), status="in_progress"
                )
            task.update(current_task)
        authority_validated = True
        
        task_type = task.get('task_type')
        
        # 2. 실행 가능 여부 확인
        if not is_action_allowed(task_type):
            # 승인 경로에서 차단된 액션 — Slack으로 명확히 실패 사유 전달
            try:
                await mcp_client.call_tool(
                    "slack_notify",
                    {"message": f"⚠️ 미지원 액션: {task_type} — 현재 지원하지 않는 액션입니다 (task_id={task_id})"}
                )
            except Exception as slack_err:
                logger.warning(f"Slack 알림 실패 (차단된 액션): {slack_err}")
            raise ValueError(f"차단된 액션: {task_type}")
        
        # 3. 태스크 실행
        logger.info(f"태스크 실행: {task_id} ({task_type})")
        result_status, result = await _execute_task(mcp_client, task, audit)

        if result_status == "pending":
            # reply_email 전용 플로우 — completed로 마킹하지 않음
            logger.info(f"태스크 {task_id}: reply_email은 send 플로우에서 완료됩니다")
            audit.log(
                AuditAction.EVENT_PROCESSED,
                details={"task_id": task_id, "note": "reply_email pending send flow"},
                event_id=event_id
            )
            return True, None

        if not result_status:
            raise ValueError(f"태스크 실행 실패: {result}")

        # 4. 태스크 상태 업데이트
        await _update_task_status(mcp_client, task_id, "completed", result)
        
        audit.log(
            AuditAction.TASK_EXECUTED,
            details={"task_id": task_id, "task_type": task_type, "result": result[:100] if result else None},
            task_id=str(task_id)
        )
        
        audit.log(
            AuditAction.EVENT_PROCESSED,
            details={"task_id": task_id},
            event_id=event_id
        )
        
        return True, None
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"approval_granted 처리 실패: {error_msg}", exc_info=True)
        
        # 태스크 상태를 failed로 업데이트
        if task_id and authority_validated:
            try:
                await _update_task_status(mcp_client, task_id, "failed", error_msg)
            except Exception:
                pass
        
        audit.log(
            AuditAction.TASK_FAILED,
            event_id=event_id,
            task_id=str(task_id) if task_id else None,
            success=False,
            error=error_msg
        )
        
        return False, error_msg
    
    finally:
        audit.clear_event_context()


async def handle_approval_denied(
    event: dict,
    mcp_client: Any,
    audit: AuditLogger,
    config: Any
) -> Tuple[bool, Optional[str]]:
    """
    approval_denied 이벤트를 처리합니다.
    
    거부된 태스크를 cancelled 상태로 업데이트합니다.
    
    Args:
        event: 이벤트 데이터
        mcp_client: MCP 클라이언트
        audit: 감사 로거
        config: Operator 설정
        
    Returns:
        Tuple[bool, Optional[str]]: (성공 여부, 에러 메시지)
    """
    event_id = event['id']
    payload = event['payload']
    task_id = payload.get('task_id')
    deny_reason = payload.get('reason', '사용자 거부')
    
    audit.set_event_context(event_id)
    audit.log(
        AuditAction.APPROVAL_RECEIVED,
        details={"task_id": task_id, "approved": False, "reason": deny_reason},
        event_id=event_id,
        task_id=str(task_id) if task_id else None
    )
    
    try:
        from db.approval_store import validate_stored_event
        from db.connection import get_connection

        task = await _fetch_task(mcp_client, task_id)
        if not task:
            raise ValueError(f"태스크를 찾을 수 없습니다: {task_id}")
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (int(task_id),)
            ).fetchone()
            if current_row is None:
                raise ValueError(f"태스크를 찾을 수 없습니다: {task_id}")
            validate_stored_event(
                conn,
                event_id=event_id,
                task=dict(current_row),
                expected_type="approval_denied",
            )

        # 태스크 상태 업데이트 (거부 이벤트 생성 시 이미 원자적으로 취소됨)
        logger.info(f"태스크 거부됨: {task_id}")
        await _update_task_status(mcp_client, task_id, "cancelled", deny_reason)
        
        audit.log(
            AuditAction.EVENT_PROCESSED,
            details={"task_id": task_id, "status": "cancelled"},
            event_id=event_id
        )
        
        return True, None
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"approval_denied 처리 실패: {error_msg}", exc_info=True)
        
        audit.log(
            AuditAction.EVENT_FAILED,
            event_id=event_id,
            success=False,
            error=error_msg
        )
        
        return False, error_msg
    
    finally:
        audit.clear_event_context()


async def handle_send_confirmed(
    event: dict,
    mcp_client: Any,
    audit: AuditLogger,
    config: Any
) -> Tuple[bool, Optional[str]]:
    """Process one approval-bound SMTP attempt through the durable ledger.

    ``sending`` is deliberately not reclaimable. If it is observed again after
    the atomic pre-SMTP claim, delivery is quarantined as unknown and requires a
    new review/confirmation instead of automatic re-send.
    """
    import json
    from db import outbound_send_store, task_store
    from core.smtp_client import (
        SMTPAdapter,
        SMTPDeliveryUnknownError,
        SMTPNotConfiguredError,
    )
    from core.config import Config
    from core.email_headers import normalize_message_id
    from mail_operator.send_confirmation import (
        stable_outbound_message_id,
        validate_send_confirmation,
    )

    event_id = event.get("id")
    payload = event.get("payload", {})
    task_id = payload.get("task_id")

    audit.set_event_context(event_id)
    audit.log(
        AuditAction.APPROVAL_RECEIVED,
        details={"task_id": task_id, "event_type": "send_confirmed"},
        event_id=event_id,
        task_id=str(task_id) if task_id else None,
    )

    try:
        if not event_id or not task_id:
            logger.error("handle_send_confirmed: event_id/task_id 없음")
            return False, "event_id or task_id missing"

        task = task_store.get_task(int(task_id))
        if not task:
            logger.error(f"handle_send_confirmed: task {task_id} 없음")
            return False, f"task {task_id} not found"

        # Terminal task states never reach SMTP, even if their event is re-leased.
        send_state = task.get("send_state")
        if send_state in {"sent", "send_failed", "delivery_unknown"}:
            logger.info(
                f"handle_send_confirmed: task {task_id} terminal send state "
                f"(send_state={send_state!r}) — 재전송 없음"
            )
            audit.log(
                AuditAction.EVENT_PROCESSED,
                details={"task_id": task_id, "skipped": True, "send_state": send_state},
                event_id=event_id,
            )
            return True, None

        # send_payload에서 recipient/subject/body 조회
        send_payload_raw = task.get("send_payload")
        if not send_payload_raw:
            logger.error(f"handle_send_confirmed: task {task_id} send_payload 없음")
            return False, "send_payload missing"

        confirmation_error = validate_send_confirmation(event, task, send_payload_raw)
        if confirmation_error:
            logger.error(
                "handle_send_confirmed: task %s 승인 검증 실패: %s",
                task_id,
                confirmation_error,
            )
            audit.log(
                AuditAction.TASK_FAILED,
                event_id=event_id,
                task_id=str(task_id),
                success=False,
                error=confirmation_error,
            )
            return False, confirmation_error

        try:
            send_payload = json.loads(send_payload_raw)
        except json.JSONDecodeError as e:
            logger.error(f"handle_send_confirmed: send_payload JSON 파싱 실패: {e}")
            return False, f"send_payload JSON error: {e}"

        recipient = (send_payload.get("recipient") or "").strip()
        subject = (send_payload.get("subject") or "Reply").strip()
        body = send_payload.get("body") or ""
        message_id = send_payload.get("message_id")
        in_reply_to = send_payload.get("in_reply_to")
        references = send_payload.get("references")
        thread_topic = send_payload.get("thread_topic")
        if not recipient or not body:
            logger.error(f"handle_send_confirmed: task {task_id} invalid send_payload")
            return False, "send_payload recipient/body missing"

        approval_payload_hash = payload.get("send_payload_sha256")
        smtp_config = Config.from_env()
        outbound_message_id = outbound_send_store.resolve_outbound_message_id(
            task_id=int(task_id),
            approval_payload_hash=approval_payload_hash,
            fallback_message_id=(
                normalize_message_id(message_id)
                if message_id
                else stable_outbound_message_id(
                    int(task_id), approval_payload_hash, smtp_config.smtp_user
                )
            ),
        )
        claim = outbound_send_store.claim_outbound_send(
            task_id=int(task_id),
            event_id=str(event_id),
            approval_payload_hash=approval_payload_hash,
            outbound_message_id=outbound_message_id,
        )
        if claim["disposition"] != outbound_send_store.CLAIMED:
            logger.info(
                "handle_send_confirmed: task %s SMTP 미실행 (claim=%s, status=%s)",
                task_id,
                claim["disposition"],
                claim.get("status"),
            )
            audit.log(
                AuditAction.EVENT_PROCESSED,
                details={
                    "task_id": task_id,
                    "skipped": True,
                    "reason": claim["disposition"],
                    "send_state": claim.get("status"),
                },
                event_id=event_id,
            )
            if claim["disposition"] == outbound_send_store.REJECTED:
                return False, str(claim.get("reason") or "send authority rejected")
            return True, None

        ledger_id = int(claim["ledger_id"])
        audit.log_tool_call(
            "smtp_send",
            {
                "task_id": task_id,
                "ledger_id": ledger_id,
                "outbound_message_id": outbound_message_id,
            },
        )

        # No live SMTP is used in tests; production execution still occurs only
        # after the durable claim above.
        try:
            smtp = SMTPAdapter(smtp_config)
            smtp.send(
                to=recipient,
                subject=subject,
                body=body,
                message_id=outbound_message_id,
                in_reply_to=in_reply_to,
                references=references,
                thread_topic=thread_topic,
            )

            try:
                outbound_send_store.finalize_outbound_send(
                    ledger_id,
                    execution_result="SMTP accepted outbound message",
                )
            except Exception as finalize_error:
                # SMTP has returned success but local commit did not. Never
                # re-send automatically; a later operator may not know whether
                # the provider accepted the message.
                outbound_send_store.mark_delivery_unknown(
                    ledger_id,
                    f"SMTP accepted but local finalize failed: {finalize_error}",
                    smtp_returned_success=True,
                )
                logger.error(
                    "handle_send_confirmed: task %s SMTP 성공 후 DB 확정 실패; "
                    "delivery_unknown 격리",
                    task_id,
                    exc_info=True,
                )
                audit.log(
                    AuditAction.TASK_FAILED,
                    event_id=event_id,
                    task_id=str(task_id),
                    success=False,
                    error="SMTP accepted but delivery state is unknown",
                )
                return True, None

            logger.info(
                "handle_send_confirmed: task %s 발송 완료 (ledger=%s)",
                task_id,
                ledger_id,
            )

            audit.log(
                AuditAction.TASK_EXECUTED,
                details={"task_id": task_id, "ledger_id": ledger_id},
                task_id=str(task_id),
            )
            audit.log(
                AuditAction.EVENT_PROCESSED,
                details={"task_id": task_id},
                event_id=event_id,
            )
            return True, None

        except SMTPDeliveryUnknownError as e:
            msg = f"SMTP delivery result unknown: {e}"
            logger.error(
                "handle_send_confirmed: task %s SMTP 결과 불확실; 자동 재발송 금지",
                task_id,
            )
            outbound_send_store.mark_delivery_unknown(ledger_id, msg)
            audit.log(
                AuditAction.TASK_FAILED,
                event_id=event_id,
                task_id=str(task_id),
                success=False,
                error=msg,
            )
            return True, None

        except Exception as e:
            prefix = "SMTP 미설정" if isinstance(e, SMTPNotConfiguredError) else "SMTP 발송 실패"
            msg = f"{prefix}: {e}"
            logger.error("handle_send_confirmed: task %s known send failure", task_id)
            outbound_send_store.mark_send_failed(ledger_id, msg)
            audit.log(
                AuditAction.TASK_FAILED,
                event_id=event_id,
                task_id=str(task_id),
                success=False,
                error=msg,
            )
            return False, msg

    except Exception as e:
        error_msg = str(e)
        logger.error(f"handle_send_confirmed 처리 실패: {error_msg}", exc_info=True)
        audit.log(
            AuditAction.EVENT_FAILED,
            event_id=event_id,
            success=False,
            error=error_msg,
        )
        return False, error_msg

    finally:
        audit.clear_event_context()


async def _fetch_task(mcp_client: Any, task_id: int) -> Optional[dict]:
    """태스크 정보 조회"""
    try:
        from db import task_store as _task_store
        task = _task_store.get_task(int(task_id))
        if task is None:
            return None
        task = dict(task)
        task.setdefault('task_id', task.get('id'))
        return task
    except Exception as e:
        logger.error(f"태스크 조회 실패: {e}")
        return None


async def _create_reply_draft(task: dict) -> dict:
    """reply_email 태스크의 답장 초안을 생성하고 send_payload를 DB에 저장합니다."""
    import asyncio
    import json
    from mail_operator.cli_runner import parse_llm_json, run_llm
    from mail_operator.prompts import build_reply_draft_prompt
    from mail_operator.schemas import ReplyDraft
    from db import attachment_store, email_store, preference_store, task_store
    from core.email_headers import (
        UnsafeEmailHeaderError,
        build_reply_references,
        generate_message_id,
        normalize_message_id,
        normalize_thread_topic,
        validate_header_value,
        validate_mailbox,
    )

    task_id = task.get("id") or task.get("task_id")
    email_id = task.get("email_id")

    # recipient 조회
    email_data = email_store.get_email(email_id) if email_id else None
    raw_recipient = email_data.get("sender_email", "") if email_data else ""
    try:
        recipient = validate_mailbox("To", raw_recipient)
    except UnsafeEmailHeaderError:
        payload = {
            "recipient": "",
            "subject": "Reply",
            "body": "",
            "error": "recipient_missing",
        }
        if task_id:
            task_store.update_send_payload(int(task_id), json.dumps(payload, ensure_ascii=False))
            task_store.update_send_state(int(task_id), "draft_failed")
        return payload

    attachments = attachment_store.get_attachments_for_email(email_id)
    preference_rules = preference_store.resolve_reply_preferences(email_data or {})
    prompt = build_reply_draft_prompt(
        task,
        email_data,
        attachments=attachments,
        preference_rules=preference_rules,
    )
    result = await asyncio.to_thread(
        run_llm,
        prompt,
        60,
        ReplyDraft.model_json_schema(),
    )
    if not result["success"]:
        payload = {
            "recipient": recipient,
            "subject": "",
            "body": "",
            "error": "draft_generation_failed",
        }
        if task_id:
            task_store.update_send_payload(int(task_id), json.dumps(payload, ensure_ascii=False))
            task_store.update_send_state(int(task_id), "draft_failed")
        raise RuntimeError("답장 초안 생성 실패")

    try:
        draft = ReplyDraft.model_validate(
            parse_llm_json(result["content"])
        ).model_dump(mode="json")
    except Exception as exc:
        payload = {
            "recipient": recipient,
            "subject": "",
            "body": "",
            "error": "draft_validation_failed",
        }
        if task_id:
            task_store.update_send_payload(int(task_id), json.dumps(payload, ensure_ascii=False))
            task_store.update_send_state(int(task_id), "draft_failed")
        raise ValueError("답장 초안 검증 실패") from exc

    subject = draft["subject"]
    body = draft["body"]

    try:
        subject = validate_header_value("Subject", subject)
    except UnsafeEmailHeaderError as exc:
        payload = {
            "recipient": recipient,
            "subject": "",
            "body": "",
            "error": "unsafe_draft_header",
        }
        if task_id:
            task_store.update_send_state(int(task_id), "draft_failed")
            task_store.update_send_payload(int(task_id), json.dumps(payload, ensure_ascii=False))
        raise ValueError("답장 초안 제목에 허용되지 않는 헤더 문자가 있습니다") from exc

    # send_payload 구성 및 저장
    payload = {
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "message_id": generate_message_id(task_id=int(task_id) if task_id else None),
    }
    if preference_rules:
        payload["preference_rule_ids"] = [
            int(rule["id"]) for rule in preference_rules
        ]
    original_message_id = email_data.get("message_id") if email_data else None
    if original_message_id:
        try:
            original_message_id = normalize_message_id(
                original_message_id,
                name="source Message-ID",
            )
        except UnsafeEmailHeaderError:
            logger.warning(
                "원본 Message-ID가 안전한 RFC 형식이 아니어서 답장 스레드 헤더에서 제외합니다: email_id=%s",
                email_id,
            )
        else:
            payload["in_reply_to"] = original_message_id
            parent_references = email_data.get("references_header")
            try:
                payload["references"] = build_reply_references(
                    parent_references,
                    original_message_id,
                )
            except UnsafeEmailHeaderError:
                logger.warning(
                    "원본 References가 안전한 RFC 형식이 아니어서 부모 Message-ID만 사용합니다: email_id=%s",
                    email_id,
                )
                payload["references"] = original_message_id

    thread_topic = email_data.get("thread_topic") if email_data else None
    if thread_topic:
        try:
            payload["thread_topic"] = normalize_thread_topic(thread_topic)
        except UnsafeEmailHeaderError:
            logger.warning(
                "원본 Thread-Topic이 안전하지 않아 답장 헤더에서 제외합니다: email_id=%s",
                email_id,
            )
    if task_id:
        task_store.update_send_payload(int(task_id), json.dumps(payload, ensure_ascii=False))
        task_store.update_send_state(int(task_id), "draft_generated")

    return payload


async def _execute_task(
    mcp_client: Any,
    task: dict,
    audit: AuditLogger
) -> Tuple[bool, Optional[str]]:
    """태스크 실행"""
    task_type = task.get('task_type')
    task_id = task.get('task_id')
    
    audit.log_tool_call("task_execute", {"task_id": task_id, "task_type": task_type})
    
    try:
        # 태스크 타입별 실행
        if task_type == "notify_slack":
            result = await mcp_client.call_tool(
                "slack_notify",
                {"message": task.get('description', 'Task notification')}
            )
            return True, result
        
        elif task_type == "create_reminder":
            title = task.get("title", "리마인더")
            description = task.get("description", "")
            message = f"🔔 리마인더: {title}"
            if description:
                message += f" — {description}"
            try:
                await mcp_client.call_tool("slack_notify", {"message": message})
                audit.log_tool_call("slack_notify", {"task_id": task_id, "type": "create_reminder"})
                return True, f"🔔 리마인더 전송 완료: {title}"
            except Exception as e:
                return False, f"slack_notify 실패: {e}"

        elif task_type == "reply_email":
            # reply_email은 별도 reply 플로우(reply_send 버튼 → modal)를 통해 처리됨
            # _execute_task 경로는 방어적 fallback — completed로 마킹하지 않음
            await _create_reply_draft(task)
            return "pending", {"message": "reply_email uses dedicated send flow"}

        else:
            logger.warning(f"미지원 액션 타입 도달: {task_type} (task_id={task_id})")
            try:
                await mcp_client.call_tool(
                    "slack_notify",
                    {"message": f"⚠️ 미지원 액션: {task_type} — 현재 지원하지 않는 액션입니다 (task_id={task_id})"}
                )
            except Exception as slack_err:
                logger.warning(f"Slack 알림 실패 (미지원 액션): {slack_err}")
            return False, f"Unsupported action type: {task_type}"
    
    except Exception as e:
        logger.error(f"태스크 실행 실패: {e}")
        return False, str(e)


async def _update_task_status(
    mcp_client: Any,
    task_id: int,
    status: str,
    result: Optional[str] = None
) -> None:
    """태스크 상태 업데이트"""
    try:
        await mcp_client.call_tool(
            "task_update_status",
            {
                "task_id": task_id,
                "status": status,
                "execution_result": result
            }
        )
    except Exception as e:
        logger.error(f"태스크 상태 업데이트 실패: {e}")
