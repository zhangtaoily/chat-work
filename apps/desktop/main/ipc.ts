// IPC 白名单：枚举式通道注册 + zod 参数校验（PRD 5.5.3）
// 安全红线：渲染层不得调用白名单之外的任何主进程能力
import { ipcMain } from 'electron'
import { z } from 'zod'

// IPC 通道枚举（唯一合法通道清单；新增通道必须在此登记）
export const IpcChannel = {
  SelectFile: 'dialog:selectFile',
  Notify: 'app:notify',
  SaveAs: 'app:saveAs'
} as const

export type IpcChannelName = (typeof IpcChannel)[keyof typeof IpcChannel]

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
  })
} as const

// 注册全部白名单通道（骨架占位：handler 体 TODO）
export function registerIpcHandlers(): void {
  // 选择文件（local__select_file 的授权入口：用户显式选择即授权）
  ipcMain.handle(IpcChannel.SelectFile, async (_event, raw: unknown) => {
    const args = ipcSchemas[IpcChannel.SelectFile].parse(raw)
    // TODO: 调用 dialog.showOpenDialog，返回用户所选路径
    void args
    return null
  })

  // 系统通知
  ipcMain.handle(IpcChannel.Notify, async (_event, raw: unknown) => {
    const args = ipcSchemas[IpcChannel.Notify].parse(raw)
    // TODO: 调用 new Notification(...) 推送系统通知
    void args
    return null
  })

  // 另存为（任务产物导出）
  ipcMain.handle(IpcChannel.SaveAs, async (_event, raw: unknown) => {
    const args = ipcSchemas[IpcChannel.SaveAs].parse(raw)
    // TODO: 调用 dialog.showSaveDialog 后写入文件
    void args
    return null
  })
}
