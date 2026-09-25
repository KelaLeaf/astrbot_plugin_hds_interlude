/**
 * 剧本切换器上一条剧本怎么显示——纯函数，单独一个零依赖模块。
 *
 * 为什么要拆出来：这一段踩过一次真实的误解。共享主剧本下同一个角色只有一部活动
 * 剧本，但库里可能还躺着旧 beta 的"每 QQ 一部"遗留剧本；面板默认只显示最新那一部，
 * 于是新用户一发消息，界面就跳到新剧本，看起来像"前面的剧本没了"。切换器要把
 * **角色 / 平台 / 条目数 / 状态** 摆出来，用户才分得清哪部是哪部。
 *
 * 纯函数就能覆盖的判定放这里，`frontend/scripts/check-story-label.ts` 用
 * `node --experimental-strip-types` 直接跑（见 `package.json` 的 `test:unit`）。
 */

export interface StoryLabelInput {
  id: string
  character?: string
  platform?: string
  status?: string
  entries?: number
  participants?: number
  shared?: boolean
}

/** 状态中文名（上游只有 active / paused / archived 三种）。 */
export function storyStatusText(status: string | undefined): string {
  if (status === 'archived') return '已归档'
  if (status === 'paused') return '已暂停'
  if (status === 'active') return '进行中'
  return status || '未知'
}

/** 切换器里的显示文案：`凌梦 · qq · 128 条 · 已归档`。 */
export function storyLabel(item: StoryLabelInput): string {
  const bits: string[] = []
  bits.push(item.character || item.id.slice(0, 12))
  if (item.platform) bits.push(item.platform)
  const entries = typeof item.entries === 'number' ? item.entries : 0
  bits.push(`${entries} 条`)
  if (typeof item.participants === 'number' && item.participants > 1) bits.push(`${item.participants} 人`)
  bits.push(storyStatusText(item.status))
  return bits.join(' · ')
}

/** 这部剧本是不是当前共享主剧本（`character:…`）。 */
export function isSharedStory(item: StoryLabelInput | null | undefined): boolean {
  if (!item) return false
  return item.shared === true || item.id.startsWith('character:')
}

/** 该不该给「并入主剧本」按钮：选了剧本、知道主剧本是谁、且不是主剧本本身。 */
export function canMergeStory(
  selected: StoryLabelInput | null | undefined,
  canonicalId: string,
): boolean {
  if (!selected || !canonicalId) return false
  return selected.id !== canonicalId
}
