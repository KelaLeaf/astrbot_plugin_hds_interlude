/**
 * 「动作」面板的纯逻辑：分组、参数摘要、档位/风险文案、后端标注与筛选。
 *
 * 零依赖（不 import preact、不碰宿主），所以能直接用 Node 的 TS 剥离跑断言：
 *     cd plugin/frontend && pnpm test:unit      # scripts/check-actions-view.ts
 *
 * 为什么值得单独一个模块：档位与风险的**文案来源只有一个**（后端 payload 里的
 * `tiers` / `risk_labels`），这里只负责"取不到就回落成原值"——下拉里出现空标签
 * 或徽章写错一个字（"危险" vs "敏感"）都是安全事故级别的显示错误。
 *
 * 后端标注（`backends` / `napcat_only`）同理：core 只声明**正式通道**，
 * 这里只负责"把非标准后端标出来、标准 OneBot 不显示"。
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

/** 参数摘要（动作名的悬停里那一行；没有参数的动作写「无参数」而不是留空）。 */
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

/**
 * 这条动作**实际适用**的档位：优先用它自己的 `tiers`（后端下发，来源是
 * `platform_actions.permission_tiers_for`），只有拿不到时才回落到全量档位。
 *
 * 为什么要按动作过滤：「仅群管」对私聊动作没有意义，列在下拉里只会让用户选到一个
 * 永远不生效的档位（用户直接指出过这一点）。
 */
export function tierOptionsFor(
  row: { tiers?: string[] },
  tiers: PermissionTierBrief[],
): Array<{ value: string; label: string }> {
  const allowed = row && Array.isArray(row.tiers) && row.tiers.length ? row.tiers : null
  const pool = allowed
    ? (tiers ?? []).filter((tier) => allowed.includes(String(tier.id)))
    : (tiers ?? [])
  return pool.map((tier) => ({ value: String(tier.id), label: tier.label || String(tier.id) }))
}

/** 适用范围的中文说明（私聊 / 群聊 / 两者）。 */
export function scopeLabel(scopes?: string[]): string {
  const list = Array.isArray(scopes) ? scopes.map((item) => String(item)) : []
  const hasPrivate = list.includes('private')
  const hasGroup = list.includes('group')
  if (hasPrivate && hasGroup) return '私聊与群聊'
  if (hasGroup) return '仅群聊'
  if (hasPrivate) return '仅私聊'
  return ''
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

/* ------------------------------------------------------------------ 后端标注 */

/**
 * 后端标签（**逐字 = core `BACKEND_LABELS` 的值**）。
 *
 * 为什么在前端再钉一遍字面量：徽章要按后端种类给语气（NapCat 醒目），而 payload 里
 * 只有人话标签、没有后端 id。改 core 的文案必须同时改这里，Python 侧
 * `test_qzone_napcat_channel.BackendCatalogTests` 与这里的断言互为对账。
 *
 * core 只声明**正式通道**，所以这里也没有"回退徽章"这种东西：一条动作的非标准后端
 * 全部平铺成一枚枚同权重的标注（**没有回退实现**：上游那套扩展动作名在 AstrBot 的任何
 * 后端上都不存在，回退分支已整条删除——见 `docs/PORTING_NOTES.md` §34.1）。
 */
export const BACKEND_ONEBOT = '标准 OneBot'
export const BACKEND_NAPCAT = 'NapCat 专属'

export interface BackendBadge {
  label: string
  tone: 'accent' | 'warn' | 'neutral'
  title: string
}

/** 后端标签 → 徽章语气：NapCat 醒目，其余中性。 */
export function backendTone(label: string): 'accent' | 'warn' | 'neutral' {
  if (label === BACKEND_NAPCAT) return 'accent'
  return 'neutral'
}

/**
 * 一行的后端徽章。规则：
 *
 * 1. **只有「标准 OneBot」= 不显示**（目录里绝大多数动作都是它，显示了全是噪音）；
 * 2. 其余每个后端一枚徽章，语气按后端种类分；
 * 3. 缺 `backends`（老后端）当作标准 OneBot，同样不显示。
 */
export function backendBadges(row: PlatformActionRow): BackendBadge[] {
  const labels = (row?.backends ?? []).filter((label): label is string => Boolean(label))
  if (!labels.length) return []
  if (labels.length === 1 && labels[0] === BACKEND_ONEBOT) return []
  return labels.map((label) => ({
    label,
    tone: backendTone(label),
    title: `这条动作走这个后端：${label}`,
  }))
}

/** NapCat 专属：后端里没有标准 OneBot（`napcat_only` 缺省时按标签回推）。 */
export function isNapcatOnly(row: PlatformActionRow): boolean {
  if (typeof row?.napcat_only === 'boolean') return row.napcat_only
  const labels = row?.backends ?? []
  return labels.length > 0 && !labels.includes(BACKEND_ONEBOT)
}

/** 「只看 NapCat 专属」筛选：`on=false` 时原样返回（不重排）。 */
export function filterNapcatOnly(actions: PlatformActionRow[], on: boolean): PlatformActionRow[] {
  if (!on) return actions
  return actions.filter(isNapcatOnly)
}

/**
 * 行内那句「这条动作走什么通道」。
 *
 * v1.7.5：统一成**一句人话** —— 「非 NapCat 后端无法使用」（用户指定的口径）。
 * 早先这里按类别分三套写法（空间那套还会展开 `get_cookies` / `p_skey` / `g_tk` 的
 * 握手细节），用户明确要求删掉那些实现细节、只留结论：界面上只承诺"哪个后端能做"。
 */
export function backendNote(row: PlatformActionRow): string {
  if (!isNapcatOnly(row)) return ''
  return '非 NapCat 后端无法使用。'
}

/** 「只看 NapCat 专属」按钮上的条数：优先用后端统计，缺了按行数。 */
export function napcatOnlyCount(
  actions: PlatformActionRow[],
  stats?: { napcat_only?: number },
): number {
  const declared = stats?.napcat_only
  if (typeof declared === 'number' && declared >= 0) return declared
  return (actions ?? []).filter(isNapcatOnly).length
}
