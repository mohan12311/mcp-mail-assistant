"""
MCP Mail Assistant MCP Server
기존 skills/ 모듈을 MCP Tools로 래핑하여 Claude Desktop/Code에서 사용

실행: python mcp_server/server.py
"""
import asyncio
import sys
import os

# 프로젝트 루트를 Python 경로에 추가
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from core.config import Config
from skills import mail_skills, attachment_skills, notification_skills

from mcp_server.mcp_tools.email_tools import get_email_tools, handle_email_tool
from mcp_server.mcp_tools.email_db_tools import get_email_db_tools, handle_email_db_tool
from mcp_server.mcp_tools.attachment_tools import get_attachment_tools, handle_attachment_tool
from mcp_server.mcp_tools.notification_tools import get_notification_tools, handle_notification_tool
from mcp_server.mcp_tools.task_tools import get_task_tools, handle_task_tool
from mcp_server.mcp_tools.event_tools import get_event_tools, handle_event_tool


# MCP 서버 인스턴스
server = Server("mcp-mail-assistant")


def initialize_skills() -> Config:
    """설정 로드 및 Skills 초기화"""
    try:
        config = Config.from_env()
    except Exception as e:
        print(f"Config 로드 실패: {e}", file=sys.stderr)
        raise
    
    # Skills 초기화
    mail_skills.init_config(config)
    attachment_skills.init_config(config)
    notification_skills.init_config(config)
    
    return config


def register_all_tools():
    """모든 MCP 도구 등록"""
    
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """모든 도구 목록 반환"""
        all_tools = []
        all_tools.extend(get_email_tools())
        all_tools.extend(get_email_db_tools())
        all_tools.extend(get_attachment_tools())
        all_tools.extend(get_notification_tools())
        all_tools.extend(get_task_tools())
        all_tools.extend(get_event_tools())
        return all_tools
    
    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """도구 실행 라우팅"""
        # 이메일 DB 도구 (email_db_* - email_* 보다 먼저 체크)
        if name.startswith("email_db_"):
            return await handle_email_db_tool(name, arguments)
        
        # 이메일 도구 (IMAP 기반)
        elif name.startswith("email_"):
            return await handle_email_tool(name, arguments)

        # 이메일(DB) 도구
        if name.startswith("emaildb_"):
            return await handle_email_db_tool(name, arguments)
        
        # 첨부파일 도구
        elif name.startswith("attachment_"):
            return await handle_attachment_tool(name, arguments)
        
        # Slack 알림 도구
        elif name.startswith("slack_"):
            return await handle_notification_tool(name, arguments)
        
        # 태스크 도구
        elif name.startswith("task_"):
            return await handle_task_tool(name, arguments)

        # 이벤트 도구
        elif name.startswith("event_"):
            return await handle_event_tool(name, arguments)
        
        # 이벤트 도구
        elif name.startswith("event_"):
            return await handle_event_tool(name, arguments)
        
        else:
            raise ValueError(f"Unknown tool: {name}")


async def main():
    """MCP 서버 메인"""
    print("MCP Mail Assistant MCP Server 시작 중...", file=sys.stderr)
    
    # 설정 및 Skills 초기화
    try:
        config = initialize_skills()
        print(f"Config 로드 완료: {config.imap_user}", file=sys.stderr)
    except Exception as e:
        print(f"초기화 실패: {e}", file=sys.stderr)
        return
    
    # 도구 등록
    register_all_tools()
    print("MCP 도구 등록 완료", file=sys.stderr)
    
    # stdio 전송으로 실행
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())

