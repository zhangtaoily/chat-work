@echo off
rem ============================================================
rem  Chat-Work: stop all background dev services
rem  Kills listeners on: 8001-8007, 8011, 8012, 5173
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
set KILLED=0
for %%p in (8001 8002 8003 8004 8005 8006 8007 8011 8012 5173) do call :kill_port %%p
echo.
echo Done. %KILLED% process(es) killed.
pause
exit /b 0

:kill_port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%1 " ^| findstr "LISTENING"') do (
  taskkill /F /T /PID %%a >nul 2>&1
  if not errorlevel 1 (
    echo   [ok] port %1 - killed PID %%a
    set /a KILLED+=1
  )
)
exit /b 0
