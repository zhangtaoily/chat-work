// 待确认卡状态（PLAN P3.4，PRD 4.1 HITL）
// 确认卡/计划卡从对话流沉淀到此处，审批 Tab 聚合展示，POST /confirmations/{token} 处置
import { create } from 'zustand';
import type { ConfirmCardEvent, PlanCardEvent } from '@/types/chat';

export interface PendingConfirm {
  /** 统一令牌：confirm_card 用 confirm_token，plan_card 用 plan_token */
  token: string;
  kind: 'confirm' | 'plan';
  payload: Record<string, unknown>;
  expiresAt: number;
  /** 发起问题（用于列表摘要展示） */
  title: string;
  createdAt: number;
}

/** 处置历史（本地展示用） */
export interface ConfirmHistory {
  token: string;
  title: string;
  action: 'confirm' | 'reject';
  /** 后端处置是否成功（演示模式恒 true） */
  ok: boolean;
  at: number;
}

interface ConfirmationsState {
  items: PendingConfirm[];
  history: ConfirmHistory[];
  upsert: (item: PendingConfirm) => void;
  /** 处置完成：移出待办并记录历史 */
  settle: (token: string, title: string, action: 'confirm' | 'reject', ok: boolean) => void;
  clearExpired: () => void;
  /** 演示模式批量注入种子数据 */
  seed: (items: PendingConfirm[]) => void;
  /** 演示模式注入处置历史 */
  seedHistory: (items: ConfirmHistory[]) => void;
}

/** 从事件中提取标题（计划卡取 skill_title，确认卡取单据类型+编号） */
export function titleOfEvent(ev: ConfirmCardEvent | PlanCardEvent): string {
  if (ev.type === 'plan_card') {
    const p = ev.payload as { skill_title?: string; steps?: unknown[] };
    return `执行计划：${p.skill_title || '未命名技能'}（${(p.steps || []).length} 步）`;
  }
  const p = ev.payload as Record<string, unknown>;
  const docNo = (p.doc_no || p.order_no || '') as string;
  const docType = (p.doc_type || '单据') as string;
  return docNo ? `${docType} ${docNo}` : `${docType} 确认`;
}

export const useConfirmations = create<ConfirmationsState>((set) => ({
  items: [],
  history: [],
  upsert: (item) =>
    set((s) => {
      const rest = s.items.filter((i) => i.token !== item.token);
      return { items: [item, ...rest] };
    }),
  settle: (token, title, action, ok) =>
    set((s) => ({
      items: s.items.filter((i) => i.token !== token),
      history: [{ token, title, action, ok, at: Date.now() }, ...s.history].slice(0, 20),
    })),
  clearExpired: () =>
    set((s) => ({ items: s.items.filter((i) => i.expiresAt > Date.now()) })),
  seed: (items) => set({ items }),
  seedHistory: (items) => set({ history: items }),
}));
