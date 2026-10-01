/**
 * 「表情库」分组 + 上传纯逻辑的断言（`pnpm test:unit`）。
 *
 * 这里钉的都是"界面上看不出来的错"——它们失败的方向全是**静默做错事**：
 *
 * 1. 默认分组被写死成某个字面量（后端改 id 后，删组时素材悄悄挪错地方）；
 * 2. 组名按**字符**而不是**字节**算（33 个汉字合法、34 个被后端 400，前端却反着判）；
 * 3. 去读那个恒为 `true` 的兼容字段 `registered`（于是界面又长出"第二等公民"）；
 * 4. `group=''` 被当成"未分组"筛选（后端把空串当"不过滤"→ 用户以为筛了，其实拿到整库）；
 * 5. 改名请求不带 `description`（后端当"清空"→ 顺手抹掉分组描述）；
 * 6. 上传参数没挂查询串 / 挂错名字（宿主 bridge 只发 `file` 一个字段）；
 * 7. 重复上传被写成一幅报错的样子（库里已有这张是**结果**，不是失败）。
 *
 * 最后一段是**文案纪律**的源码扫描：面板里凡是有中文的字符串字面量都要短
 * （不许成句、不许带句号），`FORBIDDEN` 词表继续生效并扩充。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  FILTER_ALL_LABEL, GROUP_DELETE_CONFIRM_LABEL, GROUP_DELETE_LABEL, GROUP_DESCRIBE_LABEL,
  GROUP_DESCRIPTION_LIMIT, GROUP_MOVE_LABEL, GROUP_MOVE_PLACEHOLDER, GROUP_MOVE_TO_LABEL,
  GROUP_NAME_HINT, GROUP_NAME_MAX_BYTES, GROUP_SAVE_LABEL, UPLOAD_DUPLICATE_NOTE,
  UPLOAD_DONE_NOTE, UPLOADING_LABEL, UNGROUPED_NAME, canFilterByGroup, canRename,
  defaultGroupId, defaultTarget, deleteMoveOptions, filterGroupOptions, groupDeleteBlocker,
  groupCountText, groupDeleteNote, groupDeletePayload, groupLabel, groupNameBytes, groupNameProblem,
  groupSaveBlocker, groupSaveNote, groupSavePayload, groupsTitle, hasGroupId, itemGroupLabel,
  moveNote, movePayload, normalizeGroups, targetGroupOptions, uploadBlocker, uploadEndpoint,
  uploadOutcome,
} from '../src/sticker-groups.ts'
import { EMPTY_FILTERS, activeFilterCount, overrideMap, stickerParams } from '../src/stickers-view.ts'
import type { StickerGroup } from '../src/types.ts'

const here = dirname(fileURLToPath(import.meta.url))

/**
 * 一份"像后端真回的那种"分组视图（v1.8.4：`groupId` 就是目录名，顺序 =
 * 内置 → 有描述的 → 其余目录 / 有素材的 → 空桶）。
 *
 * 注意 fixture 里**故意留着** `registered`（后端确实还在回这个兼容字段）：
 * 下面的断言要钉住"前端一个字符都不读它"。
 */
function payload(patch: Record<string, unknown> = {}) {
  return {
    items: [
      { groupId: 'collected', name: '未整理', description: '自动收藏与手动上传都先落这里。', count: 90, builtin: true, registered: true, createdAt: '2026-09-01T00:00:00+00:00', updatedAt: '2026-09-01T00:00:00+00:00' },
      { groupId: '日常', name: '日常', description: '闲聊用', count: 12, builtin: false, registered: true, createdAt: '', updatedAt: '' },
      { groupId: '工作', name: '工作', description: '', count: 30, builtin: false, registered: true, createdAt: '', updatedAt: '' },
      { groupId: '', name: '未分组', description: '', count: 5, builtin: false, registered: true, createdAt: '', updatedAt: '' },
    ],
    total: 4,
    truncated: false,
    defaultGroupId: 'collected',
    ...patch,
  }
}

function group(patch: Partial<StickerGroup> = {}): StickerGroup {
  return {
    groupId: '日常', name: '日常', description: '', count: 3,
    builtin: false, createdAt: '', updatedAt: '', ...patch,
  }
}

/* ------------------------------------------------------------ 分组视图拼装 */

const groups = normalizeGroups(payload())
assert.equal(groups.length, 4, '同 id 去重后应当还是四条')
assert.deepEqual(groups.map((row) => row.groupId), ['collected', '日常', '工作', ''],
  '**顺序照后端给的抄**（内置 → 有描述的 → 其余目录 → 空桶），前端不重排')
assert.equal(groups[0].builtin, true)
assert.equal(groups[0].name, '未整理', '内置组的显示名与目录名不同，照后端给的显示')
assert.equal(groups[2].description, '', '只有目录、还没写描述的分组：描述空着就行')
assert.equal(groups[3].groupId, '', '空桶的 id 就是空串')

// **不读 `registered`**：新模型里"目录 / 描述行 / 有素材"都算正式分组，这个字段恒 true。
// 连"模型里没有这个键"也要钉住——哪天有人把它加回来，界面就会长出第二等公民。
assert.ok(!('registered' in groups[0]), '归一化之后不该再有 registered 这个键')
assert.ok(!('registered' in groups[2]))

// 缺字段 / 脏数据不能把整页弄瘫：缺 name 回显目录名，重复 id 只留第一条。
const dirty = normalizeGroups({ items: [{ groupId: 'x' }, { groupId: 'x' }, null, { name: '没有 id' }] })
assert.deepEqual(dirty.map((row) => row.groupId), ['x', ''], '同 id 只留第一条，脏行不炸')
assert.equal(dirty[0].name, 'x', '缺 name 时回显目录名')
assert.deepEqual(normalizeGroups(null), [])
assert.deepEqual(normalizeGroups({}), [])
assert.equal(normalizeGroups({ items: [{ groupId: 'collected', builtin: true }] })[0].builtin, true)
// 计数是脏数据时用 0，不显示 NaN。
assert.equal(normalizeGroups({ items: [{ groupId: 'a', count: 'abc' }] })[0].count, 0)
// 中文目录名原样保留（一个字都不许"规范化"掉）。
assert.equal(normalizeGroups({ items: [{ groupId: '猫猫（表情）' }] })[0].groupId, '猫猫（表情）')

assert.equal(groupLabel({ name: '未整理', groupId: 'collected' }), '未整理')
assert.equal(groupLabel({ groupId: '日常' }), '日常')
assert.equal(groupLabel({ groupId: '' }), UNGROUPED_NAME)
assert.equal(itemGroupLabel({ groupName: '日常', groupId: '日常' }), '日常')
assert.equal(itemGroupLabel({ groupId: '' }), UNGROUPED_NAME, '空 groupId 的素材显示"未分组"')
assert.equal(groupCountText(3), '3 条')
assert.equal(groupsTitle(3), '分组（3）')

/* ------------------------------------------------------------ 默认分组 */

// **直接用后端给的**：写死 'collected' 的话，后端换个默认组就悄悄挪错地方。
assert.equal(defaultGroupId(payload()), 'collected')
assert.equal(defaultGroupId(payload({ defaultGroupId: '未整理' })), '未整理')
assert.equal(defaultGroupId(payload({ defaultGroupId: '  日常  ' })), '日常', '首尾空白去掉')
// 缺字段才兜底：第一个内置组，没有内置才第一个非空分组。
assert.equal(defaultGroupId(payload({ defaultGroupId: '' })), 'collected')
assert.equal(
  defaultGroupId({ items: [{ groupId: 'a' }, { groupId: 'b' }] }),
  'a',
)
assert.equal(defaultGroupId({ items: [] }), '', '什么都没有时给空串，不要编一个 id')
assert.equal(defaultGroupId(null), '')

/* -------------------------------------------------- 目标分组（移动 / 上传） */

const targets = targetGroupOptions(groups)
assert.deepEqual(targets.map((option) => option.value), ['collected', '日常', '工作'],
  '"目录即分组"：列表里每一个有目录名的组都能当目标；空桶除外')
assert.deepEqual(targets, [
  { value: 'collected', label: '未整理' },
  { value: '日常', label: '日常' },
  { value: '工作', label: '工作' },
])
assert.deepEqual(targetGroupOptions(groups, { exclude: 'collected' }).map((o) => o.value), ['日常', '工作'])
assert.deepEqual(targetGroupOptions([]), [])

// 兼容字段 `registered` 就算回了 false（旧形状）也**不许**影响目标资格：
// 新模型里它恒 true，赌它会等于"能不能当目标"就是赌后端永远不改。
assert.deepEqual(
  targetGroupOptions(normalizeGroups({ items: [{ groupId: '老分组', registered: false }] })),
  [{ value: '老分组', label: '老分组' }],
)
// 完全不带这个字段的老后端也一样。
assert.deepEqual(
  targetGroupOptions(normalizeGroups({ items: [{ groupId: '老分组' }] })),
  [{ value: '老分组', label: '老分组' }],
)
assert.deepEqual(
  targetGroupOptions(normalizeGroups({ items: [{ groupId: 'collected', name: '未整理', builtin: true }] })),
  [{ value: 'collected', label: '未整理' }],
)

// 删除时的候选 = 目标候选去掉正在删的那一组。
assert.deepEqual(deleteMoveOptions(groups, '日常').map((o) => o.value), ['collected', '工作'])
assert.deepEqual(deleteMoveOptions(groups, 'collected').map((o) => o.value), ['日常', '工作'])
assert.deepEqual(deleteMoveOptions(groups, '').map((o) => o.value), ['collected', '日常', '工作'])

// 默认目标：优先用后端给的默认分组；它不可用时退回第一个可用目标。
assert.equal(defaultTarget(groups, 'collected'), 'collected')
assert.equal(defaultTarget(groups, '不存在'), 'collected', '默认值不在列表里就不能塞进下拉（否则第一次上传就 400）')
assert.equal(defaultTarget(groups, ''), 'collected')
assert.equal(defaultTarget([], 'collected'), '', '一个目标都没有时给空串（界面据此禁用按钮）')

/* ---------------------------------------------------------- 筛选下拉 */

const filterOptions = filterGroupOptions(groups)
assert.equal(filterOptions[0].value, '')
assert.equal(filterOptions[0].label, FILTER_ALL_LABEL)
assert.deepEqual(filterOptions.map((o) => o.value), ['', 'collected', '日常', '工作'],
  '每个有目录名的组都能筛（素材确实挂在它下面）')
assert.ok(!filterOptions.slice(1).some((o) => o.value === ''), '空桶不许进筛选下拉')
// 空串在后端 = "不过滤"：把它当"未分组"筛，用户会拿到整整一库素材。
assert.equal(canFilterByGroup(''), false)
assert.equal(canFilterByGroup('   '), false)
assert.equal(canFilterByGroup('collected'), true)
assert.equal(canFilterByGroup('日常'), true)
assert.equal(canFilterByGroup(undefined), false)
assert.equal(hasGroupId(''), false)
assert.equal(hasGroupId('日常'), true)

/* ------------------------------------------------------ 内置组的两条禁手 */

assert.equal(canRename(group()), true, '普通分组能改名（改名 = 重命名目录）')
assert.equal(canRename(group({ groupId: 'collected', builtin: true })), false, '内置组的目录名不许改')
assert.equal(canRename({ builtin: 1 }), true, '只有真布尔 true 才算内置（别猜数字）')
assert.equal(canRename(null), true)

assert.equal(groupDeleteBlocker(group({ builtin: true })), '内置分组没有删除', '内置组不给删除入口')
assert.equal(groupDeleteBlocker(group()), '', '普通分组都能删')
assert.deepEqual(GROUP_DELETE_LABEL, '删除分组')
assert.deepEqual(GROUP_DESCRIBE_LABEL, '改描述', '内置组的那个动作只写描述，不碰目录名')

/* -------------------------------------------------- 组名（= 目录名，按字节） */

assert.equal(GROUP_NAME_MAX_BYTES, 100)
assert.equal(GROUP_NAME_HINT, '100 字节（中文约 33 字）')
assert.equal(groupNameBytes('猫'), 3, '一个汉字 3 字节')
assert.equal(groupNameBytes('猫猫'), 6)
assert.equal(groupNameBytes('abc'), 3)
assert.equal(groupNameBytes(' 猫 '), 3, '首尾空白不算（后端也会 strip）')

// 字节上限不是字符上限：**33 个汉字（99 字节）过，34 个（102 字节）拦**。
assert.equal('猫'.repeat(33).length, 33)
assert.equal(groupNameBytes('猫'.repeat(33)), 99)
assert.equal(groupNameProblem('猫'.repeat(33)), '', '33 个汉字 = 99 字节，必须放行')
assert.match(groupNameProblem('猫'.repeat(34)), /100 字节/, '34 个汉字 = 102 字节，必须拦下')
assert.equal(groupNameBytes('a'.repeat(100)), 100)
assert.equal(groupNameProblem('a'.repeat(100)), '', '整 100 字节放行')
assert.match(groupNameProblem('a'.repeat(101)), /最多 100 字节/, '101 字节拦下')
// 上一版按字符数算（上限 60）：60 个汉字会被前端当成"没超"放过去，然后后端 400。
assert.match(groupNameProblem('猫'.repeat(60)), /100 字节/, '60 汉字（180 字节）必须拦')

// 允许的名字：中文、字母、数字、空格、- _ . （） 都行。
for (const name of ['猫猫', '日常 表情', 'work-cats', 'a_b.c', '猫猫（撒娇）', '表情 1 号']) {
  assert.equal(groupNameProblem(name), '', `这个名字该放行：${name}`)
}
// 保留名（写入侧）：`default` 是根目录素材的桶。
assert.match(groupNameProblem('default', { reserved: true }), /保留名/)
assert.equal(groupNameProblem('default'), '', '给既有 default/ 目录写描述时不传 reserved（历史目录要能管理）')
assert.equal(groupNameProblem('Default', { reserved: true }), '', '保留名按字面量判（大小写敏感，与后端一致）')

// 禁字符 / 首字符 / 空名 / 控制字符。
assert.match(groupNameProblem('a/b'), /不能有 \//)
for (const ch of ['\\', ':', '*', '?', '"', '<', '>', '|']) {
  assert.match(groupNameProblem(`a${ch}b`), /不能有/, `禁字符 ${ch} 必须拦`)
}
assert.match(groupNameProblem('.hidden'), /以「.」开头/)
assert.match(groupNameProblem('..'), /以「.」开头/, "'..' 结构性进不来")
assert.equal(groupNameProblem('   '), '分组名必填')
assert.match(groupNameProblem('a\nb'), /控制字符|换行/)
assert.match(groupNameProblem('a\tb'), /控制字符|换行/)

/* ------------------------------------------------------ 保存（请求体 / 模式） */

// 只认 groupId / name / description 三个键（多一个 400）。
assert.deepEqual(Object.keys(groupSavePayload({ name: '日常', description: '' }, '日常')),
  ['groupId', 'name', 'description'])
assert.deepEqual(groupSavePayload({ name: ' 日常 ', description: ' 闲聊用 ' }, '日常'),
  { groupId: '日常', name: '日常', description: '闲聊用' }, '首尾空白由前端先去掉')
// 新建：**不带 groupId 这个键**（缺省即新建），name 就是新目录名。
assert.deepEqual(groupSavePayload({ name: '猫猫', description: '' }, ''), { name: '猫猫', description: '' })
assert.deepEqual(Object.keys(groupSavePayload({ name: '猫猫', description: '' })), ['name', 'description'])
// 描述**永远要发**：后端缺省值是"清空"，只发 name 会顺手抹掉分组描述。
assert.ok('description' in groupSavePayload({ name: '日常', description: '' }, '日常'))
assert.equal(groupSavePayload({ name: '日常', description: '' }, '日常').description, '',
  '空描述是有效值（= 清空），不能被当成"没填"丢掉')

// full 模式（新建 / 改名）：组名与描述都校验。
assert.equal(groupSaveBlocker({ name: '猫猫', description: '' }, 'full'), '')
assert.equal(groupSaveBlocker({ name: '   ', description: '' }, 'full'), '分组名必填')
assert.match(groupSaveBlocker({ name: 'a/b', description: '' }, 'full'), /不能有/)
assert.match(groupSaveBlocker({ name: 'default', description: '' }, 'full'), /保留名/)
assert.match(groupSaveBlocker({ name: '猫'.repeat(34), description: '' }, 'full'), /100 字节/)
assert.equal(groupSaveBlocker({ name: '猫'.repeat(33), description: '' }, 'full'), '')
assert.equal(groupSaveBlocker({ name: '日常', description: 'x'.repeat(GROUP_DESCRIPTION_LIMIT) }, 'full'), '')
assert.match(groupSaveBlocker({ name: '日常', description: 'x'.repeat(GROUP_DESCRIPTION_LIMIT + 1) }, 'full'), /分组描述太长/)
assert.equal(GROUP_DESCRIPTION_LIMIT, 500)

// description 模式（内置组 / 组名没动）：只看描述——名字是后端给的，不该在这儿被拦。
assert.equal(groupSaveBlocker({ name: '未整理', description: '' }, 'description'), '')
assert.equal(groupSaveBlocker({ name: '', description: '' }, 'description'), '', '这条路不看组名')
assert.equal(groupSaveBlocker({ name: 'default', description: '' }, 'description'), '',
  '给既有 default/ 目录写描述要放行')
assert.match(groupSaveBlocker({ name: '', description: 'x'.repeat(GROUP_DESCRIPTION_LIMIT + 1) }, 'description'), /分组描述太长/)
// 默认模式 = full（别让调用点忘了传就跑成"不校验"）。
assert.match(groupSaveBlocker({ name: '   ', description: '' }), /必填/)

/* ------------------------------------------------------ 删除 / 移动 */

assert.deepEqual(groupDeletePayload('日常', 'collected'), { groupId: '日常', moveTo: 'collected' })
assert.deepEqual(groupDeletePayload('日常', ''), { groupId: '日常' }, '没给目标就让后端用它的默认组')
assert.deepEqual(Object.keys(groupDeletePayload('日常', 'collected')), ['groupId', 'moveTo'])

assert.deepEqual(movePayload(['a', 'b'], 'collected'), { assetIds: ['a', 'b'], groupId: 'collected' })
assert.deepEqual(movePayload(['a', 'a', ' b '], 'collected'), { assetIds: ['a', 'b'], groupId: 'collected' },
  '同一批里重复的 assetId 只发一次')
assert.equal(movePayload([], 'collected'), null, '没选素材不发请求')
assert.equal(movePayload(['a'], ''), null, '没选目标不发请求')
assert.equal(movePayload('a', 'collected'), null, '不是数组就是垃圾，别发出去')
assert.deepEqual(Object.keys(movePayload(['a'], 'collected')!), ['assetIds', 'groupId'])
assert.deepEqual(movePayload(['a'], '猫猫'), { assetIds: ['a'], groupId: '猫猫' }, '中文目录名原样发')

/* ------------------------------------------------------------ 上传 */

// 参数只挂查询串（宿主 bridge 的 upload 只发 file 一个字段），空值一律不挂。
assert.equal(uploadEndpoint(), 'console/sticker-upload')
assert.equal(uploadEndpoint({}), 'console/sticker-upload')
assert.equal(
  uploadEndpoint({ groupId: '日常' }),
  'console/sticker-upload?groupId=%E6%97%A5%E5%B8%B8',
)
assert.equal(
  uploadEndpoint({ groupId: 'cats', description: '一只猫', name: '小猫咪' }),
  'console/sticker-upload?groupId=cats&description=%E4%B8%80%E5%8F%AA%E7%8C%AB&name=%E5%B0%8F%E7%8C%AB%E5%92%AA',
  '顺序固定，值要转义',
)
assert.equal(uploadEndpoint({ groupId: '', description: '', name: '' }), 'console/sticker-upload',
  '没填的参数不要挂空串上去')
assert.equal(uploadEndpoint({ groupId: 'a b/c' }), 'console/sticker-upload?groupId=a%20b%2Fc')
// 端点名一个字都不能错：写错只会在运行时 404。
assert.ok(uploadEndpoint({ name: 'x' }).startsWith('console/sticker-upload?'))

assert.equal(uploadBlocker({ enabled: true, hasFile: true }), '')
assert.equal(uploadBlocker({ enabled: false, hasFile: true }), '库未启用')
assert.equal(uploadBlocker({ enabled: true, hasFile: false }), '先选一张图')

// 重复上传是**结果**不是失败：那句话说"库里已有这张"，而且不定性成错误。
const dup = uploadOutcome({ assetId: 'sticker-1', duplicated: true, item: {} as never })
assert.equal(dup.text, UPLOAD_DUPLICATE_NOTE)
assert.equal(dup.duplicated, true)
assert.notEqual(dup.tone, 'danger', '重复上传不许显示成报错')
assert.equal(UPLOAD_DUPLICATE_NOTE, '库里已有这张')
const fresh = uploadOutcome({ assetId: 'sticker-2', duplicated: false, item: {} as never })
assert.equal(fresh.text, UPLOAD_DONE_NOTE)
assert.equal(fresh.duplicated, false)
assert.notEqual(fresh.text, dup.text, '两种结果不能说同一句话')
assert.equal(uploadOutcome(null).duplicated, false)
assert.equal(UPLOADING_LABEL, '上传中…')

/* ------------------------------------------------------------ 结果提示 */

assert.equal(groupSaveNote('create'), '已新建分组')
assert.equal(groupSaveNote('rename'), '已改名分组')
assert.equal(groupSaveNote('description'), '已保存描述')
assert.equal(new Set([groupSaveNote('create'), groupSaveNote('rename'), groupSaveNote('description')]).size, 3,
  '新建 / 改名 / 改描述是三条路，不能说成同一句话')
assert.equal(groupDeleteNote(12), '已删除分组 · 挪走 12 条')
assert.equal(groupDeleteNote(0), '已删除分组 · 挪走 0 条')
assert.ok(!groupDeleteNote(12).includes('素材'), '删组那句话里不许出现"删素材"的暗示')
assert.match(groupDeleteNote(12), /挪走/, '必须说清素材被挪走了')
assert.equal(moveNote(3), '已移动 3 条')
assert.equal(moveNote(undefined), '已移动 0 条')

/* -------------------------------------------------- 筛选组合（带分组） */

// 分组筛选与既有筛选是**叠加**的：都进同一份查询串。
assert.deepEqual(stickerParams(EMPTY_FILTERS, 60, 0), { limit: 60, offset: 0 })
assert.deepEqual(
  stickerParams({ ...EMPTY_FILTERS, group: '日常' }, 60, 0),
  { limit: 60, offset: 0, group: '日常' },
)
assert.deepEqual(
  stickerParams({ status: 'active', kind: 'image', source: 'auto', q: '猫', group: '日常' }, 30, 0),
  { limit: 30, offset: 0, status: 'active', kind: 'image', source: 'auto', q: '猫', group: '日常' },
  '五个筛选项一起上',
)
assert.deepEqual(stickerParams({ ...EMPTY_FILTERS, group: '  ' }, 60, 0), { limit: 60, offset: 0 },
  '空串一定是"不过滤"，不许发一个 group= 上去')
assert.equal(activeFilterCount({ ...EMPTY_FILTERS, group: '日常' }), 1)
assert.equal(activeFilterCount({ ...EMPTY_FILTERS, group: '' }), 0)
assert.equal(activeFilterCount({ status: 'active', kind: 'image', source: 'auto', q: '猫', group: '日常' }), 5)

// 批量移动的响应 → 就地更新映射。
const moved = overrideMap([
  { assetId: 'a', groupId: '日常', groupName: '日常' } as never,
  { assetId: '', groupId: '日常' } as never,
])
assert.deepEqual(Object.keys(moved), ['a'], '没有 assetId 的行丢掉（拿不到键就没法回填）')
assert.equal(moved.a.groupId, '日常')
assert.deepEqual(overrideMap([]), {})
assert.deepEqual(overrideMap(null as never), {})

/* ------------------------------------------------------------ 文案纪律 */

// 面板里**凡是有中文的字符串字面量**都要短：成句的、带句号的说明一律不许回来。
const panelSource = readFileSync(join(here, '..', 'src', 'panels', 'Stickers.tsx'), 'utf8')
const viewSource = readFileSync(join(here, '..', 'src', 'stickers-view.ts'), 'utf8')
const groupsSource = readFileSync(join(here, '..', 'src', 'sticker-groups.ts'), 'utf8')

const FORBIDDEN = [
  // 上一轮那份词表（继续生效）
  '这一页在改什么',
  '彻底删除',
  '不可恢复',
  '可恢复',
  '移除（',
  '已移除',
  '不参与挑选',
  '素材目录：',
  '她下次挑素材时看到的就是这一份',
  // v1.8.3（分组 / 上传）新钉的解释性长句
  '删组会',
  '会把组里的素材',
  '素材会跟着',
  '这个分组里的素材',
  '上传后会',
  '会自动进入',
  '会一并删除',
  '不能撤销',
  '请注意',
  '注意：',
  '说明：',
  '如果你',
  '以便',
  '这样就能',
  // v1.8.4（目录即分组）新钉的：改名 = 重命名目录，这类因果解释只进注释
  '改名会',
  '会把目录',
  '目录就是分组',
  '会影响',
  '会重命名',
]
for (const word of FORBIDDEN) {
  assert.ok(!panelSource.includes(word), `面板里不许再出现「${word}」`)
  assert.ok(!viewSource.includes(word), `文案模块里不许再出现「${word}」`)
}

/** 取出源码里**会出现在界面上**的中文：字符串字面量 + JSX 文本。 */
function commentsBlanked(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (block) => block.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:])\/\/[^\n]*/g, (match, head: string) => head + ' '.repeat(match.length - head.length))
}

function literals(source: string): Array<[string, number]> {
  const stripped = commentsBlanked(source)
  const found: Array<[string, number]> = []
  const pattern = /'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`/g
  let match: RegExpExecArray | null
  while ((match = pattern.exec(stripped)) !== null) {
    const text = match[1] ?? match[2] ?? match[3] ?? ''
    const line = stripped.slice(0, match.index).split('\n').length
    found.push([text, line])
  }
  return found
}

/** JSX 里的静态文本（`>文字<`）：按钮上的字大部分在这儿，不在字面量里。 */
function jsxTexts(source: string): Array<[string, number]> {
  const stripped = commentsBlanked(source)
  const found: Array<[string, number]> = []
  const pattern = />([^<>{}]+)</g
  let match: RegExpExecArray | null
  while ((match = pattern.exec(stripped)) !== null) {
    const text = (match[1] ?? '').trim()
    if (!text) continue
    const line = stripped.slice(0, match.index).split('\n').length
    found.push([text, line])
  }
  return found
}

function renderedStrings(source: string): string[] {
  return [...literals(source), ...jsxTexts(source)].map(([text]) => text)
}

// 分组模块的**注释**里会提到那些历史词（那正是要记住的事），所以只扫它渲染出来的文案。
for (const word of [...FORBIDDEN, '采纳', '未注册']) {
  assert.ok(!renderedStrings(groupsSource).join('｜').includes(word), `分组模块的文案里不许出现「${word}」`)
}

const CJK = /[\u4e00-\u9fff]/
const cjkCount = (text: string) => text.replace(/\$\{[^}]*\}/g, '').match(/[\u4e00-\u9fff]/g)?.length ?? 0

// 只挑"像人话"的那些（含中文），逐条量长度：超过 18 个汉字就是句子了。
const long: string[] = []
for (const [text, line] of [...literals(panelSource), ...jsxTexts(panelSource)]) {
  if (!CJK.test(text)) continue
  if (cjkCount(text) > 18 || text.includes('。')) long.push(`Stickers.tsx:${line} 「${text.trim()}」`)
}
assert.deepEqual(long, [], '面板文案只留状态词与动作词：不许成句、不许带句号')

// JSX 上的字（按钮、标签、空态）单独卡一道更紧的：超过 12 个汉字就是一句话了。
const longJsx = jsxTexts(panelSource)
  .filter(([text]) => cjkCount(text) > 12)
  .map(([text, line]) => `Stickers.tsx:${line} 「${text}」`)
assert.deepEqual(longJsx, [], '按钮 / 标签上的字要短（状态词、动作词）')

// 分组模块里只有"提示 / 错误 / 动作"这几类字符串，同样不许成句。
const longGroups = literals(groupsSource)
  .filter(([text]) => CJK.test(text) && !text.includes('${'))
  .filter(([text]) => text.includes('。'))
  .map(([text, line]) => `sticker-groups.ts:${line} 「${text}」`)
assert.deepEqual(longGroups, [], '短词模块里不许有句号结尾的句子')

// 动作词就得是动作词。
assert.equal(GROUP_SAVE_LABEL, '保存')
assert.equal(GROUP_MOVE_LABEL, '移动')
assert.equal(GROUP_DELETE_CONFIRM_LABEL, '确认删除分组')
assert.ok(GROUP_DELETE_CONFIRM_LABEL.includes('分组'), '第二下必须说清删的是分组')
assert.equal(GROUP_MOVE_TO_LABEL, '素材挪到')
assert.equal(GROUP_MOVE_PLACEHOLDER, '移到分组…')
// 删组 / 上传 / 内置组这三块要真的挂在面板上（别只写在模块里没人用）。
assert.ok(panelSource.includes('GROUP_DELETE_CONFIRM_LABEL') && panelSource.includes('GROUP_MOVE_TO_LABEL'))
assert.ok(panelSource.includes('uploadEndpoint') && panelSource.includes('FilePicker'))
assert.ok(panelSource.includes('defaultGroupId') && panelSource.includes('targetGroupOptions'))
assert.ok(panelSource.includes('canRename') && panelSource.includes('GROUP_DESCRIBE_LABEL'),
  '内置组的"不许改目录名"要在面板上真的用起来')
assert.ok(!panelSource.includes("'collected'"), '面板里不许写死默认分组的目录名')

console.log('sticker-groups ok（目录即分组 / 目标合法性 / 组名字节 / 删组挪素材 / 上传查询串 / 文案短词）')
