// PLAN P3.5，PRD 语音交互：支持语音输入，适配生产车间、仓库等场景
// Web Speech API 封装（Electron Chromium 原生支持，无外部 SDK）：
//   输入 = SpeechRecognition（zh-CN，实时 interim 结果）
//   播报 = speechSynthesis（final 回复朗读，免手操作）

// —— Web Speech API 最小类型声明（TS DOM lib 未内置，仅覆盖用到的事件面） ——
interface SpeechRecognitionAlternativeLike {
  transcript: string
  confidence: number
}

interface SpeechRecognitionResultLike {
  readonly length: number
  isFinal: boolean
  [index: number]: SpeechRecognitionAlternativeLike
}

interface SpeechRecognitionResultListLike {
  readonly length: number
  [index: number]: SpeechRecognitionResultLike
}

interface SpeechRecognitionEventLike extends Event {
  resultIndex: number
  results: SpeechRecognitionResultListLike
}

interface SpeechRecognitionErrorEventLike extends Event {
  /** no-speech / aborted / audio-capture / network / not-allowed / service-not-allowed 等 */
  error: string
}

interface SpeechRecognitionLike extends EventTarget {
  lang: string
  continuous: boolean
  interimResults: boolean
  maxAlternatives: number
  start(): void
  stop(): void
  abort(): void
  onresult: ((event: SpeechRecognitionEventLike) => void) | null
  onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null
  onend: (() => void) | null
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike

function getRecognitionCtor(): SpeechRecognitionCtor | null {
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor
    webkitSpeechRecognition?: SpeechRecognitionCtor
  }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

/** 语音输入能力探测（Chromium 内核可用；Firefox 不支持） */
export function speechInputSupported(): boolean {
  return getRecognitionCtor() !== null
}

/** TTS 播报能力探测 */
export function ttsSupported(): boolean {
  return (
    typeof window.speechSynthesis !== 'undefined' &&
    typeof window.SpeechSynthesisUtterance !== 'undefined'
  )
}

const ERROR_LABELS: Record<string, string> = {
  'not-allowed': '麦克风权限被拒绝，请在系统设置中允许后重试',
  'service-not-allowed': '语音识别服务不可用，请检查系统权限',
  'no-speech': '未检测到语音，请靠近麦克风重试',
  'audio-capture': '未找到可用麦克风',
  network: '语音识别服务网络异常，请稍后重试',
  'language-not-supported': '当前语言不受支持',
  'bad-grammar': '语音识别语法错误'
}

export interface RecognizerHandlers {
  /** 实时识别文本（含临时结果，全量累积） */
  onPartial: (text: string) => void
  /** 错误提示（已转中文） */
  onError: (message: string) => void
  /** 会话结束（自动停顿 / 手动停止 / 出错后） */
  onEnd: () => void
}

export interface Recognizer {
  start: () => void
  stop: () => void
}

/** 单次语音听写会话（车间/仓库短指令场景：说完停顿即结束） */
export function createRecognizer(handlers: RecognizerHandlers): Recognizer {
  const Ctor = getRecognitionCtor()
  if (!Ctor) {
    handlers.onError('当前环境不支持语音识别（需 Chromium 内核客户端）')
    handlers.onEnd()
    return { start() {}, stop() {} }
  }
  const rec = new Ctor()
  rec.lang = 'zh-CN'
  rec.continuous = false
  rec.interimResults = true
  rec.maxAlternatives = 1
  rec.onresult = (event) => {
    let text = ''
    for (let i = 0; i < event.results.length; i += 1) {
      const result = event.results[i]
      const alt = result?.[0]
      if (alt) text += alt.transcript
    }
    handlers.onPartial(text)
  }
  rec.onerror = (event) => {
    // aborted 为用户手动停止，无需提示
    const label = ERROR_LABELS[event.error]
    if (label) handlers.onError(label)
  }
  rec.onend = () => handlers.onEnd()
  return {
    start: () => rec.start(),
    stop: () => rec.stop()
  }
}

/** TTS 播报 final 回复（重复调用会打断上一段播报） */
export function speak(text: string): void {
  if (!ttsSupported() || !text) return
  window.speechSynthesis.cancel()
  const utter = new SpeechSynthesisUtterance(text)
  utter.lang = 'zh-CN'
  utter.rate = 1
  window.speechSynthesis.speak(utter)
}

/** 停止播报（关闭播报开关时调用） */
export function cancelSpeak(): void {
  if (ttsSupported()) window.speechSynthesis.cancel()
}
