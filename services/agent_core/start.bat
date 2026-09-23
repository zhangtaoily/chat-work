@echo off
rem Start agent-core (FastAPI + LangGraph pipeline) on port 8011.
cd /d "%~dp0"
uv run uvicorn agent_core.api.main:app --host 127.0.0.1 --port 8011
pause
