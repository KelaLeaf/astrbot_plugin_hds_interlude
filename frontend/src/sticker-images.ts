/**
 * 缩略图字节的取回、缓存、并发闸与生命周期（纯逻辑 + 注入副作用）。
 *
 * 为什么图片非走 bridge 不可（宿主级约束，实测）：
 * 插件页跑在 `<iframe sandbox="allow-scripts allow-forms allow-downloads">` 里 → 文档是
 * opaque origin → 它发起的任何子资源请求都被 Chrome 判成 `Sec-Fetch-Site: cross-site`，
 * 宿主那枚 `astrbot_dashboard_jwt`（`SameSite=Strict`）不会带上，而扩展路由
 * `require_plugin_scope` 只认 Authorization 头或这枚 cookie → `<img src="…/console/sticker-file">`
 * 必然 401。**只有宿主 bridge（postMessage 代理、在宿主自己的页面里带登录态取）能拿到数据。**
 *
 * 但宿主的 bridge 只回 JSON（`apiGet` 走 axios 的 json 通道），拿不回 blob；
 * `download` 是"存盘"不是"给我字节"。所以后端要有一条**回 JSON 信封**的取图路
 * （形状见 `parseImageEnvelope` 的注释）。这里把"取回来之后怎么办"做扎实：
 *
 * - 同一 assetId 只取一次（命中缓存直接给 URL）；
 * - 同时最多 `concurrency` 个在飞，其余排队（列表一屏 12 张，别一次轰 200 个请求）；
 * - 失败重试一次；后端**没有**这条 JSON 路时（`UnsupportedStickerImage`）立刻停手，
 *   之后所有请求直接失败——不要拿 60 次 401/乱码去撞墙；
 * - `dispose()` 释放全部 objectURL（面板卸载时调用，别泄漏）。
 *
 * 副作用全部注入（`load` / `toUrl` / `release`），所以这个模块能在 Node 里直接断言：
 *     cd plugin/frontend && pnpm test:unit      # scripts/check-sticker-images.ts
 *
 * 这个文件**不 import 任何东西**（连 `./bridge` 都不）：Node 的 TS 剥离不解析无扩展名的
 * 相对导入，纯逻辑模块才能被 check 脚本直接跑。浏览器侧那三个函数由面板自己组装。
 */
/** 后端回给我们的图片信封：MIME + base64（不含 `data:` 前缀）。 */
export interface StickerImagePayload {
  mimeType: string
  base64: string
}

/**
 * 后端**没有**这条 JSON 取图路（老后端：`sticker-file` 直接回字节）。
 *
 * 为什么要单独一个错误类型：它和"这次网络抖了一下"是两回事——前者要立刻停手
 * （别再问 60 次），后者值得重试一次。
 */
export class UnsupportedStickerImage extends Error {
  constructor(message = '当前后端没有 JSON 取图路') {
    super(message)
    this.name = 'UnsupportedStickerImage'
  }
}

const BASE64 = /^[A-Za-z0-9+/=\s]+$/

/** `data:image/png;base64,AAAA` → 信封；不是 data URL 就回 `null`。 */
export function parseDataUrl(value: unknown): StickerImagePayload | null {
  if (typeof value !== 'string') return null
  const match = /^data:([^;,]+);base64,([\s\S]*)$/.exec(value.trim())
  if (!match) return null
  const base64 = match[2].replace(/\s+/g, '')
  if (!base64 || !BASE64.test(base64)) return null
  return { mimeType: match[1] || 'image/png', base64 }
}

/**
 * 解析取图响应。
 *
 * 只认**对象信封**：
 * - `{data: "<base64>", mimeType?}` / `{base64: "<base64>", mimeType?}`
 * - `{dataUrl: "data:image/png;base64,…"}`
 * 别的（裸字符串 / 数字 / null）一律当"后端没这条形状"——**这一点是有意的**：
 * 老后端把 PNG 字节当 JSON 回，axios 会把它按 UTF-8 解成一串带替换字符的乱码字符串，
 * 谁也不能把它当 base64 用（丢了字节，变不回去）。宁可显示占位框，也不要画一张坏图。
 */
export function parseImageEnvelope(value: unknown): StickerImagePayload | null {
  if (typeof value === 'string') return parseDataUrl(value)
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const fromDataUrl = parseDataUrl(record.dataUrl ?? record.data_url)
  if (fromDataUrl) return fromDataUrl
  const raw = record.data ?? record.base64 ?? record.image
  if (typeof raw !== 'string') return null
  const base64 = raw.trim().replace(/\s+/g, '')
  if (!base64 || !BASE64.test(base64)) return null
  const mimeType = typeof record.mimeType === 'string' && record.mimeType
    ? record.mimeType
    : (typeof record.mime_type === 'string' && record.mime_type ? record.mime_type : 'image/png')
  return { mimeType, base64 }
}

/** base64 → 字节（Node 与浏览器都有 `atob`）。 */
export function base64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64.replace(/\s+/g, ''))
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
  return bytes
}

/** 字节 → base64（桩数据/harness 用）。 */
export function bytesToBase64(bytes: Uint8Array): string {
  let binary = ''
  for (let index = 0; index < bytes.length; index += 1) binary += String.fromCharCode(bytes[index])
  return btoa(binary)
}

export interface StickerImageCache {
  /** 取一条素材的图片地址（objectURL / data URL）；失败回 `null`。 */
  get: (assetId: string) => Promise<string | null>
  /** 已经在缓存里的地址（同步，渲染时先用它，避免闪一下占位框）。 */
  peek: (assetId: string) => string | null
  /**
   * 上一条**失败理由**（后端 404 的 `message`，里面带着"已找过哪儿"）；没失败过回空串。
   *
   * 为什么要留它：占位框只写"取不到图"，用户与维护者都看不出它找的是哪儿——真机上的
   * 404 就是这么瞎的。这里只负责**记录**，怎么显示由面板决定（`stickers-view.brokenTitle()`）。
   * 网络抖这类没有后端消息的失败回空串（**不编原因**）。
   */
  reason: (assetId: string) => string
  /** 后端有没有这条 JSON 取图路（`false` = 已经撞过一次，别再问）。 */
  supported: () => boolean
  /** 还在飞/排队的条数（harness 断言用）。 */
  inFlight: () => number
  stats: () => { hits: number; loads: number; failures: number; released: number }
  /** 释放全部 objectURL 并清空缓存（面板卸载时调用）。 */
  dispose: () => void
}

export interface StickerImageCacheOptions {
  /** 真正取一条素材（浏览器里 = 过一次 `apiGet`；测试里 = 假函数）。 */
  load: (assetId: string) => Promise<StickerImagePayload>
  /** 信封 → 可直接当 `src` 的地址（浏览器 = base64→Blob→objectURL）。 */
  toUrl: (payload: StickerImagePayload) => string
  /** 释放地址（浏览器 = `URL.revokeObjectURL`）。 */
  release: (url: string) => void
  /** 同时最多几个在飞（默认 4）。 */
  concurrency?: number
  /** 失败重试几次（默认 1）。 */
  retries?: number
}

export function createStickerImageCache(options: StickerImageCacheOptions): StickerImageCache {
  const concurrency = Math.max(1, Math.floor(options.concurrency ?? 4))
  const retries = Math.max(0, Math.floor(options.retries ?? 1))
  const urls = new Map<string, string>()
  //: 失败理由（assetId → 后端 message）。只在失败时写、成功时清。
  const reasons = new Map<string, string>()
  const waiters = new Map<string, Array<(url: string | null) => void>>()
  const queue: Array<() => void> = []
  let running = 0
  let unsupported = false
  // 头一次取图之前先**单飞**：一次探明"后端到底有没有这条 JSON 取图路"。
  // 否则一屏 12 张会同时轰出去，老后端上要白挨 4 个请求（实测 2 个）才能收敛。
  let capability: 'unknown' | 'ok' = 'unknown'
  const stats = { hits: 0, loads: 0, failures: 0, released: 0 }

  function pump() {
    const limit = capability === 'unknown' ? 1 : concurrency
    while (running < limit && queue.length) {
      const start = queue.shift()
      if (start) {
        running += 1
        start()
      }
    }
  }

  function done() {
    running -= 1
    pump()
  }

  async function attempt(assetId: string): Promise<StickerImagePayload> {
    let lastError: unknown = null
    for (let tryIndex = 0; tryIndex <= retries; tryIndex += 1) {
      try {
        stats.loads += 1
        return await options.load(assetId)
      } catch (error) {
        if (error instanceof UnsupportedStickerImage) throw error
        lastError = error
      }
    }
    throw lastError instanceof Error ? lastError : new Error(String(lastError))
  }

  function enqueue(assetId: string): Promise<string | null> {
    return new Promise<string | null>((resolve) => {
      const list = waiters.get(assetId) ?? []
      list.push(resolve)
      waiters.set(assetId, list)
      if (list.length > 1) return // 同一条已经在飞：搭车，不再排一次
      queue.push(() => {
        void (async () => {
          // 已经确认"后端没这条路"了：排队的直接判失败，一次网络都不发。
          if (unsupported) {
            const skipped = waiters.get(assetId) ?? []
            waiters.delete(assetId)
            stats.failures += 1
            for (const resolveWaiter of skipped) resolveWaiter(null)
            done()
            return
          }
          let url: string | null = null
          try {
            const payload = await attempt(assetId)
            capability = 'ok'
            url = options.toUrl(payload)
            urls.set(assetId, url)
            reasons.delete(assetId)
          } catch (error) {
            if (error instanceof UnsupportedStickerImage) {
              unsupported = true
            } else {
              // 非"没这条路"的失败（网络抖/文件不在）说明路是通的，放开并发。
              capability = 'ok'
              // 后端给的理由（404 的 message 里带着"已找过哪儿"）原样留下来；
              // 没有 message 的失败（网络抖动）留空——**不编一个理由**。
              const message = error instanceof Error ? error.message.trim() : ''
              if (message) reasons.set(assetId, message)
            }
            stats.failures += 1
            url = null
          } finally {
            const waiting = waiters.get(assetId) ?? []
            waiters.delete(assetId)
            for (const resolveWaiter of waiting) resolveWaiter(url)
            done()
          }
        })()
      })
      pump()
    })
  }

  return {
    get(assetId) {
      const key = String(assetId || '')
      if (!key) return Promise.resolve(null)
      const cached = urls.get(key)
      if (cached) {
        stats.hits += 1
        return Promise.resolve(cached)
      }
      if (unsupported) {
        stats.failures += 1
        return Promise.resolve(null)
      }
      return enqueue(key)
    },
    peek(assetId) {
      return urls.get(String(assetId || '')) ?? null
    },
    reason(assetId) {
      return reasons.get(String(assetId || '')) ?? ''
    },
    supported() {
      return !unsupported
    },
    inFlight() {
      return running + queue.length
    },
    stats() {
      return { ...stats }
    },
    dispose() {
      for (const url of urls.values()) {
        options.release(url)
        stats.released += 1
      }
      urls.clear()
      reasons.clear()
      waiters.clear()
      queue.length = 0
    },
  }
}
/**
 * 浏览器侧：信封 → objectURL（用完必须 `revokeStickerImageUrl`）。
 *
 * `create` 可注入：Node 里没有 `URL.createObjectURL`，check 脚本靠传入假工厂来断言
 * "拼出来的 Blob 大小/类型对不对"（金样断言用的就是这条链）。
 */
export function stickerImageObjectUrl(
  payload: StickerImagePayload,
  create: (blob: Blob) => string = (blob) => URL.createObjectURL(blob),
): string {
  const bytes = base64ToBytes(payload.base64)
  return create(new Blob([bytes.slice().buffer], { type: payload.mimeType || 'image/png' }))
}

export function revokeStickerImageUrl(url: string): void {
  try {
    URL.revokeObjectURL(url)
  } catch {
    /* 已经释放过就算了 */
  }
}

/*
 * 「取一条素材」那一步（`load`）不放在这里：它要用 `./bridge` 的 apiGet，而无扩展名的
 * 相对导入在 Node 的 TS 剥离下解析不了（check 脚本就跑不起来）。面板自己组装：
 *
 *   createStickerImageCache({
 *     load: async (assetId) => {
 *       const value = await apiGet('console/sticker-file', { assetId, inline: 1 })
 *       const payload = parseImageEnvelope(value)
 *       if (!payload) throw new UnsupportedStickerImage()   // 后端没这条 JSON 路
 *       return payload
 *     },
 *     toUrl: stickerImageObjectUrl,
 *     release: revokeStickerImageUrl,
 *     concurrency: 4,
 *   })
 */
