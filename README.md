# chat-work

企业内网 AI Agent（Electron 桌面端 + Taro 小程序，桌面端为唯一主客户端）。Agent 核心（FastAPI + LangGraph）通过 MCP 协议对接七大业务域：**OA 流程审批、BI 自然语言查询、CRM 销售订单、ERP 采购/库存/凭证、WMS 库存单据、MES 生产工单、U8 总账**；配套记忆、知识库、HITL 确认、自动化任务与语音输入。技术基调：Python（服务端）+ TypeScript（客户端）。

## 愿景：工业 5.0 下的「员工数字分身」

工业 4.0 追求全自动与效率极致；**工业 5.0（Industry 5.0）的核心转向以人为本、韧性、可持续**——机器不是替代人，而是增强人。本系统的关键设计即源于此思想：

- **人机协作，而非无人化**：所有跨系统写入必须经 HITL 确认卡由人最终拍板——AI 负责跨域检索、拟稿与执行，人负责判断与担责，人始终拥有否决权
- **以人为本的交互范式**：员工用一句自然语言即可穿越 OA/CRM/ERP/WMS/MES/U8/BI 的界面与组织墙，把人从「系统操作员」解放为「决策者」
- **组织韧性**：单一入口聚合全域数据视图与预警（库存/审批/生产/财务），组织对异常的感知与响应不再依赖个人经验和系统切换成本

**产品定位——「员工的数字分身」**：它不是又一个需要学习的企业系统，而是每个员工在数字世界里的另一个自己——替你穿越系统墙、记住你的上下文、7×24 替你值守：

1. **你的统一入口**：自然语言是唯一不需要培训的企业 UI，你向分身提问/下指令，分身翻译为对 OA/CRM/ERP/WMS/MES/U8/BI 的编排调用
2. **你的记忆**：对话记忆 + 知识库 + 审计流水随你沉淀——你不在场时，分身依然带着完整上下文办事
3. **你的执行臂**：定时周报、巡检、预警等自动化任务让分身 7×24 值守，例行事务自动化，你聚焦判断与决策
4. **替你执行、由你拍板**：权限护栏、HITL 确认卡、审计、幂等与灰度机制，保证分身的每个动作可追溯、可回滚——它再像你，关键决定也永远由人做出

个人分身的协同与沉淀，长期涌现为组织级能力——协调各业务系统「器官」的「集团大脑」：人会流动，经验与流程留在组织里。

当前仓库是该愿景的第一阶段落地面（单机 mock 链路），后续按以下路线推进：

### 落地实施步骤（路线图）

| 阶段 | 目标 | 关键动作 | 验收标志 |
|------|------|----------|----------|
| **一：单机 mock 链路（已完成）** | 打通协议契约与交互流程 | 七域 MCP Server + agent-core 流水线 + 桌面端/小程序 | pytest 336 绿；HITL 确认卡闭环 |
| **二：真实业务对接** | mock → 真实系统 | 各业务方提供 OpenAPI 与 Service Account → 设 `{XXX}_MODE=http` + `{XXX}_BASE_URL` 联调；接入真实 LLM（`LLM_BASE_URL`）；mcp_bi 对接真实 BI OpenAPI | 真实下单/审批/过账写路径全量走通，代理人双标记在业务系统侧可审计 |
| **三：生产化运行** | 容器栈实跑 + 安全底座 | infra compose 实跑（Keycloak 替换 mock_idp、APISIX 路由）；`GRAY_PERCENT` 灰度放量；数据持久化（MySQL/Redis） | 生产 env 全量配置；灰度按用户百分比放量；审计可查 |
| **四：数字分身 → 集团大脑** | 从个人分身到组织智能 | 组织记忆沉淀（跨人会话知识入库）；自动化任务扩面（巡检/预警/周报）；跨域编排技能（如「订单→生产→发货」全链路）；小程序成为移动触点 | 例行决策自动化率提升；新员工零培训上手 |

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

> **Windows 一键启动**：双击根目录 [start-all.bat](./start-all.bat)——9 个后端服务（mock_idp、7 个 MCP Server、agent-core）在**后台隐藏运行**（不弹窗口，日志写入 `logs/`，端口已被占用时自动跳过），最终只保留 1 个控制台窗口运行 **Electron 桌面端**。追加参数可改浏览器预览：`start-all.bat web`。停止后台服务：双击 [stop-all.bat](./stop-all.bat)。各服务目录下也有独立 `start.bat` 可单独启动（如 [services/mcp_oa/start.bat](./services/mcp_oa/start.bat)、[apps/desktop/start.bat](./apps/desktop/start.bat)）。

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

> **启动桌面端（Electron）**：`pnpm --filter @chat-work/desktop dev`，或双击 [apps/desktop/start.bat](./apps/desktop/start.bat)。需后端已启动（`start-all.bat` 或上表逐个启动），否则登录不可用；`dev:web` 仅为浏览器预览模式（[apps/desktop/start-web.bat](./apps/desktop/start-web.bat)）。

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
