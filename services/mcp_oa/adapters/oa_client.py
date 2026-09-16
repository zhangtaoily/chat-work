"""OA OpenAPI 客户端（httpx）占位。

TODO:
- Service Account 调用 + 「代理人」双标记（PRD 8.5.5）
- 熔断：连续失败达到阈值即摘除（ARCHITECTURE 4.2 统一职责）
- 重试：幂等 GET 可安全重试；写入类不盲目重试（靠幂等键兜底）
- 超时与连接池上限
"""

import httpx

# OA 服务地址（占位，经环境变量注入）
OA_BASE_URL = "http://oa.example.internal/api"


class OaClient:
    """OA OpenAPI 客户端骨架。"""

    def __init__(self, base_url: str = OA_BASE_URL) -> None:
        # TODO: Service Account 认证头 + 熔断器初始化
        self._client = httpx.AsyncClient(base_url=base_url)

    async def aclose(self) -> None:
        """关闭底层连接。"""
        await self._client.aclose()
