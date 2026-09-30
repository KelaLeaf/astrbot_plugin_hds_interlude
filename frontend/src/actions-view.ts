/**
 * 「动作」面板的纯逻辑：分组、参数摘要、档位/风险文案。
 *
 * 零依赖（不 import preact、不碰宿主），所以能直接用 Node 的 TS 剥离跑断言：
 *     cd plugin/frontend && pnpm test:unit      # scripts/check-actions-view.ts
 *
 * 为什么值得单独一个模块：档位与风险的**文案来源只有一个**（后端 payload 里的
 * `tiers` / `risk_labels`），这里只负责"取不到就回落成原值"——下拉里出现空标签
 * 或徽章写错一个字（"危险" vs "敏感"）都是安全事故级别的显示错误。
 */
import type { ActionParamBrief, PermissionTierBrief, PlatformActionRow } from './types'

export interface ActionGroup {
  category: string
  label: string
  rows: PlatformActionRow[]
}

/** 按类别分组，保持后端给的顺序（后端已按目录声明顺序排好）。 */
export function groupActions(actions: PlatformActionRow[]): ActionGroup[] {
  const groups: ActionGroup[] = []
  const index = new Map<string, ActionGroup>()
  for (const row of actions) {
    let group = index.get(row.category)
    if (!group) {
      group = { category: row.category, label: row.category_label || row.category, rows: [] }
      index.set(row.category, group)
      groups.push(group)
    }
    group.rows.push(row)
  }
  return groups
}

/** 单个参数的紧凑写法：`times:int[1~20]`、`type(allow|audit|refuse)`、`content 必填`。 */
export function describeParam(param: ActionParamBrief): string {
  let head = String(param.name)
  if (param.choices && param.choices.length) head += `(${param.choices.join('|')})`
  else if (param.type && param.type !== 'string') head += `:${param.type}`
  const low = param.minimum === null || param.minimum === undefined ? '' : String(param.minimum)
  const high = param.maximum === null || param.maximum === undefined ? '' : String(param.maximum)
  if (low || high) head += `[${low}~${high}]`
  return param.required ? `${head} 必填` : head
}

/** 参数摘要（表格里那一列）；没有参数的动作写「无参数」而不是留空。 */
export function paramSummary(params: ActionParamBrief[]): string {
  if (!params || !params.length) return '无参数'
  return params.map(describeParam).join('，')
}

/** 下拉选项：`tiers` 为准，标签缺失时回落档位 id（绝不给空标签）。 */
export function tierOptions(
  tiers: PermissionTierBrief[],
): Array<{ value: string; label: string }> {
  return (tiers ?? []).map((tier) => ({ value: String(tier.id), label: tier.label || String(tier.id) }))
}

export function tierLabel(tiers: PermissionTierBrief[], tier: string): string {
  const found = (tiers ?? []).find((item) => String(item.id) === String(tier))
  return (found && found.label) || String(tier)
}

export function tierDescription(tiers: PermissionTierBrief[], tier: string): string {
  const found = (tiers ?? []).find((item) => String(item.id) === String(tier))
  return (found && found.description) || ''
}

/**
 * 档位分布 → 一行紧凑文案（`所有人 42 · 关闭 17`），按四档固定顺序。
 *
 * **只列非零档位**：四档全写出来会撑爆统计卡（"仅群主 / 管理员 0" 这种没人看，
 * 还会把真正有数的档位截断）；全是 0 时回 `—`（空目录）。
 */
export function tierBreakdown(
  tiers: PermissionTierBrief[],
  counts: Record<string, number> | undefined,
): string {
  const table = counts ?? {}
  const parts = (tiers ?? [])
    .filter((tier) => (table[String(tier.id)] ?? 0) > 0)
    .map((tier) => `${tier.label || String(tier.id)} ${table[String(tier.id)]}`)
  return parts.length ? parts.join(' · ') : '—'
}

/** 风险文案：后端 `risk_labels` 为准，未知层级回落原值（不猜）。 */
export function riskLabel(labels: Record<string, string> | undefined, risk: string): string {
  const found = (labels ?? {})[String(risk)]
  return found || String(risk || '未知')
}

/** 风险徽章语气：dangerous → danger，sensitive → warn，其余中性。 */
export function riskTone(risk: string): 'neutral' | 'warn' | 'danger' {
  if (risk === 'dangerous') return 'danger'
  if (risk === 'sensitive') return 'warn'
  return 'neutral'
}

/** 行状态：实际能不能用（`enabled`），以及是不是被配置开关挡住的。 */
export function rowState(row: PlatformActionRow): {
  text: string
  tone: 'ok' | 'neutral' | 'warn'
} {
  if (row.enabled) return { text: '已启用', tone: 'ok' }
  if (row.config_enabled === false) return { text: '配置开关已关闭', tone: 'warn' }
  return { text: '已关闭', tone: 'neutral' }
}
