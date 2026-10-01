/**
 * 「表情库」的**分组与上传**纯逻辑：分组视图拼装、目标分组合法性、组名校验、
 * 写请求体、上传查询串，以及每条路走完之后那句话。
 *
 * 零依赖（不 import preact、不碰宿主），所以能直接用 Node 的 TS 剥离跑断言：
 *     cd plugin/frontend && pnpm test:unit      # scripts/check-sticker-groups.ts
 *
 * v1.8.4 的口径只有一条：**`groupId` 的字面量就是磁盘上的目录名**。由此：
 *
 * 1. **没有"注册"这回事**：磁盘上有这个目录、或描述表里有这一行、或库里有素材挂着它，
 *    它就是一个正式分组。所以这一层**没有**"采纳"这类动作与"未注册"徽章
 *    （`registered` 只是后端留的兼容字段，恒 true，前端一个字符都不读）。
 * 2. **组名 = 目录名**：规则按文件系统来——**字节**上限 100（≈33 个汉字）、
 *    禁 `/ \ : * ? " < > |`、首字符不许 `.`、`default` 是保留名（根目录素材的桶）。
 *    这些都是**写入侧**的规则（新建 / 改名 / 挪进去）；界面上"看得见的组"照常显示。
 * 3. **默认分组不在前端写死**：`console/sticker-groups` 会一起回 `defaultGroupId`，照用。
 * 4. **改名不是改一个显示名**：后端要重命名目录 + 批量改该组素材的行，所以改完
 *    必须重拉列表与分组（详见 `panels/Stickers.tsx` 里的 `saveGroup`）。
 *
 * 用户可见文案一律短（状态词、动作词），要解释"为什么"就写在这里的注释里。
 */
import type { StickerGroup, StickerUploadResult } from './types'

/** 与 `core/service/helpers.STICKER_GROUP_NAME_MAX_BYTES` 逐字对齐：**字节**，不是字符。 */
export const GROUP_NAME_MAX_BYTES = 100
/** 与 `helpers.STICKER_GROUP_NAME_FORBIDDEN` 对齐（各平台建不出目录的字符）。 */
export const GROUP_NAME_FORBIDDEN = '/\\:*?"<>|'
/** 与 `helpers.STICKER_GROUP_RESERVED_NAMES` 对齐：根目录素材的桶，不许当新组名。 */
export const GROUP_NAME_RESERVED = ['default']
/** 与 `helpers.STICKER_GROUP_DESCRIPTION_MAX` 对齐。 */
export const GROUP_DESCRIPTION_LIMIT = 500

/** 空 `group` 的旧行在后端是「未分组」桶：`groupId` 是空串，且**筛不出来**。 */
export const UNGROUPED_ID = ''
export const UNGROUPED_NAME = '未分组'

/** 组名输入框旁边的短提示（说清"字节"这件事，不写句子）。 */
export const GROUP_NAME_HINT = '100 字节（中文约 33 字）'

/* ---------------------------------------------------------- 动作词（按钮） */

export const GROUP_NEW_LABEL = '新建'
export const GROUP_SAVE_LABEL = '保存'
export const GROUP_CANCEL_LABEL = '取消'
/** 组名 = 目录名，改名会重命名目录：非内置组才给这个动作。 */
export const GROUP_RENAME_LABEL = '改名'
/** 内置组只给这一个：它的目录名动不得，描述照常能写。 */
export const GROUP_DESCRIBE_LABEL = '改描述'
export const GROUP_DELETE_LABEL = '删除分组'
/** 第二下：写清删的是**分组**，不是组里的素材。 */
export const GROUP_DELETE_CONFIRM_LABEL = '确认删除分组'
export const GROUP_MOVE_LABEL = '移动'
export const GROUP_FILTER_LABEL = '筛选'
export const GROUP_MOVE_PLACEHOLDER = '移到分组…'
/** 删除分组时就地选择的目标：说的是"素材挪到哪"，不是"删什么"。 */
export const GROUP_MOVE_TO_LABEL = '素材挪到'
export const FILTER_ALL_LABEL = '全部分组'
export const UPLOAD_LABEL = '上传'
export const UPLOADING_LABEL = '上传中…'
export const UPLOAD_PICK_FIRST = '先选一张图'
export const UPLOAD_LIBRARY_OFF = '库未启用'
/** 重复上传：**不是报错**——库里已经有这一张，什么都没变。 */
export const UPLOAD_DUPLICATE_NOTE = '库里已有这张'
export const UPLOAD_DONE_NOTE = '已上传'

/* ------------------------------------------------------------------ 拼装 */

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function count(value: unknown): number {
  const number = Number(value)
  if (!Number.isFinite(number) || number <= 0) return 0
  return Math.floor(number)
}

/**
 * 分组视图归一化：**保持后端给的顺序**（内置组 → 有描述的 → 其余目录 / 有素材的 → 空桶），
 * 同 id 只留第一条，缺字段一律兜底成"能显示"的样子。
 *
 * `registered` 一个字符都不读：新模型里"磁盘上有目录 / 表里有行 / 有素材挂着"都算正式分组，
 * 那个字段只是后端留给旧前端的兼容位。看了它，界面就会凭空多出"未注册"这种第二等公民。
 */
export function normalizeGroups(payload: unknown): StickerGroup[] {
  const items = (payload as { items?: unknown } | null)?.items
  const rows = Array.isArray(items) ? items : []
  const seen = new Set<string>()
  const groups: StickerGroup[] = []
  for (const raw of rows) {
    const row = (raw ?? {}) as Record<string, unknown>
    const groupId = clean(row.groupId ?? row.group_id)
    if (seen.has(groupId)) continue
    seen.add(groupId)
    groups.push({
      groupId,
      name: clean(row.name) || groupId || UNGROUPED_NAME,
      description: clean(row.description),
      count: count(row.count),
      // 内置组是**唯一**一个"显示名 ≠ 目录名"、且目录名不许改的特例。
      builtin: row.builtin === true || row.builtin === 1,
      createdAt: clean(row.createdAt),
      updatedAt: clean(row.updatedAt),
    })
  }
  return groups
}

/**
 * 默认分组 id：**直接用后端回的 `defaultGroupId`**（删除分组时素材挪去哪、
 * 上传不给分组时落哪，都是它）。
 *
 * 只在这一处兜底：字段缺失 / 是空串时才退回列表里第一个非空分组。绝不写字面量。
 */
export function defaultGroupId(payload: unknown): string {
  const given = clean((payload as { defaultGroupId?: unknown } | null)?.defaultGroupId)
  if (given) return given
  const groups = normalizeGroups(payload)
  const fallback = groups.find((group) => group.builtin) || groups.find((group) => group.groupId)
  return fallback?.groupId ?? ''
}

/** 分组显示名：名字 → 目录名 → 「未分组」。 */
export function groupLabel(group: { groupId?: unknown; name?: unknown } | null | undefined): string {
  return clean(group?.name) || clean(group?.groupId) || UNGROUPED_NAME
}

/** 一条素材属于哪一组（列表里显示归属用）。 */
export function itemGroupLabel(item: { groupName?: unknown; groupId?: unknown } | null | undefined): string {
  return clean(item?.groupName) || clean(item?.groupId) || UNGROUPED_NAME
}

export function groupCountText(value: unknown): string {
  return `${count(value)} 条`
}

/** 没有目录名 = 后端的"未分组"桶：它既筛不出来，也不能当目标。 */
export function hasGroupId(groupId: unknown): boolean {
  return clean(groupId) !== ''
}

/** 能在素材列表里按这个分组筛吗：空串桶筛不出来（后端把空串当"不过滤"）。 */
export function canFilterByGroup(groupId: unknown): boolean {
  return hasGroupId(groupId)
}

/** 分组筛选下拉：只放筛得出来的分组（**不含空桶**）。 */
export function filterGroupOptions(groups: StickerGroup[]): Array<{ value: string; label: string }> {
  const options = [{ value: UNGROUPED_ID, label: FILTER_ALL_LABEL }]
  for (const group of groups) {
    if (!canFilterByGroup(group.groupId)) continue
    options.push({ value: group.groupId, label: groupLabel(group) })
  }
  return options
}

/**
 * 能当**目标**的分组（移动 / 上传都读这一份）：列表里每一个有目录名的组都算。
 *
 * "目录即分组"没有第二等公民——除了后端的空串桶（它不是一个目录）。
 */
export function targetGroupOptions(
  groups: StickerGroup[],
  options: { exclude?: string } = {},
): Array<{ value: string; label: string }> {
  const skip = clean(options.exclude)
  const out: Array<{ value: string; label: string }> = []
  for (const group of groups) {
    if (!hasGroupId(group.groupId)) continue
    if (group.groupId === skip) continue
    out.push({ value: group.groupId, label: groupLabel(group) })
  }
  return out
}

/** 删除分组时的「素材挪到」：候选与移动目标同一份判据，去掉正在删的这一组。 */
export function deleteMoveOptions(groups: StickerGroup[], groupId: string): Array<{ value: string; label: string }> {
  return targetGroupOptions(groups, { exclude: groupId })
}

/**
 * 目标下拉的默认值：后端给的默认分组**可用**就用它，否则退回第一个可用目标。
 *
 * 为什么要兜这一层：默认分组万一不在列表里（后端换了 id），直接把那个字符串塞进下拉，
 * 第一次上传 / 删除就会撞 400——而界面看起来一切正常。
 */
export function defaultTarget(groups: StickerGroup[], preferred: string): string {
  const options = targetGroupOptions(groups)
  const wanted = clean(preferred)
  if (wanted && options.some((option) => option.value === wanted)) return wanted
  return options.length ? options[0].value : ''
}

/** 内置组的目录名不许动（后端也 400）：只给它留"改描述"。 */
export function canRename(group: { builtin?: unknown } | null | undefined): boolean {
  return group?.builtin !== true
}

/* ------------------------------------------------------ 组名（= 目录名） */

/** 组名的**字节**长度：文件系统按字节算，`猫` 是 3 字节。 */
export function groupNameBytes(value: unknown): number {
  return new TextEncoder().encode(clean(value)).length
}

/**
 * 组名（= 目录名）不合法时回**中文短句**，合法回空串。与后端 `sticker_group_name_problem`
 * 同一条规则（那边是唯一权威，这里只是"别让用户白点一次"）。
 *
 * `reserved` 是**写入侧**的加严（新建 / 改名 / 挪进去）：`default` 是根目录素材的桶，
 * 允许建同名目录的话，那个计数会把两件不同的事混在一起。给既有的 `default/` 目录
 * 写描述时**不传**它（历史目录要能管理）。
 */
export function groupNameProblem(value: unknown, options: { reserved?: boolean } = {}): string {
  const text = clean(value)
  if (!text) return '分组名必填'
  if (options.reserved === true && GROUP_NAME_RESERVED.includes(text)) return `「${text}」是保留名`
  if (text.startsWith('.')) return '组名不能以「.」开头'
  if (groupNameBytes(text) > GROUP_NAME_MAX_BYTES) return `组名最多 ${GROUP_NAME_MAX_BYTES} 字节（中文约 33 字）`
  for (const character of text) {
    if (GROUP_NAME_FORBIDDEN.includes(character)) return `组名不能有 ${character}`
    const code = character.codePointAt(0) ?? 0
    if (code < 32 || code === 127) return '组名不能有控制字符'
    if (character.trim() === '' && character !== ' ') return '组名不能有换行'
  }
  return ''
}

/* ------------------------------------------------------ 写操作（请求体） */

export interface GroupDraft {
  name: string
  description: string
}

/**
 * 保存的模式：
 * * `full` = 组名会变（新建 / 改名）→ 组名与描述都要过校验；
 * * `description` = 只写描述（内置组、或组名没动）→ 只看描述。
 */
export type GroupSaveMode = 'full' | 'description'

/** 保存前的校验：返回不能保存的原因，`''` = 可以保存。 */
export function groupSaveBlocker(draft: GroupDraft, mode: GroupSaveMode = 'full'): string {
  if (mode === 'full') {
    const problem = groupNameProblem(draft?.name, { reserved: true })
    if (problem) return problem
  }
  if (clean(draft?.description).length > GROUP_DESCRIPTION_LIMIT) {
    return `分组描述太长了：上限 ${GROUP_DESCRIPTION_LIMIT} 个`
  }
  return ''
}

/**
 * `sticker-group-save` 的请求体：**只认 `groupId` / `name` / `description` 三个键**。
 *
 * * 新建（没有 `groupId`）时**不带 `groupId` 这个键**——缺省即新建，且 `name` **就是**
 *   新目录名（后端建目录）；
 * * `description` **永远要发**（哪怕没改）：后端缺省值是"清空"，只发 name 会抹掉描述；
 * * `name` 与 `groupId` 相同 = 只写描述；不同 = 改名（后端会重命名目录）。
 */
export function groupSavePayload(
  draft: GroupDraft,
  groupId = '',
): { groupId: string; name: string; description: string } | { name: string; description: string } {
  const name = clean(draft?.name)
  const description = clean(draft?.description)
  const wanted = clean(groupId)
  if (!wanted) return { name, description }
  return { groupId: wanted, name, description }
}

/** 删除分组：`{groupId, moveTo}`；不给 `moveTo` 时后端才用它自己的默认组。 */
export function groupDeletePayload(groupId: string, moveTo: string): { groupId: string; moveTo?: string } {
  const wanted = clean(groupId)
  const target = clean(moveTo)
  return target ? { groupId: wanted, moveTo: target } : { groupId: wanted }
}

/** 内置组没有删除这条路（后端 400）；其余分组都能删。 */
export function groupDeleteBlocker(group: { builtin?: unknown } | null | undefined): string {
  if (group?.builtin === true) return '内置分组没有删除'
  return ''
}

/** 批量移动：`{assetIds, groupId}`（去重、丢空值；目标为空不发请求）。 */
export function movePayload(
  assetIds: unknown,
  groupId: unknown,
): { assetIds: string[]; groupId: string } | null {
  const target = clean(groupId)
  if (!target) return null
  const list = Array.isArray(assetIds) ? assetIds : []
  const unique: string[] = []
  for (const value of list) {
    const id = clean(value)
    if (id && !unique.includes(id)) unique.push(id)
  }
  if (!unique.length) return null
  return { assetIds: unique, groupId: target }
}

/**
 * 上传的端点：**参数只能挂查询串**。
 *
 * 宿主 bridge 的 `upload(endpoint, file)` 只发一个 `file` 字段（字段名写死在宿主里），
 * 所以 `groupId` / `description` / `name` 除了查询串没有第二条路。
 * 只带**真的给了**的参数（顺序固定，便于断言与手敲 curl 对照）。
 */
export function uploadEndpoint(options: { groupId?: unknown; description?: unknown; name?: unknown } = {}): string {
  const pairs: string[] = []
  const groupId = clean(options.groupId)
  const description = clean(options.description)
  const name = clean(options.name)
  if (groupId) pairs.push(`groupId=${encodeURIComponent(groupId)}`)
  if (description) pairs.push(`description=${encodeURIComponent(description)}`)
  if (name) pairs.push(`name=${encodeURIComponent(name)}`)
  const base = 'console/sticker-upload'
  return pairs.length ? `${base}?${pairs.join('&')}` : base
}

/** 上传前的校验：`''` = 可以传。 */
export function uploadBlocker(input: { enabled: boolean; hasFile: boolean }): string {
  if (!input?.enabled) return UPLOAD_LIBRARY_OFF
  if (!input?.hasFile) return UPLOAD_PICK_FIRST
  return ''
}

/** 上传结果那句话：重复 = 库里已有这张（**不是报错**），新入库 = 已上传。 */
export function uploadOutcome(result: StickerUploadResult | null | undefined): {
  text: string
  tone: 'neutral' | 'ok'
  duplicated: boolean
} {
  if (result?.duplicated === true) return { text: UPLOAD_DUPLICATE_NOTE, tone: 'neutral', duplicated: true }
  return { text: UPLOAD_DONE_NOTE, tone: 'ok', duplicated: false }
}

/* -------------------------------------------------------------- 结果提示 */

/** 分组保存提示：新建 / 改名 / 只写描述分开说（都是短句）。 */
export function groupSaveNote(mode: 'create' | 'rename' | 'description'): string {
  if (mode === 'create') return '已新建分组'
  if (mode === 'rename') return '已改名分组'
  return '已保存描述'
}

/** 删组提示：说清删的是组、素材挪走了多少条。 */
export function groupDeleteNote(moved: unknown): string {
  return `已删除分组 · 挪走 ${count(moved)} 条`
}

/** 批量移动提示。 */
export function moveNote(moved: unknown): string {
  return `已移动 ${count(moved)} 条`
}

/** 分组区标题：数字摆在标题上，别让它随列表折叠藏起来。 */
export function groupsTitle(total: unknown): string {
  return `分组（${count(total)}）`
}
