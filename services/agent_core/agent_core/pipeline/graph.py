"""LangGraph 对话流水线：八节点状态图（ARCHITECTURE 4.1 + PLAN P1.1 权限面）。

intent → route → extract → validate → permission → hitl → execute → format

权限校验（PLAN P1.1，PRD 2.3/8.5.5）：permission 节点在 validate 之后、
hitl 之前——写路径按 registry.required_roles 角色矩阵校验，BI 查询按
区域白名单校验（数据级·业务维）；拒绝以友好文案走 format 渲染并落审计。

HITL 机制（PRD 4.1/8.7）：
- 写入类技能在草稿齐备且校验通过后，hitl 节点生成 confirm_token，
  状态快照存 confirm_store（Redis / 内存），推送 confirm_card 后
  本轮流以「等待确认」终态结束（SSE 流关闭）；
- 桌面端 POST /confirmations/{token} 恢复：从快照重建 state，
  走恢复图（execute → format）完成写入；
- 幂等键 {userId}_{sessionId}_{intentHash}_{draftVersion} 在 hitl 生成，
  随 tools/call 传递，mcp-oa 侧幂等兜底（重复提交返回同一单据编号）。

三模式（PLAN P2.6，PRD 3.3）：hitl 按生效模式分流——请求级
mode_override 覆盖技能默认 mode；plan 未批准先出执行计划卡
（plan_card 挂起，批准后重进 hitl）；写路径无论什么模式挂确认卡
（规则2 模式不豁免单步确认）；只读直接执行。

业务系统接入（PLAN P2.1，PRD 6.1）：CRM 销售订单录入（唯一 CRM 写入技能，
默认值链 + 多轮补问 + 大额审批流）、客户 360 / 跟单、ERP 库存 / 采购 / 凭证、
WMS 出入库 / 预警；工具调用按 crm__ / erp__ / wms__ 前缀分流到对应 MCP 服务。

事件流：节点返回增量 events（Annotated[list, operator.add] 累积），
api 层以 astream(stream_mode="updates") 逐节点转 SSE
（事件类型对齐 packages/protocol/events/chat-events.ts）。
"""

import hashlib
import operator
import time
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent_core import audit
from agent_core.guardrail import confirm_store
from agent_core.knowledge import store as knowledge_store
from agent_core.mcp_client.bi import call_bi_tool
from agent_core.mcp_client.crm import call_crm_tool
from agent_core.mcp_client.erp import call_erp_tool
from agent_core.mcp_client.mes import call_mes_tool
from agent_core.mcp_client.oa import McpToolError, call_oa_tool
from agent_core.mcp_client.u8 import call_u8_tool
from agent_core.mcp_client.wms import call_wms_tool
from agent_core.memory import store as memory_store
from agent_core.pipeline import permissions, rules, slots
from agent_core.skills import store
from agent_core.skills.registry import get_skill, match_skill
from agent_core.workflow import store as workflow_store

_STAGE_MESSAGES = {
    "intent": "正在识别意图…",
    "route": "正在匹配技能…",
    "extract": "正在提取参数并查询余额…",
    "validate": "正在校验表单…",
    "permission": "正在校验权限…",
    "hitl": "正在生成计划与确认卡…",
    "execute": "正在执行…",
    "format": "正在生成回复…",
}

_LEAVE_TYPE_LABELS = rules.LEAVE_TYPE_LABELS


class ChatState(TypedDict, total=False):
    """流水线全局状态（概念对齐 ARCHITECTURE 4.1）。"""

    auth: dict[str, Any]  # 网关透传的 JWT 解析结果（工号/科室/权限版本）
    user_id: str
    session_id: str
    message: str
    intent: dict[str, Any] | None  # 阶段1输出：意图识别
    skill: dict[str, Any] | None  # 阶段2输出：技能优先，未命中降级闲聊
    draft: dict[str, Any] | None  # 阶段3输出：参数草稿 + 缺失必填列表
    validation: dict[str, Any] | None  # 阶段4输出：Schema + 业务规则校验结果
    confirm_token: str | None  # 阶段5输出：HITL 待确认令牌
    confirmed: bool | None  # 恢复路径标记（POST /confirmations 置 True；
    # LangGraph 输入仅保留 schema 内字段，必须显式声明）
    mode_override: str | None  # 请求级三模式临时切换（API /chat mode，PRD 3.3 规则1）
    plan_pending: bool | None  # Plan 计划卡已发待批准（hitl 置位，format 区分终态文案）
    plan_approved: bool | None  # Plan 计划批准标记（恢复路径置 True，写步骤随后挂确认卡）
    tool_call: dict[str, Any] | None  # 阶段5输出：待执行工具调用（含幂等键）
    tool_result: Any | None  # 阶段6输出：MCP tools/call 结果
    knowledge: dict[str, Any] | None  # RAG 注入结果（PRD 9.5.3：route 消歧命中 /
    # execute 技能绑定知识检索，format 渲染引用）
    memory_hits: list[dict[str, Any]] | None  # 记忆注入（P3.3，PRD 9.3 注入点1：
    # intent 阶段 Top-K=3，format 渲染「已参考记忆」）
    final: dict[str, Any] | None  # 阶段7输出：文本 + 卡片
    events: Annotated[list[dict[str, Any]], operator.add]


def _stage(stage: str) -> dict[str, Any]:
    """构造 stage_progress 事件。"""
    return {"type": "stage_progress", "stage": stage, "message": _STAGE_MESSAGES[stage]}


def _auth_dept(state: ChatState) -> str:
    """auth 科室（知识检索空间过滤：dept 空间仅本科室，group 空间全员）。

    取尾段（口径对齐 permissions.check_dept_scope 与 API _dept_of）：
    auth.dept 为组织全路径「事业部A/销售科」，入库 dept_scope 为科室短名。
    """
    dept = (state.get("auth") or {}).get("dept") or ""
    return dept.split("/")[-1] if dept else ""


async def _knowledge_search(
    state: ChatState, query: str, tags_filter: list[str] | None = None
) -> dict[str, Any]:
    """知识检索（PRD 9.5.3 RAG）：空间过滤 + 阈值 Top-K（store 内 D3 脱敏/缺口记录）。"""
    return await knowledge_store.search(
        query, dept=_auth_dept(state), tags_filter=tags_filter
    )


async def intent_node(state: ChatState) -> dict[str, Any]:
    """阶段1 意图识别（LLM 接入点：OpenAI 兼容 API 未配置时走规则兜底）。

    记忆注入（P3.3，PRD 9.3 注入点1）：意图识别阶段注入 L2/L3 Top-K=3
    相似记忆辅助消歧，命中以 stage_progress 提示「已参考记忆」并携带
    memory_hits 供 format 渲染；未同意 PIPL / 无相似条目时静默跳过。
    """
    message = state.get("message", "")
    skill = match_skill(message)
    intent_name = skill["name"] if skill else "chat"
    # TODO: LLM_BASE_URL 配置时走小模型路由（LangChain/自研均可），规则结果降级为后校验
    events = [_stage("intent")]
    memory_hits: list[dict[str, Any]] | None = None
    if intent_name not in {"memory_save"}:  # 记忆写入轮自身不注入（避免自我引用）
        try:
            memory_hits = await memory_store.recall(
                query=message,
                user_id=state.get("user_id", ""),
                dept=_auth_dept(state),
            )
        except Exception:  # noqa: BLE001  记忆故障不阻塞对话主流程
            memory_hits = None
        if memory_hits:
            events.append(
                {
                    "type": "stage_progress",
                    "stage": "intent",
                    "message": "已参考记忆：" + "；".join(
                        h["content"][:40] for h in memory_hits
                    ),
                }
            )
    return {
        "intent": {"name": intent_name, "confidence": 1.0 if skill else 0.5},
        "memory_hits": memory_hits,
        "events": events,
    }


async def route_node(state: ChatState) -> dict[str, Any]:
    """阶段2 技能/工具路由：Skill Registry 匹配；未命中且存在补问挂起
    （pending slot）则沿用挂起技能继续收集；命中其他技能则清除挂起。

    知识消歧（PRD 9.5.3 注入点1）：技能与挂起均未命中时检索知识库
    （科室 FAQ/制度文档 Top-3），命中即转 knowledge_qa 纯 RAG 问答流
    （不调业务工具）；未命中维持闲聊兜底，store 侧记知识缺口。
    """
    events = [_stage("route")]
    skill = match_skill(state.get("message", ""))
    pending = await slots.get_pending(state["user_id"], state["session_id"])
    if skill is None and pending:
        skill = get_skill(pending["skill_name"])
    elif skill is not None and pending and pending["skill_name"] != skill["name"]:
        await slots.clear_pending(state["user_id"], state["session_id"])
    payload: dict[str, Any] = {"skill": skill, "events": events}
    if skill is None:
        hits = await _knowledge_search(state, state.get("message", ""))
        if hits["results"]:
            payload["skill"] = get_skill("knowledge_qa")
            payload["knowledge"] = hits
    return payload


async def extract_node(state: ChatState) -> dict[str, Any]:
    """阶段3 参数提取（含知识注入点2，PRD 9.5.3）：抽取字段 + 查余额 +
    时长自动计算；技能绑定知识标签且存在缺失必填时注入填写说明提示。"""
    result = await _extract_fields(state)
    skill = state.get("skill")
    tags = (skill or {}).get("knowledge_tags")
    if skill and tags and skill.get("required_fields"):
        draft = result.get("draft") or {}
        missing = [f for f in skill["required_fields"] if f not in draft]
        if missing:
            # 查询重写为技能标题短语（同注入点4）：tags 限定领域 + title 语义锚，
            # 避免消息中的日期等参数 token 稀释相似度（PRD 9.5.3 查询改写规则化）
            hits = await _knowledge_search(state, skill["title"], tags_filter=tags)
            if hits["results"]:
                top = hits["results"][0]
                result["events"].append(
                    {
                        "type": "stage_progress",
                        "stage": "extract",
                        "message": (
                            f"填写提示：{top['text']}"
                            f"（来源：《{top['title']}》{top['section']}）"
                        ),
                    }
                )
    return result


async def _extract_fields(state: ChatState) -> dict[str, Any]:
    """阶段3 参数提取：抽取字段 + 查余额 + 时长自动计算（computed 来源标记）。"""
    events = [_stage("extract")]
    skill = state.get("skill")
    if not skill:
        return {"draft": None, "events": events}

    if skill["name"] == "oa_leave_request":
        # 分段收集：本条消息提取字段合并到挂起草稿（补问轮续上下文，PRD 5.2）
        pending = await slots.get_pending(state["user_id"], state["session_id"])
        draft = rules.merge_leave_draft(
            state.get("message", ""), pending["draft"] if pending else None
        )
        # 时长自动计算：起止齐备即按工作日重算并覆盖旧值
        # （用户改期/改半天后下一轮自动重算，computed 以起止为准，PRD 5.2）
        if "start_time" in draft and "end_time" in draft:
            try:
                days = rules.calculate_workdays(
                    draft["start_time"]["value"], draft["end_time"]["value"]
                )
                draft["duration_days"] = {"value": days, "source": "computed"}
            except ValueError:
                pass
        leave_type = draft.get("leave_type", {}).get("value")
        # 病假 ≥ 2 天医疗证明提示（PRD 5.2 附件行；MVP 无附件上传，提交后补交 HR）
        if leave_type == "sick" and draft.get("duration_days", {}).get("value", 0) >= 2:
            events.append(
                {
                    "type": "stage_progress",
                    "stage": "extract",
                    "message": "病假 ≥ 2 天需提供医疗证明，请在提交后补交至 HR。",
                }
            )
        # 年假/调休必须查余额（只读工具调用）
        if leave_type in ("annual", "comp"):
            try:
                balances = await call_oa_tool(
                    "oa__query_leave_balance",
                    {"user_id": state["user_id"], "leave_type": leave_type},
                )
                remaining = next((b["remaining_days"] for b in balances), None)
                if remaining is not None:
                    draft["balance_days"] = {"value": remaining, "source": "computed"}
            except McpToolError:
                events.append(
                    {
                        "type": "stage_progress",
                        "stage": "extract",
                        "message": "余额查询暂不可用，将跳过余额校验",
                    }
                )
        missing = [f for f in skill["required_fields"] if f not in draft]
        # 挂起合并草稿：补问/校验失败/取消后下一轮继续改参；成功提交后清除
        await slots.set_pending(state["user_id"], state["session_id"], skill["name"], draft)
        events.append(
            {
                "type": "draft_card",
                "draft": draft,
                "missing_fields": missing,
                "draft_version": 1,
            }
        )
        return {"draft": draft, "events": events}

    # 待办审批：读路径（列表查询）与写路径（行内审批，"同意/驳回第 N 条"）分流
    if skill["name"] == "oa_todo_approve":
        parsed = rules.parse_approval_action(state.get("message", ""))
        if parsed is None:
            return {"draft": {}, "events": events}
        action, target = parsed
        draft = await _approval_draft(state, action, target)
        # 驳回原因附言（PRD 5.2 场景 2：「这条驳回，原因是预算超了」）
        comment = rules.extract_approval_comment(state.get("message", ""))
        if comment:
            draft["comment"] = {"value": comment, "source": "ask"}
        return {"draft": draft, "events": events}

    # BI 查询（PRD 5.2 场景 3）：整句作为查询输入；追加维度追问继承上轮口径
    if skill["name"] == "bi_query":
        msg = state.get("message", "")
        prev = await slots.get_query_result(state["user_id"], state["session_id"], "bi")
        query = f"{prev['query']}，{msg}" if rules.is_bi_followup(msg, prev) else msg
        return {"draft": {"query": {"value": query, "source": "ask"}}, "events": events}

    # CRM 销售订单录入（PLAN P2.1，PRD 6.1.1）：分段收集 + 客户匹配 + 默认值链
    if skill["name"] == "crm_sales_order_entry":
        return await _crm_order_extract(state, skill, events)

    # 跨系统编排（PLAN P2.6，PRD 6.3）：复用 CRM 订单抽取全链（客户匹配 +
    # 默认值链产出编排 inputs 全字段），execute 阶段映射为 workflow 入参
    if skill["name"] == "cross_system_order_flow":
        return await _crm_order_extract(state, skill, events)

    # 客户 360 视图：抽取客户关键词（补问轮挂起，点名后精确匹配）
    if skill["name"] == "crm_customer_360":
        pending = await slots.get_pending(state["user_id"], state["session_id"])
        prev_kw = (pending or {}).get("draft", {}).get("keyword", {}).get("value")
        kw = rules.extract_customer_keyword(state.get("message", "")) or prev_kw
        draft = {"keyword": {"value": kw, "source": "ask"}} if kw else {}
        await slots.set_pending(state["user_id"], state["session_id"], skill["name"], draft)
        return {"draft": draft, "events": events}

    # 订单跟单：抽取订单号（SO + 8 位以上数字），缺号挂起待补问
    if skill["name"] == "crm_order_track":
        order_no = rules.extract_order_no(state.get("message", ""))
        draft = {"order_no": {"value": order_no, "source": "ask"}} if order_no else {}
        if not order_no:
            prev = await slots.get_pending(state["user_id"], state["session_id"])
            if prev:
                draft = prev["draft"] or {}
        await slots.set_pending(state["user_id"], state["session_id"], skill["name"], draft)
        return {"draft": draft, "events": events}

    # ERP 库存查询：SKU 过滤；缺料/告急 → 仅看低于安全库存（PRD 6.1.3）
    if skill["name"] == "erp_inventory_query":
        msg = state.get("message", "")
        draft: dict[str, Any] = {}
        sku = rules.extract_sku(msg)
        if sku:
            draft["sku"] = {"value": sku, "source": "ask"}
        if any(kw in msg for kw in ("缺料", "告急", "缺口", "不足")):
            draft["below_safety"] = {"value": True, "source": "ask"}
        return {"draft": draft, "events": events}

    # ERP 采购单：SKU / 状态（在途/已入库）过滤
    if skill["name"] == "erp_po_sync":
        msg = state.get("message", "")
        draft = {}
        sku = rules.extract_sku(msg)
        if sku:
            draft["sku"] = {"value": sku, "source": "ask"}
        status = rules.extract_po_status(msg)
        if status:
            draft["status"] = {"value": status, "source": "ask"}
        return {"draft": draft, "events": events}

    # 凭证摘要：会计期间缺省当月（PRD 6.1.3）
    if skill["name"] == "erp_voucher_summary":
        period = rules.extract_period(state.get("message", "")) or rules.current_period()
        return {"draft": {"period": {"value": period, "source": "computed"}}, "events": events}

    # WMS 出入库 + 预警：SKU 过滤；方向缺省出库（缺料跟单常用，PRD 6.1.4）
    if skill["name"] == "wms_stock_alert":
        msg = state.get("message", "")
        draft = {}
        sku = rules.extract_sku(msg)
        if sku:
            draft["sku"] = {"value": sku, "source": "ask"}
        draft["order_type"] = {
            "value": "inbound" if "入库" in msg and "出库" not in msg else "outbound",
            "source": "default",
        }
        return {"draft": draft, "events": events}

    # ---- P2.2 科室工作台（PRD 6.2）：只读视图，过滤参数均为可选 ----

    # 生产备料齐套：缺料口径固定 below_safety=True；SKU 可选
    if skill["name"] == "prod_material_check":
        draft = {"below_safety": {"value": True, "source": "default"}}
        sku = rules.extract_sku(state.get("message", ""))
        if sku:
            draft["sku"] = {"value": sku, "source": "ask"}
        return {"draft": draft, "events": events}

    # 计划到货视图 / 品管效期批次 / 仓库运营概览：SKU 可选过滤
    if skill["name"] in ("plan_inbound_view", "qa_batch_trace", "wh_stock_overview"):
        sku = rules.extract_sku(state.get("message", ""))
        draft = {"sku": {"value": sku, "source": "ask"}} if sku else {}
        return {"draft": draft, "events": events}

    # 信息科数据巡检：期间缺省当月（与凭证摘要同口径）
    if skill["name"] == "it_data_check":
        period = rules.extract_period(state.get("message", "")) or rules.current_period()
        return {"draft": {"period": {"value": period, "source": "computed"}}, "events": events}

    # ---- P3.2 全部科室覆盖（PLAN P3.2，PRD 7.2）：只读视图，参数均可选 ----

    # 人力速览（本人近 7 天 OA 动态）/ 管理速览（当月双指标）/
    # 审计流水（本人近 20 条留痕）：均无必填入参
    if skill["name"] in ("hr_roster", "mgmt_overview", "audit_trace"):
        return {"draft": {}, "events": events}

    # 技术备件巡检：SKU 可选过滤（与仓库概览同口径）
    if skill["name"] == "tech_inventory":
        sku = rules.extract_sku(state.get("message", ""))
        draft = {"sku": {"value": sku, "source": "ask"}} if sku else {}
        return {"draft": draft, "events": events}

    # 产品销售看板：客户关键词可选（显式句式命中才提取，附客户 360 摘要）
    if skill["name"] == "product_sales":
        kw = rules.extract_customer_keyword(state.get("message", ""), allow_bare=False)
        draft = {"keyword": {"value": kw, "source": "ask"}} if kw else {}
        return {"draft": draft, "events": events}

    # P3.3 记住偏好（PLAN P3.3，PRD 9.3）：引导词剥离后取正文
    if skill["name"] == "memory_save":
        content = rules.extract_memory_content(state.get("message", ""))
        if content:
            return {"draft": {"content": {"value": content, "source": "ask"}}, "events": events}
        return {"draft": {}, "events": events}

    # ---- P3.1 MES / U8（PLAN P3.1，PRD 7.1）：只读查询，参数可选分流 ----

    # 生产报工：工单号直达报工明细；SKU 可选过滤（无单号 → 工单进度列表）
    if skill["name"] == "mes_production_report":
        msg = state.get("message", "")
        draft = {}
        work_order = rules.extract_work_order_no(msg)
        if work_order:
            draft["work_order"] = {"value": work_order, "source": "ask"}
        sku = rules.extract_sku(msg)
        if sku:
            draft["sku"] = {"value": sku, "source": "ask"}
        return {"draft": draft, "events": events}

    # U8 总账：凭证号直达凭证明细；否则科目余额表（期间缺省当月，与凭证摘要同口径）
    if skill["name"] == "u8_gl_summary":
        msg = state.get("message", "")
        voucher_no = rules.extract_voucher_no(msg)
        if voucher_no:
            return {
                "draft": {"voucher_no": {"value": voucher_no, "source": "ask"}},
                "events": events,
            }
        draft: dict[str, Any] = {
            "period": {
                "value": rules.extract_period(msg) or rules.current_period(),
                "source": "computed",
            }
        }
        subject = rules.extract_gl_subject(msg)
        if subject:
            draft["subject"] = {"value": subject, "source": "ask"}
        return {"draft": draft, "events": events}

    return {"draft": {}, "events": events}


async def _approval_draft(state: ChatState, action: str, target: str) -> dict[str, Any]:
    """行内审批草稿：从最近待办查询结果定位目标条目（slots 30 分钟缓存）。

    target: "id:AP-..."（单号直达）/ "index:N"（序号）/ "last"（"这条"承接
    上轮定位条目，approval_last 槽位记录 id 后从列表取最新状态）。
    定位失败（未查过列表/单号不在列表/序号越界）→ 占位 draft 触发补问。
    """
    items = await slots.get_query_result(state["user_id"], state["session_id"], "approval") or []
    item: dict[str, Any] | None = None
    if target.startswith("id:"):
        approval_id = target[3:]
        item = next((it for it in items if it.get("approval_id") == approval_id), None)
    elif target.startswith("index:"):
        n = int(target[6:])
        if 1 <= n <= len(items):
            item = items[n - 1]
    elif target == "last":
        last = await slots.get_query_result(
            state["user_id"], state["session_id"], "approval_last"
        )
        if isinstance(last, dict) and last.get("approval_id"):
            approval_id = last["approval_id"]
            item = next((it for it in items if it.get("approval_id") == approval_id), None)
    if item is None:
        return {"approval_target": {"value": None}}
    # 记录最近定位条目：下一轮"这条驳回…"承接本条（PRD 5.2 场景 2 验收句）
    await slots.set_query_result(
        state["user_id"], state["session_id"], dict(item), "approval_last"
    )
    return {
        "approval_id": {"value": item["approval_id"], "source": "memory"},
        "action": {"value": action, "source": "ask"},
        "approval_title": {"value": item.get("title", ""), "source": "memory"},
        "applicant": {
            "value": item.get("applicant") or item.get("applicant_id", ""),
            "source": "memory",
        },
        "submitted_at": {"value": item.get("submitted_at", ""), "source": "memory"},
        "comment": {"value": "", "source": "default"},
    }


def _validate_leave(draft: dict[str, Any], skill: dict[str, Any]) -> dict[str, Any]:
    """请假草稿校验（对齐 oa__submit_leave_request.json required + 业务规则）。"""
    missing = [f for f in skill["required_fields"] if f not in draft]
    errors: list[str] = []
    if not missing:
        leave_type = draft["leave_type"]["value"]
        start = datetime.fromisoformat(draft["start_time"]["value"])
        end = datetime.fromisoformat(draft["end_time"]["value"])
        duration = draft["duration_days"]["value"]
        if end <= start:
            errors.append("结束时间必须晚于开始时间")
        # 提前拦截：开始时间不得早于当前时间（与 mcp_oa 侧校验口径一致；
        # start 为用户选择的 naive 本地时间，now 同口径比较）
        if not errors and start < datetime.now():  # noqa: DTZ005
            errors.append("开始时间早于当前时间，请选择未来的时间")
        if duration == 0:
            errors.append("所选区间不含工作日（均为周末），请调整起止时间")
        elif duration <= 0 or abs(duration * 2 - round(duration * 2)) > 1e-9:
            errors.append("时长必须为大于 0 的 0.5 天整数倍")
        balance = draft.get("balance_days", {}).get("value")
        if (
            not errors
            and leave_type in ("annual", "comp")
            and balance is not None
            and duration > balance
        ):
            errors.append(
                f"{_LEAVE_TYPE_LABELS[leave_type]}余额不足：剩余 {balance} 天，"
                f"本次申请 {duration} 天"
            )
    return {"missing_fields": missing, "errors": errors, "passed": not missing and not errors}


def _validate_approval(draft: dict[str, Any]) -> dict[str, Any]:
    """行内审批草稿校验：目标条目缺失→补问；审批动作枚举校验。"""
    if "approval_target" in draft:
        return {"missing_fields": ["approval_target"], "errors": [], "passed": False}
    if "approval_id" not in draft:  # 读路径（列表查询）
        return {"missing_fields": [], "errors": [], "passed": True}
    errors: list[str] = []
    if draft.get("action", {}).get("value") not in ("approve", "reject"):
        errors.append("无效审批动作")
    return {"missing_fields": [], "errors": errors, "passed": not errors}


async def _crm_order_extract(
    state: ChatState, skill: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    """CRM 订单草稿提取（PRD 6.1.1）：分段收集 → 客户匹配 → 默认值链。

    - 客户关键词变更即作废旧命中（merge_crm_order_draft），重新模糊搜索；
      多候选列确认（点名锁定），唯一命中直接锁定，未命中标记待核对
    - 锁定后拉取客户 360 一次（_crm_profile_done 标记）：单联系人自动带出、
      多联系人列候选必问；地址/付款方式默认主数据；单价默认最近成交价
    - 单据类型默认 standard；交期默认 T+7
    """
    msg = state.get("message", "")
    pending = await slots.get_pending(state["user_id"], state["session_id"])
    draft = rules.merge_crm_order_draft(msg, pending["draft"] if pending else None)

    # 客户匹配：候选点名补问轮 → 精确锁定；否则模糊搜索（多候选/未命中标记）
    if "customer_id" not in draft and draft.get("customer_kw", {}).get("value"):
        candidates = draft.get("customer_candidates", {}).get("value") or []
        chosen = rules.match_candidate_customer(msg, candidates) if candidates else None
        if chosen:
            draft["customer_id"] = {"value": chosen["customer_id"], "source": "ask"}
            draft["customer_name"] = {"value": chosen["name"], "source": "ask"}
            draft.pop("customer_candidates", None)
        else:
            try:
                hits = await call_crm_tool(
                    "crm__search_customers", {"keyword": draft["customer_kw"]["value"]}
                )
            except McpToolError:
                hits = []
            if len(hits) == 1:
                draft["customer_id"] = {"value": hits[0]["customer_id"], "source": "computed"}
                draft["customer_name"] = {"value": hits[0]["name"], "source": "computed"}
                draft.pop("customer_candidates", None)
            elif hits:
                draft["customer_candidates"] = {"value": hits, "source": "computed"}
            else:
                draft["customer_not_found"] = {"value": True, "source": "computed"}

    # 默认值链：客户锁定后拉取 360（一次），填联系人/地址/付款方式/单价默认
    if "customer_id" in draft and not draft.get("_crm_profile_done", {}).get("value"):
        try:
            profile = await call_crm_tool(
                "crm__get_customer_360", {"customer_id": draft["customer_id"]["value"]}
            )
        except McpToolError:
            profile = None
        if profile:
            draft["_crm_profile_done"] = {"value": True, "source": "computed"}
            draft["_crm_contacts"] = {"value": profile.get("contacts") or [], "source": "computed"}
            draft["customer_name"] = {"value": profile.get("name", ""), "source": "computed"}
            contacts = profile.get("contacts") or []
            if "contact" not in draft:
                if len(contacts) == 1:
                    draft["contact"] = {"value": contacts[0], "source": "default"}
                elif contacts:
                    draft["contact_candidates"] = {"value": contacts, "source": "computed"}
            if "address" not in draft:
                draft["address"] = {"value": profile.get("default_address", ""), "source": "default"}
            if "payment_term" not in draft:
                draft["payment_term"] = {"value": profile.get("payment_term", ""), "source": "default"}
            prices = profile.get("last_prices") or {}
            for it in draft.get("items", {}).get("value") or []:
                if it.get("price") is None and it.get("sku") in prices:
                    it["price"] = prices[it["sku"]]

    # 多联系人候选点名（补问轮裸回复「李四」）：不受 _crm_profile_done 门控，
    # profile 拉取后的后续补问轮同样生效
    if "customer_id" in draft and "contact" not in draft and draft.get("contact_candidates"):
        named = rules.match_candidate_contact(msg, draft["contact_candidates"]["value"])
        if named:
            draft["contact"] = {"value": named, "source": "ask"}
            draft.pop("contact_candidates", None)

    # 默认值链兜底：单据类型 standard / 交期 T+7（PRD 6.1.1）
    draft.setdefault("order_type", {"value": "standard", "source": "default"})
    draft.setdefault("delivery_date", {"value": rules.default_delivery_date(), "source": "default"})

    missing = [f for f in skill["required_fields"] if f not in draft]
    await slots.set_pending(state["user_id"], state["session_id"], skill["name"], draft)
    events.append(
        {"type": "draft_card", "draft": draft, "missing_fields": missing, "draft_version": 1}
    )
    return {"draft": draft, "events": events}


def _crm_ask_text(field: str, draft: dict[str, Any]) -> str:
    """CRM 订单补问文案（PRD 6.1.1）：候选客户/联系人列选项融入追问。"""
    if field == "customer_id":
        candidates = draft.get("customer_candidates", {}).get("value") or []
        if candidates:
            lines = "\n".join(f"- {c['name']}（{c['customer_id']}）" for c in candidates)
            return (
                f"「{draft.get('customer_kw', {}).get('value', '')}」匹配到 "
                f"{len(candidates)} 家客户，请确认是哪家：\n{lines}"
            )
        if draft.get("customer_not_found", {}).get("value"):
            return f"CRM 未找到客户「{draft.get('customer_kw', {}).get('value', '')}」，请核对客户名称。"
        return "请问是哪家客户？请提供客户名称，我来 CRM 匹配。"
    if field == "contact":
        ask = "请问订单联系人是谁？（需为该客户在册联系人）"
        candidates = draft.get("contact_candidates", {}).get("value") or []
        if candidates:
            ask += "在册联系人：" + "、".join(candidates)
        return ask
    return "请提供商品明细（如：SKU-A x10 单价 50）"


def _validate_crm_order(draft: dict[str, Any], skill: dict[str, Any]) -> dict[str, Any]:
    """CRM 订单草稿校验（PRD 6.1.1）：必填 + 明细完整 + 联系人在册。"""
    missing = [f for f in skill["required_fields"] if f not in draft]
    errors: list[str] = []
    if not missing:
        items = draft["items"]["value"] or []
        if not items:
            errors.append("商品明细不能为空")
        for it in items:
            sku = it.get("sku", "")
            if not it.get("qty") or it["qty"] <= 0:
                errors.append(f"{sku} 数量无效（需为大于 0 的数字）")
            if it.get("price") is None or it["price"] <= 0:
                errors.append(f"{sku} 无最近成交价记录，请提供单价")
        contact = draft["contact"]["value"]
        contacts = draft.get("_crm_contacts", {}).get("value") or []
        if contacts and contact not in contacts:
            errors.append(f"联系人「{contact}」不在该客户在册联系人中（{'、'.join(contacts)}）")
    return {"missing_fields": missing, "errors": errors, "passed": not missing and not errors}


async def validate_node(state: ChatState) -> dict[str, Any]:
    """阶段4 Schema 校验：必填/枚举/时间/时长/余额（PRD 5.2 校验矩阵）。"""
    events = [_stage("validate")]
    skill = state.get("skill")
    if not skill:
        return {"validation": {"passed": True}, "events": events}
    if skill["name"] == "oa_leave_request":
        validation = _validate_leave(state.get("draft") or {}, skill)
        return {"validation": validation, "events": events}
    if skill["name"] == "oa_todo_approve":
        validation = _validate_approval(state.get("draft") or {})
        return {"validation": validation, "events": events}
    if skill["name"] == "crm_sales_order_entry":
        validation = _validate_crm_order(state.get("draft") or {}, skill)
        return {"validation": validation, "events": events}
    # 跨系统编排：仅必填缺失校验（缺失触发补问；明细/联系人细节由
    # workflow 步骤执行与 MCP 契约校验兜底）
    if skill["name"] == "cross_system_order_flow":
        draft = state.get("draft") or {}
        missing = [f for f in skill["required_fields"] if f not in draft]
        return {
            "validation": {"missing_fields": missing, "errors": [], "passed": not missing},
            "events": events,
        }
    return {"validation": {"passed": True}, "events": events}


async def permission_node(state: ChatState) -> dict[str, Any]:
    """权限校验（PLAN P1.1，PRD 2.3/8.5.5）：写路径角色矩阵 + BI 区域白名单。

    - auth 缺省（本地冒烟无 token）→ 放行，保持既有链路不变
    - 技能级：required_roles any-of，仅约束写路径（读/查询放行，
      数据可见性由"本人范围"保证：工具入参 user_id 恒取 JWT sub）
    - 数据级·组织维（P2.2）：科室工作台技能 dept_scope 与 auth.dept
      尾段匹配才放行（本科室隔离，读路径同样校验）
    - 数据级·业务维：BI 查询文本命中白名单外区域 → 拒绝（与组织维独立）
    - 拒绝 → 友好文案写入 validation.errors（PRD 阶段5：不暴露权限
      细节），hitl/execute 自然跳过，format 渲染；并落审计 permission_denied
    """
    events = [_stage("permission")]
    skill = state.get("skill")
    if not skill:
        return {"events": events}
    auth = state.get("auth")
    draft = state.get("draft") or {}
    message: str | None = None
    if _is_write_turn(skill, draft):
        message = permissions.check_skill_roles(auth, skill)
    if message is None and skill.get("dept_scope"):
        # 数据级·组织维（P2.2 本科室级）：科室工作台技能按 auth.dept 准入
        message = permissions.check_dept_scope(auth, skill)
    if message is None and skill["name"] == "bi_query":
        message = permissions.check_region_scope(
            auth, draft.get("query", {}).get("value", "")
        )
    if message is not None:
        params: dict[str, Any] = {"skill": skill["name"]}
        if skill["name"] == "bi_query":
            params["query"] = draft.get("query", {}).get("value", "")
        await audit.record(
            "permission_denied",
            user_id=state.get("user_id"),
            session_id=state.get("session_id"),
            tool=skill.get("write_tool") or (skill.get("read_tools") or [None])[0],
            params=params,
            result="denied",
            detail=message,
        )
        return {
            "events": events,
            "validation": {"missing_fields": [], "errors": [message], "passed": False},
        }
    return {"events": events}


def _snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    """提取恢复执行所需的最小状态快照（JSON 可序列化）。"""
    return {
        "user_id": state.get("user_id"),
        "session_id": state.get("session_id"),
        "intent": state.get("intent"),
        "skill": state.get("skill"),
        "draft": state.get("draft"),
        "confirm_token": state.get("confirm_token"),
    }


def _is_write_turn(skill: dict[str, Any], draft: dict[str, Any]) -> bool:
    """本轮是否写路径：表单写入技能恒为写；行内审批由 draft 是否含 approval_id 决定。"""
    if skill.get("write_mode") == "inline":
        return "approval_id" in draft
    return skill.get("rw") == "write"


_SYSTEM_OF = {"oa": "OA", "bi": "BI", "crm": "CRM", "erp": "ERP", "wms": "WMS"}


def _system_of(tool: str) -> str:
    """工具前缀 → 业务系统名（计划卡展示「将调用哪些系统」）。"""
    return _SYSTEM_OF.get(tool.split("__", 1)[0], "MCP")


def _eff_mode(state: Mapping[str, Any]) -> str:
    """生效模式（PRD 3.3 规则1）：请求级 mode_override 覆盖技能默认 mode。

    技能未声明 mode 时按读写兜底（写技能 craft 保持现行 HITL，读技能 ask）。
    """
    override = state.get("mode_override")
    if override:
        return override
    skill = state.get("skill") or {}
    return skill.get("mode") or ("craft" if skill.get("rw") == "write" else "ask")


def _plan_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    """计划卡展示快照（PRD 3.3 Plan：将调用哪些系统、读改哪些数据）。

    步骤 = 读工具序列 + 写工具（写步骤 requires_confirm=True——规则2 模式
    不豁免单步确认，批准计划后仍挂确认卡）。
    """
    skill = state.get("skill") or {}
    steps: list[dict[str, Any]] = [
        {
            "seq": i,
            "tool": tool,
            "system": _system_of(tool),
            "rw": "read",
            "requires_confirm": False,
        }
        for i, tool in enumerate(skill.get("read_tools") or [], 1)
    ]
    if skill.get("write_tool"):
        steps.append(
            {
                "seq": len(steps) + 1,
                "tool": skill["write_tool"],
                "system": _system_of(skill["write_tool"]),
                "rw": "write",
                "requires_confirm": True,
            }
        )
    return {"skill_title": skill.get("title", ""), "mode": "plan", "steps": steps}


async def hitl_node(state: ChatState) -> dict[str, Any]:
    """阶段5 HITL（PRD 3.3 三模式）：
    - plan 未批准：先出执行计划卡挂起（批准后经恢复图重进本节点）；
    - 写路径（craft 默认 / ask 强制规则2 / plan 已批准）：确认卡挂起；
    - 只读（ask/craft 默认 / plan 已批准）：直接执行。
    """
    events = [_stage("hitl")]
    skill = state.get("skill")
    validation = state.get("validation") or {"passed": True}
    if not skill or not validation.get("passed"):
        return {"events": events}
    if (
        _eff_mode(state) == "plan"
        and not state.get("plan_approved")
        and (skill.get("read_tools") or skill.get("write_tool"))
    ):
        # Plan 模式（PRD 3.3）：先出执行计划卡挂起，批准后从快照恢复——
        # 写步骤重进本节点挂确认卡（规则2），只读步骤直接执行
        token = uuid.uuid4().hex
        snap = _snapshot(state)
        snap["plan_pending"] = True
        await confirm_store.set_token(token, {"state": snap})
        expires_at = int(time.time() * 1000) + 10 * 60 * 1000
        events.append(
            {
                "type": "plan_card",
                "plan_token": token,
                "payload": _plan_payload(state),
                "expires_at": expires_at,
            }
        )
        # 审计：计划卡签发（PRD 10）
        await audit.record(
            "plan_issued",
            user_id=state.get("user_id"),
            session_id=state.get("session_id"),
            tool=skill.get("write_tool") or (skill.get("read_tools") or [None])[0],
            params={"skill": skill["name"], "mode": "plan"},
        )
        return {"confirm_token": token, "plan_pending": True, "events": events}
    if skill.get("write_tool") and _is_write_turn(skill, state.get("draft") or {}):
        # 幂等键：{userId}_{sessionId}_{intentHash}_{draftVersion}（PRD 8.7）
        # intentHash：表单技能按技能名（补问轮 intent 可能漂移）；
        # 行内审批按 目标条目+动作（不同待办互不冲突，同操作重试幂等命中）
        if skill["name"] == "oa_todo_approve":
            draft = state.get("draft") or {}
            target = f"{draft['approval_id']['value']}:{draft['action']['value']}"
            intent_hash = hashlib.md5(target.encode()).hexdigest()[:12]
        else:
            intent_hash = hashlib.md5(skill["name"].encode()).hexdigest()[:12]
        idempotency_key = f"{state.get('user_id')}_{state.get('session_id')}_{intent_hash}_1"
        tool_call = {
            "name": skill["write_tool"],
            "arguments": _tool_arguments(state, idempotency_key),
        }
        # 状态快照入确认存储（Redis confirm:{token} TTL 10min / 内存兜底）
        token = uuid.uuid4().hex
        snap = _snapshot(state)
        snap["tool_call"] = tool_call
        await confirm_store.set_token(token, {"state": snap})
        expires_at = int(time.time() * 1000) + 10 * 60 * 1000
        events.append(
            {
                "type": "confirm_card",
                "confirm_token": token,
                "payload": _confirm_payload(state),
                "expires_at": expires_at,
            }
        )
        # 审计：确认卡签发（敏感操作入口，PRD 10）
        await audit.record(
            "confirm_issued",
            user_id=state.get("user_id"),
            session_id=state.get("session_id"),
            tool=tool_call["name"],
            params=tool_call["arguments"],
        )
        return {"confirm_token": token, "tool_call": tool_call, "events": events}
    return {"events": events}


def _tool_arguments(state: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
    """从草稿构造工具入参（对齐 oa__submit_leave_request.json / oa__approve.json）。"""
    skill = state.get("skill") or {}
    draft = state.get("draft") or {}
    if skill.get("name") == "oa_todo_approve":
        return {
            "approval_id": draft["approval_id"]["value"],
            "action": draft["action"]["value"],
            "comment": draft.get("comment", {}).get("value", ""),
            "idempotency_key": idempotency_key,
        }
    if skill.get("name") == "crm_sales_order_entry":
        # CRM 订单入参（契约 crm__submit_sales_order.json）：明细直接透传，
        # 金额由服务端重算兜底；付款方式缺省 net30（360 拉取失败时兜底）
        return {
            "user_id": state.get("user_id"),
            "order_type": draft["order_type"]["value"],
            "customer_id": draft["customer_id"]["value"],
            "contact": draft["contact"]["value"],
            "address": draft.get("address", {}).get("value", ""),
            "items": draft["items"]["value"],
            "delivery_date": draft["delivery_date"]["value"],
            "payment_term": draft.get("payment_term", {}).get("value", "net30"),
            "idempotency_key": idempotency_key,
        }
    args = {
        f: draft[f]["value"]
        for f in ("leave_type", "start_time", "end_time", "duration_days", "reason")
        if f in draft
    }
    args["user_id"] = state.get("user_id")
    args["idempotency_key"] = idempotency_key
    return args


def _confirm_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    """确认卡展示快照（桌面端渲染确认卡，PRD 5.2 场景 1-1 / 场景 2）。"""
    draft = state.get("draft") or {}
    skill = state.get("skill") or {}
    if skill.get("name") == "oa_todo_approve":
        fields = {
            "approval_title": draft.get("approval_title", {}).get("value", ""),
            "applicant": draft.get("applicant", {}).get("value", ""),
            "submitted_at": draft.get("submitted_at", {}).get("value", ""),
            "action": draft.get("action", {}).get("value", ""),
        }
        comment = draft.get("comment", {}).get("value", "")
        if comment:
            fields["comment"] = comment
        return {"skill_title": skill.get("title", ""), "fields": fields}
    if skill.get("name") == "crm_sales_order_entry":
        # 确认卡字段：明细摊平 + 合计金额（PRD 6.1.1 确认卡）
        return {
            "skill_title": skill.get("title", ""),
            "fields": rules.build_crm_order_confirm_fields(draft),
        }
    fields = {f: draft[f]["value"] for f in draft}
    if "leave_type" in fields:
        fields["leave_type_label"] = _LEAVE_TYPE_LABELS.get(
            fields["leave_type"], fields["leave_type"]
        )
    return {"skill_title": skill.get("title", ""), "fields": fields}


async def execute_node(state: ChatState) -> dict[str, Any]:
    """阶段6 执行（含知识注入点4，PRD 9.5.3）：MCP tools/call（恢复路径从
    本节点起，带幂等键）；技能绑定知识且执行成功时检索 SOP/规范注入引用。"""
    result = await _execute_tools(state)
    skill = state.get("skill")
    tags = (skill or {}).get("knowledge_tags")
    tool_result = result.get("tool_result")
    if (
        skill
        and tags
        and tool_result is not None
        and not (isinstance(tool_result, dict) and "error" in tool_result)
    ):
        # 查询重写为技能标题短语（PRD 9.5.3 查询改写的规则化实现）：tags 已限定
        # 知识领域，title 是稳定语义锚；原始 message 常带日期/参数 token 稀释相似度
        hits = await _knowledge_search(state, skill["title"], tags_filter=tags)
        if hits["results"]:
            result["knowledge"] = hits
    # P3.3 高频行为自动沉淀（PLAN P3.3，PRD 9.3 写入触发②）：同一只读查询
    # 连续 3 次 → L2 习惯记忆（source=auto_consolidated）；未同意 PIPL /
    # 敏感内容在 store 内静默跳过，不阻塞对话主流程
    if (
        skill
        and skill["name"] != "memory_save"
        and tool_result is not None
        and not (isinstance(tool_result, dict) and "error" in tool_result)
    ):
        digest_text = (
            "常查报表："
            + str((state.get("draft") or {}).get("query", {}).get("value", "")).strip()
            if skill["name"] == "bi_query"
            else f"常用查询：{skill['title']}"
        )
        try:
            saved = await memory_store.track(
                user_id=state["user_id"],
                skill=skill["name"],
                text=digest_text,
                dept=_auth_dept(state) or None,
            )
        except Exception:  # noqa: BLE001  自动沉淀失败不影响主流程
            saved = None
        if saved:
            result.setdefault("events", result.get("events") or []).append(
                {
                    "type": "stage_progress",
                    "stage": "execute",
                    "message": f"已自动沉淀习惯记忆：{saved['content']}",
                }
            )
    return result


async def _execute_tools(state: ChatState) -> dict[str, Any]:
    """阶段6 执行：MCP tools/call（恢复路径从本节点起，带幂等键）。

    HITL 挂起语义：已发确认卡（confirm_token 存在）但未经用户确认
    （confirmed 标记）时，本轮不执行写入——等待 POST /confirmations 恢复。
    """
    events = [_stage("execute")]
    if not (state.get("validation") or {"passed": True}).get("passed", True):
        # 校验未通过（待补问/报错）：不执行任何工具，format 节点渲染补问文案
        return {"tool_result": None, "events": events}
    if state.get("confirm_token") and not state.get("confirmed"):
        return {"tool_result": None, "events": events}
    tool_call = state.get("tool_call")
    if not tool_call:
        # 只读技能或闲聊
        skill = state.get("skill")
        if skill and skill["name"] == "oa_todo_approve":
            try:
                result = await call_oa_tool(
                    "oa__query_pending_approvals", {"user_id": state["user_id"]}
                )
                # 缓存列表供行内审批"第 N 条"定位（slots 30 分钟过期）
                await slots.set_query_result(
                    state["user_id"], state["session_id"], result or [], "approval"
                )
                return {"tool_result": result, "events": events}
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
        if skill and skill["name"] == "bi_query":
            draft = state.get("draft") or {}
            try:
                result = await call_bi_tool(
                    "bi__execute_query", {"query": draft.get("query", {}).get("value", "")}
                )
                # 缓存结果供追加维度追问继承口径（slots 30 分钟过期）
                await slots.set_query_result(state["user_id"], state["session_id"], result, "bi")
                return {"tool_result": result, "events": events}
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
        if skill and skill["name"] == "weekly_report":
            try:
                result = await call_oa_tool(
                    "oa__query_activity_log", {"user_id": state["user_id"], "days": 7}
                )
                return {"tool_result": result, "events": events}
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
        # CRM 客户 360：keyword 模糊搜索 → 唯一命中拉全档；多候选/未命中交渲染
        if skill and skill["name"] == "crm_customer_360":
            draft = state.get("draft") or {}
            kw = draft.get("keyword", {}).get("value", "")
            if not kw:
                return {"tool_result": None, "events": events}
            try:
                hits = await call_crm_tool("crm__search_customers", {"keyword": kw})
                if len(hits) == 1:
                    profile = await call_crm_tool(
                        "crm__get_customer_360", {"customer_id": hits[0]["customer_id"]}
                    )
                    return {"tool_result": profile, "events": events}
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": {"candidates": hits}, "events": events}
        # CRM 跟单：订单号查询进度（缺号 → format 渲染补问）
        if skill and skill["name"] == "crm_order_track":
            draft = state.get("draft") or {}
            order_no = draft.get("order_no", {}).get("value", "")
            if not order_no:
                return {"tool_result": None, "events": events}
            try:
                result = await call_crm_tool(
                    "crm__query_order_progress", {"order_no": order_no}
                )
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # ERP 库存：SKU + 缺料过滤（PRD 6.1.3）
        if skill and skill["name"] == "erp_inventory_query":
            draft = state.get("draft") or {}
            args: dict[str, Any] = {
                "below_safety": bool(draft.get("below_safety", {}).get("value"))
            }
            if draft.get("sku"):
                args["sku"] = draft["sku"]["value"]
            try:
                result = await call_erp_tool("erp__query_inventory", args)
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # ERP 采购单：SKU / 状态过滤
        if skill and skill["name"] == "erp_po_sync":
            draft = state.get("draft") or {}
            args = {}
            if draft.get("sku"):
                args["sku"] = draft["sku"]["value"]
            if draft.get("status"):
                args["status"] = draft["status"]["value"]
            try:
                result = await call_erp_tool("erp__query_purchase_orders", args)
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # ERP 凭证摘要：期间缺省当月（extract 已填 computed）
        if skill and skill["name"] == "erp_voucher_summary":
            draft = state.get("draft") or {}
            period = draft.get("period", {}).get("value") or rules.current_period()
            try:
                result = await call_erp_tool("erp__query_voucher_summary", {"period": period})
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # WMS 预警 + 出入库单合并查询（PRD 6.1.4：缺料跟单全景）
        if skill and skill["name"] == "wms_stock_alert":
            draft = state.get("draft") or {}
            sku = draft.get("sku", {}).get("value")
            order_type = draft.get("order_type", {}).get("value", "outbound")
            try:
                result: dict[str, Any] = {
                    "alerts": await call_wms_tool(
                        "wms__query_stock_alerts", {"sku": sku} if sku else {}
                    ),
                    "orders": await call_wms_tool(
                        "wms__query_stock_orders",
                        {"order_type": order_type, **({"keyword": sku} if sku else {})},
                    ),
                }
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # ---- P2.2 科室工作台（PRD 6.2）：五科室只读双工具编排 ----
        # 生产备料齐套：缺料清单（below_safety 固定 True）+ 近期出库单（领料进度）
        if skill and skill["name"] == "prod_material_check":
            draft = state.get("draft") or {}
            sku = draft.get("sku", {}).get("value")
            try:
                result: dict[str, Any] = {
                    "inventory": await call_erp_tool(
                        "erp__query_inventory",
                        {"below_safety": True, **({"sku": sku} if sku else {})},
                    ),
                    "orders": await call_wms_tool(
                        "wms__query_stock_orders",
                        {"order_type": "outbound", **({"keyword": sku} if sku else {})},
                    ),
                }
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # 计划到货视图：在途采购单 + 关联库存（指定 SKU 看全量，未指定看缺料）
        if skill and skill["name"] == "plan_inbound_view":
            draft = state.get("draft") or {}
            sku = draft.get("sku", {}).get("value")
            try:
                result = {
                    "pos": await call_erp_tool(
                        "erp__query_purchase_orders",
                        {"status": "in_transit", **({"sku": sku} if sku else {})},
                    ),
                    "inventory": await call_erp_tool(
                        "erp__query_inventory",
                        {"below_safety": not sku, **({"sku": sku} if sku else {})},
                    ),
                }
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # 品管效期/批次追溯：效期临期预警（含批次明细）+ 近期入库单（批次来源）
        if skill and skill["name"] == "qa_batch_trace":
            draft = state.get("draft") or {}
            sku = draft.get("sku", {}).get("value")
            try:
                result = {
                    "alerts": await call_wms_tool(
                        "wms__query_stock_alerts", {"sku": sku} if sku else {}
                    ),
                    "orders": await call_wms_tool(
                        "wms__query_stock_orders",
                        {"order_type": "inbound", **({"keyword": sku} if sku else {})},
                    ),
                }
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # 仓库运营概览：库存水位（全量）+ 待处置预警
        if skill and skill["name"] == "wh_stock_overview":
            draft = state.get("draft") or {}
            sku = draft.get("sku", {}).get("value")
            try:
                result = {
                    "inventory": await call_erp_tool(
                        "erp__query_inventory", {"sku": sku} if sku else {}
                    ),
                    "alerts": await call_wms_tool(
                        "wms__query_stock_alerts", {"sku": sku} if sku else {}
                    ),
                }
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # 信息科数据巡检：BI 指标快照 + 凭证平衡核查（期间缺省当月）
        if skill and skill["name"] == "it_data_check":
            draft = state.get("draft") or {}
            period = draft.get("period", {}).get("value") or rules.current_period()
            try:
                result = {
                    "bi": await call_bi_tool("bi__execute_query", {"query": "本月销售额"}),
                    "voucher": await call_erp_tool(
                        "erp__query_voucher_summary", {"period": period}
                    ),
                }
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # ---- P3.2 全部科室覆盖（PLAN P3.2，PRD 7.2）：只读工具编排 ----

        # 人力速览：本人近 7 天 OA 操作动态（用户标识恒取会话 user_id）
        if skill and skill["name"] == "hr_roster":
            try:
                result = await call_oa_tool(
                    "oa__query_activity_log", {"user_id": state["user_id"], "days": 7}
                )
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}

        # 管理速览：BI 当月销售 KPI + 当期凭证平衡（企管双指标看板）
        if skill and skill["name"] == "mgmt_overview":
            try:
                result = {
                    "bi": await call_bi_tool("bi__execute_query", {"query": "本月销售额"}),
                    "voucher": await call_erp_tool(
                        "erp__query_voucher_summary", {"period": rules.current_period()}
                    ),
                }
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}

        # 审计流水（法务科）：本人近 20 条操作留痕
        # （D3 敏感：仅本人范围；进程内直调 audit.recent，不走 MCP）
        if skill and skill["name"] == "audit_trace":
            rows = await audit.recent(limit=20, user_id=state.get("user_id"))
            return {"tool_result": {"rows": rows}, "events": events}

        # P3.3 记住偏好（PLAN P3.3，PRD 9.3 写入触发①）：
        # 进程内直调 memory.add（PIPL 门禁 + 敏感过滤在 store 层）
        if skill and skill["name"] == "memory_save":
            draft = state.get("draft") or {}
            content = draft.get("content", {}).get("value", "")
            try:
                entry = await memory_store.add(
                    user_id=state["user_id"],
                    content=content,
                    kind="preference",
                    source="ask",
                    dept=_auth_dept(state) or None,
                )
            except ValueError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": entry, "events": events}

        # 技术备件巡检：备件库存水位 + 备件预警（SKU 可选）
        if skill and skill["name"] == "tech_inventory":
            draft = state.get("draft") or {}
            sku = draft.get("sku", {}).get("value")
            try:
                result = {
                    "inventory": await call_erp_tool(
                        "erp__query_inventory", {"sku": sku} if sku else {}
                    ),
                    "alerts": await call_wms_tool(
                        "wms__query_stock_alerts", {"sku": sku} if sku else {}
                    ),
                }
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}

        # 产品销售看板：BI 产品线拆分 + 可选客户 360（显式句式命中客户名时）
        if skill and skill["name"] == "product_sales":
            draft = state.get("draft") or {}
            kw = draft.get("keyword", {}).get("value")
            try:
                result = {
                    "bi": await call_bi_tool(
                        "bi__execute_query", {"query": "本月销售额", "dimensions": ["product"]}
                    ),
                }
                if kw:
                    hits = await call_crm_tool("crm__search_customers", {"keyword": kw})
                    if len(hits) == 1:
                        result["customer"] = await call_crm_tool(
                            "crm__get_customer_360", {"customer_id": hits[0]["customer_id"]}
                        )
                    else:
                        result["customer_candidates"] = hits
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}

        # ---- P3.1 MES / U8（PLAN P3.1，PRD 7.1）：只读工具编排 ----

        # 生产报工：工单号直达报工明细；无单号 → 工单进度列表（SKU 可选）
        if skill and skill["name"] == "mes_production_report":
            draft = state.get("draft") or {}
            work_order = draft.get("work_order", {}).get("value")
            sku = draft.get("sku", {}).get("value")
            try:
                if work_order:
                    result = await call_mes_tool(
                        "mes__query_production_reports", {"work_order": work_order}
                    )
                else:
                    result = await call_mes_tool(
                        "mes__query_work_orders", {"sku": sku} if sku else {}
                    )
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}

        # U8 总账：凭证号直达凭证明细；否则科目余额表（期间缺省当月）
        if skill and skill["name"] == "u8_gl_summary":
            draft = state.get("draft") or {}
            voucher_no = draft.get("voucher_no", {}).get("value")
            try:
                if voucher_no:
                    result = await call_u8_tool(
                        "u8__query_voucher_detail", {"voucher_no": voucher_no}
                    )
                else:
                    args = {
                        "period": draft.get("period", {}).get("value") or rules.current_period()
                    }
                    subject = draft.get("subject", {}).get("value")
                    if subject:
                        args["subject"] = subject
                    result = await call_u8_tool("u8__query_gl_balance", args)
            except McpToolError as exc:
                return {"tool_result": {"error": str(exc)}, "events": events}
            return {"tool_result": result, "events": events}
        # ---- P2.6 跨系统编排（PLAN P2.6，PRD 6.3）：workflow 域引用执行 ----
        if skill and skill["name"] == "cross_system_order_flow":
            return await _execute_workflow_skill(state, events)
        return {"tool_result": None, "events": events}
    # 写入执行：按工具前缀分流到对应 MCP 服务（crm__ → mcp-crm，其余 → mcp-oa）
    call = call_crm_tool if tool_call["name"].startswith("crm__") else call_oa_tool
    try:
        result = await call(tool_call["name"], tool_call["arguments"])
    except McpToolError as exc:
        result = {"error": str(exc)}
    return {"tool_result": result, "events": events}


async def _execute_workflow_skill(
    state: ChatState, events: list[dict[str, Any]]
) -> dict[str, Any]:
    """跨系统编排技能执行（PLAN P2.6 p2-6c，PRD 6.3）：workflow 域三分形桥接。

    技能经 workflow_id 引用官方编排（seed_official 幂等兜底，编排被删除后
    自愈）；draft 按 CRM 录单同款映射为编排 inputs。返回分形对齐
    workflow_store.execute：
    - plan 未批准 → 签发计划卡（read_tools/write_tool 皆空 → hitl_node
      直通，plan_card 在此签发；批准后经恢复图 plan_approved=True 重入）
    - pending_confirm → 透传 workflow 确认卡事件（payload key "workflow"，
      API confirm 端点按 key 分流恢复，规则2 模式不豁免单步确认）
    - 终态 → tool_result=run（format 编排分支渲染 run 步骤明细）
    """
    wf = await workflow_store.seed_official()
    draft = state.get("draft") or {}
    inputs = {
        "order_type": draft.get("order_type", {}).get("value", "standard"),
        "customer_id": draft["customer_id"]["value"],
        "contact": draft["contact"]["value"],
        "address": draft.get("address", {}).get("value", ""),
        "items": draft["items"]["value"],
        "delivery_date": draft.get("delivery_date", {}).get("value", ""),
        "payment_term": draft.get("payment_term", {}).get("value", "net30"),
    }
    res = await workflow_store.execute(
        wf["id"],
        inputs,
        trigger="skill",
        actor_auth=state.get("auth"),
        user_id=state.get("user_id"),
        session_id=state.get("session_id"),
        mode=_eff_mode(state),  # 规则1：请求级 mode_override 传导至编排引擎
        plan_approved=bool(state.get("plan_approved")),
    )
    if res["status"] == "plan":
        token = uuid.uuid4().hex
        snap = _snapshot(state)  # 快照含 draft：批准恢复后按同参重入执行
        snap["plan_pending"] = True
        await confirm_store.set_token(token, {"state": snap})
        events.append(
            {
                "type": "plan_card",
                "plan_token": token,
                "payload": res["plan"],
                "expires_at": int(time.time() * 1000) + 10 * 60 * 1000,
            }
        )
        await audit.record(
            "plan_issued",
            user_id=state.get("user_id"),
            session_id=state.get("session_id"),
            tool="workflow",
            params={"skill": "cross_system_order_flow", "workflow_id": wf["id"]},
        )
        return {"confirm_token": token, "plan_pending": True, "events": events}
    if res["status"] == "pending_confirm":
        events.extend(res["events"])
        return {"confirm_token": res["confirm_token"], "events": events}
    events.extend(res["events"])
    return {"tool_result": res["run"], "events": events}


def _balance_note(draft: dict[str, Any]) -> str:
    """请假补问前缀：融入余额展示（PRD 5.2 示例「查到你的年假余额为 5 天…」）。

    仅年假/调休有 balance_days（extract 查询写入）；时长已知时附扣减预览。
    """
    balance = draft.get("balance_days", {}).get("value")
    if balance is None:
        return ""
    leave_type = draft.get("leave_type", {}).get("value", "")
    label = _LEAVE_TYPE_LABELS.get(leave_type, leave_type)
    duration = draft.get("duration_days", {}).get("value")
    if isinstance(duration, (int, float)) and duration > 0:
        return (
            f"查到你的{label}余额为 {balance:g} 天"
            f"（本次申请 {duration:g} 天，扣后剩 {max(balance - duration, 0):g} 天）。\n"
        )
    return f"查到你的{label}余额为 {balance:g} 天。\n"


def _render_knowledge(knowledge: dict[str, Any] | None) -> tuple[str, list[dict[str, Any]]]:
    """知识问答渲染（PRD 9.5.3 注入点3）：原文摘录 + 来源文档名/章节 + 密级标记。

    D3 敏感文本已在 store.search 脱敏；注入预算 ≤800 Token（top_k=3、单块 ≤500 字）。
    """
    results = (knowledge or {}).get("results") or []
    if not results:
        return "知识库中未找到相关内容。你可以换个说法，或联系知识库管理员补充相关文档。", []
    lines = []
    for i, r in enumerate(results, 1):
        mark = "（D3 敏感，已脱敏）" if r["classification"] == "D3" else ""
        lines.append(f"{i}. {r['text']}{mark}\n   —— 来源：《{r['title']}》{r['section']}")
    return "根据知识库检索结果：\n" + "\n".join(lines), []


def _knowledge_refs(knowledge: dict[str, Any]) -> str:
    """技能回复尾部的知识引用段（注入点4 渲染，Top-2 控制注入预算）。"""
    results = (knowledge or {}).get("results") or []
    if not results:
        return ""
    lines = [
        f"- {r['text']}（来源：《{r['title']}》{r['section']}）" for r in results[:2]
    ]
    return "相关制度参考：\n" + "\n".join(lines)


async def format_node(state: ChatState) -> dict[str, Any]:
    """阶段7 格式化（含知识注入点3/4 渲染，PRD 9.5.3）：渲染最终回复（final
    事件，本轮对话终态）；技能执行注入的知识在回复尾部附引用段。"""
    result = await _format_reply(state)
    skill = state.get("skill")
    knowledge = state.get("knowledge")
    final = result.get("final")
    if knowledge and skill and skill["name"] != "knowledge_qa" and final:
        refs = _knowledge_refs(knowledge)
        if refs:
            final["text"] += "\n\n" + refs
            for ev in result.get("events") or []:
                if ev.get("type") == "final":
                    ev["text"] = final["text"]
    return result


async def _format_reply(state: ChatState) -> dict[str, Any]:
    """阶段7 格式化：渲染最终回复（final 事件，本轮对话终态）。"""
    events = [_stage("format")]
    skill = state.get("skill")

    if not skill:
        text = "收到。我是你的工作助手，可以帮你处理请假申请、待办审批等事务。"
        events.append({"type": "final", "text": text, "cards": []})
        return {"final": {"text": text, "cards": []}, "events": events}

    # 知识问答流（PRD 9.5.3）：纯 RAG 回复（不调业务工具），附来源与原文摘录
    if skill["name"] == "knowledge_qa":
        text, cards = _render_knowledge(state.get("knowledge"))
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    validation = state.get("validation") or {}
    missing = validation.get("missing_fields") or []
    if missing:
        ask = (skill.get("ask_messages") or {}).get(missing[0], "请补充必要信息。")
        if skill["name"] == "oa_leave_request":
            # 补问文案融入余额展示（PRD 5.2 示例）
            ask = _balance_note(state.get("draft") or {}) + ask
        if skill["name"] in ("crm_sales_order_entry", "cross_system_order_flow"):
            # CRM 订单补问：候选客户/联系人列选项融入追问（PRD 6.1.1；
            # 编排技能复用同款文案）
            ask = _crm_ask_text(missing[0], state.get("draft") or {})
        events.append({"type": "final", "text": ask, "cards": []})
        return {"final": {"text": ask, "cards": []}, "events": events}

    errors = validation.get("errors") or []
    if errors:
        events.append({"type": "final", "text": "；".join(errors), "cards": []})
        return {"final": {"text": "；".join(errors), "cards": []}, "events": events}

    if state.get("plan_pending") and not state.get("plan_approved"):
        text = "已生成执行计划，请批准后执行（10 分钟内有效）。"
        events.append({"type": "final", "text": text, "cards": []})
        return {"final": {"text": text, "cards": []}, "events": events}

    if state.get("confirm_token") and not state.get("tool_result"):
        text = "已生成确认卡，请确认后提交（10 分钟内有效）。"
        events.append({"type": "final", "text": text, "cards": []})
        return {"final": {"text": text, "cards": []}, "events": events}

    result = state.get("tool_result")
    # 使用统计（P2.3，PRD 3.4）：仅计触达 MCP 的技能调用（tool_result 非
    # None）；permission 拒绝（tool_result None）不计，MCP 报错计失败
    if result is not None:
        store.record_usage(
            skill["name"], ok=not (isinstance(result, dict) and "error" in result)
        )
    if skill["name"] == "oa_todo_approve":
        draft = state.get("draft") or {}
        # 写分支：审批处理结果（HITL 确认后恢复图回到此处）
        if "approval_id" in draft and isinstance(result, dict):
            if "error" in result:
                text = f"审批处理失败：{result['error']}"
            elif result.get("idempotent_reuse"):
                text = f"该待办已处理过（幂等命中，未重复处理）。{result.get('message', '')}"
            else:
                text = (
                    f"待办审批完成：{result.get('message', '')}"
                    f"（单据号 {result.get('doc_no', result.get('approval_id', ''))}）"
                )
                comment = draft.get("comment", {}).get("value", "")
                if draft.get("action", {}).get("value") == "reject" and comment:
                    text += f"驳回原因「{comment}」已记录，将通知申请人。"
            events.append({"type": "final", "text": text, "cards": [result]})
            return {"final": {"text": text, "cards": [result]}, "events": events}
        # 读分支：待办列表（卡片驱动桌面端行内"同意/驳回"按钮，场景 2）
        if isinstance(result, dict) and "error" in result:
            text = f"待办查询失败：{result['error']}"
            events.append({"type": "final", "text": text, "cards": []})
            return {"final": {"text": text, "cards": []}, "events": events}
        items = result or []
        if not items:
            text = "当前没有待办审批。"
            cards: list[dict[str, Any]] = []
        else:
            lines = [
                f"{i + 1}. [{it['doc_type']}] {it['title']}（{it['submitted_at']}）"
                for i, it in enumerate(items)
            ]
            text = f"你有 {len(items)} 条待办审批：\n" + "\n".join(lines)
            cards = [{"type": "todo_list", "items": items}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # BI 查询（PRD 5.2 场景 3）：KPI 文本摘要 + bi_result 卡（KPI/趋势/拆分序列）
    if skill["name"] == "bi_query":
        if isinstance(result, dict) and "error" in result:
            text = f"BI 查询失败：{result['error']}"
            events.append({"type": "final", "text": text, "cards": []})
            return {"final": {"text": text, "cards": []}, "events": events}
        data = result if isinstance(result, dict) else {}
        kpi = data.get("kpi") or {}
        mom = data.get("mom_pct")
        text = (
            f"{kpi.get('region', '')} {kpi.get('period', '')} "
            f"{kpi.get('label', '')}：{kpi.get('value', 0):,.2f} 元"
        )
        if mom is not None:
            text += f"，环比{'+' if mom >= 0 else ''}{mom}%"
        if data.get("cached"):
            text += "（热点缓存命中）"
        cards = [{"type": "bi_result", **data}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 周报生成（PRD 5.2 场景 4）：聚合操作记录 → Markdown 草稿卡（编辑后复制/下载）
    if skill["name"] == "weekly_report":
        if isinstance(result, dict) and "error" in result:
            text = f"周报生成失败：{result['error']}"
            events.append({"type": "final", "text": text, "cards": []})
            return {"final": {"text": text, "cards": []}, "events": events}
        items = result if isinstance(result, list) else []
        markdown = rules.build_weekly_markdown(items)
        text = f"已生成本周工作周报草稿（{len(items)} 条操作记录），可在卡片中编辑、复制或下载。"
        cards = [{"type": "weekly_report", "markdown": markdown, "item_count": len(items)}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # ---- P2.1 业务系统技能渲染（PRD 6.1）----

    # CRM 销售订单提交结果（HITL 确认后恢复图回到此处；审批流含大额加签）
    if skill["name"] == "crm_sales_order_entry" and isinstance(result, dict):
        if "error" in result:
            text = f"订单提交失败：{result['error']}"
            events.append({"type": "final", "text": text, "cards": []})
            return {"final": {"text": text, "cards": []}, "events": events}
        await slots.clear_pending(state["user_id"], state["session_id"])
        flow = " → ".join(result.get("approval_flow") or [])
        text = (
            f"销售订单已提交（单据号 {result.get('doc_no', '')}），"
            f"合计 {result.get('total_amount', 0):,.2f} 元。"
        )
        if result.get("idempotent_reuse"):
            text += "（幂等命中，未重复提交）"
        if flow:
            text += f"审批流：{flow}，进度可在 CRM 跟单中查看。"
        events.append({"type": "final", "text": text, "cards": [result]})
        return {"final": {"text": text, "cards": [result]}, "events": events}

    # 客户 360：唯一命中渲染档案摘要；多候选/未命中列选项补问
    if skill["name"] == "crm_customer_360" and isinstance(result, dict):
        if "error" in result:
            text, cards = f"客户查询失败：{result['error']}", []
        elif "candidates" in result:
            cands = result["candidates"] or []
            if cands:
                lines = "\n".join(f"- {c['name']}（{c['customer_id']}）" for c in cands)
                text = f"匹配到 {len(cands)} 家客户，请指明是哪家：\n{lines}"
            else:
                text = "CRM 未找到该客户，请核对客户名称。"
            cards = []
        else:
            text, cards = rules.build_customer_360_text(result), [result]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 跟单进度：状态 + 节点推进 + 异常（PRD 6.1.2）
    if skill["name"] == "crm_order_track" and isinstance(result, dict):
        if "error" in result:
            text = f"跟单查询失败：{result['error']}"
            cards = []
        else:
            text = rules.build_order_progress_text(result)
            cards = [result]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # ERP 库存：行文本列表（缺料标记）
    if skill["name"] == "erp_inventory_query" and isinstance(result, list):
        rows = result
        lines = rules.build_inventory_lines(rows)
        text = "\n".join(lines) if lines else "未查询到库存记录。"
        cards = [{"type": "inventory", "items": rows}] if rows else []
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # ERP 采购单：行文本列表（在途 ETA）
    if skill["name"] == "erp_po_sync" and isinstance(result, list):
        rows = result
        lines = rules.build_po_lines(rows)
        text = "\n".join(lines) if lines else "未查询到采购订单。"
        cards = [{"type": "purchase_orders", "items": rows}] if rows else []
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 财务凭证摘要：期间/张数/借贷平衡 + 科目明细
    if skill["name"] == "erp_voucher_summary" and isinstance(result, dict):
        if "error" in result:
            text = f"凭证查询失败：{result['error']}"
            cards = []
        else:
            text = rules.build_voucher_text(result)
            cards = [{"type": "voucher_summary", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # WMS 预警 + 出入库单：合并文本（缺料/临期处置建议）
    if skill["name"] == "wms_stock_alert" and isinstance(result, dict):
        if "error" in result:
            text = f"出入库/预警查询失败：{result['error']}"
            cards = []
        else:
            text = rules.build_stock_text(result)
            cards = [{"type": "stock_alert", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # ---- P2.2 科室工作台渲染（PRD 6.2，五科室只读视图）----

    # 生产备料齐套：缺料清单 + 近期出库单
    if skill["name"] == "prod_material_check" and isinstance(result, dict):
        if "error" in result:
            text = f"备料检查失败：{result['error']}"
            cards = []
        else:
            text = rules.build_material_check_text(result)
            cards = [{"type": "material_check", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 计划物料到货视图：在途采购单 + 关联库存
    if skill["name"] == "plan_inbound_view" and isinstance(result, dict):
        if "error" in result:
            text = f"到货视图查询失败：{result['error']}"
            cards = []
        else:
            text = rules.build_inbound_view_text(result)
            cards = [{"type": "inbound_view", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 品管效期/批次追溯：临期预警批次明细 + 入库单来源
    if skill["name"] == "qa_batch_trace" and isinstance(result, dict):
        if "error" in result:
            text = f"效期追溯查询失败：{result['error']}"
            cards = []
        else:
            text = rules.build_batch_trace_text(result)
            cards = [{"type": "batch_trace", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 仓库运营概览：库存水位 + 待处置预警
    if skill["name"] == "wh_stock_overview" and isinstance(result, dict):
        if "error" in result:
            text = f"仓库概览查询失败：{result['error']}"
            cards = []
        else:
            text = rules.build_stock_overview_text(result)
            cards = [{"type": "stock_overview", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 信息科经营数据巡检：BI 快照 + 凭证平衡核查
    if skill["name"] == "it_data_check" and isinstance(result, dict):
        if "error" in result:
            text = f"数据巡检失败：{result['error']}"
            cards = []
        else:
            text = rules.build_data_check_text(result)
            cards = [{"type": "data_check", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # ---- P3.2 全部科室覆盖渲染（PLAN P3.2，PRD 7.2）：五科室只读视图 ----

    # 人力资源速览：本人近 7 天 OA 操作动态
    if skill["name"] == "hr_roster" and isinstance(result, list):
        lines = rules.build_hr_activity_lines(result)
        if lines:
            text = "\n".join(["近 7 天 OA 员工动态（本人）：", *lines])
            cards = [{"type": "hr_roster", "rows": result}]
        else:
            text = "近 7 天无 OA 操作记录。"
            cards = []
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 管理层速览：BI 当月 KPI + 凭证平衡（与数据巡检同构，复用渲染）
    if skill["name"] == "mgmt_overview" and isinstance(result, dict):
        if "error" in result:
            text = f"管理速览查询失败：{result['error']}"
            cards = []
        else:
            text = rules.build_data_check_text(result)
            cards = [{"type": "mgmt_overview", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 审计流水：本人近 20 条操作留痕（D3 敏感，仅本人范围）
    if skill["name"] == "audit_trace" and isinstance(result, dict):
        rows = result.get("rows") or []
        text = rules.build_audit_trace_text(rows)
        cards = [{"type": "audit_trace", "rows": rows}] if rows else []
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 技术备件巡检：备件库存水位 + 待处置预警（与仓库概览同构，复用渲染）
    if skill["name"] == "tech_inventory" and isinstance(result, dict):
        if "error" in result:
            text = f"备件巡检失败：{result['error']}"
            cards = []
        else:
            text = rules.build_stock_overview_text(result)
            cards = [{"type": "tech_inventory", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # 产品销售看板：BI 产品线拆分 + 可选客户 360 摘要
    if skill["name"] == "product_sales" and isinstance(result, dict):
        if "error" in result:
            text = f"产品看板查询失败：{result['error']}"
            cards = []
        else:
            text = rules.build_product_sales_text(result)
            cards = [{"type": "product_sales", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # P3.3 记住偏好：写入确认（可感知性——告知用户记住了什么，可到记忆页管理）
    if skill["name"] == "memory_save" and isinstance(result, dict):
        if "error" in result:
            text = f"没能记住：{result['error']}"
            cards = []
        else:
            text = f"已记住：{result['content']}（可在「我的记忆」页查看或删除）"
            cards = [{"type": "memory_save", "entry": result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # ---- P2.6 跨系统编排渲染（PLAN P2.6，PRD 6.3）：workflow run 终态 ----
    if skill["name"] == "cross_system_order_flow" and isinstance(result, dict):
        steps = result.get("steps") or []
        status = result.get("status", "failed")
        if status == "ok":
            await slots.clear_pending(state["user_id"], state["session_id"])
            head = "跨系统订单一条龙已完成："
        elif status == "partial":
            head = "编排部分执行（Ask 模式止步于写步骤）："
        else:
            head = "编排执行失败（写步骤幂等键兜底，可修正后重试）："
        lines = [f"- 第 {s.get('seq')} 步 {s.get('tool')}：{s.get('detail', '')}" for s in steps]
        text = "\n".join([head, *lines])
        events.append({"type": "final", "text": text, "cards": [result]})
        return {"final": {"text": text, "cards": [result]}, "events": events}

    # ---- P3.1 MES / U8 渲染（PLAN P3.1，PRD 7.1）：只读查询结果 ----

    # 生产报工：无工单号 → 工单进度列表；有单号 → 报工明细
    if skill["name"] == "mes_production_report" and isinstance(result, list):
        rows = result
        is_reports = bool((state.get("draft") or {}).get("work_order", {}).get("value"))
        lines = (
            rules.build_production_report_lines(rows)
            if is_reports
            else rules.build_work_order_lines(rows)
        )
        text = (
            "\n".join(lines)
            if lines
            else ("该工单暂无报工记录。" if is_reports else "未查询到工单。")
        )
        card_type = "production_reports" if is_reports else "work_orders"
        cards = [{"type": card_type, "rows": rows}] if rows else []
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    # U8 总账：科目余额表 / 凭证明细（按 draft 是否有凭证号分流）
    if skill["name"] == "u8_gl_summary" and isinstance(result, dict):
        if "error" in result:
            text = f"U8 查询失败：{result['error']}"
            cards = []
        elif (state.get("draft") or {}).get("voucher_no", {}).get("value"):
            text = rules.build_voucher_detail_text(result)
            cards = [{"type": "u8_voucher_detail", **result}]
        else:
            text = rules.build_gl_balance_text(result)
            cards = [{"type": "u8_gl_balance", **result}]
        events.append({"type": "final", "text": text, "cards": cards})
        return {"final": {"text": text, "cards": cards}, "events": events}

    if isinstance(result, dict) and "error" in result:
        events.append({"type": "final", "text": f"提交失败：{result['error']}", "cards": []})
        return {"final": {"text": f"提交失败：{result['error']}", "cards": []}, "events": events}

    if result and isinstance(result, dict) and "doc_no" in result:
        await slots.clear_pending(state["user_id"], state["session_id"])
        reuse = result.get("idempotent_reuse")
        draft = state.get("draft") or {}
        duration = draft.get("duration_days", {}).get("value")
        leave_type = draft.get("leave_type", {}).get("value")
        # 审批流说明（PRD 5.2：直属主管 → HR 备案，≥ 3 天加签部门总监）
        flow = "直属主管 → 部门总监 → HR 备案" if duration >= 3 else "直属主管 → HR 备案"
        text = f"请假申请已提交（单据号 {result['doc_no']}）。"
        if reuse:
            text += "（幂等命中，未重复提交）"
        text += f"审批流：{flow}，进度可在待办中查看。"
        if leave_type == "sick" and duration >= 2:
            text += "病假 ≥ 2 天：请补交医疗证明至 HR。"
        events.append({"type": "final", "text": text, "cards": [result]})
        return {"final": {"text": text, "cards": [result]}, "events": events}

    text = "操作已完成。"
    events.append({"type": "final", "text": text, "cards": []})
    return {"final": {"text": text, "cards": []}, "events": events}


def build_graph() -> StateGraph:  # type: ignore[type-arg]
    """构建八节点流水线图（主流程：完整对话轮，PLAN P1.1 增权限节点）。"""
    graph = StateGraph(ChatState)
    graph.add_node("intent", intent_node)
    graph.add_node("route", route_node)
    graph.add_node("extract", extract_node)
    graph.add_node("validate", validate_node)
    graph.add_node("permission", permission_node)
    graph.add_node("hitl", hitl_node)
    graph.add_node("execute", execute_node)
    graph.add_node("format", format_node)
    graph.add_edge(START, "intent")
    graph.add_edge("intent", "route")
    graph.add_edge("route", "extract")
    graph.add_edge("extract", "validate")
    graph.add_edge("validate", "permission")
    graph.add_edge("permission", "hitl")
    graph.add_edge("hitl", "execute")
    graph.add_edge("execute", "format")
    graph.add_edge("format", END)
    return graph


def build_resume_graph() -> StateGraph:  # type: ignore[type-arg]
    """恢复图：HITL 确认后从 execute 节点续跑（execute → format）。"""
    graph = StateGraph(ChatState)
    graph.add_node("execute", execute_node)
    graph.add_node("format", format_node)
    graph.add_edge(START, "execute")
    graph.add_edge("execute", "format")
    graph.add_edge("format", END)
    return graph


def build_plan_resume_graph() -> StateGraph:  # type: ignore[type-arg]
    """Plan 计划批准恢复图（hitl → execute → format，PLAN P2.6 PRD 3.3）。

    写步骤重进 hitl 挂确认卡（规则2 模式不豁免单步确认，形成两次
    确认链）；只读步骤 hitl 直通后执行。
    """
    graph = StateGraph(ChatState)
    graph.add_node("hitl", hitl_node)
    graph.add_node("execute", execute_node)
    graph.add_node("format", format_node)
    graph.add_edge(START, "hitl")
    graph.add_edge("hitl", "execute")
    graph.add_edge("execute", "format")
    graph.add_edge("format", END)
    return graph
