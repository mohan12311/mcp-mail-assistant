"""Two-step local review and explicit re-confirmation for quarantined sends.

This module never calls SMTP. It only creates a new SEND_CONFIRMED event after
the operator re-enters both the task ID and exact reviewed payload hash.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Sequence, TextIO

from db import outbound_send_store, task_store
from mail_operator.send_confirmation import LOCAL_APPROVAL_SOURCE, send_payload_sha256


class RetryReviewError(Exception):
    """Safe user-facing failure for the retry review CLI."""


@dataclass(frozen=True)
class RetryReview:
    task_id: int
    send_state: str
    recipient: str
    subject: str
    body: str
    payload_hash: str


def _load_review(task_id: int) -> RetryReview:
    task = task_store.get_task(task_id)
    if not task or (
        task.get("task_type") != "reply_email"
        or task.get("status") != "pending"
        or task.get("approval_status") not in {"pending", "approved", "expired"}
        or task.get("send_state") not in outbound_send_store.RETRYABLE_TASK_STATES
    ):
        raise RetryReviewError(
            "태스크가 send_failed/delivery_unknown 재확인 대상이 아닙니다."
        )
    raw_payload = task.get("send_payload")
    try:
        payload = json.loads(raw_payload)
        recipient = str(payload["recipient"]).strip()
        subject = str(payload["subject"]).strip()
        body = str(payload["body"])
    except (TypeError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise RetryReviewError("저장된 발송 payload를 안전하게 검토할 수 없습니다.") from exc
    if (
        not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+", recipient)
        or not subject
        or "\r" in subject
        or "\n" in subject
        or not body
    ):
        raise RetryReviewError("수신자, 제목 또는 본문이 유효하지 않습니다.")
    return RetryReview(
        task_id=task_id,
        send_state=str(task["send_state"]),
        recipient=recipient,
        subject=subject,
        body=body,
        payload_hash=send_payload_sha256(raw_payload),
    )


def _print_review(review: RetryReview, out: TextIO) -> None:
    print(f"Task ID: {review.task_id}", file=out)
    print(f"Current state: {review.send_state}", file=out)
    print(f"Recipient: {review.recipient}", file=out)
    print(f"Subject: {review.subject}", file=out)
    print("Body:", file=out)
    print(review.body, file=out)
    print(f"Payload SHA-256: {review.payload_hash}", file=out)
    if review.send_state == "delivery_unknown":
        print(
            "WARNING: The previous SMTP attempt may already have been delivered.",
            file=out,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mail_operator.retry_send",
        description="실패/불확실 발송을 다시 검토하고 새 명시적 승인을 생성합니다.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review", help="수신자·제목·본문과 payload hash 재검토")
    review.add_argument("task_id", type=int)
    confirm = commands.add_parser("confirm", help="검토한 hash로 새 승인 이벤트 생성")
    confirm.add_argument("task_id", type=int)
    confirm.add_argument("--confirm", type=int, required=True, metavar="TASK_ID")
    confirm.add_argument("--payload-sha256", required=True, metavar="SHA256")
    confirm.add_argument(
        "--accept-possible-duplicate",
        action="store_true",
        help="delivery_unknown이 이미 배달되었을 수 있음을 명시적으로 수락",
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
    try:
        if args.task_id <= 0:
            raise RetryReviewError("task_id는 양의 정수여야 합니다.")
        review = _load_review(args.task_id)
        if args.command == "review":
            _print_review(review, output)
            return 0
        if args.confirm != args.task_id:
            raise RetryReviewError("승인 확인 ID가 task_id와 일치하지 않습니다.")
        if not re.fullmatch(r"[0-9a-f]{64}", args.payload_sha256 or ""):
            raise RetryReviewError("payload SHA-256 형식이 올바르지 않습니다.")
        if args.payload_sha256 != review.payload_hash:
            raise RetryReviewError("검토한 payload hash가 현재 초안과 일치하지 않습니다.")
        if review.send_state == "delivery_unknown" and not args.accept_possible_duplicate:
            raise RetryReviewError(
                "delivery_unknown은 --accept-possible-duplicate 명시가 필요합니다."
            )
        event_id, _confirmation_id = outbound_send_store.create_retry_confirmation(
            task_id=review.task_id,
            approval_payload_hash=review.payload_hash,
            approval_source=LOCAL_APPROVAL_SOURCE,
        )
        print(
            f"새 재확인 이벤트 큐잉 완료: task_id={review.task_id} event_id={event_id}",
            file=output,
        )
        return 0
    except RetryReviewError as exc:
        print(f"오류: {exc}", file=errors)
        return 1
    except Exception:
        print("오류: 발송 재확인 이벤트를 생성하지 못했습니다.", file=errors)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
