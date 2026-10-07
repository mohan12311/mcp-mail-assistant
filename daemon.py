#!/usr/bin/env python
"""
MCP Mail Assistant - 메인 진입점

AI-powered email assistant daemon for Microsoft 365 Outlook.
"""
import sys
from daemon.cli import main

if __name__ == "__main__":
    sys.exit(main())

