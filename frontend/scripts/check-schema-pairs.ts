/**
 * 「名单 + 它自己的开关」配对规则的回归测试（无需任何依赖）：
 *
 *     cd plugin/frontend && pnpm test:unit
 */
import assert from 'node:assert/strict'
import { attachOnlySwitches, fieldKey, onlyTarget } from '../src/schema-pairs.ts'

const field = (path: string, type: string) => ({ path, node: { type } })

// 路径工具。
assert.equal(fieldKey('qq_access.bot_accounts_only'), 'bot_accounts_only')
assert.equal(fieldKey('bot_accounts'), 'bot_accounts')
assert.equal(fieldKey(''), '')
assert.equal(onlyTarget('bot_accounts_only'), 'bot_accounts')
assert.equal(onlyTarget('bot_accounts'), '', '不以 _only 结尾的不是开关')

// 三张名单 + 三个开关：开关被收进名单，且不再单独渲染。
const group = [
  field('qq_access.bot_accounts', 'list'),
  field('qq_access.bot_accounts_only', 'bool'),
  field('qq_access.user_accounts', 'list'),
  field('qq_access.user_accounts_only', 'bool'),
  field('qq_access.group_chats', 'list'),
  field('qq_access.group_chats_only', 'bool'),
  field('qq_access.ignore_self_messages', 'bool'),
]
const { visible, attached } = attachOnlySwitches(group)
assert.deepEqual(
  visible.map((item) => item.path),
  ['qq_access.bot_accounts', 'qq_access.user_accounts', 'qq_access.group_chats', 'qq_access.ignore_self_messages'],
  '三个开关从独立渲染里摘掉，其它布尔字段（忽略自己消息）保持独立',
)
assert.deepEqual(
  Object.keys(attached).sort(),
  ['qq_access.bot_accounts', 'qq_access.group_chats', 'qq_access.user_accounts'],
)
assert.equal(attached['qq_access.user_accounts'].path, 'qq_access.user_accounts_only')

// 反向：分组里只有开关、没有对应名单（名字碰巧以 _only 结尾的普通开关）→ 不配对、不动它。
const lonely = [field('runtime.auto_create', 'bool'), field('x.something_only', 'bool')]
const lonelyResult = attachOnlySwitches(lonely)
assert.deepEqual(lonelyResult.visible.map((item) => item.path), ['runtime.auto_create', 'x.something_only'])
assert.deepEqual(lonelyResult.attached, {})

// 反向：同名但不是 list（比如两个 bool）→ 不配对。
const notAList = [field('g.cache', 'bool'), field('g.cache_only', 'bool')]
assert.deepEqual(attachOnlySwitches(notAList).visible.map((item) => item.path), ['g.cache', 'g.cache_only'])
assert.deepEqual(attachOnlySwitches(notAList).attached, {})

// 开关不是 bool → 不配对（schema 写错时不至于把它塞进名单卡里）。
const notBool = [field('g.rows', 'list'), field('g.rows_only', 'string')]
assert.equal(attachOnlySwitches(notBool).visible.length, 2)

// 空分组不炸。
assert.deepEqual(attachOnlySwitches([]).visible, [])
assert.deepEqual(attachOnlySwitches([]).attached, {})

console.log('schema-pairs: 14 项断言通过')
