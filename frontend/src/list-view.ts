/**
 * 长列表的折叠/滚动规则（纯逻辑，便于断言）。
 *
 * 控制台里长期事实、承诺、投递账本、聊天列表都可能长到几千条，直接铺开会把页面拉得
 * 很长。统一规则：
 * - `limit <= 0`：不折叠，原样全显（用于本来就短的固定表，例如数据库表清单）；
 * - 收起状态：只显示前 `limit` 条，剩下的用一行「还有 N 条」提示；
 * - 展开状态：全部显示，但放进固定高度的滚动框里，页面高度不再跟着条目数涨。
 */
export type ListWindow = {
  /** 收起时要渲染的条数；展开或 `limit <= 0` 时等于总数。 */
  shown: number
  /** 被折叠起来的条数，0 表示没有藏起来的内容。 */
  hidden: number
  /** 是否应该套滚动框（展开且确实超过一屏时）。 */
  scroll: boolean
}

function normalizeCount(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0
  return Math.floor(value)
}

export function listWindow(total: number, limit: number, expanded: boolean): ListWindow {
  const count = normalizeCount(total)
  const cap = normalizeCount(limit)
  if (cap === 0 || count <= cap) return { shown: count, hidden: 0, scroll: false }
  if (!expanded) return { shown: cap, hidden: count - cap, scroll: false }
  return { shown: count, hidden: 0, scroll: true }
}

/** 折叠时那行按钮的文案。 */
export function moreLabel(hidden: number, unit = '条'): string {
  return `还有 ${normalizeCount(hidden)} ${unit}`
}

/** 列表底部的计数说明，只在确实折叠过时给。`unit` 与折叠按钮保持一致。 */
export function countLabel(total: number, limit: number, unit = '条'): string {
  const count = normalizeCount(total)
  const cap = normalizeCount(limit)
  if (cap === 0 || count <= cap) return ''
  return `共 ${count} ${unit}`
}
