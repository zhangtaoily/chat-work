"""Mock OIDC Provider 主入口（PRD 8.5，协议面对齐 Keycloak 端点风格）。

端点：
- GET  /health
- GET  /realms/chat-work/.well-known/openid-configuration   # OIDC 发现
- GET  /realms/chat-work/protocol/openid-connect/auth        # 授权端点（登录页）
- POST /realms/chat-work/protocol/openid-connect/auth        # 工号认证 → 发授权码
- POST /realms/chat-work/protocol/openid-connect/token        # code+PKCE 换 token / refresh 轮换
- GET  /realms/chat-work/protocol/openid-connect/certs        # JWKS（RS256 公钥）
- POST /realms/chat-work/protocol/openid-connect/revoke       # 登出吊销（refresh token）

安全实现（PRD 8.5.2 安全要求）：
- PKCE 必须（S256）；code 一次性 5 分钟；state 由客户端校验（Broker 透传）
- redirect_uri 仅允许 127.0.0.1 loopback（RFC 8252）+ 配置内的生产回调
- 账号映射：工号必须命中 HR 主数据，否则拒绝（PRD 8.5.6，不自动创建）
"""

import os
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mock_idp import keys, pages
from mock_idp.store import (
    AUTH_CODE_TTL_SECONDS,
    AuthCode,
    TokenStore,
    base_claims,
    jwt_id,
    store,
)

app = FastAPI(title="mock-idp", version="0.1.0")

# ---- 配置（生产切换真实 Keycloak 时仅需替换 issuer 与密钥） ----
ISSUER = os.environ.get("SSO_ISSUER", "http://localhost:8012/realms/chat-work")
ACCESS_TTL_SECONDS = 30 * 60  # access_token 30 分钟（PRD 8.5.4）
PERM_VER = int(os.environ.get("MOCK_PERM_VER", "17"))  # 权限数据版本号
CLIENT_ID = "chat-work-desktop"

# 授权端点必备参数
_REQUIRED_AUTH_PARAMS = (
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
)


def _validate_loopback_redirect(redirect_uri: str) -> bool:
    """仅允许 127.0.0.1 loopback（RFC 8252 原生应用回调）。"""
    from urllib.parse import urlparse

    parsed = urlparse(redirect_uri)
    return parsed.scheme == "http" and parsed.hostname == "127.0.0.1"


def _issue_tokens(sub: str, session_id: str) -> dict[str, Any]:
    """签发 access_token(JWT RS256) + id_token + refresh_token（PRD 8.5.4 claims）。"""
    from mock_idp.users import find_by_emp_no

    user = find_by_emp_no(sub)
    if user is None:  # 理论上 authorize 已拦截；防御式兜底
        raise ValueError(f"工号不在 HR 主数据：{sub}")

    now = int(time.time())
    claims = base_claims(
        sub=user.emp_no,
        idp=user.idp,
        idp_sub=user.idp_sub,
        dept=user.dept,
        roles=list(user.roles),
        perm_ver=PERM_VER,
    )
    access = keys.sign_jwt(
        {
            **claims,
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sid": session_id,
            "iat": now,
            "exp": now + ACCESS_TTL_SECONDS,
            "jti": jwt_id(),
        }
    )
    id_token = keys.sign_jwt(
        {
            "iss": ISSUER,
            "sub": user.emp_no,
            "aud": CLIENT_ID,
            "name": user.name,
            "iat": now,
            "exp": now + ACCESS_TTL_SECONDS,
            "jti": jwt_id(),
        }
    )
    session = store._sessions.get(session_id)
    refresh = store.issue_refresh(session) if session else ""
    return {
        "access_token": access,
        "id_token": id_token,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "scope": "openid profile",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0", "issuer": ISSUER}


@app.get("/realms/chat-work/.well-known/openid-configuration")
async def discovery() -> dict[str, Any]:
    """OIDC 发现文档（客户端据此取端点）。"""
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
        "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
        "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
        "revocation_endpoint": f"{ISSUER}/protocol/openid-connect/revoke",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }


@app.get("/realms/chat-work/protocol/openid-connect/certs")
async def certs() -> dict[str, Any]:
    """JWKS：agent_core / 网关本地验签公钥（PRD 8.5.4，避免每次远程校验）。"""
    return keys.jwks()


@app.get("/realms/chat-work/protocol/openid-connect/auth", response_model=None)
async def authorize(
    request: Request,
    response_type: str = Query(""),
    client_id: str = Query(""),
    redirect_uri: str = Query(""),
    scope: str = Query(""),
    state: str = Query(""),
    code_challenge: str = Query(""),
    code_challenge_method: str = Query(""),
    nonce: str = Query(""),
) -> HTMLResponse:
    """授权端点：校验请求合法性后展示登录页（模拟 Keycloak 按用户路由 IdP）。"""
    given: dict[str, str] = {
        "response_type": response_type,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "nonce": nonce,
    }
    missing = [k for k in _REQUIRED_AUTH_PARAMS if not given[k]]
    if missing:
        return HTMLResponse(
            pages.token_error_page(
                "invalid_request", f"缺少授权参数：{', '.join(missing)}"
            ),
            status_code=400,
        )
    if response_type != "code":
        return HTMLResponse(
            pages.token_error_page("unsupported_response_type", "仅支持 response_type=code"),
            status_code=400,
        )
    if client_id != CLIENT_ID:
        return HTMLResponse(
            pages.token_error_page("invalid_client", f"未知客户端：{client_id}"),
            status_code=400,
        )
    if not _validate_loopback_redirect(redirect_uri):
        return HTMLResponse(
            pages.token_error_page(
                "invalid_request", "redirect_uri 仅允许 127.0.0.1 loopback（RFC 8252）"
            ),
            status_code=400,
        )
    if "openid" not in scope:
        return HTMLResponse(
            pages.token_error_page("invalid_scope", "scope 必须包含 openid"),
            status_code=400,
        )
    if code_challenge_method != "S256":
        return HTMLResponse(
            pages.token_error_page("invalid_request", "PKCE 必须使用 S256"),
            status_code=400,
        )
    return HTMLResponse(pages.login_page(given))


@app.post("/realms/chat-work/protocol/openid-connect/auth", response_model=None)
async def authorize_submit(
    response_type: str = Form(""),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    scope: str = Form(""),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form(""),
    nonce: str = Form(""),
    emp_no: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """工号认证 → 签发一次性授权码 → 302 回 loopback（state 透传由客户端校验）。"""
    if not emp_no.strip():
        params = {
            "response_type": response_type,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "nonce": nonce,
        }
        return HTMLResponse(pages.login_page(params, error="请输入员工工号"))

    from mock_idp.users import find_by_emp_no

    user = find_by_emp_no(emp_no)
    if user is None:
        # PRD 8.5.6：无法匹配工号 → 拒绝登录（不自动创建，防影子账号）
        params = {
            "response_type": response_type,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "nonce": nonce,
        }
        return HTMLResponse(
            pages.login_page(params, error="工号无法匹配 HR 主数据，请联系信息科"),
            status_code=401,
        )

    session = store.new_session(sub=user.emp_no)
    code = store.issue_code(
        AuthCode(
            sub=user.emp_no,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
            nonce=nonce or None,
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
        )
    )
    # session_id 记录到 code（token 端点兑换时绑定 refresh 链）
    code_session_binding[code] = session.session_id
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state})}",
        status_code=302,
    )


# 授权码 → 会话绑定（内存；生产由 Keycloak 服务端持久化）
code_session_binding: dict[str, str] = {}


@app.post("/realms/chat-work/protocol/openid-connect/token", response_model=None)
async def token_endpoint(
    request: Request,
    grant_type: str = Form(""),
    code: str = Form(""),
    redirect_uri: str = Form(""),
    client_id: str = Form(""),
    code_verifier: str = Form(""),
    refresh_token: str = Form(""),
) -> JSONResponse | dict[str, Any]:
    """token 端点：authorization_code（PKCE）或 refresh_token（轮换）。"""
    if client_id != CLIENT_ID:
        return _oauth_error("invalid_client", "unknown client", 401)

    if grant_type == "authorization_code":
        return await _exchange_code(code, redirect_uri, code_verifier)
    if grant_type == "refresh_token":
        return await _rotate_refresh(refresh_token)
    return _oauth_error("unsupported_grant_type", "grant_type 必须为 authorization_code 或 refresh_token")  # noqa: E501


async def _exchange_code(
    code: str, redirect_uri: str, code_verifier: str
) -> JSONResponse | dict[str, Any]:
    import hashlib

    entry = store.consume_code(code)
    if entry is None:
        return _oauth_error("invalid_grant", "授权码无效、已使用或已过期（5 分钟一次性）", 400)
    if entry.client_id != CLIENT_ID or entry.redirect_uri != redirect_uri:
        return _oauth_error("invalid_grant", "client_id / redirect_uri 与授权请求不一致", 400)

    # PKCE S256 校验：b64url(sha256(verifier)) == code_challenge（PRD 8.5.2 要求 #1）
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    import base64

    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    if computed != entry.code_challenge:
        return _oauth_error("invalid_grant", "PKCE code_verifier 校验失败", 400)

    session_id = code_session_binding.pop(code, "")
    return _issue_tokens(entry.sub, session_id)


async def _rotate_refresh(refresh_token: str) -> JSONResponse | dict[str, Any]:
    result = store.rotate_refresh(refresh_token)
    if result is None:
        return _oauth_error(
            "invalid_grant", "refresh_token 无效、已吊销或检测到复用（整链吊销）", 400
        )
    session, new_refresh = result
    tokens = _issue_tokens(session.sub, session.session_id)
    # 覆盖 refresh_token 为轮换产物
    return {**tokens, "refresh_token": new_refresh}


@app.post("/realms/chat-work/protocol/openid-connect/revoke")
async def revoke(
    token: str = Form(""),
) -> dict[str, Any]:
    """登出：吊销 refresh 会话链（PRD 8.5.7 强制下线/登出）。"""
    revoked = store.revoke_refresh(token)
    return {"revoked": revoked}


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=status
    )
