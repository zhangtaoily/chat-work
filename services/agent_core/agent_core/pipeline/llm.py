"""LLM 槽位抽取（OpenAI 兼容 /chat/completions + function calling）。

接入策略（规则优先，LLM 兜底）：
- 模型配置来源：syscfg 模型注册表（管理后台可视化配置，员工自选 > 默认
  模型）→ 均未配置时回落 env LLM_BASE_URL → 仍无则 enabled() 为 False，
  图内直接跳过，行为与纯规则完全一致；
- 已配置但调用失败 / 返回不合法 → 静默降级 None，调用方维持规则结果不变
  （降级风格对齐 knowledge/embedding.py：不阻塞对话主流程）；
- 图内三个接入点（pipeline/graph.py）：
  intent 节点规则关键词未命中时由小模型选技能；extract 节点规则未抽齐
  必填字段时由小模型补槽位（只补缺失、不覆盖规则值）；format 节点技能
  未命中（闲聊轮）由小模型生成自然回复。
"""

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from agent_core.skills.registry import SKILLS

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10
# 执行体技能不参与对话路由（仅调度器经子图调用，同 registry 注释口径）
_EXCLUDED_SKILLS = {"send_reminder"}


def resolve_config(user_id: str | None = None) -> dict[str, str] | None:
    """当前生效的 LLM 配置：syscfg 注册表（员工自选 > 默认）→ env 兜底。

    返回 {name?, base_url, api_key, model}；全部未配置返回 None。
    syscfg 故障（如测试隔离外的意外状态）静默回落 env，不阻塞对话。
    """
    try:
        from agent_core.syscfg import store as syscfg_store

        entry = syscfg_store.resolve_model(user_id)
    except Exception:  # noqa: BLE001 — 配置域故障不阻塞对话主流程
        logger.warning("syscfg 模型解析失败，回落 env", exc_info=True)
        entry = None
    if entry is not None:
        return {
            "name": entry["name"],
            "base_url": entry["base_url"],
            "api_key": entry["api_key"] or "",
            "model": entry["model"],
        }
    from agent_core.config import settings

    if settings.llm_base_url is None:
        return None
    return {"base_url": settings.llm_base_url, "api_key": settings.llm_api_key or "", "model": settings.llm_model}


def enabled(user_id: str | None = None) -> bool:
    """未配置任何模型（注册表 + env 均无）→ 抽取走纯规则，零行为变化。"""
    return resolve_config(user_id) is not None


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
                            "tx_reason": {
                                "type": "integer",
                                "description": (
                                    "调休时长来源（0=加班/1=旅游/2=其他）；"
                                    "仅用户明确说明来源时填写"
                                ),
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


async def _post_chat(message: str, user_id: str | None = None) -> dict[str, Any] | None:
    """调用 OpenAI 兼容 /chat/completions，解析 tool_calls 参数；无调用返回 None。"""
    cfg = resolve_config(user_id)
    if cfg is None:
        return None
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": message},
        ],
        "tools": [_tool_schema()],
        "tool_choice": "auto",
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            cfg["base_url"].rstrip("/") + "/chat/completions",
            json=body,
            headers=headers,
        )
        resp.raise_for_status()
        choice = resp.json()["choices"][0]
    calls = (choice.get("message") or {}).get("tool_calls") or []
    if not calls:
        return None
    return json.loads(calls[0]["function"]["arguments"])


async def extract_slots(message: str, user_id: str | None = None) -> dict[str, Any] | None:
    """LLM 槽位抽取入口：返回 {"skill": str|None, "fields": {...}}。

    None 表示「LLM 未配置 / 调用失败 / 未产生函数调用」，调用方维持规则结果。
    """
    if not message.strip():
        return None
    try:
        data = await _post_chat(message, user_id)
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


def _chat_system_prompt() -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return (
        f"当前时间：{now}。你是企业工作助手 Chat-Work，负责请假申请、待办审批、"
        "库存查询、知识问答、定时提醒等事务。当前消息没有明确业务诉求，请正常聊天："
        "友好自然、简短（两三句以内）、使用中文；不要编造业务数据，"
        "需要办事时可提示你能处理的事务。"
    )


async def chat(message: str, user_id: str | None = None) -> str | None:
    """闲聊兜底（format 节点技能未命中轮）：由 LLM 生成自然回复。

    None 表示「LLM 未配置 / 调用失败 / 返回不合法」，调用方回落固定文案。
    """
    cfg = resolve_config(user_id)
    if cfg is None or not message.strip():
        return None
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": _chat_system_prompt()},
            {"role": "user", "content": message},
        ],
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                cfg["base_url"].rstrip("/") + "/chat/completions",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            choice = resp.json()["choices"][0]
    except Exception:  # noqa: BLE001  LLM 故障静默降级固定文案
        logger.warning("llm chat 降级固定文案", exc_info=True)
        return None
    content = (choice.get("message") or {}).get("content")
    return content if isinstance(content, str) and content.strip() else None
