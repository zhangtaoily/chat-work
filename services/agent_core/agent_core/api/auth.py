"""JWT 验签与 AuthContext 构建（PRD 8.5.4/8.5.5）。

生产链路：APISIX 网关统一验签 → Agent Core 本地复核（RS256 公钥，JWKS 缓存）；
开发链路：agent-core 直收桌面端 Bearer token（X-Chat-Auth 兼容 MCP 透传头）。

- RS256 + iss/aud/exp 校验，时钟偏移容差 ±60 秒（PRD 8.5.7）
- jti 黑名单：吊销 token 拒绝（进程内 + Redis 双写，多 worker 共享；
  TTL=token 剩余有效期，PRD 8.5.7）
- SSO_REQUIRED=false（默认）：无 token 走请求体直传身份（本地冒烟兼容）；
  true：无 token / 验签失败 → 401

配置外置（P1.3，mock_idp → 真实 Keycloak 仅需改环境变量，代码零改动）：
- SSO_ISSUER：token iss 校验值（默认本地 mock Broker）
- SSO_AUDIENCE：默认 chat-work-desktop
- SSO_JWKS_URI：可选。issuer 面向桌面的地址（如 http://localhost:8080/realms/...）
  与容器内网络可达地址不一致时，覆盖 JWKS 拉取地址（生产标准做法）
"""

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

# ---- 配置（env 注入，compose/内网配置中心统一管理）----

SSO_ISSUER = os.environ.get(
    "SSO_ISSUER", "http://localhost:8012/realms/chat-work"
)  # mock_idp 默认（本地链路）；生产指向真实 Keycloak
SSO_AUDIENCE = os.environ.get("SSO_AUDIENCE", "chat-work-desktop")
SSO_JWKS_URI = os.environ.get("SSO_JWKS_URI") or None
CLOCK_LEEWAY = 60  # 时钟偏移 ±60 秒（PRD 8.5.7）
JWKS_TTL_SECONDS = 10 * 60

_jti_blacklist: dict[str, float] = {}  # jti -> 拒绝截止时间（token exp）
_redis_client: Any = None


_sso_required = False


def set_sso_required(required: bool) -> None:
    """切换强制鉴权开关（生产入口置 true；本地冒烟默认 false）。"""
    global _sso_required
    _sso_required = required


def sso_required() -> bool:
    return _sso_required


def _get_redis() -> Any:
    """jti 黑名单 Redis（多 worker 共享，PRD 8.5.7）；未配置返回 None。"""
    global _redis_client
    if _redis_client is None:
        redis_url = os.environ.get("REDIS_URL")
        if not redis_url:
            return None
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(redis_url, decode_responses=True)
    return _redis_client


def revoke_jti(jti: str, until: float) -> None:
    """吊销 token（强制下线/登出，PRD 8.5.7）；until 通常为 token exp。

    进程内立即生效；Redis 可用时双写（TTL=剩余有效期），多 worker 共享黑名单。
    """
    _jti_blacklist[jti] = until
    try:
        redis = _get_redis()
        if redis is not None:
            ttl = max(int(until - time.time()), 1)
            asyncio.get_running_loop().create_task(
                redis.setex(f"jti:blacklist:{jti}", ttl, "1")
            )
    except RuntimeError:
        pass  # 无运行中事件循环（同步测试调用）→ 仅进程内生效


def _gc_blacklist() -> None:
    now = time.time()
    expired = [j for j, until in _jti_blacklist.items() if until < now]
    for j in expired:
        del _jti_blacklist[j]


# ---- JWKS 拉取与缓存（RS256 公钥本地验签，PRD 8.5.4） ----

_jwks_cache: dict[str, Any] | None = None
_jwks_cached_at = 0.0


def _jwks_url() -> str:
    """JWKS 拉取地址：SSO_JWKS_URI 覆盖（容器网络与 issuer 面向地址不一致时）。"""
    return SSO_JWKS_URI or f"{SSO_ISSUER}/protocol/openid-connect/certs"


async def _fetch_jwks() -> dict[str, Any]:
    """从 IdP 拉取 JWKS（生产亦可由网关静态注入公钥）。"""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(_jwks_url())
        resp.raise_for_status()
        jwks: dict[str, Any] = resp.json()
        return jwks


async def get_verification_key(token: str) -> Any:
    """按 kid 取公钥（PyJWT RSAAlgorithm.from_jwk）。"""
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
    # kid 未命中：强制刷新一次 JWKS（密钥轮换场景），仍无则失败
    _jwks_cache = await _fetch_jwks()
    _jwks_cached_at = now
    for key in _jwks_cache.get("keys", []):
        if key.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key)
    raise jwt.InvalidKeyError(f"JWKS 中无 kid：{kid}")


# ---- 验签与 AuthContext ----


@dataclass(frozen=True)
class AuthContext:
    """一次已认证请求的身份上下文（PRD 8.5.5）。

    权限不进 Token：完整权限经 Redis 权限缓存（TTL 5min）按 perm_ver 构建，
    MVP 阶段权限面 = JWT roles + perm_ver（权限实接在组织权限任务落地）。
    """

    user_id: str  # 工号（HR 主数据主键，sub）
    idp: str
    idp_sub: str
    dept: str
    roles: list[str]
    perm_ver: int
    jti: str
    session_id: str


async def verify_token(token: str) -> AuthContext:
    """验签 + claims 校验 → AuthContext；失败抛 jwt 异常（调用方转 401）。"""
    _gc_blacklist()
    key = await get_verification_key(token)
    claims: dict[str, Any] = jwt.decode(
        token,
        key=key,
        algorithms=["RS256"],
        issuer=SSO_ISSUER,
        audience=SSO_AUDIENCE,
        leeway=CLOCK_LEEWAY,
        options={"require": ["exp", "iss", "aud", "sub", "jti"]},
    )
    jti = str(claims["jti"])
    if jti in _jti_blacklist:
        raise jwt.InvalidTokenError("token 已被吊销（jti 黑名单）")
    redis = _get_redis()
    if redis is not None and await redis.exists(f"jti:blacklist:{jti}"):
        raise jwt.InvalidTokenError("token 已被吊销（jti 黑名单）")

    # user_id=工号：Keycloak sub 为 UUID，username（=工号）优先；mock 链路 sub 即工号
    user_id = str(claims.get("preferred_username") or claims["sub"])

    return AuthContext(
        user_id=user_id,
        idp=str(claims.get("idp", "")),
        idp_sub=str(claims.get("idp_sub", "")),
        dept=str(claims.get("dept", "")),
        roles=[str(r) for r in claims.get("roles", [])],
        perm_ver=int(claims.get("perm_ver", 0)),
        jti=jti,
        session_id=str(claims.get("sid", "")),
    )


def bearer_token(request: Any) -> str | None:
    """从 Authorization: Bearer 或 X-Chat-Auth（MCP 透传，PRD 8.5.5）提取 token。"""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    forwarded = request.headers.get("x-chat-auth", "")
    return forwarded.strip() or None


async def authenticate(request: Any) -> AuthContext | None:
    """统一入口：提取并验证 token；未带 token 返回 None（是否放行由 SSO_REQUIRED 决定）。"""
    token = bearer_token(request)
    if token is None:
        return None
    return await verify_token(token)
