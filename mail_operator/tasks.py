"""Slack 없이 pending 답장 태스크를 검토하고 최종 승인하는 로컬 CLI.

승인은 Operator용 SEND_CONFIRMED 이벤트만 큐잉한다. SMTP를 직접 호출하지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from typing import Sequence, TextIO

from db import attachment_store, task_store
from mail_operator.schemas import ReplyDraft


_APPROVABLE_SEND_STATES = frozenset(
    {"draft_generated", "pending_send_confirmation"}
)


class TaskReviewError(Exception):
    """사용자에게 원문이나 내부 오류를 노출하지 않고 보고할 수 있는 CLI 오류."""


@dataclass(frozen=True)
class ReplyReview:
    task_id: int
    recipient: str
    subject: str
    draft: str


@dataclass(frozen=True)
class AttachmentReview:
    filename: str
    content_type: str
    file_size: int
    processing_status: str
    extracted_text_length: int


def _safe_inline(value: object) -> str:
    """목록 출력이 새 줄이나 제어 문자로 위조되지 않도록 한 줄로 정규화한다."""
    raw = str(value or "")
    without_controls = "".join(
        " "
        if char.isspace()
        else ""
        if unicodedata.category(char).startswith("C")
        else char
        for char in raw
    )
    return " ".join(without_controls.split())


def _validate_recipient(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("recipient is not a string")
    recipient = value.strip()
    if (
        len(recipient) > 320
        or "\r" in recipient
        or "\n" in recipient
        or not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+", recipient)
    ):
        raise ValueError("recipient is missing or unsafe")
    return recipient


def _load_pending_reply_task(task_id: int) -> dict:
    task = task_store.get_task(task_id)
    if not task:
        raise TaskReviewError(f"태스크 {task_id}를 찾을 수 없습니다.")

    if (
        task.get("task_type") != "reply_email"
        or task.get("status") != "pending"
        or task.get("approval_status") != "pending"
    ):
        raise TaskReviewError(
            f"태스크 {task_id}는 승인 대기 중인 pending reply_email 태스크가 아닙니다."
        )
    return task


def _review_from_payload(task: dict, payload: object | None = None) -> ReplyReview:
    raw_payload = task.get("send_payload") if payload is None else payload
    if not raw_payload:
        raise TaskReviewError("검토할 초안이 없습니다. 먼저 draft 명령을 실행하세요.")

    try:
        parsed = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        if not isinstance(parsed, dict):
            raise ValueError("payload is not an object")

        recipient = _validate_recipient(parsed.get("recipient"))

        draft = ReplyDraft.model_validate(
            {"subject": parsed.get("subject"), "body": parsed.get("body")}
        )
    except Exception as exc:
        raise TaskReviewError(
            "저장된 답장 초안이 없거나 검증에 실패했습니다. draft 명령으로 다시 생성하세요."
        ) from exc

    return ReplyReview(
        task_id=int(task["id"]),
        recipient=recipient,
        subject=draft.subject,
        draft=draft.body,
    )


def _attachment_reviews(task: dict) -> list[AttachmentReview]:
    """검토에 필요한 첨부 메타데이터만 반환하고 본문·경로·오류는 숨긴다."""
    email_id = task.get("email_id")
    if not email_id:
        return []

    try:
        attachments = attachment_store.get_attachments_for_email(str(email_id))
    except Exception as exc:
        raise TaskReviewError("첨부파일 검토 정보를 불러오지 못했습니다.") from exc

    reviews = []
    for attachment in attachments:
        try:
            file_size = max(0, int(attachment.get("file_size") or 0))
        except (TypeError, ValueError):
            file_size = 0

        extracted_text = attachment.get("extracted_text")
        reviews.append(
            AttachmentReview(
                filename=_safe_inline(attachment.get("filename")) or "(unnamed)",
                content_type=(
                    _safe_inline(
                        attachment.get("detected_mime")
                        or attachment.get("file_type")
                    )
                    or "unknown"
                ),
                file_size=file_size,
                processing_status=(
                    _safe_inline(attachment.get("processing_status")) or "pending"
                ),
                extracted_text_length=len(
                    extracted_text
                    if isinstance(extracted_text, str)
                    else str(extracted_text or "")
                ),
            )
        )
    return reviews


def _print_review(
    review: ReplyReview,
    out: TextIO,
    attachments: Sequence[AttachmentReview] = (),
) -> None:
    print(f"Task ID: {review.task_id}", file=out)
    print(f"Recipient: {review.recipient}", file=out)
    print(f"Subject: {review.subject}", file=out)
    print("Draft:", file=out)
    print(review.draft, file=out)
    print(f"Attachments: {len(attachments)}", file=out)
    for index, attachment in enumerate(attachments, start=1):
        print(f"Attachment {index}:", file=out)
        print(f"  Filename: {attachment.filename}", file=out)
        print(f"  Content-Type: {attachment.content_type}", file=out)
        print(f"  File-Size: {attachment.file_size}", file=out)
        print(f"  Processing-Status: {attachment.processing_status}", file=out)
        print(
            f"  Extracted-Text-Length: {attachment.extracted_text_length}",
            file=out,
        )


def _command_list(out: TextIO) -> None:
    tasks = [
        task
        for task in task_store.get_pending_tasks()
        if task.get("task_type") == "reply_email"
        and task.get("approval_status") == "pending"
    ]
    print("ID\tAPPROVAL\tDRAFT\tTITLE", file=out)
    for task in tasks:
        draft_state = task.get("send_state") or "not_generated"
        print(
            f"{task['id']}\t{task['approval_status']}\t"
            f"{_safe_inline(draft_state)}\t{_safe_inline(task.get('title'))}",
            file=out,
        )


def _command_show(task_id: int, out: TextIO) -> None:
    task = _load_pending_reply_task(task_id)
    _print_review(_review_from_payload(task), out, _attachment_reviews(task))


def _command_draft(task_id: int, out: TextIO) -> None:
    task = _load_pending_reply_task(task_id)
    if task.get("send_state") not in {None, "draft_generated", "draft_failed"}:
        raise TaskReviewError(
            f"태스크 {task_id}의 현재 발송 상태에서는 초안을 다시 생성할 수 없습니다."
        )

    from mail_operator.handlers.approval import _create_reply_draft

    try:
        payload = asyncio.run(_create_reply_draft(task))
    except Exception as exc:
        raise TaskReviewError(
            "답장 초안 생성에 실패했습니다. 원문 메일과 LLM CLI 설정을 확인하세요."
        ) from exc

    _print_review(
        _review_from_payload(task, payload), out, _attachment_reviews(task)
    )


def _command_export(task_id: int, out: TextIO) -> None:
    task = _load_pending_reply_task(task_id)
    review = _review_from_payload(task)
    exported = asdict(review)
    exported["attachments"] = [
        asdict(attachment) for attachment in _attachment_reviews(task)
    ]
    print(json.dumps(exported, ensure_ascii=False, indent=2), file=out)


def _local_approval_source_id(task_id: int) -> str:
    return "initial"


def _command_approve(task_id: int, confirmed_task_id: int, out: TextIO) -> None:
    if confirmed_task_id != task_id:
        raise TaskReviewError(
            "승인 확인 ID가 task_id와 일치하지 않습니다. 이벤트를 생성하지 않았습니다."
        )

    task = task_store.get_task(task_id)
    if not task or task.get("task_type") != "reply_email" or task.get("status") != "pending":
        raise TaskReviewError(
            f"태스크 {task_id}는 승인 가능한 pending reply_email 태스크가 아닙니다."
        )
    _review_from_payload(task)

    send_state = task.get("send_state")
    if send_state not in _APPROVABLE_SEND_STATES:
        raise TaskReviewError(
            f"태스크 {task_id}의 현재 발송 상태에서는 최종 승인할 수 없습니다."
        )

    from db.approval_store import (
        ApprovalAuthorityError,
        LOCAL_CLI,
        create_send_confirmation,
        payload_sha256,
    )

    try:
        event_id, created = create_send_confirmation(
            task_id=task_id,
            source=LOCAL_CLI,
            action="tasks.approve",
            source_ref=_local_approval_source_id(task_id),
            expected_payload_hash=payload_sha256(task["send_payload"]),
            allowed_send_states=_APPROVABLE_SEND_STATES,
        )
    except ApprovalAuthorityError as exc:
        raise TaskReviewError(f"승인 권한이 유효하지 않습니다: {exc}") from exc
    result = "승인 이벤트 큐잉 완료" if created else "이미 큐잉된 승인 이벤트"
    print(f"{result}: task_id={task_id} event_id={event_id}", file=out)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mail_operator.tasks",
        description="Slack 없이 pending 답장 태스크의 초안을 생성·검토·최종 승인합니다.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="승인 대기 중인 pending 답장 태스크 목록")

    for command, help_text in (
        ("show", "저장된 수신자, 제목, 초안 표시"),
        ("draft", "원문 기반 일본어 답장 초안 생성(발송하지 않음)"),
        ("export", "검토용 답장 초안을 JSON으로 표준 출력"),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("task_id", type=int, help="양의 정수 태스크 ID")

    approve_parser = subparsers.add_parser(
        "approve",
        help="검토한 답장 태스크를 최종 승인하고 Operator 이벤트 큐에 등록",
    )
    approve_parser.add_argument("task_id", type=int, help="양의 정수 태스크 ID")
    approve_parser.add_argument(
        "--confirm",
        type=int,
        required=True,
        metavar="TASK_ID",
        help="최종 승인을 위해 같은 task_id를 다시 입력",
    )

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    output = out if out is not None else sys.stdout
    errors = err if err is not None else sys.stderr
    args = _build_parser().parse_args(argv)

    task_id = getattr(args, "task_id", None)
    if task_id is not None and task_id <= 0:
        print("오류: task_id는 양의 정수여야 합니다.", file=errors)
        return 2

    try:
        if args.command == "list":
            _command_list(output)
        elif args.command == "show":
            _command_show(task_id, output)
        elif args.command == "draft":
            _command_draft(task_id, output)
        elif args.command == "export":
            _command_export(task_id, output)
        elif args.command == "approve":
            _command_approve(task_id, args.confirm, output)
        else:  # argparse가 차단하지만, 새 명령 추가 시에도 fail-closed 한다.
            raise TaskReviewError("지원하지 않는 명령입니다.")
    except TaskReviewError as exc:
        print(f"오류: {exc}", file=errors)
        return 1
    except Exception:
        print("오류: 로컬 태스크 처리에 실패했습니다.", file=errors)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
