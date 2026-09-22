"""知识库测试（PLAN P2.5，PRD 9.5）。

单元层（store/embedding）：切片（标题层级路径/无标题正文/超长句界二次切片）、
入库校验（D4 拒绝/非法 space/分类/来源/dept_scope 强制）、状态机全迁移
（draft→pending_review→published→deprecated + 非法迁移 ValueError + 驳回
改后重提）、版本更新（version+1 回 draft 重切片）、D3 脱敏（手机号/身份证）、
检索（本地后端命中/section 标题路径强信号/空间过滤/tags 过滤/阈值 Top-K/
缺口记录/命中统计）、后端切换自愈（api→local reindex 全量重建）。
流水线层（graph 四注入点 e2e）：route 消歧转 knowledge_qa 纯 RAG 流（不调
业务工具）、负例未命中记缺口、extract 缺字段注入填写提示、weekly_report
执行后尾部知识引用、dept 空间本科室隔离（auth.dept 尾段口径）。
API 层（TestClient + make_token）：恒需认证 401、group 空间管理权限 403、
上传→提交→审核发布全流程、检索 + D3 脱敏、会话产物沉淀 + 跨科室 404/
403、列表空间可见性、D4 拒绝 + 版本更新回 draft + 下线即失效、stats/gaps。
"""

import asyncio
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from agent_core import audit
from agent_core.api import auth as auth_mod
from agent_core.api.main import app
from agent_core.knowledge import embedding as kb_embedding
from agent_core.knowledge import store as knowledge_store
from agent_core.knowledge.embedding import LOCAL_TAG, THRESHOLD_LOCAL, _embed_local
from agent_core.knowledge.store import _mask_sensitive, _split_chunks
from agent_core.pipeline.graph import build_graph

ISSUER = auth_mod.SSO_ISSUER
AUDIENCE = auth_mod.SSO_AUDIENCE


def make_token(rsa_key: Any, **overrides: Any) -> str:
    """按 PRD 8.5.4 claims 结构签发测试 token（与 test_automation 同口径）。"""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": "E1001",
        "idp": "ad",
        "idp_sub": "zhangsan@corp.com",
        "dept": "事业部A/销售科",
        "roles": ["employee"],
        "perm_ver": 17,
        "aud": AUDIENCE,
        "sid": "sess-1",
        "iat": now,
        "exp": now + 1800,
        "jti": "jti-1",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return pyjwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-kid"})


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_knowledge(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """测试隔离：强制本地嵌入后端（确定性）+ 知识域/向量缓存/审计清空。"""
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    knowledge_store.reset()
    kb_embedding.reset()
    auth_mod.set_sso_required(False)
    asyncio.run(audit.clear())
    yield
    asyncio.run(audit.clear())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


async def _pub(
    title: str,
    content: str,
    *,
    space: str = "group",
    dept_scope: str | None = None,
    classification: str = "D2",
    tags: list[str] | None = None,
    uploaded_by: str = "E9001",
    source: str = "upload",
) -> dict[str, Any]:
    """便捷入库：上传 → 提交 → 审核通过（缺省直达 published 已向量化；D2+ 双人复核）。"""
    doc = await knowledge_store.upload(
        title=title,
        content=content,
        space=space,
        dept_scope=dept_scope,
        classification=classification,
        tags=tags,
        uploaded_by=uploaded_by,
        source=source,
    )
    await knowledge_store.submit(doc["doc_id"], by=uploaded_by)
    pub = await knowledge_store.review(doc["doc_id"], approve=True, by="E9002")
    if pub["status"] != "published":  # D2+ 双人复核：第二审核人放行（PRD 5.6.4）
        pub = await knowledge_store.review(doc["doc_id"], approve=True, by="E9003")
    return pub


# ---- 切片（PRD 9.5.2：按标题层级，单块 ≤500 字，保留文档路径元数据）----


def test_split_chunks_heading_paths() -> None:
    """标题层级切片：section 为「一级 / 二级 / 三级」路径，同级回退不串级。"""
    content = (
        "# 员工手册\n## 考勤\n上班时间九点。\n### 请假\n年假需提前申请。\n## 报销\n月底前提交。"
    )
    chunks = _split_chunks(content)
    assert chunks == [
        ("员工手册 / 考勤", "上班时间九点。"),
        ("员工手册 / 考勤 / 请假", "年假需提前申请。"),
        ("员工手册 / 报销", "月底前提交。"),
    ]


def test_split_chunks_plain_text() -> None:
    """无标题内容视为「正文」单节。"""
    assert _split_chunks("第一段。\n第二段。") == [("正文", "第一段。\n第二段。")]


def test_split_chunks_long_text_rechunk() -> None:
    """超长正文按句边界二次切片：每块 ≤500 字且顺序保持。"""
    text = "甲" * 300 + "。" + "乙" * 300 + "。"
    chunks = _split_chunks(text)
    assert [sec for sec, _ in chunks] == ["正文", "正文"]
    assert all(len(text_part) <= 500 for _, text_part in chunks)
    assert chunks[0][1].startswith("甲") and chunks[1][1].startswith("乙")


# ---- 入库校验 + 状态机（PRD 9.5.2）----


async def test_upload_validation() -> None:
    """入库校验：D4 拒绝入库、非法枚举、dept 空间强制 dept_scope、group 忽略。"""
    with pytest.raises(ValueError, match="D4 机密不入知识库"):
        await knowledge_store.upload(
            title="机密", content="x", space="group", classification="D4", uploaded_by="u"
        )
    with pytest.raises(ValueError, match="classification"):
        await knowledge_store.upload(
            title="t", content="x", space="group", classification="D9", uploaded_by="u"
        )
    with pytest.raises(ValueError, match="space"):
        await knowledge_store.upload(
            title="t", content="x", space="personal", uploaded_by="u"
        )
    with pytest.raises(ValueError, match="dept_scope"):
        await knowledge_store.upload(title="t", content="x", space="dept", uploaded_by="u")
    with pytest.raises(ValueError, match="source"):
        await knowledge_store.upload(
            title="t", content="x", space="group", source="email", uploaded_by="u"
        )
    doc = await knowledge_store.upload(
        title="集团制度", content="x", space="group", dept_scope="销售科", uploaded_by="u"
    )
    assert doc["space"] == "group" and doc["dept_scope"] is None  # group 空间忽略科室
    assert doc["status"] == "draft" and doc["version"] == 1
    assert doc["chunks"][0]["chunk_id"] == f"{doc['doc_id']}_c00"


async def test_state_machine_lifecycle() -> None:
    """状态机：发布后不可再提交/再审/更新；下线仅从 published；下线即检索失效。"""
    # 正文与 query 全等 → bigram cosine=1.0（检索行为确定性，不受阈值影响）
    doc = await _pub("周报规范", "周报规范")
    assert doc["status"] == "published" and doc["reviewed_by"] == "E9003"  # 第二复核人
    with pytest.raises(ValueError, match="不可提交"):
        await knowledge_store.submit(doc["doc_id"], by="u")
    with pytest.raises(ValueError, match="不在审核中"):
        await knowledge_store.review(doc["doc_id"], approve=True, by="u")
    dep = await knowledge_store.deprecate(doc["doc_id"], by="E9002", note="制度过期")
    assert dep["status"] == "deprecated" and dep["deprecated_at"]
    assert all(c["vector"] is None for c in dep["chunks"])  # 向量立即出索引
    with pytest.raises(ValueError, match="不可下线"):
        await knowledge_store.deprecate(doc["doc_id"], by="E9002")
    with pytest.raises(ValueError, match="不可更新"):
        await knowledge_store.update(doc["doc_id"], content="new", by="u")
    res = await knowledge_store.search("周报规范", dept="")
    assert res["results"] == []  # R10：下线即失效


async def test_reject_then_revise_flow() -> None:
    """驳回流：reject → rejected；update 重切片 version+1 回 draft；重提后发布。"""
    doc = await knowledge_store.upload(
        title="报销制度", content="旧内容。", space="group", uploaded_by="E1001"
    )
    await knowledge_store.submit(doc["doc_id"], by="E1001")
    rejected = await knowledge_store.review(
        doc["doc_id"], approve=False, by="E9002", note="内容太薄"
    )
    assert rejected["status"] == "rejected" and rejected["review_note"] == "内容太薄"
    updated = await knowledge_store.update(
        doc["doc_id"], content="# 新报销制度\n新规则。", by="E1001"
    )
    assert updated["status"] == "draft" and updated["version"] == 2
    assert updated["chunks"][0]["section"] == "新报销制度"  # 重新切片
    await knowledge_store.submit(doc["doc_id"], by="E1001")
    first = await knowledge_store.review(doc["doc_id"], approve=True, by="E9002")
    assert first["status"] == "pending_review"  # D2 双人复核：第一核保持待复核
    republished = await knowledge_store.review(doc["doc_id"], approve=True, by="E9003")
    assert republished["status"] == "published" and republished["version"] == 2


async def test_state_machine_invalid_transitions() -> None:
    """非法迁移：pending_review 不可重复提交；不存在文档 ValueError。"""
    doc = await knowledge_store.upload(
        title="t", content="x", space="group", uploaded_by="u"
    )
    await knowledge_store.submit(doc["doc_id"], by="u")
    with pytest.raises(ValueError, match="不可提交"):
        await knowledge_store.submit(doc["doc_id"], by="u")
    with pytest.raises(ValueError, match="不存在"):
        await knowledge_store.submit("kb_99999", by="u")
    # 空内容校验需在 draft 状态（pending_review 先被状态检查拦截）
    with pytest.raises(ValueError, match="不存在"):
        await knowledge_store.update("kb_99999", content="x", by="u")
    draft = await knowledge_store.upload(
        title="d", content="x", space="group", uploaded_by="u"
    )
    with pytest.raises(ValueError, match="内容不能为空"):
        await knowledge_store.update(draft["doc_id"], content="  ", by="u")


# ---- D3 脱敏（PRD 10.2）----


def test_mask_sensitive_patterns() -> None:
    """脱敏规则：手机号 138****5678、身份证前 6 后 4，非敏感文本不动。"""
    assert (
        _mask_sensitive("联系张三 13812345678 或 15987654321")
        == "联系张三 138****5678 或 159****4321"
    )
    assert _mask_sensitive("身份证 110101199001011234") == "身份证 110101********1234"
    assert _mask_sensitive("工号 E1001，共 3 人") == "工号 E1001，共 3 人"


async def test_search_masks_d3_results() -> None:
    """D3 文档检索命中后文本脱敏（存储原文不动，PRD D4 不入/D3 脱敏展示）。"""
    raw = "张三电话 13812345678。"
    await _pub("项目联系人清单", raw, classification="D3")
    res = await knowledge_store.search("张三电话", dept="")
    assert len(res["results"]) == 1
    text = res["results"][0]["text"]
    assert "138****5678" in text and "13812345678" not in text
    assert res["results"][0]["classification"] == "D3"
    assert knowledge_store.detail(res["results"][0]["doc_id"])["content"] == raw


# ---- RAG 检索（PRD 9.5.3）：阈值/空间/标签/统计/缺口 ----


async def test_search_hit_and_stats() -> None:
    """命中：backend 标记 + 阈值断言 + 标题路径 section 强信号 + 命中统计。"""
    doc = await _pub("报销流程", "# 报销流程\n## 发票要求\n增值税发票需在月底前提交财务。", tags=["报销"])
    res = await knowledge_store.search("报销流程", dept="")
    assert res["backend"] == LOCAL_TAG
    assert len(res["results"]) == 1
    top = res["results"][0]
    assert top["doc_id"] == doc["doc_id"]
    assert top["score"] >= THRESHOLD_LOCAL
    assert top["section"] == "报销流程 / 发票要求"  # 命中标题路径而非正文
    assert top["version"] == 1 and top["space"] == "group"
    stats = knowledge_store.stats_overview()
    assert stats[0]["doc_id"] == doc["doc_id"] and stats[0]["hits"] == 1
    assert stats[0]["last_hit_at"] is not None


async def test_search_miss_records_gap() -> None:
    """未命中：空结果 + 知识缺口记录（query/dept/计数，PRD 9.5.4）。"""
    await _pub("报销流程", "增值税发票需在月底前提交。")
    res = await knowledge_store.search("今天天气怎么样", dept="销售科")
    assert res["results"] == []
    gaps = knowledge_store.gaps_list()
    assert gaps[0]["query"] == "今天天气怎么样"
    assert gaps[0]["dept"] == "销售科" and gaps[0]["count"] == 1
    await knowledge_store.search("今天天气怎么样", dept="销售科")
    assert knowledge_store.gaps_list()[0]["count"] == 2  # 同问累计


async def test_search_dept_space_isolation() -> None:
    """科室空间隔离：本科室可检索，跨科室不可见（group 空间全员）。"""
    await _pub("销售科值班表", "销售科值班表每周轮换。", space="dept", dept_scope="销售科")
    hit = await knowledge_store.search("销售科值班表", dept="销售科")
    assert len(hit["results"]) == 1 and hit["results"][0]["space"] == "dept"
    miss = await knowledge_store.search("销售科值班表", dept="生产科")
    assert miss["results"] == []
    group = await _pub("集团通讯录", "集团通讯录收录总机号码。")
    cross = await knowledge_store.search("集团通讯录", dept="生产科")
    assert [r["doc_id"] for r in cross["results"]] == [group["doc_id"]]


async def test_search_tags_filter() -> None:
    """标签过滤（注入点2/4 领域限定）：tags 无交集即过滤，命中走 section 信号。"""
    await _pub("请假制度", "# 请假制度\n年假需提前三天申请。", tags=["请假"])
    filtered = await knowledge_store.search("请假制度", dept="", tags_filter=["周报"])
    assert filtered["results"] == []
    hit = await knowledge_store.search("请假制度", dept="", tags_filter=["请假", "考勤"])
    assert len(hit["results"]) == 1 and hit["results"][0]["tags"] == ["请假"]


async def test_search_top_k() -> None:
    """Top-K 截断：多文档命中时按分数降序取前 K。"""
    await _pub("文档甲", "会议纪要模板正文内容。")
    await _pub("文档乙", "会议纪要模板正文内容。")
    res = await knowledge_store.search("会议纪要模板", dept="", top_k=1)
    assert len(res["results"]) == 1


async def test_reindex_after_backend_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """后端切换自愈（R10）：api 入库 → 切回本地 → 检索检测不一致自动 reindex。"""
    async def fake_post(texts: list[str]) -> list[list[float]]:
        return [_embed_local(t) for t in texts]

    monkeypatch.setattr(kb_embedding, "_post_embeddings", fake_post)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://fake-embed")
    # 正文与 query 全等 → cosine=1.0，越过 API 阈值 0.75（本地向量伪装 api 后端）
    doc = await _pub("周报规范", "周报规范")
    res = await knowledge_store.search("周报规范", dept="")
    assert res["backend"].startswith("api:") and len(res["results"]) == 1
    monkeypatch.delenv("EMBEDDING_BASE_URL")  # 切回本地后端
    res2 = await knowledge_store.search("周报规范", dept="")
    assert res2["backend"] == LOCAL_TAG  # 自动全量重建并按本地阈值命中
    assert [r["doc_id"] for r in res2["results"]] == [doc["doc_id"]]


# ---- 四注入点 e2e（graph，PRD 9.5.3）----


async def _seed_kb() -> None:
    """集团知识库种子文档：请假/周报（技能绑定标签）+ 入职（纯知识问答）。"""
    await _pub(
        "请假申请填写说明",
        "# 请假申请填写说明\n## 请假申请\n年假需提前 3 个工作日在 OA 提交，事由必填、时长按工作日计算。",
        tags=["请假"],
    )
    await _pub(
        "周报生成规范",
        "# 周报生成规范\n## 周报生成\n周报每周五 17:00 前提交，内容涵盖本周完成事项与下周计划。",
        tags=["周报"],
    )
    await _pub(
        "新员工入职指引",
        "# 新员工入职指引\n## 入职流程\n入职首日到 HR 报到，领取账号后加入企业微信。",
        tags=["入职"],
    )


def _finals(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in state.get("events", []) if e.get("type") == "final"]


async def _invoke(message: str, auth_ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    graph = build_graph().compile()
    payload: dict[str, Any] = {
        "user_id": "u-kb",
        "session_id": f"s-kb-{abs(hash(message)) % 10000}",
        "message": message,
    }
    if auth_ctx is not None:
        payload["auth"] = auth_ctx
    return await graph.ainvoke(payload)


async def test_inject1_route_disambiguation_to_knowledge_qa() -> None:
    """注入点1：技能未命中 → 知识检索命中 → knowledge_qa 纯 RAG 流（不调业务工具）。"""
    await _seed_kb()
    state = await _invoke("新员工入职流程是什么？")
    assert state["skill"]["name"] == "knowledge_qa"
    assert state["tool_result"] is None  # 纯 RAG 流不调业务工具
    text = _finals(state)[0]["text"]
    assert text.startswith("根据知识库检索结果")
    assert "入职首日到 HR 报到" in text
    assert "来源：《新员工入职指引》" in text  # 来源溯源（PRD 9.5.3）


async def test_inject1_negative_records_gap() -> None:
    """注入点1 负例：知识也未命中 → 闲聊兜底 + 记知识缺口。"""
    await _seed_kb()
    state = await _invoke("今天天气不错，适合出去走走")
    assert state["skill"] is None
    assert _finals(state)[0]["text"].startswith("收到。我是你的工作助手")
    assert knowledge_store.gaps_list()[0]["query"] == "今天天气不错，适合出去走走"


async def test_inject2_extract_field_hint() -> None:
    """注入点2：请假技能缺字段 → 检索技能标题短语 → stage_progress 填写提示。"""
    await _seed_kb()

    def _mock_oa(name: str, arguments: dict[str, Any]) -> Any:
        assert name == "oa__query_leave_balance"
        return [{"leave_type": "annual", "remaining_days": 5}]

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_oa):
        state = await _invoke("我要请年假")
    assert state["skill"]["name"] == "oa_leave_request"
    hints = [
        e
        for e in state["events"]
        if e.get("type") == "stage_progress"
        and e.get("stage") == "extract"
        and e.get("message", "").startswith("填写提示")
    ]
    assert len(hints) == 1
    assert "年假需提前 3 个工作日" in hints[0]["message"]
    assert "来源：《请假申请填写说明》" in hints[0]["message"]


async def test_inject4_execute_sop_reference() -> None:
    """注入点4：周报执行成功 → 检索绑定标签 SOP → 回复尾部附制度引用。"""
    await _seed_kb()

    def _mock_oa(name: str, arguments: dict[str, Any]) -> Any:
        assert name == "oa__query_activity_log"
        return []

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_oa):
        state = await _invoke("帮我生成本周工作周报")
    assert state["skill"]["name"] == "weekly_report"
    text = _finals(state)[0]["text"]
    assert "已生成本周工作周报草稿" in text
    assert "相关制度参考：" in text
    assert "周报每周五 17:00 前提交" in text
    assert "来源：《周报生成规范》" in text


async def test_inject1_dept_space_isolation() -> None:
    """科室空间经流水线隔离：本科室命中、跨科室未命中记缺口（auth.dept 尾段）。"""
    await _pub(
        "差旅报销标准",
        "# 差旅报销标准\n## 住宿标准\n差旅住宿标准：一线城市每晚 400 元，其他城市 300 元。",
        space="dept",
        dept_scope="销售科",
        tags=["报销"],
    )
    msg = "差旅报销标准是多少？"
    hit = await _invoke(msg, auth_ctx={"dept": "事业部A/销售科", "roles": ["employee"]})
    assert hit["skill"]["name"] == "knowledge_qa"
    assert "一线城市每晚 400 元" in _finals(hit)[0]["text"]
    miss = await _invoke(msg, auth_ctx={"dept": "事业部A/生产科", "roles": ["employee"]})
    assert miss["skill"] is None
    assert _finals(miss)[0]["text"].startswith("收到。")
    assert knowledge_store.gaps_list()[0]["dept"] == "生产科"


# ---- API 层（TestClient + make_token）----


def test_api_requires_auth(client: TestClient) -> None:
    """知识库端点恒需认证：无 token 401（口径对齐技能市场）。"""
    assert client.get("/knowledge/docs").status_code == 401
    assert client.get("/knowledge/stats").status_code == 401
    assert client.get("/knowledge/gaps").status_code == 401
    assert client.post("/knowledge/search", json={"query": "x"}).status_code == 401
    assert client.post(
        "/knowledge/docs", json={"title": "t", "content": "c"}
    ).status_code == 401


def test_api_group_space_permission(client: TestClient, rsa_key: Any) -> None:
    """group 空间：普通员工上传 403；knowledge_manager 可上传。"""
    emp = make_token(rsa_key, sub="E2001", dept="事业部A/销售科", roles=["employee"])
    resp = client.post(
        "/knowledge/docs",
        headers=auth(emp),
        json={"title": "t", "content": "c", "space": "group"},
    )
    assert resp.status_code == 403
    mgr = make_token(rsa_key, sub="E9001", dept="事业部A/信息科", roles=["knowledge_manager"])
    resp = client.post(
        "/knowledge/docs",
        headers=auth(mgr),
        json={"title": "集团报销制度", "content": "# 集团报销制度\n## 发票\n月底前提交。", "tags": ["报销"]},
    )
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["space"] == "group" and doc["status"] == "draft"


def test_api_group_lifecycle_and_search(client: TestClient, rsa_key: Any) -> None:
    """全流程：上传→提交→审核发布→检索命中→下线→检索失效。"""
    mgr = make_token(rsa_key, sub="E9001", dept="事业部A/信息科", roles=["knowledge_manager"])
    headers = auth(mgr)
    doc = client.post(
        "/knowledge/docs",
        headers=headers,
        json={"title": "集团报销制度", "content": "# 集团报销制度\n## 发票\n增值税发票月底前提交财务。", "tags": ["报销"]},
    ).json()
    doc_id = doc["doc_id"]
    assert client.post(f"/knowledge/docs/{doc_id}/submit", headers=headers).status_code == 200
    reviewed = client.post(
        f"/knowledge/docs/{doc_id}/review", headers=headers, json={"approve": True}
    ).json()
    assert reviewed["status"] == "pending_review"  # D2 双人复核：第一核
    mgr2 = make_token(rsa_key, sub="E9002", dept="事业部A/信息科", roles=["knowledge_manager"])
    reviewed = client.post(
        f"/knowledge/docs/{doc_id}/review", headers=auth(mgr2), json={"approve": True}
    ).json()
    assert reviewed["status"] == "published"
    emp = make_token(rsa_key, sub="E2001", dept="事业部A/销售科", roles=["employee"])
    res = client.post(
        "/knowledge/search", headers=auth(emp), json={"query": "集团报销制度"}
    ).json()
    assert len(res["results"]) == 1
    assert res["results"][0]["doc_id"] == doc_id
    assert client.post(
        f"/knowledge/docs/{doc_id}/deprecate", headers=headers, json={"note": "过期"}
    ).json()["status"] == "deprecated"
    res2 = client.post(
        "/knowledge/search", headers=auth(emp), json={"query": "集团报销制度"}
    ).json()
    assert res2["results"] == []  # 下线即检索失效


def test_api_session_deposit_and_cross_dept(client: TestClient, rsa_key: Any) -> None:
    """会话产物沉淀：员工保存到本科室空间走审核；跨科室 403 审核 / 404 详情。"""
    sales_emp = make_token(rsa_key, sub="E2001", dept="事业部A/销售科", roles=["employee"])
    # source=session + group 空间 → 400（沉淀仅科室空间）
    resp = client.post(
        "/knowledge/docs",
        headers=auth(sales_emp),
        json={"title": "t", "content": "c", "space": "group", "source": "session"},
    )
    assert resp.status_code == 400
    resp = client.post(
        "/knowledge/docs",
        headers=auth(sales_emp),
        json={"title": "销售科周会纪要", "content": "本周周会决议。", "space": "dept", "source": "session"},
    )
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["space"] == "dept" and doc["dept_scope"] == "销售科" and doc["source"] == "session"
    assert client.post(f"/knowledge/docs/{doc['doc_id']}/submit", headers=auth(sales_emp)).status_code == 200
    prod_mgr = make_token(rsa_key, sub="E3001", dept="事业部A/生产科", roles=["dept_manager"])
    assert (
        client.post(
            f"/knowledge/docs/{doc['doc_id']}/review", headers=auth(prod_mgr), json={"approve": True}
        ).status_code
        == 403  # 跨科室管理员无审核权
    )
    sales_mgr = make_token(rsa_key, sub="E2000", dept="事业部A/销售科", roles=["dept_manager"])
    first = client.post(
        f"/knowledge/docs/{doc['doc_id']}/review", headers=auth(sales_mgr), json={"approve": True}
    ).json()
    assert first["status"] == "pending_review"  # D2 双人复核：第一核
    sales_mgr2 = make_token(rsa_key, sub="E2002", dept="事业部A/销售科", roles=["dept_manager"])
    assert (
        client.post(
            f"/knowledge/docs/{doc['doc_id']}/review", headers=auth(sales_mgr2), json={"approve": True}
        ).json()["status"]
        == "published"
    )
    prod_emp = make_token(rsa_key, sub="E3002", dept="事业部A/生产科", roles=["employee"])
    assert client.get(f"/knowledge/docs/{doc['doc_id']}", headers=auth(prod_emp)).status_code == 404
    assert client.get(f"/knowledge/docs/{doc['doc_id']}", headers=auth(sales_emp)).status_code == 200


def test_api_docs_list_visibility(client: TestClient, rsa_key: Any) -> None:
    """列表可见性：group 全员；dept 仅本科室；跨科室查询 403；集团管理员全可见。"""
    mgr = make_token(rsa_key, sub="E9001", dept="事业部A/信息科", roles=["knowledge_manager"])
    group_doc = client.post(
        "/knowledge/docs",
        headers=auth(mgr),
        json={"title": "集团制度", "content": "c"},
    ).json()["doc_id"]
    # dept 文档由销售科管理员上传（_upload_scope 强制本科室归属）
    sales_mgr = make_token(rsa_key, sub="E2000", dept="事业部A/销售科", roles=["dept_manager"])
    await_doc = client.post(
        "/knowledge/docs",
        headers=auth(sales_mgr),
        json={"title": "销售科文档", "content": "c", "space": "dept"},
    ).json()["doc_id"]
    for doc_id, token in ((group_doc, mgr), (await_doc, sales_mgr)):
        client.post(f"/knowledge/docs/{doc_id}/submit", headers=auth(token))
        client.post(f"/knowledge/docs/{doc_id}/review", headers=auth(token), json={"approve": True})
    sales_emp = make_token(rsa_key, sub="E2001", dept="事业部A/销售科", roles=["employee"])
    items = client.get("/knowledge/docs", headers=auth(sales_emp)).json()["items"]
    assert {d["doc_id"] for d in items} == {group_doc, await_doc}
    prod_emp = make_token(rsa_key, sub="E3002", dept="事业部A/生产科", roles=["employee"])
    items = client.get("/knowledge/docs", headers=auth(prod_emp)).json()["items"]
    assert [d["doc_id"] for d in items] == [group_doc]
    resp = client.get(
        "/knowledge/docs", headers=auth(sales_emp), params={"dept_scope": "生产科"}
    )
    assert resp.status_code == 403
    all_dept = client.get(
        "/knowledge/docs", headers=auth(mgr), params={"space": "dept"}
    ).json()["items"]
    assert {d["doc_id"] for d in all_dept} == {await_doc}


def test_api_d4_reject_update_and_deprecate(client: TestClient, rsa_key: Any) -> None:
    """D4 拒绝入库；版本更新回 draft 重审；非上传人非管理员更新 403。"""
    mgr = make_token(rsa_key, sub="E9001", dept="事业部A/信息科", roles=["knowledge_manager"])
    resp = client.post(
        "/knowledge/docs",
        headers=auth(mgr),
        json={"title": "薪酬方案", "content": "c", "classification": "D4"},
    )
    assert resp.status_code == 400
    assert "D4 机密不入知识库" in resp.json()["detail"]
    doc = client.post(
        "/knowledge/docs", headers=auth(mgr), json={"title": "报销制度", "content": "v1 内容。"}
    ).json()
    doc_id = doc["doc_id"]
    emp = make_token(rsa_key, sub="E2001", dept="事业部A/销售科", roles=["employee"])
    resp = client.post(
        f"/knowledge/docs/{doc_id}/update", headers=auth(emp), json={"content": "篡改"}
    )
    assert resp.status_code == 403  # 非上传人非管理员
    updated = client.post(
        f"/knowledge/docs/{doc_id}/update", headers=auth(mgr), json={"content": "# v2\n新规则。"}
    ).json()
    assert updated["version"] == 2 and updated["status"] == "draft"  # 回 draft 重审
    client.post(f"/knowledge/docs/{doc_id}/submit", headers=auth(mgr))
    client.post(f"/knowledge/docs/{doc_id}/review", headers=auth(mgr), json={"approve": True})
    mgr2 = make_token(rsa_key, sub="E9002", dept="事业部A/信息科", roles=["knowledge_manager"])
    client.post(f"/knowledge/docs/{doc_id}/review", headers=auth(mgr2), json={"approve": True})
    res = client.post(
        "/knowledge/search", headers=auth(emp), json={"query": "新规则"}
    ).json()
    assert len(res["results"]) == 1 and res["results"][0]["version"] == 2


def test_api_stats_and_gaps(client: TestClient, rsa_key: Any) -> None:
    """使用统计与知识缺口：命中计 hits、未命中记 gaps（PRD 9.5.4）。"""
    mgr = make_token(rsa_key, sub="E9001", dept="事业部A/信息科", roles=["knowledge_manager"])
    doc = client.post(
        "/knowledge/docs", headers=auth(mgr), json={"title": "考勤制度", "content": "考勤制度规定每日打卡。"}
    ).json()
    client.post(f"/knowledge/docs/{doc['doc_id']}/submit", headers=auth(mgr))
    client.post(f"/knowledge/docs/{doc['doc_id']}/review", headers=auth(mgr), json={"approve": True})
    mgr2 = make_token(rsa_key, sub="E9002", dept="事业部A/信息科", roles=["knowledge_manager"])
    client.post(f"/knowledge/docs/{doc['doc_id']}/review", headers=auth(mgr2), json={"approve": True})
    emp = make_token(rsa_key, sub="E2001", dept="事业部A/销售科", roles=["employee"])
    client.post("/knowledge/search", headers=auth(emp), json={"query": "考勤制度"})
    client.post("/knowledge/search", headers=auth(emp), json={"query": "食堂菜单 today"})
    stats = client.get("/knowledge/stats", headers=auth(emp)).json()
    assert stats["count"] == 1 and stats["items"][0]["hits"] == 1
    gaps = client.get("/knowledge/gaps", headers=auth(emp)).json()
    assert gaps["count"] == 1 and gaps["items"][0]["query"] == "食堂菜单 today"
