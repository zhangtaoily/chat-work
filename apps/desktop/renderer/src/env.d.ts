/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** agent-core 服务地址（开发期默认本机 8011，生产经网关注入） */
  readonly VITE_AGENT_CORE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

// preload 白名单桥（Electron 内注入；浏览器 dev:web 模式无此对象，走直连 mock 流程）
interface Window {
  readonly chatwork?: typeof import('../preload/bridge').bridge
}
