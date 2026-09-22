"""灰度门禁与观测测试（PLAN P1.4，PRD 5.5.6 / 16.4）。

覆盖：稳定分桶（确定性/放量口径）、版本协商（解析/比较/缺头阻断）、
/chat 门禁 403 code、chat_turn 埋点、/metrics/gray 权限与聚合、
确认卡 modified 标记。
"""

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

# 复用 test_api_auth 的 token 签发助手；rsa_key fixture 由 conftest 注入
from test_api_auth import make_token

from agent_core import audit
from agent_core.api import auth as auth_mod
from agent_core.api import gray as gray_mod
from agent_core.api.main import app
from agent_core.guardrail import confirm_store


@pytest.fixture(autouse=True)
def _reset_gray_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """灰度环境隔离：默认门禁全关（放量 0 / 无最低版本要求）。"""
    monkeypatch.delenv("GRAY_PERCENT", raising=False)
    monkeypatch.delenv("MIN_CLIENT_VERSION", raising=False)
    auth_mod.set_sso_required(False)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_audit() -> Any:
    asyncio.run(audit.clear())
    yield
    asyncio.run(audit.clear())


def _user_in_bucket(pct: int, inside: bool) -> str:
    """找到命中/未命中放量桶的工号（分桶确定性 → 可构造正反例）。"""
    i = 0
    while True:
        user = f"GU{i}"
        if (gray_mod.bucket_of(user) < pct) == inside:
            return user
        i += 1


# ---- 分桶与版本协商单元 ----


def test_bucket_of_is_stable() -> None:
    """同一用户恒定命中同一桶（放量期间不抖动）。"""
    assert gray_mod.bucket_of("E1001") == gray_mod.bucket_of("E1001")
    assert 0 <= gray_mod.bucket_of("E1001") < 100


def test_gray_percent_clamped() -> None:
    import os

    os.environ["GRAY_PERCENT"] = "150"
    assert gray_mod.gray_percent() == 100
    os.environ["GRAY_PERCENT"] = "-5"
    assert gray_mod.gray_percent() == 0
    os.environ["GRAY_PERCENT"] = "abc"
    assert gray_mod.gray_percent() == 0
    del os.environ["GRAY_PERCENT"]


def test_is_gray_user_percent_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """0/未设=门禁关闭全放行；100=全放行；N=仅前 N 桶放行。"""
    assert gray_mod.is_gray_user("anyone") is True  # 未设
    monkeypatch.setenv("GRAY_PERCENT", "0")
    assert gray_mod.is_gray_user("anyone") is True
    monkeypatch.setenv("GRAY_PERCENT", "100")
    assert gray_mod.is_gray_user("anyone") is True
    monkeypatch.setenv("GRAY_PERCENT", "10")
    assert gray_mod.is_gray_user(_user_in_bucket(10, inside=True)) is True
    assert gray_mod.is_gray_user(_user_in_bucket(10, inside=False)) is False


def test_version_compare() -> None:
    assert gray_mod.version_lt("0.1.9", "0.2.0")
    assert not gray_mod.version_lt("0.2.0", "0.2.0")
    assert gray_mod.version_lt("0.2", "0.2.1")  # 短版本补零
    assert not gray_mod.version_lt("0.10.0", "0.9.9")  # 数值而非字典序
    assert gray_mod.version_lt("v0.1.0", "0.2.0")  # 前缀剥离
    assert gray_mod.version_tuple("0.2.1-beta.3") == (0, 2, 1, 3)


def test_client_version_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert gray_mod.client_version_allowed(None) is True  # 未启用协商
    monkeypatch.setenv("MIN_CLIENT_VERSION", "0.2.0")
    assert gray_mod.client_version_allowed(None) is False  # 缺头=强制升级口径
    assert gray_mod.client_version_allowed("") is False
    assert gray_mod.client_version_allowed("0.1.9") is False
    assert gray_mod.client_version_allowed("0.2.0") is True
    assert gray_mod.client_version_allowed("0.3.0") is True


# ---- /chat 门禁集成 ----


def test_chat_gate_closed_by_default(client: TestClient) -> None:
    """默认（未设 GRAY_PERCENT）：门禁关闭，行为与 P1.3 兼容。"""
    resp = client.post(
        "/chat", json={"session_id": "s1", "user_id": "E1001", "message": "你好"}
    )
    assert resp.status_code == 200


def test_chat_gray_percent_denies_out_of_bucket(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未命中放量桶 → 403 gray_percent_exceeded（稳定拒绝）。"""
    monkeypatch.setenv("GRAY_PERCENT", "10")
    outsider = _user_in_bucket(10, inside=False)
    resp = client.post(
        "/chat", json={"session_id": "s1", "user_id": outsider, "message": "你好"}
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "gray_percent_exceeded"
    # 拒绝留痕（灰度观测门禁拒绝数）
    entries = asyncio.run(audit.recent(action="gray_denied"))
    assert len(entries) == 1
    assert entries[0]["user_id"] == outsider
    assert entries[0]["result"] == "denied"


def test_chat_gray_percent_admits_in_bucket(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命中放量桶 → 正常受理。"""
    monkeypatch.setenv("GRAY_PERCENT", "10")
    insider = _user_in_bucket(10, inside=True)
    resp = client.post(
        "/chat", json={"session_id": "s1", "user_id": insider, "message": "你好"}
    )
    assert resp.status_code == 200


def test_chat_version_too_low_blocked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """低于 MIN_CLIENT_VERSION → 403 client_version_too_low（PRD 5.5.6 强制升级）。"""
    monkeypatch.setenv("MIN_CLIENT_VERSION", "0.2.0")
    resp = client.post(
        "/chat",
        json={"session_id": "s1", "user_id": "E1001", "message": "你好"},
        headers={"X-Client-Version": "0.1.9"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "client_version_too_low"


def test_chat_version_negotiation_passes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """带达标版本头 → 放行；强制升级场景缺头 → 阻断。"""
    monkeypatch.setenv("MIN_CLIENT_VERSION", "0.2.0")
    resp = client.post(
        "/chat",
        json={"session_id": "s1", "user_id": "E1001", "message": "你好"},
        headers={"X-Client-Version": "0.2.1"},
    )
    assert resp.status_code == 200
    resp = client.post(
        "/chat", json={"session_id": "s1", "user_id": "E1001", "message": "你好"}
    )
    assert resp.status_code == 403


def test_chat_records_chat_turn(client: TestClient) -> None:
    """对话埋点：chat_turn 含 user/session/耗时（PRD 16.4 对话总数/活跃用户）。"""
    client.post("/chat", json={"session_id": "s1", "user_id": "E1001", "message": "你好"})
    entries = asyncio.run(audit.recent(action="chat_turn"))
    assert len(entries) == 1
    assert entries[0]["user_id"] == "E1001"
    assert entries[0]["session_id"] == "s1"
    assert entries[0]["duration_ms"] is not None


# ---- 确认卡 modified 标记（PRD 16.4：修改字段占比）----


def _issue_confirm_token(user_id: str, session_id: str) -> str:
    token = uuid.uuid4().hex
    snap = {
        "state": {
            "user_id": user_id,
            "session_id": session_id,
            "tool_call": {
                "name": "oa__approve",
                "arguments": {"approval_id": "AP-2026-0001", "action": "approve"},
            },
        }
    }
    asyncio.run(confirm_store.set_token(token, snap))
    return token


def test_confirm_modified_flag_audited(
    client: TestClient, rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户修改字段后确认 → confirm_approved.detail 含 modified=true。"""

    async def _mock(name: str, arguments: dict[str, Any]) -> Any:
        return {"approval_id": arguments["approval_id"], "status": "approved"}

    monkeypatch.setattr("agent_core.pipeline.graph.call_oa_tool", _mock)
    token = _issue_confirm_token("E1001", "s1")
    headers = {"Authorization": f"Bearer {make_token(rsa_key)}"}
    resp = client.post(
        f"/confirmations/{token}",
        json={"action": "confirm", "modified": True},
        headers=headers,
    )
    assert resp.status_code == 200
    entries = asyncio.run(audit.recent(action="confirm_approved"))
    assert len(entries) == 1
    assert "modified=true" in entries[0]["detail"]


# ---- /metrics/gray（PLAN P1.4，PRD 16.4）----


def _seed(**kwargs: Any) -> None:
    asyncio.run(audit.record(**kwargs))


def test_metrics_gray_requires_auth(client: TestClient) -> None:
    resp = client.get("/metrics/gray")
    assert resp.status_code == 401


def test_metrics_gray_manager_only(client: TestClient, rsa_key: Any) -> None:
    """非 dept_manager → 403（观测口径对齐管理端审计）。"""
    headers = {"Authorization": f"Bearer {make_token(rsa_key)}"}
    resp = client.get("/metrics/gray", headers=headers)
    assert resp.status_code == 403


def test_metrics_gray_aggregates(client: TestClient, rsa_key: Any) -> None:
    """dept_manager → 五类指标聚合：对话/活跃用户/修改占比/写入失败/门禁拒绝。"""
    _seed(action="chat_turn", user_id="E1001", session_id="s1", duration_ms=100)
    _seed(action="chat_turn", user_id="E1001", session_id="s2", duration_ms=100)
    _seed(action="chat_turn", user_id="E1002", session_id="s3", duration_ms=100)
    _seed(action="confirm_approved", user_id="E1001", detail="modified=true")
    _seed(action="confirm_approved", user_id="E1002", detail="")
    _seed(
        action="tool_call",
        tool="oa__create_leave",
        user_id="E1001",
        result="error",
        detail="OA 接口超时",
    )
    _seed(
        action="tool_call",
        tool="oa__create_leave",
        user_id="E1002",
        result="error",
        detail="OA 接口超时",
    )
    _seed(action="tool_call", tool="bi__query", user_id="E1001")  # 成功不计失败
    _seed(action="gray_denied", user_id="E2001", result="denied")
    _seed(action="client_version_denied", user_id="E2002", result="denied")

    headers = {
        "Authorization": f"Bearer {make_token(rsa_key, sub='E9000', roles=['dept_manager'], jti='jti-gm')}"
    }
    resp = client.get("/metrics/gray", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_chats"] == 3
    assert body["active_users"] == 2
    assert body["confirm"]["approved"] == 2
    assert body["confirm"]["modified"] == 1
    assert body["confirm"]["modified_ratio"] == 0.5
    assert body["write_failures"]["total"] == 2
    assert body["write_failures"]["by_tool"] == {"oa__create_leave": 2}
    assert body["write_failures"]["by_reason"] == {"OA 接口超时": 2}
    assert body["gate_denials"]["gray_percent_exceeded"] == 1
    assert body["gate_denials"]["client_version_too_low"] == 1
    assert body["session_abandons"] is None  # 待桌面端埋点
