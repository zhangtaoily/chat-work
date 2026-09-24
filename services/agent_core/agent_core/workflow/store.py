"""Workflow Store：跨系统编排域模型与执行引擎（PLAN P2.6，PRD 3.2.2/6.3）。

定位：声明式步骤编排（steps 按序 DAG）的定义存储 + 运行记录 + 执行
引擎。「独立 workflow 域」（用户确认）：编排定义独立管理、独立生命
周期（active/disabled），技能与自动化/事件触发通过 workflow_id 引用
（官方编排技能 p2-6c 落地）；既有 _execute_tools 的技能内硬编码分支
不动——技能内编排（单技能多工具）与跨系统编排（workflow 域）并存。

编排结构（PRD 3.2.2 flow JSON 扩展运行元数据）：
    {id: wf_{seq:06d}, name, description, steps, mode, status, owner,
     owner_auth 快照, scope, created_at, updated_at, stats}
steps（声明式，按序执行，PRD 3.2.2）：
    {seq, tool, desc, args, rw, requires_confirm}
  - tool：MCP 工具名，仅限契约池白名单（packages/protocol/tools 15 工具）
  - args：参数模板，占位符实现步骤间上下文传递——{{input.x}} 引用
    输入参数、{{step_N.x}} 引用前步输出（点路径取值；全匹配替换为
    对象本身，内嵌出现做字符串内插；解析失败该步失败即中止）
  - rw：自动从契约池推断（不接受手传）；写工具自动注入 user_id 与
    idempotency_key（wf{run_id}_{seq} 确定性键：挂起→确认同键重试
    幂等命中，PRD 8.7）
  - requires_confirm：写工具强制 True（PRD 3.3 规则2：写入步骤无论
    什么模式强制走 HITL 确认卡，模式不豁免单步确认）；读工具默认
    False，可显式开启

三模式（PRD 3.3，技能层 p2-6b 复用）：ask 只跑读步骤（遇写步骤
partial 止步）/ plan 先出执行计划卡批准后执行 / craft 立即执行；
写步骤一律挂确认卡（confirm_store 复用，TTL 10 分钟）。默认 plan
（PRD 6.3 跨系统编排口径）。

防护栏（对齐 automation/store）：单次执行超时 30 分钟；运行记录/
通知截尾 500；科室编排禁 craft 模式（PRD 3.3 规则3）。

存储：进程内 dict/list + 快照（workflow:snapshot：REDIS_URL 配置走
Redis，未配置落本地文件兜底；写事件镜像 + restore 恢复 + 截尾）——
与 automation/skills store 同风格。
"""

import asyncio
import json
import re
import time
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from agent_core import audit, persist, wecom
from agent_core.guardrail import confirm_store

_SNAPSHOT_KEY = "workflow:snapshot"

_WF_STATUSES = ("active", "disabled")
_MODES = ("ask", "plan", "craft")
_SCOPES = ("official", "dept")  # 个人编排 Phase 3（对齐技能 store 口径）
_EXEC_TIMEOUT_S = 30 * 60  # 防护栏：单次执行超时终止（对齐 automation）
_RUNS_KEEP = 500  # 快照截尾：运行记录/通知保留条数
_RUN_STATUS_TEXT = {
    "ok": "执行完成",
    "partial": "部分执行（Ask 模式止步于写步骤）",
    "failed": "执行失败",
    "cancelled": "已取消（写步骤被拒绝）",
}

# 契约池（packages/protocol/tools/*.json，15 工具）：编排步骤工具白名单
_WRITE_TOOLS = frozenset({
    "oa__submit_leave_request",
    "oa__approve",
    "crm__submit_sales_order",
})
TOOL_CONTRACTS = frozenset({
    # oa（5）
    "oa__query_leave_balance",
    "oa__submit_leave_request",
    "oa__query_pending_approvals",
    "oa__approve",
    "oa__query_activity_log",
    # bi（1）
    "bi__execute_query",
    # crm（4）
    "crm__search_customers",
    "crm__get_customer_360",
    "crm__submit_sales_order",
    "crm__query_order_progress",
    # erp（3）
    "erp__query_inventory",
    "erp__query_purchase_orders",
    "erp__query_voucher_summary",
    # wms（2）
    "wms__query_stock_orders",
    "wms__query_stock_alerts",
})

# 占位符：{{input.x}} / {{step_1.customer_id}}（首段 input 或 step_N，点路径取值）
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_]\w*(?:\.[\w-]+)+)\s*\}\}")
_FULL_RE = re.compile(r"^\{\{\s*([a-zA-Z_]\w*(?:\.[\w-]+)+)\s*\}\}$")

_workflows: dict[str, dict[str, Any]] = {}
_runs: list[dict[str, Any]] = []
_notifications: list[dict[str, Any]] = []
_seq = 0  # 编排号自增（wf-*）
_run_seq = 0  # 运行号自增（wfr-*）
_msg_seq = 0  # 通知号自增
_redis_client: Any = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _get_redis() -> Any:
    """惰性初始化 Redis（与 audit/slots/automation store 同一单例约定）。"""
    global _redis_client
    if _redis_client is None:
        from agent_core.config import settings

        if settings.redis_url:
            import redis.asyncio as aioredis

            _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


async def _snapshot() -> None:
    """写事件后镜像快照（无 Redis 落本地文件兜底；运行记录/通知截尾防膨胀）。"""
    payload = {
        "workflows": _workflows,
        "runs": _runs[-_RUNS_KEEP:],
        "notifications": _notifications[-_RUNS_KEEP:],
        "seq": _seq,
        "run_seq": _run_seq,
        "msg_seq": _msg_seq,
    }
    r = _get_redis()
    if r is None:
        await persist.write_json(_SNAPSHOT_KEY, payload)
        return
    await r.set(_SNAPSHOT_KEY, json.dumps(payload, ensure_ascii=False))


async def restore() -> None:
    """启动恢复：Redis 快照优先，无 Redis 读本地文件兜底（API lifespan 调用）。"""
    global _seq, _run_seq, _msg_seq
    snap: dict[str, Any] | None = None
    r = _get_redis()
    if r is not None:
        raw = await r.get(_SNAPSHOT_KEY)
        if raw:
            snap = json.loads(raw)
    if snap is None:
        snap = await persist.read_json(_SNAPSHOT_KEY)
    if snap:
        _workflows.update(snap.get("workflows", {}))
        _runs.extend(snap.get("runs", []))
        _notifications.extend(snap.get("notifications", []))
        _seq = int(snap.get("seq", 0))
        _run_seq = int(snap.get("run_seq", 0))
        _msg_seq = int(snap.get("msg_seq", 0))


def reset() -> None:
    """清空全部编排/运行记录/通知（测试隔离用）。"""
    global _seq, _run_seq, _msg_seq
    _workflows.clear()
    _runs.clear()
    _notifications.clear()
    _seq = 0
    _run_seq = 0
    _msg_seq = 0


# ---- 参数模板：占位符解析（纯函数，可单测）----


def _lookup(ctx: dict[str, Any], path: str) -> Any:
    """按点路径从上下文取值（首段 input 或 step_N，其余为字段路径）。"""
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise ValueError(f"占位符 {{{{{path}}}}} 无法解析（输入参数或前步输出中不存在）")
    return cur


def resolve_value(value: Any, ctx: dict[str, Any]) -> Any:
    """渲染参数值：全匹配占位符替换为对象本身，内嵌占位符做字符串内插。"""
    if isinstance(value, str):
        full = _FULL_RE.match(value)
        if full:
            return _lookup(ctx, full.group(1))
        return _PLACEHOLDER_RE.sub(lambda m: str(_lookup(ctx, m.group(1))), value)
    if isinstance(value, dict):
        return {k: resolve_value(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_value(v, ctx) for v in value]
    return value


def resolve_args(template: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """渲染步骤参数模板（deepcopy 防模板污染，未声明占位符原样透传）。"""
    return {k: resolve_value(v, ctx) for k, v in deepcopy(template).items()}


# ---- steps 校验与计划卡（纯函数，可单测）----


def validate_steps(steps: Any) -> list[dict[str, Any]]:
    """steps 声明合法性（PRD 3.2.2/3.3），非法 raise ValueError。

    归一化：seq 按序重排 1..N；rw 从契约池推断；写工具强制
    requires_confirm=True（PRD 3.3 规则2）。
    """
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps 必须是非空数组")
    normalized: list[dict[str, Any]] = []
    for i, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str):
            raise ValueError(f"第 {i} 步必须是含 tool 的对象")  # noqa: TRY004 - 统一 ValueError→API 400
        tool = raw["tool"]
        if tool not in TOOL_CONTRACTS:
            raise ValueError(f"第 {i} 步工具 {tool} 不在契约池（15 工具白名单）")
        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError(f"第 {i} 步 args 必须是对象（参数模板支持输入参数/前步输出占位符）")  # noqa: TRY004 - 统一 ValueError→API 400
        rw = "write" if tool in _WRITE_TOOLS else "read"
        normalized.append({
            "seq": i,
            "tool": tool,
            "desc": str(raw.get("desc", "")),
            "args": deepcopy(args),
            "rw": rw,
            # 写工具强制确认卡；读工具默认不确认，可显式 requires_confirm=True
            "requires_confirm": True if rw == "write" else bool(raw.get("requires_confirm", False)),
        })
    return normalized


def render_plan(wf: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    """执行计划卡（PRD 3.3 Plan 模式）：各步骤系统/读改/参数预览。

    前步输出占位符此时无法解析（未执行）——预览置 None 并标注待定，
    不阻塞计划展示。
    """
    ctx: dict[str, Any] = {"input": inputs}
    steps = []
    for step in wf["steps"]:
        try:
            preview: Any = resolve_args(step["args"], ctx)
        except ValueError:
            preview = None  # 引用前步输出：执行期才可解析
        steps.append({
            "seq": step["seq"],
            "tool": step["tool"],
            "system": step["tool"].split("__", 1)[0].upper(),
            "desc": step["desc"],
            "rw": step["rw"],
            "requires_confirm": step["requires_confirm"],
            "args": preview,
        })
    return {
        "workflow_id": wf["id"],
        "name": wf["name"],
        "mode": wf["mode"],
        "steps": steps,
    }


# ---- 查询（API 消费，同步无 IO）----


def _require(wf_id: str) -> dict[str, Any]:
    wf = _workflows.get(wf_id)
    if wf is None:
        raise ValueError(f"编排 {wf_id} 不存在")
    return wf


def list_workflows(
    *, owner: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    """编排列表（新编排在前；owner/status 可选过滤）。"""
    items = [
        dict(w)
        for w in _workflows.values()
        if (owner is None or w["owner"] == owner) and (status is None or w["status"] == status)
    ]
    return sorted(items, key=lambda w: w["id"], reverse=True)


def detail(wf_id: str) -> dict[str, Any] | None:
    """编排详情（不存在返回 None）。"""
    wf = _workflows.get(wf_id)
    return dict(wf) if wf else None


def _find_run(run_id: str) -> dict[str, Any] | None:
    for r in reversed(_runs):
        if r["run_id"] == run_id:
            return r
    return None


def runs(wf_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """运行历史（新的在前；编排删除后仍可查，PRD 10 审计覆盖口径）。"""
    rows = [dict(r) for r in _runs if r["workflow_id"] == wf_id]
    return list(reversed(rows))[:limit]


def get_run(run_id: str) -> dict[str, Any] | None:
    """单次运行详情（不存在返回 None）。"""
    run = _find_run(run_id)
    return dict(run) if run else None


def notifications(user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """编排结果通知（新的在前；wecom 未配置前仅进程内信箱）。"""
    rows = [dict(m) for m in _notifications if m["user_id"] == user_id]
    return list(reversed(rows))[:limit]


def mark_notifications_read(user_id: str) -> int:
    """编排通知全部已读（返回标记条数）。"""
    n = 0
    for m in _notifications:
        if m["user_id"] == user_id and not m["read"]:
            m["read"] = True
            n += 1
    return n


# ---- 编排生命周期（写路径，async 以落 Redis 快照 + 审计）----


async def create(
    *,
    name: str,
    steps: list[dict[str, Any]],
    owner: str,
    owner_auth: dict[str, Any],
    description: str = "",
    mode: str = "plan",
    scope: str = "official",
) -> dict[str, Any]:
    """创建编排定义（API 注册与官方技能编排声明的共同入口）。

    防线：名称非空；steps 声明式合法（工具白名单/写步骤强制确认）；
    mode 合法；科室编排禁 craft（PRD 3.3 规则3）。
    """
    if not str(name).strip():
        raise ValueError("编排名称不能为空")
    if mode not in _MODES:
        raise ValueError(f"mode 仅支持 {'/'.join(_MODES)}")
    if scope not in _SCOPES:
        raise ValueError(f"scope 仅支持 {'/'.join(_SCOPES)}")
    if scope == "dept" and mode == "craft":
        raise ValueError("科室编排 mode 仅支持 ask/plan（PRD 3.3 规则3）")
    norm_steps = validate_steps(steps)
    global _seq
    _seq += 1
    wf: dict[str, Any] = {
        "id": f"wf_{_seq:06d}",
        "name": str(name).strip(),
        "description": str(description or ""),
        "steps": norm_steps,
        "mode": mode,
        "status": "active",
        "owner": owner,
        "owner_auth": {k: owner_auth.get(k) for k in ("user_id", "dept", "roles")},
        "scope": scope,
        "created_at": _now(),
        "updated_at": _now(),
        "stats": {"runs": 0, "success": 0, "failed": 0},
    }
    _workflows[wf["id"]] = wf
    await audit.record(
        "workflow_create",
        tool="workflow",
        params={"workflow_id": wf["id"], "steps": len(norm_steps), "mode": mode},
        user_id=owner,
        result="ok",
        detail=f"创建跨系统编排「{wf['name']}」（{len(norm_steps)} 步）",
    )
    await _snapshot()
    return dict(wf)


async def set_status(wf_id: str, *, status: str, by: str) -> dict[str, Any]:
    """启停编排（停用后技能/自动化/事件触发均不可执行）。"""
    if status not in _WF_STATUSES:
        raise ValueError(f"status 仅支持 {'/'.join(_WF_STATUSES)}")
    wf = _require(wf_id)
    if wf["status"] == status:
        raise ValueError(f"编排当前已是 {status} 状态")
    wf["status"] = status
    wf["updated_at"] = _now()
    await audit.record(
        "workflow_enable" if status == "active" else "workflow_disable",
        tool="workflow",
        params={"workflow_id": wf_id},
        user_id=by,
        result="ok",
        detail=f"{'启用' if status == 'active' else '停用'}编排「{wf['name']}」",
    )
    await _snapshot()
    return dict(wf)


async def delete(wf_id: str, *, by: str) -> None:
    """删除编排定义（运行记录保留，PRD 10 审计覆盖口径）。"""
    wf = _require(wf_id)
    del _workflows[wf_id]
    await audit.record(
        "workflow_delete",
        tool="workflow",
        params={"workflow_id": wf_id},
        user_id=by,
        result="ok",
        detail=f"删除编排「{wf['name']}」",
    )
    await _snapshot()


# ---- 官方编排 seed（PLAN P2.6 p2-6c，PRD 6.3 跨系统一条龙）----

OFFICIAL_ORDER_FLOW = "跨系统订单一条龙"

# PRD 6.3 口径：CRM 销售订单录入 → 提交审批（CRM 录单自动生成审批流，
# 录单后查审批/节点进度）→ 触发 WMS 备货（出库单参考）→ 通知业务员
# （execute 终态 _notify 发起编排的销售员，进程内信箱 / p2-6e wecom）。
# 首步写操作强制确认卡（PRD 3.3 规则2，模式不豁免单步确认）。
_OFFICIAL_ORDER_STEPS = [
    {
        "tool": "crm__submit_sales_order",
        "desc": "CRM 录入销售订单（提交后自动生成审批流）",
        "args": {
            "order_type": "{{input.order_type}}",
            "customer_id": "{{input.customer_id}}",
            "contact": "{{input.contact}}",
            "address": "{{input.address}}",
            "items": "{{input.items}}",
            "delivery_date": "{{input.delivery_date}}",
            "payment_term": "{{input.payment_term}}",
        },
    },
    {
        "tool": "crm__query_order_progress",
        "desc": "CRM 查询订单审批与节点进度",
        "args": {"order_no": "{{step_1.doc_no}}"},
    },
    {
        "tool": "wms__query_stock_orders",
        "desc": "WMS 查询备货出库单（备货参考）",
        "args": {"order_type": "outbound"},
    },
]


async def seed_official() -> dict[str, Any]:
    """官方跨系统编排声明（幂等：同名官方编排已存在直接返回）。

    API lifespan 启动 seed；编排技能执行前兜底调用（编排被删除后自愈）。
    """
    for wf in _workflows.values():
        if wf["scope"] == "official" and wf["name"] == OFFICIAL_ORDER_FLOW:
            return dict(wf)
    return await create(
        name=OFFICIAL_ORDER_FLOW,
        steps=deepcopy(_OFFICIAL_ORDER_STEPS),
        owner="system",
        owner_auth={"user_id": "system", "dept": "", "roles": []},
        description="CRM 销售订单录入 → 审批进度查询 → WMS 备货参考 → 通知业务员",
        mode="plan",
        scope="official",
    )


# ---- 执行引擎：逐步执行 + 写步骤 HITL 挂起/恢复 ----


async def _call_tool(tool_name: str, args: dict[str, Any]) -> Any:
    """按工具前缀路由到对应 MCP Server（与 _execute_tools 分发口径一致）。"""
    from agent_core.mcp_client import McpToolError
    from agent_core.mcp_client.bi import call_bi_tool
    from agent_core.mcp_client.crm import call_crm_tool
    from agent_core.mcp_client.erp import call_erp_tool
    from agent_core.mcp_client.oa import call_oa_tool
    from agent_core.mcp_client.wms import call_wms_tool

    callers = {
        "oa": call_oa_tool,
        "bi": call_bi_tool,
        "crm": call_crm_tool,
        "erp": call_erp_tool,
        "wms": call_wms_tool,
    }
    caller = callers.get(tool_name.split("__", 1)[0])
    if caller is None:
        raise McpToolError(f"工具 {tool_name} 无对应系统客户端")
    return await caller(tool_name, args)


def _step_summary(result: Any) -> str:
    """步骤结果摘要（run 历史/通知用，截断 500）。"""
    try:
        text = json.dumps(result, ensure_ascii=False)
    except TypeError:
        text = str(result)
    return text[:500]


def _finalize_run(
    run: dict[str, Any], wf: dict[str, Any] | None, status: str, duration_ms: int
) -> None:
    """运行终态收尾：状态/耗时/统计回写（编排已删除时跳过统计）。"""
    run["status"] = status
    run["ok"] = status in ("ok", "partial")
    run["duration_ms"] = duration_ms
    if wf is not None:
        if run["ok"]:
            wf["stats"]["success"] += 1
        else:
            wf["stats"]["failed"] += 1


async def _notify(wf: dict[str, Any], run: dict[str, Any], *, text: str) -> None:
    """编排结果通知（owner 收件）。

    双通道（PLAN P2.6 p2-6e，PRD 14 章）：WECOM_WEBHOOK_URL 已配置时推
    企微群机器人（与 automation._deliver 共用 wecom.push_markdown 通道，
    PRD 6.3 通知业务员）；未配置或推送失败降级进程内信箱（零依赖默认值）。
    """
    if await wecom.push_markdown(text):
        return
    global _msg_seq
    _msg_seq += 1
    _notifications.append(
        {
            "id": f"wf-msg-{_msg_seq:06d}",
            "kind": "result",
            "workflow_id": wf["id"],
            "workflow_name": wf["name"],
            "run_id": run["run_id"],
            # 通知业务员（PRD 6.3）：技能/手动触发优先发操作者；system
            # 触发（自动化/事件，user_id 缺省）回落编排 owner
            "user_id": run.get("actor") or wf["owner"],
            "ok": run["ok"],
            "text": (text or "")[:2000],
            "run_at": _now(),
            "read": False,
        }
    )


def _publish_finished(run: dict[str, Any], status: str) -> None:
    """编排终态发布系统事件（PLAN P2.6 p2-6d，PRD 3.6.1）：供 event
    自动化任务订阅。publish_event 同步无 IO（纯入队），无失败路径；
    延迟 import 防与 automation 执行管线循环引用。"""
    from agent_core.automation import store as automation_store

    automation_store.publish_event(
        "workflow.run.finished",
        payload={
            "workflow_id": run["workflow_id"],
            "workflow_name": run["workflow_name"],
            "run_id": run["run_id"],
            "status": status,
        },
    )


async def _run_steps(
    wf: dict[str, Any],
    run: dict[str, Any],
    ctx: dict[str, Any],
    *,
    actor: str | None,
    session: str,
    actor_auth: dict[str, Any],
    events: list[dict[str, Any]],
    start_index: int = 0,
    read_only: bool = False,
    resume: bool = False,
) -> str:
    """逐步执行 steps[start_index:]，返回终止状态（ok/partial/failed/pending_confirm）。

    写步骤（requires_confirm）→ confirm_card 挂起，恢复从本步续跑；
    任一步失败即中止（不做回滚——写操作以确定性幂等键兜底重试，PRD 8.7）。
    resume=True（确认恢复路径）：起始步已批准，跳过其确认门直接执行
    （幂等键与挂起时同键，重复提交幂等命中）；后续写步骤仍正常挂卡。
    """
    steps = wf["steps"]
    last_result: Any = None
    for idx in range(start_index, len(steps)):
        step = steps[idx]
        try:
            args = resolve_args(step["args"], ctx)
        except ValueError as exc:
            run["steps"].append(
                {"seq": step["seq"], "tool": step["tool"], "ok": False, "detail": str(exc)}
            )
            return "failed"
        if step["tool"] in _WRITE_TOOLS:
            # 写工具自动注入操作者与幂等键（模板显式声明则尊重模板）
            args.setdefault("user_id", actor)
            args.setdefault("idempotency_key", f"{run['run_id']}_{step['seq']}")
        if read_only and (step["tool"] in _WRITE_TOOLS or step["requires_confirm"]):
            run["steps"].append(
                {"seq": step["seq"], "tool": step["tool"], "ok": True, "detail": "Ask 模式止步于写步骤（未执行）"}
            )
            run["outputs"] = last_result
            return "partial"
        if step["requires_confirm"] and not (resume and idx == start_index):
            token = uuid.uuid4().hex
            snap = {
                "workflow_id": wf["id"],
                "run_id": run["run_id"],
                "step_index": idx,
                "ctx": ctx,
                "actor_auth": actor_auth or {},
                "user_id": actor,
                "session_id": session,
                "trigger": run["trigger"],
                "args": args,
            }
            await confirm_store.set_token(token, {"workflow": snap})
            events.append(
                {
                    "type": "confirm_card",
                    "confirm_token": token,
                    "payload": {
                        "workflow_title": wf["name"],
                        "step": {"seq": step["seq"], "desc": step["desc"]},
                        "fields": args,
                    },
                    "expires_at": int(time.time() * 1000) + 10 * 60 * 1000,
                }
            )
            await audit.record(
                "confirm_issued",
                user_id=actor,
                session_id=session,
                tool=step["tool"],
                params=args,
                detail=f"编排「{wf['name']}」第 {step['seq']} 步等待确认",
            )
            run["status"] = "pending_confirm"
            run["pending"] = {"confirm_token": token, "step_index": idx}
            return "pending_confirm"
        try:
            result = await _call_tool(step["tool"], args)
        except Exception as exc:  # noqa: BLE001 - 步骤失败即中止：任何异常计该步失败
            run["steps"].append(
                {"seq": step["seq"], "tool": step["tool"], "ok": False, "detail": str(exc)[:500]}
            )
            return "failed"
        last_result = result
        ctx[f"step_{step['seq']}"] = result  # 上下文传递：后步 {{step_N.x}} 引用
        run["steps"].append(
            {"seq": step["seq"], "tool": step["tool"], "ok": True, "detail": _step_summary(result)}
        )
    run["outputs"] = last_result
    return "ok"


async def execute(
    wf_id: str,
    inputs: dict[str, Any],
    *,
    trigger: str = "manual",
    actor_auth: dict[str, Any] | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    mode: str | None = None,
    plan_approved: bool = False,
) -> dict[str, Any]:
    """执行编排（技能层/API/事件触发的共同入口，PRD 3.3 三模式）。

    返回分形：
    - plan 未批准：{"status": "plan", "plan": 计划卡}（不产生运行记录）
    - 写步骤挂起：{"status": "pending_confirm", "run", "confirm_token", "events"}
    - 完成：{"status": "ok|partial|failed", "run", "events"}

    防护栏：非 active 拒绝执行；单次执行 30 分钟超时终止。
    """
    wf = _require(wf_id)
    if wf["status"] != "active":
        raise ValueError(f"编排 {wf_id} 已停用（当前 {wf['status']}），不可执行")
    if not isinstance(inputs, dict):
        raise ValueError("inputs 必须是对象")  # noqa: TRY004 - 统一 ValueError→API 400
    eff_mode = mode or wf["mode"]
    if eff_mode not in _MODES:
        raise ValueError(f"mode 仅支持 {'/'.join(_MODES)}")
    if eff_mode == "plan" and not plan_approved:
        return {"status": "plan", "plan": render_plan(wf, inputs)}
    auth = actor_auth or {}
    actor = user_id or auth.get("user_id")
    global _run_seq
    _run_seq += 1
    session = session_id or f"wf_{wf_id}_{_run_seq:06d}"
    run: dict[str, Any] = {
        "run_id": f"wfr_{_run_seq:06d}",
        "workflow_id": wf_id,
        "workflow_name": wf["name"],
        "trigger": trigger,
        "run_at": _now(),
        "ok": False,
        "status": "running",
        "duration_ms": 0,
        "session_id": session,
        "actor": actor,
        "steps": [],
        "outputs": None,
    }
    _runs.append(run)
    wf["stats"]["runs"] += 1
    events: list[dict[str, Any]] = []
    ctx: dict[str, Any] = {"input": deepcopy(inputs)}
    started = time.monotonic()
    try:
        status = await asyncio.wait_for(
            _run_steps(
                wf, run, ctx,
                actor=actor, session=session, actor_auth=auth, events=events,
                read_only=eff_mode == "ask",
            ),
            _EXEC_TIMEOUT_S,
        )
    except TimeoutError:
        status = "failed"
        run["steps"].append(
            {"seq": None, "tool": "", "ok": False, "detail": "执行超时（30 分钟上限，已终止）"}
        )
    except Exception as exc:  # noqa: BLE001 - 执行器兜底：任何异常都计一次失败
        status = "failed"
        run["steps"].append({"seq": None, "tool": "", "ok": False, "detail": f"执行异常：{exc}"})
    duration_ms = int((time.monotonic() - started) * 1000)
    if status == "pending_confirm":
        run["duration_ms"] = duration_ms
        await audit.record(
            "workflow_run",
            tool="workflow",
            params={"workflow_id": wf_id, "trigger": trigger, "run_id": run["run_id"]},
            user_id=actor,
            session_id=session,
            result="ok",
            detail=f"编排「{wf['name']}」等待写步骤确认",
        )
        await _snapshot()
        return {
            "status": "pending_confirm",
            "run": dict(run),
            "confirm_token": run["pending"]["confirm_token"],
            "events": events,
        }
    _finalize_run(run, wf, status, duration_ms)
    text = (
        f"编排「{wf['name']}」{_RUN_STATUS_TEXT.get(status, status)}："
        f"{len(run['steps'])}/{len(wf['steps'])} 步"
    )
    await _notify(wf, run, text=text)
    _publish_finished(run, status)  # 旁路事件：订阅 workflow.run.finished 的自动化任务
    await audit.record(
        "workflow_run",
        tool="workflow",
        params={"workflow_id": wf_id, "trigger": trigger, "run_id": run["run_id"]},
        user_id=actor,
        session_id=session,
        result="ok" if run["ok"] else "failed",
        detail=text,
    )
    await _snapshot()
    return {"status": status, "run": dict(run), "events": events}


async def resume_confirm(
    token: str,
    *,
    approved: bool,
    by: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """确认/拒绝挂起的写步骤（确认卡恢复路径，p2-6f API 消费）。

    拒绝 → run 终态 cancelled；批准 → 从挂起步骤续跑（幂等键在快照
    args 中保留：确认前后同键，重复确认/重试幂等命中，PRD 8.7）。
    payload：调用方已 pop_token 时直接传入（API confirm 端点统一 pop
    后按 payload key 分流，避免双重消费一次性令牌）。
    """
    if payload is None:
        payload = await confirm_store.pop_token(token)
    if payload is None:
        return None
    snap = payload["workflow"]
    wf = _workflows.get(snap["workflow_id"])
    run = _find_run(snap["run_id"])
    if wf is None or run is None:
        return {"status": "failed", "detail": "编排或运行记录已不存在"}
    actor = snap.get("user_id")
    events: list[dict[str, Any]] = []
    base_ms = int(run.get("duration_ms") or 0)
    if not approved:
        run.pop("pending", None)
        _finalize_run(run, wf, "cancelled", base_ms)
        await audit.record(
            "workflow_confirm",
            tool="workflow",
            params={"run_id": run["run_id"], "approved": False},
            user_id=by or actor,
            session_id=snap.get("session_id"),
            result="denied",
            detail=f"编排「{wf['name']}」写步骤被拒绝，编排终止",
        )
        await _snapshot()
        return {"status": "cancelled", "run": dict(run), "events": events}
    await audit.record(
        "workflow_confirm",
        tool="workflow",
        params={"run_id": run["run_id"], "approved": True},
        user_id=by or actor,
        session_id=snap.get("session_id"),
        result="ok",
        detail=f"编排「{wf['name']}」写步骤已确认，继续执行",
    )
    started = time.monotonic()
    try:
        status = await asyncio.wait_for(
            _run_steps(
                wf, run, snap["ctx"],
                actor=actor, session=snap.get("session_id") or "",
                actor_auth=snap.get("actor_auth") or {}, events=events,
                start_index=snap["step_index"],
                resume=True,
            ),
            _EXEC_TIMEOUT_S,
        )
    except TimeoutError:
        status = "failed"
        run["steps"].append(
            {"seq": None, "tool": "", "ok": False, "detail": "恢复执行超时（30 分钟上限，已终止）"}
        )
    except Exception as exc:  # noqa: BLE001 - 执行器兜底：任何异常都计一次失败
        status = "failed"
        run["steps"].append({"seq": None, "tool": "", "ok": False, "detail": f"执行异常：{exc}"})
    duration_ms = base_ms + int((time.monotonic() - started) * 1000)
    run.pop("pending", None)
    _finalize_run(run, wf, status, duration_ms)
    text = (
        f"编排「{wf['name']}」{_RUN_STATUS_TEXT.get(status, status)}："
        f"{len(run['steps'])}/{len(wf['steps'])} 步"
    )
    await _notify(wf, run, text=text)
    _publish_finished(run, status)  # 旁路事件：订阅 workflow.run.finished 的自动化任务
    await audit.record(
        "workflow_run",
        tool="workflow",
        params={"workflow_id": wf["id"], "trigger": run["trigger"], "run_id": run["run_id"]},
        user_id=actor,
        session_id=snap.get("session_id"),
        result="ok" if run["ok"] else "failed",
        detail=text,
    )
    await _snapshot()
    return {"status": status, "run": dict(run), "events": events}
