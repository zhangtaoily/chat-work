"""授权码与 refresh 会话存储（内存态，进程生命周期；PRD 8.5.2/8.5.4）。

- 授权码：一次性、5 分钟有效，用后即焚（防重放）
- refresh_token：8 小时工作会话，使用后轮换（rotation）；
  已轮换的旧 token 再次出现 → 复用被盗检测 → 吊销整个会话链（PRD 8.5.4）
"""

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

AUTH_CODE_TTL_SECONDS = 5 * 60  # code 一次性 5 分钟（PRD 8.5.2 安全要求 #2）
REFRESH_TTL_SECONDS = 8 * 3600  # refresh_token 8 小时工作会话（PRD 8.5.4）


@dataclass
class AuthCode:
    """授权码条目（PKCE challenge 绑定授权请求）。"""

    sub: str  # 已认证工号
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    scope: str
    nonce: str | None
    expires_at: float
    used: bool = False


@dataclass
class RefreshToken:
    """refresh_token 条目（挂在会话链上，轮换记录后继）。"""

    session_id: str
    sub: str
    expires_at: float
    rotated_to: str | None = None  # 轮换后指向新 token；非 None 即旧 token


@dataclass
class Session:
    """一次登录的会话链（refresh 轮换复用 → 整链吊销）。"""

    session_id: str
    sub: str
    revoked: bool = False
    refresh_tokens: dict[str, RefreshToken] = field(default_factory=dict)


class TokenStore:
    """授权码 + 会话链存储。"""

    def __init__(self) -> None:
        self._codes: dict[str, AuthCode] = {}
        self._sessions: dict[str, Session] = {}

    # ---- 授权码 ----

    def issue_code(self, entry: AuthCode) -> str:
        code = "ac_" + secrets.token_urlsafe(32)
        self._codes[code] = entry
        return code

    def consume_code(self, code: str) -> AuthCode | None:
        """取码并立即焚毁（一次性）；过期/不存在返回 None。"""
        entry = self._codes.pop(code, None)
        if entry is None:
            return None
        if entry.used or time.time() > entry.expires_at:
            return None
        entry.used = True
        return entry

    # ---- 会话与 refresh 轮换 ----

    def new_session(self, sub: str) -> Session:
        session = Session(session_id=secrets.token_urlsafe(16), sub=sub)
        self._sessions[session.session_id] = session
        return session

    def issue_refresh(self, session: Session) -> str:
        self._gc_session(session)
        token = "rt_" + secrets.token_urlsafe(32)
        session.refresh_tokens[token] = RefreshToken(
            session_id=session.session_id,
            sub=session.sub,
            expires_at=time.time() + REFRESH_TTL_SECONDS,
        )
        return token

    def rotate_refresh(self, token: str) -> tuple[Session, str] | None:
        """轮换 refresh_token：旧 token 失效并签发新 token。

        返回 (会话, 新 refresh_token)；复用已轮换/已吊销的旧 token →
        吊销整链（被盗检测）并返回 None。
        """
        for session in self._sessions.values():
            entry = session.refresh_tokens.get(token)
            if entry is None:
                continue
            if session.revoked or entry.rotated_to is not None:
                # 复用检测：旧 token 再次出现，吊销整个会话链（PRD 8.5.4）
                session.revoked = True
                session.refresh_tokens.clear()
                return None
            if time.time() > entry.expires_at:
                return None
            new_token = self.issue_refresh(session)
            entry.rotated_to = new_token
            return session, new_token
        return None

    def revoke_refresh(self, token: str) -> bool:
        """登出/吊销：命中任一会话的 refresh token 即吊销整链。"""
        for session in self._sessions.values():
            if token in session.refresh_tokens:
                session.revoked = True
                session.refresh_tokens.clear()
                return True
        return False

    def is_revoked(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        return session is None or session.revoked

    def session_of_refresh(self, token: str) -> Session | None:
        for session in self._sessions.values():
            if token in session.refresh_tokens:
                return session
        return None

    def _gc_session(self, session: Session) -> None:
        """惰性清理：删除已轮换且超过 refresh TTL 的旧 token。"""
        now = time.time()
        expired = [
            t
            for t, e in session.refresh_tokens.items()
            if e.rotated_to is not None and now > e.expires_at
        ]
        for t in expired:
            del session.refresh_tokens[t]


# 模块级单例（uvicorn 单进程开发态足够；生产 Keycloak 集群存储）
store = TokenStore()


def jwt_id() -> str:
    return secrets.token_urlsafe(16)


def base_claims(sub: str, idp: str, idp_sub: str, dept: str, roles: list[str], perm_ver: int) -> dict[str, Any]:
    """业务 claims（不含时间字段，由签发方统一补）。"""
    return {
        "sub": sub,
        "idp": idp,
        "idp_sub": idp_sub,
        "dept": dept,
        "roles": roles,
        "perm_ver": perm_ver,
    }
