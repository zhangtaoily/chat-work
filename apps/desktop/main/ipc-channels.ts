// IPC 通道枚举（唯一合法通道清单；新增通道必须在此登记）
// 独立零依赖文件：主进程与 preload 桥共用，preload 可安全值引用而不连带打包 handler 实现
export const IpcChannel = {
  SelectFile: 'dialog:selectFile',
  Notify: 'app:notify',
  SaveAs: 'app:saveAs',
  AuthLogin: 'auth:login',
  AuthLogout: 'auth:logout',
  AuthGetToken: 'auth:getToken',
  AuthRefresh: 'auth:refresh',
  AuthGetStatus: 'auth:getStatus',
  AppGetVersion: 'app:getVersion',
  UpdaterCheck: 'updater:check',
  UpdaterDownload: 'updater:download',
  UpdaterInstall: 'updater:install'
} as const

export type IpcChannelName = (typeof IpcChannel)[keyof typeof IpcChannel]
