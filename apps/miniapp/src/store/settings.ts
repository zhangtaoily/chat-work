// 全局设置状态（PLAN P3.4，PRD 8.5.3 企微免登留接口）
// 演示模式：全 mock，免后端；真实模式：API 地址 + 可选 Bearer Token
import { create } from 'zustand';
import Taro from '@tarojs/taro';

const STORAGE_KEY = 'chat-work-settings';

export interface SettingsState {
  /** 演示模式（true=全 mock 数据，不请求后端） */
  demoMode: boolean;
  /** agent_core 服务地址（如 https://gw.example.com/api） */
  apiBase: string;
  /** 可选 Bearer Token（X-Chat-Auth 兼容头；企微免登接入后自动换取） */
  bearerToken: string;
  /** 当前用户工号（SSO_REQUIRED=false 时 /chat 请求体直传） */
  userId: string;
  setDemoMode: (v: boolean) => void;
  setApiBase: (v: string) => void;
  setBearerToken: (v: string) => void;
  setUserId: (v: string) => void;
  load: () => void;
}

function persist(state: Partial<SettingsState>) {
  try {
    Taro.setStorageSync(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // 存储失败静默（如超出配额），不影响主流程
  }
}

export const useSettings = create<SettingsState>((set) => ({
  demoMode: true,
  apiBase: 'http://localhost:8000',
  bearerToken: '',
  userId: 'emp001',
  setDemoMode: (v) => {
    set({ demoMode: v });
    persist({ demoMode: v });
  },
  setApiBase: (v) => {
    set({ apiBase: v });
    persist({ apiBase: v });
  },
  setBearerToken: (v) => {
    set({ bearerToken: v });
    persist({ bearerToken: v });
  },
  setUserId: (v) => {
    set({ userId: v });
    persist({ userId: v });
  },
  load: () => {
    try {
      const raw = Taro.getStorageSync(STORAGE_KEY) as string;
      if (raw) {
        const saved = JSON.parse(raw) as Partial<SettingsState>;
        set(saved);
      }
    } catch {
      // 读取失败用默认值
    }
  },
}));

/** 便捷读取（store 外使用，如 utils/services 层） */
export function getSettings() {
  const s = useSettings.getState();
  return {
    demoMode: s.demoMode,
    apiBase: s.apiBase,
    bearerToken: s.bearerToken,
    userId: s.userId,
  };
}
