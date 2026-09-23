// 对话事件卡渲染器（PLAN P3.4）
// 按 ChatEvent.type 分发渲染：stage_progress / draft_card / confirm_card / plan_card
// / diff_card / material_candidates / doc_workbench / final
import React from 'react';
import { View, Text } from '@tarojs/components';
import Taro from '@tarojs/taro';
import classnames from 'classnames';
import type { ChatEvent } from '@/types/chat';
import { formatMoney, truncate } from '@/utils/format';
import styles from './index.module.scss';

interface EventCardsProps {
  events: ChatEvent[];
}

/** 常用字段中文标签 */
const FIELD_LABELS: Record<string, string> = {
  doc_type: '单据类型',
  doc_no: '单据编号',
  supplier: '供应商',
  material: '物料',
  qty: '数量',
  amount: '金额',
  doc_date: '单据日期',
  leave_type: '请假类型',
  start_date: '开始日期',
  end_date: '结束日期',
  days: '天数',
  reason: '事由',
};

/** 阶段名中文映射 */
const STAGE_LABELS: Record<string, string> = {
  intent: '意图识别',
  route: '技能路由',
  extract: '参数提取',
  validate: '参数校验',
  hitl: '人工确认',
  execute: '执行',
  format: '结果整理',
};

function displayField(key: string, value: unknown): string {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'number' && /amount|total|price/i.test(key)) {
    return `¥${formatMoney(value)}`;
  }
  return String(value);
}

/** 阶段进度行 */
const StageRow: React.FC<{ message: string; stage: string }> = ({ message, stage }) => (
  <View className={styles.stageRow}>
    <Text className={styles.stageDot} />
    <Text className={styles.stageText}>
      {STAGE_LABELS[stage] ? `${STAGE_LABELS[stage]}：` : ''}
      {message}
    </Text>
  </View>
);

/** 键值对行 */
const KV: React.FC<{ label: string; value: string; strong?: boolean }> = ({
  label,
  value,
  strong,
}) => (
  <View className={styles.kvRow}>
    <Text className={styles.kvLabel}>{label}</Text>
    <Text className={classnames(styles.kvValue, strong && styles.kvStrong)}>{value}</Text>
  </View>
);

const EventCards: React.FC<EventCardsProps> = ({ events }) => {
  return (
    <View className={styles.list}>
      {events.map((ev, idx) => {
        switch (ev.type) {
          case 'stage_progress':
            return <StageRow key={idx} stage={ev.stage} message={ev.message} />;

          case 'draft_card':
            return (
              <View key={idx} className={styles.card}>
                <View className={styles.cardHead}>
                  <Text className={styles.cardBadge}>参数草稿</Text>
                  <Text className={styles.cardMeta}>v{ev.draft_version}</Text>
                </View>
                {Object.entries(ev.draft).map(([k, v]) => (
                  <KV key={k} label={FIELD_LABELS[k] || k} value={displayField(k, v)} />
                ))}
                {ev.missing_fields.length > 0 ? (
                  <View className={styles.missingRow}>
                    {ev.missing_fields.map((f) => (
                      <Text key={f} className={styles.missingTag}>
                        缺少：{FIELD_LABELS[f] || f}
                      </Text>
                    ))}
                  </View>
                ) : null}
              </View>
            );

          case 'confirm_card':
          case 'plan_card': {
            const title =
              ev.type === 'plan_card'
                ? `执行计划：${
                    (ev.payload as { skill_title?: string }).skill_title || '未命名技能'
                  }`
                : `待确认：${
                    (ev.payload as Record<string, unknown>).doc_no ||
                    (ev.payload as Record<string, unknown>).doc_type ||
                    '表单'
                  }`;
            return (
              <View key={idx} className={classnames(styles.card, styles.cardAccent)}>
                <View className={styles.cardHead}>
                  <Text className={classnames(styles.cardBadge, styles.badgeWarn)}>
                    {ev.type === 'plan_card' ? '执行计划' : '等待确认'}
                  </Text>
                </View>
                <Text className={styles.confirmTitle}>{title}</Text>
                <View
                  className={styles.gotoApproval}
                  onClick={() => Taro.switchTab({ url: '/pages/approval/index' })}
                >
                  <Text className={styles.gotoText}>去「审批」处理 →</Text>
                </View>
              </View>
            );
          }

          case 'diff_card':
            return (
              <View key={idx} className={styles.card}>
                <View className={styles.cardHead}>
                  <Text className={styles.cardBadge}>修改记录</Text>
                  <Text className={styles.cardMeta}>v{ev.draft_version}</Text>
                </View>
                {ev.changes.map((c, ci) => (
                  <View key={ci} className={styles.diffRow}>
                    <Text className={styles.diffField}>{FIELD_LABELS[c.field] || c.field}</Text>
                    <Text className={styles.diffOld}>{displayField(c.field, c.old_value)}</Text>
                    <Text className={styles.diffArrow}>→</Text>
                    <Text className={styles.diffNew}>{displayField(c.field, c.new_value)}</Text>
                  </View>
                ))}
              </View>
            );

          case 'material_candidates':
            return (
              <View key={idx} className={styles.card}>
                <View className={styles.cardHead}>
                  <Text className={styles.cardBadge}>物料候选</Text>
                  <Text className={styles.cardMeta}>请从候选中指定，Agent 不猜</Text>
                </View>
                {ev.candidates.map((c, ci) => (
                  <View key={ci} className={styles.candidateRow}>
                    <View className={styles.candidateMain}>
                      <Text className={styles.candidateName}>
                        {String(c.name ?? '-')}（{String(c.code ?? '-')}）
                      </Text>
                      <Text className={styles.candidateSpec}>
                        规格 {String(c.spec ?? '-')} · 库存 {String(c.stock ?? '-')}
                      </Text>
                    </View>
                    <Text className={styles.candidatePrice}>
                      ¥{formatMoney(Number(c.price ?? 0))}
                    </Text>
                  </View>
                ))}
              </View>
            );

          case 'doc_workbench': {
            const h = ev.header as Record<string, unknown>;
            return (
              <View key={idx} className={styles.card}>
                <View className={styles.cardHead}>
                  <Text className={styles.cardBadge}>单据工作台</Text>
                  <Text className={styles.cardMeta}>{String(h.status ?? '')}</Text>
                </View>
                <KV label="单据" value={`${String(h.doc_type ?? '-')} ${String(h.doc_no ?? '')}`} />
                <KV label="供应商" value={String(h.supplier ?? '-')} />
                <KV
                  label="金额"
                  value={`¥${formatMoney(Number(h.amount ?? 0))}`}
                  strong
                />
                <KV label="明细" value={`${ev.lines.length} 行`} />
                <Text className={styles.workbenchHint}>
                  移动端为只读摘要，完整编辑请在桌面端打开「单据工作台」。
                </Text>
              </View>
            );
          }

          case 'final':
            return (
              <View key={idx} className={styles.final}>
                <Text className={styles.finalText}>{ev.text}</Text>
                {ev.cards.length > 0 ? (
                  <Text className={styles.finalMeta}>
                    附带 {ev.cards.length} 张卡片：
                    {ev.cards
                      .map((c) => truncate(String((c as { title?: string }).title ?? '摘要'), 12))
                      .join('、')}
                  </Text>
                ) : null}
              </View>
            );

          default:
            return null;
        }
      })}
    </View>
  );
};

export default EventCards;
