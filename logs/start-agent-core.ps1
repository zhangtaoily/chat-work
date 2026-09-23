# agent_core 后台启动器（会话外常驻，日志重定向到 logs/）
Start-Process -WindowStyle Hidden `
  -FilePath 'd:\prj\chat-work\services\agent_core\.venv\Scripts\python.exe' `
  -ArgumentList '-m', 'uvicorn', 'agent_core.api.main:app', '--host', '127.0.0.1', '--port', '8011' `
  -WorkingDirectory 'd:\prj\chat-work\services\agent_core' `
  -RedirectStandardOutput 'd:\prj\chat-work\logs\agent_core.out.log' `
  -RedirectStandardError 'd:\prj\chat-work\logs\agent_core.err.log'
