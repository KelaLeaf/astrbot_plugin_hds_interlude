/**
 * 「动作」面板纯逻辑的断言（`pnpm test:unit`）。
 *
 * 这些规则出错的后果都是"看不出来的错"：下拉变成空标签、危险动作被显示成敏感、
 * 档位分布少算一类——真机上看只是"有点怪"，没人会去核对。
 */
import assert from 'node:assert/strict'
import {
  describeParam, groupActions, paramSummary, riskLabel, riskTone, rowState, tierBreakdown,
  tierDescription, tierLabel, tierOptions,
} from '../src/actions-view.ts'

const tiers = [
  { id: 'global', label: '所有人', description: '任何会话里都能用' },
  { id: 'groupadmin', label: '仅群主 / 管理员', description: '只在群里' },
  { id: 'admin', label: '仅插件管理员', description: '只有 HDSI 管理员能用' },
  { id: 'disabled', label: '关闭', description: '任何会话都不能用' },
]

// 分组：保持顺序、不丢动作、不合并同名类别。
const grouped = groupActions([
  { category: 'interaction', category_label: '互动', id: 'send_poke' },
  { category: 'message', category_label: '消息与定时', id: 'schedule_message' },
  { category: 'interaction', category_label: '互动', id: 'send_like' },
] as never)
assert.deepEqual(grouped.map((group) => group.category), ['interaction', 'message'])
assert.deepEqual(grouped[0].rows.map((row) => row.id), ['send_poke', 'send_like'])
assert.equal(grouped[0].label, '互动')
// 类别标签缺失时回落成 key（不能渲染成空白分组标题）。
assert.equal(groupActions([{ category: 'x', category_label: '', id: 'a' }] as never)[0].label, 'x')
assert.deepEqual(groupActions([]), [])

// 参数摘要。
assert.equal(paramSummary([]), '无参数')
assert.equal(describeParam({ name: 'target', label: '目标', type: 'string', required: false, minimum: null, maximum: null, choices: [] }), 'target')
assert.equal(describeParam({ name: 'times', label: '次数', type: 'int', required: false, minimum: 1, maximum: 20, choices: [] }), 'times:int[1~20]')
assert.equal(describeParam({ name: 'option', label: '方式', type: 'string', required: true, minimum: null, maximum: null, choices: ['allow', 'audit', 'refuse'] }), 'option(allow|audit|refuse) 必填')
assert.equal(describeParam({ name: 'duration', label: '时长秒', type: 'int', required: true, minimum: 0, maximum: null, choices: [] }), 'duration:int[0~] 必填')
assert.equal(
  paramSummary([
    { name: 'user_id', label: '用户号', type: 'string', required: true, minimum: null, maximum: null, choices: [] },
    { name: 'count', label: '条数', type: 'int', required: false, minimum: 1, maximum: 50, choices: [] },
  ]),
  'user_id 必填，count:int[1~50]',
)

// 档位下拉：标签缺失也绝不给空标签（空下拉等于用户选不了）。
assert.deepEqual(tierOptions(tiers), [
  { value: 'global', label: '所有人' },
  { value: 'groupadmin', label: '仅群主 / 管理员' },
  { value: 'admin', label: '仅插件管理员' },
  { value: 'disabled', label: '关闭' },
])
assert.deepEqual(tierOptions([{ id: 'admin', label: '', description: '' }]), [{ value: 'admin', label: 'admin' }])
assert.deepEqual(tierOptions([]), [])
assert.equal(tierLabel(tiers, 'disabled'), '关闭')
assert.equal(tierLabel(tiers, 'owner'), 'owner')
assert.equal(tierDescription(tiers, 'global'), '任何会话里都能用')
assert.equal(tierDescription(tiers, 'owner'), '')

// 档位分布：按四档固定顺序，只列非零档位（全零给破折号，别撑爆统计卡）。
assert.equal(tierBreakdown(tiers, { global: 42, disabled: 17 }), '所有人 42 · 关闭 17')
assert.equal(tierBreakdown(tiers, { global: 40, groupadmin: 1, admin: 2, disabled: 19 }),
  '所有人 40 · 仅群主 / 管理员 1 · 仅插件管理员 2 · 关闭 19')
assert.equal(tierBreakdown(tiers, {}), '—')
assert.equal(tierBreakdown(tiers, undefined), '—')
assert.equal(tierBreakdown([], { global: 3 }), '—')

// 风险：文案来自后端，未知层级回落原值；危险的语气必须是 danger。
const labels = { safe: '安全', sensitive: '敏感', dangerous: '危险' }
assert.equal(riskLabel(labels, 'dangerous'), '危险')
assert.equal(riskLabel(labels, 'weird'), 'weird')
assert.equal(riskLabel(undefined, 'safe'), 'safe')
assert.equal(riskTone('dangerous'), 'danger')
assert.equal(riskTone('sensitive'), 'warn')
assert.equal(riskTone('safe'), 'neutral')
assert.equal(riskTone(''), 'neutral')

// 行状态：开关关掉与档位关闭要分开说（前者是"配置开关已关闭"）。
const base = { permission: 'global', default_permission: 'global', config_enabled: null }
assert.deepEqual(rowState({ ...base, enabled: true } as never), { text: '已启用', tone: 'ok' })
assert.deepEqual(rowState({ ...base, enabled: false, config_enabled: false } as never), { text: '配置开关已关闭', tone: 'warn' })
assert.deepEqual(rowState({ ...base, enabled: false, permission: 'disabled' } as never), { text: '已关闭', tone: 'neutral' })

console.log('actions-view ok（分组 / 参数摘要 / 档位文案 / 风险语气 / 行状态）')
