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
  /** 宿主给的插件名（页面所在插件）。相对端点要靠它才能拼成真实地址。 */
  pluginName?: string
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

/** WebUI 的 API 根 + 插件扩展接口前缀（宿主自己也是这么拼的）。 */
const EXTENSION_PREFIX = '/api/v1/plugins/extensions'
/** 已经是绝对地址（`http(s):` / `data:` / `blob:` / `//host` / `/path`）就直接用。 */
const ABSOLUTE_URL = /^(?:[a-z][a-z0-9+.-]*:|\/\/|\/)/i

/**
 * 把一个**相对端点**解析成可以直接用的真实地址（`<img src>`、`fetch` 等）。
 *
 * 为什么需要它：`apiGet` / `apiPost` 走的是宿主 postMessage 通道、拿回来的是 JSON，
 * 图片字节（`console/sticker-file`）走不了那条路；而后端给的 `thumbnailUrl` 是**相对**
 * 地址，必须拼上插件名才取得回来。
 *
 * 插件名只从宿主上下文（`pluginName`）里取，**不写死在源码里**——同一份构建产物要能
 * 在用户自己的插件目录名下工作。上下文还没就绪 / 拿不到插件名时回空串，
 * 调用方据此显示占位图（绝不拼一个必然 404 的地址出来）。
 */
export function endpointUrl(endpoint: string): string {
  const path = String(endpoint || '').trim()
  if (!path) return ''
  // 后端哪天改成回绝对地址（或内联 data:）也不用改前端。
  if (ABSOLUTE_URL.test(path)) return path
  let name = ''
  try {
    const context = bridge().getContext()
    name = typeof context?.pluginName === 'string' ? context.pluginName.trim() : ''
  } catch {
    return ''
  }
  if (!name) return ''
  return `${EXTENSION_PREFIX}/${encodeURIComponent(name)}/${path}`
}

export { describe as describeError }
