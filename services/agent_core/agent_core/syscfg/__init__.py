"""系统配置：功能开关 / MCP 注册健康探测 / LLM 模型注册表（PLAN P2.7，PRD 5.6）。

本包薄壳 re-export 对外 API（agent_core.syscfg.store）。
"""

from agent_core.syscfg.store import (
    default_model,
    delete_model,
    get_toggle,
    get_user_model,
    list_mcp,
    list_models,
    list_toggles,
    model_view,
    probe_mcp,
    probe_models,
    register_mcp,
    register_model,
    reset,
    resolve_model,
    restore,
    set_default_model,
    set_model_enabled,
    set_toggle,
    set_user_model,
)

__all__ = [
    "default_model",
    "delete_model",
    "get_toggle",
    "get_user_model",
    "list_mcp",
    "list_models",
    "list_toggles",
    "model_view",
    "probe_mcp",
    "probe_models",
    "register_mcp",
    "register_model",
    "reset",
    "resolve_model",
    "restore",
    "set_default_model",
    "set_model_enabled",
    "set_toggle",
    "set_user_model",
]
