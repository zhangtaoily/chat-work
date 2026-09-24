"""系统配置域：功能开关 / MCP 注册与健康探测 / LLM 模型注册表（PLAN P2.7，PRD 5.6）。

三块能力（系统配置工作台后端）：
1. 功能开关（syscfg_toggle 审计）：已知键白名单；automation.scheduler_enabled
   实接线——关闭时 lifespan 不启动调度器、新建任务不挂 job，set_toggle 即时
   启停（懒导入 automation 防循环依赖）。
2. MCP 服务：env 五个 MCP 地址构成内置注册表，register_mcp 允许管理员
   追加/改址（builtin 标记保留）；probe_mcp 并发 GET {url}/health 探活
   （超时 3s，上报时延与健康状态）。
3. LLM 模型注册表（可视化配置）：管理员网页注册多个 OpenAI 兼容模型
   （base_url/api_key/model），支持启停/探活/设默认；员工可自选使用哪个
   模型（user_models 偏好）。生效优先级：员工自选 > 默认模型 > env
   （LLM_BASE_URL 未注册任何模型时零行为变化）。api_key 陈列一律打码，
   仅 pipeline 内部解析明文。

存储：进程内 dict + 可选 Redis 快照（syscfg:snapshot，写事件时镜像、
restore 恢复）——与 skills/audit 同风格；开关变更、MCP/模型注册、员工
切换均落审计（"审计者也被审计"，PRD 5.6.4）。
"""

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any

from agent_core import audit, persist

_SNAPSHOT_KEY = "syscfg:snapshot"

# 功能开关白名单（key → 说明；新增开关需同步接实线）
_KNOWN_TOGGLES: dict[str, str] = {
    "automation.scheduler_enabled": "自动化任务调度器总开关（关闭后新建任务不调度、已有任务暂停触发）",
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

_toggles: dict[str, bool] = {k: True for k in _KNOWN_TOGGLES}
_mcp_servers: dict[str, dict[str, Any]] = {}
_models: dict[str, dict[str, Any]] = {}
_default_model: str | None = None
_user_models: dict[str, str] = {}  # user_id → 模型名（员工自选偏好）
_redis_client: Any = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _get_redis() -> Any:
    """惰性初始化 Redis（与 audit/skills 同一单例约定；无 REDIS_URL 返回 None）。"""
    global _redis_client
    if _redis_client is None:
        from agent_core.config import settings

        if settings.redis_url:
            import redis.asyncio as aioredis

            _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _ensure_mcp() -> None:
    """MCP 注册表惰性基线：env 五个内置地址（PRD 2.2 跨系统服务面）。"""
    if _mcp_servers:
        return
    from agent_core.config import settings

    for name, url in (
        ("oa", settings.mcp_oa_url),
        ("bi", settings.mcp_bi_url),
        ("crm", settings.mcp_crm_url),
        ("erp", settings.mcp_erp_url),
        ("wms", settings.mcp_wms_url),
    ):
        _mcp_servers[name] = {"name": name, "url": url, "builtin": True, "registered_by": None, "registered_at": None}


async def _snapshot() -> None:
    """写事件后镜像快照（无 Redis 落本地文件兜底）。"""
    payload = {
        "toggles": _toggles,
        "mcp_servers": _mcp_servers,
        "models": _models,
        "default_model": _default_model,
        "user_models": _user_models,
    }
    r = _get_redis()
    if r is None:
        await persist.write_json(_SNAPSHOT_KEY, payload)
        return
    await r.set(_SNAPSHOT_KEY, json.dumps(payload, ensure_ascii=False))


async def restore() -> None:
    """启动恢复：Redis 快照优先，无 Redis 读本地文件兜底；否则默认基线（API lifespan 调用）。"""
    global _default_model
    _ensure_mcp()
    snap: dict[str, Any] | None = None
    r = _get_redis()
    if r is not None:
        raw = await r.get(_SNAPSHOT_KEY)
        if raw:
            snap = json.loads(raw)
    if snap is None:
        snap = await persist.read_json(_SNAPSHOT_KEY)
    if snap:
        for k, v in snap.get("toggles", {}).items():
            if k in _KNOWN_TOGGLES:
                _toggles[k] = bool(v)
        for name, entry in snap.get("mcp_servers", {}).items():
            if isinstance(entry, dict) and _NAME_RE.match(name):
                entry.setdefault("builtin", False)
                _mcp_servers[name] = entry
        for name, entry in snap.get("models", {}).items():
            if isinstance(entry, dict) and _NAME_RE.match(name):
                _models[name] = entry
        default = snap.get("default_model")
        if isinstance(default, str) and default in _models:
            _default_model = default
        for uid, name in snap.get("user_models", {}).items():
            if isinstance(uid, str) and isinstance(name, str) and name in _models:
                _user_models[uid] = name


def reset() -> None:
    """清空配置状态并重建默认基线（测试隔离用）。"""
    _toggles.clear()
    _toggles.update({k: True for k in _KNOWN_TOGGLES})
    _mcp_servers.clear()
    _models.clear()
    global _default_model
    _default_model = None
    _user_models.clear()
    _ensure_mcp()


# ---------- 1. 功能开关 ----------


def list_toggles() -> list[dict[str, Any]]:
    """全部开关及说明（工作台陈列）。"""
    return [{"key": k, "value": _toggles[k], "description": desc} for k, desc in _KNOWN_TOGGLES.items()]


def get_toggle(key: str) -> bool:
    """读开关值（未知键报错，避免静默按开处理）。"""
    if key not in _KNOWN_TOGGLES:
        raise ValueError(f"未知功能开关 {key}（可用：{'、'.join(_KNOWN_TOGGLES)}）")
    return _toggles[key]


async def set_toggle(key: str, value: bool, *, by: str, note: str = "") -> dict[str, Any]:
    """改开关并接实线（scheduler_enabled 即时启停调度器），变更落审计。"""
    if key not in _KNOWN_TOGGLES:
        raise ValueError(f"未知功能开关 {key}（可用：{'、'.join(_KNOWN_TOGGLES)}）")
    if not isinstance(value, bool):
        raise ValueError("开关值必须是布尔")  # noqa: TRY004 - 统一 ValueError→API 400
    if _toggles[key] == value:  # 幂等：重复设置同值不落审计不动作
        return {"key": key, "value": value, "changed": False}
    _toggles[key] = value
    await _snapshot()
    await audit.record(
        "syscfg_toggle",
        tool="syscfg",
        params={"key": key, "value": value},
        user_id=by or None,
        result="success",
        detail=f"功能开关 {key} → {value}（{_KNOWN_TOGGLES[key]}）{('：' + note) if note else ''}",
    )
    if key == "automation.scheduler_enabled":  # 实接线：懒导入防循环依赖
        from agent_core.automation import store as automation

        if value:
            automation.start_scheduler()
        else:
            automation.stop_scheduler()
    return {"key": key, "value": value, "changed": True}


# ---------- 2. MCP 注册与健康探测 ----------


def list_mcp() -> list[dict[str, Any]]:
    """全部 MCP 注册项（按名排序，工作台陈列）。"""
    _ensure_mcp()
    return [dict(_mcp_servers[name]) for name in sorted(_mcp_servers)]


async def register_mcp(name: str, url: str, *, by: str) -> dict[str, Any]:
    """追加/改址 MCP 服务（内置五项可改址不可更名），注册动作落审计。"""
    _ensure_mcp()
    if not _NAME_RE.match(name):
        raise ValueError("服务名须为小写字母开头的 1-32 位 [a-z0-9_-]")
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL 必须以 http:// 或 https:// 开头")
    existed = name in _mcp_servers
    _mcp_servers[name] = {
        "name": name,
        "url": url.rstrip("/"),
        "builtin": _mcp_servers.get(name, {}).get("builtin", False),
        "registered_by": by,
        "registered_at": _now(),
    }
    await _snapshot()
    await audit.record(
        "syscfg_mcp_register",
        tool="syscfg",
        params={"name": name, "url": url},
        user_id=by or None,
        result="success",
        detail=f"{'更新' if existed else '注册'} MCP 服务 {name} → {url}",
    )
    return dict(_mcp_servers[name])


async def probe_mcp(*names: str) -> dict[str, Any]:
    """并发探活 GET {url}/health（超时 3s）；不指定则探测全部。

    只读健康检查，不落审计（避免轮询噪音）；结果含时延与错误摘要。
    """
    import httpx

    _ensure_mcp()
    targets = [n for n in (names or sorted(_mcp_servers)) if n in _mcp_servers]
    unknown = [n for n in (names or []) if n not in _mcp_servers]
    if unknown:
        raise ValueError(f"未知 MCP 服务：{'、'.join(unknown)}")

    async def _probe(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
        url = _mcp_servers[name]["url"]
        started = datetime.now(UTC)
        try:
            resp = await client.get(f"{url}/health", timeout=3.0)
            latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
            return {"name": name, "url": url, "healthy": resp.status_code == 200, "status": resp.status_code, "latency_ms": latency, "error": None}
        except Exception as exc:  # noqa: BLE001 — 探活对任意网络异常统一降级为不健康
            return {"name": name, "url": url, "healthy": False, "status": None, "latency_ms": None, "error": str(exc) or type(exc).__name__}

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(_probe(client, n) for n in targets))
    return {"checked_at": _now(), "results": list(results)}


# ---------- 3. LLM 模型注册表（可视化配置 + 员工自选） ----------


def _mask_key(api_key: str) -> str:
    """api_key 打码陈列：只露末 4 位。"""
    if not api_key:
        return "未配置（免密）"
    return f"已配置（****{api_key[-4:]}）" if len(api_key) >= 4 else "已配置"


def _masked(entry: dict[str, Any]) -> dict[str, Any]:
    view = dict(entry)
    view["api_key_masked"] = _mask_key(view.pop("api_key"))
    return view


def _get_model(name: str) -> dict[str, Any]:
    entry = _models.get(name)
    if entry is None:
        raise ValueError(f"未知模型 {name}（已注册：{'、'.join(sorted(_models)) or '无'}）")
    return entry


def default_model() -> str | None:
    """当前默认模型名（未设置返回 None → 生效走 env 兜底）。"""
    return _default_model


def list_models(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    """全部模型（按名排序；api_key 打码；enabled_only 供员工切换清单）。"""
    items = [dict(e) for e in _models.values() if (e["enabled"] or not enabled_only)]
    items.sort(key=lambda e: e["name"])
    return [_masked(e) for e in items]


def get_user_model(user_id: str) -> str | None:
    """员工自选模型名（未自选/已失效返回 None → 跟随默认）。"""
    name = _user_models.get(user_id)
    return name if name and name in _models else None


async def set_user_model(user_id: str, name: str | None, *, by: str | None = None) -> dict[str, Any]:
    """员工自选对话模型（None = 跟随默认），变更落审计。

    幂等：与当前偏好相同不动作；指向不存在/停用模型报错。
    """
    if not user_id:
        raise ValueError("缺少用户身份")
    if name is None:
        if user_id not in _user_models:
            return {"user_id": user_id, "model": None, "changed": False}
        old = _user_models.pop(user_id)
        await _snapshot()
        await audit.record(
            "syscfg_user_model",
            tool="syscfg",
            params={"user_id": user_id, "model": None},
            user_id=by or user_id or None,
            result="success",
            detail=f"{user_id} 取消自选模型，恢复跟随默认（原 {old}）",
        )
        return {"user_id": user_id, "model": None, "changed": True}
    _get_model(name)
    if not _models[name]["enabled"]:
        raise ValueError(f"模型 {name} 已停用，不可选择")
    if _user_models.get(user_id) == name:
        return {"user_id": user_id, "model": name, "changed": False}
    _user_models[user_id] = name
    await _snapshot()
    await audit.record(
        "syscfg_user_model",
        tool="syscfg",
        params={"user_id": user_id, "model": name},
        user_id=by or user_id or None,
        result="success",
        detail=f"{user_id} 自选对话模型 → {name}",
    )
    return {"user_id": user_id, "model": name, "changed": True}


async def register_model(
    name: str,
    base_url: str,
    model: str,
    api_key: str = "",
    *,
    by: str,
) -> dict[str, Any]:
    """注册/更新 OpenAI 兼容模型（更新保留启停状态与既有偏好），落审计。"""
    if not _NAME_RE.match(name):
        raise ValueError("模型名须为小写字母开头的 1-32 位 [a-z0-9_-]")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    if not model.strip():
        raise ValueError("model 名不能为空")
    existed = name in _models
    _models[name] = {
        "name": name,
        "base_url": base_url.rstrip("/"),
        "model": model.strip(),
        "api_key": api_key.strip(),
        "enabled": _models[name]["enabled"] if existed else True,
        "registered_by": by,
        "registered_at": _now(),
    }
    await _snapshot()
    await audit.record(
        "syscfg_model_register",
        tool="syscfg",
        params={"name": name, "base_url": base_url, "model": model},
        user_id=by or None,
        result="success",
        detail=f"{'更新' if existed else '注册'} LLM 模型 {name}（{model} @ {base_url}）",
    )
    return _masked(_models[name])


async def delete_model(name: str, *, by: str) -> dict[str, Any]:
    """删除模型：级联清默认指针与员工偏好，落审计。"""
    _get_model(name)
    del _models[name]
    global _default_model
    if _default_model == name:
        _default_model = None
    dropped = [uid for uid, m in _user_models.items() if m == name]
    for uid in dropped:
        _user_models.pop(uid)
    await _snapshot()
    await audit.record(
        "syscfg_model_delete",
        tool="syscfg",
        params={"name": name},
        user_id=by or None,
        result="success",
        detail=f"删除 LLM 模型 {name}（级联清理偏好 {len(dropped)} 人）",
    )
    return {"name": name, "deleted": True, "user_prefs_dropped": len(dropped)}


async def set_default_model(name: str, *, by: str) -> dict[str, Any]:
    """设默认模型（未自选的员工都走它），落审计。"""
    _get_model(name)
    if not _models[name]["enabled"]:
        raise ValueError(f"模型 {name} 已停用，请先启用再设默认")
    global _default_model
    if _default_model == name:
        return {"name": name, "changed": False}
    _default_model = name
    await _snapshot()
    await audit.record(
        "syscfg_model_default",
        tool="syscfg",
        params={"name": name},
        user_id=by or None,
        result="success",
        detail=f"默认对话模型 → {name}",
    )
    return {"name": name, "changed": True}


async def set_model_enabled(name: str, value: bool, *, by: str) -> dict[str, Any]:
    """启停模型（停用后员工偏好自动回落默认/env），落审计。"""
    _get_model(name)
    if _models[name]["enabled"] == value:
        return {"name": name, "enabled": value, "changed": False}
    _models[name]["enabled"] = value
    await _snapshot()
    await audit.record(
        "syscfg_model_toggle",
        tool="syscfg",
        params={"name": name, "enabled": value},
        user_id=by or None,
        result="success",
        detail=f"模型 {name} {'启用' if value else '停用'}",
    )
    return {"name": name, "enabled": value, "changed": True}


def resolve_model(user_id: str | None = None) -> dict[str, Any] | None:
    """解析某员工当前生效的模型配置（明文）。

    优先级：员工自选（须存在且启用）> 默认模型（须启用）> None
    （调用方回落 env，未注册任何模型时零行为变化）。
    """
    pref = _user_models.get(user_id or "")
    if pref and pref in _models and _models[pref]["enabled"]:
        return _models[pref]
    if _default_model and _default_model in _models and _models[_default_model]["enabled"]:
        return _models[_default_model]
    return None


async def probe_models(*names: str) -> dict[str, Any]:
    """连通性探测：GET {base_url}/models（超时 5s；带 Bearer 须有 api_key）。"""
    import httpx

    targets = [n for n in (names or sorted(_models)) if n in _models]
    unknown = [n for n in (names or []) if n not in _models]
    if unknown:
        raise ValueError(f"未知模型：{'、'.join(unknown)}")

    async def _probe(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
        entry = _models[name]
        url = entry["base_url"]
        started = datetime.now(UTC)
        try:
            headers = {"Authorization": f"Bearer {entry['api_key']}"} if entry["api_key"] else {}
            resp = await client.get(f"{url}/models", timeout=5.0, headers=headers)
            latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
            return {"name": name, "url": url, "healthy": resp.status_code == 200, "status": resp.status_code, "latency_ms": latency, "error": None}
        except Exception as exc:  # noqa: BLE001 — 探测对任意网络异常统一降级为不健康
            return {"name": name, "url": url, "healthy": False, "status": None, "latency_ms": None, "error": str(exc) or type(exc).__name__}

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(_probe(client, n) for n in targets))
    return {"checked_at": _now(), "results": list(results)}


def model_view() -> dict[str, Any]:
    """系统配置页陈列：模型表（打码）+ 默认指针 + env 兜底状态。"""
    from agent_core.config import settings

    return {
        "models": list_models(),
        "default": _default_model,
        "env_fallback": {
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "configured": settings.llm_base_url is not None,
        },
        "note": "未注册任何模型或已全部停用时，LLM 通道回落环境变量 LLM_BASE_URL（未配置则纯规则，零行为变化）；Embedding 通道仍走环境变量 EMBEDDING_BASE_URL",
    }
