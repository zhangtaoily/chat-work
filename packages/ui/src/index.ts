// @chat-work/ui：业务卡片组件库（唯一 UI 重复消除点，ARCHITECTURE 第 3 章边界规则）
// 桌面 renderer 按 SSE 事件类型（packages/protocol/events）分发到对应卡片。
// TODO 计划组件（全部占位）：
//   - ConfirmCard     确认卡（字段来源徽标：客户默认/追问/记忆恢复/自动计算）
//   - ApprovalCard    审批卡（待办审批视图）
//   - BiCard          BI 查询结果卡（表格/图表）
//   - MaterialCard    物料歧义候选选择卡（Top-N 必选不猜，Phase 2）
//   - SummaryCard     单据摘要卡（行数/金额/异常警示）
//   - DiffCard        变更卡（旧值删除线 → 新值，金额联动重算）
//   - Workbench       全屏单据工作台（分组折叠/子表虚拟滚动/孙表钻取，Phase 2）
export const UI_PACKAGE_VERSION = '0.1.0'
