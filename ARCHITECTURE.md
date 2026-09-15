# Chat-Work Agent 技术架构规划

> 配套文档：[PRD-chat-work-agent.md](./PRD-chat-work-agent.md)（v1.7）｜ 交互原型：[prototype.html](./prototype.html)
>
> 技术基调：**Python（服务端） + TypeScript（客户端） + Electron（桌面端，MVP 唯一客户端，无独立 Web 应用）**

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.0 | 2026-09-15 | 初版：总体架构、Monorepo 工程结构、模块设计、协议矩阵、关键数据流、共享契约、部署架构、工程化规范 |
| v1.1 | 2026-09-15 | 响应"仅保留桌面端"决策：移除独立 Web 应用（apps/web 删除，renderer 并入 desktop 结构）；架构图/设计原则/技术栈/协议矩阵/部署（update-server MVP 起）/测试（Playwright Electron）联动更新；对应 PRD v1.7 |

---

## 1. 架构总览

### 1.1 分层架构图

```
┌────────────────────── 客户端层（TypeScript · Electron，唯一客户端）──────────┐
│  Desktop App（Electron 33+：主进程 / preload / renderer）                     │
│    renderer（React 18 + AntD）── HTTPS / WSS（SSE 流式）                      │
│    主进程 utilityProcess ── stdio ──► local-mcp（本地文件工具，随端分发）      │
└─────────┬─────────────────────────────────────────────────────────────────────┘
          ▼
┌───────────────────── 接入层 ─────────────────────────────────────────────────┐
│  Gateway（APISIX）：路由 / JWT 验签(RS256 公钥本地) / 限流 / 审计日志          │
│  Keycloak（Identity Broker）：AD / 企微 / 钉钉 → OIDC 收敛（PRD 8.5）        │
└─────────┬────────────────────────────────────────────────────────────────────┘
          ▼
┌───────────────────── 应用层（Python 3.12）───────────────────────────────────┐
│  agent-core（FastAPI + LangGraph）                                            │
│  ├─ Agent 流水线：意图识别 → 技能/工具路由 → 参数提取 → Schema校验              │
│  │   → 权限校验 → HITL 确认卡 → 执行 → 格式化（PRD 4.1）                       │
│  ├─ Skill Registry（技能注册/匹配）+ LangGraph SubGraph 编排（PRD 3.2）       │
│  ├─ Automation Scheduler（定时/事件触发，APScheduler + Redis 锁，PRD 3.6）    │
│  ├─ Memory Service（L1-L4 记忆，PRD 9.1） ──► Milvus / Redis                  │
│  ├─ Knowledge Service（RAG 切片/检索/注入，PRD 9.5）──► Milvus                │
│  └─ Guardrail：确认卡管理 / 幂等键管理 / 审计埋点（PRD 8.7 / 10）              │
└─────────┬────────────────────────────────────────────────────────────────────┘
          │ MCP（Streamable HTTP，MVP 用 SSE）
          ▼
┌───────────────────── 集成层（Python，每系统独立进程）────────────────────────┐
│  mcp-oa（OA 封装）   mcp-bi（BI 封装）   mcp-crm / mcp-erp / …（Phase 2）    │
│  统一职责：inputSchema 校验 / 枚举值对齐 / Service Account / 幂等记录 / 熔断   │
└─────────┬────────────────────────────────────────────────────────────────────┘
          │ HTTPS OpenAPI / JDBC / RPA / 文件（PRD 8.2）
          ▼
   业务系统：OA │ BI │ CRM │ ERP │ WMS │ MES │ U8

┌───────────────────── 基础设施层 ─────────────────────────────────────────────┐
│  PostgreSQL 16（业务/审计/幂等记录）  Redis 7（会话/权限缓存/黑名单/锁）       │
│  Milvus 2.4（记忆向量 + 知识向量）    vLLM（私有化 LLM 推理，A10×2）          │
│  MinIO（产物/附件对象存储）           内网更新服务器（桌面端 electron-updater） │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 核心设计原则

| # | 原则 | 落地方式 |
|---|------|---------|
| 1 | **语言边界清晰** | Python 只做服务端（Agent 逻辑/LLM/MCP 集成）；TS 只做客户端（Desktop/共享类型）；不跨界 |
| 2 | **契约先行** | 所有跨进程接口 = 机器可读契约（OpenAPI / JSON Schema / MCP 协议），双语言从契约生成代码 |
| 3 | **单一桌面客户端** | MVP 起仅 Electron 桌面端（无独立 Web 应用）；渲染层 React 与 Electron 壳分离（PRD 5.5.2），未来如需 Web 版可直接复用 renderer |
| 4 | **MCP 是唯一集成协议** | Agent Core 只通过 MCP 调工具；本地文件操作也实现为 MCP Server（stdio） |
| 5 | **安全左移** | JWT 验签在网关、HITL 在流水线、幂等在 MCP Server、审计贯穿三层 |

---

## 2. 技术栈与版本基线

| 分层 | 技术 | 版本基线 | 说明 |
|------|------|---------|------|
| **Python 服务端** | Python | 3.12 | 全部服务统一版本 |
| | FastAPI | 0.115+ | agent-core HTTP 框架 |
| | Pydantic | v2 | 配置/参数模型，JSON Schema 直接生成 |
| | LangGraph | 0.2+ | 流水线与技能编排（SubGraph） |
| | mcp（官方 Python SDK） | 1.x | MCP Server 实现 |
| | httpx | — | 异步 HTTP 客户端（调业务系统/LLM） |
| | SQLAlchemy 2 + asyncpg | — | PostgreSQL 异步访问 |
| | APScheduler + redis-lock | — | 自动化任务调度（Phase 2） |
| | uv | — | 依赖与虚拟环境管理 |
| | ruff + mypy + pytest | — | Lint / 类型检查 / 测试 |
| **TS 客户端** | Node | 22 LTS | 构建与 Electron 运行时 |
| | TypeScript | 5.6+（strict） | 全前端 |
| | React + Ant Design | 18 / 5.x | renderer UI |
| | TanStack Query + Zustand | — | 服务端状态 / 客户端状态 |
| | pnpm workspace | 9+ | Monorepo |
| | electron-vite + electron-builder | 33+ / 26+ | 桌面端构建与打包 |
| | Biome（或 ESLint+Prettier）+ Vitest + Playwright | — | Lint / 单测 / E2E |
| **基础设施** | PostgreSQL / Redis / Milvus | 16 / 7 / 2.4 | 见 1.1 |
| | Keycloak | 26（Quarkus） | Identity Broker |
| | APISIX | 3.x | 网关 |
| | vLLM | 最新稳定 | OpenAI 兼容 API，跑 Qwen/GLM 级私有模型 |
| | Docker Compose → K8s | — | MVP 单机 → Phase 2 集群 |

> **共享契约包** `@chat-work/protocol`：MCP 工具 JSON Schema（双语言读取）+ TS 类型；`@chat-work/api-client`：由 FastAPI OpenAPI 自动生成的 TS SDK。

---

## 3. 仓库与工程结构（Monorepo）

单一仓库，pnpm workspace 管 TS、uv 管 Python，CI 按路径触发：

```
chat-work/
├── apps/                                # TS 应用（pnpm workspace）
│   └── desktop/                         # Electron 桌面端（唯一客户端，electron-vite）
│       ├── main/                        # 主进程：窗口/托盘/快捷键/更新/IPC
│       ├── preload/                     # contextBridge 白名单桥（PRD 5.5.3）
│       ├── renderer/                    # React 应用（页面与业务卡片，引用 packages/ui）
│       └── local-mcp/                   # 本地 MCP Server（stdio，Node utilityProcess）
├── services/                            # Python 服务（各自 uv 项目）
│   ├── agent_core/
│   │   ├── agent_core/
│   │   │   ├── api/                     # FastAPI 路由（对话 SSE/确认卡/技能/记忆）
│   │   │   ├── pipeline/                # 流水线阶段：intent/route/extract/validate/execute
│   │   │   ├── skills/                  # Skill Registry + LangGraph 编排 + 表单 Schema
│   │   │   ├── memory/                  # L1-L4 记忆读写与检索注入
│   │   │   ├── knowledge/               # RAG：入库/切片/检索/注入
│   │   │   ├── guardrail/               # HITL 状态机 / 幂等键 / 权限缓存
│   │   │   ├── automation/              # 调度器 + 推送（Phase 2）
│   │   │   └── mcp_client/              # MCP Client（多 Server 连接池/能力协商）
│   │   └── tests/
│   ├── mcp_oa/                          # OA MCP Server（tools：oa__*）
│   ├── mcp_bi/                          # BI MCP Server（tools：bi__*）
│   └── mcp_local/  ← 不放这里（在 apps/desktop/local-mcp，TS 实现，随端分发）
├── packages/                            # TS 共享包
│   ├── protocol/                        # ★ 契约源：MCP 工具 JSON Schema（.json）
│   ├── api-client/                      # openapi-typescript 生成的 SDK + 请求封装
│   ├── ui/                              # 聊天卡片组件库（确认卡/审批卡/BI 卡…）
│   └── shared/                          # 纯工具（时间/金额格式化、枚举）
├── infra/
│   ├── compose/                         # MVP docker-compose（全家桶）
│   ├── k8s/                             # Phase 2 manifests/Helm
│   ├── keycloak/                        # realm 导入配置（IdP 桥接）
│   ├── apisix/                          # 路由/JWT 验签插件配置
│   └── llm/                             # vLLM 部署与模型配置
├── evals/                               # 评估集（PRD 16 章）：用例 YAML + 评测脚本（Python）
├── docs/                                # PRD / 架构 / ADR 决策记录
├── pnpm-workspace.yaml
└── README.md
```

**边界规则：**

| 规则 | 说明 |
|------|------|
| `apps/*` 不写业务逻辑 | 业务在 `services/`，前端只做交互与展示 |
| `packages/ui` 是唯一 UI 重复消除点 | 确认卡等业务卡片组件在这里，桌面 renderer 使用（未来 Web 版可直接复用） |
| `services/agent_core` 不得 import 各 mcp_* 内部代码 | 只能走 MCP 协议，防止隐式耦合 |
| `packages/protocol/*.json` 是唯一工具 Schema 源 | Python（pydantic 校验加载）与 TS（类型生成）都从这里取 |
| 契约变更 = PR 必须双端同步 | CI 校验 JSON Schema 与两端代码一致性 |

---

## 4. 核心模块设计

### 4.1 agent-core（Python / FastAPI / LangGraph）

**进程模型：** 单进程多 Worker（uvicorn），异步 IO 为主；LLM 调用与 MCP 调用全异步并发。

**流水线实现（LangGraph StateGraph）：**

```python
# 概念代码：pipeline/graph.py
class ChatState(TypedDict):
    auth: AuthContext           # 网关透传的 JWT 解析结果（工号/科室/权限版本）
    session_id: str
    messages: list[Message]
    intent: Intent | None       # 阶段1输出
    skill: SkillSpec | None     # 阶段2输出（技能优先，未命中降级工具）
    draft: FormDraft | None     # 阶段3输出：参数草稿 + 缺失必填列表
    validation: ValidationResult | None   # 阶段4：JSON Schema + 枚举对齐
    confirm_token: str | None   # 阶段5：HITL 待确认状态
    tool_result: ToolResult | None        # 阶段6输出
    final: ResponseMessage | None         # 阶段7：卡片/文本

graph = StateGraph(ChatState)
graph.add_node("intent", intent_node)        # 小模型路由（DeepSeek 级）
graph.add_node("route", route_node)          # Skill Registry 匹配 → 降级 Tool Registry
graph.add_node("extract", extract_node)      # LLM 抽参 + 记忆/知识注入（RAG）
graph.add_node("validate", validate_node)    # pydantic + protocol schema
graph.add_node("hitl", hitl_node)            # 写入类→挂起等确认；询问类→直接出追问
graph.add_node("execute", execute_node)      # MCP tools/call（带幂等键）
graph.add_node("format", format_node)        # 渲染卡片 JSON（前端按类型渲染）
```

**关键机制：**

| 机制 | 实现 |
|------|------|
| HITL 挂起/恢复 | 确认状态存 Redis（`confirm:{token}`，TTL 10 分钟）：表单卡内容 + 草稿版本 + 幂等键前缀；用户点确认 → 恢复 graph 继续执行（LangGraph checkpoint） |
| 幂等键 | `hitl` 通过后生成 `{userId}_{sessionId}_{intentHash}_{draftVersion}`，随 `tools/call` 传 `_idempotencyKey`（PRD 8.7） |
| 记忆注入 | `extract` 前置钩子：L2 个人记忆（Milvus 检索 Top-3，≤300 Token）+ 技能绑定知识（≤800 Token） |
| 流式输出 | SSE 推送阶段事件（`stage_progress`/`draft_card`/`confirm_card`/`final`），前端按事件渲染"思考与执行"折叠块 |
| 工具能力协商 | 会话建立时带客户端能力（桌面端 + local-file；Phase 3 小程序无本地工具），MCP Client 只向 LLM 暴露可用工具集（PRD 5.5.5） |
| 权限 | `route` 前用 `perm_ver` 查 Redis 权限缓存，过滤技能/工具白名单（PRD 8.5.4） |

### 4.2 mcp-servers（Python / 官方 MCP SDK）

每个系统一个独立 uv 项目、独立进程、独立 Service Account：

```
mcp_oa/
├── server.py                 # MCP Server 入口（Streamable HTTP）
├── tools/
│   ├── customers.py          # oa__query_customers / oa__get_customer_defaults
│   ├── sales_order.py        # oa__create_sales_order（幂等写入）
│   └── approvals.py          # oa__query_pending_approvals / oa__approve
├── schemas/                  # 从 packages/protocol 同步的 JSON Schema（CI 校验）
├── adapters/                 # OA OpenAPI 客户端（httpx，熔断+重试）
└── idempotency.py            # 幂等记录表（PG）
```

**统一职责（所有 mcp-* 一致）：**

1. `inputSchema` 校验（pydantic，加载 protocol JSON）
2. 枚举值对齐（产品编码/客户名先调系统查询接口验证存在）
3. Service Account 调业务系统 + `代理人` 双标记（PRD 8.5.5）
4. 写入幂等（PG 幂等表，保留 24h）
5. 健康探测 + tools 热注册（Agent Core 启动/定时 `tools/list` 刷新）

### 4.3 renderer（桌面端渲染层，TS / React）

```
apps/desktop/renderer/src/
├── chat/          # 会话流：消息渲染、思考折叠块、SSE 消费
├── cards/         # 业务卡片渲染器（确认卡/成功卡/审批卡/BI卡）——来自 packages/ui
├── skills/        # 我的技能、技能市场（Phase 2）
├── approval/      # 待办审批视图
├── knowledge/     # 知识库管理（Phase 2）
└── lib/api/       # @chat-work/api-client 封装 + SSE 解析
```

- **SSE 消费**：`fetch` + ReadableStream 解析（不用 EventSource，需带 JWT header）
- **状态**：TanStack Query（服务端缓存）+ Zustand（UI 态），不引入 Redux

### 4.4 desktop（Electron / TS）+ local-mcp

```
apps/desktop/
├── main/
│   ├── index.ts            # 窗口/托盘/Quick Ask/自动更新
│   ├── ipc.ts              # 白名单 IPC（枚举式注册 + zod 校验，PRD 5.5.3）
│   ├── auth.ts             # Loopback 登录（RFC 8252，PRD 8.5.8）+ OS 密钥链
│   └── local-mcp-spawn.ts  # utilityProcess 拉起 local-mcp 子进程
├── preload/bridge.ts       # contextBridge：仅暴露 selectFile/notify/saveAs…
├── renderer/               # React 渲染层（见 4.3）
└── local-mcp/
    ├── server.ts           # MCP stdio Server（@modelcontextprotocol/sdk）
    └── tools.ts            # local__select_file/read/write/list_dir（授权目录+扩展名白名单）
```

**类型共享**：`renderer` 复用 `packages/ui` 的业务卡片组件；`local-mcp` 的工具 Schema 同样来自 `packages/protocol`（local__*.json）。

### 4.5 身份与网关

| 组件 | 职责 |
|------|------|
| Keycloak | Identity Broker：AD/企微/钉钉 → OIDC；JWT RS256 签发；realm 配置纳入 `infra/keycloak`（版本化） |
| APISIX | 路由 `/api/*` → agent-core、`/mcp/*` → 各 mcp server；插件：jwt-verify（本地公钥验签）、limit-count、audit-log（Kafka/PG 落审计） |

### 4.6 数据层

| 存储 | 用途 | 关键表/集合 |
|------|------|------------|
| PostgreSQL | 业务单据草稿、确认状态持久化、幂等记录、审计日志、技能/自动化元数据 | `confirmations`、`idempotency_records`、`audit_logs`、`skills`、`automation_tasks` |
| Redis | L1 会话记忆（TTL 7d）、权限缓存（perm_ver 键）、JWT jti 黑名单、调度分布式锁 | — |
| Milvus | L2/L3/L4 记忆向量 + 知识库向量（分 Collection 隔离） | `mem_personal_{org}`、`mem_dept_{dept}`、`kb_corp`、`kb_dept_{dept}` |
| MinIO | 任务产物（周报/报表导出）、附件 | — |

---

## 5. 通信协议矩阵

| 通道 | 协议 | 数据格式 | 备注 |
|------|------|---------|------|
| Desktop → Gateway → agent-core | HTTPS REST + **SSE**（流式） | JSON（OpenAPI 契约） | 确认卡提交走 REST `POST /confirmations/{token}` |
| agent-core → mcp-* | **MCP over Streamable HTTP**（MVP：SSE） | MCP JSON-RPC | 每系统独立 endpoint；`X-Chat-Auth` 透传 JWT |
| Desktop main → local-mcp | **MCP over stdio** | MCP JSON-RPC | utilityProcess，无网络 |
| Desktop renderer → main | IPC（contextBridge） | zod 校验的 JSON | 白名单枚举通道 |
| agent-core → LLM | OpenAI 兼容 API（vLLM） | — | 小模型/大模型双路由 |
| agent-core/desktop → 推送 | 企微 webhook / WebSocket（桌面通知） | — | Phase 2 |
| Keycloak ↔ 各端 | OIDC（Auth Code + PKCE / Loopback） | JWT | PRD 8.5.2 / 8.5.8 |

---

## 6. 关键数据流：销售订单录入（端到端）

```
用户          agent-core                mcp-oa            OA系统
 │ "录订单5G模块500个" │                     │                 │
 │──HTTPS POST /chat──▶│                     │                 │
 │                     │ ①意图+技能路由(小模型) │                 │
 │                     │ ②oa__query_customers │                 │
 │                     │───MCP call─────────▶│──OpenAPI───────▶│
 │                     │◀──客户+默认值────────│◀────────────────│
 │◀─SSE: 思考块+追问"交货日期?"                │                 │
 │ "10月15号"           │                     │                 │
 │──HTTPS POST /chat──▶│ ③补参→Schema校验→权限 │                 │
 │◀─SSE: 确认卡(草稿v2+confirm_token)         │                 │
 │ [点击"确认提交"]      │                     │                 │
 │─POST /confirmations/tok─▶ ④验token→生成幂等键│                │
 │                     │ ⑤oa__create_sales_order(+幂等键)         │
 │                     │───MCP call─────────▶│──查幂等表→写入──▶│
 │                     │◀──单据号SO-xxx──────│◀────────────────│
 │◀─SSE: 成功卡(单号/审批流/审计标记)          │                 │
 │                     │ ⑥异步: 审计日志 + L2记忆(常用客户+1)      │
```

异常分支：⑤ 超时 → 同幂等键调 `oa__get_idempotency_result` → 有结果返回单号 / 无记录查当日同参单据 → 均未知则提示用户勿重复提交（PRD 8.7 流程）。

---

## 7. 共享契约设计（protocol 包，双语言之源）

```
packages/protocol/
├── tools/
│   ├── oa__create_sales_order.json   # MCP inputSchema + annotations(_meta: hitl/idempotency/requiredPermissions)
│   ├── oa__query_customers.json
│   ├── bi__execute_query.json
│   └── local__write_file.json
├── events/                           # SSE 事件类型定义（stage_progress/draft_card/confirm_card/final）
└── package.json                      # TS 类型生成（json-schema-to-typescript）
```

- **Python 侧**：mcp server 加载 JSON 注册工具 + pydantic 运行时校验；agent_core 用同一 JSON 构造给 LLM 的 tool 定义
- **TS 侧**：生成工具参数类型；`events/` 生成 SSE 事件的判别联合（discriminated union）
- **CI 门禁**：schema 变更 → 强制触发 Python+TS 两端契约测试；`annotations` 必须含 `_meta.humanConfirmation` 与 `_meta.idempotencyRequired`（写入类），否则拒绝合并

---

## 8. 部署架构

### 8.1 MVP（Docker Compose，单机起步）

```yaml
# infra/compose/docker-compose.yml（服务清单）
services:
  apisix:        # 网关（80/443）
  keycloak:      # + PostgreSQL（keycloak 专用 schema）
  agent-core:    # 2 副本（uvicorn worker=2）
  mcp-oa:  mcp-bi:
  postgres: redis: milvus:  minio:
  vllm:          # A10×2，OpenAI 兼容 :8000
  update-server: # 桌面端 electron-updater feed（MVP 起）
```

### 8.2 Phase 2（K8s）

| 组件 | 形态 | 扩缩策略 |
|------|------|---------|
| agent-core | Deployment | HPA（CPU + 活跃 SSE 连接数） |
| mcp-* | Deployment（每系统独立） | 按需 2+ 副本；熔断摘除 |
| vLLM | Deployment（GPU 节点池） | 固定副本（2×A10），队列化推理 |
| Milvus | Operator 部署 | 独立存储 |
| 桌面端 | 企微/官网内网分发 + 内网 update-server 灰度通道 | — |

---

## 9. 工程化规范

### 9.1 代码规范

| 项 | Python | TypeScript |
|----|--------|-----------|
| Lint/格式 | ruff（含 isort） | Biome（lint+format 一体） |
| 类型 | mypy strict（核心模块） | tsconfig strict 全开 |
| 提交 | Conventional Commits | 同左 |
| 命名 | 工具名 `oa__verb_resource`（PRD 8.3） | 组件 PascalCase |

### 9.2 测试策略

| 层级 | 工具 | 覆盖目标 |
|------|------|---------|
| Python 单测 | pytest | 流水线各 node、幂等、权限过滤 |
| Python 契约测 | pytest | mcp tools 与 protocol JSON 一致、schema 校验用例 |
| TS 单测 | Vitest | 卡片渲染器（确认卡字段来源徽标）、SSE 解析 |
| E2E（桌面端） | Playwright（Electron） | 三个 MVP 场景脚本化（对照 PRD 5.2） |
| 评估集 | `evals/`（YAML 用例 + Python 评分脚本） | PRD 16.2 九项指标，回归门禁 |
| 桌面端 | Vitest（IPC handler）+ 打包冒烟 | 白名单 IPC、local-mcp 授权目录边界 |

### 9.3 CI/CD（内网 GitLab）

```
路径触发：
  services/**  → uv sync → ruff/mypy → pytest → 镜像构建 → 部署 dev
  apps/**,packages/**  → pnpm i → Biome → Vitest → Playwright(dev 环境)
  apps/desktop  → + electron-builder 三平台包 → 签名 → 内网制品库
  packages/protocol/**  → 双端契约测试 + 变更标签（minor/major 提醒）
环境：dev（日常）→ staging（评估集回归）→ prod（灰度通道，PRD 16.4）
```

### 9.4 可观测性

- **Metrics**：Prometheus（各服务埋点：流水线阶段耗时、工具调用成功率、LLM Token 消耗、SSE 并发数）
- **Tracing**：OpenTelemetry，trace_id 贯穿「前端 → agent-core → mcp → 业务系统」，随 SSE 事件回传前端展示（对话可复制 trace 用于排障）
- **日志**：结构化 JSON（含工号/会话ID/trace_id），ELK 或 Loki
- **业务观测**：确认卡修改率、幂等命中率、审批处理时长（Grafana 大盘，对应 PRD 16.4 指标）

---

## 10. 与 PRD 的映射检查表

| PRD 章节 | 架构落点 |
|---------|---------|
| 3.2 技能体系 | agent_core/skills（Registry + LangGraph SubGraph） |
| 3.6 自动化 | agent_core/automation（APScheduler + 推送，Phase 2） |
| 4.1 Agent Core 流水线 | agent_core/pipeline（LangGraph StateGraph） |
| 8.3 MCP 规范 | services/mcp_* + packages/protocol |
| 8.5 SSO | Keycloak + APISIX jwt 插件 + desktop/main/auth.ts（Loopback） |
| 8.7 幂等 | mcp-* 的 idempotency 模块（PG 表）+ agent_core/guardrail |
| 9 记忆/知识库 | agent_core/memory + knowledge + Milvus/Redis |
| 5.5 桌面端 | apps/desktop + local-mcp（TS/stdio） |
| 16 评估 | evals/ 目录 + CI 回归门禁 |
