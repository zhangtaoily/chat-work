# 开发轮次总结：单据工作台与 Phase 3 全量落地

> 记录时间：2026-09-23。
> 本轮范围：wb-a（单据工作台弹层 MVP）+ P3.1–P3.5（Phase 3 全部任务）。
> 依据用户指令，**P1.3 容器栈实跑与 P1.4 灰度实际放量明确排除**（待外部条件：Docker 环境、GPU 节点 vLLM、内网基建确认——PRD 14 章待确认项），优先实现其余全部功能。
> 任务状态明细见 [PLAN.md](PLAN.md)。

## 总览

| 任务 | 内容 | 状态 |
|------|------|------|
| wb-a | 单据工作台弹层 MVP | ✅ |
| P3.1 | MES / U8 接入（生产执行与财务域） | ✅ |
| P3.2 | 全部科室覆盖（剩余科室技能补齐） | ✅ |
| P3.3 | 个人 / 组织记忆 | ✅ |
| P3.4 | 小程序（移动端入口） | ✅ |
| P3.5 | 语音（输入/播报） | ✅ |
| P1.3 / P1.4 实跑放量 | 容器栈起停、GRAY_PERCENT 0→10 | ⏸ 按指令排除，待外部条件 |

---

## wb-a 单据工作台弹层 MVP ✅

**目标**：原型中工作台的全屏单据详情弹层落地（复杂单据可视化复核）。

**交付物**：[DocWorkbenchModal.tsx](../apps/desktop/renderer/src/chat/DocWorkbenchModal.tsx)

- 全屏弹层容器，单据详情一站式预览
- 主表字段分组渲染（key 不在白名单的归入「其他」组），跳过子表与技术性隐藏字段
- 子表明细渲染（rules.py CRM 订单解析产物 `{ sku, qty, price? }`）
- 异常分级确认：price 缺失即异常行高亮，配合分级确认交互
- **零后端改动**：数据源复用现有 draft_card（items 子表数组）+ confirm_card 快照，不改事件契约；`doc_workbench` 事件真实接线留后续（[chatModel.ts](../apps/desktop/renderer/src/chat/chatModel.ts) 中为 Phase 2 事件暂不渲染）

---

## P3.1 MES / U8 接入 ✅

**目标**：生产执行域（MES）与财务域（用友 U8）接入技能体系（PRD 7.1/8.2）。

**交付物**：

- 新增 2 个 MCP 服务（沿用 OA/CRM 适配器模式）：
  - [mcp_mes](../services/mcp_mes/)：工单进度查询、报工记录（[tools/production.py](../services/mcp_mes/tools/production.py) + mes_client 适配器）
  - [mcp_u8](../services/mcp_u8/)：科目余额表、凭证明细（[tools/gl.py](../services/mcp_u8/tools/gl.py) + u8_client 适配器，服务端重算合计与借贷平衡）
- agent_core 接入：[mcp_client/mes.py](../services/agent_core/agent_core/mcp_client/mes.py)、[u8.py](../services/agent_core/agent_core/mcp_client/u8.py) 客户端；registry 注册 `mes_production_report`、`u8_gl_summary` 两个只读技能（无 HITL 写路径）
- 技能编排：工单进度列表（MES_STATUS 中文渲染）/ SKU 可选过滤透传 / 工单号直达报工明细（操作工、工时）；科目余额表（期间缺省当月 computed 槽位）/ 期间 + 科目口语关键词过滤 / 凭证号直达明细 + 借贷平衡渲染
- 桌面端 labels.ts 补字段中文标签

**验证**：[test_mes_u8.py](../services/agent_core/tests/test_mes_u8.py) 六类场景（进度列表 / SKU 过滤 / 报工直达 / 余额表 / 期间科目过滤 / 凭证借贷平衡）。

---

## P3.2 全部科室覆盖 ✅

**目标**：补齐剩余科室只读技能，实现「全部科室」覆盖。

**交付物**：

- registry 新增 5 科室技能（dept_scope 门禁 + match_skill 最长关键词路由）：
  `hr_roster` 人力速览 / `mgmt_overview` 企管速览 / `audit_trace` 审计流水 / `tech_inventory` 备件巡检 / `product_sales` 产品看板
- graph.py extract/execute/format 三节点分支各加 5 科室：
  - `audit_trace` 进程内直调 audit.recent（本人过滤，不走 MCP）
  - `product_sales` BI dimensions=["product"] + CRM 客户 360（单命中直达 / 多命中候选）
  - `mgmt_overview` 复用 build_data_check_text、`tech_inventory` 复用 build_stock_overview_text
- rules.py 新增 build_hr_activity_lines / build_audit_trace_text / build_product_sales_text 渲染辅助
- Keycloak realm 追加 E2006–E2010 五种子用户（perm_ver 17）；桌面端 labels.ts 补 6 字段中文标签

**验证**：test_dept_phase3.py 11 项（本科室放行 / 跨科室拒绝 / 审计本人过滤 / 客户 360 编排）+ test_skill_store dept 数 5→10，全量 pytest 281 绿。

---

## P3.3 个人 / 组织记忆 ✅

**目标**：跨会话偏好与组织知识沉淀（PRD 9.2/9.3、ARCHITECTURE 4.9）。

**交付物**（详细见 [PLAN.md](PLAN.md#L73) P3.3 行）：

- 新增 [memory/](../services/agent_core/agent_core/memory/store.py) 域：L2 个人记忆 + L3 组织记忆，进程内存储 + Redis 快照 `memory:snapshot` 写镜像
- 合规与安全：PIPL 知情同意门禁（未同意 400，撤回即 `_purge_personal` 全清 + 审计）、敏感信息前置过滤（身份证/银行卡/密码）、遗忘机制（90 天降权 / 180 天归档）
- 写入触发双通道：高频行为自动沉淀（同 skill+text 连续 ≥3 次 → habit）+ 技能路由（`memory_save` 技能，正则匹配剥前缀）
- graph 集成：intent 阶段注入记忆上下文（stage_progress「已参考记忆」）、execute 阶段自动 track 沉淀、成功返回确认卡
- API 7 端点（list/add/delete/clear/consent/promote/export.md）
- 桌面端 MemoryView 占位页转正：PIPL 开关 / 撤回 / 导出 / 分层清单 / 升级 / decayed 标记与统计

**验证**：pytest 297（新增 test_memory 16 项：store 单元 8 + 流水线 e2e 3 + API 5）+ 17（crm）+ 12（erp）+ 9（wms）+ 5（bi）全绿。

---

## P3.4 小程序（移动端入口）✅

**目标**：Taro 跨端小程序，覆盖移动端问答 / 审批 / 通知 / 个人设置（PRD 7.3/8.5.3）。

**交付物**：[apps/miniapp](../apps/miniapp/)（Taro 4.1.9 + React 18 + TS + CSS Modules）

- 四 Tab（对话 / 审批 / 信箱 / 我的）+ TabBar 8 SVG
- 对话页：SSE 流式渲染 8 类事件卡（契约对齐 packages/protocol/chat-events.ts）；**SSE 跨端双实现**——H5 fetch ReadableStream + TextDecoder，微信 Taro.request enableChunked + 手写 UTF-8 增量解码（多字节尾部拆分保留）
- HITL 沉淀闭环：confirm_card/plan_card → zustand confirmations store → 审批 Tab 聚合 + POST 处置 + 本地历史
- 信箱页：/automations/inbox + 已读；我的页：连接设置 + 记忆面板移动端 + 关于
- 演示模式：内置 5 剧本 + 12 条信箱 + 10 条记忆 mock，与真实模式零改动切换；灰度门禁 X-Client-Version: 1.0.0

**过程修复**（Taro 模板依赖冲突，本轮实际踩坑）：

| 问题 | 修复 |
|------|------|
| @tarojs/taro-loader peer 要求 webpack@5.91.0 | package.json webpack 5.78.0 → 5.91.0 |
| babel-preset-taro peer 要求 react-refresh@^0.14.0 | ^0.11.0 → ^0.14.0 |
| minidev 发布类 CLI 沙箱写日志被拦 | 移除 miniprogram-ci / tt-ide-cli / minidev |
| tsc 报 minimatch/sass 类型库缺失 | tsconfig types 显式白名单 + skipLibCheck |
| app.tsx 未使用 React 导入（TS6133） | 删除默认导入（jsx: react-jsx） |

**验证**：tsc --noEmit 0 错误 + npm run build:h5 通过 + H5 四页浏览器冒烟全通过（对话剧本流式 / 确认卡处置闭环 / 信箱 / 记忆面板）。

---

## P3.5 语音（输入/播报）✅

**目标**：语音输入 + 回复播报，适配生产车间、仓库等免手场景（PRD 语音交互）。

**方案**：Web Speech API 原生实现（Electron Chromium 内置，**零外部 SDK / 零网络依赖**）。

**交付物**：

- 新增 [speech.ts](../apps/desktop/renderer/src/lib/speech.ts)：
  - SpeechRecognition 最小类型声明（TS DOM lib 未内置）
  - `createRecognizer` 单次听写会话：zh-CN、interimResults 实时中间结果、说完停顿自动结束、错误码中文映射（not-allowed / no-speech / audio-capture / network 等）
  - `speak` / `cancelSpeak`：speechSynthesis zh-CN 播报，重复调用打断上一段
- [App.tsx](../apps/desktop/renderer/src/App.tsx) 集成：
  - 输入区左侧麦克风按钮：识别中 primary+danger 红色高亮、再次点击手动停止、实时文本直接进输入框、错误以 danger 文案显示输入区上方
  - 播报开关（SoundOutlined）：localStorage `chatwork.tts` 持久化、关闭即打断进行中播报
  - `sendText` final 事件到达且开关开启时自动朗读回复
  - 能力探测：不支持环境按钮 disabled + Tooltip 提示

**边界**：小程序端微信不支持 Web Speech API，语音输入需接微信同声传译插件，留待后续（PLAN 外）。

**验证**：tsc typecheck 0 错误 + electron-vite build 三段全绿 + dev:web 浏览器冒烟（输入区渲染 / 麦克风错误路径中文提示 / 播报开关 on-off 持久化 / 发送流程回归 / console 无报错）。

---

## 质量基线汇总

| 域 | 回归结果 |
|----|----------|
| agent_core（Python） | pytest 297 绿（P3.3 后全量），ruff 0 错误 |
| MCP 服务 | crm 17 + erp 12 + wms 9 + bi 5 + mes + u8 全绿 |
| 桌面端（Electron） | typecheck 0 错误 + electron-vite build 三段全绿 + dev:web 冒烟 |
| 小程序（Taro） | tsc --noEmit 0 错误 + build:h5 通过 + H5 四页浏览器冒烟 |

## 遗留与待外部条件

| 项 | 状态 | 依赖 |
|----|------|------|
| P1.3 容器栈实跑 | 部署物已交付（infra/README.md），未实际起停 | 本机无 Docker |
| P1.4 灰度实际放量 | GRAY_PERCENT 0→10 未执行 | GPU 节点（vLLM）与内网基建确认（PRD 14 待确认项） |
| P2.8 桌面端 macOS 版 | 📋 视需求启动 | — |
| mock_idp → 真实 Keycloak | 仅需改 `SSO_ISSUER` env | 生产环境 |
| 小程序语音输入 | 需微信同声传译插件 | PLAN 外 |
| doc_workbench 事件真实接线 | 弹层 MVP 已就绪，后端事件契约已有 | Phase 2+ |
