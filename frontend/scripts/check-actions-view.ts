/**
 * 「动作」面板纯逻辑的断言（`pnpm test:unit`）。
 *
 * 这些规则出错的后果都是"看不出来的错"：下拉变成空标签、危险动作被显示成敏感、
 * 档位分布少算一类、"NapCat 专属"被标成普通动作——真机上看只是"有点怪"，
 * 没人会去核对。
 */
import assert from 'node:assert/strict'
import {
  BACKEND_NAPCAT, BACKEND_ONEBOT, BACKEND_SNOWLUMA, backendBadges, backendNote, backendTone,
  describeParam, filterNapcatOnly, groupActions, isNapcatOnly, napcatOnlyCount, paramSummary,
  riskLabel, riskTone, rowState, scopeLabel, tierBreakdown, tierDescription, tierLabel,
  tierOptions, tierOptionsFor,
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

// 后端标注：标签逐字 = core 的 BACKEND_LABELS（Python 侧 test_qzone_napcat_channel 对账）。
assert.equal(BACKEND_ONEBOT, '标准 OneBot')
assert.equal(BACKEND_NAPCAT, 'NapCat 专属')
assert.equal(BACKEND_SNOWLUMA, '需要 SnowLuma 扩展')
assert.equal(backendTone(BACKEND_NAPCAT), 'accent')
assert.equal(backendTone(BACKEND_SNOWLUMA), 'warn')
assert.equal(backendTone(BACKEND_ONEBOT), 'neutral')
assert.equal(backendTone('某个新后端'), 'neutral')

// 标准 OneBot / 没声明后端 = 不打徽章（目录里绝大多数动作，显示了全是噪音）。
assert.deepEqual(backendBadges({ backends: [BACKEND_ONEBOT] } as never), [])
assert.deepEqual(backendBadges({} as never), [])
assert.deepEqual(backendBadges({ backends: [] } as never), [])
assert.deepEqual(backendBadges({ backends: ['', null] } as never), [])
// 空字符串/空值不能变成一枚空标签的徽章。
assert.deepEqual(backendBadges({ backends: [undefined, BACKEND_NAPCAT] } as never).map((b) => b.label), [BACKEND_NAPCAT])
// 纯 NapCat：一枚醒目徽章，没有回退。
assert.deepEqual(
  backendBadges({ backends: [BACKEND_NAPCAT], napcat_only: true } as never),
  [{ label: BACKEND_NAPCAT, tone: 'accent', title: `优先走这条通道：${BACKEND_NAPCAT}`, primary: true }],
)
// NapCat 优先 + SnowLuma 回退：首选醒目、回退带前缀且是中性语气（别读成"两个都要"）。
assert.deepEqual(
  backendBadges({ backends: [BACKEND_NAPCAT, BACKEND_SNOWLUMA], napcat_only: true } as never),
  [
    { label: BACKEND_NAPCAT, tone: 'accent', title: `优先走这条通道：${BACKEND_NAPCAT}`, primary: true },
    { label: `回退：${BACKEND_SNOWLUMA}`, tone: 'neutral', title: `首选通道不可用时回退到：${BACKEND_SNOWLUMA}`, primary: false },
  ],
)
// 混合里出现标准 OneBot：它是回退，保留但不加前缀（"回退：标准 OneBot"读起来别扭）。
assert.deepEqual(
  backendBadges({ backends: [BACKEND_NAPCAT, BACKEND_ONEBOT] } as never).map((b) => b.label),
  [BACKEND_NAPCAT, BACKEND_ONEBOT],
)
// 顺序就是优先级：首选必须原样取 `backends[0]`，不许重排。
assert.equal(backendBadges({ backends: [BACKEND_SNOWLUMA, BACKEND_NAPCAT] } as never)[0].label, BACKEND_SNOWLUMA)

// NapCat 专属判定：后端给的布尔优先，缺了按"没有标准 OneBot"回推。
assert.equal(isNapcatOnly({ backends: [BACKEND_NAPCAT], napcat_only: true } as never), true)
assert.equal(isNapcatOnly({ backends: [BACKEND_NAPCAT], napcat_only: false } as never), false)
assert.equal(isNapcatOnly({ backends: [BACKEND_NAPCAT, BACKEND_SNOWLUMA] } as never), true)
assert.equal(isNapcatOnly({ backends: [BACKEND_ONEBOT] } as never), false)
assert.equal(isNapcatOnly({ backends: [] } as never), false)
assert.equal(isNapcatOnly({} as never), false)

// 筛选：打开后只剩这 8 类（这里用 3 行样本），顺序不变；关掉时原样返回。
const sample = [
  { id: 'send_poke', backends: [BACKEND_ONEBOT] },
  { id: 'like_qzone_post', backends: [BACKEND_NAPCAT, BACKEND_SNOWLUMA], napcat_only: true },
  { id: 'update_qq_status', backends: [BACKEND_NAPCAT], napcat_only: true },
] as never
assert.deepEqual(filterNapcatOnly(sample, false), sample)
assert.deepEqual(filterNapcatOnly(sample, true).map((row) => row.id), ['like_qzone_post', 'update_qq_status'])
assert.deepEqual(filterNapcatOnly([], true), [])

// 条数：后端统计优先（筛选按钮上的 N 与目录统计必须同源），没有就按行数。
assert.equal(napcatOnlyCount(sample, { napcat_only: 8 }), 8)
assert.equal(napcatOnlyCount(sample, { napcat_only: 0 }), 0)
assert.equal(napcatOnlyCount(sample, undefined), 2)
assert.equal(napcatOnlyCount(sample, {}), 2)

// 通道说明：空间动作要说清两步机制（get_cookies → p_skey → g_tk），改状态说清"只有 NapCat 有"。
const qzoneNote = backendNote({ id: 'like_qzone_post', category: 'qzone', backends: [BACKEND_NAPCAT, BACKEND_SNOWLUMA], napcat_only: true } as never)
assert.match(qzoneNote, /get_cookies/)
assert.match(qzoneNote, /p_skey/)
assert.match(qzoneNote, /g_tk/)
assert.match(qzoneNote, /SnowLuma/)
const statusNote = backendNote({ id: 'update_qq_status', category: 'status', backends: [BACKEND_NAPCAT], napcat_only: true } as never)
assert.match(statusNote, /set_online_status/)
assert.match(statusNote, /标准 OneBot/)
assert.equal(backendNote({ id: 'send_poke', category: 'interaction', backends: [BACKEND_ONEBOT] } as never), '')
assert.equal(
  backendNote({ id: 'some_new_napcat_action', category: 'misc', backends: [BACKEND_NAPCAT], napcat_only: true } as never),
  '这条动作只有 NapCat 后端提供。',
)

console.log('actions-view ok（分组 / 参数摘要 / 档位文案 / 风险语气 / 行状态 / 后端标注与筛选）')

// 权限档位按动作过滤：私聊动作的下拉里不许出现「仅群管」（选了等于关掉）。
assert.deepEqual(
  tierOptionsFor({ tiers: ['global', 'admin', 'disabled'] }, tiers).map((option) => option.value),
  ['global', 'admin', 'disabled'],
)
assert.equal(tierOptionsFor({ tiers: ['global', 'groupadmin', 'admin', 'disabled'] }, tiers).length, 4)
assert.equal(tierOptionsFor({}, tiers).length, tiers.length, '拿不到 tiers 时回落到全量档位')
assert.equal(scopeLabel(['private', 'group']), '私聊与群聊')
assert.equal(scopeLabel(['group']), '仅群聊')
assert.equal(scopeLabel(['private']), '仅私聊')
assert.equal(scopeLabel([]), '')
console.log('tiers-and-scopes ok（档位按动作过滤 / 适用范围文案）')
