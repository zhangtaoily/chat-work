# SSO 登录开发计划（PRD 8.5，sl-1 ~ sl-5）

> 回溯补档（2026-09-18）。SSO 登录已全部完成并验证通过；本计划为交付过程记录。
> 隶属于产品总计划 [docs/PLAN.md](PLAN.md) 的 P0.5 阶段。

## 目标

桌面端统一身份登录（OIDC Authorization Code + PKCE，RS256）。
生产切真实 Keycloak 仅需将 `SSO_ISSUER` 指向真实环境，协议端点完全兼容 OIDC 标准。

## 依赖关系

```
sl-1（地基：端点契约 + JWKS）
  ├─→ sl-2（客户端侧）──→ sl-4（前端接入）──┐
  └─→ sl-3（服务侧）──────────────────────┴─→ sl-5（汇总验收）
```

## 阶段明细

| 阶段 | 内容 | 交付物 | 验证 |
|------|------|--------|------|
| **sl-1 mock OIDC Broker** | 模拟 Keycloak：discovery / authorize（两步式登录页）/ token / JWKS；RS256 签发、PKCE S256 与 state 校验；HR 种子用户（E1001 张三 / E1002 李四 / E1003 王五），工号即身份（PRD 8.5.6） | `services/mock_idp`（端口 8012） | 13 passed |
| **sl-2 桌面端认证核心** | auth-core 纯逻辑（PKCE 对 / authorize URL / id_token 解析 / 回调解析 / 回执页）；Loopback 登录（RFC 8252：随机端口仅绑 127.0.0.1、60s 超时、state 防 CSRF，PRD 8.5.8）；safeStorage 加密落盘 tokens.bin，不可加密时拒绝明文（PRD 安全红线）；静默刷新（60s 提前量）+ single-flight 防并发；IPC 通道 + zod schema 白名单 + preload 桥（IpcChannel 拆零依赖文件，避免 preload 连带打包主进程 handler） | `apps/desktop/main/auth-core.ts`、`auth.ts`、`ipc.ts`、`ipc-channels.ts`、`preload/bridge.ts` | 主/渲染双侧 typecheck 通过 |
| **sl-3 agent_core JWT 验签** | RS256 + JWKS 缓存（10min TTL，kid 未命中强制刷新，密钥轮换兼容）；iss/aud/exp 校验（±60s 时钟容差，PRD 8.5.7）；jti 黑名单吊销（进程内，生产迁 Redis）；AuthContext（sub/roles/perm_ver/sid），user_id 强制取 JWT sub 防请求体伪造（PRD 8.5.5）；`X-Chat-Auth` 兼容 MCP 透传 | `services/agent_core/agent_core/api/auth.py`、`main.py`（`_authenticate_or_401`） | 54 passed |
| **sl-4 前端登录门接入** | App.tsx 三态（检测中 / 登录门 / 主界面 + 头部身份 Tag 与登出按钮）；authedFetch 统一 Bearer 头 + 401 静默刷新重试一次；web 无桥回退 `u001` 本地冒烟身份 | `apps/desktop/renderer/src/App.tsx`、`lib/api.ts` | 双侧 typecheck 通过 |
| **sl-5 单测 + 全链路验收** | auth-core 单测 17 项（PKCE/URL/解析/表单/XSS 转义）；补 `SSO_REQUIRED` 环境变量接线（生产强制鉴权开关）；agent_core 回归；curl E2E；负路径验证 | PRD 13.1 里程碑更新 | 见下表 |

## 验收记录（curl E2E，SSO_REQUIRED=true 强制鉴权模式）

| 步骤 | 结果 |
|------|------|
| GET authorize（7 参数 + PKCE S256） | 200 登录页 |
| POST `emp_no=E1001` | 302 → `callback?code&state`，state 一致 |
| POST token（authorization_code + verifier） | 200，access_token / id_token / refresh_token |
| POST /chat 带 Bearer（body 伪造 `user_id=u999-forged`） | 200 SSE，身份以 JWT sub 为准 |
| 无 token | 401 |
| 篡改 access_token 尾部 8 字符 | 401（RS256 验签拒绝） |

单测：桌面端 auth-core 17 passed；agent_core 54 passed（含接线改动无回归）。

## 遗留边界

组织权限实接（roles / perm_ver 权限面）与审计日志不属于 SSO 计划，单列于 [PLAN.md](PLAN.md) P1.1 / P1.2。
