/**
 * 聊天记录面板的纯判定：气泡属于哪一侧、会话怎么排、时间怎么念。
 *
 * 前端没有 JS 测试框架，凡是纯逻辑都放成这样的零依赖模块，再用
 * `pnpm test:unit`（`node --experimental-strip-types scripts/check-chat-log.ts`）跑断言。
 */

export type ChatSide = 'in' | 'out' | 'system'

/** 来信的条目类型（其余私聊/群聊条目都是她发出去的）。 */
const INCOMING = new Set(['user-message', 'group-message'])
/** 系统条目：写了但没投出去，既不是来信也不是她的发言。 */
const SYSTEM = new Set(['outgoing-delivery-failed'])

export function bubbleSide(kind: string): ChatSide {
  if (SYSTEM.has(String(kind ?? ''))) return 'system'
  return INCOMING.has(String(kind ?? '')) ? 'in' : 'out'
}

export function sideLabel(side: ChatSide): string {
  if (side === 'in') return '对方'
  if (side === 'system') return '系统'
  return '她'
}

/** `private:<参与者 id>` / `group:<群号>`；认不出来时返回空 id，调用方按"没选中"处理。 */
export function parseConversation(value: string): { kind: 'private' | 'group'; id: string } {
  const text = String(value ?? '')
  const index = text.indexOf(':')
  if (index <= 0) return { kind: 'private', id: '' }
  const kind = text.slice(0, index) === 'group' ? 'group' : 'private'
  const id = text.slice(index + 1)
  return id ? { kind, id } : { kind, id: '' }
}

/**
 * 会话排序：**还在等她回的排前面**（不管多久没动），其余按最后一条消息的时间倒序。
 * 从来没说过话、只是配置里列着的群沉到最后。
 */
export function conversationOrder<T extends { last_at?: string; awaiting?: number }>(items: T[]): T[] {
  const waiting = (item: T) => (Number(item.awaiting ?? 0) > 0 ? 1 : 0)
  return [...items].sort((a, b) => {
    if (waiting(a) !== waiting(b)) return waiting(b) - waiting(a)
    const left = String(a.last_at ?? '')
    const right = String(b.last_at ?? '')
    if (left === right) return 0
    return left < right ? 1 : -1
  })
}

/** ISO 时间戳 → `MM-DD HH:MM`；认不出来就原样返回，页面不显示空白。 */
export function clockOf(value: string): string {
  const text = String(value ?? '')
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/)
  if (!match) return text
  return `${match[2]}-${match[3]} ${match[4]}:${match[5]}`
}

/** 未回复条数的文案：0 条不写，别在列表里挂一排"0"。 */
export function awaitingText(awaiting?: number, unread?: number): string {
  const pending = Number(awaiting ?? 0)
  const unseen = Number(unread ?? 0)
  if (pending > 0) return `等她回 ${pending}`
  if (unseen > 0) return `未读 ${unseen}`
  return ''
}
