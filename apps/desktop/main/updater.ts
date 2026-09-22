// 自动更新（PRD 5.5.2 / 5.5.6）：electron-updater + 内网 update-server 分通道 feed
// generic provider：feed 指向 ${UPDATE_SERVER_URL}/releases/desktop/{channel}/latest.yml，
// 由 update-server 静态托管安装包与 latest.yml（stable/beta 目录隔离，见 infra/README.md）
import { app } from 'electron'
import { autoUpdater } from 'electron-updater'

export type UpdaterChannel = 'stable' | 'beta'

// 更新器状态（IPC 同步给渲染层：check/download 均 resolve 为最新状态）
export interface UpdaterState {
  status: 'idle' | 'checking' | 'available' | 'not-available' | 'downloading' | 'downloaded' | 'error'
  version?: string
  /** 下载进度百分比（0-100，仅 status=downloading） */
  progress?: number
  /** 错误或提示信息 */
  message?: string
}

// 更新通道（PRD 5.5.6）：UPDATER_CHANNEL=beta 进 beta 通道，默认 stable
export function updaterChannel(): UpdaterChannel {
  return (process.env.UPDATER_CHANNEL ?? '').trim().toLowerCase() === 'beta' ? 'beta' : 'stable'
}

function feedUrl(): string {
  const base = (process.env.UPDATE_SERVER_URL ?? 'http://localhost:8090').replace(/\/+$/, '')
  return `${base}/releases/desktop/${updaterChannel()}`
}

let state: UpdaterState = { status: 'idle' }
let configured = false

function configureOnce(): void {
  if (configured) return
  configured = true
  // 显式下载（用户确认后才拉包，HITL 惯例）；下载完成后退出即静默安装
  autoUpdater.autoDownload = false
  autoUpdater.autoInstallOnAppQuit = true
  autoUpdater.setFeedURL({ provider: 'generic', url: feedUrl() })
  autoUpdater.on('update-available', (info) => {
    state = { status: 'available', version: info.version }
  })
  autoUpdater.on('update-not-available', (info) => {
    state = { status: 'not-available', version: info.version }
  })
  autoUpdater.on('download-progress', (progress) => {
    state = { ...state, status: 'downloading', progress: Math.round(progress.percent) }
  })
  autoUpdater.on('update-downloaded', (info) => {
    state = { status: 'downloaded', version: info.version }
  })
  autoUpdater.on('error', (err) => {
    state = { status: 'error', message: err.message }
  })
}

function devUnavailable(): UpdaterState {
  return { status: 'not-available', message: '开发模式不支持自动更新' }
}

/** 启动时初始化（开发态跳过：electron-updater 依赖打包安装器上下文） */
export function setupUpdater(): void {
  if (!app.isPackaged) return
  configureOnce()
}

export async function checkForUpdates(): Promise<UpdaterState> {
  if (!app.isPackaged) return devUnavailable()
  configureOnce()
  state = { status: 'checking' }
  try {
    await autoUpdater.checkForUpdates()
    return state
  } catch (err) {
    state = { status: 'error', message: err instanceof Error ? err.message : String(err) }
    return state
  }
}

export async function downloadUpdate(): Promise<UpdaterState> {
  if (!app.isPackaged) return devUnavailable()
  configureOnce()
  try {
    await autoUpdater.downloadUpdate()
    return state
  } catch (err) {
    state = { status: 'error', message: err instanceof Error ? err.message : String(err) }
    return state
  }
}

/** 退出并安装已下载的更新（Windows NSIS 下静默接管退出流程） */
export function installUpdate(): null {
  autoUpdater.quitAndInstall()
  return null
}
