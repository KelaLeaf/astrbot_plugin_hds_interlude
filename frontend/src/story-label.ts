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
  /** 是不是当前共享主剧本。 */
  main?: boolean
}

/** 状态中文名（上游只有 active / paused / archived 三种）。 */
export function storyStatusText(status: string | undefined): string {
  if (status === 'archived') return '已归档'
  if (status === 'paused') return '已暂停'
  if (status === 'active') return '进行中'
  return status || '未知'
}

/** 角色定位：主剧本 / 旧剧本。用户分不清"哪部是主线"就是缺这两个字。 */
export function storyRoleText(item: StoryLabelInput): string {
  return item.main ? '主剧本' : '旧剧本'
}

/** 切换器里的显示文案：`凌梦 · qq · 主剧本 · 128 条 · 进行中`。 */
export function storyLabel(item: StoryLabelInput): string {
  const bits: string[] = []
  bits.push(item.character || item.id.slice(0, 12))
  if (item.platform) bits.push(item.platform)
  bits.push(storyRoleText(item))
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

/**
 * 该不该给「并入主剧本」按钮：主剧本已经定下来了，且选中的不是它。
 * 还没定主剧本时给的是「设为主剧本」（`canPromoteStory`），两个按钮不同时出现。
 */
export function canMergeStory(
  selected: StoryLabelInput | null | undefined,
  mainStoryId: string,
): boolean {
  if (!selected || !mainStoryId) return false
  return selected.id !== mainStoryId
}

/** 该不该给「设为主剧本」按钮：还没有主剧本，且选中了某一部（含被归档的旧剧本）。 */
export function canPromoteStory(
  selected: StoryLabelInput | null | undefined,
  mainStoryId: string,
): boolean {
  if (!selected) return false
  return !mainStoryId
}
