"""Web 管理后台（PLAN P2.7，PRD 5.6）：会话 / OIDC 网页登录 / 门禁 / CSRF / 模板基建。

会话（PRD 5.6.4）：服务端会话表（进程内 dict），cookie 只存随机 sid
（HttpOnly + SameSite=Lax + Path=/admin）；30 分钟无操作自动锁定——
滑动闲置超时（每次访问续期），过期即弃（重新登录）。

登录（PRD 5.6.3）：复用 mock_idp / Keycloak 同 realm 授权码 + PKCE S256
流程——/admin/login 302 到 IdP 授权页（复用员工登录页），/admin/callback
兑换 token 后用 auth.verify_token 本地验签取 AuthContext（roles/dept）。

门禁：ADMIN_ROLES 任一角色方可建会话（PRD 5.6.1 管理角色矩阵）；
CSRF：会话绑定随机 token，全部 POST 表单校验（_csrf 隐藏域）；
敏感页响应统一 no-store，不落浏览器缓存（PRD 5.6.4）。
"""

import base64
import csv
import hashlib
import io
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from agent_core.api import auth as api_auth

router = APIRouter(prefix="/admin")

# ---- 配置（env 注入；默认本地冒烟拓扑） ----
SSO_ISSUER = os.environ.get("SSO_ISSUER", "http://localhost:8012/realms/chat-work")
CLIENT_ID = "chat-work-desktop"  # 与桌面端同 client（同 realm 一套账号，PRD 5.6.3）
ADMIN_BASE_URL = os.environ.get("ADMIN_BASE_URL", "http://127.0.0.1:8011").rstrip("/")
_REDIRECT_URI = f"{ADMIN_BASE_URL}/admin/callback"

# 管理角色（PRD 5.6.1 矩阵：任一角色可登录工作台；页面级再细分）
ADMIN_ROLES = {"system_admin", "security_reviewer", "knowledge_manager", "org_admin", "auditor"}

# mock_idp 内部管理端点基地址（账号管理页联动：停用/启用/重置密码，P0-3）
MOCK_IDP_URL = os.environ.get("MOCK_IDP_URL", "http://127.0.0.1:8012").rstrip("/")

IDLE_TTL_SECONDS = 30 * 60  # 会话 30 分钟无操作锁定（PRD 5.6.4）
FLOW_TTL_SECONDS = 5 * 60  # OIDC 授权流程态有效期（与授权码生命周期对齐）
SESSION_COOKIE = "admin_session"

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ---- 服务端会话表（sid → 会话数据；进程内存放，重启即全体重登） ----
_sessions: dict[str, dict[str, Any]] = {}
# ---- OIDC 授权流程态（state → {verifier, next, created_at}；防 CSRF/重放） ----
_flows: dict[str, dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _gc() -> None:
    """惰性清理：过期会话与超时流程态。"""
    now = _now()
    for sid in [s for s, v in _sessions.items() if now - v["last_seen"] > IDLE_TTL_SECONDS]:
        _sessions.pop(sid, None)
    for state in [s for s, v in _flows.items() if now - v["created_at"] > FLOW_TTL_SECONDS]:
        _flows.pop(state, None)


def create_session(ctx: api_auth.AuthContext) -> dict[str, Any]:
    """由已验签身份建管理会话（含 CSRF token；进入即视为活跃）。"""
    sess = {
        "sid": secrets.token_urlsafe(32),
        "user_id": ctx.user_id,
        "dept": ctx.dept,
        "roles": list(ctx.roles),
        "csrf": secrets.token_urlsafe(32),
        "last_seen": _now(),
    }
    _sessions[sess["sid"]] = sess
    return sess


def get_session(sid: str | None) -> dict[str, Any] | None:
    """取会话并滑动续期（30 分钟无操作锁定，PRD 5.6.4）。"""
    _gc()
    if not sid or sid not in _sessions:
        return None
    sess = _sessions[sid]
    sess["last_seen"] = _now()
    return sess


def drop_session(sid: str | None) -> None:
    if sid:
        _sessions.pop(sid, None)


async def _exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    """授权码 + PKCE 兑换 token（独立函数便于测试替换）。"""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SSO_ISSUER}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": code_verifier,
            },
            timeout=10.0,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"token 端点兑换失败：HTTP {resp.status_code}")
    return resp.json()


def _pkce_pair() -> tuple[str, str]:
    """(verifier, challenge)：S256（RFC 7636，PRD 8.5.2）。"""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _authorize_url(state: str, challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "scope": "openid",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{SSO_ISSUER}/protocol/openid-connect/auth?{query}"


def _render_error(request: Request, status: int, message: str) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "error.html",
        {"message": message},
        status_code=status,
    )


# ---- 登录 / 回调 / 登出 ----


@router.get("/login", response_model=None)
async def login(request: Request, next: str = "/") -> RedirectResponse | HTMLResponse:
    """已有有效会话直达工作台；否则 302 到 IdP 授权页（复用员工登录界面）。"""
    _gc()
    sess = get_session(request.cookies.get(SESSION_COOKIE))
    if sess is not None:
        return RedirectResponse("/admin/", status_code=302)
    if not next.startswith("/"):  # 防 open redirect
        next = "/"
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    _flows[state] = {"verifier": verifier, "next": next, "created_at": _now()}
    return RedirectResponse(_authorize_url(state, challenge), status_code=302)


@router.get("/callback", response_model=None)
async def callback(request: Request, code: str = "", state: str = "") -> RedirectResponse | HTMLResponse:
    """IdP 回调：验 state → 兑 token → 本地验签 → 管理角色门禁 → 建会话。"""
    flow = _flows.pop(state, None) if state else None
    if flow is None:
        return _render_error(request, 400, "登录流程态无效或已过期（state 校验失败），请重新登录")
    if not code:
        return _render_error(request, 400, "授权回调缺少 code，请重新登录")
    try:
        tokens = await _exchange_code(code, flow["verifier"])
        ctx = await api_auth.verify_token(str(tokens["access_token"]))
    except Exception as exc:  # noqa: BLE001 — 兑换/验签失败统一回错误页（IdP 未起/密钥不符等）
        return _render_error(request, 502, f"登录失败：{exc}")
    if not ADMIN_ROLES & set(ctx.roles):
        return _render_error(
            request,
            403,
            f"账号 {ctx.user_id} 无管理角色（需要 {'/'.join(sorted(ADMIN_ROLES))} 之一），禁止进入管理后台",
        )
    sess = create_session(ctx)
    resp = RedirectResponse(flow["next"] or "/admin/", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE,
        sess["sid"],
        max_age=IDLE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/admin",
    )
    return resp


@router.post("/logout", response_model=None)
async def logout(request: Request) -> RedirectResponse | HTMLResponse:
    """登出：销毁会话 + 清 cookie（CSRF 校验防跨站强登）。"""
    form = await request.form()
    sess = get_session(request.cookies.get(SESSION_COOKIE))
    if sess is not None and form.get("_csrf") != sess["csrf"]:
        return _render_error(request, 403, "CSRF 校验失败，操作被拒绝")
    drop_session(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/admin/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/admin")
    return resp


# ---- 页面/表单共用助手（p2-7d 工作台路由使用） ----


def require_session(request: Request) -> dict[str, Any] | None:
    """页面门禁：无有效会话返回 None（调用方 302 /admin/login）。"""
    return get_session(request.cookies.get(SESSION_COOKIE))


def verify_csrf(sess: dict[str, Any], form: Any) -> bool:
    """表单 CSRF 校验（会话绑定 token，PRD 8.5 安全基线）。"""
    return secrets.compare_digest(str(form.get("_csrf", "")), str(sess["csrf"]))


def page_ctx(sess: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """公共模板上下文：当前用户 / 角色 / CSRF token（base.html 导航用）。"""
    ctx = {
        "user_id": sess["user_id"],
        "dept": sess["dept"],
        "roles": sess["roles"],
        "csrf": sess["csrf"],
    }
    ctx.update(extra)
    return ctx


def login_redirect(next_path: str = "/") -> RedirectResponse:
    """未登录跳转（保留回跳目标，quote 防 header 注入）。"""
    return RedirectResponse(f"/admin/login?next={quote(next_path)}", status_code=302)


def _gate(request: Request, sess: dict[str, Any], allowed: set[str]) -> HTMLResponse | None:
    """页面级角色门禁（PRD 5.6.1 矩阵）：无任一角色 → 403 错误页。"""
    if not allowed & set(sess["roles"]):
        return _render_error(
            request,
            403,
            f"需要角色 {'/'.join(sorted(allowed))}，当前角色 {'/'.join(sess['roles']) or '-'}",
        )
    return None


def _back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    """操作后回跳（msg 成功 / err 失败提示经 query 传递）。"""
    params: dict[str, str] = {}
    if msg:
        params["msg"] = msg
    if err:
        params["err"] = err
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}{urlencode(params)}" if params else url, status_code=302)


# ---- 工作台：根跳转 ----


@router.get("/", response_model=None)
async def index(request: Request) -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/")
    return RedirectResponse("/admin/reviews", status_code=302)


# ---- 工作台 1：技能评审（安全评审员，PRD 5.6.1/3.5） ----


@router.get("/reviews", response_model=None)
async def reviews_page(request: Request, msg: str = "", err: str = "") -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/reviews")
    if bad := _gate(request, sess, {"security_reviewer"}):
        return bad
    from agent_core.skills import store as skill_store

    items = skill_store.list_meta(include_unpublished=True)
    return _templates.TemplateResponse(
        request,
        "reviews.html",
        page_ctx(sess, nav="reviews", items=items, msg=msg, err=err),
    )


@router.post("/reviews/{name}/decision", response_model=None)
async def review_decide(request: Request, name: str) -> RedirectResponse | HTMLResponse:
    """评审动作：approve/reject → skills store（双人复核逻辑在 store：首核
    保持 in_review 落 first_approved 审计，异核通过才发布，PRD 5.6.4）。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/reviews")
    if bad := _gate(request, sess, {"security_reviewer"}):
        return bad
    from agent_core.skills import store as skill_store

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/reviews", err="CSRF 校验失败，操作被拒绝")
    approve = str(form.get("action", "")) == "approve"
    try:
        meta = await skill_store.review(name, approve=approve, by=sess["user_id"], note=str(form.get("note", "")))
    except ValueError as exc:
        return _back("/admin/reviews", err=str(exc))
    if approve and meta["status"] == "published":
        return _back("/admin/reviews", msg=f"技能 {name} 双人复核通过，已发布")
    if approve:
        return _back("/admin/reviews", msg=f"技能 {name} 第一复核通过，待第二评审人复核")
    return _back("/admin/reviews", msg=f"技能 {name} 已驳回")


# ---- 工作台 2：知识库审核（知识库管理员，PRD 5.6.1/9.5） ----


@router.get("/knowledge", response_model=None)
async def knowledge_page(request: Request, msg: str = "", err: str = "") -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/knowledge")
    if bad := _gate(request, sess, {"knowledge_manager"}):
        return bad
    from agent_core.knowledge import store as knowledge_store

    items = knowledge_store.list_docs(status="pending_review")
    return _templates.TemplateResponse(
        request,
        "knowledge.html",
        page_ctx(sess, nav="knowledge", items=items, msg=msg, err=err),
    )


@router.post("/knowledge/{doc_id}/decision", response_model=None)
async def knowledge_decide(request: Request, doc_id: str) -> RedirectResponse | HTMLResponse:
    """审核动作：D2/D3 双人复核（首核保持 pending_review 落 first_approved），
    D1 单人即过；驳回单人生效（逻辑在 knowledge store）。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/knowledge")
    if bad := _gate(request, sess, {"knowledge_manager"}):
        return bad
    from agent_core.knowledge import store as knowledge_store

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/knowledge", err="CSRF 校验失败，操作被拒绝")
    approve = str(form.get("action", "")) == "approve"
    try:
        doc = await knowledge_store.review(doc_id, approve=approve, by=sess["user_id"], note=str(form.get("note", "")))
    except ValueError as exc:
        return _back("/admin/knowledge", err=str(exc))
    if approve and doc["status"] == "published":
        return _back("/admin/knowledge", msg=f"文档「{doc['title']}」审核通过，已发布")
    if approve:
        return _back("/admin/knowledge", msg=f"文档「{doc['title']}」第一复核通过，待第二审核人复核")
    return _back("/admin/knowledge", msg=f"文档「{doc['title']}」已驳回")


# ---- 工作台 3：自动化治理（PRD 5.6.2 全员总览/熔断暂停） ----

_AUTOMATION_ADMINS = {"system_admin", "dept_manager", "security_reviewer"}


@router.get("/automation", response_model=None)
async def automation_page(request: Request, msg: str = "", err: str = "") -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/automation")
    if bad := _gate(request, sess, _AUTOMATION_ADMINS):
        return bad
    from agent_core import automation, syscfg

    items = automation.list_tasks()
    return _templates.TemplateResponse(
        request,
        "automation.html",
        page_ctx(
            sess,
            nav="automation",
            items=items,
            scheduler_enabled=syscfg.get_toggle("automation.scheduler_enabled"),
            is_system_admin="system_admin" in sess["roles"],
            msg=msg,
            err=err,
        ),
    )


@router.post("/automation/{task_id}/status", response_model=None)
async def automation_status(request: Request, task_id: str) -> RedirectResponse | HTMLResponse:
    """治理动作：暂停/恢复任意成员任务（归属校验放宽为管理角色，PRD 5.6.2）。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/automation")
    if bad := _gate(request, sess, _AUTOMATION_ADMINS):
        return bad
    from agent_core import automation

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/automation", err="CSRF 校验失败，操作被拒绝")
    try:
        if str(form.get("action", "")) == "pause":
            await automation.pause(task_id, by=sess["user_id"])
            return _back("/admin/automation", msg=f"任务 {task_id} 已暂停")
        await automation.resume(task_id, by=sess["user_id"])
        return _back("/admin/automation", msg=f"任务 {task_id} 已恢复")
    except ValueError as exc:
        return _back("/admin/automation", err=str(exc))


# ---- 工作台 3.5：任务单治理（P1-2 @分身布置任务，组织人事域） ----

_ASSIGNMENT_VIEWERS = {"org_admin", "system_admin", "auditor"}
_ASSIGNMENT_ADMINS = {"org_admin", "system_admin"}


@router.get("/assignments", response_model=None)
async def assignments_page(
    request: Request, status: str = "", msg: str = "", err: str = ""
) -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/assignments")
    if bad := _gate(request, sess, _ASSIGNMENT_VIEWERS):
        return bad
    from agent_core.assignments import store as assignments_store

    items = assignments_store.list_all(status=status or None)
    return _templates.TemplateResponse(
        request,
        "assignments.html",
        page_ctx(sess, nav="assignments", items=items, status=status, msg=msg, err=err),
    )


@router.post("/assignments/{task_id}/status", response_model=None)
async def assignment_status(request: Request, task_id: str) -> RedirectResponse | HTMLResponse:
    """治理动作：取消任意未终态任务单（归属校验放宽为管理角色，状态机
    仍生效；接收人推进走聊天/REST，不走治理页）。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/assignments")
    if bad := _gate(request, sess, _ASSIGNMENT_ADMINS):
        return bad
    from agent_core.assignments import store as assignments_store

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/assignments", err="CSRF 校验失败，操作被拒绝")
    try:
        await assignments_store.update_status(
            task_id, "cancelled", by=sess["user_id"], as_admin=True
        )
    except ValueError as exc:
        return _back("/admin/assignments", err=str(exc))
    return _back("/admin/assignments", msg=f"任务单 {task_id} 已取消")


# ---- 工作台 4：系统配置（系统管理员，PRD 5.6.1/8.3/11） ----


@router.get("/syscfg", response_model=None)
async def syscfg_page(
    request: Request, msg: str = "", err: str = "", probed: str = "", mprobed: str = ""
) -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/syscfg")
    if bad := _gate(request, sess, {"system_admin"}):
        return bad
    from agent_core import syscfg

    probe_results = json.loads(probed) if probed else None
    model_probe_results = json.loads(mprobed) if mprobed else None
    return _templates.TemplateResponse(
        request,
        "syscfg.html",
        page_ctx(
            sess,
            nav="syscfg",
            toggles=syscfg.list_toggles(),
            mcp_servers=syscfg.list_mcp(),
            models_data=syscfg.model_view(),
            probe_results=probe_results,
            model_probe_results=model_probe_results,
            msg=msg,
            err=err,
        ),
    )


@router.post("/syscfg/toggle", response_model=None)
async def syscfg_toggle(request: Request) -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/syscfg")
    if bad := _gate(request, sess, {"system_admin"}):
        return bad
    from agent_core import syscfg

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/syscfg", err="CSRF 校验失败，操作被拒绝")
    key = str(form.get("key", ""))
    value = str(form.get("value", "")) == "true"
    try:
        await syscfg.set_toggle(key, value, by=sess["user_id"])
    except ValueError as exc:
        return _back("/admin/syscfg", err=str(exc))
    return _back("/admin/syscfg", msg=f"功能开关 {key} → {value}")


@router.post("/syscfg/mcp", response_model=None)
async def syscfg_mcp_register(request: Request) -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/syscfg")
    if bad := _gate(request, sess, {"system_admin"}):
        return bad
    from agent_core import syscfg

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/syscfg", err="CSRF 校验失败，操作被拒绝")
    try:
        await syscfg.register_mcp(str(form.get("name", "")), str(form.get("url", "")), by=sess["user_id"])
    except ValueError as exc:
        return _back("/admin/syscfg", err=str(exc))
    return _back("/admin/syscfg", msg=f"MCP 服务 {form.get('name')} 已注册")


@router.post("/syscfg/mcp/probe", response_model=None)
async def syscfg_mcp_probe(request: Request) -> RedirectResponse | HTMLResponse:
    """健康探测：POST 后回跳带结果（GET 页面无副作用，探测结果经 probed 传递）。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/syscfg")
    if bad := _gate(request, sess, {"system_admin"}):
        return bad
    from agent_core import syscfg

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/syscfg", err="CSRF 校验失败，操作被拒绝")
    report = await syscfg.probe_mcp()
    probed = quote(json.dumps(report["results"], ensure_ascii=False))
    return _back(f"/admin/syscfg?probed={probed}", msg=f"已探测 {len(report['results'])} 个 MCP 服务")


# ---- 系统配置：LLM 模型注册表（可视化配置，多模型 + 员工自选） ----


@router.post("/syscfg/model", response_model=None)
async def syscfg_model_register(request: Request) -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/syscfg")
    if bad := _gate(request, sess, {"system_admin"}):
        return bad
    from agent_core import syscfg

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/syscfg", err="CSRF 校验失败，操作被拒绝")
    try:
        await syscfg.register_model(
            str(form.get("name", "")),
            str(form.get("base_url", "")).strip(),
            str(form.get("model", "")),
            str(form.get("api_key", "")),
            by=sess["user_id"],
        )
    except ValueError as exc:
        return _back("/admin/syscfg", err=str(exc))
    return _back("/admin/syscfg", msg=f"模型 {form.get('name')} 已保存")


@router.post("/syscfg/model/delete", response_model=None)
async def syscfg_model_delete(request: Request) -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/syscfg")
    if bad := _gate(request, sess, {"system_admin"}):
        return bad
    from agent_core import syscfg

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/syscfg", err="CSRF 校验失败，操作被拒绝")
    name = str(form.get("name", ""))
    try:
        await syscfg.delete_model(name, by=sess["user_id"])
    except ValueError as exc:
        return _back("/admin/syscfg", err=str(exc))
    return _back("/admin/syscfg", msg=f"模型 {name} 已删除")


@router.post("/syscfg/model/default", response_model=None)
async def syscfg_model_default(request: Request) -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/syscfg")
    if bad := _gate(request, sess, {"system_admin"}):
        return bad
    from agent_core import syscfg

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/syscfg", err="CSRF 校验失败，操作被拒绝")
    name = str(form.get("name", ""))
    try:
        await syscfg.set_default_model(name, by=sess["user_id"])
    except ValueError as exc:
        return _back("/admin/syscfg", err=str(exc))
    return _back("/admin/syscfg", msg=f"默认模型 → {name}")


@router.post("/syscfg/model/toggle", response_model=None)
async def syscfg_model_toggle(request: Request) -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/syscfg")
    if bad := _gate(request, sess, {"system_admin"}):
        return bad
    from agent_core import syscfg

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/syscfg", err="CSRF 校验失败，操作被拒绝")
    name = str(form.get("name", ""))
    value = str(form.get("value", "")) == "true"
    try:
        await syscfg.set_model_enabled(name, value, by=sess["user_id"])
    except ValueError as exc:
        return _back("/admin/syscfg", err=str(exc))
    return _back("/admin/syscfg", msg=f"模型 {name} {'已启用' if value else '已停用'}")


@router.post("/syscfg/model/probe", response_model=None)
async def syscfg_model_probe(request: Request) -> RedirectResponse | HTMLResponse:
    """模型连通性探测：GET {base_url}/models，结果经 mprobed 回显。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/syscfg")
    if bad := _gate(request, sess, {"system_admin"}):
        return bad
    from agent_core import syscfg

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/syscfg", err="CSRF 校验失败，操作被拒绝")
    report = await syscfg.probe_models()
    mprobed = quote(json.dumps(report["results"], ensure_ascii=False))
    return _back(f"/admin/syscfg?mprobed={mprobed}", msg=f"已探测 {len(report['results'])} 个模型")


# ---- 工作台 5：审计日志（审计员，PRD 5.6.1/10；导出带水印，PRD 5.6.4） ----


@router.get("/audit", response_model=None)
async def audit_page(
    request: Request,
    user_id: str = "",
    action: str = "",
    limit: int = 100,
    msg: str = "",
    err: str = "",
) -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/audit")
    if bad := _gate(request, sess, {"auditor", "system_admin"}):
        return bad
    from agent_core import audit

    limit = max(1, min(limit, 500))
    items = await audit.recent(limit, user_id=user_id or None, action=action or None)
    return _templates.TemplateResponse(
        request,
        "audit.html",
        page_ctx(
            sess,
            nav="audit",
            items=items,
            f_user=user_id,
            f_action=action,
            f_limit=limit,
            msg=msg,
            err=err,
        ),
    )


@router.get("/audit/export", response_model=None)
async def audit_export(request: Request, user_id: str = "", action: str = "", limit: int = 500) -> RedirectResponse | HTMLResponse | StreamingResponse:
    """审计导出 CSV：文件头水印行带操作人与时间（PRD 5.6.4）；导出动作自身落审计
    （"审计者也被审计"）。PRD 5.6.2 的"双人复核导出"本轮简化为操作人标记留痕。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/audit")
    if bad := _gate(request, sess, {"auditor", "system_admin"}):
        return bad
    from agent_core import audit

    limit = max(1, min(limit, 2000))
    items = await audit.recent(limit, user_id=user_id or None, action=action or None)
    await audit.record(
        "audit_export",
        tool="admin_web",
        params={"user_id": user_id or None, "action": action or None, "limit": limit},
        user_id=sess["user_id"],
        result="success",
        detail=f"审计导出 {len(items)} 条（操作人 {sess['user_id']}，CSV 水印标记）",
    )
    buf = io.StringIO()
    buf.write("# Chat-Work 审计导出（PRD 5.6.4 水印）\n")
    buf.write(f"# 操作人: {sess['user_id']}  导出时间: {datetime.now(UTC).isoformat(timespec='seconds')}  条数: {len(items)}\n")
    writer = csv.writer(buf)
    writer.writerow(["time", "user_id", "session_id", "action", "tool", "params_digest", "result", "duration_ms", "detail"])
    for e in items:
        writer.writerow(
            [e.get(k) if e.get(k) is not None else "" for k in
             ("time", "user_id", "session_id", "action", "tool", "params_digest", "result", "duration_ms", "detail")]
        )
    filename = f"audit_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{sess['user_id']}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


# ---- 工作台 6：数字分身账号（P0-3：org_admin 管理，system_admin 只读） ----
#
# 分身模型：分身不是独立账号，而是「账号的 agent 化执行面」——分身身份
# 即员工本人身份。本页治理：分身开关（@可达性）、冻结（三合一）、
# 技能授权绑定（写入技能授予/回收，deny 语义）、密码重置（IdP 联动）。

_ACCOUNT_ADMINS = {"org_admin"}
_ACCOUNT_VIEWERS = {"org_admin", "system_admin"}


async def _idp_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """调 mock_idp 内部管理端点（/internal/users/*）；模块级便于测试打桩。"""
    import httpx

    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(f"{MOCK_IDP_URL}{path}", json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


@router.get("/accounts", response_model=None)
async def accounts_page(request: Request, msg: str = "", err: str = "") -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/accounts")
    if bad := _gate(request, sess, _ACCOUNT_VIEWERS):
        return bad
    from agent_core.accounts import store as accounts_store
    from agent_core.skills import store as skill_store

    items = accounts_store.list_accounts()
    for a in items:
        a["granted"] = skill_store.granted_names(a["emp_no"])
        a["revoked"] = skill_store.revoked_names(a["emp_no"])
    # 授权下拉只列写入类技能：deny 语义仅约束写路径（读路径由数据范围保证）
    skills = [m for m in skill_store.list_meta() if m["rw"] == "write"]
    return _templates.TemplateResponse(
        request,
        "accounts.html",
        page_ctx(
            sess,
            nav="accounts",
            items=items,
            skills=skills,
            can_admin=bool(_ACCOUNT_ADMINS & set(sess["roles"])),
            msg=msg,
            err=err,
        ),
    )


@router.post("/accounts/{emp_no}/twin", response_model=None)
async def account_twin(request: Request, emp_no: str) -> RedirectResponse | HTMLResponse:
    """数字分身开关：关闭仅影响 @数字人可达性（P1 委托会话），不影响本人使用。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/accounts")
    if bad := _gate(request, sess, _ACCOUNT_ADMINS):
        return bad
    from agent_core.accounts import store as accounts_store

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/accounts", err="CSRF 校验失败，操作被拒绝")
    enabled = str(form.get("value", "")) == "true"
    try:
        await accounts_store.set_twin(emp_no, enabled, by=sess["user_id"])
    except ValueError as exc:
        return _back("/admin/accounts", err=str(exc))
    return _back("/admin/accounts", msg=f"{emp_no} 数字分身已{'开启' if enabled else '关闭'}")


@router.post("/accounts/{emp_no}/freeze", response_model=None)
async def account_freeze(request: Request, emp_no: str) -> RedirectResponse | HTMLResponse:
    """冻结三合一：agent 侧 frozen 标记 + token denylist（全部 token 立即 401）
    + IdP 停用（不能再登录）；解冻反向联动。IdP 不可达不阻断 agent 侧生效。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/accounts")
    if bad := _gate(request, sess, _ACCOUNT_ADMINS):
        return bad
    from agent_core.accounts import store as accounts_store

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/accounts", err="CSRF 校验失败，操作被拒绝")
    freeze = str(form.get("action", "")) == "freeze"
    if freeze and emp_no == sess["user_id"]:
        return _back("/admin/accounts", err="不能冻结当前登录账号")
    try:
        await accounts_store.set_frozen(emp_no, freeze, by=sess["user_id"])
        if freeze:
            api_auth.deny_user(emp_no)
        else:
            api_auth.allow_user(emp_no)
        idp_note = ""
        try:
            await _idp_post(f"/internal/users/{emp_no}/status", {"enabled": not freeze})
        except Exception as exc:  # noqa: BLE001 — IdP 失败降级为提示，agent 侧已生效
            idp_note = f"（IdP 侧{'停用' if freeze else '启用'}失败：{exc}）"
    except ValueError as exc:
        return _back("/admin/accounts", err=str(exc))
    if freeze:
        return _back("/admin/accounts", msg=f"账号 {emp_no} 已冻结（token 立即失效）{idp_note}")
    return _back("/admin/accounts", msg=f"账号 {emp_no} 已解冻{idp_note}")


@router.post("/accounts/{emp_no}/password", response_model=None)
async def account_password(request: Request, emp_no: str) -> RedirectResponse | HTMLResponse:
    """密码重置：调 mock_idp /internal/users/{emp_no}/password（≥8 位）。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/accounts")
    if bad := _gate(request, sess, _ACCOUNT_ADMINS):
        return bad
    from agent_core import audit

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/accounts", err="CSRF 校验失败，操作被拒绝")
    new_password = str(form.get("password", ""))
    if len(new_password) < 8:
        return _back("/admin/accounts", err="新密码至少 8 位")
    try:
        await _idp_post(f"/internal/users/{emp_no}/password", {"password": new_password})
    except Exception as exc:  # noqa: BLE001 — IdP 不可达/未知工号
        return _back("/admin/accounts", err=f"密码重置失败：{exc}")
    await audit.record(
        "account_password_reset",
        tool="admin_web",
        params={"emp_no": emp_no},
        user_id=sess["user_id"],
        result="ok",
        detail=f"重置 {emp_no} 登录密码（明文不落审计）",
    )
    return _back("/admin/accounts", msg=f"账号 {emp_no} 密码已重置")


@router.post("/accounts/{emp_no}/grants", response_model=None)
async def account_grants(request: Request, emp_no: str) -> RedirectResponse | HTMLResponse:
    """技能授权绑定（技能 ↔ 数字分身账号）：授予 = 授权记录；回收后该账号
    对此写技能立即拒绝（permission 节点消费 is_revoked，deny 语义）。"""
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/accounts")
    if bad := _gate(request, sess, _ACCOUNT_ADMINS):
        return bad
    from agent_core import audit
    from agent_core.accounts import store as accounts_store
    from agent_core.skills import store as skill_store

    form = await request.form()
    if not verify_csrf(sess, form):
        return _back("/admin/accounts", err="CSRF 校验失败，操作被拒绝")
    if accounts_store.get(emp_no) is None:
        return _back("/admin/accounts", err=f"账号 {emp_no} 不存在")
    name = str(form.get("skill", ""))
    action = str(form.get("action", ""))
    try:
        if action == "grant":
            await skill_store.grant(name, user_id=emp_no, by=sess["user_id"])
            await audit.record(
                "skill_grant",
                tool="admin_web",
                params={"skill": name, "user_id": emp_no},
                user_id=sess["user_id"],
                result="ok",
                detail=f"授予 {emp_no} 技能「{name}」",
            )
            return _back("/admin/accounts", msg=f"已向 {emp_no} 授予技能「{name}」")
        if action == "revoke":
            await skill_store.revoke_access(
                name, user_id=emp_no, by=sess["user_id"], note=str(form.get("note", ""))
            )
            return _back(
                "/admin/accounts",
                msg=f"已回收 {emp_no} 对「{name}」的授权，写路径立即拒绝",
            )
    except ValueError as exc:
        return _back("/admin/accounts", err=str(exc))
    return _back("/admin/accounts", err="未知操作")

