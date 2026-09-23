// Mock 审批数据（PLAN P3.4）
// 演示模式注入的待确认卡（字段与真实 confirm_card payload 一致）+ 预置处置历史
import type { PendingConfirm, ConfirmHistory } from '@/store/confirmations';

const now = Date.now();

/** 演示待确认卡（真实模式不注入，由对话流 confirm_card/plan_card 自动沉淀） */
export function seedPending(): PendingConfirm[] {
  return [
    {
      token: 'demo-token-po-001',
      kind: 'confirm',
      title: '采购订单 PO-20260923-001',
      payload: {
        doc_type: '采购订单',
        doc_no: 'PO-20260923-001',
        supplier: '华东精密',
        material: 'M-100236 伺服电机 750W',
        qty: 10,
        amount: 12800,
        required_date: '2026-10-08',
      },
      expiresAt: now + 10 * 60 * 1000,
      createdAt: now - 3 * 60 * 1000,
    },
    {
      token: 'demo-token-leave-001',
      kind: 'confirm',
      title: '请假单（2 天）',
      payload: {
        doc_type: '请假单',
        leave_type: '事假',
        start_date: '2026-09-24',
        end_date: '2026-09-25',
        days: 2,
        reason: '家中事务',
      },
      expiresAt: now + 8 * 60 * 1000,
      createdAt: now - 5 * 60 * 1000,
    },
    {
      token: 'demo-token-plan-001',
      kind: 'plan',
      title: '执行计划：月度领料汇总（3 步）',
      payload: {
        skill_title: '月度领料汇总',
        steps: [
          { tool: 'mes.query_material_issues', rw: 'read', requires_confirm: false },
          { tool: 'erp.aggregate_by_dept', rw: 'read', requires_confirm: false },
          { tool: 'report.export', rw: 'write', requires_confirm: true },
        ],
      },
      expiresAt: now + 9 * 60 * 1000,
      createdAt: now - 1 * 60 * 1000,
    },
    {
      token: 'demo-token-pay-001',
      kind: 'confirm',
      title: '付款单 PAY-20260922-014',
      payload: {
        doc_type: '付款单',
        doc_no: 'PAY-20260922-014',
        supplier: '南方数控',
        amount: 86400,
        currency: 'CNY',
        account: '工行 6222***8871',
      },
      expiresAt: now + 6 * 60 * 1000,
      createdAt: now - 12 * 60 * 1000,
    },
    {
      token: 'demo-token-reimb-001',
      kind: 'confirm',
      title: '差旅报销 TR-20260921-006',
      payload: {
        doc_type: '差旅报销',
        doc_no: 'TR-20260921-006',
        trip: '上海→苏州 客户现场支持',
        hotel: 680,
        transport: 156,
        total: 836,
      },
      expiresAt: now + 4 * 60 * 1000,
      createdAt: now - 18 * 60 * 1000,
    },
  ];
}

/** 预置处置历史（仅演示模式首屏展示） */
export function seedHistory(): ConfirmHistory[] {
  return [
    {
      token: 'demo-token-po-009',
      title: '采购订单 PO-20260920-009',
      action: 'confirm',
      ok: true,
      at: now - 26 * 60 * 60 * 1000,
    },
    {
      token: 'demo-token-leave-003',
      title: '请假单（1 天）',
      action: 'reject',
      ok: true,
      at: now - 30 * 60 * 60 * 1000,
    },
    {
      token: 'demo-token-pay-008',
      title: '付款单 PAY-20260919-008',
      action: 'confirm',
      ok: true,
      at: now - 50 * 60 * 60 * 1000,
    },
  ];
}
