@echo off
rem Start mock OIDC IdP (local SSO login) on port 8012.
cd /d "%~dp0"
uv run python -m mock_idp
pause
