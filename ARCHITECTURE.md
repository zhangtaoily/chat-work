# Chat-Work Agent 技术架构规划

> 配套文档：[PRD-chat-work-agent.md](./PRD-chat-work-agent.md)（v1.7.2）｜ 交互原型：[prototype.html](./prototype.html)
>
> 技术基调：**Python（服务端） + TypeScript（客户端） + Electron（桌面端，MVP 唯一客户端，无独立 Web 应用）**

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.0 | 2026-09-15 | 初版：总体架构、Monorepo 工程结构、模块设计、协议矩阵、关键数据流、共享契约、部署架构、工程化规范 |
| v1.1 | 2026-09-15 | 响应"仅保留桌面端"决策：移除独立 Web 应用（apps/web 删除，renderer 并入 desktop 结构）；架构图/设计原则/技术栈/协议矩阵/部署（update-server MVP 起）/测试（Playwright Electron）联动更新；对应 PRD v1.7 |
| v1.2 | 2026-09-16 | 对应 PRD v1.7.2 三处原型演示态的架构深化：新增 4.7 技能市场上架与安全评审（上架状态机/沙箱试运行/SLA 通道）、4.8 自动化任务（调度器/试运行 HITL/防护栏/执行身份）、6.2 ERP 复杂单据录入数据流（三级表单模型/物料歧义候选/全屏工作台/整单幂等）；4.2 展开 Phase 2 首批 mcp-crm / mcp-erp 模块设计；数据层/契约包/映射表联动更新 |
| v1.3 | 2026-09-16 | 吸收 OpenClaw 三点设计：4.1 关键机制新增**会话串行化车道队列**（同会话请求/确认提交/自动化推送串行执行，消除草稿竞态，借鉴 Lane Queue）；新增 4.9 记忆离线固化与可迁移（借鉴 Dreaming：低峰期小模型聚合会话提炼 L2 候选 + 敏感字段前置过滤 + 衰减对齐；借鉴 MEMORY.md：L2 记忆 Markdown 导出/导入，对应 PRD 9.2 可感知性） |

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
│  ├─ Skill Registry + Market（注册/匹配/上架评审，PRD 3.2/3.4/3.5）            │
│  ├─ Automation（调度/试运行 HITL/推送，APScheduler + Redis 锁，PRD 3.6）      │
│  ├─ Memory Service（L1-L4 记忆，PRD 9.1） ──► Milvus / Redis                  │
│  ├─ Knowledge Service（RAG 切片/检索/注入，PRD 9.5）──► Milvus                │
│  └─ Guardrail：确认卡管理 / 幂等键管理 / 审计埋点（PRD 8.7 / 10）              │
└─────────┬────────────────────────────────────────────────────────────────────┘
          │ MCP（Streamable HTTP，MVP 用 SSE）
          ▼
┌───────────────────── 集成层（Python，每系统独立进程）────────────────────────┐
│  mcp-oa（OA）  mcp-bi（BI）  mcp-crm / mcp-erp / mcp-wms（Phase 2 首批接入） │
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
│   │   │   ├── skills/                  # Skill Registry + Market（上架评审，见 4.7）
│   │   │   ├── memory/                  # L1-L4 记忆读写与检索注入
│   │   │   ├── knowledge/               # RAG：入库/切片/检索/注入
│   │   │   ├── guardrail/               # HITL 状态机 / 幂等键 / 权限缓存
│   │   │   ├── automation/              # 调度/试运行 HITL/推送/模板（Phase 2，见 4.8）
│   │   │   └── mcp_client/              # MCP Client（多 Server 连接池/能力协商）
│   │   └── tests/
│   ├── mcp_oa/                          # OA MCP Server（tools：oa__*）
│   ├── mcp_bi/                          # BI MCP Server（tools：bi__*）
│   ├── mcp_crm/                         # CRM MCP Server（tools：crm__*，Phase 2）
│   ├── mcp_erp/                         # ERP MCP Server（tools：erp__*，Phase 2）
│   ├── mcp_wms/                         # WMS MCP Server（tools：wms__*，Phase 2）
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
| 会话串行化（车道队列） | 每会话一条 FIFO 队列（Redis `session_lane:{sessionId}`）：对话请求、确认卡提交、自动化试运行推送**同会话排队串行执行**，消除草稿状态与 confirm token 的并发竞态（借鉴 OpenClaw Lane Queue） |
| 工具能力协商 | 会话建立时带客户端能力（桌面端 + local-file；Phase 3 小程序无本地工具），MCP Client 只向 LLM 暴露可用工具集（PRD 5.5.5） |
| 权限 | `route` 前用 `perm_ver` 查 Redis 权限缓存，过滤技能/工具白名单（PRD 8.5.4） |

**复杂单据扩展（Phase 2 ERP，见 6.2 数据流）：** `FormDraft` 支持三级嵌套结构——`header`（主表字段 + **分组元数据** `groups`，驱动工作台分组折叠）、`lines[]`（子表行，每行 `material_ref` 指向已选物料候选）、`batches`（孙表，不随草稿加载，钻取时按行拉取）。**分步抽参**：主表上百字段不整体进 LLM 上下文——Schema 按分组裁剪，LLM 只处理对话提及的字段，其余分组在工作台标记"待补"。每个字段携带来源标记 `source`（`default` / `memory` / `ask` / `computed`），渲染为字段来源徽标；用户修改字段 → 草稿版本 +1 → 生成 `diff_card`（旧值删除线 → 新值，金额类字段联动重算）并产生新幂等键。

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

**Phase 2 首批接入（PRD 6.1）：**

```
mcp_crm/                                # 三大核心系统之三：销售下单/跟单（OpenAPI）
├── tools/
│   ├── contracts.py        # crm__query_contracts（跟单：订单进度/到期/回款条件）
│   ├── receivables.py      # crm__query_receivables（回款情况，只读）
│   └── remark.py           # crm__update_contract_remark（写入类：跟单备注）
├── adapters/               # CRM OpenAPI 客户端
└── idempotency.py

mcp_erp/                                # ERP（OpenAPI + RPA 补充，PRD 6.1）
├── tools/
│   ├── materials.py        # erp__search_materials（★ 物料模糊检索 Top-N：
│   │                        #   编码/名称/规格/库存/协议价，歧义候选选择卡数据源；
│   │                        #   分页游标，绝不全量返回上万物料）
│   ├── batches.py          # erp__get_batches（孙表批次懒加载：按子表行 ID 拉取）
│   └── sales_order.py      # erp__create_sales_order（复杂单据：主/子/孙三级
│                            #   整单写入，单事务边界 + 整单幂等键，见 6.2）
├── adapters/
│   ├── openapi.py          # ERP OpenAPI 客户端（主通道）
│   └── rpa_bridge.py       # RPA 补充通道（无 API 的老旧单据入口，PRD 8.2）
└── idempotency.py          # 幂等记录含三级行数/金额快照，对账用

mcp_wms/  # 入库/出库单查询、库存预警（OpenAPI，只读为主）
```

> **CRM 工具读写约束**（对应原型上架向导的评审规则）：`crm__query_*` 只读工具配合 Ask 模式可走快审通道；`crm__update_*` 写入工具必须走完整安全评审（见 4.7），且调用时强制 HITL 确认。

> **ERP 物料歧义原则**（对应原型物料候选选择卡）：上万种物料中 Agent **只做检索与呈现，不猜测**——`erp__search_materials` 返回 Top-N 候选（编码/规格/库存/协议价），由用户必选其一；选定后协议价等默认值由服务端带出，Agent 不得代填。

### 4.3 renderer（桌面端渲染层，TS / React）

```
apps/desktop/renderer/src/
├── chat/          # 会话流：消息渲染、思考折叠块、SSE 消费
├── cards/         # 业务卡片渲染器（来自 packages/ui，按 SSE 事件类型分发）
│   ├── confirm/       # 确认卡（字段来源徽标：客户默认/追问/记忆恢复/自动计算）
│   ├── material/      # 物料歧义候选选择卡（Top-N 必选不猜，ERP，Phase 2）
│   ├── summary/       # 单据摘要卡（行数/金额/异常警示）+ diff 变更卡（旧值删除线→新值）
│   ├── workbench/    # 全屏单据工作台（主表分组折叠 + 子表虚拟滚动
│   │                  #   + 仅看异常行过滤 + 孙表批次懒加载钻取，Phase 2）
│   ├── skillpub/      # 技能上架向导（信息→评审单→成功三视图，Phase 2）
│   └── autowiz/       # 自动化新建向导（来源→配置→试运行→启用四视图，Phase 2）
├── skills/        # 我的技能、技能市场（含「评审中」状态卡，Phase 2）
├── automation/    # 自动化任务：任务列表/模板库/执行历史（Phase 2）
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
| PostgreSQL | 业务单据草稿（含复杂单据三级结构）、确认状态持久化、幂等记录、审计日志、技能/自动化/评审元数据 | `doc_drafts`、`confirmations`、`idempotency_records`、`audit_logs`、`skills`、`skill_versions`、`skill_reviews`、`automation_tasks`、`automation_runs` |
| Redis | L1 会话记忆（TTL 7d）、权限缓存（perm_ver 键）、JWT jti 黑名单、调度分布式锁、自动化并发信号量、会话车道队列（`session_lane:{sessionId}`） | — |
| Milvus | L2/L3/L4 记忆向量 + 知识库向量（分 Collection 隔离） | `mem_personal_{org}`、`mem_dept_{dept}`、`kb_corp`、`kb_dept_{dept}` |
| MinIO | 任务产物（周报/报表导出/自动化执行产物）、附件 | — |

### 4.7 技能市场上架与安全评审（Phase 2，PRD 3.4 / 3.5）

**上架状态机（agent_core/skills/review.py）：**

```
draft（科室管理员低代码搭建：表单拖拽 + 工具编排）
  → submitted（三步向导提交：基本信息/发布范围/执行模式
  │            + 工具与数据范围声明，含只读/写入徽标）
  → reviewing（信息科安全评审，生成评审单 SEC-RV-{date}-{seq}）
  │     ├─ approved → published（市场上架，卡片+统计联动）
  │     └─ rejected → draft（驳回原因留痕，整改后重提）
published 升级版本 → 新版本走 lite 评审
                  （diff 未引入新写入类工具时自动快审）
```

**评审规则引擎（对应原型动态评审规则）：**

| 声明内容 | 评审通道 | SLA |
|---------|---------|-----|
| 含写入类工具（`annotations._meta.rw: write`） | 强制完整安全评审（写入范围/数据范围/Craft 模式额外审批） | ≤ 2 个工作日 |
| 纯只读工具 + Ask 模式 | 快审通道（声明一致性抽查） | ≤ 4 小时 |

**关键实现：**

| 机制 | 实现 |
|------|------|
| 沙箱试运行 | 上架前必须跑通全部示例问法（每问法 ≥ 3 次）：只读真实数据 + 写入路由到测试环境（mcp server 的 `sandbox` profile，按 `_meta.rw` 自动切换写入目标） |
| 工具声明快照 | 提交时固化 `skill_reviews.tool_decls`（工具 ID/读写/数据范围），评审与后续版本 diff 都以此为基线 |
| 评审任务分发 | 信息科评审人的站内待办（复用审批待办视图），超 SLA 升级提醒 |
| 市场统计联动 | 运行时埋点回写 `skills.stats`（调用量/成功率/确认卡修改率），驱动市场卡片展示与 16.4 灰度观测 |
| 版本管理 | `skill_versions` 语义化版本；在用用户不自动升级，破坏性变更（表单字段变化）需用户二次确认 |

### 4.8 自动化任务（Phase 2，PRD 3.6.2）

**模块结构（agent_core/automation/）：**

```
├── scheduler.py    # APScheduler + Redis 分布式锁（多副本防重复触发）
├── templates.py    # 模板库：预置场景（早报/周报/审批超时/库存预警/合同到期/月度简报），
│                   #   一键套用预填（名称/调度/渠道/技能/模式）
├── trial.py        # 试运行 HITL：首次执行结果推送创建者确认后才转 active
├── runner.py       # 以创建者身份执行；硬性拒绝 Craft 模式；30 分钟超时终止
└── pusher.py       # 结果推送：企微 bot / Chat-Work 会话 / 邮件
```

**任务状态机（对应原型四步向导 + PRD「先跑通再自动化」）：**

```
draft（来源三选：会话转换[推荐，参数自动带入] / 模板创建 / 空白）
  → configured（调度规则 + 推送渠道；事件触发型无时间字段）
  → trial_running（首次试运行）
  → pending_confirm（结果推送创建者，HITL 确认）
       ├─ confirmed → active（正式启用）
       └─ rejected  → paused（调整配置后重试）
运行态：active ⇄ paused（连续 3 次失败自动暂停并通知创建者）
```

**防护栏落地（PRD 3.6.2 执行约束 → 架构实现）：**

| 约束 | 实现 |
|------|------|
| 单任务 ≥ 15 分钟间隔 | scheduler 注册时校验调度规则，拒绝高频配置 |
| 30 分钟超时终止 | `runner` 用 `asyncio.timeout` 包裹执行，超时终止并推送失败通知 |
| 单用户并发 ≤ 3 | Redis 信号量 `auto_run:{userId}`，超出排队（全系统按 Token 预算） |
| 连续 3 次失败自动暂停 | `automation_runs` 失败连击计数 → 状态翻转 + 通知创建者；失败不自动重试（区别于幂等写入） |
| 仅 Ask / Plan 模式 | `runner` 硬编码校验，无人值守禁止无人在场写入 |
| 执行身份 = 创建者 | 任务携带创建者 JWT 上下文执行（权限随人走，perm_ver 变更即时收敛）；创建者离职 → 8.5.6 生命周期联动自动停用 |

**执行历史：** `automation_runs`（run_id/task_id/产物 URI/耗时/Token 消耗/成败），产物落 MinIO；支持自然语言管理（"把早报改成 9 点发"→ 结构化更新调度规则，走 pipeline 同一抽参链路）。

### 4.9 记忆离线固化与可迁移（Phase 2+，借鉴 OpenClaw Dreaming / MEMORY.md）

**离线固化任务（agent_core/memory/consolidator.py，借鉴 OpenClaw「Dreaming」）：**

| 环节 | 设计 |
|------|------|
| 触发 | 系统级定时任务（低峰期每日一次，如 02:00），复用 APScheduler 基础设施但独立于用户自动化任务（系统内部任务，无 owner） |
| 输入 | 近 7 天已结束会话（L1 过期前快照）+ 当期 L2 已有记忆（防重复提炼） |
| 提炼 | 小模型离线聚合：识别重复操作模式、常用参数、偏好与习惯 → 生成 L2 候选记忆条目（`source: auto_consolidated`） |
| 敏感过滤 | **前置过滤**（写入前而非读取时）：工资/合同金额/联系方式等敏感字段直接丢弃（PRD 9.2 敏感隔离），与 Dreaming 的"敏感内容不写记忆产物"一致 |
| 入池 | 仅对已同意 PIPL 知情同意的用户写入；产物进记忆面板「近期沉淀」列表，用户可查看/删除（可感知性），默认启用但可关闭 |
| 衰减对齐 | 沿用 PRD 9.2 遗忘机制（90 天未引用降权、180 天归档），固化产物不豁免衰减 |

**记忆可迁移（MEMORY.md 式文件化，对应 PRD 9.2「可感知性」）：**

- **导出**：记忆面板一键导出 `memories_{userId}_{date}.md`——按「偏好 / 常用参数 / 操作习惯」分节的 Markdown，每条含来源（`default`/`ask`/`auto_consolidated`）与时间戳，用户可读可编辑（对齐 OpenClaw 记忆文件化理念：行为可检查、可版本化）
- **导入**：本地编辑后导回走同一校验链（敏感字段过滤 + 格式校验），条目标记 `source: imported`
- **边界**：导出文件属个人数据，不进知识库、不跨科室共享（PRD 9.2 默认隐私）；离职清除逻辑不受导出影响

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

## 6. 关键数据流

### 6.1 销售订单录入（OA，MVP，端到端）

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

### 6.2 ERP 复杂单据录入（Phase 2：主表上百字段 + 子表 + 孙表批次）

```
用户                agent-core                     mcp-erp           ERP系统
 │ "录ERP销售订单，华东仓…" │                           │                  │
 │──HTTPS POST /chat───▶│                           │                  │
 │                      │ ① 技能路由（erp_sales_order） │                  │
 │                      │ ② erp__search_materials(Top-N)                  │
 │                      │────MCP call──────────────▶│──模糊检索(游标)──▶│
 │                      │◀─候选[编码/规格/库存/协议价]─│◀────────────────│
 │◀─SSE: 物料候选选择卡（Top-N，必选不猜）              │                  │
 │ [选用 SP-1024]       │                           │                  │
 │──POST 材料选定──────▶│ ③ 服务端带出协议价/默认值（Agent 不代填）          │
 │                      │ ④ 分步抽参：LLM 只抽对话提及字段，               │
 │                      │    未提及分组折叠标记"待补"（Token 预算控制）     │
 │◀─SSE: 单据摘要卡（组数/金额/异常行数）              │                  │
 │◀─SSE: 工作台卡片（可展开全屏）                      │                  │
 │ [全屏工作台：分组折叠│子表滚动|仅看异常行过滤]        │                  │
 │ [钻取子表行]          │──erp__get_batches(行ID)──▶│──批次查询───────▶│
 │                      │  [孙表懒加载：按行 REST 拉取，不进对话流]         │
 │ [修改字段"税率改13%"] │                           │                  │
 │◀─SSE: diff 卡（旧值删除线→新值，金额联动重算）        │                  │
 │ [提交（大额触发分级审批提示）]                        │                  │
 │─POST /confirmations/tok─▶ ⑤ 整单幂等键生成           │                  │
 │                      │ ⑥ erp__create_sales_order(主+子+孙 三级整单)    │
 │                      │    单事务边界：主表→子表→孙表逐级写入，          │
 │                      │    任一级失败整单回滚 + 异常留痕                  │
 │                      │────MCP call──────────────▶│──三级写入(事务)──▶│
 │                      │◀─单据号 ERP-SO-xxx + 行数/金额快照─│◀───────────│
 │◀─SSE: 成功卡（单号/三级写入流水线/分级审批流）        │                  │
```

**关键设计（与 6.1 的差异）：**

| 维度 | OA 简单单据（6.1） | ERP 复杂单据（本节） |
|------|------------------|-------------------|
| 参数提取 | 单轮 LLM 抽全字段 | **分步抽参**：Schema 按分组裁剪进上下文，只处理提及字段 |
| 物料 | 客户/商品候选（枚举对齐） | **上万物料 Top-N 检索 + 用户必选**，选定后服务端带出协议价 |
| 草稿结构 | 平面字段 | 三级嵌套（header 分组 + lines + batches 懒加载），存 `doc_drafts` |
| 确认交互 | 确认卡 | 摘要卡 + **全屏工作台**（分组折叠/仅看异常行/孙表钻取）+ diff 卡 |
| 幂等粒度 | 单据级 | **整单级**（主+子+孙共用一个幂等键，幂等记录含行数/金额快照供对账） |
| 写入 | 单次调用 | **单事务边界三级写入**，任一级失败整单回滚、异常留痕（PRD 8.7 扩展） |
| 审批 | OA 审批流 | 金额超阈值 → ERP 侧分级审批流（大额单独提示） |

---

## 7. 共享契约设计（protocol 包，双语言之源）

```
packages/protocol/
├── tools/
│   ├── oa__create_sales_order.json   # MCP inputSchema + annotations(_meta: hitl/idempotency/requiredPermissions)
│   ├── oa__query_customers.json
│   ├── bi__execute_query.json
│   ├── erp__search_materials.json    # Phase 2：Top-N 候选，_meta.rw: read（候选卡数据源）
│   ├── erp__create_sales_order.json  # Phase 2：三级整单结构，_meta.idempotencyRequired: true（整单粒度）
│   ├── erp__get_batches.json         # Phase 2：孙表懒加载
│   ├── crm__update_contract_remark.json  # Phase 2：_meta.rw: write（走完整评审 + HITL）
│   └── local__write_file.json
├── events/                           # SSE 事件类型定义（stage_progress/draft_card/confirm_card/
│                                    #   diff_card/material_candidates/doc_workbench/final）
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
  mcp-crm: mcp-erp: mcp-wms:   # Phase 2 接入时加入（PRD 6.1 首批）
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
| 3.4 / 3.5 技能市场上架与生命周期 | agent_core/skills（market.py + review.py：上架状态机/沙箱试运行/评审 SLA），见 4.7 |
| 3.6.2 自动化任务 | agent_core/automation（scheduler/templates/trial/runner/pusher），见 4.8 |
| 4.1 Agent Core 流水线 | agent_core/pipeline（LangGraph StateGraph；复杂单据见 4.1 扩展 + 6.2） |
| 6.1 Phase 2 系统对接（CRM/ERP/WMS） | mcp_crm / mcp_erp / mcp_wms（见 4.2 Phase 2 首批） |
| 8.3 MCP 规范 | services/mcp_* + packages/protocol |
| 8.5 SSO | Keycloak + APISIX jwt 插件 + desktop/main/auth.ts（Loopback） |
| 8.7 幂等 | mcp-* 的 idempotency 模块（PG 表）+ agent_core/guardrail；ERP 整单粒度扩展见 6.2 |
| 9 记忆/知识库 | agent_core/memory + knowledge + Milvus/Redis（离线固化与可迁移见 4.9） |
| 5.5 桌面端 | apps/desktop + local-mcp（TS/stdio） |
| 16 评估 | evals/ 目录 + CI 回归门禁 |
