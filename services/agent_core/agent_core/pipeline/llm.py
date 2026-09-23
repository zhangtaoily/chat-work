"""LLM 槽位抽取（OpenAI 兼容 /chat/completions + function calling）。

接入策略（规则优先，LLM 兜底）：
- LLM_BASE_URL 未配置 → enabled() 为 False，图内直接跳过，行为与纯规则完全一致；
- 已配置但调用失败 / 返回不合法 → 静默降级 None，调用方维持规则结果不变
  （降级风格对齐 knowledge/embedding.py：不阻塞对话主流程）；
- 图内两个接入点（pipeline/graph.py）：
  intent 节点规则关键词未命中时由小模型选技能；extract 节点规则未抽齐
  必填字段时由小模型补槽位（只补缺失、不覆盖规则值）。
"""

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from agent_core.config import settings
from agent_core.skills.registry import SKILLS

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10
# 执行体技能不参与对话路由（仅调度器经子图调用，同 registry 注释口径）
_EXCLUDED_SKILLS = {"send_reminder"}


def enabled() -> bool:
    """LLM_BASE_URL 未配置 → 抽取走纯规则，零行为变化。"""
    return settings.llm_base_url is not None


def _skill_enum() -> list[str]:
    """技能名枚举（function calling enum，chat 为闲聊兜底值）。"""
    return sorted(set(SKILLS) - _EXCLUDED_SKILLS | {"chat"})


def _tool_schema() -> dict[str, Any]:
    """extract_slots 工具定义：技能归类 + 通用槽位（规则层当前覆盖的字段）。"""
    return {
        "type": "function",
        "function": {
            "name": "extract_slots",
            "description": "从用户消息中识别业务技能意图并抽取结构化槽位",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "enum": _skill_enum(),
                        "description": "最匹配的技能名；无法归类为业务技能时填 chat",
                    },
                    "fields": {
                        "type": "object",
                        "description": "消息中明确出现的槽位键值对；未出现的槽位不要编造",
                        "properties": {
                            "remind_at": {
                                "type": "string",
                                "description": (
                                    "提醒/任务执行时刻，ISO 8601 本地时间"
                                    "（如 2026-09-23T17:00:00）"
                                ),
                            },
                            "content": {
                                "type": "string",
                                "description": "提醒或任务的正文",
                            },
                        },
                        "additionalProperties": True,
                    },
                },
                "required": ["skill"],
            },
        },
    }


def _system_prompt() -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return (
        f"当前时间：{now}。你是企业助手的意图识别与槽位抽取器。"
        "从用户消息中判断最匹配的业务技能并抽取槽位，"
        "只输出 extract_slots 函数调用；消息未明确出现的槽位一律省略。"
    )


async def _post_chat(message: str) -> dict[str, Any] | None:
    """调用 OpenAI 兼容 /chat/completions，解析 tool_calls 参数；无调用返回 None。"""
    base = settings.llm_base_url
    if base is None:
        return None
    body = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": message},
        ],
        "tools": [_tool_schema()],
        "tool_choice": "auto",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            base.rstrip("/") + "/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
        resp.raise_for_status()
        choice = resp.json()["choices"][0]
    calls = (choice.get("message") or {}).get("tool_calls") or []
    if not calls:
        return None
    return json.loads(calls[0]["function"]["arguments"])


async def extract_slots(message: str) -> dict[str, Any] | None:
    """LLM 槽位抽取入口：返回 {"skill": str|None, "fields": {...}}。

    None 表示「LLM 未配置 / 调用失败 / 未产生函数调用」，调用方维持规则结果。
    """
    if not message.strip():
        return None
    try:
        data = await _post_chat(message)
    except Exception:  # noqa: BLE001  LLM 故障静默降级规则路径
        logger.warning("llm extract_slots 降级规则路径", exc_info=True)
        return None
    if not data:
        return None
    result: dict[str, Any] = {"skill": None, "fields": {}}
    skill = data.get("skill")
    if isinstance(skill, str) and (skill in SKILLS or skill == "chat"):
        result["skill"] = skill
    fields = data.get("fields")
    if isinstance(fields, dict):
        result["fields"] = {
            k: v for k, v in fields.items() if isinstance(k, str) and v is not None
        }
    return result
