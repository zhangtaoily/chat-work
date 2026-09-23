// 待确认卡组件（PLAN P3.4，PRD 4.1 HITL）
// 覆盖 confirm_card / plan_card 两种形态：键值摘要 + 有效期倒计时 + 确认/驳回
import React from 'react';
import { View, Text } from '@tarojs/components';
import classnames from 'classnames';
import type { PendingConfirm } from '@/store/confirmations';
import { formatMoney, formatCompactTime } from '@/utils/format';
import styles from './index.module.scss';

interface ApprovalCardProps {
  item: PendingConfirm;
  /** 处置中（防重复提交） */
  busy?: boolean;
  onConfirm: (item: PendingConfirm) => void;
  onReject: (item: PendingConfirm) => void;
}

/** 金额类字段展示千分位，日期/布尔原样，其余字符串化 */
function displayValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'number' && /amount|total|price|money|hotel|transport/i.test(key)) {
    return `¥${formatMoney(value)}`;
  }
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
}

/** 字段中文标签映射（确认卡 payload 常用键） */
const FIELD_LABELS: Record<string, string> = {
  doc_type: '单据类型',
  doc_no: '单据编号',
  supplier: '供应商',
  material: '物料',
  qty: '数量',
  amount: '金额',
  required_date: '需求日期',
  leave_type: '请假类型',
  start_date: '开始日期',
  end_date: '结束日期',
  days: '天数',
  reason: '事由',
  currency: '币种',
  account: '收款账户',
  trip: '行程',
  total: '合计',
  skill_title: '技能',
};

/** 步骤行（计划卡 payload.steps） */
interface PlanStep {
  tool?: string
  rw?: string
  requires_confirm?: boolean
}

const ApprovalCard: React.FC<ApprovalCardProps> = ({ item, busy, onConfirm, onReject }) => {
  const isPlan = item.kind === 'plan';
  const expired = item.expiresAt <= Date.now();

  const entries = isPlan
    ? []
    : Object.entries(item.payload).filter(([key]) => key !== 'doc_type' && key !== 'doc_no');
  const docTitle = item.title;
  const steps = isPlan
    ? ((item.payload.steps as PlanStep[] | undefined) || [])
    : [];

  return (
    <View className={classnames(styles.card, expired && styles.cardExpired)}>
      <View className={styles.head}>
        <Text className={classnames(styles.badge, isPlan && styles.badgePlan)}>
          {isPlan ? '执行计划' : '待确认'}
        </Text>
        <Text className={styles.title}>{docTitle}</Text>
      </View>

      {isPlan ? (
        <View className={styles.steps}>
          {steps.map((step, idx) => (
            <View key={idx} className={styles.stepRow}>
              <Text className={styles.stepIdx}>{idx + 1}</Text>
              <Text className={styles.stepTool}>{step.tool || '-'}</Text>
              <Text
                className={classnames(
                  styles.stepTag,
                  step.rw === 'write' && styles.stepTagWrite
                )}
              >
                {step.rw === 'write' ? '写入' : '只读'}
              </Text>
              {step.requires_confirm ? (
                <Text className={styles.stepConfirm}>需确认</Text>
              ) : null}
            </View>
          ))}
        </View>
      ) : (
        <View className={styles.fields}>
          {entries.map(([key, value]) => (
            <View key={key} className={styles.fieldRow}>
              <Text className={styles.fieldLabel}>{FIELD_LABELS[key] || key}</Text>
              <Text className={styles.fieldValue}>{displayValue(key, value)}</Text>
            </View>
          ))}
        </View>
      )}

      <View className={styles.footer}>
        <Text className={styles.expire}>
          {expired ? '已过期' : `有效期至 ${formatCompactTime(item.expiresAt)}`}
        </Text>
        <View className={styles.actions}>
          <View
            className={classnames(styles.btn, styles.btnReject, busy && styles.btnDisabled)}
            onClick={() => !busy && !expired && onReject(item)}
          >
            <Text className={styles.btnRejectText}>驳回</Text>
          </View>
          <View
            className={classnames(styles.btn, styles.btnConfirm, busy && styles.btnDisabled)}
            onClick={() => !busy && !expired && onConfirm(item)}
          >
            <Text className={styles.btnConfirmText}>{isPlan ? '批准执行' : '确认提交'}</Text>
          </View>
        </View>
      </View>
    </View>
  );
};

export default ApprovalCard;
