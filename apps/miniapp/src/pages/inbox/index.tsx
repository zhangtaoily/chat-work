// 信箱页（PLAN P3.4，PRD 3.6.2 自动化结果信箱移动端）
// GET /automations/inbox + POST /automations/inbox/read；演示模式走 data/inbox mock
import React, { useCallback, useState } from 'react';
import { View, Text, ScrollView } from '@tarojs/components';
import Taro, { useDidShow } from '@tarojs/taro';
import classnames from 'classnames';
import EmptyState from '@/components/EmptyState';
import { fetchInbox, markInboxRead } from '@/services/api';
import { formatCompactTime } from '@/utils/format';
import type { InboxEntry } from '@/types/chat';
import styles from './index.module.scss';

/** kind 中文标签与样式 */
const KIND_META: Record<string, { label: string; cls: string }> = {
  result: { label: '执行结果', cls: 'kindResult' },
  pause: { label: '自动暂停', cls: 'kindPause' },
  notify: { label: '通知', cls: 'kindResult' },
};

const Inbox: React.FC = () => {
  const [items, setItems] = useState<InboxEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [unreadOnly, setUnreadOnly] = useState(false);

  const refresh = useCallback(async (onlyUnread: boolean) => {
    setLoading(true);
    try {
      const list = await fetchInbox(onlyUnread);
      setItems(list);
    } catch (err) {
      const msg = err instanceof Error ? err.message : '加载失败';
      Taro.showToast({ title: msg, icon: 'none' });
    } finally {
      setLoading(false);
    }
  }, []);

  useDidShow(() => {
    void refresh(unreadOnly);
  });

  const handleToggleFilter = () => {
    const next = !unreadOnly;
    setUnreadOnly(next);
    void refresh(next);
  };

  const handleMarkAll = async () => {
    try {
      const marked = await markInboxRead();
      Taro.showToast({ title: `已标记 ${marked} 条`, icon: 'success' });
      void refresh(unreadOnly);
    } catch (err) {
      const msg = err instanceof Error ? err.message : '操作失败';
      Taro.showToast({ title: msg, icon: 'none' });
    }
  };

  const unreadCount = items.filter((i) => !i.read).length;

  return (
    <View className={styles.page}>
      <View className={styles.toolbar}>
        <View
          className={classnames(styles.filterChip, unreadOnly && styles.filterActive)}
          onClick={handleToggleFilter}
        >
          <Text className={classnames(styles.filterText, unreadOnly && styles.filterTextActive)}>
            {unreadOnly ? '只看未读' : '全部'}
          </Text>
        </View>
        <View className={styles.toolbarRight} onClick={handleMarkAll}>
          <Text className={styles.markAll}>{unreadCount > 0 ? `全部已读 (${unreadCount})` : '全部已读'}</Text>
        </View>
      </View>

      <ScrollView className={styles.scroll} scrollY enhanced showScrollbar={false}>
        {!loading && items.length === 0 ? (
          <EmptyState icon="📬" title="信箱是空的" desc="自动化任务的执行结果会送到这里" />
        ) : (
          <View className={styles.list}>
            {items.map((item) => {
              const meta = KIND_META[item.kind] || KIND_META.result;
              return (
                <View key={item.id} className={styles.item}>
                  <View className={styles.itemHead}>
                    <View className={styles.itemHeadLeft}>
                      {!item.read ? <View className={styles.unreadDot} /> : null}
                      <Text
                        className={classnames(
                          styles.kindTag,
                          meta.cls === 'kindPause' && styles.kindPause
                        )}
                      >
                        {meta.label}
                      </Text>
                      <Text className={styles.taskName}>{item.task_name}</Text>
                    </View>
                    <Text className={styles.runAt}>{formatCompactTime(item.run_at)}</Text>
                  </View>
                  <Text className={classnames(styles.bodyText, !item.ok && styles.bodyFail)}>
                    {item.text}
                  </Text>
                  <Text className={styles.skill}>技能：{item.skill}</Text>
                </View>
              );
            })}
          </View>
        )}
        {loading ? (
          <View className={styles.loading}>
            <Text className={styles.loadingText}>加载中…</Text>
          </View>
        ) : null}
        <View className={styles.scrollPad} />
      </ScrollView>
    </View>
  );
};

export default Inbox;
