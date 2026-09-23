// Mock 自动化信箱（PLAN P3.4，PRD 3.6.2）
// 字段与 automation/store.py InboxEntry 精确对齐：{id,kind,task_id,task_name,skill,user_id,ok,text,run_at,read}
import type { InboxEntry } from '@/types/chat';

interface InboxBucket {
  items: InboxEntry[];
}

function seed(): InboxBucket {
  return {
    items: [
      {
        id: 'inbox_1001',
        kind: 'result',
        task_id: 'task_daily_stock',
        task_name: '每日库存水位检查',
        skill: 'erp.query_stock',
        user_id: 'emp001',
        ok: true,
        text: '库存检查完成：2 项低于安全水位（M-100238 伺服电机 1.5kW 余 5/安全 8；M-300112 密封圈 余 12/安全 20），已生成补货建议。',
        run_at: '2026-09-23T08:00:00+08:00',
        read: false,
      },
      {
        id: 'inbox_1002',
        kind: 'result',
        task_id: 'task_daily_stock',
        task_name: '每日库存水位检查',
        skill: 'erp.query_stock',
        user_id: 'emp001',
        ok: true,
        text: '库存检查完成：全部物料高于安全水位，无需补货。',
        run_at: '2026-09-22T08:00:00+08:00',
        read: true,
      },
      {
        id: 'inbox_1003',
        kind: 'pause',
        task_id: 'task_po_track',
        task_name: '采购单到货跟踪',
        skill: 'mes.query_po_status',
        user_id: 'emp001',
        ok: false,
        text: '任务已自动暂停：连续 3 次调用 mes.query_po_status 失败（网络超时），请检查 MES 网关后在「我的」页恢复。',
        run_at: '2026-09-21T14:30:00+08:00',
        read: false,
      },
      {
        id: 'inbox_1004',
        kind: 'result',
        task_id: 'task_po_track',
        task_name: '采购单到货跟踪',
        skill: 'mes.query_po_status',
        user_id: 'emp001',
        ok: true,
        text: 'PO-20260915-011 已发货（预计 09-25 到仓），PO-20260918-002 在途，PO-20260920-007 已签收。',
        run_at: '2026-09-21T09:00:00+08:00',
        read: true,
      },
      {
        id: 'inbox_1005',
        kind: 'result',
        task_id: 'task_dept_report',
        task_name: '周度工单产出汇总',
        skill: 'mes.weekly_output',
        user_id: 'emp001',
        ok: true,
        text: '本周一至周五装配车间产出 1,284 台，一次通过率 98.2%，环比 +1.1pct；明细已导出至报表中心。',
        run_at: '2026-09-20T18:00:00+08:00',
        read: true,
      },
      {
        id: 'inbox_1006',
        kind: 'result',
        task_id: 'task_u8_recon',
        task_name: '应收对账抽查',
        skill: 'u8.query_receivable',
        user_id: 'emp001',
        ok: false,
        text: '对账异常：客户「苏州宏达机械」9 月发票累计 428,000 元，回款仅 120,000 元，逾期 35 天，建议跟进催收。',
        run_at: '2026-09-20T16:00:00+08:00',
        read: false,
      },
      {
        id: 'inbox_1007',
        kind: 'result',
        task_id: 'task_material_watch',
        task_name: '关键物料价格监控',
        skill: 'erp.price_watch',
        user_id: 'emp001',
        ok: true,
        text: '价格监控：铝型材协议价本月上调 3.2%（供应商：华金铝业），涉及在途采购单 2 张，价差合计 +4,860 元。',
        run_at: '2026-09-19T10:00:00+08:00',
        read: true,
      },
      {
        id: 'inbox_1008',
        kind: 'result',
        task_id: 'task_daily_stock',
        task_name: '每日库存水位检查',
        skill: 'erp.query_stock',
        user_id: 'emp001',
        ok: true,
        text: '库存检查完成：M-300112 密封圈 低于安全水位（余 15/安全 20），已生成补货建议 PR-20260918-004。',
        run_at: '2026-09-18T08:00:00+08:00',
        read: true,
      },
      {
        id: 'inbox_1009',
        kind: 'pause',
        task_id: 'task_u8_recon',
        task_name: '应收对账抽查',
        skill: 'u8.query_receivable',
        user_id: 'emp001',
        ok: false,
        text: '任务已自动暂停：U8 凭证接口返回权限不足（403），请联系财务系统管理员开通 query_receivable 范围。',
        run_at: '2026-09-17T11:20:00+08:00',
        read: true,
      },
      {
        id: 'inbox_1010',
        kind: 'result',
        task_id: 'task_dept_report',
        task_name: '周度工单产出汇总',
        skill: 'mes.weekly_output',
        user_id: 'emp001',
        ok: true,
        text: '上周装配车间产出 1,210 台，一次通过率 97.1%；机加车间产出 682 件，准交率 95.4%。',
        run_at: '2026-09-13T18:00:00+08:00',
        read: true,
      },
      {
        id: 'inbox_1011',
        kind: 'result',
        task_id: 'task_material_watch',
        task_name: '关键物料价格监控',
        skill: 'erp.price_watch',
        user_id: 'emp001',
        ok: true,
        text: '价格监控：本周 12 项监控物料价格无变化，1 项下调（M-200401 轴承钢 -1.8%）。',
        run_at: '2026-09-12T10:00:00+08:00',
        read: true,
      },
      {
        id: 'inbox_1012',
        kind: 'result',
        task_id: 'task_po_track',
        task_name: '采购单到货跟踪',
        skill: 'mes.query_po_status',
        user_id: 'emp001',
        ok: true,
        text: 'PO-20260910-003 部分到货（8/20 台），余量预计 09-28 到仓，已在 MES 登记到货计划。',
        run_at: '2026-09-11T09:00:00+08:00',
        read: true,
      },
    ],
  };
}

const bucket = seed();

/** 演示模式：信箱列表（新在前，可选只看未读） */
export function inboxList(unreadOnly = false): InboxEntry[] {
  const items = [...bucket.items].sort((a, b) => (a.run_at < b.run_at ? 1 : -1));
  return unreadOnly ? items.filter((i) => !i.read) : items;
}

/** 演示模式：全部已读 */
export function inboxMarkAllRead(): number {
  let marked = 0;
  for (const item of bucket.items) {
    if (!item.read) {
      item.read = true;
      marked += 1;
    }
  }
  return marked;
}
