// utilityProcess 拉起 local-mcp 子进程（stdio，无网络暴露，ARCHITECTURE 4.4）
// local-mcp 是随桌面端分发的本地文件工具 MCP Server（见 local-mcp/server.ts）
import { utilityProcess } from 'electron'
import path from 'node:path'

// 启动 local-mcp 子进程：加载打包后的 local-mcp/server 产物
export function spawnLocalMcp(): void {
  const child = utilityProcess.fork(path.join(__dirname, '../local-mcp/index.js'))
  // TODO: 经 stdio JSON-RPC 与子进程内的 MCP Server 通信，
  //       会话建立时做 tools/list 能力协商（PRD 5.5.5，客户端能力上报）
  // TODO: 退出监控 + 自动重启 + 版本随端升级
  child.postMessage({ type: 'ping' })
}
