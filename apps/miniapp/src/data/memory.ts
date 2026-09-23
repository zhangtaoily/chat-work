// Mock 记忆数据（PLAN P3.4，PRD 9.2/9.3）
// 字段与 memory/store.py MemoryEntry 精确对齐：
// {id,layer,owner,dept,kind,content,source,status,decayed,created_by,created_at,last_used_at,use_count}
import type { MemoryEntry, MemoryListResponse, MemoryStats } from '@/types/chat';

const NOW = new Date('2026-09-23T10:00:00+08:00').getTime();
const daysAgo = (d: number) => new Date(NOW - d * 24 * 60 * 60 * 1000).toISOString();

interface MemoryBucket {
  entries: MemoryEntry[];
  consent: { granted: boolean; at: string | null };
  seq: number;
}

function seed(): MemoryBucket {
  return {
    consent: { granted: true, at: '2026-09-01T09:12:00+08:00' },
    seq: 100,
    entries: [
      {
        id: 'mem_001',
        layer: 'L2',
        owner: 'emp001',
        dept: '装配车间',
        kind: 'preference',
        content: '我习惯用「两班倒产能」口径看车间产出，不要用日历工时。',
        source: 'auto_consolidated',
        status: 'active',
        decayed: false,
        created_by: 'emp001',
        created_at: daysAgo(3),
        last_used_at: daysAgo(1),
        use_count: 6,
      },
      {
        id: 'mem_002',
        layer: 'L2',
        owner: 'emp001',
        dept: '装配车间',
        kind: 'preference',
        content: '导出报表默认 Excel 格式，文件名带日期后缀。',
        source: 'ask',
        status: 'active',
        decayed: false,
        created_by: 'emp001',
        created_at: daysAgo(6),
        last_used_at: daysAgo(2),
        use_count: 3,
      },
      {
        id: 'mem_003',
        layer: 'L2',
        owner: 'emp001',
        dept: '装配车间',
        kind: 'context',
        content: '我负责 M2 产线（伺服装配）的排产跟进。',
        source: 'ask',
        status: 'active',
        decayed: false,
        created_by: 'emp001',
        created_at: daysAgo(12),
        last_used_at: daysAgo(4),
        use_count: 9,
      },
      {
        id: 'mem_004',
        layer: 'L2',
        owner: 'emp001',
        dept: '装配车间',
        kind: 'preference',
        content: '金额超 5 万的采购单，提醒我先核对协议价。',
        source: 'auto_consolidated',
        status: 'active',
        decayed: false,
        created_by: 'emp001',
        created_at: daysAgo(20),
        last_used_at: daysAgo(8),
        use_count: 2,
      },
      {
        id: 'mem_005',
        layer: 'L2',
        owner: 'emp001',
        dept: '装配车间',
        kind: 'context',
        content: '每周五 17:00 前要交周度产出汇总给王经理。',
        source: 'ask',
        status: 'active',
        decayed: false,
        created_by: 'emp001',
        created_at: daysAgo(45),
        last_used_at: daysAgo(3),
        use_count: 11,
      },
      {
        id: 'mem_006',
        layer: 'L3',
        owner: '装配车间',
        dept: '装配车间',
        kind: 'org',
        content: '车间安全库存口径：按 3 天平均消耗量上浮 20% 计。',
        source: 'promoted',
        status: 'active',
        decayed: false,
        created_by: 'emp001',
        created_at: daysAgo(30),
        last_used_at: daysAgo(2),
        use_count: 15,
      },
      {
        id: 'mem_007',
        layer: 'L3',
        owner: '装配车间',
        dept: '装配车间',
        kind: 'org',
        content: 'MES 工单号规则：WO + 年月 + 4 位流水（如 WO2026090231）。',
        source: 'promoted',
        status: 'active',
        decayed: false,
        created_by: 'emp010',
        created_at: daysAgo(60),
        last_used_at: daysAgo(5),
        use_count: 22,
      },
      {
        id: 'mem_008',
        layer: 'L3',
        owner: '装配车间',
        dept: '装配车间',
        kind: 'org',
        content: '对账异常统一先挂「待跟进」标签，再走催收流程。',
        source: 'promoted',
        status: 'active',
        decayed: false,
        created_by: 'emp015',
        created_at: daysAgo(50),
        last_used_at: daysAgo(7),
        use_count: 8,
      },
      {
        id: 'mem_009',
        layer: 'L3',
        owner: '装配车间',
        dept: '装配车间',
        kind: 'org',
        content: '月末盘点前 2 天冻结 MES 出入库操作，改走手工台账。',
        source: 'promoted',
        status: 'active',
        decayed: true,
        created_by: 'emp010',
        created_at: daysAgo(120),
        last_used_at: daysAgo(95),
        use_count: 4,
      },
      {
        id: 'mem_010',
        layer: 'L2',
        owner: 'emp001',
        dept: '装配车间',
        kind: 'preference',
        content: '旧版排产口径（按日历工时）已废弃，参考 mem_001。',
        source: 'ask',
        status: 'archived',
        decayed: true,
        created_by: 'emp001',
        created_at: daysAgo(200),
        last_used_at: daysAgo(185),
        use_count: 1,
      },
    ],
  };
}

const bucket = seed();

function statsOf(items: MemoryEntry[]): MemoryStats {
  return {
    personal: items.filter((e) => e.layer === 'L2').length,
    dept: items.filter((e) => e.layer === 'L3').length,
    auto_consolidated: items.filter((e) => e.source === 'auto_consolidated').length,
    references: items.reduce((sum, e) => sum + e.use_count, 0),
  };
}

/** 演示模式：记忆清单（L2 本人 + L3 本科室，active 默认） */
export function memoryList(layer?: string): MemoryListResponse {
  let items = bucket.entries.filter((e) => e.status === 'active');
  if (layer) items = items.filter((e) => e.layer === layer);
  return {
    items,
    count: items.length,
    stats: statsOf(bucket.entries.filter((e) => e.status === 'active')),
    consent: { ...bucket.consent },
  };
}

/** 演示模式：手动写入个人记忆（PIPL 门禁：未同意 → 抛错） */
export function memoryAdd(content: string, kind = 'preference'): MemoryEntry {
  if (!bucket.consent.granted) {
    throw new Error('未开启个人记忆同意（PIPL），请先在「我的-记忆」页开启');
  }
  if (content.includes('身份证') || content.includes('密码')) {
    throw new Error('内容包含敏感信息，已被过滤');
  }
  bucket.seq += 1;
  const entry: MemoryEntry = {
    id: `mem_${bucket.seq}`,
    layer: 'L2',
    owner: 'emp001',
    dept: '装配车间',
    kind,
    content,
    source: 'ask',
    status: 'active',
    decayed: false,
    created_by: 'emp001',
    created_at: new Date().toISOString(),
    last_used_at: null,
    use_count: 0,
  };
  bucket.entries.unshift(entry);
  return entry;
}

/** 演示模式：删除单条（L2 本人） */
export function memoryRemove(entryId: string): void {
  bucket.entries = bucket.entries.filter((e) => e.id !== entryId);
}

/** 演示模式：一键清除个人记忆（遗忘权） */
export function memoryClear(): number {
  const doomed = bucket.entries.filter((e) => e.layer === 'L2');
  bucket.entries = bucket.entries.filter((e) => e.layer !== 'L2');
  return doomed.length;
}

/** 演示模式：PIPL 同意开关（撤回即清除全部个人记忆） */
export function memorySetConsent(granted: boolean): { granted: boolean; purged: number } {
  let purged = 0;
  if (!granted) {
    purged = memoryClear();
  }
  bucket.consent = { granted, at: granted ? new Date().toISOString() : null };
  return { granted, purged };
}
