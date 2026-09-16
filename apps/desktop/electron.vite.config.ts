// electron-vite 构建配置：主进程 / preload / 渲染层三段式（ARCHITECTURE 4.4）
import { defineConfig } from 'electron-vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  // 主进程：main/index.ts（窗口/托盘/IPC/认证/local-mcp 拉起）
  main: {},
  // 预加载：preload/bridge.ts（contextBridge 白名单桥，PRD 5.5.3）
  preload: {},
  // 渲染层：React 18 + AntD 5（业务卡片组件来自 packages/ui）
  renderer: {
    plugins: [react()]
  }
})
