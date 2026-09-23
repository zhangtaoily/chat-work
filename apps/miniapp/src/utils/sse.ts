// SSE 流式解析（PLAN P3.4，PRD 7.3 移动端）
// H5：fetch + ReadableStream；微信：Taro.request enableChunked + 手写 UTF-8 增量解码
import Taro from '@tarojs/taro';
import type { ChatEvent, ChatRequest } from '@/types/chat';
import { getSettings } from '@/store/settings';

/** 固定客户端版本（灰度门禁 X-Client-Version，MIN_CLIENT_VERSION 未设时不启用） */
export const CLIENT_VERSION = '1.0.0';

/** 解析单条 SSE data 行为事件（[DONE] 哨兵返回 null） */
function parseEventLine(line: string): ChatEvent | null {
  const trimmed = line.trim();
  if (!trimmed || trimmed === '[DONE]') return null;
  if (!trimmed.startsWith('data:')) return null;
  const json = trimmed.slice(5).trim();
  if (!json || json === '[DONE]') return null;
  try {
    return JSON.parse(json) as ChatEvent;
  } catch {
    return null;
  }
}

function commonHeaders(): Record<string, string> {
  const { bearerToken } = getSettings();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Client-Version': CLIENT_VERSION,
  };
  if (bearerToken) {
    // X-Chat-Auth 兼容头（PRD 8.5：Bearer / X-Chat-Auth 双通道）
    headers['X-Chat-Auth'] = bearerToken;
  }
  return headers;
}

/** 从错误响应体提取可读信息（灰度门禁 403 等） */
async function errorFromResponse(status: number, body: string): Promise<Error> {
  try {
    const data = JSON.parse(body) as { code?: string; message?: string; detail?: string };
    return new Error(data.message || data.detail || `请求失败（${status}）`);
  } catch {
    return new Error(`请求失败（${status}）`);
  }
}

// ---- H5 实现：fetch + ReadableStream ----

async function streamChatH5(req: ChatRequest, onEvent: (ev: ChatEvent) => void): Promise<void> {
  const { apiBase } = getSettings();
  const res = await fetch(`${apiBase}/chat`, {
    method: 'POST',
    headers: commonHeaders(),
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    throw await errorFromResponse(res.status, await res.text());
  }
  const reader = res.body?.getReader();
  if (!reader) {
    throw new Error('当前环境不支持流式响应');
  }
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split('\n\n');
    buffer = parts.pop() || '';
    for (const part of parts) {
      for (const line of part.split('\n')) {
        const ev = parseEventLine(line);
        if (ev) onEvent(ev);
      }
    }
  }
  buffer += decoder.decode();
  for (const line of buffer.split('\n')) {
    const ev = parseEventLine(line);
    if (ev) onEvent(ev);
  }
}

// ---- 微信实现：Taro.request enableChunked + 手写 UTF-8 增量解码 ----

/** 判断字节序列尾部是否有未完成的多字节 UTF-8 前缀，返回需保留的字节数（0~3） */
function incompleteTailCount(bytes: Uint8Array): number {
  const n = bytes.length;
  for (let keep = 1; keep <= 3 && keep <= n; keep += 1) {
    const b = bytes[n - keep];
    if (b >= 0xc0) return keep; // 多字节起始字节
    if (b < 0x80) return 0; // ASCII
  }
  return 0;
}

/** UTF-8 字节序列解码为字符串（字节输入保证为完整序列） */
function utf8Decode(bytes: Uint8Array): string {
  let out = '';
  let i = 0;
  while (i < bytes.length) {
    const b = bytes[i];
    if (b < 0x80) {
      out += String.fromCharCode(b);
      i += 1;
    } else if (b < 0xe0) {
      out += String.fromCharCode(((b & 0x1f) << 6) | (bytes[i + 1] & 0x3f));
      i += 2;
    } else if (b < 0xf0) {
      out += String.fromCharCode(
        ((b & 0x0f) << 12) | ((bytes[i + 1] & 0x3f) << 6) | (bytes[i + 2] & 0x3f)
      );
      i += 3;
    } else {
      let cp =
        ((b & 0x07) << 18) |
        ((bytes[i + 1] & 0x3f) << 12) |
        ((bytes[i + 2] & 0x3f) << 6) |
        (bytes[i + 3] & 0x3f);
      cp -= 0x10000;
      out += String.fromCharCode(0xd800 + (cp >> 10), 0xdc00 + (cp & 0x3ff));
      i += 4;
    }
  }
  return out;
}

function toBytes(data: ArrayBuffer | string): Uint8Array {
  if (typeof data === 'string') {
    // 部分环境直接回传字符串
    const out = new Uint8Array(data.length);
    for (let i = 0; i < data.length; i += 1) out[i] = data.charCodeAt(i) & 0xff;
    return out;
  }
  return new Uint8Array(data);
}

async function streamChatWeapp(req: ChatRequest, onEvent: (ev: ChatEvent) => void): Promise<void> {
  const { apiBase } = getSettings();
  let pending: Uint8Array = new Uint8Array(0);
  let textBuffer = '';

  const consume = (bytes: Uint8Array) => {
    // 合并上一次未完成的多字节尾部
    if (pending.length > 0) {
      const merged = new Uint8Array(pending.length + bytes.length);
      merged.set(pending, 0);
      merged.set(bytes, pending.length);
      bytes = merged;
      pending = new Uint8Array(0);
    }
    const keep = incompleteTailCount(bytes);
    if (keep > 0) {
      pending = bytes.slice(bytes.length - keep);
      bytes = bytes.slice(0, bytes.length - keep);
    }
    textBuffer += utf8Decode(bytes);
    const parts = textBuffer.split('\n\n');
    textBuffer = parts.pop() || '';
    for (const part of parts) {
      for (const line of part.split('\n')) {
        const ev = parseEventLine(line);
        if (ev) onEvent(ev);
      }
    }
  };

  return new Promise<void>((resolve, reject) => {
    const task = Taro.request({
      url: `${apiBase}/chat`,
      method: 'POST',
      header: commonHeaders(),
      data: req,
      enableChunked: true,
      responseType: 'arraybuffer',
      success: () => resolve(),
      fail: (err) => reject(new Error(err.errMsg || '网络请求失败')),
    });
    // 状态码门禁（灰度拒绝 403 等）：statusCodeChunked 时跳过事件解析
    task.onChunkReceived((res: { data: ArrayBuffer | string }) => {
      consume(toBytes(res.data));
    });
  });
}

/**
 * 发起对话并流式接收事件（SSE，POST /chat）
 * 结束哨兵 data: [DONE] 由后端发送，解析层自动忽略
 */
export async function streamChat(
  req: ChatRequest,
  onEvent: (ev: ChatEvent) => void
): Promise<void> {
  if (process.env.TARO_ENV === 'h5') {
    await streamChatH5(req, onEvent);
  } else {
    await streamChatWeapp(req, onEvent);
  }
}
