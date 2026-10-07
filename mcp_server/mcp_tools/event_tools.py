"""
Event Tools - MCP Tools for event queue operations
db/event_store.py의 함수들을 MCP Tool로 래핑

KIRA식 이벤트 큐를 Operator가 소비할 수 있도록 합니다.
"""
import json
from mcp.types import Tool, TextContent

from db import event_store
from db.event_store import EventType, EventStatus


def get_event_tools() -> list[Tool]:
    """이벤트 도구 목록 반환"""
    return [
        Tool(
            name="event_lease_next",
            description="""다음 처리할 이벤트를 lease(임대)합니다.

Operator가 이벤트를 가져와 처리하기 위한 도구입니다.
- pending 상태이거나 lease 만료된 이벤트를 대상으로 합니다.
- 한 번에 여러 이벤트를 lease할 수 있습니다.
- lease 시간 내에 ack 또는 fail을 호출해야 합니다.

반환 정보: 이벤트 ID, 타입, 페이로드, 시도 횟수 등""",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "lease 소유자 ID (operator instance 식별자)"
                    },
                    "lease_seconds": {
                        "type": "integer",
                        "description": "lease 유효 시간(초) (기본: 300)",
                        "default": 300,
                        "minimum": 30,
                        "maximum": 3600
                    },
                    "event_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "처리할 이벤트 타입 목록 (예: ['new_email']). 미지정시 전체"
                    },
                    "max_count": {
                        "type": "integer",
                        "description": "한 번에 lease할 최대 이벤트 수 (기본: 1)",
                        "default": 1,
                        "minimum": 1,
                        "maximum": 10
                    }
                },
                "required": ["owner"]
            }
        ),
        Tool(
            name="event_ack",
            description="""이벤트 처리 완료를 확인(acknowledge)합니다.

이벤트가 성공적으로 처리되었음을 표시합니다.
ack된 이벤트는 done 상태가 되어 다시 처리되지 않습니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "이벤트 ID"
                    }
                },
                "required": ["event_id"]
            }
        ),
        Tool(
            name="event_fail",
            description="""이벤트 처리 실패를 기록합니다.

에러 메시지와 재시도 여부를 기록합니다.
- retryable=true: 재시도 대기 상태로 (attempts < max_attempts면)
- retryable=false: 즉시 failed 상태로 (DLQ로 이동)

max_attempts 초과 시 자동으로 failed 상태가 됩니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "이벤트 ID"
                    },
                    "error": {
                        "type": "string",
                        "description": "에러 메시지"
                    },
                    "retryable": {
                        "type": "boolean",
                        "description": "재시도 가능 여부 (기본: true)",
                        "default": True
                    }
                },
                "required": ["event_id", "error"]
            }
        ),
        Tool(
            name="event_create",
            description="""새 이벤트를 생성합니다.

Operator 또는 외부 시스템에서 이벤트를 직접 생성할 때 사용합니다.
예: approval_granted, task_executed 등의 후속 이벤트 생성""",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "description": "이벤트 타입 (new_email, approval_granted, approval_denied, task_created, task_executed, error)",
                        "enum": ["new_email", "approval_granted", "approval_denied", "task_created", "task_executed", "error"]
                    },
                    "payload": {
                        "type": "object",
                        "description": "이벤트 페이로드 (JSON 객체)"
                    },
                    "source_id": {
                        "type": "string",
                        "description": "원본 ID (email_id, task_id 등) - idempotency 체크용"
                    },
                    "max_attempts": {
                        "type": "integer",
                        "description": "최대 재시도 횟수 (기본: 3)",
                        "default": 3
                    }
                },
                "required": ["event_type", "payload"]
            }
        ),
        Tool(
            name="event_get",
            description="""이벤트를 ID로 조회합니다.

특정 이벤트의 상세 정보를 확인할 때 사용합니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "이벤트 ID"
                    }
                },
                "required": ["event_id"]
            }
        ),
        Tool(
            name="event_list",
            description="""이벤트 목록을 조회합니다.

상태별, 타입별로 이벤트 목록을 확인할 수 있습니다.
운영/디버깅용으로 사용합니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "필터링할 상태 (pending, leased, done, failed)",
                        "enum": ["pending", "leased", "done", "failed"]
                    },
                    "event_type": {
                        "type": "string",
                        "description": "필터링할 이벤트 타입"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "최대 조회 개수 (기본: 20)",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 100
                    }
                }
            }
        ),
        Tool(
            name="event_list_failed",
            description="""실패한 이벤트(DLQ) 목록을 조회합니다.

재처리가 필요한 실패 이벤트들을 확인할 때 사용합니다.
send_confirmed를 제외한 안전한 이벤트만 event_retry로 재시도할 수 있습니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "최대 조회 개수 (기본: 20)",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 100
                    }
                }
            }
        ),
        Tool(
            name="event_retry",
            description="""안전한 실패 이벤트를 재시도 대기 상태로 변경합니다.

failed 상태의 이벤트를 pending으로 변경하여 재처리할 수 있게 합니다.
attempts는 0으로 리셋됩니다. send_confirmed는 새 명시적 확인이 필요하므로 거부합니다.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "이벤트 ID"
                    }
                },
                "required": ["event_id"]
            }
        ),
        Tool(
            name="event_stats",
            description="""이벤트 통계를 반환합니다.

상태별 이벤트 수를 확인하여 큐 상태를 모니터링합니다.""",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        )
    ]


async def handle_event_tool(name: str, arguments: dict) -> list[TextContent]:
    """이벤트 도구 실행"""
    
    if name == "event_lease_next":
        return await _event_lease_next(
            owner=arguments["owner"],
            lease_seconds=arguments.get("lease_seconds", 300),
            event_types=arguments.get("event_types"),
            max_count=arguments.get("max_count", 1)
        )
    
    elif name == "event_ack":
        return await _event_ack(event_id=arguments["event_id"])
    
    elif name == "event_fail":
        return await _event_fail(
            event_id=arguments["event_id"],
            error=arguments["error"],
            retryable=arguments.get("retryable", True)
        )
    
    elif name == "event_create":
        return await _event_create(
            event_type=arguments["event_type"],
            payload=arguments["payload"],
            source_id=arguments.get("source_id"),
            max_attempts=arguments.get("max_attempts", 3)
        )
    
    elif name == "event_get":
        return await _event_get(event_id=arguments["event_id"])
    
    elif name == "event_list":
        return await _event_list(
            status=arguments.get("status"),
            event_type=arguments.get("event_type"),
            limit=arguments.get("limit", 20)
        )
    
    elif name == "event_list_failed":
        return await _event_list_failed(limit=arguments.get("limit", 20))
    
    elif name == "event_retry":
        return await _event_retry(event_id=arguments["event_id"])
    
    elif name == "event_stats":
        return await _event_stats()
    
    else:
        raise ValueError(f"Unknown event tool: {name}")


async def _event_lease_next(
    owner: str,
    lease_seconds: int,
    event_types: list[str] | None,
    max_count: int
) -> list[TextContent]:
    """다음 이벤트 lease"""
    try:
        events = event_store.lease_next_event(
            owner=owner,
            lease_seconds=lease_seconds,
            event_types=event_types,
            max_count=max_count
        )
        
        if not events:
            return [TextContent(type="text", text="📭 처리할 이벤트가 없습니다.")]
        
        result_text = f"📥 {len(events)}개 이벤트 lease 완료 (owner: {owner}, {lease_seconds}초)\n\n"
        
        for i, event in enumerate(events, 1):
            result_text += f"[{i}] 이벤트 ID: {event['id']}\n"
            result_text += f"    타입: {event['event_type']}\n"
            result_text += f"    시도: {event['attempts']}/{event['max_attempts']}\n"
            result_text += f"    생성: {event['created_at']}\n"
            result_text += f"    페이로드: {json.dumps(event['payload'], ensure_ascii=False, indent=2)}\n\n"
        
        # JSON 데이터 포함
        result_text += f"\n---\n[RAW_DATA]\n{json.dumps(events, ensure_ascii=False, indent=2)}"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 이벤트 lease 실패: {str(e)}")]


async def _event_ack(event_id: str) -> list[TextContent]:
    """이벤트 ack"""
    try:
        success = event_store.ack_event(event_id)
        
        if success:
            return [TextContent(type="text", text=f"✅ 이벤트 ack 완료: {event_id}")]
        else:
            return [TextContent(type="text", text=f"⚠️ 이벤트를 찾을 수 없습니다: {event_id}")]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 이벤트 ack 실패: {str(e)}")]


async def _event_fail(event_id: str, error: str, retryable: bool) -> list[TextContent]:
    """이벤트 fail 기록"""
    try:
        success = event_store.fail_event(event_id, error, retryable)
        
        if success:
            retry_info = "(재시도 예정)" if retryable else "(DLQ로 이동)"
            return [TextContent(type="text", text=f"⚠️ 이벤트 fail 기록: {event_id} {retry_info}\n에러: {error}")]
        else:
            return [TextContent(type="text", text=f"⚠️ 이벤트를 찾을 수 없습니다: {event_id}")]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 이벤트 fail 기록 실패: {str(e)}")]


async def _event_create(
    event_type: str,
    payload: dict,
    source_id: str | None,
    max_attempts: int
) -> list[TextContent]:
    """이벤트 생성"""
    try:
        event_id = event_store.create_event(
            event_type=event_type,
            payload=payload,
            source_id=source_id,
            max_attempts=max_attempts
        )
        
        return [TextContent(
            type="text",
            text=f"✅ 이벤트 생성 완료\nID: {event_id}\n타입: {event_type}\n소스: {source_id or '(없음)'}"
        )]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 이벤트 생성 실패: {str(e)}")]


async def _event_get(event_id: str) -> list[TextContent]:
    """이벤트 조회"""
    try:
        event = event_store.get_event(event_id)
        
        if not event:
            return [TextContent(type="text", text=f"⚠️ 이벤트를 찾을 수 없습니다: {event_id}")]
        
        result_text = f"📋 이벤트 상세\n\n"
        result_text += f"ID: {event['id']}\n"
        result_text += f"타입: {event['event_type']}\n"
        result_text += f"상태: {event['status']}\n"
        result_text += f"시도: {event['attempts']}/{event['max_attempts']}\n"
        result_text += f"소스 ID: {event.get('source_id', '(없음)')}\n"
        result_text += f"생성: {event['created_at']}\n"
        result_text += f"수정: {event['updated_at']}\n"
        
        if event.get('lease_owner'):
            result_text += f"Lease 소유자: {event['lease_owner']}\n"
            result_text += f"Lease 만료: {event['lease_until']}\n"
        
        if event.get('last_error'):
            result_text += f"마지막 에러: {event['last_error']}\n"
        
        result_text += f"\n페이로드:\n{json.dumps(event['payload'], ensure_ascii=False, indent=2)}"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 이벤트 조회 실패: {str(e)}")]


async def _event_list(
    status: str | None,
    event_type: str | None,
    limit: int
) -> list[TextContent]:
    """이벤트 목록 조회"""
    try:
        events = event_store.list_events(
            status=status,
            event_type=event_type,
            limit=limit
        )
        
        if not events:
            return [TextContent(type="text", text="📭 조건에 맞는 이벤트가 없습니다.")]
        
        filter_info = []
        if status:
            filter_info.append(f"상태={status}")
        if event_type:
            filter_info.append(f"타입={event_type}")
        filter_str = f" ({', '.join(filter_info)})" if filter_info else ""
        
        result_text = f"📋 이벤트 목록{filter_str} - 총 {len(events)}건\n\n"
        
        for event in events:
            status_emoji = {
                "pending": "⏳",
                "leased": "🔒",
                "done": "✅",
                "failed": "❌"
            }.get(event['status'], "❓")
            
            result_text += f"{status_emoji} [{event['event_type']}] {event['id'][:8]}...\n"
            result_text += f"   상태: {event['status']} | 시도: {event['attempts']}/{event['max_attempts']}\n"
            result_text += f"   생성: {event['created_at']}\n\n"
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 이벤트 목록 조회 실패: {str(e)}")]


async def _event_list_failed(limit: int) -> list[TextContent]:
    """실패한 이벤트 목록 조회"""
    try:
        events = event_store.list_dlq_summary(limit=limit)
        
        if not events:
            return [TextContent(type="text", text="✅ 실패한 이벤트가 없습니다.")]
        
        result_text = f"❌ 실패한 이벤트 (DLQ) - 총 {len(events)}건\n\n"
        
        for event in events:
            result_text += f"[{event['event_type']}] {event['id']}\n"
            result_text += f"   시도: {event['attempts']}/{event['max_attempts']}\n"
            result_text += f"   생성: {event['created_at']}\n\n"
        
        result_text += (
            "\n💡 안전한 이벤트만 event_retry(event_id)로 재시도하세요. "
            "send_confirmed는 새 명시적 확인이 필요합니다."
        )
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 실패 이벤트 조회 실패: {str(e)}")]


async def _event_retry(event_id: str) -> list[TextContent]:
    """이벤트 재시도"""
    try:
        success = event_store.retry_event(event_id)
        
        if success:
            return [TextContent(type="text", text=f"🔄 이벤트 재시도 설정 완료: {event_id}\n상태가 pending으로 변경되었습니다.")]
        else:
            return [TextContent(type="text", text=f"⚠️ 재시도 설정 실패: 이벤트가 없거나 failed 상태가 아닙니다 ({event_id})")]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 이벤트 재시도 설정 실패: {str(e)}")]


async def _event_stats() -> list[TextContent]:
    """이벤트 통계"""
    try:
        stats = event_store.get_event_stats()
        
        total = sum(stats.values())
        
        result_text = f"📊 이벤트 큐 통계\n\n"
        result_text += f"전체: {total}건\n"
        result_text += f"├─ ⏳ pending: {stats.get('pending', 0)}건\n"
        result_text += f"├─ 🔒 leased: {stats.get('leased', 0)}건\n"
        result_text += f"├─ ✅ done: {stats.get('done', 0)}건\n"
        result_text += f"└─ ❌ failed: {stats.get('failed', 0)}건\n"
        
        if stats.get('failed', 0) > 0:
            result_text += f"\n⚠️ 실패한 이벤트가 있습니다. event_list_failed로 확인하세요."
        
        return [TextContent(type="text", text=result_text)]
    
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 통계 조회 실패: {str(e)}")]
