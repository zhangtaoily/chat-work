# chat-work

企业内网 AI Agent（Electron 桌面端 + Taro 小程序，桌面端为唯一主客户端）。Agent 核心（FastAPI + LangGraph）通过 MCP 协议对接七大业务域：**OA 流程审批、BI 自然语言查询、CRM 销售订单、ERP 采购/库存/凭证、WMS 库存单据、MES 生产工单、U8 总账**；配套记忆、知识库、HITL 确认、自动化任务与语音输入。技术基调：Python（服务端）+ TypeScript（客户端）。

## ⚠️ 当前状态：业务系统对接均为 Mock 假数据

**所有 MCP 服务当前运行在 mock 模式（默认），对接的是进程内模拟数据，不是真实业务系统。**

- 数据层面：客户/SKU/库存/工单等主数据为硬编码样例（如 mcp_crm 的客户 C-2001「华辰机械制造有限公司」、SKU-A/B/C 目录）
- 写入层面：下单/审批/过账等写操作仅在服务进程内存中模拟，**不会写入任何真实系统**，服务重启即丢失
- LLM 层面：未配置 `LLM_BASE_URL` 时，agent-core 使用规则兜底而非真实大模型

因此目前端到端测试验证的是**链路联通性、协议契约与交互流程**，不验证真实业务语义。切换真实系统见下文 [切换真实业务系统](#切换真实业务系统)。

## 文档

- [PRD](./PRD-chat-work-agent.md) ｜ [技术架构](./ARCHITECTURE.md) ｜ [Phase 3 交付总结](./docs/PLAN-p3-summary.md) ｜ [容器部署说明](./infra/README.md) ｜ [交互原型](./prototype.html)

## 工程结构（对齐 ARCHITECTURE 第 3 章）

```
chat-work/
├── apps/
│   ├── desktop/           # Electron 桌面端（唯一主客户端）
│   │   ├── main/          #   主进程：窗口/托盘/IPC/认证/local-mcp 拉起
│   │   ├── preload/       #   渲染桥接
│   │   ├── renderer/      #   React 18 + AntD 5 渲染层（含 wb-a 文档弹层）
│   │   └── local-mcp/     #   本地文件工具 MCP Server（stdio，随端分发）
│   └── miniapp/           # Taro 小程序端（审批/收件箱/对话，H5 构建）
├── services/              # Python 服务（各自 uv 项目）
│   ├── agent_core/        # Agent 核心（FastAPI + LangGraph 七节点流水线）
│   ├── mcp_oa/            # OA MCP Server :8001（oa__*）
│   ├── mcp_bi/            # BI MCP Server :8002（bi__*，只读）
│   ├── mcp_crm/           # CRM MCP Server :8003（客户/销售订单）
│   ├── mcp_erp/           # ERP MCP Server :8004（采购/库存/凭证）
│   ├── mcp_wms/           # WMS MCP Server :8005（库存单据/预警）
│   ├── mcp_mes/           # MES MCP Server :8006（生产工单）
│   ├── mcp_u8/            # U8 MCP Server :8007（总账查询）
│   └── mock_idp/          # Mock OIDC 身份提供方 :8012（本地登录用）
├── packages/              # TS 共享包
│   ├── protocol/          # ★ 契约源：MCP 工具 JSON Schema + SSE 事件类型
│   ├── api-client/        # OpenAPI 生成的 TS SDK
│   ├── ui/                # 业务卡片组件库（确认卡/摘要卡/BI 卡…）
│   └── shared/            # 纯工具（格式化/枚举）
├── infra/                 # compose / keycloak / apisix / llm / k8s（已交付，未实跑）
├── evals/                 # 评估用例（YAML）+ 评分脚本
├── pnpm-workspace.yaml
└── tsconfig.base.json
```

## 开发环境

- Node 22 LTS + pnpm 9（TS 应用与共享包）
- Python 3.12 + uv（Python 服务）

## 快速开始

### 端口分配

| 服务 | 端口 | 启动方式 |
|------|------|----------|
| mcp_oa / mcp_bi / mcp_crm / mcp_erp / mcp_wms / mcp_mes / mcp_u8 | 8001–8007 | `python server.py`（各服务目录下） |
| agent-core | 8011（本地直跑；env 默认 8000 为生产容器端口） | `uvicorn agent_core.api.main:app --port 8011` |
| mock_idp | 8012 | `python -m mock_idp` |
| 桌面端 Web 预览 | 5173 | `pnpm --filter @chat-work/desktop dev:web` |
| Electron 桌面端 | — | `pnpm --filter @chat-work/desktop dev` |

### 启动步骤

```bash
# 0. 安装依赖
pnpm install                              # TS 侧
cd services/agent_core && uv sync && cd ../..   # 每个 Python 服务同理

# 1. 启动 mock 身份提供方（本地 SSO 登录）
cd services/mock_idp && python -m mock_idp     # :8012

# 2. 启动各 MCP Server（每个服务独立目录）
cd services/mcp_oa && python server.py         # :8001
cd services/mcp_bi && python server.py         # :8002
cd services/mcp_crm && python server.py        # :8003
cd services/mcp_erp && python server.py        # :8004
cd services/mcp_wms && python server.py        # :8005
cd services/mcp_mes && python server.py        # :8006
cd services/mcp_u8 && python server.py         # :8007

# 3. 启动 agent-core
cd services/agent_core && uvicorn agent_core.api.main:app --port 8011

# 4. 启动前端（二选一）
pnpm --filter @chat-work/desktop dev:web       # Web 预览 :5173
pnpm --filter @chat-work/desktop dev           # Electron 桌面端
```

agent-core 通过 env `MCP_OA_URL` ~ `MCP_U8_URL`（默认 `http://127.0.0.1:8001` ~ `:8007`）发现各 MCP 服务，详见 [agent_core/config.py](./services/agent_core/agent_core/config.py)。

### 测试

```bash
cd services/agent_core && uv run pytest    # 服务端测试（297 项）
pnpm --filter @chat-work/desktop typecheck && pnpm --filter @chat-work/desktop test
```

## 切换真实业务系统

每个 MCP 服务通过环境变量在 mock/http 双模式间切换，**无需改代码**：

| 服务 | 模式开关 | 真实系统地址 |
|------|----------|--------------|
| mcp_oa | `OA_MODE=http` | `OA_BASE_URL`（默认 `http://oa.example.internal/api`） |
| mcp_crm | `CRM_MODE=http` | `CRM_BASE_URL` |
| mcp_erp | `ERP_MODE=http` | `ERP_BASE_URL` |
| mcp_wms | `WMS_MODE=http` | `WMS_BASE_URL` |
| mcp_mes | `MES_MODE=http` | `MES_BASE_URL` |
| mcp_u8 | `U8_MODE=http` | `U8_BASE_URL` |

mcp_bi（BI 查询）目前仅实现了进程内确定性 mock 数据（区域 × 产品线 × 月份），**未实现 http 模式**，生产需按 ARCHITECTURE 4.2.2 替换为真实 BI OpenAPI。

http 模式下，MCP 服务以 **Service Account** 调用真实系统 OpenAPI，并按 PRD 8.5.5 携带「代理人」双标记（操作人=发起用户，执行账号=Service Account）；写路径保留幂等键（24h）。SSO 从 mock_idp 切真实 IdP（Keycloak）的 env 配置见 [infra/README.md](./infra/README.md)。

## 已知限制与遗留项

- **容器栈实跑（P1.3）与灰度实际放量（P1.4）暂缓**：infra 交付物（compose/Keycloak/APISIX/发版流程）已就绪，待外部条件后联调；`GRAY_PERCENT` 机制已实现未实际放量
- **mock_idp 为临时身份方案**：生产应替换为 Keycloak，SSO env 三件套（`SSO_ISSUER`/`JWKS_URI`/`AUDIENCE`）已预留
- **LLM 未接入**：未配置 `LLM_BASE_URL` 时走规则兜底，意图识别与槽位提取的覆盖面有限（如客户名与动词间带空格等句式会解析失败）
- **SSE doc_workbench 事件未接线**：wb-a 文档弹层的后端推送事件尚未在桌面端消费
- macOS 打包分发、数据持久化（写路径重启即失）未做
