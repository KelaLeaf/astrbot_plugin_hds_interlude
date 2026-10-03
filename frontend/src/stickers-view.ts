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
 * 2. **删除**：行内「删除」= 真删磁盘文件（不可逆），必须走就地二次确认——
 *    按钮第一下变危险态、第二下才发请求；
 * 3. **`total` / `truncated`**：后端一次最多扫 500 行，只显示 60 条时用户会以为
 *    库里就这么多；数字与"还有多少没算进来"必须由这里给准话。
 *
 * 另外：这一页的用户可见文案一律短（状态词、动作词），**不写说明书**——
 * 谁想解释一句"为什么"，请写进本文件的注释或 `docs/PORTING_NOTES.md`。
 */
import type { StickerDeleteResult, StickerItem, StickerRescanResult } from './types'

export type Tone = 'neutral' | 'accent' | 'ok' | 'warn' | 'danger'

/** 描述 / 名字的长度上限，与 `console_api.STICKER_DESCRIPTION_MAX` / `STICKER_NAME_MAX` 对齐。 */
export const DESCRIPTION_LIMIT = 2_000
export const NAME_LIMIT = 60
/** 一页多少条：默认与后端一致；`PAGE_SIZE_MAX` 是后端 `limit` 的上限。 */
export const DEFAULT_PAGE_SIZE = 60
export const PAGE_SIZE_MAX = 200

/** 行内两个动作的文案：说动作本身，不解释（解释是文档的事）。 */
export const DELETE_LABEL = '删除'
export const DELETE_CONFIRM_LABEL = '确认删除'
export const DISABLE_LABEL = '停用'
export const ENABLE_LABEL = '启用'

export interface StickerFilters {
  status: string
  kind: string
  source: string
  q: string
  /** 分组 id（精确匹配）。空串 = 不过滤——**"未分组"桶筛不出来**，别拿空串当它。 */
  group: string
}

export const EMPTY_FILTERS: StickerFilters = { status: '', kind: '', source: '', q: '', group: '' }

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
  const group = clean(filters?.group)
  if (status) params.status = status
  if (kind) params.kind = kind
  if (source) params.source = source
  if (needle) params.q = needle
  // 分组：**空串一定是"不过滤"**（后端的口径），所以只有非空才带上这个键。
  if (group) params.group = group
  return params
}

/** 现在生效的筛选项个数（不含分页）：用来决定「重置筛选」能不能按。 */
export function activeFilterCount(filters: StickerFilters): number {
  let total = 0
  if (clean(filters?.status)) total += 1
  if (clean(filters?.kind)) total += 1
  if (clean(filters?.source)) total += 1
  if (clean(filters?.q)) total += 1
  if (clean(filters?.group)) total += 1
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
  return `${text} · 只统计了最近一批`
}

/* ------------------------------------------------------------ 徽章映射 */

/** 状态徽章文案。不认识的取值照原样显示（绝不编一个状态）。 */
export function statusLabel(status: unknown): string {
  const value = clean(status).toLowerCase()
  if (value === 'active') return '在用'
  if (value === 'pending') return '待自动描述'
  if (value === 'missing') return '缺失'
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

export interface DescriptionOwner {
  /** 描述是谁写的。 */
  manual: boolean
  label: string
  tone: Tone
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
    return { manual: true, label: '人工写的', tone: 'accent' }
  }
  return { manual: false, label: '自动描述', tone: 'neutral' }
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
  return `已保存（${list.join('、')}）`
}

/** 删除完成后的提示（行内只有"真删文件"一种删除，短说即可）。 */
export function deleteDoneNote(result: StickerDeleteResult): string {
  return result?.purged ? '已删除' : '已删除（文件保留）'
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
 * 写操作响应里的 `item[]` → 就地更新的映射（批量移动一次回好几条）。
 *
 * 没有 `assetId` 的行直接丢掉：拿不到键就没法回填，硬塞一行进去只会让列表里
 * 多出一条看不见来源的素材。
 */
export function overrideMap(items: StickerItem[]): Record<string, StickerItem> {
  const out: Record<string, StickerItem> = {}
  for (const item of Array.isArray(items) ? items : []) {
    const assetId = item?.assetId
    if (assetId) out[assetId] = item
  }
  return out
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
  return `合计待刷新（刚改了 ${total} 条）`
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
    return '本地表情包库未启用，去「配置」里打开它。'
  }
  if (input.hasFilters) {
    return '没有符合条件的素材，试试「重置筛选」。'
  }
  if (count(input.total) === 0 && !input.autoCollect) {
    return '库里还没有素材（自动收藏已关闭）。'
  }
  return '库里还没有素材，点「重新扫描」找找。'
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

/**
 * 占位框那句短提示：**取不到图的理由比"取不到图"三个字有用得多**。
 *
 * 后端 404 的 `message` 里带着"已找过哪儿"（库根 + 目标路径）。宿主 bridge 只把
 * `message` 透给面板（`data` 在 `plugin_page_bridge` 那一层丢掉了，实测），所以能拿到的
 * 就是它。这里只做两件事：拼上状态词、把过长的理由截断（工具提示不是小作文）。
 * 没有理由时回那句状态词——**不许**因为没理由就什么都不显示。
 */
export function brokenTitle(reason: unknown): string {
  const detail = clean(reason).split('\n')[0].trim()
  if (!detail) return '取不到图'
  const trimmed = detail.length > 200 ? `${detail.slice(0, 199)}…` : detail
  return `取不到图 · ${trimmed}`
}
