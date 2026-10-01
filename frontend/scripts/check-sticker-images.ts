/**
 * 缩略图取回/缓存逻辑的断言（`pnpm test:unit`）。
 *
 * 这里钉的都是"真机上会疼但肉眼看不出来"的：
 * 同一张图被请求 60 次、一屏 12 张同时轰出去、后端没这条 JSON 路时还一直撞、
 * 把"老后端回的乱码字节串"当成 base64 去画一张坏图、面板卸载后 objectURL 泄漏。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  UnsupportedStickerImage, base64ToBytes, bytesToBase64, createStickerImageCache,
  parseDataUrl, parseImageEnvelope, stickerImageObjectUrl,
} from '../src/sticker-images.ts'

const here = dirname(fileURLToPath(import.meta.url))

/* ------------------------------------------------------------ 信封解析 */

const PNG = bytesToBase64(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 255]))

// 主形状：{data, mimeType}
assert.deepEqual(parseImageEnvelope({ data: 'AAAA', mimeType: 'image/gif' }), {
  mimeType: 'image/gif', base64: 'AAAA',
})
// 别名：base64 / image / data_url / snake_case 的 mime_type
assert.deepEqual(parseImageEnvelope({ base64: PNG }), { mimeType: 'image/png', base64: PNG })
assert.deepEqual(parseImageEnvelope({ image: 'AAAA', mime_type: 'image/webp' }), {
  mimeType: 'image/webp', base64: 'AAAA',
})
// dataUrl 形状（含 base64 里的换行）
assert.deepEqual(parseImageEnvelope({ dataUrl: `data:image/gif;base64,${PNG}` }), {
  mimeType: 'image/gif', base64: PNG,
})
assert.deepEqual(parseImageEnvelope(`data:image/png;base64,AA\nBB`), { mimeType: 'image/png', base64: 'AABB' })
assert.deepEqual(parseDataUrl('data:image/svg+xml;base64,AAA'), { mimeType: 'image/svg+xml', base64: 'AAA' })
assert.equal(parseDataUrl('https://example.com/x.png'), null)
assert.equal(parseDataUrl('data:image/png,notbase64'), null)

// 关键：老后端把字节当 JSON 回，axios 会解成这种带替换字符的乱码串。
// 它**不能**被当成 base64（丢了字节，画出来是坏图）——必须判成"没有这条路"。
const mangled = '\ufffdPNG\r\n\u001a\n\ufffd\ufffd\ufffd'
assert.equal(parseImageEnvelope(mangled), null, '乱码字节串不许当 base64')
assert.equal(typeof mangled, 'string')
assert.equal(parseImageEnvelope(''), null)
assert.equal(parseImageEnvelope('   '), null)
assert.equal(parseImageEnvelope(null), null)
assert.equal(parseImageEnvelope(42), null)
assert.equal(parseImageEnvelope({ status: 'error', message: '找不到这条素材' }), null)
assert.equal(parseImageEnvelope({ data: 123 }), null)
assert.equal(parseImageEnvelope({ data: '!!!!' }), null, '不是 base64 字符集的就别猜')

// base64 往返
assert.equal(bytesToBase64(base64ToBytes(PNG)), PNG)
assert.deepEqual([...base64ToBytes('AAECAw==')], [0, 1, 2, 3])

/* ------------------------------------------------------------ 缓存与并发 */

function envelope(n = 1): { mimeType: string; base64: string } {
  return { mimeType: 'image/png', base64: bytesToBase64(new Uint8Array([n, n, n])) }
}

function harness(options: { fail?: (assetId: string) => boolean; unsupported?: (assetId: string) => boolean; hold?: number } = {}) {
  const loads: string[] = []
  const released: string[] = []
  let live = 0
  let peak = 0
  const cache = createStickerImageCache({
    concurrency: 2,
    retries: 1,
    load: async (assetId) => {
      loads.push(assetId)
      live += 1
      peak = Math.max(peak, live)
      try {
        await new Promise((resolve) => setTimeout(resolve, options.hold ?? 1))
        if (options.unsupported?.(assetId)) throw new UnsupportedStickerImage()
        if (options.fail?.(assetId)) throw new Error('网络抖了一下')
        return envelope(loads.length)
      } finally {
        live -= 1
      }
    },
    toUrl: (payload) => `blob:${payload.base64}`,
    release: (url) => released.push(url),
  })
  return { cache, loads, released, peak: () => peak }
}

// 命中缓存：同一条只取一次
{
  const { cache, loads } = harness()
  const first = await cache.get('a')
  const second = await cache.get('a')
  assert.equal(first, second)
  assert.equal(first, 'blob:' + envelope(1).base64)
  assert.deepEqual(loads, ['a'], '同一 assetId 只许取一次')
  assert.equal(cache.stats().hits, 1)
  assert.equal(cache.peek('a'), first)
  assert.equal(cache.peek('zzz'), null)
}

// 并发闸：6 条同时要，最多 2 个在飞
{
  const { cache, peak } = harness({ hold: 5 })
  const all = ['a', 'b', 'c', 'd', 'e', 'f'].map((id) => cache.get(id))
  assert.ok(cache.inFlight() > 0, '排队里应该有活')
  const urls = await Promise.all(all)
  assert.equal(urls.filter(Boolean).length, 6)
  assert.ok(peak() <= 2, `同时最多 2 个在飞，实测 ${peak()}`)
  assert.equal(cache.inFlight(), 0)
}

// 同一条并发要两次：搭同一班车，不重复请求
{
  const { cache, loads } = harness({ hold: 5 })
  const [one, two] = await Promise.all([cache.get('a'), cache.get('a')])
  assert.equal(one, two)
  assert.deepEqual(loads, ['a'])
}

// 失败重试一次；两次都失败就回 null（不抛，界面显示占位框）
{
  const { cache, loads } = harness({ fail: () => true })
  assert.equal(await cache.get('a'), null)
  assert.deepEqual(loads, ['a', 'a'], '失败要重试一次')
  assert.equal(cache.stats().failures, 1)
  assert.equal(cache.supported(), true, '普通失败不代表后端没这条路')
  // 失败不缓存：再问一次还会重试
  assert.equal(await cache.get('a'), null)
  assert.deepEqual(loads, ['a', 'a', 'a', 'a'])
}
{
  const { cache, loads } = harness({ fail: (id) => id === 'bad' })
  assert.equal(await cache.get('bad'), null)
  const good = await cache.get('good')
  assert.ok(good, '一条失败不影响别的')
  assert.deepEqual(loads, ['bad', 'bad', 'good'])
}

// 后端没有这条 JSON 路：撞一次就停手，别拿 60 张图去撞墙
{
  const { cache, loads } = harness({ unsupported: () => true })
  assert.equal(await cache.get('a'), null)
  assert.equal(cache.supported(), false, '撞过一次就要记下来')
  assert.deepEqual(loads, ['a'], '不支持时不重试（重试也没有意义）')
  assert.equal(await cache.get('b'), null)
  assert.equal(await cache.get('c'), null)
  assert.deepEqual(loads, ['a'], '之后一条请求都不许再发')
}

// 头一次取图之前单飞：6 条同时要、后端没这条路 —— 只许 1 个请求出去
{
  const { cache, loads } = harness({ unsupported: () => true, hold: 5 })
  const all = ['a', 'b', 'c', 'd', 'e', 'f'].map((id) => cache.get(id))
  const urls = await Promise.all(all)
  assert.deepEqual(urls, [null, null, null, null, null, null])
  assert.deepEqual(loads, ['a'], `探路只许发一次，实测 ${loads.length} 次`)
}

// 探明"路是通的"之后就恢复并发（别一直单飞拖慢列表）
{
  const { cache, peak, loads } = harness({ hold: 5 })
  const first = await cache.get('first')
  assert.ok(first)
  const rest = ['a', 'b', 'c', 'd'].map((id) => cache.get(id))
  await Promise.all(rest)
  assert.equal(loads.length, 5)
  assert.ok(peak() <= 2, '探明之后就按并发闸跑')
}

// dispose：释放所有 objectURL、清空缓存、不泄漏
{
  const { cache, released } = harness()
  const one = await cache.get('a')
  const two = await cache.get('b')
  assert.ok(one && two)
  cache.dispose()
  assert.deepEqual(released.sort(), [one, two].sort())
  assert.equal(cache.stats().released, 2)
  assert.equal(cache.peek('a'), null, 'dispose 之后缓存要清空')
  assert.equal(cache.inFlight(), 0)
  cache.dispose()
  assert.equal(cache.stats().released, 2, '重复 dispose 不许重复释放')
}

// 空 assetId 不发请求
{
  const { cache, loads } = harness()
  assert.equal(await cache.get(''), null)
  assert.deepEqual(loads, [])
}

/* ------------------------------------------------- 生产金样（真后端取下来的那份） */

// fixtures/sticker-inline-response.json 是**真实后端** `console/sticker-file?inline=1` 的
// 响应（生成命令见 fixtures/README.md）。这里不造形状：直接拿它喂解析器——
// 后端哪天改了字段名/集合/MIME/base64 方式，这条断言立刻红，逼两边一起动。
const goldenRaw = readFileSync(join(here, 'fixtures', 'sticker-inline-response.json'), 'utf8')
const golden = JSON.parse(goldenRaw) as Record<string, unknown>
assert.deepEqual(
  Object.keys(golden).sort(),
  ['assetId', 'data', 'mimeType', 'size'],
  '金样的四键集合变了（后端形状漂了？先看 fixtures/README.md）',
)
assert.equal(typeof golden.assetId, 'string')
assert.equal(typeof golden.size, 'number')

const goldenPayload = parseImageEnvelope(golden)
assert.ok(goldenPayload, '金样必须能被前端解析器认出来（认不出 = 夹具过期或解析器漏了形状）')
assert.equal(goldenPayload?.mimeType, 'image/png', 'MIME 要原样接受，别自己改成别的')
const goldenBytes = base64ToBytes(goldenPayload!.base64)
assert.equal(goldenBytes.length, golden.size, 'data 解出来的字节数必须等于 size')
assert.deepEqual(
  [...goldenBytes.slice(0, 8)],
  [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a],
  'data 得是真 PNG 字节',
)
// Blob / objectURL 链路：Node 没有 createObjectURL，注入假工厂看它拿到什么。
let seen: Blob | null = null
const goldenUrl = stickerImageObjectUrl(goldenPayload!, (blob) => {
  seen = blob
  return 'blob:golden'
})
assert.equal(goldenUrl, 'blob:golden')
assert.equal(seen?.size, golden.size, 'Blob 的字节数要等于 size')
assert.equal(seen?.type, 'image/png', 'Blob 的 type 要用信封里的 mimeType')
// 注入工厂只是**可选**参数：不传时走真正的 `URL.createObjectURL`（浏览器路径）。
// 新版 Node 也有这个 API，所以这里能顺手把默认路径也跑一遍（没有就跳过）。
if (typeof URL.createObjectURL === 'function') {
  const realUrl = stickerImageObjectUrl(goldenPayload!)
  assert.ok(realUrl.startsWith('blob:'), `默认路径要回 blob: 地址，实测 ${realUrl}`)
  URL.revokeObjectURL(realUrl)
}

console.log('sticker-images ok（含生产金样：四键齐 / 真 PNG 字节 / Blob 链路）')
