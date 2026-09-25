/**
 * 剧本切换器文案的回归测试（无需任何依赖）：
 *
 *     cd plugin/frontend && pnpm test:unit
 */
import assert from 'node:assert/strict'
import {
  storyLabel, storyStatusText, storyRoleText, isSharedStory, canMergeStory, canPromoteStory,
} from '../src/story-label.ts'

// 状态中文名。
assert.equal(storyStatusText('active'), '进行中')
assert.equal(storyStatusText('paused'), '已暂停')
assert.equal(storyStatusText('archived'), '已归档')
assert.equal(storyStatusText(''), '未知')

// 定位文案：用户分不清"哪部是主线"就是缺这两个字。
assert.equal(storyRoleText({ id: 'character:qq:1', main: true }), '主剧本')
assert.equal(storyRoleText({ id: 'qq:1:2' }), '旧剧本')

// 标签：角色 · 平台 · 主/旧剧本 · 条目数 ·（多人时才写人数）· 状态。
assert.equal(
  storyLabel({ id: 'character:qq:1', character: '凌梦', platform: 'qq', status: 'active', entries: 128, participants: 3, main: true }),
  '凌梦 · qq · 主剧本 · 128 条 · 3 人 · 进行中',
)
assert.equal(
  storyLabel({ id: 'qq:1:2', character: '凌梦', platform: 'qq', status: 'archived', entries: 4, participants: 1 }),
  '凌梦 · qq · 旧剧本 · 4 条 · 已归档',
  '只有一条关系分支时不写人数',
)
// 角色名缺失时用 id 前缀兜底，别显示成空按钮。
assert.equal(storyLabel({ id: 'qq:1:2222222222', status: 'active' }).startsWith('qq:1:222222'), true)
// entries 缺失按 0，不能显示 NaN。
assert.equal(storyLabel({ id: 'x', character: '凌梦', status: 'active' }), '凌梦 · 旧剧本 · 0 条 · 进行中')

// 主剧本判定：认 `shared` 标记，也认 `character:` 前缀。
assert.equal(isSharedStory({ id: 'character:qq:1' }), true)
assert.equal(isSharedStory({ id: 'qq:1:2', shared: true }), true)
assert.equal(isSharedStory({ id: 'qq:1:2' }), false)
assert.equal(isSharedStory(null), false)

// 「并入主剧本」按钮的出现条件：主剧本已定，且选中的不是它。
assert.equal(canMergeStory({ id: 'qq:1:2' }, 'character:qq:1'), true)
assert.equal(canMergeStory({ id: 'character:qq:1' }, 'character:qq:1'), false, '主剧本自己不显示并入按钮')
assert.equal(canMergeStory({ id: 'qq:1:2' }, ''), false, '还没定主剧本时给的是「设为主剧本」')
assert.equal(canMergeStory(null, 'character:qq:1'), false)

// 「设为主剧本」按钮：只有在还没有主剧本时出现；两个按钮互斥。
assert.equal(canPromoteStory({ id: 'qq:1:2' }, ''), true)
assert.equal(canPromoteStory({ id: 'qq:1:2', status: 'archived' }, ''), true, '归档的旧剧本也能立为主剧本（会先复活）')
assert.equal(canPromoteStory({ id: 'qq:1:2' }, 'character:qq:1'), false)
assert.equal(canPromoteStory(null, ''), false)
assert.equal(
  canMergeStory({ id: 'qq:1:2' }, '') && canPromoteStory({ id: 'qq:1:2' }, ''),
  false,
  '两个按钮不能同时出现',
)

console.log('story-label: 24 项断言通过')
