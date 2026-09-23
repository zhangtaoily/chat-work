// 对话页（PLAN P3.4，PRD 3.1/3.3/4.1 移动端）
// SSE 流式渲染 8 类事件卡 + 三模式切换 + HITL 确认卡沉淀到审批 Tab
import React, { useEffect, useRef, useState } from 'react';
import { View, Text, ScrollView, Textarea } from '@tarojs/components';
import { useDidShow } from '@tarojs/taro';
import classnames from 'classnames';
import EventCards from '@/components/EventCards';
import { streamChat } from '@/utils/sse';
import { uid, formatCompactTime } from '@/utils/format';
import { useSettings } from '@/store/settings';
import { useConfirmations, titleOfEvent } from '@/store/confirmations';
import { mockStreamReply } from '@/data/chat-events';
import type { ChatEvent, ChatMessage } from '@/types/chat';
import styles from './index.module.scss';

const MODES: Array<{ key: 'ask' | 'plan' | 'act'; label: string }> = [
  { key: 'ask', label: '问答' },
  { key: 'plan', label: '计划' },
  { key: 'act', label: '执行' },
];

/** 演示快捷提问（覆盖 5 个演示剧本） */
const QUICK_PROMPTS = ['帮我请假', '下采购单', '查库存', '生成月度领料汇总', '解析采购订单'];

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const Chat: React.FC = () => {
  const { demoMode, userId } = useSettings();
  const upsertConfirm = useConfirmations((s) => s.upsert);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [mode, setMode] = useState<'ask' | 'plan' | 'act'>('ask');
  const [streaming, setStreaming] = useState(false);
  const [anchor, setAnchor] = useState('');
  const sessionIdRef = useRef(uid('sess'));

  useDidShow(() => {
    useConfirmations.getState().clearExpired();
  });

  /** 消息变化后滚到底部 */
  useEffect(() => {
    const last = messages[messages.length - 1];
    if (last) setAnchor(`anchor_${last.id}`);
  }, [messages]);

  /** 将事件并入最后一条助手消息（无则新建） */
  const appendEvents = (events: ChatEvent[]) => {
    for (const ev of events) {
      if (ev.type === 'confirm_card' || ev.type === 'plan_card') {
        // HITL 卡片沉淀到审批 Tab（PRD 4.1）
        upsertConfirm({
          token: ev.type === 'plan_card' ? ev.plan_token : ev.confirm_token,
          kind: ev.type === 'plan_card' ? 'plan' : 'confirm',
          payload: ev.payload,
          expiresAt: ev.expires_at,
          title: titleOfEvent(ev),
          createdAt: Date.now(),
        });
      }
    }
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.role === 'assistant') {
        const updated = { ...last, events: [...(last.events || []), ...events] };
        return [...prev.slice(0, -1), updated];
      }
      return [
        ...prev,
        { id: uid('msg'), role: 'assistant', events, at: Date.now() },
      ];
    });
  };

  const handleSend = async (raw?: string) => {
    const text = (raw ?? input).trim();
    if (!text || streaming) return;
    setInput('');
    setMessages((prev) => [
      ...prev,
      { id: uid('msg'), role: 'user', text, at: Date.now() },
    ]);
    setStreaming(true);
    try {
      if (useSettings.getState().demoMode) {
        // 演示模式：剧本事件逐条推送（模拟流式节奏）
        const events = mockStreamReply(text);
        for (const ev of events) {
          await sleep(ev.type === 'final' ? 300 : 350);
          appendEvents([ev]);
        }
      } else {
        // 真实模式：SSE 流式（POST /chat，直传 user_id）
        await streamChat(
          {
            session_id: sessionIdRef.current,
            user_id: useSettings.getState().userId,
            message: text,
            mode,
          },
          (ev) => appendEvents([ev])
        );
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : '网络异常，请稍后重试';
      setMessages((prev) => [
        ...prev,
        { id: uid('msg'), role: 'assistant', text: `出错了：${msg}`, at: Date.now() },
      ]);
    } finally {
      setStreaming(false);
    }
  };

  return (
    <View className={styles.page}>
      <ScrollView
        className={styles.scroll}
        scrollY
        scrollIntoView={anchor}
        scrollWithAnimation
        enhanced
        showScrollbar={false}
      >
        {demoMode ? (
          <View className={styles.demoBanner}>
            <Text className={styles.demoText}>演示模式：内置剧本，可在「我的」页接入真实服务</Text>
          </View>
        ) : null}

        {messages.length === 0 ? (
          <View className={styles.welcome}>
            <Text className={styles.welcomeTitle}>你好，{userId}</Text>
            <Text className={styles.welcomeDesc}>
              我是 chat-work 企业助手，可以帮你下单、查库存、提单据。试试下面的示例：
            </Text>
            <View className={styles.quickWrap}>
              {QUICK_PROMPTS.map((q) => (
                <View key={q} className={styles.quickChip} onClick={() => handleSend(q)}>
                  <Text className={styles.quickText}>{q}</Text>
                </View>
              ))}
            </View>
          </View>
        ) : (
          messages.map((m) => (
            <View key={m.id} id={`anchor_${m.id}`} className={styles.message}>
              {m.role === 'user' ? (
                <View className={styles.userRow}>
                  <Text className={styles.msgTime}>{formatCompactTime(m.at)}</Text>
                  <View className={styles.userBubble}>
                    <Text className={styles.userText}>{m.text}</Text>
                  </View>
                </View>
              ) : (
                <View className={styles.aiRow}>
                  <View className={styles.aiAvatar}>
                    <Text className={styles.aiAvatarText}>AI</Text>
                  </View>
                  <View className={styles.aiBody}>
                    {m.events && m.events.length > 0 ? (
                      <EventCards events={m.events} />
                    ) : null}
                    {m.text ? (
                      <View className={styles.aiTextBubble}>
                        <Text className={styles.aiText}>{m.text}</Text>
                      </View>
                    ) : null}
                  </View>
                </View>
              )}
            </View>
          ))
        )}

        {streaming ? (
          <View className={styles.typing}>
            <Text className={styles.typingText}>正在处理…</Text>
          </View>
        ) : null}

        <View className={styles.scrollPad} />
      </ScrollView>

      <View className={styles.inputBar}>
        <View className={styles.modeRow}>
          {MODES.map((m) => (
            <View
              key={m.key}
              className={classnames(styles.modePill, mode === m.key && styles.modeActive)}
              onClick={() => setMode(m.key)}
            >
              <Text
                className={classnames(styles.modeText, mode === m.key && styles.modeTextActive)}
              >
                {m.label}
              </Text>
            </View>
          ))}
        </View>
        <View className={styles.inputRow}>
          <Textarea
            className={styles.input}
            value={input}
            placeholder={demoMode ? '试试「帮我请假」' : '输入你的问题…'}
            placeholderClass={styles.placeholder}
            maxlength={500}
            autoHeight
            onInput={(e) => setInput(e.detail.value)}
          />
          <View
            className={classnames(styles.sendBtn, (!input.trim() || streaming) && styles.sendDisabled)}
            onClick={() => handleSend()}
          >
            <Text className={styles.sendText}>发送</Text>
          </View>
        </View>
      </View>
    </View>
  );
};

export default Chat;
