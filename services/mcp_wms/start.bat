@echo off
rem Start mcp-wms MCP Server (wms__*, stock orders/alerts) on port 8005, mock mode by default.
cd /d "%~dp0"
uv run server.py
pause
