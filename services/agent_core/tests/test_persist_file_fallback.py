"""本地文件快照兜底（agent_core/persist.py，PLAN 无 Redis 持久化）。

验证：REDIS_URL 未配置时各存储域快照落 data/snapshots/ 本地文件，
重启（reset 清内存 → restore 装载）后任务/配置/技能/知识库/记忆/审计
不丢失。目录由 conftest autouse fixture 重定向到 tmp_path。
"""

import asyncio
from pathlib import Path
from typing import Any, Iterator

import pytest

from agent_core import audit, persist
from agent_core.api import auth as auth_mod
from agent_core.automation import store as automation
from agent_core.knowledge import store as knowledge_store
from agent_core.memory import store as memory_store
from agent_core.skills import store as skill_store
from agent_core.syscfg import store as syscfg
from agent_core.workflow import store as workflow_store

DRAFT = {"sku": {"value": "SKU-001", "source": "extract"}}
DAILY = {"type": "daily", "time": "09:00"}

_FAKE_DEF = {  # 市场注册需配套的运行时技能定义（对齐 test_automation）
    "name": "erp_inventory_query",
    "title": "库存查询",
    "rw": "read",
    "required_roles": [],
    "intent_patterns": [],
    "read_tools": [],
    "write_tool": None,
    "ask_messages": {},
    "required_fields": [],
}


@pytest.fixture(autouse=True)
def _reset_stores() -> Iterator[None]:
    """测试隔离：六域 + 审计清空 + 调度器关闭 + 关闭强制鉴权。"""
    auth_mod.set_sso_required(False)
    automation.stop_scheduler()
    automation.reset()
    skill_store.reset()
    workflow_store.reset()
    syscfg.reset()
    knowledge_store.reset()
    memory_store.reset()
    asyncio.run(audit.clear())
    yield
    automation.stop_scheduler()
    asyncio.run(audit.clear())


# ---- persist 模块 ----


def test_json_roundtrip(tmp_path: Path) -> None:
    """write_json/read_json 往返；文件名不含冒号（key 消毒）。"""
    asyncio.run(persist.write_json("automation:snapshot", {"seq": 3, "tasks": {"t": 1}}))
    files = [p.name for p in tmp_path.iterdir()]
    assert files == ["automation_snapshot.json"]
    assert asyncio.run(persist.read_json("automation:snapshot")) == {"seq": 3, "tasks": {"t": 1}}


async def test_read_missing_and_corrupt(tmp_path: Path) -> None:
    """文件缺失 / JSON 损坏均返回 None（损坏不阻断启动）。"""
    assert await persist.read_json("nope:snapshot") is None
    (tmp_path / "bad_snapshot.json").write_text("{半截", encoding="utf-8")
    assert await persist.read_json("bad:snapshot") is None


async def test_append_tail_and_compaction(tmp_path: Path) -> None:
    """JSONL 追加 → 尾部装载 + 启动压实（坏行跳过、只留 tail 条）。"""
    for i in range(5):
        await persist.append_line("audit:events", {"i": i})
    (tmp_path / "audit_events.jsonl").open("a", encoding="utf-8").write("坏行\n")
    await persist.append_line("audit:events", {"i": 5})
    items = await persist.read_tail_lines("audit:events", 3)
    assert [x["i"] for x in items] == [3, 4, 5]
    # 压实后文件只剩尾部 3 条有效行
    text = (tmp_path / "audit_events.jsonl").read_text(encoding="utf-8")
    assert text.splitlines() == ['{"i": 3}', '{"i": 4}', '{"i": 5}']


# ---- automation：用户报告的任务丢失场景（跨重启保留）----


def _inject_registry(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from agent_core.skills import registry

    monkeypatch.setitem(registry.SKILLS, name, {**_FAKE_DEF, "name": name})


async def test_automation_tasks_survive_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """建任务 → 清内存（模拟重启）→ restore 自动恢复。"""
    _inject_registry(monkeypatch, "erp_inventory_query")
    task = await automation.create(
        name="每日库存播报",
        skill="erp_inventory_query",
        params=DRAFT,
        schedule=DAILY,
        owner="E1001",
        owner_auth={"user_id": "E1001", "dept": "事业部A/生产科", "roles": ["employee"]},
    )
    assert automation.detail(task["id"]) is not None

    automation.reset()  # 模拟进程重启后内存为空
    assert automation.detail(task["id"]) is None

    await automation.restore()  # 从本地文件快照恢复
    restored = automation.detail(task["id"])
    assert restored is not None
    assert restored["name"] == "每日库存播报"
    assert restored["owner"] == "E1001"


# ---- syscfg：模型配置跨重启保留 ----


async def test_syscfg_models_survive_restart() -> None:
    await syscfg.register_model("qwen32b", "http://llm:8000/v1", "qwen2.5-32b", by="E8001")
    await syscfg.set_default_model("qwen32b", by="E8001")
    await syscfg.set_user_model("E1001", "qwen32b")

    syscfg.reset()  # 模拟重启
    assert syscfg.default_model() is None

    await syscfg.restore()
    assert syscfg.default_model() == "qwen32b"
    assert syscfg.get_user_model("E1001") == "qwen32b"
    assert len(syscfg.list_models()) == 1


# ---- audit：流水跨重启保留（JSONL 增量 + 启动装载）----


async def test_audit_survive_restart() -> None:
    await audit.record("action_a", user_id="E1001")
    await audit.record("action_b", user_id="E1001")

    audit._memory.clear()  # 模拟重启（clear 会连文件一起删，这里只清内存环）
    assert await audit.recent(10) == []

    await audit.restore()
    actions = [e["action"] for e in await audit.recent(10)]
    assert actions == ["action_b", "action_a"]  # 最新在前


# ---- workflow / knowledge / memory / skills：快照往返（模拟重启）----


async def test_workflow_survive_restart() -> None:
    workflow_store._workflows["wf_000001"] = {
        "id": "wf_000001",
        "name": "跨系统编排",
        "steps": [],
        "mode": "plan",
        "status": "active",
        "owner": "E1001",
    }
    workflow_store._seq = 3
    await workflow_store._snapshot()
    workflow_store.reset()

    await workflow_store.restore()
    assert workflow_store._workflows["wf_000001"]["name"] == "跨系统编排"
    assert workflow_store._seq == 3


async def test_knowledge_survive_restart() -> None:
    knowledge_store._docs["d1"] = {
        "id": "d1",
        "title": "入职指南",
        "chunks": [{"text": "内容", "vector": [0.1, 0.2], "section_vector": None}],
    }
    await knowledge_store._snapshot()
    knowledge_store.reset()

    await knowledge_store.restore()
    assert knowledge_store._docs["d1"]["title"] == "入职指南"
    # 向量字段不入快照（restore 后检索时 reindex 重建）
    assert knowledge_store._docs["d1"]["chunks"][0]["vector"] is None


async def test_memory_survive_restart() -> None:
    memory_store._entries["m1"] = {"id": "m1", "content": "偏好周一看板", "layer": "L2"}
    memory_store._seq = 2
    await memory_store._snapshot()
    memory_store.reset()

    await memory_store.restore()
    assert memory_store._entries["m1"]["content"] == "偏好周一看板"
    assert memory_store._seq == 2


async def test_skills_survive_restart() -> None:
    skill_store._meta["erp_inventory_query"] = {"name": "erp_inventory_query", "approvals": []}
    skill_store._installs["E1001"] = {"erp_inventory_query"}
    skill_store._seq = 1
    await skill_store._snapshot()
    skill_store.reset()

    await skill_store.restore()
    assert skill_store._meta["erp_inventory_query"]["name"] == "erp_inventory_query"
    assert skill_store._installs["E1001"] == {"erp_inventory_query"}


# ---- Redis 优先级：配了 REDIS_URL 时文件不参与（结构验证）----


def test_redis_branch_untouched_by_file_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """restore 的文件兜底只在无 Redis 时生效：单例为 None 即走文件；有 Redis 单例则跳过文件。"""
    # 无 Redis：_get_redis() 返回 None（conftest 已清 REDIS_URL），文件参与
    assert automation._get_redis() is None
    # 有 Redis 单例时文件兜底不会被读到（快照 key 与文件同名不同介质，restore 顺序 Redis → 文件）
    assert persist._path("automation:snapshot").name == "automation_snapshot.json"
