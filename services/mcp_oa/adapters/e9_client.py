"""泛微 E-cology 9（E9）适配器：请假单创建走「流程实例：新建」REST 接口。

认证链路（与 xinao-crm xinao-sdk xinao-oa OaClient.java 一致，三步）：
1) regist：POST /api/ec/dev/auth/regist
   headers：appid / ClientId / OperationCode / cpk（客户端 RSA 公钥，Base64）
   → 返回服务端公钥 spk 与密钥 secrit（动态签发，无需配置静态 secret）
2) applytoken：POST /api/ec/dev/auth/applytoken
   headers：appid / secret（secrit 用 spk 做 RSA/ECB/PKCS1Padding 加密后 Base64）/
   ClientId / OperationCode / time=3600 → 返回 token，进程内缓存 30 分钟；
   token 为空时清服务端密钥重新 regist 再取一次（同 OaClient.getToken 兜底）
3) 业务请求（POST form-urlencoded）：
   headers：appid / token / ClientId / OperationCode /
   userid（E9 人员 id 用 spk RSA 加密后 Base64）/ userIdNoEncrypt（明文）；
   E9 要求所有 POST 的 Content-Type 为 application/x-www-form-urlencoded; charset=utf-8

接口依据（泛微 E9 后端接口文档 e-cloudstore + xinao-sdk OaClient 落地实现）：
- 工作流程 / 流程实例：新建(对外)
  POST {base}/api/workflow/paService/doCreateRequest
  form 参数：mainData（``[{"fieldName": 字段名, "fieldValue": 字段值}]`` JSON）、
  detailData（"[]"，请假单无明细）、requestName（流程标题）、workflowId（流程 id）、
  otherParams（{"isnextflow": 0}，仅建单不自动流转，提交人在 E9 手工流转）；
  返回 {code, data: {requestid}, errMsg, msg}，code=1/SUCCESS 为成功
  （BaseResponse.isSuccess 口径），data.requestid 为流程请求 id。

配置（env，OA_MODE=e9 时启用）：
- E9_BASE_URL           E9 网关地址；缺省 dev 环境 http://192.168.0.83:9026
- E9_APPID              E9 应用 appid；缺省 dev key 4E2DA988-2568-11F0-96FA-5254005EB14A
- E9_CLIENT_ID          集成标识；缺省 xa.crm（与 dev key 的 OperationCode 许可配套）
- E9_LEAVE_WORKFLOW_ID  流程 id（必填；E9 后台「调休申请」流程的编号）
- E9_LEAVE_FIELD_MAP    可选 JSON：字段名映射，缺省即「调休申请」表单真实字段
                        （Requester/dxqs/dxqz/dxts/dxxss/Content，逐字一致）
- E9_LEAVE_TYPE_LABELS  可选 JSON：假期类型 → 表单选项文案（仅当 leave_type 被
                        映射时才发送，通用请假流程才需要）
- E9_USER_ID_MAP        可选 JSON：系统 user_id → E9 人员 id。Requester 为 int
                        人员浏览框，系统账号与 E9 hrm id 不一致时必配，缺省透传
- E9_HOURS_PER_DAY      可选：一天折算小时数（默认 8），用于调休小时数字段
- E9_TXREASON           可选：TXReason（调休时长来源）下拉选项值，加班=0/旅游=1/
                        其他=2，默认 0（加班）；submit_leave 显式传 tx_reason 优先，
                        置空不发送该字段

字段发送口径（E9 接口注意事项 4：主表参数与表单字段不一致、参数多了都不允许
正常新建流程）：只发送已映射的非空业务字段——
- Requester 申请人、dxqs 调休期始、dxqz 调休期止、dxts 调休天数、
  dxxss 调休小时数（= 天数 × E9_HOURS_PER_DAY）、Content 申请描述
- 制单人/制单日期、申请部门/公司、加签/抄送人、相关附件、ndljdxsc（年度累计
  调休时长）、WorkflowCode 等由 E9 端联动/默认值带出，不发送
- TXReason（调休时长来源）为下拉 int 选项值：加班=0、旅游=1、其他=2。
  对话指定（agent 传 tx_reason）优先；缺省 E9_TXREASON（默认 0 加班），置空不发送
- dxts 与 dxxss 同为表单可见字段均发送；若实际生效的是 Txxss（调休小时数1，
  decimal），用 E9_LEAVE_FIELD_MAP 把 duration_hours 覆盖为 Txxss

边界说明：E9 对外 REST 未开放假期余额接口，读路径方法抛 NotImplementedError，
由上层统一降级：余额查询失败 → pipeline 跳过余额校验（graph.py extract 节点已
兜底），年假/调休额度由 E9 流程端校验。xinao-sdk OaClient 另有进行中流程列表
（getDoingWorkflowRequestList）/提交/删除等接口（同一认证链路），后续按需接入。

userid 口径：header（RSA 加密后）与 mainData 的申请人均传映射后的 E9 人员 id
（E9_USER_ID_MAP，缺省透传），两处保持一致。
"""

import base64
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, cast

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# token 进程内缓存 30 分钟（OaClient.java 的 Redis 30 分钟口径）
_TOKEN_TTL_SECONDS = 1800

# dev（测试）环境缺省值，与 xinao-crm xinao-sdk xinao-oa OaClient.java 一致
_DEV_BASE_URL = "http://192.168.0.83:9026"
_DEV_APPID = "4E2DA988-2568-11F0-96FA-5254005EB14A"
_DEFAULT_CLIENT_ID = "xa.crm"

# 各接口的 OperationCode（E9 集成中心按 OperationCode 绑定许可）
_OP_REGIST = "xa.oa.wf.regist"
_OP_APPLYTOKEN = "xa.oa.wf.applytoken"
_OP_DOCREATE = "xa.oa.wf.doCreateRequest"

# 请假表单默认字段名：「调休申请」流程（XA.TXSQ）真实表单字段
# （标签↔字段名对照由客户提供；E9 拒绝表单外字段，未映射的不发送）
_DEFAULT_FIELD_MAP: dict[str, str] = {
    "applicant": "Requester",  # 申请人（int 人员浏览框，传 E9 人员 id）
    "start_time": "dxqs",  # 调休期始
    "end_time": "dxqz",  # 调休期止
    "duration_days": "dxts",  # 调休天数
    "duration_hours": "dxxss",  # 调休小时数（varchar；decimal 版为 Txxss，可覆盖）
    "tx_reason": "TXReason",  # 调休时长来源（下拉 int：加班0/旅游1/其他2）
    "reason": "Content",  # 申请描述
    "leave_type": "",  # 调休专用流程无类型字段；通用请假流程可映射为 qjlx
}

# 假期类型 → E9 表单选项文案（枚举键与 adapters.oa_client.LEAVE_TYPES 一致）
_DEFAULT_TYPE_LABELS: dict[str, str] = {
    "annual": "年假",
    "comp": "调休",
    "sick": "病假",
    "personal": "事假",
    "marriage": "婚假",
    "bereavement": "丧假",
    "maternity": "产假",
}

_E9_DT_FORMAT = "%Y-%m-%d %H:%M"  # E9 日期时间字段常规口径

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"

# 诊断日志：请求/响应全文走 stderr（server.py basicConfig 落地），
# E9 4xx/5xx 的响应体（含真正 errMsg）随异常透传给调用方
logger = logging.getLogger(__name__)


def _ensure_ok(resp: httpx.Response, path: str) -> None:
    """E9 4xx/5xx 抛 RuntimeError 并携带响应体（raise_for_status 会丢弃错误详情）。"""
    if resp.status_code >= 400:
        logger.error(
            "E9 HTTP %s %s 响应体：%s", resp.status_code, path, resp.text[:2000]
        )
        raise RuntimeError(
            f"E9 HTTP {resp.status_code}（{path}）：{resp.text[:2000]}"
        )


def _decode_json(resp: httpx.Response) -> dict[str, Any]:
    """解析 JSON 响应；E9 部分响应为 GBK / 含未转义控制字符，逐级放宽。"""
    raw = resp.content
    for encoding in ("utf-8", "gbk"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            return cast(dict[str, Any], json.loads(text))
        except json.JSONDecodeError:
            return cast(dict[str, Any], json.loads(text, strict=False))
    return cast(dict[str, Any], json.loads(raw.decode("utf-8", errors="replace"), strict=False))


def _load_json_env(name: str, default: dict[str, str]) -> dict[str, str]:
    raw = os.environ.get(name)
    if not raw:
        return dict(default)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"env {name} 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise RuntimeError(f"env {name} 必须是 {{字符串: 字符串}} 的 JSON 对象")
    return parsed


class E9OaAdapter:
    """泛微 E9 适配器：实现 OaAdapter 协议的写路径（请假创建）。

    认证状态（服务端公钥/密钥、token、客户端密钥对）随实例缓存；
    token 30 分钟提前刷新，401 时强制刷新重试一次。
    """

    def __init__(
        self,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        appid: str | None = None,
        client_id: str | None = None,
        leave_workflow_id: str | None = None,
    ) -> None:
        # base_url/appid/client_id 缺省用 dev 环境值（OaClient.java 口径）；生产用 env 覆盖
        self._base_url = (
            base_url or os.environ.get("E9_BASE_URL", _DEV_BASE_URL)
        ).rstrip("/")
        self._appid = appid or os.environ.get("E9_APPID", _DEV_APPID)
        self._client_id = client_id or os.environ.get("E9_CLIENT_ID", _DEFAULT_CLIENT_ID)
        workflow_id = (
            leave_workflow_id
            if leave_workflow_id is not None
            else os.environ.get("E9_LEAVE_WORKFLOW_ID", "")
        )
        if not workflow_id:
            raise RuntimeError("E9 适配器缺少配置：E9_LEAVE_WORKFLOW_ID")
        self._workflow_id = int(workflow_id)
        self._field_map = _load_json_env("E9_LEAVE_FIELD_MAP", _DEFAULT_FIELD_MAP)
        self._type_labels = _load_json_env("E9_LEAVE_TYPE_LABELS", _DEFAULT_TYPE_LABELS)
        self._user_map = _load_json_env("E9_USER_ID_MAP", {})
        self._hours_per_day = float(os.environ.get("E9_HOURS_PER_DAY", "8"))
        self._tx_reason = os.environ.get("E9_TXREASON", "0").strip()
        self._client = client or httpx.AsyncClient(base_url=self._base_url)
        # 认证链状态：regist 签发的服务端公钥/密钥 + 客户端密钥对 + token 缓存
        self._server_pub: rsa.RSAPublicKey | None = None
        self._server_secret: str | None = None
        self._client_key: rsa.RSAPrivateKey | None = None
        self._client_pub_b64: str | None = None
        self._cached_token: str | None = None
        self._token_fetched_at = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- 认证（regist → applytoken，OaClient.java 口径） ----------

    def _rsa_encrypt_b64(self, text: str) -> str:
        """服务端公钥 RSA/ECB/PKCS1Padding 加密后 Base64（hutool RSA 缺省算法）。"""
        pub = self._server_pub
        if pub is None:
            raise RuntimeError("E9 未完成 regist：缺少服务端公钥")
        return base64.b64encode(pub.encrypt(text.encode("utf-8"), padding.PKCS1v15())).decode(
            "ascii"
        )

    async def _regist(self) -> None:
        """第一步：注册应用，换取服务端公钥（spk）与密钥（secrit）。"""
        if self._client_key is None or self._client_pub_b64 is None:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self._client_key = key
            self._client_pub_b64 = base64.b64encode(
                key.public_key().public_bytes(
                    Encoding.DER, PublicFormat.SubjectPublicKeyInfo
                )
            ).decode("ascii")
        resp = await self._client.post(
            "/api/ec/dev/auth/regist",
            headers={
                "appid": self._appid,
                "ClientId": self._client_id,
                "OperationCode": _OP_REGIST,
                "cpk": self._client_pub_b64,
            },
        )
        _ensure_ok(resp, "/api/ec/dev/auth/regist")
        logger.info("E9 regist 成功：appid=%s ClientId=%s", self._appid, self._client_id)
        body: dict[str, Any] = _decode_json(resp)
        spk = body.get("spk")
        secrit = body.get("secrit")
        if not spk or not secrit:
            raise RuntimeError(f"E9 regist 失败：{body}")
        self._server_pub = cast(
            rsa.RSAPublicKey, serialization.load_der_public_key(base64.b64decode(str(spk)))
        )
        self._server_secret = str(secrit)

    async def _fetch_token(self) -> str:
        """第二步：secrit 用 spk RSA 加密后换 token（secret 动态签发，非静态配置）。"""
        if self._server_secret is None or self._server_pub is None:
            await self._regist()
        assert self._server_secret is not None and self._server_pub is not None
        resp = await self._client.post(
            "/api/ec/dev/auth/applytoken",
            headers={
                "appid": self._appid,
                "secret": self._rsa_encrypt_b64(self._server_secret),
                "ClientId": self._client_id,
                "OperationCode": _OP_APPLYTOKEN,
                "time": "3600",
            },
        )
        _ensure_ok(resp, "/api/ec/dev/auth/applytoken")
        body: dict[str, Any] = _decode_json(resp)
        token = str(body.get("token") or "")
        logger.info("E9 applytoken 成功：token=%s…", token[:8])
        return token

    async def _token(self) -> str:
        """获取（或复用缓存的）E9 REST token；空 token 时重新 regist 再取一次。"""
        if self._cached_token and (
            time.monotonic() - self._token_fetched_at
        ) < _TOKEN_TTL_SECONDS:
            return self._cached_token
        token = await self._fetch_token()
        if not token:
            # 服务端密钥/公钥可能已轮换：清缓存重新 regist 再取（OaClient.getToken 兜底）
            self._server_pub = None
            self._server_secret = None
            token = await self._fetch_token()
        if not token:
            raise RuntimeError("E9 applytoken 失败：未获取到 token")
        self._cached_token = token
        self._token_fetched_at = time.monotonic()
        return token

    async def _post_with_auth(
        self, path: str, operation_code: str, data: dict[str, str], user_id: str
    ) -> dict[str, Any]:
        # userid 头与 mainData 申请人口径一致：均传映射后的 E9 人员 id，RSA 加密后传输
        e9_user_id = self._user_map.get(user_id, user_id)
        headers = {
            "appid": self._appid,
            "token": await self._token(),
            "ClientId": self._client_id,
            "OperationCode": operation_code,
            "userid": self._rsa_encrypt_b64(e9_user_id),
            "userIdNoEncrypt": e9_user_id,
            "Content-Type": _FORM_CONTENT_TYPE,
        }
        resp = await self._client.post(path, data=data, headers=headers)
        if resp.status_code == 401:  # token 失效：强制刷新重试一次
            logger.warning("E9 %s 返回 401：token 失效，刷新后重试", path)
            self._cached_token = None
            headers["token"] = await self._token()
            resp = await self._client.post(path, data=data, headers=headers)
        _ensure_ok(resp, path)
        body: dict[str, Any] = _decode_json(resp)
        logger.info("E9 %s 响应：%s", path, body)
        return body

    # ---------- 写路径：请假创建（doCreateRequest） ----------

    async def submit_leave(
        self,
        user_id: str,
        leave_type: str,
        start_time: datetime,
        end_time: datetime,
        duration_days: float,
        reason: str,
        tx_reason: int | None = None,
    ) -> dict[str, Any]:
        e9_user_id = self._user_map.get(user_id, user_id)
        if tx_reason is not None and tx_reason not in (0, 1, 2):
            raise ValueError(f"无效调休时长来源：{tx_reason}，可选值 0=加班/1=旅游/2=其他")
        effective_tx = self._tx_reason if tx_reason is None else str(tx_reason)
        fields_spec: list[tuple[str, str]] = [
            ("applicant", e9_user_id),
            ("start_time", start_time.strftime(_E9_DT_FORMAT)),
            ("end_time", end_time.strftime(_E9_DT_FORMAT)),
            ("duration_days", f"{duration_days:g}"),
            ("duration_hours", f"{duration_days * self._hours_per_day:g}"),
            ("reason", reason),
        ]
        if self._field_map.get("leave_type"):
            # 类型字段按需发送：调休专用流程表单无此字段（多传被 E9 拒绝）
            if leave_type not in self._type_labels:
                raise ValueError(
                    f"E9 表单选项映射缺失假期类型：{leave_type}，"
                    f"可用 E9_LEAVE_TYPE_LABELS 配置"
                )
            fields_spec.append(("leave_type", self._type_labels[leave_type]))
        if effective_tx:
            # 调休时长来源：下拉 int 选项值（加班0/旅游1/其他2）；
            # 对话指定（tx_reason 传参）优先，缺省 E9_TXREASON（默认加班 0）
            fields_spec.append(("tx_reason", effective_tx))
        main_data = [
            {"fieldName": self._field_map[key], "fieldValue": value}
            for key, value in fields_spec
            if self._field_map.get(key)
        ]
        # isnextflow=0：仅建单不自动流转（dev E9 流转环节报 SYSTEM_INNER_ERROR），
        # 由提交人在 E9 流程中心手工流转
        form = {
            "mainData": json.dumps(main_data, ensure_ascii=False),
            "detailData": "[]",
            "requestName": f"{user_id}的请假申请（{duration_days:g}天）",
            "workflowId": str(self._workflow_id),
            "otherParams": json.dumps({"isnextflow": 0}),
        }
        logger.info(
            "E9 doCreateRequest 请求：workflowId=%s requestName=%s otherParams=%s "
            "mainData=%s（user_id=%s→E9 id=%s）",
            self._workflow_id, form["requestName"], form["otherParams"],
            form["mainData"], user_id, e9_user_id,
        )
        body = await self._post_with_auth(
            "/api/workflow/paService/doCreateRequest", _OP_DOCREATE, form, user_id
        )
        code = str(body.get("code", ""))
        if code not in ("1", "SUCCESS"):  # BaseResponse.isSuccess 口径
            logger.error("E9 建单失败（HTTP 200 但 code 异常）：%s", body)
            raise ValueError(
                f"E9 新建流程失败：code={code} errMsg={body.get('errMsg') or body.get('msg')}"
            )
        requestid = (body.get("data") or {}).get("requestid")
        if requestid is None:
            raise ValueError(f"E9 返回缺少 requestid：{body}")
        return {
            "doc_no": f"E9-{requestid}",
            "approval_id": f"E9-REQ-{requestid}",
            "status": "submitted",
            "message": f"请假单已在 E9 创建流程（requestid={requestid}），进入流程审批",
        }

    # ---------- 读路径：E9 对外 REST 未开放，统一明确降级 ----------

    async def get_leave_balance(
        self, user_id: str, leave_type: str | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "E9 对外 REST 未提供假期余额接口：余额校验由 E9 流程端完成"
        )

    async def list_pending_approvals(
        self, user_id: str, doc_type: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("E9 待办暂在流程中心处理，对外接口待接")

    async def approve(
        self, approval_id: str, action: str, comment: str, operator_id: str
    ) -> dict[str, Any]:
        raise NotImplementedError("E9 审批动作暂在流程中心处理，对外接口待接")

    async def list_activity_log(self, user_id: str, days: int = 7) -> list[dict[str, Any]]:
        raise NotImplementedError("E9 操作日志暂无对外接口")
