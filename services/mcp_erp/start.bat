@echo off
rem Start mcp-erp MCP Server (erp__*, purchase/inventory/vouchers) on port 8004, mock mode by default.
cd /d "%~dp0"
uv run server.py
pause
