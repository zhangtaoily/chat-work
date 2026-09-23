@echo off
rem Start mcp-u8 MCP Server (u8__*, GL balance/voucher detail) on port 8007, mock mode by default.
cd /d "%~dp0"
uv run server.py
pause
