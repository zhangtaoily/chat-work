"""系统配置：功能开关 / MCP 注册健康探测 / 模型路由只读（PLAN P2.7，PRD 5.6）。

本包薄壳 re-export 对外 API（agent_core.syscfg.store）。
"""

from agent_core.syscfg.store import (
    get_toggle,
    list_mcp,
    list_toggles,
    model_view,
    probe_mcp,
    register_mcp,
    reset,
    restore,
    set_toggle,
)

__all__ = [
    "get_toggle",
    "list_mcp",
    "list_toggles",
    "model_view",
    "probe_mcp",
    "register_mcp",
    "reset",
    "restore",
    "set_toggle",
]
