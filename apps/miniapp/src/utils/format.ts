// 格式化工具（PLAN P3.4）
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import relativeTime from 'dayjs/plugin/relativeTime';

dayjs.extend(relativeTime);
dayjs.locale('zh-cn');

/** 相对时间（如「3 分钟前」） */
export function formatRelative(isoOrMs: string | number): string {
  return dayjs(isoOrMs).fromNow();
}

/** 紧凑时间（今天显示 HH:mm，否则 MM-DD HH:mm） */
export function formatCompactTime(isoOrMs: string | number): string {
  const d = dayjs(isoOrMs);
  if (d.isSame(dayjs(), 'day')) {
    return d.format('HH:mm');
  }
  if (d.isSame(dayjs(), 'year')) {
    return d.format('MM-DD HH:mm');
  }
  return d.format('YYYY-MM-DD HH:mm');
}

/** 金额格式化：千分位 + 两位小数 */
export function formatMoney(n: number): string {
  return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** 短 ID 生成器（消息/会话键） */
export function uid(prefix = 'id'): string {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

/** 截断文本（卡片摘要） */
export function truncate(text: string, max = 40): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}…`;
}
