"""记忆域测试（PLAN P3.3，PRD 9.1-9.3）。

单元层（store）：PIPL 同意门禁（未同意 ValueError / 撤回即清除全部个人记忆）、
敏感前置过滤（写入前拒绝）、L2 增删（同正文幂等 / 一键清除）、L3 升级
（dept_manager 门禁 + 敏感词二次校验 + 科室可见性隔离）、遗忘机制（90 天未引用
降权 / 180 天未引用归档不参与检索）、高频行为自动沉淀（同 skill+摘要连续 3 次
达标，达标归零重计）、recall Top-K（命中 touch 引用计数 / 归档跳过）、export_md
分节渲染。
流水线层（graph e2e）：「记住这个」→ memory_save 写入确认（已记住 + memory_save
卡片）、未同意 → 没能记住不阻塞、intent 阶段记忆注入（stage_progress 已参考记忆）。
API 层（TestClient + make_token）：恒需认证 401、consent→add→list→delete 全流程、
敏感词 400、promote 非管理员 403 / 管理员 200、export.md Markdown 下载。
"""

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from agent_core import audit
from agent_core.api import auth as auth_mod
from agent_core.api.main import app
from agent_core.knowledge import embedding as kb_embedding
from agent_core.memory import store as memory_store
from agent_core.pipeline.graph import build_graph, intent_node

ISSUER = auth_mod.SSO_ISSUER
AUDIENCE = auth_mod.SSO_AUDIENCE


def make_token(rsa_key: Any, **overrides: Any) -> str:
    """按 PRD 8.5.4 claims 结构签发测试 token（与 test_knowledge 同口径）。"""
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
def _reset_memory(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """测试隔离：强制本地嵌入后端（确定性）+ 记忆域/审计清空。"""
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    memory_store.reset()
    kb_embedding.reset()
    auth_mod.set_sso_required(False)
    asyncio.run(audit.clear())
    yield
    asyncio.run(audit.clear())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---- 单元层：store ----


async def test_consent_gate_and_revoke_purges() -> None:
    """PIPL 门禁（PRD 9.2）：未同意写入 ValueError；撤回同意即清除全部个人记忆。"""
    with pytest.raises(ValueError, match="PIPL"):
        await memory_store.add(user_id="E1001", content="偏好按周汇报")
    await memory_store.set_consent(user_id="E1001", granted=True)
    entry = await memory_store.add(user_id="E1001", content="偏好按周汇报")
    assert entry["layer"] == "L2" and entry["owner"] == "E1001"
    res = await memory_store.set_consent(user_id="E1001", granted=False)
    assert res == {"granted": False, "purged": 1}
    assert memory_store.list_entries(user_id="E1001", dept="销售科") == []
    assert memory_store.consent_of("E1001")["granted"] is False


async def test_sensitive_pre_filter() -> None:
    """敏感前置过滤（PRD 9.2 + ARCHITECTURE 4.9）：写入前拒绝而非读取时脱敏。"""
    await memory_store.set_consent(user_id="E1001", granted=True)
    for content in ("我的工资是 2 万", "合同金额 50 万", "银行卡号 6222…"):
        with pytest.raises(ValueError, match="敏感"):
            await memory_store.add(user_id="E1001", content=content)


async def test_add_idempotent_and_remove() -> None:
    """L2 增删（PRD 9.2 可感知性）：同层同属同正文幂等；仅本人可删。"""
    await memory_store.set_consent(user_id="E1001", granted=True)
    first = await memory_store.add(user_id="E1001", content="汇报偏好按周汇总")
    again = await memory_store.add(user_id="E1001", content="汇报偏好按周汇总")
    assert first["id"] == again["id"]  # 幂等
    rows = memory_store.list_entries(user_id="E1001", dept="销售科")
    assert len(rows) == 1
    with pytest.raises(ValueError, match="无权删除"):
        await memory_store.remove(
            entry_id=first["id"], user_id="E2002", dept="销售科", roles=["employee"]
        )
    await memory_store.remove(
        entry_id=first["id"], user_id="E1001", dept="销售科", roles=["employee"]
    )
    assert memory_store.list_entries(user_id="E1001", dept="销售科") == []


async def test_promote_permission_and_dept_visibility() -> None:
    """L2→L3（PRD 9.3）：仅科室管理员；本科室可见、跨科室不可见。"""
    await memory_store.set_consent(user_id="E1001", granted=True)
    l2 = await memory_store.add(user_id="E1001", content="本科室汇报默认带指标趋势")
    with pytest.raises(ValueError, match="科室管理员"):
        await memory_store.promote(
            entry_id=l2["id"], user_id="E1001", dept="销售科", roles=["employee"]
        )
    l3 = await memory_store.promote(
        entry_id=l2["id"], user_id="E1001", dept="销售科", roles=["dept_manager"]
    )
    assert l3["layer"] == "L3" and l3["owner"] == "销售科" and l3["source"] == "promoted"
    # 本科室同事可见 L3；跨科室不可见
    assert any(e["id"] == l3["id"] for e in memory_store.list_entries(user_id="E2002", dept="销售科"))
    assert not any(
        e["id"] == l3["id"] for e in memory_store.list_entries(user_id="E3003", dept="生产科")
    )
    # L3 删除需管理员 + 本科室
    with pytest.raises(ValueError, match="无权删除"):
        await memory_store.remove(
            entry_id=l3["id"], user_id="E2002", dept="销售科", roles=["employee"]
        )
    await memory_store.remove(
        entry_id=l3["id"], user_id="E1001", dept="销售科", roles=["dept_manager"]
    )
    # 敏感词二次校验：L3 直接写入敏感内容拒绝
    with pytest.raises(ValueError, match="敏感"):
        await memory_store.add(
            user_id="E1001",
            content="科室工资口径说明",
            kind="faq",
            dept="销售科",
            layer="L3",
            by_roles=["dept_manager"],
        )


async def test_forgetting_decay_and_archive() -> None:
    """遗忘机制（PRD 9.2）：90 天未引用降权（不弃用）；180 天未引用归档（不参与检索）。"""
    await memory_store.set_consent(user_id="E1001", granted=True)
    entry = await memory_store.add(user_id="E1001", content="汇报偏好按周汇总")
    stored = memory_store._entries[entry["id"]]
    # 100 天未引用 → decayed 降权，仍可被 recall 命中
    stored["last_used_at"] = (datetime.now(UTC) - timedelta(days=100)).isoformat(
        timespec="seconds"
    )
    rows = memory_store.list_entries(user_id="E1001", dept="销售科")
    assert rows[0]["decayed"] is True
    hits = await memory_store.recall(
        query="汇报偏好按周汇总", user_id="E1001", dept="销售科"
    )
    assert any(h["id"] == entry["id"] for h in hits)
    # 200 天未引用 → archived，列表与检索均不出现
    stored["last_used_at"] = (datetime.now(UTC) - timedelta(days=200)).isoformat(
        timespec="seconds"
    )
    assert memory_store.list_entries(user_id="E1001", dept="销售科") == []
    assert (
        await memory_store.recall(query="汇报偏好按周汇总", user_id="E1001", dept="销售科")
        == []
    )


async def test_track_auto_consolidation_threshold() -> None:
    """高频行为自动沉淀（PRD 9.3 写入触发②）：连续 3 次达标沉淀，达标归零重计。"""
    # 未同意 PIPL：静默不沉淀
    assert (
        await memory_store.track(user_id="E1001", skill="bi_query", text="常查报表：销售额")
        is None
    )
    await memory_store.set_consent(user_id="E1001", granted=True)
    key_args = {"user_id": "E1001", "skill": "bi_query", "text": "常查报表：销售额"}
    assert await memory_store.track(**key_args) is None  # 第 1 次
    assert await memory_store.track(**key_args) is None  # 第 2 次
    saved = await memory_store.track(**key_args)  # 第 3 次 → 沉淀
    assert saved is not None
    assert saved["source"] == "auto_consolidated" and saved["kind"] == "habit"
    # 达标归零：紧接着第 4 次不重复沉淀（同正文幂等兜底）
    assert await memory_store.track(**key_args) is None
    # 敏感摘要静默放弃，不阻塞主流程
    assert (
        await memory_store.track(user_id="E1001", skill="bi_query", text="常查报表：工资")
        is None
    )


async def test_recall_topk_and_touch() -> None:
    """检索注入（PRD 9.3 注入点1）：Top-K 相似命中 + touch 引用计数。"""
    await memory_store.set_consent(user_id="E1001", granted=True)
    for content in ("汇报偏好按周汇总", "审批偏好先看金额再看事由"):
        await memory_store.add(user_id="E1001", content=content)
    hits = await memory_store.recall(
        query="我的汇报偏好按周汇总就行", user_id="E1001", dept="销售科"
    )
    assert len(hits) <= memory_store.RECALL_TOP_K
    assert hits and "汇报偏好按周汇总" == hits[0]["content"]
    stored = memory_store._entries[hits[0]["id"]]
    assert stored["use_count"] == 1  # 命中即 touch（遗忘机制口径）
    assert hits[0]["score"] >= kb_embedding.THRESHOLD_LOCAL


def test_export_md_sections() -> None:
    """MEMORY.md 导出（ARCHITECTURE 4.9）：按偏好/常用参数/操作习惯/科室经验分节。"""

    async def _seed() -> None:
        await memory_store.set_consent(user_id="E1001", granted=True)
        await memory_store.add(user_id="E1001", content="汇报偏好按周汇总")
        await memory_store.add(
            user_id="E1001", content="默认查华东区数据", kind="params"
        )
        await memory_store.add(
            user_id="E1001",
            content="本科室汇报默认带指标趋势",
            dept="销售科",
            layer="L3",
            by_roles=["dept_manager"],
        )

    asyncio.run(_seed())
    md = memory_store.export_md("E1001", "销售科")
    assert "## 偏好（1）" in md and "汇报偏好按周汇总" in md
    assert "## 常用参数（1）" in md and "默认查华东区数据" in md
    assert "## 科室经验（1）" in md and "本科室汇报默认带指标趋势" in md
    assert "## 操作习惯（0）" in md


# ---- 流水线层：graph e2e ----


async def _invoke(message: str, user_id: str = "E1001") -> dict[str, Any]:
    graph = build_graph().compile()
    return await graph.ainvoke(
        {
            "user_id": user_id,
            "session_id": f"s-mem-{abs(hash(message)) % 10000}",
            "message": message,
            "auth": {"user_id": user_id, "dept": "事业部A/销售科", "roles": ["employee"]},
        }
    )


def _finals(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in state.get("events", []) if e.get("type") == "final"]


async def test_memory_save_turn_confirms() -> None:
    """写入触发①（PRD 9.3）：「记住这个」→ memory_save → 已记住确认 + memory_save 卡。"""
    await memory_store.set_consent(user_id="E1001", granted=True)
    state = await _invoke("记住这个：汇报偏好按周汇总且带指标趋势")
    assert state["skill"]["name"] == "memory_save"
    final = _finals(state)[0]
    assert "已记住：汇报偏好按周汇总且带指标趋势" in final["text"]
    assert final["cards"] and final["cards"][0]["type"] == "memory_save"
    rows = memory_store.list_entries(user_id="E1001", dept="销售科")
    assert len(rows) == 1 and rows[0]["source"] == "ask"


async def test_memory_save_without_consent_fails_gracefully() -> None:
    """未同意 PIPL：写入被拒但对话不阻塞，回复「没能记住」+ 原因。"""
    state = await _invoke("记住这个：汇报偏好按周汇总")
    assert state["skill"]["name"] == "memory_save"
    final = _finals(state)[0]
    assert "没能记住" in final["text"]
    assert "PIPL" in final["text"]
    assert memory_store.list_entries(user_id="E1001", dept="销售科") == []


async def test_intent_stage_memory_injection() -> None:
    """注入点1（PRD 9.3）：意图识别阶段 Top-K 命中以 stage_progress「已参考记忆」提示。"""
    await memory_store.set_consent(user_id="E1001", granted=True)
    await memory_store.add(user_id="E1001", content="汇报偏好按周汇总且带指标趋势")
    out = await intent_node(
        {
            "message": "我的汇报偏好是按周汇总且带指标趋势",
            "user_id": "E1001",
            "auth": {"user_id": "E1001", "dept": "事业部A/销售科", "roles": ["employee"]},
        }
    )
    assert out["memory_hits"]
    progress = [e for e in out["events"] if e.get("message", "").startswith("已参考记忆")]
    assert len(progress) == 1
    assert "汇报偏好按周汇总" in progress[0]["message"]
    # 命中即 touch
    hit_id = out["memory_hits"][0]["id"]
    assert memory_store._entries[hit_id]["use_count"] == 1


# ---- API 层：/memory/* ----


def test_api_memory_requires_auth(client: TestClient) -> None:
    """恒需认证（口径对齐 /audit）：SSO_REQUIRED=true 无 token → 401。"""
    auth_mod.set_sso_required(True)
    try:
        assert client.get("/memory/list").status_code == 401
    finally:
        auth_mod.set_sso_required(False)


def test_api_memory_full_flow(client: TestClient, rsa_key: Any) -> None:
    """API 全流程：未同意 400 → consent → add → list → 敏感词 400 → promote 403 → delete。"""
    tok = make_token(rsa_key)
    headers = auth(tok)
    # 未同意写入 → 400（store ValueError 透传）
    r = client.post("/memory/add", json={"content": "偏好按周汇报"}, headers=headers)
    assert r.status_code == 400 and "PIPL" in r.json()["detail"]
    # 同意 → 写入 → 清单（含统计与同意状态）
    assert client.post("/memory/consent", json={"granted": True}, headers=headers).json()[
        "granted"
    ] is True
    entry = client.post(
        "/memory/add", json={"content": "偏好按周汇报", "kind": "preference"}, headers=headers
    ).json()
    body = client.get("/memory/list", headers=headers).json()
    assert body["count"] == 1 and body["stats"]["personal"] == 1
    assert body["consent"]["granted"] is True
    # 敏感词 → 400
    r = client.post("/memory/add", json={"content": "工资每月 2 万"}, headers=headers)
    assert r.status_code == 400 and "工资" in r.json()["detail"]
    # 普通用户 promote → 403
    assert client.post(f"/memory/{entry['id']}/promote", headers=headers).status_code == 403
    # 删除 → 清零
    assert client.delete(f"/memory/{entry['id']}", headers=headers).json()["deleted"] is True
    assert client.get("/memory/list", headers=headers).json()["count"] == 0


def test_api_memory_promote_manager(client: TestClient, rsa_key: Any) -> None:
    """科室管理员：consent → add → promote → L3（本科室清单可见）。"""
    tok = make_token(rsa_key, roles=["dept_manager"])
    headers = auth(tok)
    client.post("/memory/consent", json={"granted": True}, headers=headers)
    entry = client.post(
        "/memory/add", json={"content": "本科室汇报默认带指标趋势"}, headers=headers
    ).json()
    r = client.post(f"/memory/{entry['id']}/promote", headers=headers)
    assert r.status_code == 200
    promoted = r.json()["entry"]
    assert promoted["layer"] == "L3" and promoted["source"] == "promoted"
    layers = {e["layer"] for e in client.get("/memory/list", headers=headers).json()["items"]}
    assert layers == {"L2", "L3"}  # 原个人条目保留 + 组织条目新增


def test_api_memory_export_md(client: TestClient, rsa_key: Any) -> None:
    """导出端点：Markdown 附件下载，含分节与内容。"""
    tok = make_token(rsa_key)
    headers = auth(tok)
    client.post("/memory/consent", json={"granted": True}, headers=headers)
    client.post("/memory/add", json={"content": "汇报偏好按周汇总"}, headers=headers)
    r = client.get("/memory/export.md", headers=headers)
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    assert "# 我的记忆（E1001）" in r.text
    assert "## 偏好（1）" in r.text and "汇报偏好按周汇总" in r.text


def test_api_memory_consent_revoke_purges(client: TestClient, rsa_key: Any) -> None:
    """撤回同意：清除全部个人记忆并返回条数。"""
    tok = make_token(rsa_key)
    headers = auth(tok)
    client.post("/memory/consent", json={"granted": True}, headers=headers)
    client.post("/memory/add", json={"content": "偏好按周汇报"}, headers=headers)
    client.post("/memory/add", json={"content": "默认查华东区数据", "kind": "params"}, headers=headers)
    res = client.post("/memory/consent", json={"granted": False}, headers=headers).json()
    assert res["purged"] == 2
    assert client.get("/memory/list", headers=headers).json()["count"] == 0
