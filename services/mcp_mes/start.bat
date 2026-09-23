@echo off
rem Start mcp-mes MCP Server (mes__*, production/work orders) on port 8006, mock mode by default.
cd /d "%~dp0"
uv run server.py
pause
