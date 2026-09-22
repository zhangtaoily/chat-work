// 浏览器 dev 模式：单独起 renderer dev server（Electron 壳不可用时的联调方式）
// 桌面态仍以 electron-vite dev 为准（electron.vite.config.ts）；二者共享 renderer 源码
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  root: 'renderer',
  plugins: [react()],
  server: { port: 5173 }
})
