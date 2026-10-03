/**
 * 「表情库」面板纯逻辑的断言（`pnpm test:unit`）。
 *
 * 这里钉的都是"界面上看不出来的错"：把"你手写的描述"显示成"自动描述"、
 * 把"只标记（可恢复）"和"真删文件（不可恢复）"说成同一件事、
 * 长列表只看得到 60 条却不说还有多少、就地更新之后合计数字悄悄变成错的。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  DEFAULT_PAGE_SIZE, DELETE_CONFIRM_LABEL, DELETE_LABEL, DESCRIPTION_LIMIT, DISABLE_LABEL,
  EMPTY_FILTERS, ENABLE_LABEL, NAME_LIMIT, PAGE_SIZE_MAX, activeFilterCount, brokenTitle,
  deleteDoneNote,
  deletePayload, descriptionOwner, disabledPayload, emptyHint, fileLabel, isManual, kindLabel,
  mergeStickerItems, pageOffset, pageSize, pageWindow, rescanNote, restorePayload,
  resultSummary, saveBlocker, sourceLabel, staleOverridesNote, statusLabel,
  statusTone, stickerParams, thumbnailEndpoint, updateNote, updatePayload,
} from '../src/stickers-view.ts'

const here = dirname(fileURLToPath(import.meta.url))
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
assert.doesNotMatch(summary, /只统计了最近一批/)
const truncated = resultSummary({ total: 137, shown: 60, limit: 60, offset: 0, truncated: true })
assert.match(truncated, /共 137 条/)
assert.match(truncated, /只统计了最近一批/, 'truncated 必须让用户看见，别以为库里就这么多（一句话就够）')
assert.equal(
  resultSummary({ total: 0, shown: 0, limit: 60, offset: 0, truncated: false }),
  '共 0 条 · 本页 0 条',
)

/* ---------------------------------------------------------------- 徽章 */

assert.equal(statusLabel('active'), '在用')
assert.equal(statusLabel('pending'), '待自动描述')
assert.equal(statusLabel('missing'), '缺失', 'missing 用中性词（"移除"已经不是用户的动作了）')
assert.equal(statusLabel('disabled'), '已停用')
assert.equal(statusLabel('weird'), 'weird', '不认识的状态照原样显示，不要编一个')
assert.equal(statusLabel(''), '未知状态')
assert.equal(statusTone('active'), 'ok')
assert.equal(statusTone('pending'), 'warn')
assert.equal(statusTone('missing'), 'danger')
assert.equal(statusTone('disabled'), 'neutral')
assert.equal(statusTone('weird'), 'neutral')
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

// 行内两个动作：动作词本身就是文案（不长、不解释）
assert.equal(DELETE_LABEL, '删除')
assert.equal(DELETE_CONFIRM_LABEL, '确认删除')
assert.equal(DISABLE_LABEL, '停用')
assert.equal(ENABLE_LABEL, '启用')
assert.deepEqual(deletePayload(item(), true), { assetId: 'sticker-1a2b3c4d', purge: true })
assert.deepEqual(deletePayload(item(), false), { assetId: 'sticker-1a2b3c4d', purge: false })
// `purge` 不给时绝不能变成真删（后端那条只标记的路还要能单独调用）
assert.equal(deletePayload(item(), undefined as unknown as boolean).purge, false)
assert.equal(deleteDoneNote({ assetId: 's', purged: true, deletedFile: true, file: '', changed: ['deleted'] }), '已删除')
assert.equal(deleteDoneNote({ assetId: 's', purged: false, deletedFile: false, file: '', changed: [] }), '已删除（文件保留）')

// 重扫结果 / 保存提示。
assert.equal(rescanNote({ scanned: true, assets: 42, added: 3 }), '扫描完成：库里现在 42 条，新增 3 条')
assert.equal(rescanNote({ scanned: true, assets: 42, added: 0 }), '扫描完成：库里现在 42 条，没有发现新素材')
assert.equal(updateNote(['description']), '已保存（描述）')
assert.equal(updateNote(['description', 'disabled']), '已保存（描述、停用状态）')
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
assert.ok(staleOverridesNote(2).length < 24, '这是状态行，不是句子')

/* ------------------------------------------------------------ 空态 */

const off = emptyHint({ enabled: false, autoCollect: true, hasFilters: false, total: 0 })
const filtered = emptyHint({ enabled: true, autoCollect: true, hasFilters: true, total: 0 })
const nothing = emptyHint({ enabled: true, autoCollect: true, hasFilters: false, total: 0 })
const noAuto = emptyHint({ enabled: true, autoCollect: false, hasFilters: false, total: 0 })
assert.match(off, /配置/)
assert.match(filtered, /筛选/)
assert.match(nothing, /重新扫描/)
assert.equal(new Set([off, filtered, nothing, noAuto]).size, 4, '四种空态不能是同一句话')
assert.ok([off, filtered, nothing, noAuto].every((text) => text.length < 26), '空态也给短句')
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

/* --------------------------------------------- 取不到图那句提示（带上"找的是哪儿"） */

// 没有理由时就是那句状态词（老后端 / 直连失败都没有后端 message）。
assert.equal(brokenTitle(''), '取不到图')
assert.equal(brokenTitle(undefined), '取不到图')
assert.equal(brokenTitle('   '), '取不到图')
// 有理由就带上——后端 404 的 message 里写着库根与目标路径。
assert.equal(
  brokenTitle('表情包文件不存在：已找过 …/stickers/collected/e6f0f8cae70cbd897bad1f538ed92585.jpg'),
  '取不到图 · 表情包文件不存在：已找过 …/stickers/collected/e6f0f8cae70cbd897bad1f538ed92585.jpg',
)
// 多行 / 超长都压成一行且截断：工具提示不是小作文。
assert.equal(brokenTitle('第一行\n第二行'), '取不到图 · 第一行')
assert.ok(brokenTitle('x'.repeat(400)).endsWith('…'), '过长要截断')
assert.ok(brokenTitle('x'.repeat(400)).length <= 210, '截断后仍然很短')

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

/* --------------------------------------- 这一页要像工具，不像说明书（用户点名） */

// 用户原话："这整个都是没必要的描述" —— 整段解释、成对的"可恢复 / 不可恢复"、把内部状态讲给用户听，
// 一律不许回到这一页。这里直接扫源码，比断言某个渲染结果更不容易漏。
const panelSource = readFileSync(join(here, '..', 'src', 'panels', 'Stickers.tsx'), 'utf8')
const viewSource = readFileSync(join(here, '..', 'src', 'stickers-view.ts'), 'utf8')
const FORBIDDEN = [
  '这一页在改什么',   // 那块整段说明的面板
  '彻底删除',         // 动作就叫「删除」
  '不可恢复',         // 不解释为什么不可逆
  '可恢复',           // 更没有"两段删除"这回事了
  '移除（',           // 「移除（可恢复）」这个按钮不许回来
  '已移除',           // 内部状态改中性词「缺失」
  '不参与挑选',       // 状态徽章只写状态词
  '素材目录：',       // 目录路径不再摆给用户看
  '她下次挑素材时看到的就是这一份', // 保存提示不写小作文
  // v1.8.3（分组 / 上传）新钉的解释性长句：这些都是在讲"会发生什么"，
  // 而界面上的位置只够放状态词与动作词。
  '删组会',
  '会把组里的素材',
  '素材会跟着',
  '上传后会',
  '会自动进入',
  '会一并删除',
  '请注意',
  '说明：',
  // v1.8.4（目录即分组）：这两个词在界面上**不该存在**——
  // 磁盘上有目录 / 表里有行 / 有素材挂着都算正式分组，没有"第二等公民"。
  '采纳',
  '未注册',
]
for (const word of FORBIDDEN) {
  assert.ok(!panelSource.includes(word), `面板里不许再出现「${word}」`)
  assert.ok(!viewSource.includes(word), `文案模块里不许再出现「${word}」`)
}
// 也不许再有一个"说明"性质的面板（图标 info / 标题像说明书）。
assert.ok(!/<Panel[^>]*icon="info"/.test(panelSource), '面板里不许再挂说明性质的 Panel')
assert.ok(!/这一页|说明/.test(panelSource.match(/<Panel[\s\S]{0,80}?title=\{?[^}]*\}?/) ?? ''), 'Panel 标题不许写成说明书')
// 两个动作的词必须在（动作就是这两个词，不要长句）。
assert.ok(panelSource.includes('DELETE_LABEL') && panelSource.includes('DISABLE_LABEL') && panelSource.includes('ENABLE_LABEL'))

console.log('stickers-view ok（文案只留状态词与动作词，无说明书段落）')
