// Loopback 登录（RFC 8252，PRD 8.5.8）+ OS 密钥链落盘（safeStorage，安全红线 PRD 8.5）
// 流程：随机端口起 loopback server（仅绑定 127.0.0.1）→ shell.openExternal 拉起系统浏览器
// 完成 OIDC 登录（state 防 CSRF + PKCE S256）→ callback 收 code 换 token →
// safeStorage 加密落盘 → access_token 临近过期时以 refresh_token 静默刷新（轮换）。
// 纯逻辑（PKCE/URL/解析）在 auth-core.ts。
import { app, safeStorage, shell } from 'electron'
import { createServer, type Server } from 'node:http'
import { randomUUID } from 'node:crypto'
import { promises as fsp } from 'node:fs'
import path from 'node:path'
import {
  authorizationCodeForm,
  buildAuthorizeUrl,
  createPkcePair,
  decodeIdToken,
  LOGIN_TIMEOUT_MS,
  parseCallbackQuery,
  receiptPageHtml,
  refreshTokenForm,
  REFRESH_MARGIN_MS,
  ssoIssuer,
  type AuthStatus,
  type TokenBundle
} from './auth-core'

// ---- 本地存储（token 密文，userDocData/tokens.bin） ----
function tokenFilePath(): string {
  return path.join(app.getPath('userData'), 'tokens.bin')
}

let cachedBundle: TokenBundle | null = null
let loginInFlight: Promise<AuthStatus> | null = null
let refreshInFlight: Promise<TokenBundle> | null = null

async function loadBundle(): Promise<TokenBundle | null> {
  if (cachedBundle) return cachedBundle
  try {
    const encrypted = await fsp.readFile(tokenFilePath())
    if (!safeStorage.isEncryptionAvailable()) return null
    cachedBundle = JSON.parse(safeStorage.decryptString(encrypted)) as TokenBundle
    return cachedBundle
  } catch {
    return null // 未登录 / 文件损坏：一律按未登录处理
  }
}

async function saveBundle(bundle: TokenBundle): Promise<void> {
  if (!safeStorage.isEncryptionAvailable()) {
    throw new Error('OS 密钥链不可用，拒绝明文落盘（安全红线，PRD 8.5）')
  }
  cachedBundle = bundle
  await fsp.writeFile(tokenFilePath(), safeStorage.encryptString(JSON.stringify(bundle)))
}

async function clearBundle(): Promise<void> {
  cachedBundle = null
  await fsp.rm(tokenFilePath(), { force: true })
}

function toStatus(bundle: TokenBundle): AuthStatus {
  return {
    loggedIn: true,
    userId: bundle.userId,
    userName: bundle.userName,
    expiresAt: bundle.expiresAt
  }
}

// ---- token 端点（POST {issuer}/protocol/openid-connect/token） ----
interface TokenEndpointResponse {
  access_token: string
  refresh_token: string
  id_token: string
  expires_in: number
}

async function tokenRequest(form: URLSearchParams): Promise<TokenEndpointResponse> {
  let resp: Response
  try {
    resp = await fetch(`${ssoIssuer()}/protocol/openid-connect/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form.toString()
    })
  } catch {
    throw new Error('无法连接认证服务（SSO Broker）')
  }
  const data = (await resp.json().catch(() => ({}))) as Record<string, unknown>
  if (!resp.ok) {
    const error = String(data['error'] ?? `HTTP ${resp.status}`)
    const description = String(data['error_description'] ?? '')
    throw new Error(`token 兑换失败：${error}${description ? `（${description}）` : ''}`)
  }
  for (const key of ['access_token', 'refresh_token', 'id_token', 'expires_in'] as const) {
    if (data[key] === undefined) {
      throw new Error(`token 响应缺少字段：${key}`)
    }
  }
  return data as unknown as TokenEndpointResponse
}

function toBundle(tokens: TokenEndpointResponse): TokenBundle {
  const claims = decodeIdToken(tokens.id_token)
  return {
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    idToken: tokens.id_token,
    expiresAt: Date.now() + tokens.expires_in * 1000,
    userId: claims.sub,
    userName: claims.name ?? claims.sub
  }
}

// ---- loopback server（RFC 8252：随机端口 + 仅绑定 127.0.0.1） ----
type CallbackOutcome =
  | { kind: 'code'; code: string; redirectUri: string }
  | { kind: 'error'; message: string }

interface LoopbackServer {
  port: number
  /** 一次性登录结果（code 成功 / 失败原因），由 /callback 请求 settle */
  outcome: Promise<CallbackOutcome>
  close: () => void
}

function startLoopbackServer(expectedState: string): Promise<LoopbackServer> {
  return new Promise((resolve, reject) => {
    let settled = false
    let resolveOutcome: (outcome: CallbackOutcome) => void = () => undefined
    const outcome = new Promise<CallbackOutcome>((res) => {
      resolveOutcome = res
    })

    const server: Server = createServer((req, res) => {
      const respond = (ok: boolean, message: string, status = 200): void => {
        res.writeHead(status, { 'Content-Type': 'text/html; charset=utf-8' })
        res.end(receiptPageHtml(ok, message))
      }
      const url = req.url ?? '/'
      if (!url.startsWith('/callback')) {
        respond(false, '未知路径', 404)
        return
      }
      if (settled) {
        // 重复回调（浏览器重试/预取）：直接回执成功页
        respond(true, '登录成功，可关闭此页返回应用')
        return
      }
      const query = parseCallbackQuery(
        url.startsWith('/callback?') ? url.slice('/callback'.length + 1) : ''
      )
      // IdP 错误回调（access_denied 等）
      if (query.error) {
        settled = true
        const message = query.errorDescription
          ? `登录失败：${query.error}（${query.errorDescription}）`
          : `登录失败：${query.error}`
        respond(false, message)
        resolveOutcome({ kind: 'error', message })
        return
      }
      // 缺 code/state：多为探测/预取请求，不 settle，继续等待真实回调或超时
      if (!query.code || !query.state) {
        respond(false, '回调参数不完整（缺 code/state）', 400)
        return
      }
      // state 不一致 → 疑似 CSRF（PRD 8.5.2），拒绝并终止
      if (query.state !== expectedState) {
        settled = true
        respond(false, '登录失败：state 校验不通过，请重试')
        resolveOutcome({ kind: 'error', message: 'state 校验不通过（疑似 CSRF），请重试' })
        return
      }
      settled = true
      respond(true, '登录成功，可关闭此页返回应用')
      const addr = server.address()
      const port = addr !== null && typeof addr !== 'string' ? addr.port : 0
      resolveOutcome({
        kind: 'code',
        code: query.code,
        redirectUri: `http://127.0.0.1:${port}/callback`
      })
    })

    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address()
      if (addr === null || typeof addr === 'string') {
        reject(new Error('loopback 监听失败'))
        return
      }
      resolve({ port: addr.port, outcome, close: () => server.close() })
    })
  })
}

function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms)
    timer.unref()
    promise.then(resolve, reject).finally(() => clearTimeout(timer))
  })
}

// ---- 登录入口（幂等：有效会话直接复用；过期会话先尝试静默刷新） ----
export function login(): Promise<AuthStatus> {
  loginInFlight ??= runLogin().finally(() => {
    loginInFlight = null
  })
  return loginInFlight
}

async function runLogin(): Promise<AuthStatus> {
  const existing = await loadBundle()
  if (existing) {
    if (existing.expiresAt - Date.now() > REFRESH_MARGIN_MS) {
      return toStatus(existing)
    }
    try {
      return toStatus(await refreshSession(existing))
    } catch {
      // 刷新链已吊销/失效 → 清本机状态，走完整浏览器登录
      await clearBundle()
    }
  }

  const state = randomUUID()
  const pkce = createPkcePair()
  const loopback = await startLoopbackServer(state)
  try {
    const redirectUri = `http://127.0.0.1:${loopback.port}/callback`
    await shell.openExternal(buildAuthorizeUrl({ redirectUri, state, pkce }))
    // 等待浏览器回调，60 秒超时（PRD 8.5.8）
    const result = await withTimeout(
      loopback.outcome,
      LOGIN_TIMEOUT_MS,
      '登录超时（60 秒），请重试'
    )
    if (result.kind === 'error') {
      throw new Error(result.message)
    }
    const tokens = await tokenRequest(
      authorizationCodeForm({
        code: result.code,
        redirectUri: result.redirectUri,
        verifier: pkce.verifier
      })
    )
    const bundle = toBundle(tokens)
    await saveBundle(bundle)
    return toStatus(bundle)
  } finally {
    loopback.close()
  }
}

// ---- 静默刷新（refresh_token 轮换，PRD 8.5.4；single-flight 防并发重复使用旧 token） ----
function refreshSession(stale: TokenBundle): Promise<TokenBundle> {
  refreshInFlight ??= tokenRequest(refreshTokenForm(stale.refreshToken))
    .then(toBundle)
    .then(async (bundle) => {
      await saveBundle(bundle)
      return bundle
    })
    .finally(() => {
      refreshInFlight = null
    })
  return refreshInFlight
}

/** 有效 access_token；未登录 / 刷新失败（含复用检测整链吊销）→ null */
export async function getAccessToken(): Promise<string | null> {
  const bundle = await loadBundle()
  if (!bundle) return null
  if (bundle.expiresAt - Date.now() > REFRESH_MARGIN_MS) {
    return bundle.accessToken
  }
  try {
    return (await refreshSession(bundle)).accessToken
  } catch {
    return null
  }
}

/** 强制刷新（渲染层收到 401 时调用；服务端可能已轮换/吊销旧 token） */
export async function forceRefresh(): Promise<string | null> {
  const bundle = await loadBundle()
  if (!bundle) return null
  try {
    return (await refreshSession(bundle)).accessToken
  } catch {
    return null
  }
}

/** 登录状态（仅读本机密钥链，不做网络请求） */
export async function getAuthStatus(): Promise<AuthStatus> {
  const bundle = await loadBundle()
  return bundle
    ? toStatus(bundle)
    : { loggedIn: false, userId: '', userName: '', expiresAt: 0 }
}

// ---- 登出：吊销 refresh 会话链（PRD 8.5.7）+ 清本机密钥链 ----
export async function logout(): Promise<void> {
  const bundle = await loadBundle()
  await clearBundle()
  if (bundle) {
    try {
      await fetch(`${ssoIssuer()}/protocol/openid-connect/revoke`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ token: bundle.refreshToken }).toString()
      })
    } catch {
      // 尽力吊销：本机状态已清除，网络失败不阻塞登出
    }
  }
}
