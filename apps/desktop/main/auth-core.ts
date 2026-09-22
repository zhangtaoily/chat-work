// SSO 纯逻辑层（PRD 8.5.2/8.5.4/8.5.8）：
// PKCE(S256)、授权端点 URL、id_token 解析、loopback 回调解析、token 端点表单。
// 不依赖 Electron 运行时，便于单测；系统交互（loopback server / safeStorage / 浏览器拉起）在 main/auth.ts。
import { createHash, randomBytes } from 'node:crypto'

// ---- 配置（生产切换真实 Keycloak 时仅需替换 SSO_ISSUER 环境变量） ----
export const CLIENT_ID = 'chat-work-desktop'
export const SCOPE = 'openid profile'
/** 浏览器登录 60 秒超时（PRD 8.5.8） */
export const LOGIN_TIMEOUT_MS = 60_000
/** access_token 剩余寿命低于该值时先静默刷新 */
export const REFRESH_MARGIN_MS = 60_000

export function ssoIssuer(): string {
  return process.env.SSO_ISSUER ?? 'http://localhost:8012/realms/chat-work'
}

// ---- 会话数据结构 ----
/** 由 auth.ts 经 safeStorage 加密后落盘（安全红线：token 不得明文持久化） */
export interface TokenBundle {
  accessToken: string
  refreshToken: string
  idToken: string
  /** access_token 过期时刻（毫秒纪元） */
  expiresAt: number
  /** 工号（HR 主数据权威源，PRD 8.5.6） */
  userId: string
  userName: string
}

export interface AuthStatus {
  loggedIn: boolean
  userId: string
  userName: string
  expiresAt: number
}

// ---- base64url（RFC 4648 §5，Node 'base64url' 编码自带去填充） ----
export function b64url(input: Buffer): string {
  return input.toString('base64url')
}

// ---- PKCE（S256，PRD 8.5.2 安全要求 #1） ----
export interface PkcePair {
  /** code_verifier：32 随机字节 → base64url 43 字符（RFC 7636 §4.1 合法区间 43-128） */
  verifier: string
  /** code_challenge：b64url(sha256(verifier)) */
  challenge: string
}

export function createPkcePair(): PkcePair {
  const verifier = b64url(randomBytes(32))
  const challenge = b64url(createHash('sha256').update(verifier, 'ascii').digest())
  return { verifier, challenge }
}

// ---- 授权端点 URL（GET {issuer}/protocol/openid-connect/auth） ----
export function buildAuthorizeUrl(params: {
  redirectUri: string
  state: string
  pkce: PkcePair
}): string {
  const query = new URLSearchParams({
    response_type: 'code',
    client_id: CLIENT_ID,
    redirect_uri: params.redirectUri,
    scope: SCOPE,
    state: params.state,
    code_challenge: params.pkce.challenge,
    code_challenge_method: 'S256'
  })
  return `${ssoIssuer()}/protocol/openid-connect/auth?${query.toString()}`
}

// ---- id_token 载荷解析 ----
// token 系直接从 token 端点（本机 loopback）兑换获得，客户端可不做签名校验；
// 权威验签在 agent_core 网关（JWKS RS256，PRD 8.5.5）。
export interface IdTokenClaims {
  iss: string
  sub: string
  aud: string
  name?: string
  exp: number
}

export function decodeIdToken(idToken: string): IdTokenClaims {
  const parts = idToken.split('.')
  if (parts.length !== 3) {
    throw new Error('id_token 格式非法（应为三段式 JWT）')
  }
  let claims: IdTokenClaims
  const payload = parts[1]
  if (payload === undefined) {
    throw new Error('id_token 格式非法（应为三段式 JWT）')
  }
  try {
    claims = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8')) as IdTokenClaims
  } catch {
    throw new Error('id_token payload 不是合法 JSON')
  }
  if (!claims.sub) {
    throw new Error('id_token 缺少 sub（工号）')
  }
  return claims
}

// ---- loopback 回调解析（/callback?code=..&state=.. 或错误回调） ----
export interface CallbackQuery {
  code?: string | undefined
  state?: string | undefined
  error?: string | undefined
  errorDescription?: string | undefined
}

export function parseCallbackQuery(rawQuery: string): CallbackQuery {
  const params = new URLSearchParams(rawQuery)
  return {
    code: params.get('code') ?? undefined,
    state: params.get('state') ?? undefined,
    error: params.get('error') ?? undefined,
    errorDescription: params.get('error_description') ?? undefined
  }
}

// ---- token 端点请求体（POST x-www-form-urlencoded，mock_idp/Keycloak 同构） ----
export function authorizationCodeForm(params: {
  code: string
  redirectUri: string
  verifier: string
}): URLSearchParams {
  return new URLSearchParams({
    grant_type: 'authorization_code',
    code: params.code,
    redirect_uri: params.redirectUri,
    client_id: CLIENT_ID,
    code_verifier: params.verifier
  })
}

export function refreshTokenForm(refreshToken: string): URLSearchParams {
  return new URLSearchParams({
    grant_type: 'refresh_token',
    refresh_token: refreshToken,
    client_id: CLIENT_ID
  })
}

// ---- 回执页（loopback server 在系统浏览器标签页渲染） ----
export function receiptPageHtml(ok: boolean, message: string): string {
  return `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>Chat-Work 登录</title>
    <style>
      body { font-family: system-ui, sans-serif; display: flex; align-items: center;
             justify-content: center; height: 100vh; margin: 0; background: #f5f5f5; }
      .card { background: #fff; padding: 40px 56px; border-radius: 12px;
              box-shadow: 0 2px 12px rgba(0,0,0,.08); text-align: center; }
      h1 { font-size: 20px; margin: 0 0 12px; }
      p { color: #666; margin: 0; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>${ok ? '登录成功' : '登录失败'}</h1>
      <p>${escapeHtml(message)}</p>
    </div>
  </body>
</html>`
}

function escapeHtml(text: string): string {
  return text
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}
