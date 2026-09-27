/**
 * 聊天记录纯逻辑的回归测试（无需任何依赖）：
 *
 *     cd plugin/frontend && pnpm test:unit
 */
import assert from 'node:assert/strict'
import {
  awaitingText, bubbleSide, clockOf, conversationOrder, parseConversation, sideLabel,
} from '../src/chat-log.ts'

// 哪一侧：来信 / 她的发言 / 写了没发出去的系统条目。
assert.equal(bubbleSide('user-message'), 'in')
assert.equal(bubbleSide('group-message'), 'in')
assert.equal(bubbleSide('character-message'), 'out')
assert.equal(bubbleSide('character-group-message'), 'out')
assert.equal(bubbleSide('outgoing-delivery-failed'), 'system')
assert.equal(bubbleSide('script'), 'out', '未知类型按她的发言处理，至少不会被当成来信')
assert.equal(sideLabel('in'), '对方')
assert.equal(sideLabel('out'), '她')

// 会话键：私聊与群聊分开解析，空值不能当成一条真实会话。
assert.deepEqual(parseConversation('private:onebot:1:2:character:onebot:1'), {
  kind: 'private', id: 'onebot:1:2:character:onebot:1',
})
assert.deepEqual(parseConversation('group:100002770'), { kind: 'group', id: '100002770' })
assert.deepEqual(parseConversation(''), { kind: 'private', id: '' })
assert.deepEqual(parseConversation('group:'), { kind: 'group', id: '' }, '没有群号的键按"没选中"处理')

// 排序：还在等她的排最前，其余按最后一条消息倒序，没说过话的沉底。
const ordered = conversationOrder([
  { name: '没说过话', last_at: '', awaiting: 0 },
  { name: '昨天', last_at: '2026-09-26T10:00:00.000Z', awaiting: 0 },
  { name: '等她回但很旧', last_at: '2026-09-01T10:00:00.000Z', awaiting: 2 },
  { name: '刚刚', last_at: '2026-09-27T07:00:00.000Z', awaiting: 0 },
])
assert.deepEqual(ordered.map((item) => item.name), ['等她回但很旧', '刚刚', '昨天', '没说过话'])

// 时间：只留到分钟，认出不来就原样返回（不留空白）。
assert.equal(clockOf('2026-09-27T07:02:50.937Z'), '09-27 07:02')
assert.equal(clockOf('2026-09-27 07:02:50+00:00'), '09-27 07:02')
assert.equal(clockOf(''), '')
assert.equal(clockOf('刚刚'), '刚刚')

// 未回复文案：0 不写。
assert.equal(awaitingText(2, 5), '等她回 2')
assert.equal(awaitingText(0, 3), '未读 3')
assert.equal(awaitingText(0, 0), '')

console.log('chat-log ok')
