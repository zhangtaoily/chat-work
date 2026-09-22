// auth-core 纯逻辑单测（sl-5）：PKCE / 授权 URL / id_token 解析 / 回调解析 / token 表单 / 回执页
import { createHash } from 'node:crypto'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  authorizationCodeForm,
  b64url,
  buildAuthorizeUrl,
  CLIENT_ID,
  createPkcePair,
  decodeIdToken,
  parseCallbackQuery,
  receiptPageHtml,
  refreshTokenForm,
  SCOPE,
  ssoIssuer
} from './auth-core'

function b64urlJson(value: object): string {
  return Buffer.from(JSON.stringify(value), 'utf8').toString('base64url')
}

describe('createPkcePair（S256，PRD 8.5.2）', () => {
  it('verifier 为 43 字符 base64url 无填充', () => {
    const { verifier } = createPkcePair()
    expect(verifier).toHaveLength(43)
    expect(verifier).toMatch(/^[A-Za-z0-9_-]+$/)
  })

  it('challenge 等于 b64url(sha256(verifier))', () => {
    const { verifier, challenge } = createPkcePair()
    const expected = b64url(createHash('sha256').update(verifier, 'ascii').digest())
    expect(challenge).toBe(expected)
  })

  it('两次调用产生不同密钥对（随机性）', () => {
    expect(createPkcePair()).not.toEqual(createPkcePair())
  })
})

describe('buildAuthorizeUrl', () => {
  const pkce = createPkcePair()

  it('携带 OIDC 授权码 + PKCE 全部 7 参数', () => {
    const url = new URL(
      buildAuthorizeUrl({ redirectUri: 'http://127.0.0.1:51375/callback', state: 's1', pkce })
    )
    expect(url.pathname).toBe('/realms/chat-work/protocol/openid-connect/auth')
    expect(url.searchParams.get('response_type')).toBe('code')
    expect(url.searchParams.get('client_id')).toBe(CLIENT_ID)
    expect(url.searchParams.get('redirect_uri')).toBe('http://127.0.0.1:51375/callback')
    expect(url.searchParams.get('scope')).toBe(SCOPE)
    expect(url.searchParams.get('state')).toBe('s1')
    expect(url.searchParams.get('code_challenge')).toBe(pkce.challenge)
    expect(url.searchParams.get('code_challenge_method')).toBe('S256')
  })

  it('SSO_ISSUER 环境变量可切换 issuer（生产 Keycloak）', () => {
    vi.stubEnv('SSO_ISSUER', 'https://sso.example.com/realms/prod')
    const url = new URL(
      buildAuthorizeUrl({ redirectUri: 'http://127.0.0.1:1/callback', state: 's', pkce })
    )
    expect(url.origin).toBe('https://sso.example.com')
    expect(url.pathname).toBe('/realms/prod/protocol/openid-connect/auth')
  })

  it('ssoIssuer 默认指向本地 mock_idp', () => {
    expect(ssoIssuer()).toBe('http://localhost:8012/realms/chat-work')
  })
})

describe('decodeIdToken', () => {
  const payload = { iss: 'http://localhost:8012/realms/chat-work', sub: 'e001', aud: CLIENT_ID, exp: 9999999999 }

  it('解析三段式 JWT 载荷并返回 sub', () => {
    const idToken = `b64url.${b64urlJson(payload)}.sig`
    const claims = decodeIdToken(idToken)
    expect(claims.sub).toBe('e001')
    expect(claims.exp).toBe(9999999999)
  })

  it('两段式格式抛错', () => {
    expect(() => decodeIdToken('a.b')).toThrow('三段式')
  })

  it('载荷非 JSON 抛错', () => {
    expect(() => decodeIdToken('h.@@@.s')).toThrow('JSON')
  })

  it('缺 sub 抛错', () => {
    const idToken = `h.${b64urlJson({ iss: 'i', aud: 'a', exp: 1 })}.s`
    expect(() => decodeIdToken(idToken)).toThrow('sub')
  })
})

describe('parseCallbackQuery', () => {
  it('成功回调解析 code 与 state', () => {
    expect(parseCallbackQuery('code=abc&state=xyz')).toEqual({
      code: 'abc',
      state: 'xyz',
      error: undefined,
      errorDescription: undefined
    })
  })

  it('错误回调解析 error 与 error_description（URL 解码）', () => {
    const q = parseCallbackQuery('error=access_denied&error_description=%E7%94%A8%E6%88%B7%E6%8B%92%E7%BB%9D')
    expect(q.error).toBe('access_denied')
    expect(q.errorDescription).toBe('用户拒绝')
  })

  it('空查询全字段 undefined', () => {
    expect(parseCallbackQuery('')).toEqual({
      code: undefined,
      state: undefined,
      error: undefined,
      errorDescription: undefined
    })
  })
})

describe('token 端点表单', () => {
  it('authorizationCodeForm 携带授权码兑换全字段', () => {
    const form = authorizationCodeForm({ code: 'c1', redirectUri: 'http://127.0.0.1:9/callback', verifier: 'v1' })
    expect(form.get('grant_type')).toBe('authorization_code')
    expect(form.get('code')).toBe('c1')
    expect(form.get('redirect_uri')).toBe('http://127.0.0.1:9/callback')
    expect(form.get('client_id')).toBe(CLIENT_ID)
    expect(form.get('code_verifier')).toBe('v1')
  })

  it('refreshTokenForm 携带刷新全字段', () => {
    const form = refreshTokenForm('r1')
    expect(form.get('grant_type')).toBe('refresh_token')
    expect(form.get('refresh_token')).toBe('r1')
    expect(form.get('client_id')).toBe(CLIENT_ID)
  })
})

describe('receiptPageHtml', () => {
  it('成功/失败标题正确', () => {
    expect(receiptPageHtml(true, 'ok')).toContain('登录成功')
    expect(receiptPageHtml(false, 'bad')).toContain('登录失败')
  })

  it('消息做 HTML 转义（防 XSS）', () => {
    const html = receiptPageHtml(false, '<script>alert("x")</script>&')
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('&quot;')
    expect(html).toContain('&amp;')
  })
})

afterEach(() => {
  vi.unstubAllEnvs()
})
