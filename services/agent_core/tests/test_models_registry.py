"""LLM 模型注册表测试（管理后台可视化配置 + 员工自选）。

单元层：模型 CRUD 校验 / api_key 打码 / 生效优先级（员工自选 > 默认 > env）/
删除级联 / 探活（httpx 打桩）/ 员工偏好切换落审计。
图层：注册表模型驱动 LLM 兜底路由（不依赖 env LLM_BASE_URL）。
Web 层：admin 表单门禁（未登录 302 / 非系统管理员 403 / CSRF）+ 全流程
（注册→设默认→启停→探活→删除→审计）；REST /models 与 /me/model。
"""

import asyncio
import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from agent_core import audit, automation, syscfg
from agent_core.api import admin as admin_mod
from agent_core.api import auth as auth_mod
from agent_core.api.auth import AuthContext
from agent_core.api.main import app
from agent_core.automation import store as auto_store
from agent_core.pipeline import llm

ISSUER = auth_mod.SSO_ISSUER
AUDIENCE = auth_mod.SSO_AUDIENCE

_M1 = {"name": "qwen32b", "base_url": "http://llm-a.test/v1", "model": "qwen2.5-32b-instruct", "api_key": "sk-aaa111"}
_M2 = {"name": "deepseek", "base_url": "http://llm-b.test/v1", "model": "deepseek-v3", "api_key": ""}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """测试隔离：syscfg 四表重建 + 审计清空 + 调度器关闭 + env LLM 关闭。"""
    auth_mod.set_sso_required(False)
    admin_mod._sessions.clear()
    admin_mod._flows.clear()
    syscfg.reset()
    auto_store.stop_scheduler()
    automation.reset()
    asyncio.run(audit.clear())
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    yield
    auto_store.stop_scheduler()
    syscfg.reset()
    asyncio.run(audit.clear())


# ---- 单元层：注册表 CRUD 与生效优先级 ----


async def test_register_validation() -> None:
    """名称/base_url/model 非法 → ValueError，不落库。"""
    for kwargs in (
        {"name": "BadName", "base_url": "http://x.test", "model": "m"},
        {"name": "ok", "base_url": "ftp://x.test", "model": "m"},
        {"name": "ok", "base_url": "http://x.test", "model": "  "},
    ):
        with pytest.raises(ValueError):
            await syscfg.register_model(**kwargs, by="admin")  # type: ignore[arg-type]
    assert syscfg.list_models() == []


async def test_register_masks_key_and_resolve_raw() -> None:
    """陈列打码（只露末 4 位），resolve 解析明文供 pipeline 使用。"""
    await syscfg.register_model(**_M1, by="admin")
    await syscfg.set_default_model("qwen32b", by="admin")
    view = syscfg.list_models()[0]
    assert view["api_key_masked"] == "已配置（****a111）"
    assert "api_key" not in view
    raw = syscfg.resolve_model("E1")
    assert raw is not None
    assert raw["api_key"] == "sk-aaa111"
    assert raw["model"] == "qwen2.5-32b-instruct"
    # 免密模型陈列
    await syscfg.register_model(**_M2, by="admin")
    view2 = {m["name"]: m for m in syscfg.list_models()}["deepseek"]
    assert view2["api_key_masked"] == "未配置（免密）"


async def test_precedence_user_over_default() -> None:
    """生效优先级：员工自选 > 默认模型 > env（返回 None）。"""
    assert syscfg.resolve_model("E1") is None  # 未注册任何模型 → env 兜底
    await syscfg.register_model(**_M1, by="admin")
    await syscfg.register_model(**_M2, by="admin")
    await syscfg.set_default_model("qwen32b", by="admin")
    assert syscfg.resolve_model("E1")["name"] == "qwen32b"  # type: ignore[index]
    await syscfg.set_user_model("E1", "deepseek", by="E1")
    assert syscfg.resolve_model("E1")["name"] == "deepseek"  # type: ignore[index]
    assert syscfg.resolve_model("E2")["name"] == "qwen32b"  # type: ignore[index]  # 未自选走默认
    # 取消自选 → 回落默认
    await syscfg.set_user_model("E1", None, by="E1")
    assert syscfg.resolve_model("E1")["name"] == "qwen32b"  # type: ignore[index]
    # 幂等：重复设同值不动作
    assert (await syscfg.set_user_model("E1", None, by="E1"))["changed"] is False


async def test_disabled_skipped() -> None:
    """停用模型跳过：自选停用 → 回默认；默认停用 → env 兜底。"""
    await syscfg.register_model(**_M1, by="admin")
    await syscfg.register_model(**_M2, by="admin")
    await syscfg.set_default_model("qwen32b", by="admin")
    await syscfg.set_user_model("E1", "deepseek", by="E1")
    await syscfg.set_model_enabled("deepseek", False, by="admin")
    assert syscfg.resolve_model("E1")["name"] == "qwen32b"  # type: ignore[index]
    await syscfg.set_model_enabled("qwen32b", False, by="admin")
    assert syscfg.resolve_model("E1") is None
    # 停用模型不可设默认、不可被选择
    with pytest.raises(ValueError):
        await syscfg.set_default_model("qwen32b", by="admin")
    with pytest.raises(ValueError):
        await syscfg.set_user_model("E2", "deepseek", by="E2")
    # 员工清单只含启用项
    await syscfg.set_model_enabled("qwen32b", True, by="admin")
    assert [m["name"] for m in syscfg.list_models(enabled_only=True)] == ["qwen32b"]


async def test_delete_cascades() -> None:
    """删除模型：默认指针与员工偏好级联清理。"""
    await syscfg.register_model(**_M1, by="admin")
    await syscfg.set_default_model("qwen32b", by="admin")
    await syscfg.set_user_model("E1", "qwen32b", by="E1")
    result = await syscfg.delete_model("qwen32b", by="admin")
    assert result["user_prefs_dropped"] == 1
    assert syscfg.list_models() == []
    assert syscfg.default_model() is None
    assert syscfg.get_user_model("E1") is None
    with pytest.raises(ValueError):
        await syscfg.delete_model("qwen32b", by="admin")


async def test_probe_models_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """连通性探测：GET {base_url}/models，带 Bearer（免密不带头），错误降级。"""
    await syscfg.register_model(**_M1, by="admin")
    await syscfg.register_model(**_M2, by="admin")
    calls: list[dict[str, Any]] = []

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, timeout: Any = None, headers: Any = None) -> Any:
            calls.append({"url": url, "headers": headers})
            return _FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    report = await syscfg.probe_models()
    assert [r["healthy"] for r in report["results"]] == [True, True]
    by_name = {c["url"]: c for c in calls}
    assert by_name["http://llm-a.test/v1/models"]["headers"] == {"Authorization": "Bearer sk-aaa111"}
    assert "headers" not in by_name["http://llm-b.test/v1/models"] or not by_name["http://llm-b.test/v1/models"]["headers"]
    with pytest.raises(ValueError):
        await syscfg.probe_models("nope")


async def test_model_actions_audited() -> None:
    """注册/默认/启停/删除/员工切换均落审计（审计者也被审计）。"""
    await syscfg.register_model(**_M1, by="admin")
    await syscfg.set_default_model("qwen32b", by="admin")
    await syscfg.set_model_enabled("qwen32b", False, by="admin")
    await syscfg.delete_model("qwen32b", by="admin")
    await syscfg.set_user_model("E1", None, by="E1")  # 无偏好时幂等不落审计
    await syscfg.register_model(**_M1, by="admin")
    await syscfg.set_user_model("E1", "qwen32b", by="E1")
    actions = [item["action"] for item in await audit.recent(50)]
    assert actions == [
        "syscfg_user_model",
        "syscfg_model_register",
        "syscfg_model_delete",
        "syscfg_model_toggle",
        "syscfg_model_default",
        "syscfg_model_register",
    ]


# ---- 图层：注册表模型驱动 LLM 兜底 ----


def _tool_payload(skill: str, fields: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 兼容 tool_calls 响应体。"""
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "extract_slots",
                                "arguments": json.dumps({"skill": skill, "fields": fields}, ensure_ascii=False),
                            }
                        }
                    ]
                }
            }
        ]
    }


def _mock_http(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """替身 httpx 客户端：记录请求并返回固定载荷。"""
    calls: list[dict[str, Any]] = []

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return payload

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _Resp:
            calls.append({"url": url, **kwargs})
            return _Resp()

    monkeypatch.setattr(llm.httpx, "AsyncClient", _Client)
    return calls


async def test_llm_enabled_and_extract_via_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """注册表有模型（env 未配置）→ enabled 为真；请求命中注册的 url/model/key。"""
    await syscfg.register_model(**_M1, by="admin")
    await syscfg.set_default_model("qwen32b", by="admin")
    assert llm.enabled("E1") is True
    calls = _mock_http(
        monkeypatch,
        _tool_payload("chat", {"content": "好的"}),
    )
    result = await llm.extract_slots("随便聊聊", "E1")
    assert result is not None
    assert len(calls) == 1
    assert calls[0]["url"] == "http://llm-a.test/v1/chat/completions"
    assert calls[0]["json"]["model"] == "qwen2.5-32b-instruct"
    assert calls[0]["headers"] == {"Authorization": "Bearer sk-aaa111"}


async def test_graph_uses_registry_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """员工自选模型驱动图内 LLM 兜底路由（默认模型未设时自选生效）。"""
    from agent_core.pipeline.graph import build_graph
    from agent_core.skills.registry import match_skill

    message = "周五下班之前叫我记得给老王回电话"
    assert match_skill(message) is None  # 前置：规则层必不命中
    await syscfg.register_model(**_M1, by="admin")
    await syscfg.set_default_model("qwen32b", by="admin")
    at = f"{(datetime.now().astimezone() + timedelta(days=2)).date().isoformat()}T17:00:00"
    calls = _mock_http(
        monkeypatch,
        _tool_payload("automation_task_create", {"remind_at": at, "content": "给老王回电话"}),
    )
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {"user_id": "E2001", "session_id": "s-registry-route", "message": message}
    )
    assert "已创建定时提醒" in result["final"]["text"]
    assert calls[0]["url"] == "http://llm-a.test/v1/chat/completions"


# ---- Web 层：admin 表单 + REST ----


def make_token(rsa_key: Any, **overrides: Any) -> str:
    """按 PRD 8.5.4 claims 结构签发测试 token（与 test_admin_web 同口径）。"""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": "E1001",
        "idp": "ad",
        "idp_sub": "zhangsan@corp.com",
        "dept": "信息科",
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


def ctx_of(user: str, roles: list[str]) -> AuthContext:
    """直构造管理身份（不走 JWKS）。"""
    return AuthContext(
        user_id=user,
        idp="ad",
        idp_sub=f"{user}@corp.com",
        dept="信息科",
        roles=list(roles),
        perm_ver=17,
        jti=uuid4().hex,
        session_id=f"web-{user}",
    )


def login_as(client: TestClient, user: str, roles: list[str]) -> dict[str, Any]:
    """建会话并注入 cookie。"""
    sess = admin_mod.create_session(ctx_of(user, roles))
    client.cookies.set(admin_mod.SESSION_COOKIE, sess["sid"])
    return sess


def post(client: TestClient, url: str, data: dict[str, str], **kw: Any) -> Any:
    kw.setdefault("follow_redirects", False)
    return client.post(url, data=data, **kw)


def test_admin_model_routes_gate(client: TestClient) -> None:
    """未登录 302 / 非系统管理员 403 / CSRF 伪造被拒。"""
    r = post(client, "/admin/syscfg/model", {"name": "x", "base_url": "http://x", "model": "m"})
    assert r.status_code == 302
    assert r.headers["location"].startswith("/admin/login")
    s = login_as(client, "aud1", ["auditor"])
    r = post(client, "/admin/syscfg/model", {"name": "x", "base_url": "http://x", "model": "m", "_csrf": s["csrf"]})
    assert r.status_code == 403
    s = login_as(client, "sa1", ["system_admin"])
    r = post(client, "/admin/syscfg/model", {"name": "x", "base_url": "http://x", "model": "m", "_csrf": "forged"})
    assert r.status_code == 302
    assert "CSRF" in unquote_err(r.headers["location"])
    assert syscfg.list_models() == []


def unquote_err(location: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(location).query).get("err", [""])[0]


def test_admin_model_full_flow(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """网页全流程：注册→陈列打码→设默认→停用→探活→删除（含审计）。"""
    s = login_as(client, "sa1", ["system_admin"])
    # 注册
    r = post(
        client,
        "/admin/syscfg/model",
        {**_M1, "_csrf": s["csrf"]},
    )
    assert "已保存" in unquote_err_loc(r)
    # 页面陈列（api_key 打码、不泄漏明文）
    r = client.get("/admin/syscfg")
    assert r.status_code == 200
    assert "LLM 模型管理" in r.text
    assert "qwen32b" in r.text
    assert "sk-aaa111" not in r.text
    assert "****a111" in r.text
    # 校验错误回显
    r = post(client, "/admin/syscfg/model", {"name": "Bad", "base_url": "http://x", "model": "m", "_csrf": s["csrf"]})
    assert "模型名" in unquote_err_loc(r)
    # 设默认
    r = post(client, "/admin/syscfg/model/default", {"name": "qwen32b", "_csrf": s["csrf"]})
    assert "默认模型" in unquote_err_loc(r)
    assert syscfg.default_model() == "qwen32b"
    # 停用 / 启用
    r = post(client, "/admin/syscfg/model/toggle", {"name": "qwen32b", "value": "false", "_csrf": s["csrf"]})
    assert "已停用" in unquote_err_loc(r)
    assert syscfg.resolve_model("E1") is None
    r = post(client, "/admin/syscfg/model/toggle", {"name": "qwen32b", "value": "true", "_csrf": s["csrf"]})
    assert "已启用" in unquote_err_loc(r)
    # 探活（打桩）
    class _FakeResp:
        status_code = 200

    class _FakeClient:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, timeout: Any = None, headers: Any = None) -> Any:
            return _FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    r = post(client, "/admin/syscfg/model/probe", {"_csrf": s["csrf"]})
    assert "已探测" in unquote_err_loc(r)
    assert "mprobed=" in r.headers["location"]
    # 删除
    r = post(client, "/admin/syscfg/model/delete", {"name": "qwen32b", "_csrf": s["csrf"]})
    assert "已删除" in unquote_err_loc(r)
    assert syscfg.list_models() == []
    actions = [item["action"] for item in asyncio.run(audit.recent(50))]
    assert actions.count("syscfg_model_register") == 1
    assert actions.count("syscfg_model_default") == 1
    assert actions.count("syscfg_model_toggle") == 2
    assert actions.count("syscfg_model_delete") == 1


def unquote_err_loc(resp: Any) -> str:
    from urllib.parse import unquote, urlparse

    qs = urlparse(resp.headers["location"]).query
    from urllib.parse import parse_qs

    parts = parse_qs(qs)
    return unquote(parts.get("msg", parts.get("err", [""]))[0])


def test_rest_models_and_me_model(client: TestClient, rsa_key: Any) -> None:
    """REST：/models 陈列启用清单 + 我的当前；/me/model 切换与恢复。"""
    # 未认证：GET 可看清单（SSO 关闭的冒烟口径），POST 拒绝
    r = client.get("/models")
    assert r.status_code == 200
    assert r.json() == {"items": [], "current": None, "default": None}
    r = client.post("/me/model", json={"name": "x"})
    assert r.status_code == 401
    # 认证员工：切换自选模型
    asyncio.run(syscfg.register_model(**_M1, by="admin"))
    asyncio.run(syscfg.register_model(**_M2, by="admin"))
    token = make_token(rsa_key, sub="E2001")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/models", headers=headers)
    body = r.json()
    assert [m["name"] for m in body["items"]] == ["deepseek", "qwen32b"]
    assert body["current"] is None
    r = client.post("/me/model", json={"name": "deepseek"}, headers=headers)
    assert r.json() == {"current": "deepseek", "changed": True}
    assert syscfg.get_user_model("E2001") == "deepseek"
    # 未知模型 400
    r = client.post("/me/model", json={"name": "nope"}, headers=headers)
    assert r.status_code == 400
    # 恢复跟随默认
    r = client.post("/me/model", json={"name": None}, headers=headers)
    assert r.json() == {"current": None, "changed": True}
    assert r.status_code == 200
    assert syscfg.get_user_model("E2001") is None
