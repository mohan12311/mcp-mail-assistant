"""
Task Tools - MCP Tools for task management operations
태스크 관리 관련 MCP 도구
"""
from mcp.types import Tool, TextContent


def get_task_tools() -> list[Tool]:
    """태스크 관리 도구 목록 반환"""
    return [
        Tool(
            name="task_list",
            description="""태스크 목록 조회

상태별로 태스크 목록을 조회합니다.
각 태스크의 ID, 제목, 우선순위, 상태, 승인 상태 등을 반환합니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "태스크 상태 (all/pending/approved/denied/completed/cancelled)",
                        "default": "pending",
                        "enum": ["all", "pending", "approved", "denied", "completed", "cancelled"]
                    }
                }
            }
        ),
        Tool(
            name="task_detail",
            description="""태스크 상세 조회

태스크 ID를 사용하여 태스크의 상세 정보를 조회합니다.
기본 정보, 설명, 관련 메일, 승인/실행 정보 등을 반환합니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "태스크 ID"
                    }
                },
                "required": ["task_id"]
            }
        ),
        Tool(
            name="task_approve",
            description="""태스크 승인 (CLI)

CLI에서 직접 태스크를 승인합니다.
Slack에서 버튼을 클릭하는 것과 동일한 효과입니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "태스크 ID"
                    }
                },
                "required": ["task_id"]
            }
        ),
        Tool(
            name="task_deny",
            description="""태스크 거부

태스크를 거부하고 취소 상태로 변경합니다.
거부 사유를 선택적으로 입력할 수 있습니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "태스크 ID"
                    },
                    "reason": {
                        "type": "string",
                        "description": "거부 사유 (선택)",
                        "default": ""
                    }
                },
                "required": ["task_id"]
            }
        ),
        Tool(
            name="task_complete",
            description="""태스크 완료 처리

태스크를 완료 상태로 변경합니다.
완료 결과를 선택적으로 입력할 수 있습니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "태스크 ID"
                    },
                    "result": {
                        "type": "string",
                        "description": "완료 결과 (선택)",
                        "default": ""
                    }
                },
                "required": ["task_id"]
            }
        ),
        Tool(
            name="task_execute",
            description="""태스크 실행 (안전한 실행기)

승인된 태스크를 실행합니다.
- allowlist: 허용된 태스크 타입만 실행
- 승인 상태 확인: approved 상태인 태스크만 실행
- 정책 검사: 위험도 평가 및 차단 정책 적용
- 감사 로그: 모든 실행 내역 기록

실행 가능한 태스크 타입:
- notify_slack: Slack 알림 전송
- create_reminder: 리마인더 생성
- summarize_email: 이메일 요약
- add_label: 라벨 추가

실행 불가 (항상 차단):
- delete_email, delete_all
- export_contacts, share_external""",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "태스크 ID"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "승인 없이 강제 실행 (위험 - safe action만 허용)",
                        "default": False
                    }
                },
                "required": ["task_id"]
            }
        ),
        Tool(
            name="task_create",
            description="""태스크 생성

이메일 분석 결과를 바탕으로 태스크를 생성합니다.
생성된 태스크는 pending 상태로 시작합니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "연관된 메일 ID"
                    },
                    "task_type": {
                        "type": "string",
                        "description": "태스크 타입 (notify_slack, reply_email, create_meeting 등)"
                    },
                    "title": {
                        "type": "string",
                        "description": "태스크 제목"
                    },
                    "description": {
                        "type": "string",
                        "description": "태스크 설명"
                    },
                    "priority": {
                        "type": "string",
                        "description": "우선순위 (urgent/high/medium/low)",
                        "default": "medium",
                        "enum": ["urgent", "high", "medium", "low"]
                    },
                    "deadline": {
                        "type": "string",
                        "description": "기한 (YYYY-MM-DD)"
                    },
                    "requires_approval": {
                        "type": "boolean",
                        "description": "승인 필요 여부",
                        "default": False
                    }
                },
                "required": ["email_id", "task_type", "title"]
            }
        ),
        Tool(
            name="task_update_status",
            description="""태스크 상태 업데이트

태스크의 상태를 변경합니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "태스크 ID"
                    },
                    "status": {
                        "type": "string",
                        "description": "새 상태",
                        "enum": ["pending", "in_progress", "completed", "failed", "cancelled"]
                    },
                    "execution_result": {
                        "type": "string",
                        "description": "실행 결과 (완료/실패 시)"
                    }
                },
                "required": ["task_id", "status"]
            }
        ),
    ]


async def handle_task_tool(name: str, arguments: dict) -> list[TextContent]:
    """태스크 도구 실행 핸들러"""
    try:
        if name == "task_list":
            return await _task_list(
                status=arguments.get("status", "pending")
            )
        
        elif name == "task_detail":
            return await _task_detail(
                task_id=arguments["task_id"]
            )
        
        elif name == "task_approve":
            return await _task_approve(
                task_id=arguments["task_id"]
            )
        
        elif name == "task_deny":
            return await _task_deny(
                task_id=arguments["task_id"],
                reason=arguments.get("reason", "")
            )
        
        elif name == "task_complete":
            return await _task_complete(
                task_id=arguments["task_id"],
                result=arguments.get("result", "")
            )
        
        elif name == "task_execute":
            return await _task_execute(
                task_id=arguments["task_id"],
                force=arguments.get("force", False)
            )
        
        elif name == "task_create":
            return await _task_create(
                email_id=arguments["email_id"],
                task_type=arguments["task_type"],
                title=arguments["title"],
                description=arguments.get("description"),
                priority=arguments.get("priority", "medium"),
                deadline=arguments.get("deadline"),
                requires_approval=arguments.get("requires_approval", False)
            )
        
        elif name == "task_update_status":
            return await _task_update_status(
                task_id=arguments["task_id"],
                status=arguments["status"],
                execution_result=arguments.get("execution_result")
            )
        
        else:
            return [TextContent(
                type="text",
                text=f"❌ Unknown task tool: {name}"
            )]
    
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ Task tool error: {str(e)}"
        )]


# 내부 구현 함수들

async def _task_list(status: str) -> list[TextContent]:
    """태스크 목록 조회"""
    try:
        from db.task_store import TaskStore
        
        task_store = TaskStore()
        
        with task_store.get_connection() as conn:
            cursor = conn.cursor()
            
            if status == "all":
                query = """
                    SELECT t.id, t.task_type, t.title, t.priority, t.status, 
                           t.approval_status, t.deadline, t.created_at,
                           e.subject, e.sender
                    FROM tasks t
                    LEFT JOIN emails e ON t.email_id = e.id
                    ORDER BY t.created_at DESC
                    LIMIT 50
                """
                cursor.execute(query)
            else:
                query = """
                    SELECT t.id, t.task_type, t.title, t.priority, t.status, 
                           t.approval_status, t.deadline, t.created_at,
                           e.subject, e.sender
                    FROM tasks t
                    LEFT JOIN emails e ON t.email_id = e.id
                    WHERE t.status = ?
                    ORDER BY t.created_at DESC
                    LIMIT 50
                """
                cursor.execute(query, (status,))
            
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            tasks = [dict(zip(columns, row)) for row in rows]
        
        if not tasks:
            return [TextContent(
                type="text",
                text=f"태스크가 없습니다 (status: {status})"
            )]
        
        # 포맷팅
        output = f"# 태스크 목록 (status: {status})\n\n"
        output += f"총 {len(tasks)}개\n\n"
        
        for task in tasks:
            priority_emoji = {
                'urgent': '🔴',
                'high': '🟠',
                'medium': '🟡',
                'low': '⚪'
            }.get(task['priority'], '⚪')
            
            output += f"## [{task['id']}] {priority_emoji} {task['title']}\n"
            output += f"- **유형**: {task['task_type']}\n"
            output += f"- **우선순위**: {task['priority']}\n"
            output += f"- **상태**: {task['status']}\n"
            output += f"- **승인**: {task['approval_status']}\n"
            if task['deadline']:
                output += f"- **기한**: {task['deadline']}\n"
            output += f"- **메일**: {task['subject']} (from {task['sender']})\n"
            output += f"- **생성**: {task['created_at']}\n\n"
        
        return [TextContent(type="text", text=output)]
    
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ 태스크 목록 조회 실패: {e}"
        )]


async def _task_detail(task_id: int) -> list[TextContent]:
    """태스크 상세 조회"""
    try:
        from db.task_store import TaskStore
        
        task_store = TaskStore()
        
        with task_store.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT t.*, e.subject, e.sender, e.sender_email, 
                       e.body_text, e.received_at
                FROM tasks t
                LEFT JOIN emails e ON t.email_id = e.id
                WHERE t.id = ?
            """, (task_id,))
            
            row = cursor.fetchone()
            if not row:
                return [TextContent(
                    type="text",
                    text=f"❌ 태스크 ID {task_id}를 찾을 수 없습니다."
                )]
            
            columns = [desc[0] for desc in cursor.description]
            task = dict(zip(columns, row))
        
        # 포맷팅
        output = f"# 태스크 상세 [ID: {task['id']}]\n\n"
        output += f"## 기본 정보\n"
        output += f"- **제목**: {task['title']}\n"
        output += f"- **유형**: {task['task_type']}\n"
        output += f"- **우선순위**: {task['priority']}\n"
        output += f"- **상태**: {task['status']}\n"
        output += f"- **승인 상태**: {task['approval_status']}\n"
        
        if task['deadline']:
            output += f"- **기한**: {task['deadline']}\n"
        
        output += f"\n## 설명\n{task['description']}\n\n"
        
        output += f"## 관련 메일\n"
        output += f"- **제목**: {task['subject']}\n"
        output += f"- **발신**: {task['sender']} <{task['sender_email']}>\n"
        output += f"- **수신**: {task['received_at']}\n\n"
        
        if task['approved_at']:
            output += f"## 승인 정보\n"
            output += f"- **승인 시각**: {task['approved_at']}\n\n"
        
        if task['executed_at']:
            output += f"## 실행 정보\n"
            output += f"- **실행 시각**: {task['executed_at']}\n"
            if task['execution_result']:
                output += f"- **결과**: {task['execution_result']}\n"
            output += "\n"
        
        output += f"## 메타데이터\n"
        output += f"- **생성**: {task['created_at']}\n"
        output += f"- **Email ID**: {task['email_id']}\n"
        
        return [TextContent(type="text", text=output)]
    
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ 태스크 상세 조회 실패: {e}"
        )]


async def _task_approve(task_id: int) -> list[TextContent]:
    """태스크 승인 (CLI) - 이벤트 기반"""
    try:
        from db.approval_store import LOCAL_MCP, create_approval_decision

        create_approval_decision(
            task_id=task_id,
            approved=True,
            source=LOCAL_MCP,
            action="task_approve",
            source_ref="manual",
        )
        
        return [TextContent(
            type="text",
            text=f"✅ 태스크 {task_id}가 승인되었습니다.\n이벤트가 생성되어 Operator가 실행을 처리합니다."
        )]
    
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ 태스크 승인 실패: {e}"
        )]


async def _task_deny(task_id: int, reason: str) -> list[TextContent]:
    """태스크 거부 - 이벤트 기반"""
    try:
        from db.approval_store import LOCAL_MCP, create_approval_decision

        create_approval_decision(
            task_id=task_id,
            approved=False,
            source=LOCAL_MCP,
            action="task_deny",
            source_ref="manual",
            reason=reason,
        )
        
        return [TextContent(
            type="text",
            text=f"✅ 태스크 {task_id}가 거부되었습니다." + (f"\n사유: {reason}" if reason else "")
        )]
    
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ 태스크 거부 실패: {e}"
        )]


async def _task_complete(task_id: int, result: str) -> list[TextContent]:
    """태스크 완료 처리"""
    try:
        from db import task_store

        if not task_store.update_task_status(
            task_id,
            "completed",
            result or "Completed manually",
        ):
            return [TextContent(
                type="text",
                text=f"❌ 태스크 ID {task_id}를 찾을 수 없습니다."
            )]
        
        return [TextContent(
            type="text",
            text=f"✅ 태스크 {task_id}가 완료 처리되었습니다."
        )]
    
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ 태스크 완료 처리 실패: {e}"
        )]


# 안전한 실행 도구용 allowlist
SAFE_ACTIONS = {
    "notify_slack",
    "create_reminder",
    "summarize_email",
    "add_label",
    "mark_read",
    "update_priority",
}

APPROVAL_REQUIRED_ACTIONS = {
    "reply_email",
    "forward_email",
    "create_meeting",
    "send_email",
}

BLOCKED_ACTIONS = {
    "delete_email",
    "delete_all",
    "export_contacts",
    "share_external",
}


async def _task_execute(task_id: int, force: bool) -> list[TextContent]:
    """안전한 태스크 실행"""
    try:
        from db.connection import get_connection
        from db.approval_store import iso_utc
        from db.task_state import transition_task
        from db import event_store
        from db.event_store import EventType
        import logging
        
        logger = logging.getLogger(__name__)
        
        with get_connection() as conn:
            cursor = conn.cursor()
            
            # 태스크 조회
            cursor.execute("""
                SELECT id, email_id, task_type, title, description, 
                       status, approval_status
                FROM tasks WHERE id = ?
            """, (task_id,))
            row = cursor.fetchone()
            
            if not row:
                return [TextContent(
                    type="text",
                    text=f"❌ 태스크 ID {task_id}를 찾을 수 없습니다."
                )]
            
            task = dict(row)
            task_type = task['task_type']
            approval_status = task['approval_status']
            
            # 1. 차단된 액션 체크
            if task_type in BLOCKED_ACTIONS:
                logger.warning(f"[AUDIT] 차단된 액션 실행 시도: task_id={task_id}, type={task_type}")
                return [TextContent(
                    type="text",
                    text=f"🚫 차단된 액션입니다: {task_type}\n이 액션은 보안상 실행할 수 없습니다."
                )]
            
            # 2. 승인 상태 체크
            if task_type in APPROVAL_REQUIRED_ACTIONS:
                if approval_status != 'approved':
                    if force:
                        logger.warning(f"[AUDIT] 강제 실행 시도 (승인 필요 액션): task_id={task_id}")
                        return [TextContent(
                            type="text",
                            text=f"⚠️ 이 액션은 승인이 필요합니다: {task_type}\n현재 승인 상태: {approval_status}\n승인 후 다시 시도하세요."
                        )]
                    return [TextContent(
                        type="text",
                        text=f"⚠️ 승인이 필요한 태스크입니다.\n현재 승인 상태: {approval_status}\ntask_approve({task_id})로 먼저 승인하세요."
                    )]
            
            # 3. 안전한 액션은 승인 없이도 실행 가능 (force=True 또는 safe action)
            if task_type in SAFE_ACTIONS or (approval_status == 'approved'):
                pass  # 실행 진행
            elif force and task_type not in APPROVAL_REQUIRED_ACTIONS:
                logger.info(f"[AUDIT] 강제 실행 (safe action): task_id={task_id}, type={task_type}")
            else:
                return [TextContent(
                    type="text",
                    text=f"⚠️ 알 수 없는 태스크 타입: {task_type}\n승인 후 실행하거나 안전한 액션인지 확인하세요."
                )]
            
            # 4. 실제 실행
            logger.info(f"[AUDIT] 태스크 실행 시작: task_id={task_id}, type={task_type}")
            
            result_text = ""
            success = True
            
            if task_type == "notify_slack":
                # Slack 알림 전송 (시뮬레이션)
                result_text = f"Slack 알림 전송 완료: {task['title']}"
                logger.info(f"[Slack 알림] {task['title']}")
            
            elif task_type == "create_reminder":
                result_text = f"리마인더 생성 완료: {task['title']}"
                logger.info(f"[리마인더] {task['title']}")
            
            elif task_type == "summarize_email":
                result_text = f"이메일 요약 완료: {task['title']}"
            
            elif task_type == "add_label":
                result_text = f"라벨 추가 완료: {task['title']}"
            
            elif task_type == "reply_email":
                result_text = f"이메일 답장 완료 (시뮬레이션): {task['title']}"
            
            elif task_type == "forward_email":
                result_text = f"이메일 전달 완료 (시뮬레이션): {task['title']}"
            
            else:
                result_text = f"태스크 실행 완료: {task['title']}"
            
            # 5. 태스크 상태 업데이트
            transition_task(
                conn,
                task_id,
                status="completed",
                fields={"executed_at": iso_utc(), "execution_result": result_text},
            )
            conn.commit()
            
            # 6. 실행 완료 이벤트 생성
            event_store.create_event(
                event_type=EventType.TASK_EXECUTED,
                payload={
                    "task_id": task_id,
                    "task_type": task_type,
                    "result": result_text,
                    "executed_at": iso_utc()
                },
                source_id=f"task:{task_id}:task_executed"
            )
            
            logger.info(f"[AUDIT] 태스크 실행 완료: task_id={task_id}, result={result_text[:50]}")
            
            return [TextContent(
                type="text",
                text=f"✅ 태스크 실행 완료\nID: {task_id}\n타입: {task_type}\n결과: {result_text}"
            )]
        
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[AUDIT] 태스크 실행 실패: task_id={task_id}, error={e}")
        return [TextContent(
            type="text",
            text=f"❌ 태스크 실행 실패: {e}"
        )]


async def _task_create(
    email_id: str,
    task_type: str,
    title: str,
    description: str = None,
    priority: str = "medium",
    deadline: str = None,
    requires_approval: bool = False,
) -> list[TextContent]:
    """태스크 생성"""
    try:
        from db import task_store
        
        task_id = task_store.create_task(
            email_id=email_id,
            task_type=task_type,
            title=title,
            description=description,
            deadline=deadline,
            priority=priority,
            approval_status="pending" if requires_approval else "none",
        )
        
        return [TextContent(
            type="text",
            text=f"✅ 태스크 생성 완료\nID: {task_id}\n타입: {task_type}\n제목: {title}"
        )]
    
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ 태스크 생성 실패: {e}"
        )]


async def _task_update_status(
    task_id: int,
    status: str,
    execution_result: str = None
) -> list[TextContent]:
    """태스크 상태 업데이트"""
    try:
        from db import task_store
        
        success = task_store.update_task_status(
            task_id=task_id,
            status=status,
            execution_result=execution_result
        )
        
        if success:
            return [TextContent(
                type="text",
                text=f"✅ 태스크 상태 업데이트: {task_id} → {status}"
            )]
        else:
            return [TextContent(
                type="text",
                text=f"⚠️ 태스크를 찾을 수 없습니다: {task_id}"
            )]
    
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ 태스크 상태 업데이트 실패: {e}"
        )]
