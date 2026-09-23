@echo off
rem Start mcp-crm MCP Server (crm__*, customers/sales orders) on port 8003, mock mode by default.
cd /d "%~dp0"
uv run server.py
pause
