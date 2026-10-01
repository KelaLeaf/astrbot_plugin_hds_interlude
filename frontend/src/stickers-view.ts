/**
 * 「表情库」面板的纯逻辑：筛选串、徽章映射、分页窗口、就地更新与两段删除的文案。
 *
 * 零依赖（不 import preact、不碰宿主），所以能直接用 Node 的 TS 剥离跑断言：
 *     cd plugin/frontend && pnpm test:unit      # scripts/check-stickers-view.ts
 *
 * 为什么值得单独一个模块——这一页有三处"显示错了就是骗用户"的地方：
 *
 * 1. **人工描述 vs 自动描述**（`manual`）：模型看到的素材目录里就是这句话，
 *    把"你写的"说成"自动写的"（或反过来）会直接误导用户去改错东西；
 * 2. **两段删除**：默认只标记（可恢复）与 `purge=true`（真删文件、不可恢复）
 *    必须是两套文案，按钮上就要看得出区别——"删错了"没有回收站；
 * 3. **`total` / `truncated`**：后端一次最多扫 500 行，只显示 60 条时用户会以为
 *    库里就这么多；数字与"还有多少没算进来"必须由这里给准话。
 */
import type { StickerDeleteResult, StickerItem, StickerRescanResult } from './types'

export type Tone = 'neutral' | 'accent' | 'ok' | 'warn' | 'danger'

/** 描述 / 名字的长度上限，与 `console_api.STICKER_DESCRIPTION_MAX` / `STICKER_NAME_MAX` 对齐。 */
export const DESCRIPTION_LIMIT = 2_000
export const NAME_LIMIT = 60
/** 一页多少条：默认与后端一致；`PAGE_SIZE_MAX` 是后端 `limit` 的上限。 */
export const DEFAULT_PAGE_SIZE = 60
export const PAGE_SIZE_MAX = 200

/** 两段删除的按钮文案：区别必须写在按钮上，不能只藏在确认里。 */
export const MARK_DELETE_LABEL = '移除（可恢复）'
export const PURGE_DELETE_LABEL = '彻底删除（不可恢复）'

export interface StickerFilters {
  status: string
  kind: string
  source: string
  q: string
}

export const EMPTY_FILTERS: StickerFilters = { status: '', kind: '', source: '', q: '' }

export interface StickerDraft {
  description: string
  name: string
}

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function count(value: unknown): number {
  const number = Number(value)
  if (!Number.isFinite(number) || number <= 0) return 0
  return Math.floor(number)
}

/* ------------------------------------------------------------ 筛选与分页 */

/** 一页的条数：归一化到 `[1, PAGE_SIZE_MAX]`，缺省 60。 */
export function pageSize(limit: unknown): number {
  const size = count(limit)
  if (!size) return DEFAULT_PAGE_SIZE
  return Math.min(PAGE_SIZE_MAX, size)
}

/** 起始偏移：非数 / 负数一律 0。 */
export function pageOffset(offset: unknown): number {
  const number = Number(offset)
  if (!Number.isFinite(number) || number <= 0) return 0
  return Math.floor(number)
}

/**
 * 查询参数：只带**真的选了**的筛选项。
 *
 * 空串后端当成"不过滤"，但少发一个键就少一处歧义；`q` 只发去掉首尾空白之后
 * 还有内容的（用户在搜索框里敲了个空格不该变成一次"无结果"的查询）。
 */
export function stickerParams(
  filters: StickerFilters,
  limit: unknown,
  offset: unknown,
): Record<string, string | number> {
  const params: Record<string, string | number> = {
    limit: pageSize(limit),
    offset: pageOffset(offset),
  }
  const status = clean(filters?.status)
  const kind = clean(filters?.kind)
  const source = clean(filters?.source)
  const needle = clean(filters?.q)
  if (status) params.status = status
  if (kind) params.kind = kind
  if (source) params.source = source
  if (needle) params.q = needle
  return params
}

/** 现在生效的筛选项个数（不含分页）：用来决定「重置筛选」能不能按。 */
export function activeFilterCount(filters: StickerFilters): number {
  let total = 0
  if (clean(filters?.status)) total += 1
  if (clean(filters?.kind)) total += 1
  if (clean(filters?.source)) total += 1
  if (clean(filters?.q)) total += 1
  return total
}

export interface PageWindow {
  /** 实际生效的起始偏移（超出范围时收敛到最后一页）。 */
  start: number
  size: number
  /** 当前页码（从 1 数）。 */
  page: number
  pages: number
  /** 本页第一条 / 最后一条在结果里的序号（空结果时都是 0）。 */
  first: number
  last: number
  hasPrev: boolean
  hasNext: boolean
}

/**
 * 分页窗口：把 `total` / `limit` / `offset` 三个数算成页码与边界。
 *
 * 偏移超出范围（删了几条之后停在最后一页之外）会**收敛到最后一页**，
 * 而不是给一个永远空的页面——那种页面看起来就像"库空了"。
 */
export function pageWindow(total: unknown, limit: unknown, offset: unknown): PageWindow {
  const size = pageSize(limit)
  const found = count(total)
  const maxStart = found === 0 ? 0 : Math.floor((found - 1) / size) * size
  const start = Math.min(pageOffset(offset), maxStart)
  const pages = Math.max(1, Math.ceil(found / size))
  const page = Math.floor(start / size) + 1
  return {
    start,
    size,
    page: Math.min(page, pages),
    pages,
    first: found === 0 ? 0 : start + 1,
    last: Math.min(found, start + size),
    hasPrev: start > 0,
    hasNext: start + size < found,
  }
}

/** 列表上面那行合计：`total` 与 `truncated` 都要说出来。 */
export function resultSummary(input: {
  total: number
  shown: number
  limit: number
  offset: number
  truncated: boolean
}): string {
  const window = pageWindow(input.total, input.limit, input.offset)
  const head = [`共 ${count(input.total)} 条`]
  if (count(input.shown) > 0) {
    head.push(
      window.first === window.last
        ? `本页 1 条（第 ${window.first} 条）`
        : `本页 ${window.last - window.first + 1} 条（第 ${window.first}–${window.last} 条）`,
    )
  } else {
    head.push('本页 0 条')
  }
  const text = head.join(' · ')
  if (!input.truncated) return text
  return `${text}；素材很多：后端只统计了最近的一批，更早的没算进来（重扫不会改变这一点，多筛选几次看）`
}

/* ------------------------------------------------------------ 徽章映射 */

/** 状态徽章文案。不认识的取值照原样显示（绝不编一个状态）。 */
export function statusLabel(status: unknown): string {
  const value = clean(status).toLowerCase()
  if (value === 'active') return '在用'
  if (value === 'pending') return '待自动描述'
  if (value === 'missing') return '已移除'
  if (value === 'disabled') return '已停用'
  return value || '未知状态'
}

export function statusTone(status: unknown): Tone {
  const value = clean(status).toLowerCase()
  if (value === 'active') return 'ok'
  if (value === 'pending') return 'warn'
  if (value === 'missing') return 'danger'
  return 'neutral'
}

/** 状态徽章的悬停说明：四种状态各自"发生了什么、怎么办"。 */
export function statusHint(status: unknown): string {
  const value = clean(status).toLowerCase()
  if (value === 'active') return '在用：她挑素材时能选到这一条'
  if (value === 'pending') return '还没有描述：下一次「重新扫描」会用视觉模型描述它'
  if (value === 'missing') return '不在库里了：可能是你移除了它，也可能是文件本身不在了；文件还在磁盘上的话，重扫能找回来'
  if (value === 'disabled') return '已停用：文件和描述都留着，只是不参与她的挑选'
  return '后端给了个不认识的状态，照原样显示'
}

/** 素材形式：静止图 / 动图。 */
export function kindLabel(kind: unknown): string {
  const value = clean(kind).toLowerCase()
  if (value === 'image') return '静止图'
  if (value === 'animated') return '动图'
  return value || '未知形式'
}

/** 来源：她收到时自动收藏的 / 从表情库目录扫进来的。 */
export function sourceLabel(source: unknown): string {
  const value = clean(source).toLowerCase()
  if (value === 'auto') return '自动收藏'
  if (value === 'manual') return '扫盘入库'
  return value || '未知来源'
}

export function sourceHint(source: unknown): string {
  const value = clean(source).toLowerCase()
  if (value === 'auto') return '她收到别人发的表情包时自动收进来的'
  if (value === 'manual') return '从表情库目录里扫描进来的（你自己放进去的文件）'
  return '后端给了个不认识的来源'
}

export interface DescriptionOwner {
  /** 描述是谁写的。 */
  manual: boolean
  label: string
  tone: Tone
  hint: string
}

/**
 * 描述的作者：`manual=true` 就是"这句话是人工写的"。
 *
 * 后端 `update_sticker` 一写描述就把标记置上，所以界面上"你写的"与"模型写的"
 * 永远能分开；「交回自动描述」只在 manual 时才给。
 *
 * `1` 也算真：这条标记落在 SQLite 里是 `0/1`，中间任何一层忘了归一化，
 * 界面上就会出现"你明明手写了却显示自动描述"（坑 8 的老病）。
 */
export function isManual(value: unknown): boolean {
  return value === true || value === 1
}

export function descriptionOwner(item: { manual?: unknown }): DescriptionOwner {
  if (isManual(item?.manual)) {
    return {
      manual: true,
      label: '人工写的',
      tone: 'accent',
      hint: '这句是你手写的：自动描述不会覆盖它，「交回自动描述」会把它退回去',
    }
  }
  return {
    manual: false,
    label: '自动描述',
    tone: 'neutral',
    hint: '这句是视觉模型写的：你一改就变成「人工写的」，模型就不会再覆盖它',
  }
}

/* ------------------------------------------------------ 写操作（请求体） */

/** 保存前的校验：返回不能保存的原因，`''` = 可以保存。 */
export function saveBlocker(draft: StickerDraft): string {
  const description = clean(draft?.description)
  if (description.length > DESCRIPTION_LIMIT) {
    return `描述太长了：现在 ${description.length} 个字，上限 ${DESCRIPTION_LIMIT} 个`
  }
  const name = clean(draft?.name)
  if (name.length > NAME_LIMIT) {
    return `名字太长了：现在 ${name.length} 个字，上限 ${NAME_LIMIT} 个`
  }
  return ''
}

/**
 * `sticker-update` 的请求体：**只带真的改了的字段**。
 *
 * 后端只认 `assetId` / `description` / `name` / `disabled` 三个可改字段，多一个键 400；
 * 一个字段都没改时回 `null`（别发一个只有 `assetId` 的请求去换一句
 * "没有要修改的字段"）。空串是**有效值**（= 清空），所以"清空"要被当成改动。
 */
export function updatePayload(
  item: StickerItem,
  draft: StickerDraft,
): { assetId: string; description?: string; name?: string } | null {
  const assetId = clean(item?.assetId)
  if (!assetId) return null
  const payload: { assetId: string; description?: string; name?: string } = { assetId }
  const description = clean(draft?.description)
  if (description !== clean(item?.description)) payload.description = description
  const name = clean(draft?.name)
  if (name !== clean(item?.name)) payload.name = name
  if (payload.description === undefined && payload.name === undefined) return null
  return payload
}

/** 停用 / 启用：只发 `disabled`（布尔，后端要求严格 `true`/`false`）。 */
export function disabledPayload(item: StickerItem, disabled: boolean): { assetId: string; disabled: boolean } {
  return { assetId: clean(item?.assetId), disabled: disabled === true }
}

/** 交回自动描述：只有「人工写的」才需要（自动描述的没有东西可交回）。 */
export function restorePayload(item: StickerItem): { assetId: string } | null {
  const assetId = clean(item?.assetId)
  if (!assetId || !isManual(item?.manual)) return null
  return { assetId }
}

/** 删除：`purge` 必须显式给布尔，避免把"真删"当成默认。 */
export function deletePayload(item: StickerItem, purge: boolean): { assetId: string; purge: boolean } {
  return { assetId: clean(item?.assetId), purge: purge === true }
}

/** 保存成功后的提示：说清改了哪几样。 */
export function updateNote(changed: unknown): string {
  const names: Record<string, string> = { description: '描述', name: '名字', disabled: '停用状态' }
  const list = Array.isArray(changed) ? changed.map((key) => names[String(key)] || String(key)) : []
  if (!list.length) return '已保存'
  return `已保存（${list.join('、')}）：她下次挑素材时看到的就是这一份`
}

/** 这条素材在界面上的称呼（描述 → 名字 → assetId）。 */
export function stickerTitle(item: StickerItem): string {
  return clean(item?.description) || clean(item?.name) || clean(item?.assetId) || '这条素材'
}

/**
 * 两段删除的警告文案：默认那一段要说得出"还能恢复"，`purge` 那一段必须写明
 * **不可恢复**。这句话挂在界面上（不是浏览器原生确认框——插件页跑在
 * `sandbox` 里，没带 `allow-modals` 时宿主会静默忽略 confirm，按钮看起来点了没反应）。
 */
export function deleteWarning(item: StickerItem, purge: boolean): string {
  if (purge) {
    return `彻底删掉「${stickerTitle(item)}」？磁盘上的图片会被真删掉，不可恢复；`
      + '库里只留一条「这里曾经有」的记录，想反悔只能重新拿到那张图再发一次。'
  }
  return `把「${stickerTitle(item)}」标记为移除？文件和描述都还留着，只是不再进入她的素材目录；`
    + '之后点「重新扫描」就能把它找回来（可恢复）。'
}

/** 删除完成后的提示：`purged` 决定这是不是一句"还能恢复"。 */
export function deleteDoneNote(result: StickerDeleteResult): string {
  if (result?.purged) {
    return '已彻底删除：磁盘上的文件删掉了（不可恢复），库里留了一条记录'
  }
  return '已移除：文件和描述都还留着，重新扫描就能找回来（可恢复）'
}

/** 重扫结果：说清"跑完了、库里现在多少、新增多少"。 */
export function rescanNote(result: StickerRescanResult): string {
  const assets = count(result?.assets)
  const added = count(result?.added)
  const tail = added > 0 ? `新增 ${added} 条` : '没有发现新素材'
  return `扫描完成：库里现在 ${assets} 条，${tail}`
}

/* ------------------------------------------------------------ 就地更新 */

/**
 * 用写操作响应里的 `item` **就地**替换列表里的那一行（控制台既有做法：不整页重拉）。
 *
 * 找不到同 `assetId` 的行时忽略（换了筛选条件之后，旧的响应不该凭空插一行进来）；
 * 顺序保持不变；不改动入参数组。
 */
export function mergeStickerItems(
  items: StickerItem[],
  overrides: Record<string, StickerItem>,
): StickerItem[] {
  const rows = Array.isArray(items) ? items : []
  const patched = overrides && typeof overrides === 'object' ? overrides : {}
  if (!Object.keys(patched).length) return rows.slice()
  return rows.map((row) => {
    const fresh = patched[row?.assetId]
    return fresh ? { ...row, ...fresh } : row
  })
}

/**
 * 就地更新之后的提醒：合计与各状态数量还是**刷新前**的数字。
 *
 * 不说这一句的话，用户会看到"在用 12"下面挂着一行"已停用"（刚改的），
 * 以为界面坏了；说清了就知道点一下「刷新」会重新统计。
 */
export function staleOverridesNote(pending: number): string {
  const total = count(pending)
  if (!total) return ''
  return `刚就地改了 ${total} 条：上面的合计与各状态数量还是刷新前的数字，点「刷新」重新统计`
}

/* ------------------------------------------------------------ 空态与缩略图 */

/** 空列表的人话：功能没开 / 筛掉了 / 库真的是空的，三种说法必须不同。 */
export function emptyHint(input: {
  enabled: boolean
  autoCollect: boolean
  hasFilters: boolean
  total: number
}): string {
  if (!input.enabled) {
    return '本地表情包库没启用：去「配置」里打开「本地表情包 → 启用本地表情包库」，'
      + '自动收藏与重新扫描才会动。'
  }
  if (input.hasFilters) {
    return '没有符合这些筛选条件的素材。换个条件，或点「重置筛选」看全部。'
  }
  if (count(input.total) === 0 && !input.autoCollect) {
    return '库里还没有素材，而且自动收藏是关着的：只有你放进表情库目录的文件会被扫进来。'
  }
  return '库里还没有素材：她收到别人发的表情包之后，点「重新扫描」把它们收进来。'
}

/**
 * 缩略图的**相对端点**（要再过 `endpointUrl()` 拼上插件名才能当 `src`）。
 *
 * 优先用后端给的 `thumbnailUrl`；它缺了（旧后端 / 桩数据）就按同一份契约自己拼，
 * 这样图片不至于因为一个字段缺失就整片变成占位框。
 */
export function thumbnailEndpoint(item: { thumbnailUrl?: unknown; assetId?: unknown }): string {
  const given = clean(item?.thumbnailUrl)
  if (given) return given
  const assetId = clean(item?.assetId)
  if (!assetId) return ''
  return `console/sticker-file?assetId=${encodeURIComponent(assetId)}`
}

/** 缩略图加载失败 / 没有文件时显示的文件名（只取 basename，不显示整条路径）。 */
export function fileLabel(item: { file?: unknown }): string {
  const name = clean(item?.file).replace(/\\/g, '/')
  if (!name) return '没有文件'
  const parts = name.split('/')
  return parts[parts.length - 1] || '没有文件'
}
