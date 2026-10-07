"""
new_email 이벤트 핸들러

새 이메일 이벤트를 처리합니다:
1. DB에서 이메일 정보 조회
2. LLM으로 분석 (요약, 긴급도, 액션 아이템)
3. 분석 결과 DB에 저장
4. 태스크 생성
5. 위험도에 따라 자동 실행 또는 승인 요청
"""
import asyncio
import hashlib
import json
import logging
import uuid
from typing import Optional, Tuple, Any

from mail_operator.audit import AuditLogger, AuditAction
from mail_operator.cli_runner import run_llm, parse_llm_json
from mail_operator.policy import evaluate_risk, ExecutionPolicy, RiskLevel
from mail_operator.prompts import build_email_analysis_prompt
from mail_operator.schemas import EmailAnalysis

logger = logging.getLogger(__name__)


ACTION_TYPE_ALIASES = {
    "reply_needed": "reply_email",
    "meeting_schedule": "create_meeting",
    "task_todo": "create_reminder",
    "review_document": "create_reminder",
    "forward_to": "forward_email",
    "reminder": "create_reminder",
}


async def handle_new_email(
    event: dict,
    mcp_client: Any,
    audit: AuditLogger,
    config: Any
) -> Tuple[bool, Optional[str]]:
    """
    new_email 이벤트를 처리합니다.
    
    Args:
        event: 이벤트 데이터 (id, event_type, payload 등)
        mcp_client: MCP 클라이언트 (도구 호출용)
        audit: 감사 로거
        config: Operator 설정
        
    Returns:
        Tuple[bool, Optional[str]]: (성공 여부, 에러 메시지)
    """
    event_id = event['id']
    payload = event['payload']
    email_id = payload.get('email_id')
    
    audit.set_event_context(event_id)
    audit.log(
        AuditAction.EVENT_RECEIVED,
        details={"email_id": email_id},
        event_id=event_id
    )
    
    try:
        from db import preference_store, processing_store

        committed = processing_store.get_new_email_processing(email_id)
        if committed is not None:
            followups_ok, followup_error = await _dispatch_processing_outbox(
                event_id=event_id,
                email_id=email_id,
                mcp_client=mcp_client,
                audit=audit,
                config=config,
            )
            if not followups_ok:
                return False, followup_error
            audit.log(
                AuditAction.EVENT_PROCESSED,
                details={
                    "email_id": email_id,
                    "tasks_created": 0,
                    "replayed_committed_processing": True,
                },
                event_id=event_id,
            )
            return True, None

        # 1. DB에서 이메일 정보 조회
        logger.info(f"이메일 조회: {email_id}")
        email_data = await _fetch_email_data(mcp_client, email_id, config.max_body_length)
        
        if not email_data:
            raise ValueError(f"이메일을 찾을 수 없습니다: {email_id}")
        
        audit.log_tool_call("email_db_get_with_body", {"email_id": email_id})

        # Only rules separately confirmed by an authorized user can suppress
        # processing. The message remains stored and searchable.
        processing_policy = preference_store.resolve_processing_policy(email_data)
        if processing_policy["suppress_processing"]:
            matched_rule_ids = [
                int(rule["id"]) for rule in processing_policy["rules"]
            ]
            skipped_analysis = {
                "summary": email_data.get("body_summary")
                or "사용자 규칙에 따라 자동 분석과 후속 액션 생성을 생략했습니다.",
                "urgency": "low",
                "priority_score": 0,
                "priority_level": "low",
                "requires_response": False,
                "action_items": [],
                "draft_reply": None,
                "confidence": 1.0,
                "warnings": [],
            }
            committed = processing_store.commit_new_email_processing(
                source_event_id=event_id,
                email_id=email_id,
                analysis=skipped_analysis,
                task_specs=[],
                slack_enabled=False,
            )
            audit.log_tool_call(
                "user_preference_applied",
                {
                    "email_id": email_id,
                    "rule_ids": matched_rule_ids,
                    "effect": "archive_only",
                },
            )
            audit.log(
                AuditAction.EVENT_PROCESSED,
                details={
                    "email_id": email_id,
                    "tasks_created": len(committed["tasks"]),
                    "processing_skipped": True,
                    "preference_rule_ids": matched_rule_ids,
                },
                event_id=event_id,
            )
            return True, None
        
        # 2. 첨부파일 정보 조회 (있는 경우)
        attachments = []
        if email_data.get("has_attachments"):
            attachments = await _fetch_attachments(mcp_client, email_id)
            audit.log_tool_call("email_db_get_attachments", {"email_id": email_id})
        
        # 3. LLM으로 이메일 분석
        logger.info(f"이메일 분석 중: {email_id}")
        analysis = await _analyze_email(
            mcp_client,
            email_data,
            attachments,
            audit,
            config,
        )
        
        if not analysis:
            raise ValueError("이메일 분석 실패")
        
        # 4. 분석, 태스크, processed, 후속 알림/초안 의도를 한 트랜잭션에 커밋
        analysis = dict(analysis)
        analysis["priority_level"] = _score_to_level(
            analysis.get("priority_score", 50)
        )
        task_specs = _prepare_task_specs(email_id, analysis, audit)
        committed = processing_store.commit_new_email_processing(
            source_event_id=event_id,
            email_id=email_id,
            analysis=analysis,
            task_specs=task_specs,
            slack_enabled=bool(getattr(config, "slack_bot_token", None)),
        )
        tasks_created = committed["tasks"]

        if not committed["already_committed"]:
            audit.log_tool_call(
                "email_processing_commit",
                {
                    "email_id": email_id,
                    "tasks": len(tasks_created),
                    "followup_intents_persisted": True,
                },
            )
            for task in tasks_created:
                audit.log(
                    AuditAction.TASK_CREATED,
                    details={
                        "task_id": task["task_id"],
                        "task_type": task["task_type"],
                        "requires_approval": task["requires_approval"],
                    },
                )

        # 5. 커밋된 outbox만 실행. lease replay는 전달 완료 intent를 건너뜀.
        followups_ok, followup_error = await _dispatch_processing_outbox(
            event_id=event_id,
            email_id=email_id,
            mcp_client=mcp_client,
            audit=audit,
            config=config,
        )
        if not followups_ok:
            return False, followup_error
        
        audit.log(
            AuditAction.EVENT_PROCESSED,
            details={
                "email_id": email_id,
                "tasks_created": len(tasks_created),
                "urgency": analysis.get("urgency")
            },
            event_id=event_id
        )
        
        return True, None
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"new_email 처리 실패: {error_msg}", exc_info=True)
        
        audit.log(
            AuditAction.EVENT_FAILED,
            event_id=event_id,
            success=False,
            error=error_msg
        )
        
        return False, error_msg
    
    finally:
        audit.clear_event_context()


def _prepare_task_specs(
    email_id: str,
    analysis: dict,
    audit: AuditLogger,
) -> list[dict]:
    """Build deterministic task intents without writing any partial DB state."""
    specs = []
    for index, raw_item in enumerate(analysis.get("action_items", [])):
        item = _normalize_action_item(raw_item)
        task_type = item.get("type", "unknown")
        decision = evaluate_risk(task_type, item)
        audit.log_policy_decision(
            task_type=task_type,
            risk_level=decision.risk_level.value,
            policy=decision.policy.value,
            reason=decision.reason,
        )
        if decision.policy == ExecutionPolicy.DENY:
            logger.warning("차단된 액션: %s", task_type)
            continue

        canonical = {
            "task_type": task_type,
            "title": item.get("title", task_type),
            "description": item.get("description", ""),
            "priority": item.get("priority", "medium"),
            "deadline": item.get("deadline"),
            "approval_status": "pending" if decision.requires_approval else "none",
        }
        digest = hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        specs.append(
            {
                "email_id": email_id,
                **canonical,
                "idempotency_key": (
                    f"new-email:{email_id}:action:{index}:{digest}"
                ),
            }
        )
    return specs


class _KnownOutboxFailure(RuntimeError):
    pass


async def _call_slack_outbox_tool(
    mcp_client: Any,
    *,
    tool: str,
    arguments: dict,
    idempotency_key: str,
) -> None:
    try:
        result = await mcp_client.call_tool(
            tool,
            {**arguments, "idempotency_key": idempotency_key},
        )
    except Exception:
        raise
    if result is None or (
        isinstance(result, str) and result.lstrip().startswith(("❌", "⚠️"))
    ):
        raise _KnownOutboxFailure(f"{tool} rejected the notification")


async def _dispatch_outbox_item(
    item: dict,
    *,
    mcp_client: Any,
    audit: AuditLogger,
) -> bool:
    """Dispatch one claimed intent; return True when intentionally discarded."""
    from db import email_store, task_store

    topic = item["topic"]
    payload = item["payload"]
    idempotency_key = item["idempotency_key"]

    if topic == "reply_draft":
        task = task_store.get_task(int(payload["task_id"]))
        if not task:
            return True
        await _generate_reply_drafts(
            [{
                "task_id": int(task["id"]),
                "email_id": task["email_id"],
                "task_type": task["task_type"],
                "title": task["title"],
                "description": task.get("description") or "",
                "requires_approval": task.get("approval_status") == "pending",
                "send_state": task.get("send_state"),
                "send_payload": (
                    json.loads(task["send_payload"])
                    if task.get("send_payload")
                    else None
                ),
            }],
            audit,
        )
        return False

    if topic == "slack_email_summary":
        email = email_store.get_email(str(payload["email_id"]))
        if not email:
            return True
        emoji = {"urgent": "🔴", "normal": "🟡", "low": "🟢"}.get(
            email.get("urgency", "normal"), "🟡"
        )
        message = (
            f"{emoji} 새 이메일\n"
            f"• 발신자: {email.get('sender', '')} <{email.get('sender_email', '')}>\n"
            f"• 제목: {email.get('subject', '')}\n"
            f"• 요약: {email.get('body_summary') or '요약 없음'}\n"
        )
        if email.get("requires_response"):
            message += "• ⚠️ 답장 필요\n"
        await _call_slack_outbox_tool(
            mcp_client,
            tool="slack_notify",
            arguments={"message": message},
            idempotency_key=idempotency_key,
        )
        audit.log_tool_call("slack_notify", {"message_length": len(message)})
        return False

    task = task_store.get_task(int(payload["task_id"]))
    if not task or task.get("approval_status") != "pending":
        return True
    task_message = (
        f"• 작업: {task.get('title') or task.get('task_type')}\n"
        f"• 유형: {task.get('task_type')}\n"
        f"• 설명: {task.get('description') or '설명 없음'}"
    )
    if topic == "slack_reply_button":
        send_state = task.get("send_state")
        if send_state in {
            "pending_send_confirmation", "sending", "sent",
            "send_failed", "delivery_unknown",
        }:
            return True
        if send_state != "draft_generated" or not _is_valid_send_payload(
            json.loads(task["send_payload"]) if task.get("send_payload") else None
        ):
            return True
        await _call_slack_outbox_tool(
            mcp_client,
            tool="slack_notify_with_send_button",
            arguments={
                "message": "✉️ 답장 초안 요청\n" + task_message,
                "task_id": int(task["id"]),
            },
            idempotency_key=idempotency_key,
        )
        task_store.update_send_state(int(task["id"]), "pending_send_confirmation")
        audit.log_tool_call(
            "slack_notify_with_send_button", {"task_id": int(task["id"])}
        )
        return False

    if topic == "slack_task_approval":
        await _call_slack_outbox_tool(
            mcp_client,
            tool="slack_notify_with_approval",
            arguments={
                "message": "🔐 승인 필요 작업\n" + task_message,
                "task_id": int(task["id"]),
            },
            idempotency_key=idempotency_key,
        )
        audit.log_tool_call(
            "slack_notify_with_approval", {"task_id": int(task["id"])}
        )
        return False

    return True


async def _dispatch_processing_outbox(
    *,
    event_id: str,
    email_id: str,
    mcp_client: Any,
    audit: AuditLogger,
    config: Any,
) -> Tuple[bool, Optional[str]]:
    from db import processing_store

    owner = f"{event_id}:outbox:{uuid.uuid4().hex}"
    processing_store.recover_expired_outbox(email_id)
    for item in processing_store.list_outbox_for_email(email_id):
        if item["status"] in processing_store.TERMINAL_OUTBOX_STATUSES:
            continue
        claimed = processing_store.claim_outbox(
            int(item["id"]),
            owner=owner,
            lease_seconds=getattr(config, "event_lease_seconds", 300),
        )
        if claimed is None:
            continue
        try:
            discarded = await _dispatch_outbox_item(
                claimed,
                mcp_client=mcp_client,
                audit=audit,
            )
        except _KnownOutboxFailure:
            processing_store.retry_outbox(
                int(claimed["id"]),
                owner=owner,
                error_code="known_delivery_failure",
                base_delay_seconds=getattr(config, "event_retry_base_seconds", 5),
                max_delay_seconds=getattr(config, "event_retry_max_seconds", 3600),
            )
            return False, "notification delivery failed before acceptance"
        except Exception:
            if claimed["topic"] == "reply_draft":
                # This is a local, state-verifiable operation with no external
                # notification or SMTP side effect, so it remains retryable.
                processing_store.retry_outbox(
                    int(claimed["id"]),
                    owner=owner,
                    error_code="local_draft_failure",
                    base_delay_seconds=getattr(
                        config, "event_retry_base_seconds", 5
                    ),
                    max_delay_seconds=getattr(
                        config, "event_retry_max_seconds", 3600
                    ),
                )
                return False, "local reply draft intent failed"

            # The external call may have succeeded. Quarantine rather than resend.
            processing_store.mark_outbox_delivery_unknown(
                int(claimed["id"]),
                owner=owner,
                error_code="delivery_result_unknown",
            )
            logger.error(
                "후속 intent 결과 불확실; 자동 재전송 금지: topic=%s",
                claimed["topic"],
                exc_info=True,
            )
            continue

        if not processing_store.finish_outbox(
            int(claimed["id"]), owner=owner, discarded=discarded
        ):
            return False, "outbox completion ownership changed"

    if processing_store.has_retryable_outbox(email_id):
        return False, "follow-up intent is waiting for retry or lease recovery"
    return True, None


async def _fetch_email_data(
    mcp_client: Any, 
    email_id: str, 
    max_body_length: int
) -> Optional[dict]:
    """DB에서 이메일 데이터 조회"""
    try:
        # MCP 도구 호출
        result = await mcp_client.call_tool(
            "email_db_get_with_body",
            {"email_id": email_id, "max_body_length": max_body_length}
        )
        
        # RAW_DATA 파싱
        if result and "[RAW_DATA]" in result:
            raw_start = result.index("[RAW_DATA]") + len("[RAW_DATA]")
            raw_json = result[raw_start:].strip()
            return json.loads(raw_json)
        
        return None
        
    except Exception as e:
        logger.error(f"이메일 데이터 조회 실패: {e}")
        return None


async def _fetch_attachments(mcp_client: Any, email_id: str) -> list:
    """첨부파일 메타데이터 조회"""
    try:
        result = await mcp_client.call_tool(
            "email_db_get_attachments",
            {"email_id": email_id}
        )
        
        if result and "[RAW_DATA]" in result:
            raw_start = result.index("[RAW_DATA]") + len("[RAW_DATA]")
            raw_json = result[raw_start:].strip()
            return json.loads(raw_json)
        
        return []
        
    except Exception as e:
        logger.error(f"첨부파일 조회 실패: {e}")
        return []


async def _analyze_email_with_llm(
    email_data: dict,
    attachments: list,
    audit: AuditLogger,
    max_body_length: int = 10000,
    max_attachment_text_length: int = 5000,
) -> dict:
    """CLI subprocess LLM으로 이메일 분석"""
    prompt = build_email_analysis_prompt(
        email_data,
        attachments,
        max_body_length=max_body_length,
        max_attachment_text_length=max_attachment_text_length,
    )
    result = await asyncio.to_thread(
        run_llm,
        prompt,
        90,
        EmailAnalysis.model_json_schema(),
    )
    if not result["success"]:
        raise RuntimeError(f"LLM 분석 실패 ({result['provider']}): {result['error']}")
    raw_analysis = parse_llm_json(result["content"])
    analysis = EmailAnalysis.model_validate(raw_analysis).model_dump(mode="json")
    audit.log_llm_interaction(
        prompt_summary="새 이메일 분석",
        response_summary=f"urgency={analysis.get('urgency')}, actions={len(analysis.get('action_items', []))}",
        model=result["provider"]
    )
    return analysis


async def _analyze_email(
    mcp_client: Any,
    email_data: dict,
    attachments: list,
    audit: AuditLogger,
    config: Any,
) -> dict:
    """LLM으로 이메일 분석"""
    return await _analyze_email_with_llm(
        email_data,
        attachments,
        audit,
        max_body_length=getattr(config, "max_body_length", 10000),
        max_attachment_text_length=getattr(config, "max_attachment_text_length", 5000),
    )


def _format_attachments(attachments: list) -> str:
    """첨부파일 목록 포맷팅"""
    if not attachments:
        return "없음"
    
    lines = []
    for att in attachments:
        size = att.get('file_size', 0)
        size_str = f"{size/1024:.1f}KB" if size < 1024*1024 else f"{size/1024/1024:.1f}MB"
        lines.append(f"- {att.get('filename', 'unknown')} ({size_str})")
    
    return "\n".join(lines)


async def _save_analysis(mcp_client: Any, email_id: str, analysis: dict) -> None:
    """분석 결과를 DB에 저장"""
    result = await mcp_client.call_tool(
        "email_db_update_analysis",
        {
            "email_id": email_id,
            "body_summary": analysis.get("summary"),
            "priority_score": analysis.get("priority_score"),
            "priority_level": _score_to_level(analysis.get("priority_score", 50)),
            "urgency": analysis.get("urgency"),
            "requires_response": analysis.get("requires_response", False)
        }
    )
    _ensure_tool_success(result, "email_db_update_analysis")


def _score_to_level(score: int) -> str:
    """점수를 레벨로 변환"""
    if score >= 80:
        return "urgent"
    elif score >= 60:
        return "high"
    elif score >= 40:
        return "medium"
    else:
        return "low"


async def _create_task(
    mcp_client: Any,
    email_id: str,
    action_item: dict,
    analysis: dict,
    audit: AuditLogger
) -> Optional[dict]:
    """태스크 생성"""
    action_item = _normalize_action_item(action_item)
    task_type = action_item.get("type", "unknown")
        
    # 정책 평가
    decision = evaluate_risk(task_type, action_item)
        
    audit.log_policy_decision(
        task_type=task_type,
        risk_level=decision.risk_level.value,
        policy=decision.policy.value,
        reason=decision.reason
    )
        
    # 차단된 액션이면 스킵
    if decision.policy == ExecutionPolicy.DENY:
        logger.warning(f"차단된 액션: {task_type}")
        return None
        
    # 태스크 생성
    result = await mcp_client.call_tool(
        "task_create",
        {
            "email_id": email_id,
            "task_type": task_type,
            "title": action_item.get("title", task_type),
            "description": action_item.get("description", ""),
            "priority": action_item.get("priority", "medium"),
            "deadline": action_item.get("deadline"),
            "requires_approval": decision.requires_approval
        }
    )
    _ensure_tool_success(result, "task_create")
    raw_data = _extract_raw_data(result)
    task_id = raw_data.get("task_id") if raw_data else None
    if not task_id:
        raise RuntimeError("task_create 결과에 task_id가 없습니다")
        
    audit.log(
        AuditAction.TASK_CREATED,
        details={
            "task_id": task_id,
            "task_type": task_type,
            "requires_approval": decision.requires_approval
        }
    )
        
    return {
        "task_id": task_id,
        "email_id": email_id,
        "task_type": task_type,
        "title": action_item.get("title"),
        "description": action_item.get("description", ""),
        "requires_approval": decision.requires_approval
    }


async def _generate_reply_drafts(
    tasks_created: list,
    audit: AuditLogger,
) -> None:
    """각 reply_email 태스크의 초안을 Slack 여부와 무관하게 한 번 준비합니다.

    초안 실패는 새 메일 이벤트 전체를 실패시키지 않습니다. 해당 태스크는
    ``draft_failed``로 남아 로컬 CLI에서 안전하게 재시도할 수 있습니다.
    """
    from mail_operator.handlers.approval import _create_reply_draft

    for task in tasks_created:
        if task.get("task_type") != "reply_email":
            continue

        if task.get("send_state") in {
            "draft_generated",
            "pending_send_confirmation",
            "sending",
            "sent",
        }:
            continue

        task_id = task.get("task_id")
        try:
            payload = await _create_reply_draft(task)
        except Exception:
            task["send_state"] = "draft_failed"
            if task_id:
                # _create_reply_draft 내부 준비 단계에서 예상 밖 오류가 발생해도
                # provider 원문 없이 재시도 가능한 안전 상태를 남깁니다.
                from db import task_store
                safe_payload = {
                    "recipient": "",
                    "subject": "",
                    "body": "",
                    "error": "draft_generation_failed",
                }
                try:
                    task_store.update_send_payload(
                        int(task_id), json.dumps(safe_payload, ensure_ascii=False)
                    )
                    task_store.update_send_state(int(task_id), "draft_failed")
                except Exception:
                    logger.error(
                        "reply_email 초안 실패 상태 저장 실패: task_id=%s",
                        task_id,
                    )
            logger.warning(
                "reply_email 초안 생성 실패: task_id=%s (로컬 CLI에서 재시도 가능)",
                task_id,
            )
            continue

        if _is_valid_send_payload(payload):
            if task_id:
                from db import task_store

                task_store.update_send_payload(
                    int(task_id), json.dumps(payload, ensure_ascii=False)
                )
                task_store.update_send_state(int(task_id), "draft_generated")
            task["send_payload"] = payload
            task["send_state"] = "draft_generated"
            audit.log_tool_call("reply_draft_generated", {"task_id": task_id})
        else:
            task["send_state"] = "draft_failed"
            logger.warning(
                "reply_email 초안 검증 실패: task_id=%s (로컬 CLI에서 재시도 가능)",
                task_id,
            )


async def _send_slack_notification(
    mcp_client: Any,
    email_data: dict,
    analysis: dict,
    tasks_created: list,
    audit: AuditLogger
) -> None:
    """Slack 알림 전송"""
    try:
        # 알림 메시지 구성
        urgency_emoji = {
            "urgent": "🔴",
            "normal": "🟡",
            "low": "🟢"
        }
        
        emoji = urgency_emoji.get(analysis.get("urgency", "normal"), "🟡")
        
        message = f"{emoji} 새 이메일\n"
        message += f"• 발신자: {email_data.get('sender', '')} <{email_data.get('sender_email', '')}>\n"
        message += f"• 제목: {email_data.get('subject', '')}\n"
        message += f"• 요약: {analysis.get('summary', '요약 없음')}\n"
        
        if analysis.get("requires_response"):
            message += "• ⚠️ 답장 필요\n"
        
        if tasks_created:
            approval_needed = [t for t in tasks_created if t.get("requires_approval")]
            if approval_needed:
                message += f"\n🔐 승인 필요 작업: {len(approval_needed)}개"
        
        await mcp_client.call_tool(
            "slack_notify",
            {"message": message}
        )
        
        audit.log_tool_call("slack_notify", {"message_length": len(message)})

        for task in tasks_created:
            if not task.get("requires_approval"):
                continue
            task_id = task.get("task_id")
            if not task_id:
                logger.warning("승인 요청을 보낼 수 없습니다: task_id 없음")
                continue
            task_type = task.get("task_type")
            task_message = (
                f"• 메일: {email_data.get('subject', '')}\n"
                f"• 작업: {task.get('title') or task_type}\n"
                f"• 유형: {task_type}\n"
                f"• 설명: {task.get('description') or '설명 없음'}"
            )
            if task_type == "reply_email":
                # 초안 생성은 Slack 알림 전에 이미 끝났습니다. 여기서는 저장된
                # draft_generated payload만 사용해 중복 LLM 호출/덮어쓰기를 막습니다.
                send_payload = task.get("send_payload")
                send_state = task.get("send_state")
                if send_state != "draft_generated" or not _is_valid_send_payload(send_payload):
                    logger.warning(
                        "reply_email 초안이 준비되지 않아 버튼 발행 중단: task_id=%s",
                        task_id,
                    )
                    continue

                # Step 1: reply_send 버튼 발행
                reply_message = "✉️ 답장 초안 요청\n" + task_message
                await mcp_client.call_tool(
                    "slack_notify_with_send_button",
                    {
                        "message": reply_message,
                        "task_id": task_id,
                    }
                )

                # 버튼 발행은 승인 자체가 아닙니다. send_state는 modal 제출이
                # 서버 권위로 검증될 때만 pending_send_confirmation으로 바뀝니다.
                audit.log_tool_call(
                    "slack_notify_with_send_button",
                    {"task_id": task_id}
                )
            else:
                approval_message = "🔐 승인 필요 작업\n" + task_message
                await mcp_client.call_tool(
                    "slack_notify_with_approval",
                    {
                        "message": approval_message,
                        "task_id": task_id
                    }
                )
                audit.log_tool_call(
                    "slack_notify_with_approval",
                    {"task_id": task_id}
                )
        
    except Exception as e:
        logger.error(f"Slack 알림 실패: {e}")


async def _mark_processed(mcp_client: Any, email_id: str) -> None:
    """이메일 처리 완료 표시"""
    result = await mcp_client.call_tool(
        "email_db_mark_processed",
        {"email_id": email_id}
    )
    _ensure_tool_success(result, "email_db_mark_processed")


def _normalize_action_item(action_item: dict) -> dict:
    """LLM alias를 policy/executor가 이해하는 canonical task_type으로 변환합니다."""
    normalized = dict(action_item)
    task_type = normalized.get("type", "unknown")
    normalized["type"] = ACTION_TYPE_ALIASES.get(task_type, task_type)
    return normalized


def _extract_raw_data(result: str) -> Optional[dict]:
    """MCP-style 결과 문자열에서 [RAW_DATA] JSON 객체를 추출합니다."""
    if not result or "[RAW_DATA]" not in result:
        return None
    try:
        raw_start = result.index("[RAW_DATA]") + len("[RAW_DATA]")
        return json.loads(result[raw_start:].strip())
    except Exception as e:
        logger.warning(f"RAW_DATA 파싱 실패: {e}")
        return None


def _ensure_tool_success(result: Any, operation: str) -> None:
    """문자열 기반 내부 tool contract의 명시적 실패를 예외로 승격합니다."""
    if result is None:
        raise RuntimeError(f"{operation} 결과가 없습니다")
    if isinstance(result, str) and result.lstrip().startswith(("❌", "⚠️")):
        raise RuntimeError(f"{operation} 실패: {result.strip()}")


def _is_valid_send_payload(payload: Optional[dict]) -> bool:
    """Slack send button can be published only with a concrete recipient and body."""
    if not payload:
        return False
    return bool((payload.get("recipient") or "").strip() and (payload.get("body") or ""))
