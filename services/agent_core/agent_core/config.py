"""agent-core 配置（env 注入，ARCHITECTURE 8.1）。

MVP 冒烟零外部依赖默认值：
- MCP_OA_URL：mcp-oa 服务地址（Streamable HTTP）
- REDIS_URL：未配置时 HITL 确认存储回退进程内字典（生产必配）
- LLM_BASE_URL/LLM_API_KEY/LLM_MODEL：OpenAI 兼容 API（vLLM 或开发期网关）；
  未配置时意图识别/抽参走规则兜底，代码路径不变，生产切 vLLM 零改动
"""

import os


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class Settings:
    """集中读取环境变量（冒烟零依赖 → compose 注入 → 内网配置中心）。"""

    @property
    def mcp_oa_url(self) -> str:
        return env("MCP_OA_URL", "http://127.0.0.1:8001")

    @property
    def mcp_bi_url(self) -> str:
        return env("MCP_BI_URL", "http://127.0.0.1:8002")

    @property
    def mcp_crm_url(self) -> str:
        return env("MCP_CRM_URL", "http://127.0.0.1:8003")

    @property
    def mcp_erp_url(self) -> str:
        return env("MCP_ERP_URL", "http://127.0.0.1:8004")

    @property
    def mcp_wms_url(self) -> str:
        return env("MCP_WMS_URL", "http://127.0.0.1:8005")

    @property
    def redis_url(self) -> str | None:
        """REDIS_URL 未配置 → HITL 存储回退内存实现。"""
        return env("REDIS_URL") or None

    @property
    def llm_base_url(self) -> str | None:
        return env("LLM_BASE_URL") or None

    @property
    def llm_api_key(self) -> str:
        return env("LLM_API_KEY", "dummy")

    @property
    def llm_model(self) -> str:
        return env("LLM_MODEL", "qwen2.5-32b-instruct")

    @property
    def embedding_base_url(self) -> str | None:
        """EMBEDDING_BASE_URL：OpenAI 兼容 /embeddings 地址；未配置走本地降级向量。"""
        return env("EMBEDDING_BASE_URL") or None

    @property
    def embedding_api_key(self) -> str:
        return env("EMBEDDING_API_KEY", "dummy")

    @property
    def embedding_model(self) -> str:
        return env("EMBEDDING_MODEL", "text-embedding-3-small")

    @property
    def confirm_ttl_seconds(self) -> int:
        """HITL 确认卡 TTL（PRD 8.7：10 分钟）。"""
        return int(env("CONFIRM_TTL_SECONDS", "600"))


settings = Settings()
