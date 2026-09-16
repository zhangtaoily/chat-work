// contextBridge 白名单桥：仅暴露渲染层需要的最小能力（PRD 5.5.3）
// 通道与 payload 均受 main/ipc.ts 的枚举白名单 + zod 校验约束
import { contextBridge, ipcRenderer } from 'electron'
import type { z } from 'zod'
import type { IpcChannel, ipcSchemas } from '../main/ipc'

// 暴露给渲染层的窄接口（window.chatwork）
const bridge = {
  // 选择本地文件（local__select_file 的授权入口：用户显式选择即授权）
  selectFile: (payload: z.infer<(typeof ipcSchemas)['dialog:selectFile']>) =>
    ipcRenderer.invoke(IpcChannel.SelectFile, payload),
  // 系统通知
  notify: (payload: z.infer<(typeof ipcSchemas)['app:notify']>) =>
    ipcRenderer.invoke(IpcChannel.Notify, payload),
  // 另存为（任务产物导出）
  saveAs: (payload: z.infer<(typeof ipcSchemas)['app:saveAs']>) =>
    ipcRenderer.invoke(IpcChannel.SaveAs, payload)
}

contextBridge.exposeInMainWorld('chatwork', bridge)
