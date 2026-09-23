@echo off
rem Start the desktop app (Electron) in dev mode.
rem Prereq: run "pnpm install" at repo root once.
rem Backend: run start-all.bat at repo root first (hidden background services).
cd /d "%~dp0"
call pnpm dev
pause
