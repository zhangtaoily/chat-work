// Loopback 登录（RFC 8252，PRD 8.5.8）：
// 桌面端在本机随机高端口起 HTTP server（仅绑定 127.0.0.1），系统浏览器完成
// Keycloak OIDC 登录后重定向到 http://127.0.0.1:{port}/callback，
// 避免 redirect 外发被内网劫持。
// TODO: OIDC Authorization Code + PKCE 流程实现
import { app } from 'electron'

// OS 密钥链存储（安全红线）：
// access/refresh token 不得以明文落盘——统一存 Windows Credential Manager /
// macOS Keychain（经 Electron safeStorage 加密后再持久化）。
// TODO: 登录成功后写入密钥链；启动时静默刷新 access token；登出时清除。

// 开始 Loopback 登录（骨架占位）
export async function login(): Promise<void> {
  void app
  // TODO: 1. 随机高端口起 loopback server（仅绑定 127.0.0.1）
  // TODO: 2. 打开系统浏览器访问 Keycloak 授权端点（state + PKCE）
  // TODO: 3. callback 收 code 换 token（经 APISIX 网关），密钥链落库
  // TODO: 4. 失败/超时清理端口与临时状态
}
