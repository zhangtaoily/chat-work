// IPC 白名单：枚举式通道注册 + zod 参数校验（PRD 5.5.3）
// 安全红线：渲染层不得调用白名单之外的任何主进程能力
import { app, dialog, ipcMain, Notification } from 'electron'
import { promises as fsp } from 'node:fs'
import { z } from 'zod'
import { forceRefresh, getAccessToken, getAuthStatus, login, logout } from './auth'
import { IpcChannel } from './ipc-channels'
import { checkForUpdates, downloadUpdate, installUpdate } from './updater'

// 每个通道的参数 Schema（zod）：preload 桥与主进程 handler 共用同一份校验
export const ipcSchemas = {
  [IpcChannel.SelectFile]: z.object({
    // 文件选择器过滤条件（本地文件工具的扩展名白名单在 local-mcp 层兜底）
    filters: z
      .array(z.object({ name: z.string(), extensions: z.array(z.string()) }))
      .optional()
  }),
  [IpcChannel.Notify]: z.object({
    title: z.string(),
    body: z.string()
  }),
  [IpcChannel.SaveAs]: z.object({
    defaultFileName: z.string(),
    // 文件内容（字节数组：文本/产物文件另存）
    data: z.array(z.number())
  }),
  // ---- SSO 认证（PRD 8.5）：无参数通道 ----
  [IpcChannel.AuthLogin]: z.undefined(),
  [IpcChannel.AuthLogout]: z.undefined(),
  [IpcChannel.AuthGetToken]: z.undefined(),
  [IpcChannel.AuthRefresh]: z.undefined(),
  [IpcChannel.AuthGetStatus]: z.undefined(),
  // ---- 版本与自动更新（PRD 5.5.2 / 5.5.6）：无参数通道 ----
  [IpcChannel.AppGetVersion]: z.undefined(),
  [IpcChannel.UpdaterCheck]: z.undefined(),
  [IpcChannel.UpdaterDownload]: z.undefined(),
  [IpcChannel.UpdaterInstall]: z.undefined()
} as const

// 注册全部白名单通道
export function registerIpcHandlers(): void {
  // 选择文件（local__select_file 的授权入口：用户显式选择即授权）
  ipcMain.handle(IpcChannel.SelectFile, async (_event, raw: unknown) => {
    const args = ipcSchemas[IpcChannel.SelectFile].parse(raw)
    const result = await dialog.showOpenDialog({
      properties: ['openFile'],
      ...(args.filters ? { filters: args.filters } : {})
    })
    // 取消或未选择 → null（渲染层据此判定用户未授权）
    return result.canceled ? null : (result.filePaths[0] ?? null)
  })

  // 系统通知
  ipcMain.handle(IpcChannel.Notify, async (_event, raw: unknown) => {
    const args = ipcSchemas[IpcChannel.Notify].parse(raw)
    new Notification({ title: args.title, body: args.body }).show()
    return null
  })

  // 另存为（任务产物导出）
  ipcMain.handle(IpcChannel.SaveAs, async (_event, raw: unknown) => {
    const args = ipcSchemas[IpcChannel.SaveAs].parse(raw)
    const { canceled, filePath } = await dialog.showSaveDialog({
      defaultPath: args.defaultFileName
    })
    if (canceled || !filePath) return null
    await fsp.writeFile(filePath, Buffer.from(args.data))
    return filePath
  })

  // SSO 登录：拉起系统浏览器完成 Loopback 登录（PRD 8.5.8）；失败（超时/拒绝）以异常抛出
  ipcMain.handle(IpcChannel.AuthLogin, async (_event, raw: unknown) => {
    ipcSchemas[IpcChannel.AuthLogin].parse(raw)
    return login()
  })

  // SSO 登出：吊销 refresh 会话链 + 清本机密钥链
  ipcMain.handle(IpcChannel.AuthLogout, async (_event, raw: unknown) => {
    ipcSchemas[IpcChannel.AuthLogout].parse(raw)
    await logout()
    return null
  })

  // 有效 access_token（临近过期自动静默刷新；未登录/刷新失败 → null）
  ipcMain.handle(IpcChannel.AuthGetToken, async (_event, raw: unknown) => {
    ipcSchemas[IpcChannel.AuthGetToken].parse(raw)
    return getAccessToken()
  })

  // 强制刷新（渲染层收到 401 时调用）
  ipcMain.handle(IpcChannel.AuthRefresh, async (_event, raw: unknown) => {
    ipcSchemas[IpcChannel.AuthRefresh].parse(raw)
    return forceRefresh()
  })

  // 登录状态查询（仅读本机密钥链）
  ipcMain.handle(IpcChannel.AuthGetStatus, async (_event, raw: unknown) => {
    ipcSchemas[IpcChannel.AuthGetStatus].parse(raw)
    return getAuthStatus()
  })

  // 当前应用版本（渲染层随请求带 X-Client-Version，服务端版本协商用）
  ipcMain.handle(IpcChannel.AppGetVersion, async (_event, raw: unknown) => {
    ipcSchemas[IpcChannel.AppGetVersion].parse(raw)
    return app.getVersion()
  })

  // 自动更新：检查更新（返回 UpdaterState；开发态返回 not-available）
  ipcMain.handle(IpcChannel.UpdaterCheck, async (_event, raw: unknown) => {
    ipcSchemas[IpcChannel.UpdaterCheck].parse(raw)
    return checkForUpdates()
  })

  // 自动更新：下载已发现的更新（resolve 时下载完成）
  ipcMain.handle(IpcChannel.UpdaterDownload, async (_event, raw: unknown) => {
    ipcSchemas[IpcChannel.UpdaterDownload].parse(raw)
    return downloadUpdate()
  })

  // 自动更新：退出并安装
  ipcMain.handle(IpcChannel.UpdaterInstall, async (_event, raw: unknown) => {
    ipcSchemas[IpcChannel.UpdaterInstall].parse(raw)
    return installUpdate()
  })
}
