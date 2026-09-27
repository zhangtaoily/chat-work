// 结果信箱新消息 → 系统通知轮询（桌面端醒目提醒：Windows toast / 浏览器通知）
// 服务端调度器写信箱无推送通道，客户端轮询 /automations/inbox（30s，分钟级提醒足够）
import { useEffect, useRef, useState } from 'react'
import { automationInbox, type InboxMessage } from './api'

const POLL_MS = 30_000
// 已通知消息 id 持久化（localStorage，防重启后重复弹历史消息）
const SEEN_KEY = 'chatwork.notifiedInboxIds'
const SEEN_MAX = 200

function loadSeen(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(SEEN_KEY) ?? '[]') as string[])
  } catch {
    return new Set()
  }
}

function saveSeen(seen: Set<string>): void {
  // 防膨胀：超出上限丢弃最旧的
  localStorage.setItem(SEEN_KEY, JSON.stringify([...seen].slice(-SEEN_MAX)))
}

export interface InboxNotifyOptions {
  /** false 时不轮询（未登录/登录态检测中） */
  enabled: boolean
  /** 点击通知 → 跳转自动化信箱 */
  onOpenInbox?: () => void
}

/**
 * 轮询结果信箱并弹系统通知，返回当前未读数（供导航徽标）。
 * 通知点击自动聚焦窗口并跳转信箱；轮询失败静默（服务未起不打扰）。
 */
export function useInboxNotify({ enabled, onOpenInbox }: InboxNotifyOptions): number {
  const [unread, setUnread] = useState(0)
  const seenRef = useRef<Set<string>>(new Set())
  const onOpenInboxRef = useRef(onOpenInbox)
  onOpenInboxRef.current = onOpenInbox

  useEffect(() => {
    if (!enabled) return
    seenRef.current = loadSeen()
    // web 冒烟模式需显式授权；Electron 渲染层默认已授权
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      void Notification.requestPermission()
    }

    let cancelled = false
    let first = true

    const notify = (msg: InboxMessage) => {
      const title = msg.ok ? `⏰ 提醒：${msg.task_name}` : `⚠️ 任务失败：${msg.task_name}`
      const n = new Notification(title, {
        body: msg.text.slice(0, 200),
        tag: msg.id, // 同 id 系统级去重
        silent: false
      })
      n.onclick = () => {
        window.focus()
        onOpenInboxRef.current?.()
        n.close()
      }
    }

    const poll = async () => {
      try {
        const result = await automationInbox(20)
        if (cancelled) return
        const unreadMsgs = result.items.filter((m) => !m.read)
        setUnread(unreadMsgs.length)
        if (first) {
          // 启动基线：已有未读只记录不弹（避免离线积压一次性轰炸）
          first = false
          if (unreadMsgs.some((m) => !seenRef.current.has(m.id))) {
            unreadMsgs.forEach((m) => seenRef.current.add(m.id))
            saveSeen(seenRef.current)
          }
          return
        }
        const fresh = unreadMsgs.filter((m) => !seenRef.current.has(m.id))
        if (fresh.length === 0) return
        fresh.forEach((m) => seenRef.current.add(m.id))
        saveSeen(seenRef.current)
        fresh.forEach(notify)
      } catch {
        // 静默：服务未起 / web 冒烟无 token（401），不打扰用户
      }
    }

    void poll()
    const timer = window.setInterval(() => void poll(), POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [enabled])

  return unread
}
