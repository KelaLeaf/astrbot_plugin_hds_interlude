/**
 * 名单与它那个「仅处理名单内」开关的配对——纯函数，单独一个零依赖模块。
 *
 * 为什么需要：`qq_access` 里三张名单（`bot_accounts` / `user_accounts` / `group_chats`）
 * 各自跟一个 `${名单}_only` 布尔开关。按 schema 顺序平铺渲染的话，开关会变成名单卡片
 * **下面另一张独立的小卡片**，看起来像"这个开关不属于上面那张名单"（用户指出
 * "开关按钮不在它所属配置项目的背景框里"）。所以渲染时把 `${名单}_only` **收进**名单
 * 那张卡片里当它自己的开关，而不是另起一张卡。
 *
 * 判定放这里，`frontend/scripts/check-schema-pairs.ts` 用
 * `node --experimental-strip-types` 直接跑（见 `package.json` 的 `test:unit`）。
 */

export interface PairField {
  /** 字段路径（`qq_access.bot_accounts`） */
  path: string
  /** schema 节点；这里只弱读 `type`，所以不约束具体形状（调用方自己的类型照旧）。 */
  node?: unknown
}

/** 弱读一个 schema 节点的 `type`（节点不是对象时当空串）。 */
function nodeType(node: unknown): string {
  if (!node || typeof node !== 'object') return ''
  return String((node as { type?: unknown }).type ?? '')
}

/** 字段路径的最后一段（`qq_access.bot_accounts_only` → `bot_accounts_only`）。 */
export function fieldKey(path: string): string {
  const parts = String(path ?? '').split('.')
  return parts[parts.length - 1] ?? ''
}

/** 开关字段对应的名单字段名：`bot_accounts_only` → `bot_accounts`；不是开关则返回空串。 */
export function onlyTarget(key: string): string {
  return key.endsWith('_only') ? key.slice(0, -'_only'.length) : ''
}

/**
 * 把 `${名单}_only` 收进对应的名单字段。
 *
 * 返回 `{ visible, attached }`：`visible` 是要独立渲染的字段（开关已被摘掉），
 * `attached` 是「名单路径 → 开关字段」。配对成立的条件有两条，缺一不可：
 *
 * 1. 同一分组里**确实存在**那个名单字段，且它是个 `list`（不然可能只是名字碰巧以
 *    `_only` 结尾的普通开关，不该被塞进别人卡里）；
 * 2. 开关自己是 `bool`。
 */
export function attachOnlySwitches<T extends PairField>(
  fields: readonly T[],
): { visible: T[]; attached: Record<string, T> } {
  const lists = new Set(
    fields.filter((field) => nodeType(field.node) === 'list').map((field) => fieldKey(field.path)),
  )
  const attached: Record<string, T> = {}
  const visible: T[] = []
  for (const field of fields) {
    const key = fieldKey(field.path)
    const target = onlyTarget(key)
    if (target && nodeType(field.node) === 'bool' && lists.has(target)) {
      attached[`${field.path.slice(0, field.path.length - key.length)}${target}`] = field
      continue
    }
    visible.push(field)
  }
  return { visible, attached }
}
