/**
 * 「表情库」面板纯逻辑的断言（`pnpm test:unit`）。
 *
 * 这里钉的都是"界面上看不出来的错"：把"你手写的描述"显示成"自动描述"、
 * 把"只标记（可恢复）"和"真删文件（不可恢复）"说成同一件事、
 * 长列表只看得到 60 条却不说还有多少、就地更新之后合计数字悄悄变成错的。
 */
import assert from 'node:assert/strict'
import {
  DEFAULT_PAGE_SIZE, DESCRIPTION_LIMIT, EMPTY_FILTERS, MARK_DELETE_LABEL, NAME_LIMIT,
  PAGE_SIZE_MAX, PURGE_DELETE_LABEL, activeFilterCount, deleteDoneNote, deletePayload,
  deleteWarning, descriptionOwner, disabledPayload, emptyHint, fileLabel, isManual, kindLabel,
  mergeStickerItems, pageOffset, pageSize, pageWindow, rescanNote, restorePayload,
  resultSummary, saveBlocker, sourceLabel, staleOverridesNote, statusHint, statusLabel,
  statusTone, stickerParams, stickerTitle, thumbnailEndpoint, updateNote, updatePayload,
} from '../src/stickers-view.ts'
import type { StickerItem } from '../src/types.ts'

function item(patch: Partial<StickerItem> = {}): StickerItem {
  return {
    assetId: 'sticker-1a2b3c4d',
    name: 'collected-1a2b3c4d',
    description: '一只挥手的猫',
    kind: 'image',
    source: 'auto',
    addedAt: '2026-09-01T10:00:00+00:00',
    updatedAt: '2026-09-01T10:00:00+00:00',
    uses: 3,
    disabled: false,
    file: 'collected/1a2b3c4d.png',
    thumbnailUrl: 'console/sticker-file?assetId=sticker-1a2b3c4d',
    manual: false,
    status: 'active',
    group: 'collected',
    size: 40960,
    aliases: [],
    mimeType: 'image/png',
    ...patch,
  }
}

/* ---------------------------------------------------------------- 筛选串 */

// 没选任何筛选：只发分页（空串用 trim 之后判定，别把空格当成一次查询）。
assert.deepEqual(stickerParams(EMPTY_FILTERS, 60, 0), { limit: 60, offset: 0 })
assert.deepEqual(stickerParams({ ...EMPTY_FILTERS, q: '   ' }, 60, 0), { limit: 60, offset: 0 })
assert.equal(activeFilterCount(EMPTY_FILTERS), 0)
assert.equal(activeFilterCount({ ...EMPTY_FILTERS, q: '  ' }), 0)

// 选了就带上，且只带选了的；q 去首尾空白。
assert.deepEqual(
  stickerParams({ status: 'active', kind: 'animated', source: 'auto', q: ' 猫 ' }, 30, 90),
  { limit: 30, offset: 90, status: 'active', kind: 'animated', source: 'auto', q: '猫' },
)
assert.equal(activeFilterCount({ status: 'active', kind: '', source: '', q: '猫' }), 2)

// limit / offset 归一化：后端上限 200，默认 60；脏数据不炸。
assert.equal(DEFAULT_PAGE_SIZE, 60)
assert.equal(pageSize(60), 60)
assert.equal(pageSize(999), PAGE_SIZE_MAX, 'limit 必须卡在后端上限内')
assert.equal(pageSize(0), DEFAULT_PAGE_SIZE)
assert.equal(pageSize(-3), DEFAULT_PAGE_SIZE)
assert.equal(pageSize('abc'), DEFAULT_PAGE_SIZE)
assert.equal(pageOffset(-5), 0)
assert.equal(pageOffset(2.9), 2)
assert.equal(pageOffset(undefined), 0)
assert.deepEqual(stickerParams(EMPTY_FILTERS, 999, -1), { limit: 200, offset: 0 })

/* ---------------------------------------------------------------- 分页窗口 */

// 空结果：1/1，两个按钮都不能按（不是 0/0）。
assert.deepEqual(pageWindow(0, 60, 0), {
  start: 0, size: 60, page: 1, pages: 1, first: 0, last: 0, hasPrev: false, hasNext: false,
})
// 137 条、每页 60：三页；第二页是 61–120。
assert.deepEqual(pageWindow(137, 60, 0), {
  start: 0, size: 60, page: 1, pages: 3, first: 1, last: 60, hasPrev: false, hasNext: true,
})
const second = pageWindow(137, 60, 60)
assert.equal(second.page, 2)
assert.equal(second.pages, 3)
assert.equal(second.first, 61)
assert.equal(second.last, 120)
assert.equal(second.hasPrev, true)
assert.equal(second.hasNext, true)
const last = pageWindow(137, 60, 120)
assert.equal(last.page, 3)
assert.equal(last.last, 137)
assert.equal(last.hasNext, false)

// 偏移超出范围（删了几条之后停在最后一页之外）：收敛到最后一页，不是空白页。
const overflow = pageWindow(137, 60, 5_000)
assert.equal(overflow.start, 120)
assert.equal(overflow.page, 3)
assert.equal(overflow.last, 137)
// 脏数据。
assert.equal(pageWindow(Number.NaN, 60, 10).page, 1)
assert.equal(pageWindow(-4, 60, 10).start, 0)

/* ---------------------------------------------------------------- 合计行 */

const summary = resultSummary({ total: 137, shown: 60, limit: 60, offset: 60, truncated: false })
assert.match(summary, /共 137 条/)
assert.match(summary, /本页 60 条（第 61–120 条）/, '只说"共 137 条"会让用户以为这一页就是全部')
assert.doesNotMatch(summary, /最近的一批/)
const truncated = resultSummary({ total: 137, shown: 60, limit: 60, offset: 0, truncated: true })
assert.match(truncated, /共 137 条/)
assert.match(truncated, /更早的没算进来/, 'truncated 必须让用户看见，别以为库里就这么多')
assert.equal(
  resultSummary({ total: 0, shown: 0, limit: 60, offset: 0, truncated: false }),
  '共 0 条 · 本页 0 条',
)

/* ---------------------------------------------------------------- 徽章 */

assert.equal(statusLabel('active'), '在用')
assert.equal(statusLabel('pending'), '待自动描述')
assert.equal(statusLabel('missing'), '已移除')
assert.equal(statusLabel('disabled'), '已停用')
assert.equal(statusLabel('weird'), 'weird', '不认识的状态照原样显示，不要编一个')
assert.equal(statusLabel(''), '未知状态')
assert.equal(statusTone('active'), 'ok')
assert.equal(statusTone('pending'), 'warn')
assert.equal(statusTone('missing'), 'danger')
assert.equal(statusTone('disabled'), 'neutral')
assert.equal(statusTone('weird'), 'neutral')
// 四种状态的悬停说明必须各不相同（"待描述"和"已移除"混了就等于没说）。
const hints = ['active', 'pending', 'missing', 'disabled'].map((value) => statusHint(value))
assert.equal(new Set(hints).size, 4)
assert.match(statusHint('missing'), /重扫/, '移除可恢复这条要写在说明里')
assert.equal(kindLabel('image'), '静止图')
assert.equal(kindLabel('animated'), '动图')
assert.equal(kindLabel(''), '未知形式')
assert.equal(sourceLabel('auto'), '自动收藏')
assert.equal(sourceLabel('manual'), '扫盘入库')
assert.equal(sourceLabel(''), '未知来源')

/* ------------------------------------------- 人工描述 vs 自动描述（用户点名） */

const manual = descriptionOwner(item({ manual: true }))
const auto = descriptionOwner(item({ manual: false }))
assert.equal(manual.manual, true)
assert.equal(manual.label, '人工写的')
assert.equal(auto.label, '自动描述')
assert.notEqual(manual.label, auto.label, '两种描述必须在界面上分得开')
assert.notEqual(manual.tone, auto.tone, '两种描述的颜色也要分得开')
assert.match(manual.hint, /交回自动描述/, '人工写的那条要指出去哪退回')
assert.match(auto.hint, /改/, '自动描述要说明改了之后就归你')
// 后端只回 SQLite 的 1/0 时不能被判成"自动"（`is True` 的老病）。
assert.equal(descriptionOwner({ manual: 1 }).manual, true)
assert.equal(isManual(1), true)
assert.equal(isManual(true), true)
assert.equal(isManual(0), false)
assert.equal(isManual(undefined), false)
assert.equal(isManual('true'), false, '字符串 "true" 不是布尔真，别猜')

/* -------------------------------------------------------- 写操作请求体 */

// 没改动：不发请求（否则后端会回"没有要修改的字段"）。
assert.equal(updatePayload(item(), { description: '一只挥手的猫', name: 'collected-1a2b3c4d' }), null)
assert.equal(
  updatePayload(item(), { description: ' 一只挥手的猫 ', name: ' collected-1a2b3c4d ' }),
  null,
  '首尾空白不算改动（后端会 strip 之后再写，别为空格发一次请求）',
)
assert.equal(updatePayload(item({ assetId: '' }), { description: 'x', name: '' }), null)

// 只带改了的字段；空串 = 清空（是有效改动）。
assert.deepEqual(
  updatePayload(item(), { description: '一只招手的猫', name: 'collected-1a2b3c4d' }),
  { assetId: 'sticker-1a2b3c4d', description: '一只招手的猫' },
  '只改描述时不要顺手把名字也写一遍',
)
assert.deepEqual(updatePayload(item(), { description: '', name: '' }), {
  assetId: 'sticker-1a2b3c4d', description: '', name: '',
}, '清空描述与名字是有效改动，必须发出去')
assert.deepEqual(updatePayload(item(), { description: '一只挥手的猫', name: '小猫咪' }), {
  assetId: 'sticker-1a2b3c4d', name: '小猫咪',
})
// 请求体里不许多出任何键（后端多一个键就 400）。
assert.deepEqual(
  Object.keys(updatePayload(item(), { description: 'x', name: 'collected-1a2b3c4d' })!),
  ['assetId', 'description'],
)
assert.deepEqual(
  Object.keys(updatePayload(item(), { description: '', name: '' })!),
  ['assetId', 'description', 'name'],
  '描述与名字都清空时两个键都要发',
)
// 长度上限与后端一致。
assert.equal(saveBlocker({ description: 'x'.repeat(DESCRIPTION_LIMIT), name: '' }), '')
assert.match(saveBlocker({ description: 'x'.repeat(DESCRIPTION_LIMIT + 1), name: '' }), /描述太长/)
assert.match(saveBlocker({ description: '', name: 'x'.repeat(NAME_LIMIT + 1) }), /名字太长/)
assert.equal(saveBlocker({ description: '', name: 'x'.repeat(NAME_LIMIT) }), '')
assert.match(saveBlocker({ description: 'x'.repeat(DESCRIPTION_LIMIT + 1), name: '' }), /2000/)

// 停用 / 启用：布尔必须真的是布尔。
assert.deepEqual(disabledPayload(item(), true), { assetId: 'sticker-1a2b3c4d', disabled: true })
assert.deepEqual(disabledPayload(item(), 'yes' as unknown as boolean), {
  assetId: 'sticker-1a2b3c4d', disabled: false,
})

// 交回自动描述：只有"人工写的"才给得出去。
assert.deepEqual(restorePayload(item({ manual: true })), { assetId: 'sticker-1a2b3c4d' })
assert.equal(restorePayload(item({ manual: false })), null)
assert.equal(restorePayload(item({ assetId: '' })), null)
// 后端只回 SQLite 的 1 时也要认（否则按钮点了没反应）。
assert.deepEqual(restorePayload({ ...item(), manual: 1 as unknown as boolean }), { assetId: 'sticker-1a2b3c4d' })

/* ---------------------------------------------------- 删除的两段语义 */

assert.equal(MARK_DELETE_LABEL, '移除（可恢复）')
assert.equal(PURGE_DELETE_LABEL, '彻底删除（不可恢复）')
assert.deepEqual(deletePayload(item(), false), { assetId: 'sticker-1a2b3c4d', purge: false })
assert.deepEqual(deletePayload(item(), true), { assetId: 'sticker-1a2b3c4d', purge: true })
// 默认那一段（`purge` 不给）绝不能变成真删。
assert.equal(deletePayload(item(), undefined as unknown as boolean).purge, false)

const markWarning = deleteWarning(item(), false)
const purgeWarning = deleteWarning(item(), true)
assert.match(markWarning, /可恢复/, '默认那一步要能看懂"还能恢复"')
assert.match(markWarning, /重新扫描/)
assert.doesNotMatch(markWarning, /不可恢复/)
assert.match(purgeWarning, /不可恢复/, '真删文件必须写明不可恢复')
assert.match(purgeWarning, /真删掉/)
assert.notEqual(markWarning, purgeWarning, '两段删除不能是同一句文案')
// 没有描述/名字时也要说得出是哪一条。
assert.match(deleteWarning(item({ description: '', name: '', assetId: 'sticker-9' }), true), /sticker-9/)
assert.equal(stickerTitle(item({ description: '', name: '小猫咪' })), '小猫咪')

assert.match(deleteDoneNote({ assetId: 's', purged: false, deletedFile: false, file: '', changed: ['deleted'] }), /可恢复/)
assert.match(deleteDoneNote({ assetId: 's', purged: true, deletedFile: true, file: '', changed: ['deleted'] }), /不可恢复/)
assert.doesNotMatch(
  deleteDoneNote({ assetId: 's', purged: false, deletedFile: false, file: '', changed: [] }),
  /不可恢复/,
)

// 重扫结果 / 保存提示。
assert.equal(rescanNote({ scanned: true, assets: 42, added: 3 }), '扫描完成：库里现在 42 条，新增 3 条')
assert.equal(rescanNote({ scanned: true, assets: 42, added: 0 }), '扫描完成：库里现在 42 条，没有发现新素材')
assert.match(updateNote(['description']), /描述/)
assert.match(updateNote(['description', 'disabled']), /描述、停用状态/)
assert.equal(updateNote([]), '已保存')

/* ------------------------------------------------------------ 就地更新 */

const rows = [item({ assetId: 'a' }), item({ assetId: 'b' }), item({ assetId: 'c' })]
const merged = mergeStickerItems(rows, { b: item({ assetId: 'b', description: '改过的', status: 'disabled' }) })
assert.equal(merged.length, 3)
assert.equal(merged[1].description, '改过的', '就地更新要落在原来那一行')
assert.equal(merged[1].status, 'disabled')
assert.equal(merged[0].assetId, 'a')
assert.equal(merged[2].assetId, 'c', '顺序不能变')
assert.equal(rows[1].description, '一只挥手的猫', '不能改动入参（渲染用的还是原来那份）')
// 不在这一页里的 assetId：忽略，不要凭空插一行。
assert.deepEqual(mergeStickerItems(rows, { zzz: item({ assetId: 'zzz' }) }).map((row) => row.assetId), ['a', 'b', 'c'])
assert.deepEqual(mergeStickerItems([], { a: item() }), [])
assert.equal(mergeStickerItems(rows, {})[0].description, '一只挥手的猫')

assert.equal(staleOverridesNote(0), '')
assert.match(staleOverridesNote(2), /刷新/)
assert.match(staleOverridesNote(2), /2 条/)

/* ------------------------------------------------------------ 空态 */

const off = emptyHint({ enabled: false, autoCollect: true, hasFilters: false, total: 0 })
const filtered = emptyHint({ enabled: true, autoCollect: true, hasFilters: true, total: 0 })
const nothing = emptyHint({ enabled: true, autoCollect: true, hasFilters: false, total: 0 })
const noAuto = emptyHint({ enabled: true, autoCollect: false, hasFilters: false, total: 0 })
assert.match(off, /配置/)
assert.match(filtered, /筛选/)
assert.match(nothing, /重新扫描/)
assert.equal(new Set([off, filtered, nothing, noAuto]).size, 4, '四种空态不能是同一句话')
// 库里明明有（被筛掉了），不能说是空的。
assert.match(emptyHint({ enabled: true, autoCollect: true, hasFilters: true, total: 9 }), /筛选/)

/* ------------------------------------------------------------ 缩略图 */

assert.equal(thumbnailEndpoint(item()), 'console/sticker-file?assetId=sticker-1a2b3c4d')
assert.equal(
  thumbnailEndpoint(item({ thumbnailUrl: 'console/sticker-file?assetId=别的' })),
  'console/sticker-file?assetId=别的',
  '后端给了 thumbnailUrl 就用它',
)
assert.equal(
  thumbnailEndpoint(item({ thumbnailUrl: '' })),
  'console/sticker-file?assetId=sticker-1a2b3c4d',
  '缺 thumbnailUrl 时按同一份契约自己拼，别让图片整片变占位框',
)
assert.equal(
  thumbnailEndpoint(item({ thumbnailUrl: '', assetId: 'a b/中文.png' })),
  'console/sticker-file?assetId=a%20b%2F%E4%B8%AD%E6%96%87.png',
)
assert.equal(thumbnailEndpoint({ thumbnailUrl: '', assetId: '' }), '')
assert.equal(fileLabel(item({ file: 'collected/1a2b.png' })), '1a2b.png')
assert.equal(fileLabel({ file: 'a\\b\\c.gif' }), 'c.gif')
assert.equal(fileLabel({ file: '' }), '没有文件')

/* ------------------------------------------- 相对端点 → 真实地址（bridge.endpointUrl） */

// 图片字节走不了 apiGet（那条通道只回 JSON），后端给的 thumbnailUrl 是相对地址，
// 必须靠宿主上下文里的 pluginName 拼成扩展路由；拿不到就回空串（宁可不显示，也不乱拼）。
;(globalThis as { window?: unknown }).window = {
  AstrBotPluginPage: { getContext: () => ({ pluginName: 'astrbot_plugin_hds_interlude' }) },
}
const { endpointUrl } = await import('../src/bridge.ts')
assert.equal(
  endpointUrl(thumbnailEndpoint(item())),
  '/api/v1/plugins/extensions/astrbot_plugin_hds_interlude/console/sticker-file?assetId=sticker-1a2b3c4d',
)
assert.equal(endpointUrl('data:image/png;base64,AA'), 'data:image/png;base64,AA', '内联数据原样用')
assert.equal(endpointUrl('https://example.com/x.png'), 'https://example.com/x.png', '绝对地址原样用')
assert.equal(endpointUrl('/assets/x.png'), '/assets/x.png', '站内绝对路径原样用')
assert.equal(endpointUrl('  '), '')
;(globalThis as { window?: unknown }).window = {}
assert.equal(endpointUrl(thumbnailEndpoint(item())), '', '拿不到插件名时回空串')

console.log('stickers-view ok')
