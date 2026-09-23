// 审批页（PLAN P3.4，PRD 4.1 HITL 移动端）
// 聚合对话流沉淀的确认卡/计划卡，POST /confirmations/{token} 处置 + 本地处置历史
import React, { useState } from 'react';
import { View, Text, ScrollView } from '@tarojs/components';
import Taro, { useDidShow } from '@tarojs/taro';
import ApprovalCard from '@/components/ApprovalCard';
import EmptyState from '@/components/EmptyState';
import { useConfirmations } from '@/store/confirmations';
import { postConfirmation } from '@/services/api';
import { seedPending, seedHistory } from '@/data/approvals';
import { getSettings } from '@/store/settings';
import { formatRelative } from '@/utils/format';
import styles from './index.module.scss';

const Approval: React.FC = () => {
  const items = useConfirmations((s) => s.items);
  const history = useConfirmations((s) => s.history);
  const settle = useConfirmations((s) => s.settle);
  const [busyToken, setBusyToken] = useState('');

  useDidShow(() => {
    useConfirmations.getState().clearExpired();
    // 演示模式且尚无数据：注入种子确认卡与历史
    if (getSettings().demoMode) {
      const store = useConfirmations.getState();
      if (store.items.length === 0) store.seed(seedPending());
      if (store.history.length === 0) store.seedHistory(seedHistory());
    }
  });

  const handleAction = async (token: string, title: string, action: 'confirm' | 'reject') => {
    setBusyToken(token);
    try {
      const res = await postConfirmation(token, action);
      settle(token, title, action, res.ok);
      Taro.showToast({ title: action === 'confirm' ? '已确认' : '已驳回', icon: 'success' });
    } catch (err) {
      const msg = err instanceof Error ? err.message : '处置失败';
      Taro.showToast({ title: msg, icon: 'none' });
    } finally {
      setBusyToken('');
    }
  };

  const expiredCount = items.filter((i) => i.expiresAt <= Date.now()).length;

  return (
    <View className={styles.page}>
      <ScrollView className={styles.scroll} scrollY enhanced showScrollbar={false}>
        <View className={styles.header}>
          <Text className={styles.headerTitle}>
            待处置 {items.length} 项{expiredCount > 0 ? `（${expiredCount} 项已过期）` : ''}
          </Text>
          <Text className={styles.headerDesc}>来自对话流的确认卡，10 分钟内有效</Text>
        </View>

        {items.length === 0 ? (
          <EmptyState
            icon="✅"
            title="暂无待处置确认"
            desc="对话中触发的写入操作会先在这里等你确认"
          />
        ) : (
          <View className={styles.list}>
            {items.map((item) => (
              <ApprovalCard
                key={item.token}
                item={item}
                busy={busyToken === item.token}
                onConfirm={(i) => handleAction(i.token, i.title, 'confirm')}
                onReject={(i) => handleAction(i.token, i.title, 'reject')}
              />
            ))}
          </View>
        )}

        {history.length > 0 ? (
          <View className={styles.history}>
            <Text className={styles.historyHead}>处置历史</Text>
            {history.map((h) => (
              <View key={`${h.token}_${h.at}`} className={styles.historyRow}>
                <Text
                  className={h.action === 'confirm' ? styles.tagConfirm : styles.tagReject}
                >
                  {h.action === 'confirm' ? '已确认' : '已驳回'}
                </Text>
                <Text className={styles.historyTitle}>{h.title}</Text>
                <Text className={styles.historyTime}>{formatRelative(h.at)}</Text>
              </View>
            ))}
          </View>
        ) : null}

        <View className={styles.scrollPad} />
      </ScrollView>
    </View>
  );
};

export default Approval;
