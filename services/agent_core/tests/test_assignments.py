"""assignment 任务单测试（P1-2 @分身委托会话）。

规则层：@提及抽取（工号/姓名）、截止短语（受控词表）、布置正文剥离。
Store 层：直属上级准入（防越权布置）、分身开关/冻结防线、状态机与操作者校验。
图层：用户原句端到端（@工号布置 + 补问续收）、员工查进度双视角、越权文案。
"""

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from agent_core import audit
from agent_core.accounts import store as accounts
from agent_core.api import auth as auth_mod
from agent_core.api.main import app
from agent_core.assignments import store as assignments
from agent_core.pipeline import llm, rules
from agent_core.pipeline.graph import build_graph
from agent_core.skills import store as skill_store
from agent_core.skills.registry import match_skill


@pytest.fixture(autouse=True)
def _reset_assignments() -> Iterator[None]:
    """测试隔离：任务域清空 + 分身运行时态复位 + 审计清空。"""
    assignments.reset()
    accounts.reset()
    asyncio.run(audit.clear())
    yield
    accounts.reset()
    asyncio.run(audit.clear())


# ---- 规则层 ----


def test_mention_extraction() -> None:
    """@提及：工号优先，中文姓名次之，无 @ 返回 None。"""
    assert rules.extract_mention("@E1002 完成盘点") == "E1002"
    assert rules.extract_mention("给 @李四 布置盘点") == "李四"
    assert rules.extract_mention("完成盘点") is None


def test_deadline_extraction() -> None:
    """截止短语受控词表：「周五前」「10月8日前」命中；「之前」不误伤。"""
    assert rules.extract_deadline("周五前完成盘点") == "周五前"
    assert rules.extract_deadline("10月8日前给我") == "10月8日前"
    assert rules.extract_deadline("15号前交付") == "15号前"
    assert rules.extract_deadline("月底前完成") == "月底前"
    assert rules.extract_deadline("在完成之前先对齐") is None
    assert rules.extract_deadline("完成销售报表") is None


def test_assign_content_extraction() -> None:
    """布置正文：剥离 @提及 / 截止短语 / 引导词，空正文返回 None。"""
    assert (
        rules.extract_assign_content("@E1002 周五前完成华东区销售报表", "E1002")
        == "完成华东区销售报表"
    )
    assert rules.extract_assign_content("请麻烦跟进合同回款") == "跟进合同回款"
    assert rules.extract_assign_content("@李四", "李四") is None


# ---- Registry 路由 ----


def test_match_twin_skills() -> None:
    assert match_skill("布置任务给分身") is not None
    assert match_skill("布置任务给分身")["name"] == "twin_assign_task"
    assert match_skill("我的任务进度") is not None
    assert match_skill("我的任务进度")["name"] == "twin_task_progress"


# ---- Store 层：准入防线 + 状态机 ----


async def test_create_requires_manager() -> None:
    """直属上级准入：E1003 可布置给 E1001；E1002（同级）越权被拒。"""
    task = await assignments.create("E1003", "E1001", "完成华东区销售报表", deadline="周五前")
    assert task["assigner_name"] == "王五"
    assert task["assignee_name"] == "张三"
    assert task["status"] == "pending"
    with pytest.raises(ValueError, match="仅直属上级"):
        await assignments.create("E1002", "E1001", "越权布置")


async def test_create_twin_and_frozen_guards() -> None:
    """分身未开启 / 账号冻结 / 未知账号均拒收任务。"""
    await accounts.set_twin("E1001", False, by="E8001")
    with pytest.raises(ValueError, match="数字分身未开启"):
        await assignments.create("E1003", "E1001", "盘点")
    await accounts.set_twin("E1001", True, by="E8001")
    await accounts.set_frozen("E1001", True, by="E8001")
    with pytest.raises(ValueError, match="已冻结"):
        await assignments.create("E1003", "E1001", "盘点")
    with pytest.raises(ValueError, match="不存在"):
        await assignments.create("E1003", "E9999", "盘点")


async def test_update_status_transitions() -> None:
    """状态机：接收人推进 in_progress→done；布置人可取消；他人/非法流转拒绝。"""
    task = await assignments.create("E1003", "E1001", "完成华东区销售报表")
    await assignments.update_status(task["id"], "in_progress", by="E1001")
    done = await assignments.update_status(task["id"], "done", by="E1001", note="已交付")
    assert done["status"] == "done" and done["done_note"] == "已交付"
    t2 = await assignments.create("E1003", "E1002", "另一单")
    with pytest.raises(ValueError, match="仅任务接收人"):
        await assignments.update_status(t2["id"], "in_progress", by="E1001")
    t3 = await assignments.create("E1003", "E1001", "可取消单")
    with pytest.raises(ValueError, match="仅布置人"):
        await assignments.update_status(t3["id"], "cancelled", by="E1001")
    cancelled = await assignments.update_status(t3["id"], "cancelled", by="E1003")
    assert cancelled["status"] == "cancelled"
    with pytest.raises(ValueError, match="不允许"):
        await assignments.update_status(t3["id"], "done", by="E1001")
    with pytest.raises(ValueError, match="不存在"):
        await assignments.update_status("ASSIGN-9999", "done", by="E1001")


async def test_overview_views() -> None:
    """双视角：布置给我的 + 我布置的，互不串列。"""
    await assignments.create("E1003", "E1001", "单A")
    await assignments.create("E1003", "E1001", "单B", deadline="下周五前")
    await assignments.create("E1003", "E1002", "单C")
    mine = assignments.overview("E1001")
    assert [t["title"] for t in mine["assigned_to_me"]] == ["单B", "单A"]  # 新的在前
    assert mine["assigned_by_me"] == []
    boss = assignments.overview("E1003")
    assert len(boss["assigned_by_me"]) == 3
    assert boss["assigned_to_me"] == []


# ---- 图层：对话端到端 ----


async def test_chat_assign_with_mention() -> None:
    """领导原句端到端：@工号 + 截止 + 正文一步齐备 → 直建任务单（无确认卡）。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {
            "user_id": "E1003",
            "session_id": "s-assign-1",
            "message": "布置任务 @E1001 周五前完成华东区销售报表",
        }
    )
    text = result["final"]["text"]
    assert "已向「张三」的数字分身布置任务" in text
    assert "完成华东区销售报表" in text and "周五前" in text
    tasks = assignments.list_by_assigner("E1003")
    assert len(tasks) == 1
    assert tasks[0]["assignee"] == "E1001"
    assert tasks[0]["deadline"] == "周五前"


async def test_chat_assign_reask_flow() -> None:
    """缺 @提及 → 补问；补 @姓名 → 缺正文 → 补问；补正文后创建。"""
    graph = build_graph().compile()
    session = "s-assign-reask"
    first = await graph.ainvoke(
        {"user_id": "E1003", "session_id": session, "message": "布置一个任务"}
    )
    assert "布置给哪位同事" in first["final"]["text"]
    assert assignments.list_by_assigner("E1003") == []
    second = await graph.ainvoke(
        {"user_id": "E1003", "session_id": session, "message": "@李四"}
    )
    assert "布置什么工作内容" in second["final"]["text"]
    third = await graph.ainvoke(
        {"user_id": "E1003", "session_id": session, "message": "完成三季度团建方案整理"}
    )
    assert "已向「李四」的数字分身布置任务" in third["final"]["text"]
    tasks = assignments.list_for_assignee("E1002")
    assert len(tasks) == 1 and tasks[0]["title"] == "完成三季度团建方案整理"


async def test_chat_assign_denied_for_non_manager() -> None:
    """越权布置：非直属上级（同级 E1001 @ E1002）→ 友好拒绝文案，不落单。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {
            "user_id": "E1001",
            "session_id": "s-assign-deny",
            "message": "布置任务 @E1002 完成巡检报告",
        }
    )
    assert "任务布置失败" in result["final"]["text"]
    assert "仅直属上级" in result["final"]["text"]
    assert assignments.overview("E1002")["assigned_to_me"] == []


async def test_chat_task_progress_both_views() -> None:
    """员工「我的任务」看布置给我的；领导「我布置的任务」看布置出去的。"""
    graph = build_graph().compile()
    await assignments.create("E1003", "E1001", "完成华东区销售报表", deadline="周五前")
    await assignments.create("E8001", "E1003", "提交科室月度总结")
    emp = await graph.ainvoke(
        {"user_id": "E1001", "session_id": "s-progress-1", "message": "我的任务"}
    )
    text = emp["final"]["text"]
    assert "布置给你的任务" in text and "完成华东区销售报表" in text and "王五" in text
    boss = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-progress-2", "message": "我布置的任务"}
    )
    text = boss["final"]["text"]
    assert "你布置的任务" in text and "张三" in text and "pending" in text


async def test_chat_task_update_done_by_assignee() -> None:
    """接收人聊天推进：完成任务号 → 已完成（store 层操作者校验）。"""
    task = await assignments.create("E1003", "E1001", "完成华东区销售报表", deadline="周五前")
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {
            "user_id": "E1001",
            "session_id": "s-update-1",
            "message": f"把{task['id']}标记完成",
        }
    )
    text = result["final"]["text"]
    assert task["id"] in text and "已推进为「已完成」" in text
    assert assignments.get(task["id"])["status"] == "done"


async def test_chat_task_update_reask_and_cancel() -> None:
    """缺任务号 → 补问；补任务号后由布置人取消 → 已取消。"""
    task = await assignments.create("E1003", "E1001", "完成U8对账", deadline="本月底")
    graph = build_graph().compile()
    session = "s-update-reask"
    first = await graph.ainvoke(
        {"user_id": "E1003", "session_id": session, "message": "取消任务"}
    )
    assert "推进哪个任务" in first["final"]["text"]
    second = await graph.ainvoke(
        {"user_id": "E1003", "session_id": session, "message": task["id"]}
    )
    assert "已推进为「已取消」" in second["final"]["text"]
    assert assignments.get(task["id"])["status"] == "cancelled"


async def test_chat_task_update_denied_for_outsider() -> None:
    """越权推进：非接收人（E1002）标记完成他人任务 → 失败文案，状态不变。"""
    task = await assignments.create("E1003", "E1001", "完成季度盘点")
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {
            "user_id": "E1002",
            "session_id": "s-update-deny",
            "message": f"完成任务{task['id']}",
        }
    )
    text = result["final"]["text"]
    assert "任务状态更新失败" in text and "仅任务接收人" in text
    assert assignments.get(task["id"])["status"] == "pending"


# ---- 图层：@分身自由对话（P1-2.1 提及兜底）----


async def test_chat_mention_chat_twin_reply() -> None:
    """@分身非布置类消息 → 分身代答（LLM 未配置回落分身固定文案）。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-mention-1", "message": "@李四 你是谁"}
    )
    text = result["final"]["text"]
    assert "李四" in text and "数字分身" in text


async def test_chat_mention_chat_twin_disabled_falls_back() -> None:
    """被@者分身关闭 → 不代答，落闲聊兜底（回复不含分身标识）。"""
    await accounts.set_twin("E1002", False, by="E8001")
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-mention-2", "message": "@李四 你是谁"}
    )
    assert "数字分身" not in result["final"]["text"]


async def test_chat_mention_chat_passes_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """@分身自由对话：账号档案（管理后台已补录的公开口径）随人设下发，
    使「你是哪里人」类问题以档案事实作答、未补录字段如实说未记录。"""
    captured: dict[str, Any] = {}

    async def fake_twin_chat(message: str, **kw: Any) -> str:
        captured.update(kw)
        return "代答"

    monkeypatch.setattr(llm, "twin_chat", fake_twin_chat)
    await accounts.update_profile(
        "E1002", {"native_place": "浙江杭州", "education": "本科"}, by="E8001"
    )
    graph = build_graph().compile()
    await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-mention-prof", "message": "@李四 你是哪里人"}
    )
    assert captured["twin_emp_no"] == "E1002"
    assert captured["profile"] == {"native_place": "浙江杭州", "education": "本科"}


async def test_chat_mention_chat_self_or_unknown_falls_back() -> None:
    """@自己 / @无法解析的人名 → 不代答，落闲聊兜底。"""
    graph = build_graph().compile()
    self_mention = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-mention-3", "message": "@王五 你是谁"}
    )
    assert "数字分身" not in self_mention["final"]["text"]
    unknown = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-mention-4", "message": "@赵六 你是谁"}
    )
    assert "数字分身" not in unknown["final"]["text"]


# ---- 会话锁定分身（P1-2.3：会话框选择固定分身，本会话无需每条 @）----


async def test_locked_twin_routes_without_mention() -> None:
    """锁定分身后消息不带 @也由该分身代答（LLM 未配置回落分身固定文案）。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-lock-1", "message": "你是哪里人", "twin_emp_no": "E1002"}
    )
    text = result["final"]["text"]
    assert "李四" in text and "数字分身" in text


async def test_locked_twin_explicit_mention_wins() -> None:
    """消息内显式 @ 优先于锁定值：@郑拓 仍以郑拓分身代答。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-lock-2", "message": "@郑拓 你是谁", "twin_emp_no": "E1002"}
    )
    text = result["final"]["text"]
    assert "郑拓" in text and "李四" not in text


async def test_locked_twin_disabled_falls_back() -> None:
    """锁定分身被关闭 → 可达性校验失败，回落闲聊兜底（不代答）。"""
    await accounts.set_twin("E1002", False, by="E8001")
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-lock-3", "message": "你是哪里人", "twin_emp_no": "E1002"}
    )
    assert "数字分身" not in result["final"]["text"]


async def test_locked_twin_self_rejected() -> None:
    """锁定自己 → @自己语义不成立，不代答。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-lock-4", "message": "你是谁", "twin_emp_no": "E1003"}
    )
    assert "数字分身" not in result["final"]["text"]


async def test_locked_twin_assign_task_defaults_to_locked() -> None:
    """锁定分身 + 布置语义（截止短语）→ 任务单流程，接收人默认锁定分身
    （E1003 是 E1002 直属上级，store 准入通过；无 @ 不再补问布置给谁）。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {
            "user_id": "E1003",
            "session_id": "s-lock-5",
            "message": "周五前完成华东区销售报表",
            "twin_emp_no": "E1002",
        }
    )
    text = result["final"]["text"]
    assert "已向「李四」的数字分身布置任务" in text
    assert "华东区销售报表" in text


# ---- REST：会话锁定分身候选（P1-2.3 App 会话框下拉数据源）----


def _bearer_headers(rsa_key: Any, sub: str) -> dict[str, str]:
    """签发测试 JWT（口径同 test_api_auth.make_token，本文件内自用）。"""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": auth_mod.SSO_ISSUER,
        "sub": sub,
        "idp": "ad",
        "idp_sub": f"{sub}@corp.com",
        "dept": "事业部A/销售科",
        "roles": ["employee"],
        "perm_ver": 17,
        "aud": auth_mod.SSO_AUDIENCE,
        "sid": f"sess-{sub}",
        "iat": now,
        "exp": now + 1800,
        "jti": f"jti-{sub}",
    }
    token = pyjwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-kid"})
    return {"Authorization": f"Bearer {token}"}


def test_rest_twin_candidates(rsa_key: Any) -> None:
    """/twins：候选 = 分身开启且未冻结且非本人；profile 一并下发（下拉展示）。"""
    client = TestClient(app)
    r = client.get("/twins")
    assert r.status_code == 200
    smoke = r.json()["items"]
    assert len(smoke) == 13  # 冒烟口径（未认证 user_id=""）：全目录分身开启且未冻结
    assert {"emp_no", "name", "dept", "profile"} <= set(smoke[0])

    asyncio.run(accounts.set_twin("E1002", False, by="E8001"))
    r = client.get("/twins", headers=_bearer_headers(rsa_key, "E1003"))
    items = r.json()["items"]
    assert all(i["emp_no"] not in {"E1002", "E1003"} for i in items)  # 关闭/本人不列
    assert len(items) == 11  # 13 - 关闭的 E1002 - 本人 E1003


# ---- 分身能力清单（P1-2.1 能力上下文注入：只列实际授予的技能）----


def test_twin_capabilities_matches_grant_state() -> None:
    """能力清单=实际授予：未授予不列（read 不再默认放行）、授予即列入、
    回收即消失；twin_* 交互协议技能不列入。"""
    from agent_core.pipeline.graph import _twin_capabilities

    def caps_of() -> list[str]:
        return _twin_capabilities("E1002", accounts.get("E1002"))

    meta = {m["name"]: m for m in skill_store.list_meta()}
    read_name, read_title = next(
        (n, m["title"])
        for n, m in meta.items()
        if m["rw"] == "read" and not m.get("dept_scope") and not n.startswith("twin_")
    )
    caps = caps_of()
    assert not any(c.startswith("数字分身") for c in caps)  # 协议技能排除
    asyncio.run(skill_store.uninstall(read_name, user_id="E1002"))
    assert read_title not in caps_of()  # 未授予 → 不列（不虚报全系统清单）
    asyncio.run(skill_store.grant(read_name, user_id="E1002", by="E8001"))
    assert read_title in caps_of()  # 管理后台授予 → 列入
    asyncio.run(skill_store.revoke_access(read_name, user_id="E1002", by="E8001"))
    assert read_title not in caps_of()  # 回收授权 → 立即消失


async def test_chat_mention_chat_capabilities_in_fallback() -> None:
    """LLM 未配置：固定回落文案含「可代为」+ 实际授予的技能名与布置引导。"""
    meta = {m["name"]: m for m in skill_store.list_meta()}
    read_name, read_title = next(
        (n, m["title"])
        for n, m in meta.items()
        if m["rw"] == "read" and not m.get("dept_scope") and not n.startswith("twin_")
    )
    await skill_store.grant(read_name, user_id="E1002", by="E8001")
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-mention-cap", "message": "@李四 你有什么技能"}
    )
    text = result["final"]["text"]
    assert "可代为" in text and read_title in text and "数字分身" in text


async def test_chat_mention_deadline_beats_business_keyword() -> None:
    """@分身布置含业务关键词（销售订单）：进任务单流程，不卷入业务表单。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {
            "user_id": "E1003",
            "session_id": "s-assign-kw",
            "message": "@张三 周五前完成CRM 销售订单测试工作",
        }
    )
    text = result["final"]["text"]
    assert "已向「张三」的数字分身布置任务" in text
    assert "客户" not in text  # 不触发 CRM 录单补问
    tasks = [
        t
        for t in assignments.list_by_assigner("E1003")
        if t["assignee"] == "E1001" and "销售订单" in t["title"]
    ]
    assert tasks and tasks[0]["deadline"] == "周五前"


async def test_chat_mention_business_keyword_without_deadline_chats() -> None:
    """@分身 + 业务关键词但无截止/布置词：分身代答，不进业务表单。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {"user_id": "E1003", "session_id": "s-mention-kw", "message": "@张三 销售订单怎么录"}
    )
    text = result["final"]["text"]
    assert "数字分身" in text and "客户编码" not in text
