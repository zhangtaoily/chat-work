// 我的页（PLAN P3.4，PRD 8.5.3 企微免登留接口 / PRD 9.2 记忆面板移动端）
// 连接设置（演示开关/API/Token）+ 个人·组织记忆（同意/新增/删除/清除/导出）
import React, { useCallback, useState } from 'react';
import { View, Text, ScrollView, Switch, Input, Textarea } from '@tarojs/components';
import Taro, { useDidShow } from '@tarojs/taro';
import classnames from 'classnames';
import { useSettings } from '@/store/settings';
import {
  fetchMemoryList,
  addMemory,
  deleteMemory,
  clearMemory,
  setMemoryConsent,
  memoryExportUrl,
} from '@/services/api';
import { formatCompactTime, truncate } from '@/utils/format';
import type { MemoryEntry, MemoryStats } from '@/types/chat';
import styles from './index.module.scss';

const LAYER_FILTERS: Array<{ key: string; label: string }> = [
  { key: '', label: '全部' },
  { key: 'L2', label: '个人' },
  { key: 'L3', label: '科室' },
];

const Mine: React.FC = () => {
  const settings = useSettings();
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [consent, setConsent] = useState(false);
  const [layerFilter, setLayerFilter] = useState('');
  const [newMemory, setNewMemory] = useState('');
  const [adding, setAdding] = useState(false);

  const refreshMemory = useCallback(async (layer?: string) => {
    try {
      const res = await fetchMemoryList(layer || undefined);
      setEntries(res.items);
      setStats(res.stats);
      setConsent(res.consent.granted);
    } catch (err) {
      const msg = err instanceof Error ? err.message : '记忆加载失败';
      Taro.showToast({ title: msg, icon: 'none' });
    }
  }, []);

  useDidShow(() => {
    void refreshMemory(layerFilter);
  });

  const handleConsent = async (granted: boolean) => {
    try {
      const res = await setMemoryConsent(granted);
      setConsent(granted);
      if (!granted && res.purged > 0) {
        Taro.showToast({ title: `已清除 ${res.purged} 条个人记忆`, icon: 'none' });
      }
      void refreshMemory(layerFilter);
    } catch (err) {
      const msg = err instanceof Error ? err.message : '操作失败';
      Taro.showToast({ title: msg, icon: 'none' });
    }
  };

  const handleAdd = async () => {
    const content = newMemory.trim();
    if (!content || adding) return;
    setAdding(true);
    try {
      await addMemory(content);
      setNewMemory('');
      Taro.showToast({ title: '已保存', icon: 'success' });
      void refreshMemory(layerFilter);
    } catch (err) {
      const msg = err instanceof Error ? err.message : '保存失败';
      Taro.showToast({ title: msg, icon: 'none' });
    } finally {
      setAdding(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteMemory(id);
      void refreshMemory(layerFilter);
    } catch (err) {
      const msg = err instanceof Error ? err.message : '删除失败';
      Taro.showToast({ title: msg, icon: 'none' });
    }
  };

  const handleClear = () => {
    Taro.showModal({
      title: '清除个人记忆',
      content: '将删除你的全部个人（L2）记忆，科室知识不受影响。确定继续？',
      confirmText: '清除',
      confirmColor: '#f53f3f',
      success: async (res) => {
        if (!res.confirm) return;
        try {
          const purged = await clearMemory();
          Taro.showToast({ title: `已清除 ${purged} 条`, icon: 'success' });
          void refreshMemory(layerFilter);
        } catch (err) {
          const msg = err instanceof Error ? err.message : '清除失败';
          Taro.showToast({ title: msg, icon: 'none' });
        }
      },
    });
  };

  const handleFilter = (key: string) => {
    setLayerFilter(key);
    void refreshMemory(key);
  };

  const handleExport = () => {
    const url = memoryExportUrl();
    if (!url) {
      Taro.showToast({ title: '演示模式暂不支持导出', icon: 'none' });
      return;
    }
    Taro.setClipboardData({ data: url });
  };

  return (
    <View className={styles.page}>
      <ScrollView className={styles.scroll} scrollY enhanced showScrollbar={false}>
        {/* 用户卡片 */}
        <View className={styles.userCard}>
          <View className={styles.avatar}>
            <Text className={styles.avatarText}>{settings.userId.slice(0, 2)}</Text>
          </View>
          <View className={styles.userInfo}>
            <Text className={styles.userName}>{settings.userId}</Text>
            <Text className={styles.userMeta}>
              {settings.demoMode ? '演示模式 · 内置剧本' : '真实模式 · 已配置服务'}
            </Text>
          </View>
        </View>

        {/* 连接设置 */}
        <View className={styles.section}>
          <Text className={styles.sectionTitle}>连接设置</Text>
          <View className={styles.row}>
            <Text className={styles.rowLabel}>演示模式</Text>
            <Switch
              checked={settings.demoMode}
              color="#165dff"
              onChange={(e) => settings.setDemoMode(e.detail.value)}
            />
          </View>
          <Text className={styles.rowHint}>
            开启时使用内置演示数据；关闭后直连 agent_core（企微免登接入后自动换取凭证）
          </Text>
          {!settings.demoMode ? (
            <>
              <View className={styles.field}>
                <Text className={styles.fieldLabel}>服务地址</Text>
                <Input
                  className={styles.fieldInput}
                  value={settings.apiBase}
                  placeholder="https://gw.example.com/api"
                  onInput={(e) => settings.setApiBase(e.detail.value)}
                />
              </View>
              <View className={styles.field}>
                <Text className={styles.fieldLabel}>Bearer Token（可选）</Text>
                <Input
                  className={styles.fieldInput}
                  value={settings.bearerToken}
                  placeholder="X-Chat-Auth 凭证"
                  password
                  onInput={(e) => settings.setBearerToken(e.detail.value)}
                />
              </View>
              <View className={styles.field}>
                <Text className={styles.fieldLabel}>工号</Text>
                <Input
                  className={styles.fieldInput}
                  value={settings.userId}
                  placeholder="emp001"
                  onInput={(e) => settings.setUserId(e.detail.value)}
                />
              </View>
            </>
          ) : null}
        </View>

        {/* 记忆面板（P3.3 移动端） */}
        <View className={styles.section}>
          <View className={styles.sectionHead}>
            <Text className={styles.sectionTitle}>个人 / 组织记忆</Text>
            <Text className={styles.sectionAction} onClick={handleExport}>
              导出 MD
            </Text>
          </View>

          <View className={styles.row}>
            <Text className={styles.rowLabel}>记忆同意（PIPL）</Text>
            <Switch
              checked={consent}
              color="#165dff"
              onChange={(e) => handleConsent(e.detail.value)}
            />
          </View>
          <Text className={styles.rowHint}>关闭即行使遗忘权：全部个人记忆立即清除</Text>

          {stats ? (
            <View className={styles.statRow}>
              <View className={styles.stat}>
                <Text className={styles.statNum}>{stats.personal}</Text>
                <Text className={styles.statLabel}>个人</Text>
              </View>
              <View className={styles.stat}>
                <Text className={styles.statNum}>{stats.dept}</Text>
                <Text className={styles.statLabel}>科室</Text>
              </View>
              <View className={styles.stat}>
                <Text className={styles.statNum}>{stats.auto_consolidated}</Text>
                <Text className={styles.statLabel}>自动沉淀</Text>
              </View>
              <View className={styles.stat}>
                <Text className={styles.statNum}>{stats.references}</Text>
                <Text className={styles.statLabel}>被引用</Text>
              </View>
            </View>
          ) : null}

          <View className={styles.addRow}>
            <Textarea
              className={styles.addInput}
              value={newMemory}
              placeholder="记一条偏好，如：报表默认导出 Excel"
              maxlength={100}
              autoHeight
              onInput={(e) => setNewMemory(e.detail.value)}
            />
            <View
              className={classnames(styles.addBtn, (!newMemory.trim() || adding) && styles.addBtnDisabled)}
              onClick={handleAdd}
            >
              <Text className={styles.addBtnText}>保存</Text>
            </View>
          </View>

          <View className={styles.filterRow}>
            {LAYER_FILTERS.map((f) => (
              <View
                key={f.key}
                className={classnames(
                  styles.filterPill,
                  layerFilter === f.key && styles.filterPillActive
                )}
                onClick={() => handleFilter(f.key)}
              >
                <Text
                  className={classnames(
                    styles.filterPillText,
                    layerFilter === f.key && styles.filterPillTextActive
                  )}
                >
                  {f.label}
                </Text>
              </View>
            ))}
            <View className={styles.clearAll} onClick={handleClear}>
              <Text className={styles.clearAllText}>清空个人</Text>
            </View>
          </View>

          <View className={styles.memoryList}>
            {entries.map((entry) => (
              <View key={entry.id} className={styles.memoryItem}>
                <View className={styles.memoryHead}>
                  <Text
                    className={classnames(
                      styles.layerTag,
                      entry.layer === 'L3' && styles.layerTagDept
                    )}
                  >
                    {entry.layer === 'L2' ? '个人' : '科室'}
                  </Text>
                  <Text className={styles.memoryKind}>{entry.kind}</Text>
                  <Text className={styles.memoryTime}>{formatCompactTime(entry.created_at)}</Text>
                  {entry.layer === 'L2' ? (
                    <Text className={styles.memoryDelete} onClick={() => handleDelete(entry.id)}>
                      删除
                    </Text>
                  ) : null}
                </View>
                <Text className={styles.memoryContent}>{truncate(entry.content, 80)}</Text>
                <Text className={styles.memoryMeta}>
                  引用 {entry.use_count} 次{entry.decayed ? ' · 长期未用已降权' : ''}
                </Text>
              </View>
            ))}
            {entries.length === 0 ? (
              <Text className={styles.memoryEmpty}>暂无记忆条目</Text>
            ) : null}
          </View>
        </View>

        {/* 关于 */}
        <View className={styles.section}>
          <Text className={styles.sectionTitle}>关于</Text>
          <View className={styles.row}>
            <Text className={styles.rowLabel}>版本</Text>
            <Text className={styles.rowValue}>P3.4 · v1.0.0</Text>
          </View>
          <View className={styles.row}>
            <Text className={styles.rowLabel}>配套服务</Text>
            <Text className={styles.rowValue}>agent_core（SSE 事件流）</Text>
          </View>
          <Text className={styles.rowHint}>
            企业知识对话助手移动端 · 对话 / 审批 / 信箱 / 记忆
          </Text>
        </View>

        <View className={styles.scrollPad} />
      </ScrollView>
    </View>
  );
};

export default Mine;
