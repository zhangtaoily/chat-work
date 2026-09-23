@echo off
rem ============================================================
rem  Chat-Work one-click launcher (dev / mock mode)
rem  - 9 backend services run HIDDEN in background (logs in .\logs)
rem  - Only 1 console window stays: this one, running the desktop
rem  Usage: start-all.bat        -> Electron desktop app
rem         start-all.bat web    -> browser preview (:5173)
rem  Stop backend services: stop-all.bat
rem  Prereq: Node 22 + pnpm, Python 3.12 + uv (first run: pnpm install)
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
if not exist logs mkdir logs

echo [1/4] Starting mock_idp (:8012) ...
call :start_svc mock_idp 8012 "%~dp0services\mock_idp" "uv run python -m mock_idp"

echo [2/4] Starting MCP servers (:8001-8007) ...
call :start_svc mcp_oa  8001 "%~dp0services\mcp_oa"  "uv run server.py"
call :start_svc mcp_bi  8002 "%~dp0services\mcp_bi"  "uv run server.py"
call :start_svc mcp_crm 8003 "%~dp0services\mcp_crm" "uv run server.py"
call :start_svc mcp_erp 8004 "%~dp0services\mcp_erp" "uv run server.py"
call :start_svc mcp_wms 8005 "%~dp0services\mcp_wms" "uv run server.py"
call :start_svc mcp_mes 8006 "%~dp0services\mcp_mes" "uv run server.py"
call :start_svc mcp_u8  8007 "%~dp0services\mcp_u8"  "uv run server.py"

echo [3/4] Starting agent-core (:8011) ...
call :start_svc agent_core 8011 "%~dp0services\agent_core" "uv run uvicorn agent_core.api.main:app --host 127.0.0.1 --port 8011"

echo [4/4] Starting desktop ...
cd /d "%~dp0apps\desktop"
if /i "%~1"=="web" (
  echo   mode: web preview - http://localhost:5173
  call pnpm dev:web
) else (
  echo   mode: Electron desktop app
  call pnpm dev
)
goto :eof

rem --- helper: start one hidden background service ---------------
rem  %1=name  %2=port  %3=workdir  %4=command
:start_svc
netstat -ano | findstr ":%~2 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo   [skip] %~1 :%~2 already running
  exit /b 0
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -WindowStyle Hidden -FilePath $env:ComSpec -ArgumentList '/c %~4' -WorkingDirectory '%~3' -RedirectStandardOutput '%~dp0logs\%~1.out.log' -RedirectStandardError '%~dp0logs\%~1.err.log'"
echo   [ok]   %~1 :%~2 started hidden (logs\%~1.out.log)
exit /b 0
