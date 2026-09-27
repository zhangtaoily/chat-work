"""X-Chat-Auth 服务端验签（P1-1，PRD 8.5.5 / ARCHITECTURE 4.2）。

agent_core 鉴权通过后把用户 JWT 经 X-Chat-Auth 头随 MCP 连接透传，
本模块持公钥自行验签 + user_id 一致性校验（防越权访问他人数据）。

校验策略（对自动化 / 本地冒烟友好）：
- 无 HTTP 请求上下文（stdio / 直调 / 单测）→ 放行
- 有请求但无 X-Chat-Auth / Bearer 头 → 放行（自动化定时任务、本地冒烟）
- 有 token：RS256 + iss/aud/exp 验签，失败 → 拒绝
- 验签通过且工具参数带 user_id → 必须与 token 身份一致，不一致 → 拒绝
- 错误以 ValueError 抛出：FastMCP 转 isError → agent_core McpToolError，
  用户看到友好中文文案（优于传输层 401/403）

配置与 agent_core 一致（env 注入）：
- SSO_ISSUER / SSO_AUDIENCE / SSO_JWKS_URI（缺省 {issuer}/protocol/openid-connect/certs）
"""

import os
import time
from typing import Any

import httpx
import jwt

SSO_ISSUER = os.environ.get("SSO_ISSUER", "http://localhost:8012/realms/chat-work")
SSO_AUDIENCE = os.environ.get("SSO_AUDIENCE", "chat-work-desktop")
SSO_JWKS_URI = os.environ.get("SSO_JWKS_URI") or None
CLOCK_LEEWAY = 60  # 时钟偏移 ±60 秒（与 agent_core 一致，PRD 8.5.7）
JWKS_TTL_SECONDS = 10 * 60

_mcp: Any = None  # FastMCP 实例（setup 注入，供 get_context 取请求上下文）

# ---- JWKS 拉取与缓存（照 agent_core auth.py 模式） ----

_jwks_cache: dict[str, Any] | None = None
_jwks_cached_at = 0.0


def setup(mcp: Any) -> None:
    """记录 FastMCP 实例（server.py 在工具注册前调用一次）。"""
    global _mcp
    _mcp = mcp


def _jwks_url() -> str:
    """JWKS 拉取地址：SSO_JWKS_URI 覆盖（容器网络与 issuer 面向地址不一致时）。"""
    return SSO_JWKS_URI or f"{SSO_ISSUER}/protocol/openid-connect/certs"


async def _fetch_jwks() -> dict[str, Any]:
    """从 IdP 拉取 JWKS。"""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(_jwks_url())
        resp.raise_for_status()
        jwks: dict[str, Any] = resp.json()
        return jwks


async def get_verification_key(token: str) -> Any:
    """按 kid 取公钥；TTL 过期刷新，kid 未命中强制刷新一次（密钥轮换场景）。"""
    global _jwks_cache, _jwks_cached_at
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256":
        raise jwt.InvalidAlgorithmError("仅允许 RS256")

    now = time.time()
    if _jwks_cache is None or now - _jwks_cached_at > JWKS_TTL_SECONDS:
        _jwks_cache = await _fetch_jwks()
        _jwks_cached_at = now

    kid = header.get("kid")
    for key in _jwks_cache.get("keys", []):
        if key.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key)
    _jwks_cache = await _fetch_jwks()
    _jwks_cached_at = now
    for key in _jwks_cache.get("keys", []):
        if key.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key)
    raise jwt.InvalidKeyError(f"JWKS 中无 kid：{kid}")


# ---- 请求上下文与守卫 ----


def _request() -> Any | None:
    """当前 HTTP 请求（starlette Request）；请求外 / stdio / 直调返回 None。

    FastMCP.get_context() 请求外返回 Context(request_context=None)。
    """
    if _mcp is None:
        return None
    try:
        ctx = _mcp.get_context()
    except LookupError:
        return None
    rc = getattr(ctx, "request_context", None)
    if rc is None:
        return None
    return getattr(rc, "request", None)


def _bearer(request: Any) -> str | None:
    """从 Authorization: Bearer 或 X-Chat-Auth（MCP 透传头）提取 token。"""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    forwarded = request.headers.get("x-chat-auth", "")
    return forwarded.strip() or None


async def verify_token(token: str) -> dict[str, Any]:
    """RS256 + iss/aud/exp 验签 → claims；失败抛 jwt 异常。"""
    key = await get_verification_key(token)
    claims: dict[str, Any] = jwt.decode(
        token,
        key=key,
        algorithms=["RS256"],
        issuer=SSO_ISSUER,
        audience=SSO_AUDIENCE,
        leeway=CLOCK_LEEWAY,
        options={"require": ["exp", "iss", "aud", "sub"]},
    )
    return claims


async def guard(user_id: str | None = None) -> str | None:
    """工具层越权守卫：有 token 必须验签 + user_id 一致性校验。

    - 无请求上下文 / 无 token → 放行（自动化、本地冒烟），返回 None
    - 验签失败 → ValueError（拒绝）
    - user_id 传入且与 token 身份不一致 → ValueError（防越权）
    - 通过 → 返回 token 身份工号（preferred_username or sub）
    """
    request = _request()
    if request is None:
        return None
    token = _bearer(request)
    if not token:
        return None
    try:
        claims = await verify_token(token)
    except Exception as exc:
        raise ValueError(f"身份校验失败，拒绝访问：{exc}") from exc
    token_user = str(claims.get("preferred_username") or claims["sub"])
    if user_id is not None and user_id != token_user:
        raise ValueError(
            f"越权访问被拒绝：登录身份 {token_user} 与目标用户 {user_id} 不一致"
        )
    return token_user
