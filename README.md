# chat-work

企业内网 AI Agent（Electron 桌面端，唯一客户端）。MVP 覆盖 **OA 流程审批**（请假/报销/请购录入与待办审批）与 **BI 自然语言查询**；销售订单录入（CRM，提交审批）为 Phase 2（PRD 6.1.1）；技术基调：Python（服务端）+ TypeScript（客户端）。

## 文档

- [PRD](./PRD-chat-work-agent.md) ｜ [技术架构](./ARCHITECTURE.md) ｜ [交互原型](./prototype.html)

## 工程结构（对齐 ARCHITECTURE 第 3 章）

```
chat-work/
├── apps/desktop/          # Electron 桌面端（main/preload/renderer/local-mcp）
├── services/              # Python 服务（各自 uv 项目）
│   ├── agent_core/        # Agent 核心（FastAPI + LangGraph 七节点流水线）
│   ├── mcp_oa/            # OA MCP Server（oa__*）
│   └── mcp_bi/            # BI MCP Server（bi__*，只读）
├── packages/              # TS 共享包
│   ├── protocol/          # ★ 契约源：MCP 工具 JSON Schema + SSE 事件类型
│   ├── api-client/        # OpenAPI 生成的 TS SDK
│   ├── ui/                # 业务卡片组件库（确认卡/摘要卡/BI 卡…）
│   └── shared/            # 纯工具（格式化/枚举）
├── infra/                 # compose / keycloak / apisix / llm / k8s
├── evals/                 # 评估用例（YAML）+ 评分脚本
├── pnpm-workspace.yaml
└── tsconfig.base.json
```

> MVP 不含 mcp_crm / mcp_erp / mcp_wms（Phase 2 接入）。

## 开发环境

- Node 22 LTS + pnpm 9（TS 应用与共享包）
- Python 3.12 + uv（Python 服务）

## 快速开始（占位）

```bash
pnpm install                      # TS 依赖
pnpm --filter @chat-work/desktop dev   # 桌面端开发
uv sync                            # agent_core 依赖
uv run pytest                      # 服务端测试
```

## 目录说明

| 目录 | 说明 |
|------|------|
| apps/desktop/main | 主进程：窗口/托盘/Quick Ask/IPC/认证/local-mcp 拉起 |
| apps/desktop/renderer | React 18 + AntD 5 渲染层（卡片来自 packages/ui） |
| apps/desktop/local-mcp | 本地文件工具 MCP Server（stdio，随端分发） |
| services/agent_core | 流水线/技能/记忆/知识/护栏/自动化 |
| packages/protocol | 唯一工具 Schema 源（双语言契约，变更须双端同步） |
| infra/compose | MVP 单机 Docker Compose 全家桶 |
