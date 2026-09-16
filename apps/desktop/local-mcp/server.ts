// 本地 MCP Server（stdio）：随桌面端分发，经主进程 utilityProcess 拉起（无网络暴露）
// 工具实现见 local-mcp/tools.ts；工具 Schema 源自 packages/protocol（local__*.json）
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { registerLocalTools } from './tools'

// 创建 MCP Server（能力声明：仅 tools）
const server = new Server(
  { name: 'local-mcp', version: '0.1.0' },
  { capabilities: { tools: {} } }
)

// 注册本地文件工具（local__select_file / local__read_file / local__write_file / local__list_dir）
registerLocalTools(server)

// stdio 启动（父进程 utilityProcess 作为 MCP Client）
export async function start(): Promise<void> {
  const transport = new StdioServerTransport()
  await server.connect(transport)
}
