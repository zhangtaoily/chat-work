// 本地文件工具声明（骨架占位）
// 安全边界：所有操作限定「授权目录」+「扩展名白名单」（授权来自用户显式选择）
// 工具 Schema 源自 packages/protocol/tools/local__*.json（TODO：CI 同步生成）
import type { Server } from '@modelcontextprotocol/sdk/server/index.js'
import type { Tool } from '@modelcontextprotocol/sdk/types.js'

// 工具名常量（与 protocol JSON 保持一致）
export const LocalTools = {
  SelectFile: 'local__select_file',
  ReadFile: 'local__read_file',
  WriteFile: 'local__write_file',
  ListDir: 'local__list_dir'
} as const

// 工具定义占位（完整 inputSchema 见 packages/protocol）
const toolDefs: Tool[] = [
  {
    name: LocalTools.SelectFile,
    description: '选择本地文件（用户显式选择即授权动作）',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false }
  },
  {
    name: LocalTools.ReadFile,
    description: '读取授权目录内文件（扩展名白名单：txt/csv/md/json/xlsx）',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false }
  },
  {
    name: LocalTools.WriteFile,
    description: '写入授权目录内文件（覆盖已有文件需二次确认）',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false }
  },
  {
    name: LocalTools.ListDir,
    description: '列出授权目录内容',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false }
  }
]

// 注册 tools/list 与 tools/call 处理器（骨架占位）
export function registerLocalTools(server: Server): void {
  // TODO: server.setRequestHandler(ListToolsRequestSchema, ...) 返回 toolDefs
  // TODO: server.setRequestHandler(CallToolRequestSchema, ...)：
  //       校验路径在授权目录内 + 扩展名在白名单内，再执行文件操作
  void server
  void toolDefs
}
