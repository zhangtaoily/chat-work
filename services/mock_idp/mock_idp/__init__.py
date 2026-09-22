"""mock-idp：本地 OIDC Broker（PRD 8.5）。

模拟 Keycloak 身份代理的最小协议面：
- authorize：登录页（工号即身份，HR 主数据为权威源，PRD 8.5.6）
- token：Authorization Code + PKCE(S256) 兑换、refresh_token 轮换（PRD 8.5.2/8.5.4）
- certs/discovery：RS256 公钥（JWKS）+ OIDC 发现文档

生产切换：协议兼容 OIDC 标准，仅需将 SSO_ISSUER 指向真实 Keycloak。
"""

__version__ = "0.1.0"
