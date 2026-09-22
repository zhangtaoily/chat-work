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
ADMIN_BASE_URL = os.environ.get("ADMIN_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
_REDIRECT_URI = f"{ADMIN_BASE_URL}/admin/callback"

# 管理角色（PRD 5.6.1 矩阵：任一角色可登录工作台；页面级再细分）
ADMIN_ROLES = {"system_admin", "security_reviewer", "knowledge_manager", "org_admin", "auditor"}

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


# ---- 工作台 4：系统配置（系统管理员，PRD 5.6.1/8.3/11） ----


@router.get("/syscfg", response_model=None)
async def syscfg_page(request: Request, msg: str = "", err: str = "", probed: str = "") -> RedirectResponse | HTMLResponse:
    sess = require_session(request)
    if sess is None:
        return login_redirect("/admin/syscfg")
    if bad := _gate(request, sess, {"system_admin"}):
        return bad
    from agent_core import syscfg

    probe_results = json.loads(probed) if probed else None
    return _templates.TemplateResponse(
        request,
        "syscfg.html",
        page_ctx(
            sess,
            nav="syscfg",
            toggles=syscfg.list_toggles(),
            mcp_servers=syscfg.list_mcp(),
            model_route=syscfg.model_view(),
            probe_results=probe_results,
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

