"""泛微 E9 适配器单测（httpx MockTransport，零真实依赖）。

认证链路按 xinao-sdk OaClient.java 口径：
regist（cpk 换 spk/secrit）→ applytoken（secrit 用 spk RSA 加密）→
业务请求（form 体 + userid 头 RSA 加密）。
桩持有 spk 对应的私钥，可解密验证各加密头内容（防算法/口径漂移）。

覆盖：
- 完整认证链：regist → applytoken（secret 头可解密还原 secrit）→ doCreateRequest
- doCreateRequest form 体构造（headers / workflowId / mainData 字段映射 / otherParams）
- SUCCESS 返回映射为 doc_no=E9-{requestid}；失败码抛错带 errMsg
- token 缓存：多次提交仅一次 regist + applytoken；空 token 重新 regist 兜底
- 读路径 NotImplementedError；OA_MODE=e9 适配器选择
"""

import asyncio
import base64
import json
import urllib.parse
from datetime import datetime
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from adapters import oa_client
from adapters.e9_client import E9OaAdapter

_START = datetime(2026, 10, 12, 9, 0)
_END = datetime(2026, 10, 13, 18, 0)

# 服务端密钥对：公钥（SPKI DER Base64）由桩的 regist 返回，私钥用于解密验证加密头
_SERVER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_SERVER_SPK = base64.b64encode(
    _SERVER_KEY.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
).decode("ascii")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _rsa_decrypt(value: str) -> str:
    """用服务端私钥解密（PKCS1v15），还原适配器加密前的明文。"""
    return _SERVER_KEY.decrypt(base64.b64decode(value), padding.PKCS1v15()).decode("utf-8")


class _E9Stub:
    """E9 REST 桩：记录全部请求（headers 小写归一 + form 体解析），按脚本返回。"""

    def __init__(
        self,
        create_response: dict[str, Any] | None = None,
        token_responses: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.create_response = create_response or {
            "code": "SUCCESS",
            "data": {"requestid": 8888},
            "errMsg": {},
        }
        self.token_responses = token_responses or [{"token": "T-XYZ"}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        form: dict[str, Any] = {
            k: v[0]
            for k, v in urllib.parse.parse_qs(request.content.decode("utf-8")).items()
        }
        self.calls.append(
            {"path": request.url.path, "headers": headers, "form": form}
        )
        if request.url.path == "/api/ec/dev/auth/regist":
            return httpx.Response(200, json={"spk": _SERVER_SPK, "secrit": "SEC-001"})
        if request.url.path == "/api/ec/dev/auth/applytoken":
            scripted = self.token_responses.pop(0) if self.token_responses else {"token": "T-XYZ"}
            return httpx.Response(200, json=scripted)
        if request.url.path == "/api/workflow/paService/doCreateRequest":
            return httpx.Response(200, json=self.create_response)
        return httpx.Response(404, json={"code": "NOT_FOUND"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), base_url="http://e9.test"
        )

    def calls_of(self, path: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"] == path]

    def create_calls(self) -> list[dict[str, Any]]:
        return self.calls_of("/api/workflow/paService/doCreateRequest")


def _adapter(stub: _E9Stub) -> E9OaAdapter:
    return E9OaAdapter(
        base_url="http://e9.test",
        client=stub.client(),
        appid="app-001",
        leave_workflow_id="23",
    )


def test_submit_leave_builds_request_and_maps_result() -> None:
    """提交请假：完整认证链 + form 体字段映射 + SUCCESS → doc_no=E9-8888。"""
    stub = _E9Stub()
    adapter = _adapter(stub)
    result = _run(
        adapter.submit_leave(
            user_id="E1001",
            leave_type="annual",
            start_time=_START,
            end_time=_END,
            duration_days=1.5,
            reason="家中有事",
        )
    )
    assert result["doc_no"] == "E9-8888"
    assert result["status"] == "submitted"
    # 认证链第一步 regist：appid/ClientId/OperationCode/cpk 头，换 spk+secrit
    regist = stub.calls_of("/api/ec/dev/auth/regist")[0]
    assert regist["headers"]["appid"] == "app-001"
    assert regist["headers"]["clientid"] == "xa.crm"
    assert regist["headers"]["operationcode"] == "xa.oa.wf.regist"
    assert len(regist["headers"]["cpk"]) > 100  # 客户端 RSA 公钥（Base64）
    # 第二步 applytoken：secret 头 = secrit 用 spk RSA 加密（可解密还原）
    token_call = stub.calls_of("/api/ec/dev/auth/applytoken")[0]
    assert token_call["headers"]["appid"] == "app-001"
    assert token_call["headers"]["operationcode"] == "xa.oa.wf.applytoken"
    assert token_call["headers"]["time"] == "3600"
    assert _rsa_decrypt(token_call["headers"]["secret"]) == "SEC-001"
    # 业务请求头（OaClient.restfulPost 口径）：token + RSA 加密 userid + 明文备查头
    create = stub.create_calls()[0]
    headers = create["headers"]
    assert headers["appid"] == "app-001"
    assert headers["token"] == "T-XYZ"
    assert headers["clientid"] == "xa.crm"
    assert headers["operationcode"] == "xa.oa.wf.doCreateRequest"
    assert headers["useridnoencrypt"] == "E1001"
    assert _rsa_decrypt(headers["userid"]) == "E1001"
    assert headers["content-type"] == "application/x-www-form-urlencoded; charset=utf-8"
    # form 体：workflowId + requestName + mainData 字段数组 + otherParams
    form = create["form"]
    assert form["workflowId"] == "23"
    assert "E1001的请假申请" in form["requestName"]
    assert form["detailData"] == "[]"
    assert json.loads(form["otherParams"]) == {"isnextflow": 0}
    # 默认映射 = 调休申请表单真实字段；调休流程无类型字段，qjlx 不发送
    fields = {f["fieldName"]: f["fieldValue"] for f in json.loads(form["mainData"])}
    assert fields == {
        "Requester": "E1001",
        "dxqs": "2026-10-12 09:00",
        "dxqz": "2026-10-13 18:00",
        "dxts": "1.5",
        "dxxss": "12",  # 1.5 天 × 8 小时
        "TXReason": "0",  # 调休时长来源默认加班
        "Content": "家中有事",
    }


def test_submit_leave_custom_mapping_and_user_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """字段映射覆盖（duration_hours→Txxss + 类型字段）与 E9 人员 id 映射。"""
    monkeypatch.setenv(
        "E9_USER_ID_MAP",
        json.dumps({"E1001": "66"}),  # 系统账号 → E9 人员 id（int 浏览框）
    )
    monkeypatch.setenv(
        "E9_LEAVE_FIELD_MAP",
        json.dumps(
            {
                "applicant": "Requester",
                "start_time": "dxqs",
                "end_time": "dxqz",
                "duration_days": "dxts",
                "duration_hours": "Txxss",
                "reason": "Content",
                "leave_type": "qjlx",  # 通用请假流程才映射类型字段
            }
        ),
    )
    stub = _E9Stub()
    result = _run(
        _adapter(stub).submit_leave("E1001", "annual", _START, _END, 1.0, "事由")
    )
    assert result["doc_no"] == "E9-8888"
    fields = {
        f["fieldName"]: f["fieldValue"]
        for f in json.loads(stub.create_calls()[0]["form"]["mainData"])
    }
    assert fields == {
        "Requester": "66",
        "dxqs": "2026-10-12 09:00",
        "dxqz": "2026-10-13 18:00",
        "dxts": "1",
        "Txxss": "8",
        "Content": "事由",
        "qjlx": "年假",
    }
    # userid 头与 Requester 同口径：均传映射后的 E9 人员 id（RSA 加密）
    create = stub.create_calls()[0]
    assert _rsa_decrypt(create["headers"]["userid"]) == "66"
    assert create["headers"]["useridnoencrypt"] == "66"


def test_submit_leave_explicit_tx_reason_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """对话指定 tx_reason 优先于 env E9_TXREASON；非法值显式拒绝。"""
    monkeypatch.setenv("E9_TXREASON", "0")  # env 默认加班
    stub = _E9Stub()
    result = _run(
        _adapter(stub).submit_leave("E1001", "comp", _START, _END, 1.0, "事由", tx_reason=2)
    )
    assert result["doc_no"] == "E9-8888"
    fields = {
        f["fieldName"]: f["fieldValue"]
        for f in json.loads(stub.create_calls()[0]["form"]["mainData"])
    }
    assert fields["TXReason"] == "2"  # 显式传参覆盖 env 默认
    with pytest.raises(ValueError, match="无效调休时长来源"):
        _run(_adapter(stub).submit_leave("E1001", "comp", _START, _END, 1.0, "事由", tx_reason=9))


def test_submit_leave_param_error_raises_with_errmsg() -> None:
    """E9 返回失败码（PARAM_ERROR）→ ValueError 带 errMsg（参数错误不静默）。"""
    stub = _E9Stub(
        create_response={"code": "PARAM_ERROR", "data": {}, "errMsg": {"workflowId": "0"}}
    )
    with pytest.raises(ValueError, match="PARAM_ERROR"):
        _run(_adapter(stub).submit_leave("E1001", "sick", _START, _END, 0.5, "就医"))


def test_token_cached_across_submits() -> None:
    """token 缓存：两次提交仅一次 regist + applytoken。"""
    stub = _E9Stub()
    adapter = _adapter(stub)
    for _ in range(2):
        _run(adapter.submit_leave("E1001", "personal", _START, _END, 1.0, "事由"))
    assert len(stub.calls_of("/api/ec/dev/auth/regist")) == 1
    assert len(stub.calls_of("/api/ec/dev/auth/applytoken")) == 1
    assert len(stub.create_calls()) == 2


def test_applytoken_empty_then_re_regist() -> None:
    """applytoken 返回空 token → 清服务端密钥重新 regist 再取（OaClient.getToken 兜底）。"""
    stub = _E9Stub(token_responses=[{}, {"token": "T-XYZ"}])
    result = _run(_adapter(stub).submit_leave("E1001", "comp", _START, _END, 1.0, "事由"))
    assert result["doc_no"] == "E9-8888"
    assert len(stub.calls_of("/api/ec/dev/auth/regist")) == 2
    assert len(stub.calls_of("/api/ec/dev/auth/applytoken")) == 2


def test_read_paths_not_implemented() -> None:
    """E9 对外 REST 未开放的读路径：明确 NotImplementedError（上层降级）。"""
    adapter = _adapter(_E9Stub())
    with pytest.raises(NotImplementedError):
        _run(adapter.get_leave_balance("E1001"))
    with pytest.raises(NotImplementedError):
        _run(adapter.list_pending_approvals("E1001"))
    with pytest.raises(NotImplementedError):
        _run(adapter.approve("E9-REQ-1", "approve", "ok", "u001"))
    with pytest.raises(NotImplementedError):
        _run(adapter.list_activity_log("E1001"))


def test_get_adapter_mode_e9(monkeypatch: pytest.MonkeyPatch) -> None:
    """OA_MODE=e9 → E9OaAdapter（配置齐备时）。"""
    monkeypatch.setenv("OA_MODE", "e9")
    monkeypatch.setenv("E9_LEAVE_WORKFLOW_ID", "23")
    oa_client._adapter = None
    adapter = oa_client.get_adapter()
    assert isinstance(adapter, E9OaAdapter)


def test_get_adapter_mode_e9_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """OA_MODE=e9 但缺流程 id → 启动即报错（base_url/appid 有 dev 缺省值）。"""
    monkeypatch.setenv("OA_MODE", "e9")
    for key in ("E9_BASE_URL", "E9_APPID", "E9_CLIENT_ID", "E9_LEAVE_WORKFLOW_ID"):
        monkeypatch.delenv(key, raising=False)
    oa_client._adapter = None
    with pytest.raises(RuntimeError, match="E9 适配器缺少配置"):
        oa_client.get_adapter()


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch: pytest.MonkeyPatch) -> Any:
    """单测互不影响：清 E9 映射类 env（保证默认字段映射口径）+ 重置全局单例。"""
    monkeypatch.delenv("E9_LEAVE_FIELD_MAP", raising=False)
    monkeypatch.delenv("E9_LEAVE_TYPE_LABELS", raising=False)
    monkeypatch.delenv("E9_USER_ID_MAP", raising=False)
    oa_client._adapter = None
    yield
    oa_client._adapter = None
