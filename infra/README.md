# infra/ 生产部署与小流量灰度（P1.3 + P1.4，PLAN MVP 收尾）

单机 Docker Compose 拓扑（ARCHITECTURE 8.1）：APISIX 接入层 → agent-core → mcp-oa / mcp-bi，
基础设施 MySQL 8 / Redis 7 / Milvus（etcd + MinIO）/ vLLM（GPU，可选 profile），SSO 为真实 Keycloak 26，
另含 update-server 静态更新源（P1.4 桌面端灰度分发）。

```
桌面端(Electron)                    ┌─ 浏览器登录（loopback 回调）
      │  Bearer JWT                ▼
      ▼                        Keycloak :8081 ── MySQL(keycloak 库)
  APISIX :80 ── /api/* ──► agent-core :8000 (2 workers)
                              │       │        │
                              │       │        └── MySQL(chatwork) / Redis / Milvus
                              │       └────────── vLLM :8000/v1（可选，规则兜底）
                              └──► mcp-oa :8001/mcp（仅内网）
                              └──► mcp-bi :8002/mcp（仅内网）

  update-server :8090 ── /releases/desktop/{stable,beta}/（electron-updater feed）
```

## 目录结构

| 文件 | 职责 |
|------|------|
| `compose/docker-compose.yml` | 全栈编排（服务定义 / 健康检查 / env 注入） |
| `apisix/config.yaml` | APISIX 独立模式数据面配置 |
| `apisix/apisix.yaml` | 声明式路由（`#ENDPOINT` 结尾标记勿删） |
| `keycloak/realm-export.json` | chat-work realm：client（PKCE S256）/ 协议 mappers / 种子用户 |
| `mysql/init/01-init.sql` | 首建时创建 keycloak 库与账号 |
| `nginx/update-server.conf` | 更新源 nginx 配置：`latest.yml` 强制 no-cache + `/healthz` |
| `../releases/desktop/{stable,beta}/` | electron-updater feed 产物（安装包 + `latest.yml`），只读挂载进 update-server |
| `llm/`、`k8s/` | 预留：LLM 部署方案与 K8s 清单（PRD 14 待确认后补） |

## 前置条件

- Docker Engine 24+ 与 Compose v2（含 `docker compose` 子命令）
- 端口占用：80（APISIX）、8081（Keycloak）、3306（MySQL）、9000/9001（MinIO）、19530（Milvus）
- GPU 节点（可选）：仅 vLLM 需要；未启用时 agent-core 意图识别/抽参走规则兜底，链路不中断

## 快速启动

```bash
cd infra/compose

# 首次启动（构建镜像 + 建库 + 导入 realm）
docker compose up -d --build

# 查看状态（mysql/keycloak 就绪较慢，等 healthy）
docker compose ps

# 停止 / 销毁（-v 连同数据卷清除，含 MySQL 数据，慎用）
docker compose stop
docker compose down
docker compose down -v
```

服务端口与入口：

| 服务 | 地址 | 说明 |
|------|------|------|
| APISIX | `http://localhost:80` | 唯一业务入口：`/api/*` → agent-core（strip 前缀） |
| Keycloak | `http://localhost:8081` | 浏览器登录页 + OIDC 端点；admin/admin（占位） |
| agent-core | 容器网内 `:8000` | 不对外映射，仅经 APISIX 访问 |
| mcp-oa / mcp-bi | 容器网内 `:8001/:8002` | 仅 agent-core 内网直连，不暴露网关路由 |
| update-server | `http://localhost:8090` | 桌面端自动更新 feed：`/releases/desktop/{stable,beta}/latest.yml` |

## SSO：mock_idp → Keycloak 的切换

本地开发（宿主机直跑）继续用 `services/mock_idp`（`SSO_ISSUER` 默认即 mock）；容器栈即为
真实 Keycloak，切换**零代码改动**，仅 env：

| 变量 | 容器栈取值 | 说明 |
|------|-----------|------|
| `SSO_ISSUER` | `http://localhost:8081/realms/chat-work` | 桌面端浏览器视角 issuer。`start-dev` 无 `KC_HOSTNAME` 时按请求 Host 构造，浏览器经 `localhost:8081` 登录 → token `iss` 与此一致 |
| `SSO_JWKS_URI` | `http://keycloak:8080/realms/chat-work/protocol/openid-connect/certs` | 容器网内 JWKS 直连，解决「issuer 面向宿主机、验签在容器网内」双地址问题 |
| `SSO_AUDIENCE` | `chat-work-desktop` | realm 中 client id |
| `SSO_REQUIRED` | `true` | 生产强制鉴权（无 token / 验签失败一律 401） |

Keycloak 侧约定（`realm-export.json`）：

- client `chat-work-desktop`：public + PKCE 强制 S256；`redirectUris` 现为
  `http://127.0.0.1:8765/auth/callback`，**需与桌面端实际 loopback 回调端口对齐后收窄**
- 协议 mappers 把用户属性注入 access token：`dept` / `roles`（多值）/ `perm_ver`（int）/ `idp` / `idp_sub`；
  `preferred_username` = 用户名（= 工号），agent-core 侧以它优先于 UUID `sub` 取工号（PRD 8.5.4）
- 种子用户（首登密码 `ChatWork@2026`，生产必须改）：E1001 张三（employee）、
  E1002 李四（employee）、E1003 王五（employee + dept_manager）

## agent-core 环境变量清单

| 变量 | 容器栈默认 | 说明 |
|------|-----------|------|
| `DATABASE_URL` | `mysql+aiomysql://app:app@mysql:3306/chatwork` | 占位凭证，生产改密 |
| `REDIS_URL` | `redis://redis:6379/0` | HITL 确认 / 会话记忆 / **jti 黑名单跨 worker 共享**（2 workers 必配） |
| `MCP_OA_URL` / `MCP_BI_URL` | `http://mcp-oa:8001` / `http://mcp-bi:8002` | 客户端自动拼 `/mcp` 路径 |
| `SSO_*` 四项 | 见上表 | 鉴权 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | `http://vllm:8000/v1` / `dummy` / `qwen2.5-32b-instruct` | vLLM 未启动时规则兜底 |
| `MILVUS_URI` | `http://milvus:19530` | 记忆/知识向量 |
| `GRAY_PERCENT` | 未设/`0` = 全放行 | P1.4 灰度放量百分比（1–100）：对 `工号+session_id` sha256 前 8 位十六进制 % 100 做确定性分桶，同请求恒同桶；超桶 → 403 `gray_percent_exceeded`（PRD 5.5.6） |
| `MIN_CLIENT_VERSION` | 未设 = 不校验 | 最低客户端版本；`X-Client-Version` 低于它 → 403 `client_version_too_low`（强制升级，优先于灰度放量判定） |

审计留存（PRD 10 ≥180 天）：Redis 模式下热窗 `audit:events`（截断 5000 条）+ 全量归档
`audit:archive`（滑动 TTL 180 天）；内存模式仅本地冒烟用。

## P1.4 小流量灰度（PRD 5.5.6 / 5.5.2 / 16.4）

### 服务端门禁（agent-core）

- **灰度放量**：`GRAY_PERCENT` 调至 1–100 后启用；分桶键 `工号+session_id` sha256 前 8 位
  十六进制 % 100 < GRAY_PERCENT 放行，否则 403 `gray_percent_exceeded`。未设/0 = 全放行。
- **强制升级**：`MIN_CLIENT_VERSION` 设定后，`X-Client-Version` 缺失或低于该版本一律 403
  `client_version_too_low`，且**优先于**灰度放量判定。
- **灰度观测**：`GET /metrics/gray`（仅 dept_manager，PRD 16.4）从审计存储聚合对话数 /
  活跃用户 / 确认卡修改占比 / 写入失败分布，放量前后对比用。

### 桌面端更新通道（electron-updater）

桌面端环境变量（打包时注入或启动前设置）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `UPDATER_CHANNEL` | `stable` | 更新通道：`stable` / `beta`，决定 feed 目录 |
| `UPDATE_SERVER_URL` | `http://localhost:8090` | 更新源地址，feed 为 `{URL}/releases/desktop/{channel}/latest.yml` |

- 渲染层每次请求自动附加 `X-Client-Version`（取自 `app.getVersion()`）；
  收到 `client_version_too_low` 弹**不可关闭的强制升级 Modal**，走
  updater:check → updater:download → updater:install 三步；`gray_percent_exceeded`
  则提示「暂未在小流量范围」。
- 开发态（`app.isPackaged=false`）自动跳过更新检查，不崩溃。

### update-server（nginx:1.27-alpine，:8090）

- 挂载 `releases/desktop` 只读，`nginx/update-server.conf` 对 `latest.yml` 强制
  `Cache-Control: no-cache`（客户端必须每次回源拿新版本），`GET /healthz` 探活。

### 发版与灰度运维

```bash
# 1. 打包桌面端（stable 或 beta）
cd apps/desktop && pnpm dist            # 产物 .exe + latest.yml

# 2. 上传到对应通道目录（版本号递增）
#    releases/desktop/stable/ChatWork-Setup-0.x.y.exe + latest.yml
#    releases/desktop/beta/ ...

# 3. 灰度放量：改 compose 环境 GRAY_PERCENT 后重启 agent-core
docker compose up -d agent-core         # GRAY_PERCENT: 0 → 5 → 10（第 13 周目标）

# 4. 强制升级（发现严重缺陷时）：MIN_CLIENT_VERSION 设为最新版号后重启
#    旧客户端下次请求即 403，弹出强制升级 Modal

# 5. 观测：dept_manager 登录后调 GET /api/metrics/gray 对比放量前后指标
curl http://localhost:8090/healthz      # 更新源探活
```

## 冒烟验证（容器栈）

```bash
# 1. 网关 → 应用连通（401 即通：SSO_REQUIRED=true 拦下匿名请求）
curl -i http://localhost/api/chat -X POST -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","user_id":"E1001","message":"hi"}'

# 2. 桌面端视角走完整 OIDC：浏览器开 http://localhost:8081/realms/chat-work/account
#    登录 E1001 后，按 client chat-work-desktop + PKCE 走 authorize→code→token，
#    用 access token 复跑 services/agent_core/scripts/e2e_p1.py（SSO_ISSUER/端口指 8081/80）
```

## vLLM（GPU 节点，可选）

```bash
docker compose --profile gpu up -d vllm   # 需 nvidia-container-toolkit
```

镜像与模型路径按内网制品仓替换 `vllm/vllm-openai:latest` 与 `MODEL=/models/qwen` 占位；
GPU 资源挂载见 compose 内注释块。PRD 14 待确认项落实后回填本节。

## 生产化待办（PRD 14 章跟踪）

1. 所有占位凭证（Keycloak admin / MySQL app、keycloak 账号 / MinIO）改密并走 `.env` / 配置中心注入
2. APISIX 前置 TLS 终结（443）与域名；`sslRequired` 由 `none` 收紧
3. Keycloak `start-dev` → `start`（生产模式 + 正式 hostname）；redirectUris 随桌面端端口定稿收窄
4. OA / BI 真实 OpenAPI 地址与 Service Account（替换 `OA_MODE=mock`、`OA_BASE_URL`、`BI_BASE_URL` 占位）
5. ~~桌面端更新服务器~~ **已完成（P1.4）**：compose `update-server` 转正 + `releases/desktop/{stable,beta}/`，见「P1.4 小流量灰度」章节
6. 审计归档 180 天后的冷存对接（日志平台）
