// 主进程入口：BrowserWindow 创建（ARCHITECTURE 4.4）
// TODO（对应 PRD 5.5）：
//   - 托盘常驻（关闭窗口不退出）
//   - Quick Ask 全局快捷键（Alt+Space 呼出快捷问答）
import { app, BrowserWindow } from 'electron'
import path from 'node:path'
import { getAccessToken } from './auth'
import { registerIpcHandlers } from './ipc'
import { setupUpdater } from './updater'

// 创建主窗口：渲染层为 React 应用
function createWindow(): void {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.js'),
      // 安全基线：渲染层无 Node 能力，全部能力经 preload 白名单桥暴露（PRD 5.5.3）
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })

  // 开发态加载 electron-vite dev server；生产态加载打包产物
  const devUrl = process.env.ELECTRON_RENDERER_URL
  if (devUrl) {
    void win.loadURL(devUrl)
  } else {
    void win.loadFile(path.join(__dirname, '../renderer/index.html'))
  }
}

app.whenReady().then(() => {
  registerIpcHandlers()
  setupUpdater()
  createWindow()
  // 预热本地会话：密钥链有 token 且临近过期则静默刷新（PRD 8.5.4）
  void getAccessToken()
  // TODO: spawnLocalMcp()（main/local-mcp-spawn.ts，utilityProcess + stdio）
  // TODO: 托盘常驻 + Quick Ask（Alt+Space，PRD 5.5）
})

// 全部窗口关闭即退出（托盘常驻接入后移除此逻辑）
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})
