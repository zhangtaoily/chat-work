@echo off
rem Start mcp-bi MCP Server (bi__*, natural-language BI query, read-only) on port 8002.
cd /d "%~dp0"
uv run server.py
pause
