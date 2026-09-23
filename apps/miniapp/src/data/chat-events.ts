// Mock 对话事件序列（PLAN P3.4）
// 按关键词路由到不同剧本，覆盖全部 8 类 SSE 事件；字段与真实流水线一致
import type { ChatEvent } from '@/types/chat';

/** 演示剧本：请假申请（走 HITL 确认卡 → final 成功卡） */
function scenarioLeave(): ChatEvent[] {
  const now = Date.now();
  return [
    { type: 'stage_progress', stage: 'intent', message: '正在识别意图…' },
    { type: 'stage_progress', stage: 'route', message: '已路由：请假技能（leave.apply）' },
    {
      type: 'draft_card',
      draft: {
        leave_type: '事假',
        start_date: '2026-09-24',
        end_date: '2026-09-25',
        days: 2,
        reason: '家中事务',
      },
      missing_fields: [],
      draft_version: 1,
    },
    {
      type: 'confirm_card',
      confirm_token: 'demo-token-leave-001',
      payload: {
        doc_type: '请假单',
        leave_type: '事假',
        start_date: '2026-09-24',
        end_date: '2026-09-25',
        days: 2,
        reason: '家中事务',
      },
      expires_at: now + 10 * 60 * 1000,
    },
    {
      type: 'final',
      text: '请假单草稿已生成并挂起确认。你可以到「审批」Tab 里确认或驳回，也可以直接在对话里说「确认提交」。',
      cards: [],
    },
  ];
}

/** 演示剧本：采购下单（物料候选 → 修改 diff → 确认卡） */
function scenarioPurchase(): ChatEvent[] {
  const now = Date.now();
  return [
    { type: 'stage_progress', stage: 'intent', message: '正在识别意图…' },
    { type: 'stage_progress', stage: 'extract', message: '正在提取参数…' },
    {
      type: 'draft_card',
      draft: { supplier: '华东精密', doc_date: '2026-09-23' },
      missing_fields: ['material', 'qty'],
      draft_version: 1,
    },
    {
      type: 'material_candidates',
      line_key: 'line_1',
      candidates: [
        { code: 'M-100236', name: '伺服电机 750W', spec: 'MSMF082', stock: 42, price: 1280 },
        { code: 'M-100237', name: '伺服电机 400W', spec: 'MSMF042', stock: 17, price: 920 },
        { code: 'M-100238', name: '伺服电机 1.5kW', spec: 'MSMF152', stock: 5, price: 2160 },
      ],
    },
    { type: 'stage_progress', stage: 'hitl', message: '等待用户确认…' },
    {
      type: 'diff_card',
      changes: [{ field: 'qty', old_value: 1, new_value: 10 }],
      draft_version: 2,
    },
    {
      type: 'confirm_card',
      confirm_token: 'demo-token-po-001',
      payload: {
        doc_type: '采购订单',
        doc_no: 'PO-20260923-001',
        supplier: '华东精密',
        material: 'M-100236 伺服电机 750W',
        qty: 10,
        amount: 12800,
      },
      expires_at: now + 10 * 60 * 1000,
    },
    { type: 'final', text: '采购订单已就绪，等待你在「审批」Tab 确认后写入 ERP。', cards: [] },
  ];
}

/** 演示剧本：查询库存（只读技能，直出结果） */
function scenarioQuery(): ChatEvent[] {
  return [
    { type: 'stage_progress', stage: 'intent', message: '正在识别意图…' },
    { type: 'stage_progress', stage: 'execute', message: '调用库存查询技能…' },
    {
      type: 'final',
      text: '查询完成：\n· M-100236 伺服电机 750W — 库存 42 台（原料库 A-03）\n· M-100237 伺服电机 400W — 库存 17 台（原料库 A-04）\n· M-100238 伺服电机 1.5kW — 库存 5 台（原料库 A-05，低于安全水位 8）',
      cards: [{ kind: 'query_summary', title: '库存查询', count: 3 }],
    },
  ];
}

/** 演示剧本：计划模式（plan_card → 批准后执行） */
function scenarioPlan(): ChatEvent[] {
  const now = Date.now();
  return [
    { type: 'stage_progress', stage: 'intent', message: '正在识别意图…' },
    { type: 'stage_progress', stage: 'route', message: 'Plan 模式：先生成执行计划' },
    {
      type: 'plan_card',
      plan_token: 'demo-token-plan-001',
      payload: {
        skill_title: '月度领料汇总',
        steps: [
          { tool: 'mes.query_material_issues', rw: 'read', requires_confirm: false },
          { tool: 'erp.aggregate_by_dept', rw: 'read', requires_confirm: false },
          { tool: 'report.export', rw: 'write', requires_confirm: true },
        ],
      },
      expires_at: now + 10 * 60 * 1000,
    },
    { type: 'final', text: '执行计划已生成，请到「审批」Tab 批准后开始执行。', cards: [] },
  ];
}

/** 演示剧本：doc_workbench 单据工作台（移动端精简摘要） */
function scenarioWorkbench(): ChatEvent[] {
  return [
    { type: 'stage_progress', stage: 'intent', message: '正在识别意图…' },
    { type: 'stage_progress', stage: 'extract', message: '正在解析单据…' },
    {
      type: 'doc_workbench',
      header: {
        doc_type: '采购订单',
        doc_no: 'PO-20260922-018',
        supplier: '南方数控',
        amount: 86400,
        status: 'draft',
      },
      lines: [
        { no: 1, material: 'M-200415 直线导轨 HGR25', qty: 20, price: 1800, amount: 36000 },
        { no: 2, material: 'M-200416 滚珠丝杠 SFU40', qty: 12, price: 4200, amount: 50400 },
      ],
    },
    {
      type: 'final',
      text: '单据已解析为工作台视图（移动端为只读摘要，完整编辑请在桌面端打开「单据工作台」）。',
      cards: [],
    },
  ];
}

/** 默认剧本：闲聊 / 兜底 */
function scenarioDefault(message: string): ChatEvent[] {
  return [
    { type: 'stage_progress', stage: 'intent', message: '正在识别意图…' },
    {
      type: 'final',
      text: `已收到你的消息（演示模式）。当前演示剧本支持：\n· 「帮我请假」— 请假确认流\n· 「下采购单」— 物料候选 + 采购确认\n· 「查库存」— 只读查询\n· 「生成月度领料汇总」— 计划模式\n· 「解析采购订单」— 单据工作台\n\n你说的是：${message}`,
      cards: [],
    },
  ];
}

/** 按消息关键词路由演示剧本（与真实 API 返回结构完全一致） */
export function mockStreamReply(message: string): ChatEvent[] {
  const m = message.toLowerCase();
  if (m.includes('请假') || m.includes('休假') || m.includes('调休')) return scenarioLeave();
  if (m.includes('采购') || m.includes('下单') || m.includes('订货')) return scenarioPurchase();
  if (m.includes('库存') || m.includes('查询') || m.includes('查一下')) return scenarioQuery();
  if (m.includes('计划') || m.includes('汇总') || m.includes('方案')) return scenarioPlan();
  if (m.includes('解析') || m.includes('工作台') || m.includes('单据')) return scenarioWorkbench();
  return scenarioDefault(message);
}
