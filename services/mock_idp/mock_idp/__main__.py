"""mock-idp 启动入口：uvicorn mock_idp.main:app --port 8012。

开发环境默认端口 8012（8011=agent-core；避免与 mcp 服务冲突）。
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("mock_idp.main:app", host="127.0.0.1", port=8012, reload=False)
