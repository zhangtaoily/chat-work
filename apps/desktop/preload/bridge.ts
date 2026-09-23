// contextBridge 白名单桥：仅暴露渲染层需要的最小能力（PRD 5.5.3）
// 通道与 payload 均受 main/ipc.ts 的枚举白名单 + zod 校验约束
import { contextBridge, ipcRenderer } from 'electron'
import type { z } from 'zod'
import type { AuthStatus } from '../main/auth-core'
import { IpcChannel } from '../main/ipc-channels'
import type { ipcSchemas } from '../main/ipc'
import type { UpdaterState } from '../main/updater'

// 暴露给渲染层的窄接口（window.chatwork）
export const bridge = {
  // 选择本地文件（local__select_file 的授权入口：用户显式选择即授权）
  selectFile: (payload: z.infer<(typeof ipcSchemas)['dialog:selectFile']>) =>
    ipcRenderer.invoke(IpcChannel.SelectFile, payload),
  // 系统通知
  notify: (payload: z.infer<(typeof ipcSchemas)['app:notify']>) =>
    ipcRenderer.invoke(IpcChannel.Notify, payload),
  // 另存为（任务产物导出）
  saveAs: (payload: z.infer<(typeof ipcSchemas)['app:saveAs']>) =>
    ipcRenderer.invoke(IpcChannel.SaveAs, payload),
  // ---- SSO 认证（PRD 8.5） ----
  // 登录：拉起系统浏览器完成 Loopback 登录（失败以异常抛出）
  login: (): Promise<AuthStatus> => ipcRenderer.invoke(IpcChannel.AuthLogin),
  // 登出：吊销 refresh 会话链 + 清本机密钥链
  logout: (): Promise<null> => ipcRenderer.invoke(IpcChannel.AuthLogout),
  // 有效 access_token（临近过期自动静默刷新；未登录/刷新失败 → null）
  getAccessToken: (): Promise<string | null> => ipcRenderer.invoke(IpcChannel.AuthGetToken),
  // 强制刷新（请求收到 401 时调用）
  refreshAccessToken: (): Promise<string | null> => ipcRenderer.invoke(IpcChannel.AuthRefresh),
  // 登录状态查询
  getAuthStatus: (): Promise<AuthStatus> => ipcRenderer.invoke(IpcChannel.AuthGetStatus),
  // ---- 版本与自动更新（PRD 5.5.2 / 5.5.6） ----
  // 当前应用版本（authedFetch 随请求带 X-Client-Version，服务端强制升级协商）
  getAppVersion: (): Promise<string> => ipcRenderer.invoke(IpcChannel.AppGetVersion),
  // 外部链接跳转（管理后台 Web 页面等）：经系统默认浏览器打开
  openExternal: (url: string) => ipcRenderer.invoke(IpcChannel.OpenExternal, url),
  // 检查更新（返回 UpdaterState；开发态 not-available）
  updaterCheck: (): Promise<UpdaterState> => ipcRenderer.invoke(IpcChannel.UpdaterCheck),
  // 下载已发现的更新（resolve 时下载完成）
  updaterDownload: (): Promise<UpdaterState> => ipcRenderer.invoke(IpcChannel.UpdaterDownload),
  // 退出并安装
  updaterInstall: (): Promise<null> => ipcRenderer.invoke(IpcChannel.UpdaterInstall)
} satisfies Record<string, unknown>

// 渲染层类型（env.d.ts 经 typeof 引用，保持 main/preload/renderer 单一事实源）
export type ChatworkBridge = typeof bridge

contextBridge.exposeInMainWorld('chatwork', bridge)
