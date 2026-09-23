@echo off
rem Start mcp-oa MCP Server (oa__*, leave/approvals) on port 8001, mock mode by default.
cd /d "%~dp0"
uv run server.py
pause
