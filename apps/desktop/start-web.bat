@echo off
rem Start web preview (browser mode) on http://localhost:5173.
rem Prereq: run "pnpm install" at repo root once.
rem Backend: run start-all.bat at repo root first (hidden background services).
cd /d "%~dp0"
call pnpm dev:web
pause
