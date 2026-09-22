"""权限面测试（PLAN P1.1，PRD 2.3/8.5.5）：

- permissions.check_skill_roles：技能级角色矩阵（any-of，仅写路径调用）
- permissions.check_region_scope：BI 区域白名单（数据级·业务维）
- 流水线集成：permission 节点拦截越权——拒绝落审计 permission_denied、
  不触达工具、不发确认卡，文案走 format 渲染（友好、不暴露权限细节）
"""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from agent_core import audit
from agent_core.pipeline import permissions, slots
from agent_core.pipeline.graph import build_graph

AUTH_EMPLOYEE: dict[str, Any] = {
    "user_id": "E1001",
    "dept": "事业部A/销售科",
    "roles": ["employee"],
    "perm_ver": 17,
}
AUTH_MANAGER: dict[str, Any] = {
    "user_id": "E1003",
    "dept": "事业部A/销售科",
    "roles": ["employee", "dept_manager"],
    "perm_ver": 17,
}

_APPROVAL_ITEMS: list[dict[str, Any]] = [
    {
        "approval_id": "AP-PERM-0001",
        "doc_type": "leave",
        "title": "张三的年假申请（2 天）",
        "applicant": "张三",
        "submitted_at": "2026-09-12T10:00:00",
    },
    {
        "approval_id": "AP-PERM-0002",
        "doc_type": "leave",
        "title": "李四的调休申请（1 天）",
        "applicant": "李四",
        "submitted_at": "2026-09-13T14:30:00",
    },
]


@pytest.fixture(autouse=True)
def _clean_audit() -> Any:
    asyncio.run(audit.clear())
    yield
    asyncio.run(audit.clear())


# ---- 单元：check_skill_roles（技能级·操作维）----


def test_roles_none_auth_passes() -> None:
    """auth 缺省（本地冒烟无 token）放行。"""
    skill = {"required_roles": ["dept_manager"]}
    assert permissions.check_skill_roles(None, skill) is None


def test_employee_can_submit_leave() -> None:
    skill = {"required_roles": ["employee"]}
    assert permissions.check_skill_roles(AUTH_EMPLOYEE, skill) is None


def test_any_of_role_match_passes() -> None:
    """any-of 语义：roles 与 required 有交集即放行。"""
    skill = {"required_roles": ["dept_manager", "hr_admin"]}
    assert permissions.check_skill_roles(AUTH_MANAGER, skill) is None


def test_no_required_roles_passes() -> None:
    assert permissions.check_skill_roles(AUTH_EMPLOYEE, {"required_roles": []}) is None


def test_employee_cannot_approve() -> None:
    """审批属管理动作：employee 无 dept_manager → 友好文案（不暴露权限细节）。"""
    message = permissions.check_skill_roles(
        AUTH_EMPLOYEE, {"required_roles": ["dept_manager"]}
    )
    assert message is not None
    assert message == "该操作需要审批权限，请联系主管处理。"


def test_missing_role_generic_message() -> None:
    """非管理类缺失：通用文案列出所需角色（employee 无 finance_admin）。"""
    message = permissions.check_skill_roles(
        AUTH_EMPLOYEE, {"required_roles": ["finance_admin"]}
    )
    assert message is not None
    assert "finance_admin" in message


# ---- 单元：check_region_scope（数据级·业务维）----


def test_region_allowed() -> None:
    assert permissions.check_region_scope(AUTH_EMPLOYEE, "本月华东区销售额") is None
    assert permissions.check_region_scope(AUTH_EMPLOYEE, "华南区业绩") is None


def test_region_denied() -> None:
    message = permissions.check_region_scope(AUTH_EMPLOYEE, "华北区销售额多少")
    assert message is not None
    assert "华北" in message
    assert "华东" in message  # 提示当前可见范围


def test_region_none_auth_passes() -> None:
    """auth 缺省不拦截（保持既有链路；服务侧仍有过滤兜底）。"""
    assert permissions.check_region_scope(None, "华北区销售额") is None


def test_user_regions_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """业务维白名单可按工号覆盖（生产置换 HR 主数据/权限中心）。"""
    monkeypatch.setitem(permissions.USER_REGIONS, "E9001", ("华北",))
    assert permissions.check_region_scope(
        {"user_id": "E9001", "roles": ["employee"]}, "华北区销售额"
    ) is None
    message = permissions.check_region_scope(
        {"user_id": "E9001", "roles": ["employee"]}, "华东区销售额"
    )
    assert message is not None and "华东" in message


def test_visible_regions_default() -> None:
    assert permissions.visible_regions("nobody") == permissions.DEFAULT_REGIONS


# ---- 流水线集成：permission 节点 ----


async def _run(message: str, auth: dict[str, Any] | None, session: str) -> dict[str, Any]:
    graph = build_graph().compile()
    state: dict[str, Any] = {
        "user_id": auth["user_id"] if auth else "u-perm",
        "session_id": session,
        "message": message,
    }
    if auth is not None:
        state["auth"] = auth
    return await graph.ainvoke(state)


async def test_pipeline_blocks_approve_write_for_employee() -> None:
    """employee 行内审批写路径 → 拒绝：无确认卡、不触达工具、落审计。"""
    user, session = "E1001", "s-perm-emp"
    await slots.set_query_result(user, session, _APPROVAL_ITEMS, "approval")
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return []

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_spy):
        final = await _run("同意第 2 条", AUTH_EMPLOYEE, session)

    assert final["final"]["text"] == "该操作需要审批权限，请联系主管处理。"
    assert final.get("confirm_token") is None
    assert final["tool_result"] is None
    assert calls == []  # 写入未触达 MCP

    denied = await audit.recent(action="permission_denied")
    assert len(denied) == 1
    assert denied[0]["tool"] == "oa__approve"
    assert denied[0]["result"] == "denied"
    assert denied[0]["user_id"] == user
    assert "审批权限" in denied[0]["detail"]


async def test_pipeline_blocks_bi_out_of_region() -> None:
    """BI 查询命中白名单外区域（数据级·业务维）→ Agent 侧前置拦截。"""
    calls: list[dict[str, Any]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append(arguments)
        return {}

    with patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_spy):
        final = await _run("华北区销售额多少", AUTH_EMPLOYEE, "s-perm-bi")

    assert "暂无「华北」" in final["final"]["text"]
    assert calls == []  # 请求未发往 BI 服务

    denied = await audit.recent(action="permission_denied")
    assert len(denied) == 1
    assert denied[0]["tool"] == "bi__execute_query"
    assert "华北" in denied[0]["params_digest"]


async def test_pipeline_bi_allowed_region_passes() -> None:
    """白名单内区域（华东）正常放行执行。"""
    result = {
        "query": "本月华东区销售额多少",
        "kpi": {"label": "销售额", "value": 100.0, "period": "2026-09", "region": "华东"},
        "mom_pct": None,
    }

    with patch(
        "agent_core.pipeline.graph.call_bi_tool", side_effect=lambda n, a: result
    ):
        final = await _run("本月华东区销售额多少", AUTH_EMPLOYEE, "s-perm-bi-ok")

    assert final["tool_result"]["kpi"]["value"] == 100.0
    assert await audit.recent(action="permission_denied") == []


async def test_pipeline_bi_region_not_checked_without_auth() -> None:
    """auth 缺省：区域不拦截，维持服务侧过滤兜底（既有链路回归）。"""
    from agent_core.mcp_client import McpToolError

    def _mock(name: str, arguments: dict[str, Any]) -> Any:
        raise McpToolError("无权限查询 华北 区域数据（权限范围：华东、华南）")

    with patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_mock):
        final = await _run("华北区销售额多少", None, "s-perm-bi-noauth")

    assert "BI 查询失败" in final["final"]["text"]


async def test_pipeline_manager_approve_write_passes() -> None:
    """dept_manager 写路径放行 → HITL 确认卡签发（confirm_issued 审计）。"""
    user, session = "E1003", "s-perm-mgr"
    await slots.set_query_result(user, session, _APPROVAL_ITEMS, "approval")
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=lambda n, a: []):
        final = await _run("同意第 1 条", AUTH_MANAGER, session)

    assert final.get("confirm_token") is not None
    assert await audit.recent(action="permission_denied") == []
    issued = await audit.recent(action="confirm_issued")
    assert len(issued) == 1
    assert issued[0]["tool"] == "oa__approve"
    assert issued[0]["user_id"] == user


async def test_pipeline_read_path_not_role_checked() -> None:
    """读路径（待办列表查询）不做角色校验：employee 也可查。"""
    session = "s-perm-read"
    with patch(
        "agent_core.pipeline.graph.call_oa_tool",
        side_effect=lambda n, a: [dict(it) for it in _APPROVAL_ITEMS],
    ):
        final = await _run("查一下我的待办审批", AUTH_EMPLOYEE, session)

    assert final["tool_result"] == _APPROVAL_ITEMS
    assert await audit.recent(action="permission_denied") == []
