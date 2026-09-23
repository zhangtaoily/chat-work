// REST 客户端（PLAN P3.4）
// 演示模式走 data/ mock（可变内存态），真实模式走 Taro.request 直连 agent_core
// 字段与 services/agent_core/api/main.py 精确对齐，切换零改动
import Taro from '@tarojs/taro';
import { getSettings } from '@/store/settings';
import { CLIENT_VERSION } from '@/utils/sse';
import * as mockMemory from '@/data/memory';
import * as mockInbox from '@/data/inbox';
import type { MemoryListResponse, InboxEntry } from '@/types/chat';

type Method = 'GET' | 'POST' | 'DELETE';

function commonHeader(): Record<string, string> {
  const { bearerToken } = getSettings();
  const header: Record<string, string> = { 'X-Client-Version': CLIENT_VERSION };
  if (bearerToken) {
    header['X-Chat-Auth'] = bearerToken;
  }
  return header;
}

/** FastAPI 错误体兼容（detail 为字符串或校验数组） */
function readableError(status: number, data: unknown): Error {
  const body = data as { code?: string; message?: string; detail?: string | unknown[] };
  if (body?.message) return new Error(body.message);
  if (typeof body?.detail === 'string') return new Error(body.detail);
  return new Error(`请求失败（${status}）`);
}

async function request<T>(method: Method, path: string, data?: unknown): Promise<T> {
  const { apiBase } = getSettings();
  const res = await Taro.request({
    url: `${apiBase}${path}`,
    method,
    data,
    header: commonHeader(),
  });
  if (res.statusCode >= 400) {
    throw readableError(res.statusCode, res.data);
  }
  return res.data as T;
}

// ---- 记忆面板（PRD 9.2/9.3）----

export async function fetchMemoryList(layer?: string): Promise<MemoryListResponse> {
  const { demoMode } = getSettings();
  if (demoMode) {
    await delay(200);
    return mockMemory.memoryList(layer);
  }
  const qs = layer ? `?layer=${encodeURIComponent(layer)}` : '';
  return request<MemoryListResponse>('GET', `/memory/list${qs}`);
}

export async function addMemory(content: string, kind = 'preference'): Promise<void> {
  const { demoMode } = getSettings();
  if (demoMode) {
    await delay(200);
    mockMemory.memoryAdd(content, kind);
    return;
  }
  await request('POST', '/memory/add', { content, kind });
}

export async function deleteMemory(entryId: string): Promise<void> {
  const { demoMode } = getSettings();
  if (demoMode) {
    await delay(150);
    mockMemory.memoryRemove(entryId);
    return;
  }
  await request('DELETE', `/memory/${entryId}`);
}

export async function clearMemory(): Promise<number> {
  const { demoMode } = getSettings();
  if (demoMode) {
    await delay(200);
    return mockMemory.memoryClear();
  }
  const res = await request<{ purged: number }>('POST', '/memory/clear');
  return res.purged;
}

export async function setMemoryConsent(granted: boolean): Promise<{ purged: number }> {
  const { demoMode } = getSettings();
  if (demoMode) {
    await delay(200);
    return mockMemory.memorySetConsent(granted);
  }
  const res = await request<{ granted: boolean; purged: number }>('POST', '/memory/consent', {
    granted,
  });
  return { purged: res.purged };
}

/** 记忆导出地址（真实模式返回带 Token 的 URL，演示模式返回 null） */
export function memoryExportUrl(): string | null {
  const { demoMode, apiBase, bearerToken } = getSettings();
  if (demoMode) return null;
  const tokenParam = bearerToken ? `?token=${encodeURIComponent(bearerToken)}` : '';
  return `${apiBase}/memory/export.md${tokenParam}`;
}

// ---- 自动化信箱（PRD 3.6.2）----

export async function fetchInbox(unreadOnly = false): Promise<InboxEntry[]> {
  const { demoMode } = getSettings();
  if (demoMode) {
    await delay(200);
    return mockInbox.inboxList(unreadOnly);
  }
  const res = await request<{ items: InboxEntry[] }>(
    'GET',
    `/automations/inbox?limit=50&unread_only=${unreadOnly}`
  );
  return res.items;
}

export async function markInboxRead(): Promise<number> {
  const { demoMode } = getSettings();
  if (demoMode) {
    await delay(150);
    return mockInbox.inboxMarkAllRead();
  }
  const res = await request<{ marked: number }>('POST', '/automations/inbox/read');
  return res.marked;
}

// ---- HITL 确认（PRD 4.1 / 8.7）----

export interface ConfirmResult {
  ok: boolean
  /** 后端恢复执行后的摘要文本（确认卡分支返回 resumption 结果） */
  text: string
}

/** 处置确认卡/计划卡：POST /confirmations/{token}（演示模式本地模拟成功） */
export async function postConfirmation(
  token: string,
  action: 'confirm' | 'reject',
  modified?: Record<string, unknown>
): Promise<ConfirmResult> {
  const { demoMode } = getSettings();
  if (demoMode) {
    await delay(400);
    return {
      ok: true,
      text: action === 'confirm' ? '演示模式：已确认并恢复执行（mock）' : '演示模式：已驳回（mock）',
    };
  }
  const res = await request<{ status: string; result?: Record<string, unknown> }>(
    'POST',
    `/confirmations/${token}`,
    { action, ...(modified ? { modified } : {}) }
  );
  const summary = (res.result as { summary?: string } | undefined)?.summary || res.status;
  return { ok: true, text: summary };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
