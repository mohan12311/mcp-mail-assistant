"""개인용 로컬 상태 리포트.

메일 본문은 출력하지 않고 최근 분석, 태스크, 이벤트 큐 상태만 읽습니다.
"""

import argparse
import json
from typing import Any, Dict

from db import initialize as db_initialize
from db.connection import get_connection


def build_report(limit: int = 20) -> Dict[str, Any]:
    """현재 SQLite 상태를 변경하지 않고 요약합니다."""
    db_initialize()
    with get_connection() as conn:
        emails = [dict(row) for row in conn.execute(
            """
            SELECT id, received_at, sender, subject, body_summary, urgency,
                   requires_response, is_processed
            FROM emails
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()]
        tasks = [dict(row) for row in conn.execute(
            """
            SELECT id, email_id, task_type, title, priority, status,
                   approval_status, send_state, deadline
            FROM tasks
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()]
        event_counts = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM events GROUP BY status"
            ).fetchall()
        }
    return {
        "emails": emails,
        "tasks": tasks,
        "event_counts": event_counts,
    }


def format_report(report: Dict[str, Any]) -> str:
    """터미널에서 빠르게 읽을 수 있는 텍스트로 변환합니다."""
    lines = ["MCP Mail Assistant 개인용 리포트", ""]
    counts = report["event_counts"]
    event_summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    lines.append(f"이벤트: {event_summary or '없음'}")
    ready_drafts = [
        task for task in report["tasks"]
        if task.get("task_type") == "reply_email"
        and task.get("status") == "pending"
        and task.get("approval_status") == "pending"
        and task.get("send_state") == "draft_generated"
    ]
    failed_drafts = [
        task for task in report["tasks"]
        if task.get("task_type") == "reply_email"
        and task.get("status") == "pending"
        and task.get("approval_status") == "pending"
        and task.get("send_state") == "draft_failed"
    ]
    lines.append(
        f"답장 초안: 검토 가능={len(ready_drafts)}, 재시도 필요={len(failed_drafts)}"
    )
    for task in ready_drafts:
        lines.append(
            f"- DRAFT READY: #{task['id']} "
            f"(python -m mail_operator.tasks show {task['id']})"
        )
    for task in failed_drafts:
        lines.append(
            f"- DRAFT FAILED: #{task['id']} "
            f"(python -m mail_operator.tasks draft {task['id']})"
        )
    lines.append("")
    lines.append("최근 메일 분석")
    if not report["emails"]:
        lines.append("- 없음")
    for email in report["emails"]:
        state = "처리" if email.get("is_processed") else "미처리"
        response = " / 답장 필요" if email.get("requires_response") else ""
        lines.append(
            f"- [{email.get('urgency') or 'normal'} / {state}{response}] "
            f"{email.get('subject') or '(제목 없음)'} — {email.get('sender') or '(발신자 없음)'}"
        )
        if email.get("body_summary"):
            lines.append(f"  {email['body_summary']}")
    lines.append("")
    lines.append("최근 작업")
    if not report["tasks"]:
        lines.append("- 없음")
    for task in report["tasks"]:
        send_state = f" / send={task['send_state']}" if task.get("send_state") else ""
        deadline = f" / 기한={task['deadline']}" if task.get("deadline") else ""
        lines.append(
            f"- [#{task['id']} {task.get('priority') or 'medium'} / "
            f"{task.get('status')} / approval={task.get('approval_status')}"
            f"{send_state}{deadline}] {task.get('title') or task.get('task_type')}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP Mail Assistant 로컬 분석/작업 리포트")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(limit=max(1, min(args.limit, 100)))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
