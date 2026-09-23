// 通用空状态组件（PLAN P3.4）
import React from 'react';
import { View, Text } from '@tarojs/components';
import styles from './index.module.scss';

interface EmptyStateProps {
  /** 展示 emoji（规范禁用图标库，用字符图标） */
  icon?: string;
  title: string;
  desc?: string;
}

const EmptyState: React.FC<EmptyStateProps> = ({ icon = '📭', title, desc }) => {
  return (
    <View className={styles.wrapper}>
      <Text className={styles.icon}>{icon}</Text>
      <Text className={styles.title}>{title}</Text>
      {desc ? <Text className={styles.desc}>{desc}</Text> : null}
    </View>
  );
};

export default EmptyState;
