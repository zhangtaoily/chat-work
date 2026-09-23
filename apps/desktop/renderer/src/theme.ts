// 皮肤（多主题）机制：原型靛蓝为默认皮肤，经典蓝保留旧版观感
// 设计 token 源：prototype.html :root（工业 5.0 以人为本的视觉基调：浅底/靛蓝主色/软阴影）
import { useState } from 'react'
import type { CSSProperties } from 'react'
import type { ThemeConfig } from 'antd'

export type SkinKey = 'prototype' | 'classic'

export interface Skin {
  key: SkinKey
  name: string
  /** 主色（brand 强调/欢迎页点缀；与 antd.token.colorPrimary 一致） */
  primary: string
  /** AntD 全局 token（ConfigProvider theme） */
  antd: ThemeConfig
  /** 壳层（侧栏/顶栏/内容区）样式 token */
  shell: {
    siderBg: string
    menuTheme: 'light' | 'dark'
    brandText: string
    headerBg: string
    headerBorder: string
    headerText: string
    contentBg: string
    /** brand 渐变方块（原型：135° 靛蓝渐变 + 投影） */
    brandMark: CSSProperties
    /** 顶栏按钮是否需要 ghost（深色顶栏时白字） */
    headerDark: boolean
  }
}

const PROTOTYPE: Skin = {
  key: 'prototype',
  name: '原型 · 靛蓝',
  primary: '#4f46e5',
  antd: {
    token: {
      colorPrimary: '#4f46e5',
      borderRadius: 10,
      colorBgLayout: '#f2f4f8'
    }
  },
  shell: {
    siderBg: '#ffffff',
    menuTheme: 'light',
    brandText: '#1c2333',
    headerBg: '#ffffff',
    headerBorder: '#e5e8f0',
    headerText: '#1c2333',
    contentBg: '#f2f4f8',
    brandMark: {
      background: 'linear-gradient(135deg,#6f66f5 0%,#4f46e5 100%)',
      color: '#fff',
      boxShadow: '0 4px 12px -4px rgba(79,70,229,.5)'
    },
    headerDark: false
  }
}

const CLASSIC: Skin = {
  key: 'classic',
  name: '经典 · 蓝色',
  primary: '#1677ff',
  antd: {
    token: {
      colorPrimary: '#1677ff',
      colorBgLayout: '#f5f5f5'
    }
  },
  shell: {
    siderBg: '#001529',
    menuTheme: 'dark',
    brandText: '#fff',
    headerBg: '#001529',
    headerBorder: '#001529',
    headerText: '#fff',
    contentBg: '#f5f5f5',
    brandMark: {
      background: 'rgba(255,255,255,0.2)',
      color: '#fff'
    },
    headerDark: true
  }
}

export const SKINS: Record<SkinKey, Skin> = { prototype: PROTOTYPE, classic: CLASSIC }

const SKIN_PREF_KEY = 'chatwork.skin'

function initialSkinKey(): SkinKey {
  return localStorage.getItem(SKIN_PREF_KEY) === 'classic' ? 'classic' : 'prototype'
}

/** 皮肤状态 hook：localStorage 持久化，切换即时生效 */
export function useSkin(): {
  skinKey: SkinKey
  skin: Skin
  setSkin: (key: SkinKey) => void
} {
  const [skinKey, setSkinKey] = useState<SkinKey>(initialSkinKey)
  const setSkin = (key: SkinKey) => {
    localStorage.setItem(SKIN_PREF_KEY, key)
    setSkinKey(key)
  }
  return { skinKey, skin: SKINS[skinKey], setSkin }
}
