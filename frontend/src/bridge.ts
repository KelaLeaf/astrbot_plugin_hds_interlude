/**
 * AstrBot 插件页 bridge 的薄封装。
 *
 * 宿主会在页面里注入 `window.AstrBotPluginPage`（`/api/plugin/page/bridge-sdk.js`）。
 * 这里只做三件事：把回调风格的接口包成 Promise、统一错误信息、给页面提供
 * 一个「取数 + 重试」的小 hook。**没有引入任何请求库**——bridge 自己就是传输层。
 */

export interface BridgeContext {
  locale?: string
  isDark?: boolean
  i18n?: Record<string, unknown>
  [key: string]: unknown
}

interface PluginBridge {
  ready(): Promise<BridgeContext>
  getContext(): BridgeContext | null
  getLocale(): string
  t(key: string, fallback?: string): string
  onContext(handler: (context: BridgeContext) => void): () => void
  apiGet(endpoint: string, params?: Record<string, unknown>): Promise<unknown>
  apiPost(endpoint: string, body?: unknown): Promise<unknown>
  upload(endpoint: string, file: File): Promise<unknown>
  download(endpoint: string, params?: Record<string, unknown>, filename?: string): Promise<unknown>
}

declare global {
  interface Window {
    AstrBotPluginPage?: PluginBridge
  }
}

function bridge(): PluginBridge {
  const api = window.AstrBotPluginPage
  if (!api) {
    throw new Error('插件页通道未就绪（window.AstrBotPluginPage 不存在）')
  }
  return api
}

/** 页面各处的请求都走这里：统一把未知异常压成可读文案。 */
function describe(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  try {
    return JSON.stringify(error)
  } catch {
    return String(error)
  }
}

export async function apiGet<T>(endpoint: string, params?: Record<string, unknown>): Promise<T> {
  try {
    return (await bridge().apiGet(endpoint, params)) as T
  } catch (error) {
    throw new Error(describe(error))
  }
}

export async function apiPost<T>(endpoint: string, body?: unknown): Promise<T> {
  try {
    return (await bridge().apiPost(endpoint, body)) as T
  } catch (error) {
    throw new Error(describe(error))
  }
}

export async function uploadFile<T>(endpoint: string, file: File): Promise<T> {
  try {
    return (await bridge().upload(endpoint, file)) as T
  } catch (error) {
    throw new Error(describe(error))
  }
}

export async function downloadFile(
  endpoint: string,
  params: Record<string, unknown>,
  filename: string,
): Promise<void> {
  try {
    await bridge().download(endpoint, params, filename)
  } catch (error) {
    throw new Error(describe(error))
  }
}

/** 等 bridge 把宿主上下文（主题、语言）推过来。 */
export async function bootstrap(): Promise<BridgeContext> {
  const api = bridge()
  return await api.ready()
}

/** 订阅主题/语言变化；返回取消订阅函数。 */
export function onContext(handler: (context: BridgeContext) => void): () => void {
  return bridge().onContext(handler)
}

export function translate(key: string, fallback: string): string {
  try {
    return bridge().t(key, fallback) || fallback
  } catch {
    return fallback
  }
}

export { describe as describeError }
