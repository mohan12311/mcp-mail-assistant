"""Operator-owned processing for durable Slack conversation requests."""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from db import email_store, preference_store, slack_conversation_store, task_store
from db.connection import get_connection
from db.task_state import TaskStateError
from mail_operator.audit import AuditAction, AuditLogger
from mail_operator.cli_runner import parse_llm_json, run_llm
from mail_operator.prompts import (
    build_preference_proposal_prompt,
    build_reply_rewrite_prompt,
)
from mail_operator.safety import detect_prompt_injection
from mail_operator.schemas import PreferenceProposal, ReplyDraft
from mail_operator.slack_authorization import require_authorized_user


_TASK_PATTERNS = (
    re.compile(r"(?:task|태스크|작업|초안)\s*#?\s*(\d+)", re.IGNORECASE),
    re.compile(r"#(\d+)"),
    re.compile(r"\b(\d+)\s*번"),
)
_REWRITE_WORDS = re.compile(
    r"재작성|다시\s*써|고쳐|수정|정중|간결|짧게|길게|톤|부드럽|rewrite",
    re.IGNORECASE,
)
_SNOOZE_WORDS = re.compile(r"나중|미뤄|snooze|내일|\d+\s*(?:시간|일)\s*후", re.IGNORECASE)
_COMPLETE_WORDS = re.compile(r"완료|끝냈|처리했|done|complete", re.IGNORECASE)
_DRAFT_WORDS = re.compile(r"초안|draft", re.IGNORECASE)
_ACTION_WORDS = re.compile(r"할\s*일|해야\s*할|액션|todo|tasks?", re.IGNORECASE)
_TODAY_WORDS = re.compile(r"오늘", re.IGNORECASE)
_THIS_WEEK_WORDS = re.compile(r"이번\s*주(?:\s*중)?|금주(?:\s*중)?", re.IGNORECASE)
_SUMMARY_WORDS = re.compile(
    r"받은\s*편지함|인박스|inbox|메일\s*요약|정기\s*요약|오늘\s*메일|요약",
    re.IGNORECASE,
)
_PREFERENCE_WORDS = re.compile(r"규칙|선호|설정|처리|답장|회신|말투|문체|톤", re.IGNORECASE)
_PREFERENCE_FUTURE_WORDS = re.compile(r"앞으로|항상|향후|계속|이후부터", re.IGNORECASE)
_PREFERENCE_CONFIRM_WORDS = re.compile(r"적용|활성화|등록|사용해", re.IGNORECASE)
_PREFERENCE_DISABLE_WORDS = re.compile(r"해제|비활성|삭제|취소|그만", re.IGNORECASE)
_PREFERENCE_LIST_WORDS = re.compile(r"규칙\s*(?:목록|리스트)|활성\s*규칙|내\s*규칙", re.IGNORECASE)
_PREFERENCE_REFERENCE = re.compile(r"(?:규칙|preference)\s*#?\s*(\d+)", re.IGNORECASE)
_PROCESSING_EXCLUSION_WORDS = re.compile(
    r"처리\s*(?:하지|안)|무시|제외|건너|알림\s*(?:하지|안)", re.IGNORECASE
)
_REPLY_STYLE_WORDS = re.compile(
    r"답장|회신|말투|문체|정중|간결|짧게|길게|톤", re.IGNORECASE
)


@dataclass(frozen=True)
class ConversationCommand:
    kind: str
    task_id: int | None = None
    instruction: str | None = None
    scope: str | None = None
    preference_id: int | None = None


def _strip_bot_mentions(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text, flags=re.IGNORECASE).strip()


def _extract_task_id(text: str) -> int | None:
    for pattern in _TASK_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def _extract_preference_id(text: str) -> int | None:
    match = _PREFERENCE_REFERENCE.search(text)
    return int(match.group(1)) if match else None


def parse_conversation_command(
    text: str,
    *,
    active_task_id: int | None = None,
) -> ConversationCommand:
    """Classify a bounded command without delegating authority to an LLM."""
    normalized = _strip_bot_mentions(text)
    preference_id = _extract_preference_id(normalized)
    if preference_id and _PREFERENCE_DISABLE_WORDS.search(normalized):
        return ConversationCommand("preference_disable", preference_id=preference_id)
    if (
        (preference_id or not _PREFERENCE_FUTURE_WORDS.search(normalized))
        and _PREFERENCE_WORDS.search(normalized)
        and _PREFERENCE_CONFIRM_WORDS.search(normalized)
    ):
        return ConversationCommand("preference_confirm", preference_id=preference_id)
    if _PREFERENCE_LIST_WORDS.search(normalized):
        return ConversationCommand("preference_list")
    if (
        _PREFERENCE_FUTURE_WORDS.search(normalized)
        and (
            _PROCESSING_EXCLUSION_WORDS.search(normalized)
            or _REPLY_STYLE_WORDS.search(normalized)
        )
    ):
        return ConversationCommand(
            "preference_propose",
            task_id=_extract_task_id(normalized) or active_task_id,
        )
    task_id = _extract_task_id(normalized) or active_task_id
    if _SNOOZE_WORDS.search(normalized):
        return ConversationCommand("snooze", task_id=task_id)
    if _COMPLETE_WORDS.search(normalized):
        return ConversationCommand("complete", task_id=task_id)
    if _DRAFT_WORDS.search(normalized) and _REWRITE_WORDS.search(normalized):
        return ConversationCommand("rewrite", task_id=task_id, instruction=normalized)
    if _DRAFT_WORDS.search(normalized):
        return ConversationCommand("draft", task_id=task_id)
    if _ACTION_WORDS.search(normalized):
        scope = None
        if _THIS_WEEK_WORDS.search(normalized):
            scope = "week"
        elif _TODAY_WORDS.search(normalized):
            scope = "today"
        return ConversationCommand("actions", scope=scope)
    if _SUMMARY_WORDS.search(normalized):
        return ConversationCommand("summary")
    if task_id and _REWRITE_WORDS.search(normalized):
        return ConversationCommand("rewrite", task_id=task_id, instruction=normalized)
    if task_id:
        return ConversationCommand("detail", task_id=task_id)
    return ConversationCommand("help")


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def _code_block(value: object, *, limit: int = 10000) -> str:
    text = str(value or "")
    truncated = len(text) > limit
    text = text[:limit].replace("```", "``\u200b`")
    if truncated:
        text += "\n…(Slack 표시 한도에 맞춰 생략)"
    return f"```{_escape(text)}```"


def _help_text() -> str:
    return (
        "사용 가능한 요청\n"
        "• `받은편지함 요약` / `정기 요약`\n"
        "• `오늘 해야 할 일` / `이번 주 해야 할 일` / `해야 할 액션 전체`\n"
        "• `#14에 대해 알려줘`\n"
        "• `초안 12 보여줘`\n"
        "• `12번 초안을 더 정중하고 짧게 다시 써줘`\n"
        "• `12번 내일까지 미뤄줘` / `12번 완료 처리`\n\n"
        "• `앞으로 이 발신자의 메일은 처리하지 마세요`\n"
        "• `앞으로 이 발신자에게는 더 정중하고 짧게 답장해 주세요`\n"
        "• `규칙 #3 적용해줘` / `내 규칙 목록` / `규칙 #3 해제`\n\n"
        "답장 발송은 초안 메시지의 최종 확인 버튼과 확인 모달을 거쳐야 하며, "
        "Slack 텍스트만으로는 승인되지 않습니다."
    )


def _preference_scope_text(rule: dict[str, Any]) -> str:
    labels = {
        "default": "모든 발신자",
        "sender_email": "발신자 이메일",
        "sender_domain": "발신 도메인",
        "sender_name": "발신자 이름",
        "subject_contains": "제목 포함 문구",
    }
    label = labels.get(str(rule.get("scope_type")), str(rule.get("scope_type")))
    if rule.get("scope_type") == "default":
        return label
    return f"{label} `{_escape(rule.get('scope_value'))}`"


def _preference_rule_text(rule: dict[str, Any]) -> str:
    if rule.get("rule_kind") == "processing_exclusion":
        effect = "자동 분석·액션·초안·알림 생략 (메일 저장·검색 유지)"
    else:
        effect = f"답장 초안: {_escape(rule.get('instruction'))}"
    return (
        f"규칙 `#{rule['id']}` v{rule.get('version', 1)} — "
        f"{_preference_scope_text(rule)} · {effect}"
    )


def _preference_list_text(user_id: str) -> str:
    rules = preference_store.list_rules(user_id=user_id)
    if not rules:
        return "현재 활성화된 사용자별 이메일 규칙이 없습니다."
    return "⚙️ *활성 사용자 규칙*\n" + "\n".join(
        f"• {_preference_rule_text(rule)}" for rule in rules
    )


def _summary_text(limit: int = 5) -> str:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, sender, subject, received_at, body_summary,
                   priority_level, urgency, has_attachments, requires_response
            FROM emails
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    if not rows:
        return "📭 저장된 받은편지함 요약이 없습니다."
    lines = [f"📬 최근 받은편지함 요약 ({len(rows)}건)"]
    for row in rows:
        flags = []
        if row["requires_response"]:
            flags.append("답장 필요")
        if row["has_attachments"]:
            flags.append("첨부 있음")
        suffix = f" · {', '.join(flags)}" if flags else ""
        lines.extend(
            [
                f"\n• {_escape(row['sender'])} — *{_escape(row['subject'])}*",
                f"  {_escape(row['body_summary'] or '아직 요약되지 않음')}",
                f"  우선순위: {_escape(row['priority_level'] or row['urgency'])}{suffix}",
            ]
        )
    lines.append("\n원문 본문과 첨부 추출문은 Slack에 표시하지 않았습니다.")
    return "\n".join(lines)


@dataclass(frozen=True)
class _ParsedDeadline:
    value: datetime
    date_only: bool


_WEEKDAYS_KO = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")


def _local_now(now: datetime | None = None) -> datetime:
    tz = _conversation_timezone()
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    return current.astimezone(tz)


def _parse_deadline(raw: object) -> _ParsedDeadline | None:
    text_value = str(raw or "").strip()
    if not text_value:
        return None
    tz = _conversation_timezone()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text_value):
            value = datetime.combine(date.fromisoformat(text_value), time.max, tzinfo=tz)
            return _ParsedDeadline(value=value, date_only=True)
        value = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return _ParsedDeadline(value=value.astimezone(tz), date_only=False)
    except ValueError:
        return None


def _deadline_status(raw: object, *, now: datetime) -> tuple[str, str]:
    parsed = _parse_deadline(raw)
    if parsed is None:
        return "undated", "기한 없음"

    value = parsed.value
    absolute = value.strftime("%Y-%m-%d")
    if not parsed.date_only:
        absolute += value.strftime(" %H:%M")

    if value < now:
        days = (now.date() - value.date()).days
        if days > 0:
            return "overdue", f"{absolute} · 기한 초과 {days}일"
        return "overdue", f"{absolute} · 오늘 기한 경과"
    if value.date() == now.date():
        return "today", f"{absolute} · 오늘"

    week_end = now.date() + timedelta(days=6 - now.weekday())
    if value.date() <= week_end:
        weekday = _WEEKDAYS_KO[value.weekday()]
        return "week", f"{absolute} · 이번 주 {weekday}"
    return "future", absolute


def _clean_action_title(raw: object) -> str:
    title = str(raw or "")
    return re.sub(r"\s*\(\s*오늘[^)]*\)\s*$", "", title).strip()


def _format_action(task: dict[str, Any], *, now: datetime) -> str:
    _status, deadline = _deadline_status(task.get("deadline"), now=now)
    draft = " · 초안 준비됨" if task.get("send_state") == "draft_generated" else ""
    return (
        f"• `#{task['id']}` [{_escape(task.get('priority'))}] "
        f"{_escape(_clean_action_title(task.get('title')))} · {_escape(deadline)}{draft}"
    )


def _actions_text(
    limit: int = 10,
    *,
    scope: str | None = None,
    now: datetime | None = None,
) -> str:
    current = _local_now(now)
    tasks = task_store.list_actionable_tasks(limit=max(200, limit), now=current)
    if not tasks:
        return f"✅ {current.date().isoformat()} 기준, 표시할 해야 할 액션이 없습니다."

    classified = [
        (task, _deadline_status(task.get("deadline"), now=current)[0])
        for task in tasks
    ]
    if scope == "today":
        selected = [(task, status) for task, status in classified if status in {"overdue", "today"}]
        range_label = "오늘"
    elif scope == "week":
        selected = [
            (task, status)
            for task, status in classified
            if status in {"overdue", "today", "week"}
        ]
        range_label = "오늘·이번 주"
    else:
        selected = classified
        range_label = "전체"

    undated_count = sum(status == "undated" for _task, status in classified)
    visible = selected[:limit]
    lines = [
        f"✅ *{current.date().isoformat()} 기준 해야 할 액션*",
        f"_시간대: {_escape(str(current.tzinfo))} · 범위: {range_label}_",
    ]

    if not visible:
        lines.append(f"\n{range_label} 범위에 기한이 있는 미완료 항목은 없습니다.")
    else:
        headings = {
            "overdue": "⚠️ 지난 기한",
            "today": "📌 오늘",
            "week": "📅 이번 주",
            "future": "🗓️ 이후",
            "undated": "📂 기한 없음",
        }
        for status in ("overdue", "today", "week", "future", "undated"):
            group = [task for task, item_status in visible if item_status == status]
            if not group:
                continue
            lines.append(f"\n*{headings[status]} ({len(group)}건)*")
            lines.extend(_format_action(task, now=current) for task in group)

    omitted = len(selected) - len(visible)
    if omitted > 0:
        lines.append(f"\n표시 한도로 {omitted}건을 생략했습니다.")
    if scope is not None and undated_count:
        lines.append(
            f"\n기한 없는 미완료 {undated_count}건은 범위에서 제외했습니다. "
            "`해야 할 액션 전체`라고 물으면 함께 보여드립니다."
        )
    return "\n".join(lines)


def _task_detail_text(task_id: int, *, now: datetime | None = None) -> str:
    task = task_store.get_task(task_id)
    if task is None:
        raise TaskStateError("task not found")
    current = _local_now(now)
    deadline_status, deadline_label = _deadline_status(task.get("deadline"), now=current)
    status_labels = {
        "pending": "미완료",
        "in_progress": "진행 중",
        "completed": "완료",
        "failed": "실패",
        "cancelled": "취소",
    }
    lines = [
        f"🔎 *액션 `#{task_id}` — {_escape(_clean_action_title(task.get('title')))}*",
        f"• 현재 상태: {_escape(status_labels.get(task.get('status'), task.get('status')))}",
        f"• 우선순위: {_escape(task.get('priority'))}",
        f"• 기한: {_escape(deadline_label)}",
    ]
    if task.get("description"):
        lines.append(f"• 해야 할 일: {_escape(task['description'])}")

    email = email_store.get_email(str(task.get("email_id"))) if task.get("email_id") else None
    if email:
        lines.append(
            f"• 관련 메일: {_escape(email.get('sender'))} — *{_escape(email.get('subject'))}*"
        )
        if email.get("received_at"):
            lines.append(f"• 수신: {_escape(email['received_at'])}")
        if email.get("body_summary"):
            summary = str(email["body_summary"])
            if len(summary) > 800:
                summary = summary[:800] + "…"
            lines.append(f"• 메일 요약: {_escape(summary)}")

    if task.get("status") == "pending" and deadline_status == "overdue":
        lines.append("\n*현재 판단*")
        description = str(task.get("description") or "")
        if task.get("task_type") == "reply_email" and re.search(r"경우|시\s+불필요|조건", description):
            lines.append(
                "이 항목은 설명상 *조건부 연락*입니다. 오늘 기준으로 기한이 지났으므로, "
                "원래 조건이 발생했는지와 이미 처리했는지를 먼저 확인하는 것이 좋습니다."
            )
            lines.append(
                f"참석했거나 이미 연락했다면 `#{task_id} 완료 처리`; "
                "결석했고 미연락이라면 지금은 '사전 연락'보다 늦은 사과와 상황 공유가 적절합니다."
            )
        else:
            lines.append(
                f"아직 미완료이지만 기한은 지났습니다. 이미 처리했다면 `#{task_id} 완료 처리`라고 답해 주세요."
            )
    if task.get("task_type") == "reply_email":
        draft_note = (
            "준비됨"
            if task.get("send_state") in {"draft_generated", "pending_send_confirmation"}
            else "없음"
        )
        lines.append(f"\n• 저장된 답장 초안: {draft_note}")
    lines.append("\n원문 메일 본문과 첨부 추출문은 Slack에 표시하지 않았습니다.")
    return "\n".join(lines)


def _load_draft_payload(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("send_payload")
    if not isinstance(raw, str):
        raise TaskStateError("reply draft payload is missing")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskStateError("reply draft payload is invalid") from exc
    if not all(isinstance(payload.get(key), str) and payload.get(key) for key in ("recipient", "subject", "body")):
        raise TaskStateError("reply draft is incomplete")
    return payload


def _draft_text(task: dict[str, Any], payload: dict[str, Any], *, rewritten: bool = False) -> str:
    label = "재작성된 답장 초안" if rewritten else "답장 초안"
    return (
        f"✉️ *{label}* `#{task['id']}`\n"
        f"• 수신자: {_escape(payload['recipient'])}\n"
        f"• 제목: {_escape(payload['subject'])}\n"
        f"• 본문:\n{_code_block(payload['body'], limit=1800)}\n\n"
        "원문 메일 본문과 첨부 추출문은 표시하지 않았습니다. "
        "아래 버튼은 DB의 최신 초안 hash·approval version·TTL을 다시 검증한 뒤 확인 창만 엽니다."
    )


def _conversation_timezone() -> ZoneInfo:
    name = os.environ.get("SLACK_CONVERSATION_TIMEZONE", "Asia/Tokyo")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("SLACK_CONVERSATION_TIMEZONE is invalid") from exc


def _snooze_until(text: str, *, now: datetime | None = None) -> datetime:
    tz = _conversation_timezone()
    current = now or datetime.now(tz)
    current = current.astimezone(tz)
    hours = re.search(r"(\d+)\s*시간\s*후", text)
    if hours:
        value = max(1, min(int(hours.group(1)), 168))
        return current + timedelta(hours=value)
    days = re.search(r"(\d+)\s*일\s*후", text)
    if days:
        value = max(1, min(int(days.group(1)), 30))
        return current + timedelta(days=value)
    if "내일" in text:
        tomorrow = current.date() + timedelta(days=1)
        return datetime.combine(tomorrow, datetime.min.time(), tzinfo=tz).replace(hour=9)
    return current + timedelta(hours=24)


async def _rewrite_draft(task_id: int, instruction: str, interaction_id: str, audit: AuditLogger) -> dict[str, Any]:
    if detect_prompt_injection(instruction):
        raise ValueError("재작성 지시에서 시스템 지시 변경 패턴이 감지되어 처리하지 않았습니다")
    task = task_store.get_task(task_id)
    if task is None:
        raise TaskStateError("task not found")
    if task.get("last_slack_interaction_id") == interaction_id:
        return task
    payload = _load_draft_payload(task)
    prompt = build_reply_rewrite_prompt(
        current_subject=payload["subject"],
        current_body=payload["body"],
        instruction=instruction,
    )
    result = await asyncio.to_thread(
        run_llm,
        prompt,
        60,
        ReplyDraft.model_json_schema(),
    )
    if not result["success"]:
        raise RuntimeError("답장 초안 재작성 실패")
    try:
        rewritten = ReplyDraft.model_validate(
            parse_llm_json(result["content"])
        ).model_dump(mode="json")
    except Exception as exc:
        raise ValueError("재작성된 답장 초안 검증 실패") from exc

    from core.email_headers import UnsafeEmailHeaderError, validate_header_value

    try:
        rewritten["subject"] = validate_header_value("Subject", rewritten["subject"])
    except UnsafeEmailHeaderError as exc:
        raise ValueError("재작성된 제목에 허용되지 않는 헤더 문자가 있습니다") from exc
    task_store.replace_reply_draft(
        task_id,
        subject=rewritten["subject"],
        body=rewritten["body"],
        interaction_id=interaction_id,
    )
    audit.log_llm_interaction(
        prompt_summary="Slack 답장 초안 재작성",
        response_summary="validated reply draft",
        model=result["provider"],
    )
    stored = task_store.get_task(task_id)
    if stored is None:
        raise TaskStateError("task not found after rewrite")
    return stored


async def _interpret_preference_proposal(
    *,
    interaction: dict[str, Any],
    task_id: int | None,
    audit: AuditLogger,
) -> str:
    """Create an inert rule proposal from the authorized Slack request only."""
    request_text = str(interaction["request_text"])
    if not _PREFERENCE_FUTURE_WORDS.search(request_text):
        raise ValueError("장기 규칙은 `앞으로` 또는 `항상`처럼 적용 기간을 명시해 주세요")
    if not (
        _PROCESSING_EXCLUSION_WORDS.search(request_text)
        or _REPLY_STYLE_WORDS.search(request_text)
    ):
        raise ValueError("처리 제외 또는 답장 문체 규칙을 구체적으로 요청해 주세요")

    prompt = build_preference_proposal_prompt(
        request_text=request_text,
        active_task_id=task_id,
    )
    result = await asyncio.to_thread(
        run_llm,
        prompt,
        60,
        PreferenceProposal.model_json_schema(),
    )
    if not result["success"]:
        raise RuntimeError("사용자 규칙 제안을 구조화하지 못했습니다")
    try:
        proposal = PreferenceProposal.model_validate(
            parse_llm_json(result["content"])
        )
    except Exception as exc:
        raise ValueError("사용자 규칙 제안 검증에 실패했습니다") from exc
    if proposal.mode == "clarify":
        return str(proposal.response)

    if (
        proposal.rule_kind == "processing_exclusion"
        and not _PROCESSING_EXCLUSION_WORDS.search(request_text)
    ) or (
        proposal.rule_kind == "reply_style"
        and not _REPLY_STYLE_WORDS.search(request_text)
    ):
        raise ValueError("요청 내용과 제안된 사용자 규칙 종류가 일치하지 않습니다")

    scope_type = str(proposal.scope_type)
    scope_value = proposal.scope_value
    normalized_request = " ".join(request_text.split()).casefold()
    if scope_type == "default" and not re.search(
        r"모든|전체|전부|누구에게나|모든\s*발신자", request_text, re.IGNORECASE
    ):
        raise ValueError("모든 메일에 적용할지, 특정 발신자 범위인지 명시해 주세요")
    if scope_type in {"sender_email", "sender_domain", "sender_name", "subject_contains"}:
        normalized_scope = " ".join(str(scope_value or "").split()).casefold()
        if not normalized_scope or normalized_scope not in normalized_request:
            raise ValueError("요청에 명시된 발신자·도메인·이름·제목 범위를 그대로 지정해 주세요")
    if scope_type in {"current_sender", "current_domain"}:
        reference_pattern = (
            r"이\s*(?:회사|도메인)"
            if scope_type == "current_domain"
            else r"이\s*(?:발신자|사람|분)"
        )
        if not re.search(reference_pattern, request_text, re.IGNORECASE):
            raise ValueError("현재 메일을 기준으로 삼는 범위를 요청에서 명시해 주세요")
        if task_id is None:
            raise ValueError("`이 발신자`의 기준이 될 task를 같은 thread에서 먼저 지정해 주세요")
        task = task_store.get_task(task_id)
        if task is None or not task.get("email_id"):
            raise ValueError("규칙 기준이 될 task 또는 관련 메일을 찾을 수 없습니다")
        email = email_store.get_email(str(task["email_id"]))
        if email is None:
            raise ValueError("규칙 기준이 될 관련 메일을 찾을 수 없습니다")
        sender_email = str(email.get("sender_email") or "").strip()
        if scope_type == "current_domain":
            if "@" not in sender_email:
                raise ValueError("관련 메일에서 발신 도메인을 확인할 수 없습니다")
            scope_type = "sender_domain"
            scope_value = sender_email.rsplit("@", 1)[1]
        elif sender_email:
            scope_type = "sender_email"
            scope_value = sender_email
        else:
            scope_type = "sender_name"
            scope_value = str(email.get("sender") or "").strip()

    instruction = proposal.instruction
    if instruction and detect_prompt_injection(instruction):
        raise ValueError("답장 문체 규칙에서 시스템 지시 변경 패턴이 감지되었습니다")
    rule = preference_store.propose_rule(
        user_id=interaction["actor_id"],
        channel_id=interaction["channel_id"],
        thread_ts=interaction["thread_ts"],
        source_interaction_id=interaction["id"],
        rule_kind=str(proposal.rule_kind),
        scope_type=scope_type,
        scope_value=scope_value,
        instruction=instruction,
    )
    audit.log_llm_interaction(
        prompt_summary="Slack 사용자별 이메일 규칙 제안 구조화",
        response_summary="validated inert preference proposal",
        model=result["provider"],
    )
    return (
        f"⚙️ {_preference_rule_text(rule)}\n\n"
        "아직 적용되지 않았습니다. 같은 대화에서 "
        f"`규칙 #{rule['id']} 적용해줘`라고 별도로 확인하면 활성화합니다."
    )


async def _deliver_response(interaction: dict[str, Any], mcp_client: Any) -> None:
    arguments = {
        "message": interaction["response_text"],
        "channel": interaction["channel_id"],
        "thread_ts": interaction["thread_ts"],
        "idempotency_key": f"slack-conversation:{interaction['id']}:response",
    }
    tool = "slack_notify"
    if interaction.get("response_kind") == "draft_with_confirmation":
        tool = "slack_notify_with_send_button"
        arguments["task_id"] = int(interaction["response_task_id"])
    result = await mcp_client.call_tool(tool, arguments)
    if result is None or (isinstance(result, str) and result.lstrip().startswith(("❌", "⚠️"))):
        raise RuntimeError("Slack response delivery was not accepted")


async def handle_slack_conversation(
    event: dict,
    mcp_client: Any,
    audit: AuditLogger,
    config: Any,
) -> Tuple[bool, Optional[str]]:
    """Validate one persisted interaction, execute a bounded command, and reply."""
    event_id = event.get("id")
    audit.set_event_context(event_id)
    try:
        interaction = slack_conversation_store.load_authoritative_interaction(str(event_id))
        require_authorized_user(interaction["actor_id"])

        if interaction["status"] == "processed":
            await _deliver_response(interaction, mcp_client)
            return True, None

        active_task_id = slack_conversation_store.get_active_task(
            interaction["channel_id"], interaction["thread_ts"]
        )
        command = parse_conversation_command(
            interaction["request_text"], active_task_id=active_task_id
        )
        audit.log(
            AuditAction.SLACK_REQUEST,
            details={
                "interaction_id": interaction["id"],
                "command": command.kind,
                "task_id": command.task_id,
                "scope": command.scope,
                "preference_id": command.preference_id,
                "request_sha256_prefix": interaction["request_sha256"][:12],
            },
            event_id=event_id,
            task_id=str(command.task_id) if command.task_id else None,
        )

        response_kind = "message"
        response_task_id = None
        if command.kind == "summary":
            response_text = _summary_text()
        elif command.kind == "preference_propose":
            response_text = await _interpret_preference_proposal(
                interaction=interaction,
                task_id=command.task_id,
                audit=audit,
            )
        elif command.kind == "preference_confirm":
            if not _PREFERENCE_CONFIRM_WORDS.search(interaction["request_text"]):
                raise ValueError("규칙 적용 여부를 명시적으로 확인해 주세요")
            preference_id = command.preference_id
            if preference_id is None:
                pending = preference_store.get_pending_rule(
                    user_id=interaction["actor_id"],
                    channel_id=interaction["channel_id"],
                    thread_ts=interaction["thread_ts"],
                )
                if pending is None:
                    raise ValueError("이 대화에 확인 대기 중인 사용자 규칙이 없습니다")
                preference_id = int(pending["id"])
            rule = preference_store.confirm_rule(
                preference_id,
                user_id=interaction["actor_id"],
                channel_id=interaction["channel_id"],
                thread_ts=interaction["thread_ts"],
                confirmation_interaction_id=interaction["id"],
            )
            response_text = f"✅ 사용자 규칙을 활성화했습니다.\n• {_preference_rule_text(rule)}"
        elif command.kind == "preference_list":
            response_text = _preference_list_text(interaction["actor_id"])
        elif command.kind == "preference_disable":
            if command.preference_id is None:
                raise ValueError("해제할 규칙 번호를 지정해 주세요")
            rule = preference_store.disable_rule(
                command.preference_id,
                user_id=interaction["actor_id"],
                interaction_id=interaction["id"],
            )
            response_text = f"⏹️ 사용자 규칙 `#{rule['id']}`을 비활성화했습니다."
        elif command.kind == "actions":
            response_text = _actions_text(scope=command.scope)
        elif command.kind == "detail":
            if command.task_id is None:
                raise ValueError("상세히 볼 task 번호를 지정하세요")
            response_text = _task_detail_text(command.task_id)
            slack_conversation_store.set_active_task(
                interaction["channel_id"], interaction["thread_ts"], command.task_id
            )
        elif command.kind == "draft":
            if command.task_id is None:
                raise ValueError("초안을 보려면 `초안 12 보여줘`처럼 task 번호를 지정하세요")
            task = task_store.refresh_reply_review(command.task_id)
            payload = _load_draft_payload(task)
            response_text = _draft_text(task, payload)
            if task.get("approval_status") == "approved":
                response_text += "\n\n이미 최종 확인되어 Operator 처리를 기다리는 중입니다."
            else:
                response_kind = "draft_with_confirmation"
                response_task_id = command.task_id
            slack_conversation_store.set_active_task(
                interaction["channel_id"], interaction["thread_ts"], command.task_id
            )
        elif command.kind == "rewrite":
            if command.task_id is None or not command.instruction:
                raise ValueError("재작성할 task 번호와 지시를 함께 입력하세요")
            task = await _rewrite_draft(
                command.task_id,
                command.instruction,
                interaction["id"],
                audit,
            )
            payload = _load_draft_payload(task)
            response_text = _draft_text(task, payload, rewritten=True)
            response_kind = "draft_with_confirmation"
            response_task_id = command.task_id
            slack_conversation_store.set_active_task(
                interaction["channel_id"], interaction["thread_ts"], command.task_id
            )
        elif command.kind == "snooze":
            if command.task_id is None:
                raise ValueError("미룰 task 번호를 지정하세요")
            until = _snooze_until(interaction["request_text"])
            task = task_store.snooze_task(
                command.task_id,
                until,
                interaction_id=interaction["id"],
            )
            stored_until = datetime.fromisoformat(
                str(task["snoozed_until"]).replace("Z", "+00:00")
            )
            if stored_until.tzinfo is None:
                stored_until = stored_until.replace(tzinfo=timezone.utc)
            local_until = stored_until.astimezone(_conversation_timezone())
            approval_note = (
                " 기존 답장 승인 창은 무효화했습니다."
                if task.get("task_type") == "reply_email"
                else ""
            )
            response_text = (
                f"🕒 `#{command.task_id}`을 {local_until.strftime('%Y-%m-%d %H:%M %Z')}까지 "
                f"나중에 보기로 처리했습니다.{approval_note}"
            )
        elif command.kind == "complete":
            if command.task_id is None:
                raise ValueError("완료할 task 번호를 지정하세요")
            task_store.complete_task_without_send(
                command.task_id,
                interaction_id=interaction["id"],
            )
            response_text = (
                f"✅ `#{command.task_id}`을 완료 처리했습니다. "
                "미발송 답장 초안이 있었다면 발송 권한도 취소했습니다."
            )
        else:
            response_text = _help_text()

        interaction = slack_conversation_store.save_response(
            interaction["id"],
            response_text=response_text,
            response_kind=response_kind,
            response_task_id=response_task_id,
        )
        await _deliver_response(interaction, mcp_client)
        audit.log(
            AuditAction.SLACK_RESPONSE,
            details={
                "interaction_id": interaction["id"],
                "response_kind": response_kind,
                "response_length": len(response_text),
            },
            event_id=event_id,
            task_id=str(response_task_id) if response_task_id else None,
        )
        return True, None
    except Exception as exc:
        audit.log(
            AuditAction.EVENT_FAILED,
            event_id=event_id,
            success=False,
            error=str(exc),
        )
        return False, str(exc)
    finally:
        audit.clear_event_context()


__all__ = [
    "ConversationCommand",
    "handle_slack_conversation",
    "parse_conversation_command",
]
