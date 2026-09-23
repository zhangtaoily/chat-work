# UI 设计需求文档：单据工作台弹层（wb-a）

> 版本：v1.0 ｜ 日期：2026-09-23
> 关联：[PLAN.md](PLAN.md)（wb-a 行）、[PLAN-p3-summary.md](PLAN-p3-summary.md)、[prototype.html](../prototype.html) L2250–2439（设计原型）
> 实现状态：**MVP 已交付**（[DocWorkbenchModal.tsx](../apps/desktop/renderer/src/chat/DocWorkbenchModal.tsx)，桌面端），`doc_workbench` 后端事件真实接线为 Phase 2

---

## 1. 背景与目标

复杂单据（ERP 销售订单、请假单等）字段多、含子表明细，对话内的摘要确认卡（ConfirmCard）无法承载审计级复核。本弹层为确认卡提供**全屏单据详情视图**，让用户在提交前完整核对：

- 主表字段**分组呈现**，标注取值来源（系统匹配 / 默认值 / 对话获得 / 记忆恢复）
- 子表明细**逐行核对**，异常行（缺单价等）高亮
- 异常不阻塞提交，但通过「**异常已知晓 · 确认提交**」的显式文案完成分级确认（PRD 4.1 HITL 原则：必选不猜、异常必知）

## 2. 设计依据与原型映射

原型 [prototype.html](../prototype.html) L2250–2439 定义了完整单据工作台（主/子/孙三级、审计视图、双轨编辑）。MVP 按下表裁剪：

| 原型能力 | MVP 取舍 | 说明 |
|----------|----------|------|
| 主表分组（8 组网格 + 来源标签） | ✅ 保留（简化为白名单 3 组 + 其他） | 分组折叠、字段计数（86 字段）留 Phase 2 |
| 子表明细 + 异常行高亮 + 仅看异常行 | ✅ 保留（核心） | 异常判定：price/qty 缺失即异常行 |
| 孙表（批次分配，点行展开懒加载） | ⏸ 未做 | 依赖 `doc_workbench` 事件契约 batches 懒加载 |
| 审计视图（查看全部 86 字段，折叠面板） | ⏸ 未做 | 依赖 header.groups 分组元数据 |
| 双轨编辑（表格点选修改回写对话） | ⏸ 未做 | 只读预览；编辑走对话自然语言 |
| 行内变更标注（改自 2,000 / 13 处变更已应用） | ⏸ 未做 | 依赖 diff_card 事件 |
| 大额阈值提示、交货列、物料名称规格 | ⏸ 未做 | MVP 子表仅 sku/qty/price |
| 合计金额 + 异常知晓提交 | ✅ 保留（核心） | — |

## 3. 用户场景与入口

**入口**（[ChatTurn.tsx](../apps/desktop/renderer/src/chat/ChatTurn.tsx#L178-L186)）：

- 确认卡（ConfirmCardView）底部链接按钮「展开完整单据」（`ExpandOutlined` 图标，type=link / size=small）
- 仅确认卡存在时可见；同一会话轮次内可反复开关

**典型场景**：

1. 用户说「下个 5,000 件的订单」，agent 产出 draft_card + confirm_card → 摘要卡信息不够 → 点「展开完整单据」→ 核对主表来源与子表异常 → 回到 footer 提交/取消
2. 恢复历史会话（无 draft_card，仅 confirm 快照）→ 弹层回退渲染确认卡快照字段

## 4. 信息架构（布局分区）

```text
┌────────────────────────────────────────────────────────────┐
│ Modal 标题区                                                │
│   [单据标题]  [草稿 v2]  [2 处异常]                          │
├────────────────────────────────────────────────────────────┤
│ 内容区（maxHeight 62vh，纵向滚动）                           │
│  ┌─ 主表分组区（2 列网格）──────────────────────────────┐   │
│  │ ┌ 基本信息 ────────┐ ┌ 物流信息 ────────┐            │   │
│  │ │ 客户名称 [系统匹配]│ │ 收货地址 [客户默认]│  …        │   │
│  │ └──────────────────┘ └──────────────────┘            │   │
│  │ ┌ 价格与付款 ──────┐ ┌ 其他 ────────────┐            │   │
│  │ └──────────────────┘ └──────────────────┘            │   │
│  └──────────────────────────────────────────────────────┘   │
│  ┌─ 子表明细区 ─────────────────────────────────────────┐   │
│  │ 商品明细（12 行）              [仅看异常行]            │   │
│  │ #  物料编码  数量  单价  金额  状态                    │   │
│  │ 1  SP-1024  5000  128.00  640,000.00  [正常]          │   │
│  │ 4  SP-2017  1500  —       —           [缺单价]←黄底   │   │
│  └──────────────────────────────────────────────────────┘   │
├────────────────────────────────────────────────────────────┤
│ footer                                                      │
│  合计金额                [已确认，提交完成] / [取消] [确认提交]│
│  ¥ 1,775,400.00                                              │
│  ⚠ 缺少必填信息：单价                                        │
└────────────────────────────────────────────────────────────┘
```

## 5. 视觉规格

| 项 | 规格 | 出处 |
|----|------|------|
| 容器 | antd Modal，`width: min(1140px, 96vw)`，垂直居中（centered） | DocWorkbenchModal L205 |
| 内容滚动 | `maxHeight: 62vh; overflowY: auto`（footer 常驻可见） | L271 |
| 标题 | `skill_title`（无则「单据工作台」）+ Tag：草稿版本 `color="blue"`、异常数 `color="orange"` | L199–216 |
| 主表分组卡 | `border: 1px solid #f0f0f0; borderRadius: 8; padding: 10px 12px`；组标题 13px 加粗 | L112 |
| 主表网格 | `grid 2 列（minmax(0,1fr)），gap 12` | L110 |
| 字段行 | antd Descriptions `column=1, size=small`；标签用 `fieldLabel()` 中文映射，值用 `fieldValueText()` 格式化 | L116 |
| 来源标签 | 字段值右侧 antd Tag，文案取 `SOURCE_LABELS`（如 value_source: user→对话获得 / default→默认值） | L121 |
| 子表 | antd Table `size=small, pagination=false`；金额 `toLocaleString('zh-CN')` 两位小数 | L169 |
| 异常行 | 行背景 `#fffbe6`（antd warning 浅底）+ 状态列 Tag `color="orange"`「缺单价」；正常行 Tag `color="green"`「正常」 | L155, L174 |
| 缺值占位 | 数量/单价缺失显示「—」，金额不可算显示「—」 | L145–149 |
| 合计金额 | footer 左侧：标签 12px secondary + 金额 22px 加粗（`¥ x,xxx.xx`） | L223–228 |
| 缺失提示 | 12px `type="warning"`：`缺少必填信息：{字段中文，顿号连接}` | L231–234 |

## 6. 模块需求明细

### 6.1 主表分组区（MainTableSection）

- **分组白名单**（`MAIN_TABLE_GROUPS`，未来随 doc_workbench 事件 groups 元数据扩展）：
  - 基本信息：customer_name / customer_id / order_type / contact / leave_type / start_time / end_time / duration_days
  - 物流信息：address / delivery_date
  - 价格与付款：payment_term
  - **其他**：不在白名单的字段自动归入（保证无字段遗漏）
- **过滤**：跳过子表 `items` 与 `HIDDEN_FIELDS`（技术性字段）
- **字段值来源**：`{ value, source? }` 归一（兼容平铺值），source 存在时值旁展示来源 Tag
- **无草稿回退**：`draft` 缺失时渲染 `confirm.payload.fields` 平铺 Descriptions（bordered），用于恢复会话场景

### 6.2 子表明细区（ItemTableSection）

- **数据**：`draft.draft.items` 数组，行为 `{ sku, qty, price? }`（rules.py CRM 订单解析产物）
- **列**：#（序号）/ 物料编码 / 数量 / 单价（元）/ 金额（元，qty×price）/ 状态
- **异常行判定**：`price === null || qty === null` → `anomalous`
- **异常计数**：异常行数 + `draft.missing_fields` 数，进标题「N 处异常」Tag 与按钮文案
- **过滤开关**：「仅看异常行」toggle 按钮（激活态 type=primary），开启后只渲染 anomalous 行；开关状态为弹层内局部 state（关闭弹层不重置需求：可接受，MVP 不持久化）
- **合计**：仅累加 qty 与 price 均非空的行

### 6.3 底部操作区（footer）

左区：

- 合计金额块（rows 非空时展示）
- 缺少必填信息提示（`missing_fields` 非空时）

右区（确认状态机驱动，同一时刻只显示其一）：

| confirmState | 展示 |
|--------------|------|
| `pending` 且 remain > 0 | 「取消」按钮 + 主按钮「确认提交」（**有异常时文案变体：「异常已知晓 · 确认提交」**），点击走 `onConfirm(token, action)`，confirmBusy 时 loading |
| `pending` 且 remain ≤ 0 | warning Tag「确认已失效，请重新发起」 |
| `confirmed` | success Tag（CheckOutlined）「已确认，提交完成」 |
| `rejected` | default Tag「已取消」 |
| `expired` | 同失效 Tag |

> 分级确认要点：异常存在时**不禁止提交**，而是把风险显式写进主按钮文案，提交动作即代表「异常已知晓」——与原型 L2436 按钮文案一致。

## 7. 交互与状态设计

| 交互 | 规格 |
|------|------|
| 打开 | 点确认卡「展开完整单据」→ `wbOpen=true`；倒计时（remain）由 ChatTurn 持有并透传，弹层内按钮状态与摘要卡实时同步 |
| 关闭 | 右上角 × / 遮罩 / Esc（antd 默认）→ `onClose`；仅关闭视图，**不中断确认流程**（倒计时继续，再次打开状态正确） |
| 提交/取消 | 复用确认卡同一 `onConfirm` 入口；提交成功后弹层 footer 切「已确认」Tag，用户可关闭 |
| busy | confirmBusy 期间两按钮均 loading，防重复提交 |
| 过滤 | 「仅看异常行」即点即切，无需刷新 |

## 8. 数据契约

### 8.1 MVP 数据源（已实现，零后端改动）

| 弹层数据 | 来源 |
|----------|------|
| 标题 | `confirm.payload.skill_title` |
| 草稿版本 | `draft.draft_version` |
| 主表字段 | `draft.draft`（key→`{value, source?}`），跳过 items/HIDDEN_FIELDS |
| 子表行 | `draft.draft.items`（`{sku, qty, price?}`） |
| 缺失必填 | `draft.missing_fields[]` |
| 确认快照回退 | `confirm.payload.fields` |
| 状态机 | `turn.confirmState`（pending/confirmed/rejected/expired）+ `confirm.confirm_token` + `confirm.expires_at` |

### 8.2 Phase 2 事件契约（[chat-events.ts](../packages/protocol/events/chat-events.ts#L64-L71) DocWorkbenchEvent，驱动下轮扩展）

```ts
interface DocWorkbenchEvent {
  type: 'doc_workbench'
  /** 主表（含分组元数据 groups，驱动分组折叠与审计视图） */
  header: Record<string, unknown>
  /** 子表行（每行 material_ref 指向已选物料候选；孙表 batches 懒加载） */
  lines: Array<Record<string, unknown>>
}
```

Phase 2 扩展点：分组折叠（groups 元数据替换前端白名单）、物料候选联动（material_candidates 事件）、孙表懒加载（batches）、行内编辑回写对话。

## 9. MVP 范围边界（明确不做）

- 孙表（批次分配）与行展开交互
- 审计视图（全部字段折叠面板）
- 双轨编辑 / 行内编辑
- 虚拟滚动（子表万行场景）
- 大额阈值、交货期、物料名称/规格列
- 弹层独立路由 / 深链（仅组件态 open 控制）

## 10. 验收清单（MVP 已验收 ✅）

- [x] 确认卡出现「展开完整单据」入口，点击打开全屏弹层（1140px 居中）
- [x] 主表按分组渲染，字段中文标签 + 值 + 来源 Tag；白名单外字段落入「其他」组
- [x] 子表渲染 sku/qty/price/金额，异常行黄底 + 橙 Tag
- [x] 「仅看异常行」过滤生效
- [x] 标题展示草稿版本与异常计数；footer 展示合计与缺失必填提示
- [x] 有异常时主按钮文案为「异常已知晓 · 确认提交」
- [x] 确认/取消/失效/已确认四态正确切换，busy 防重复
- [x] 恢复会话（无 draft）回退渲染确认快照平铺字段
- [x] 关闭弹层不中断倒计时与确认流程
