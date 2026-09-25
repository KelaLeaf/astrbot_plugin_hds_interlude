/**
 * 剧本切换器文案的回归测试（无需任何依赖）：
 *
 *     cd plugin/frontend && pnpm test:unit
 */
import assert from 'node:assert/strict'
import { storyLabel, storyStatusText, isSharedStory, canMergeStory } from '../src/story-label.ts'

// 状态中文名。
assert.equal(storyStatusText('active'), '进行中')
assert.equal(storyStatusText('paused'), '已暂停')
assert.equal(storyStatusText('archived'), '已归档')
assert.equal(storyStatusText(''), '未知')

// 标签：角色 · 平台 · 条目数 ·（多人时才写人数）· 状态。
assert.equal(
  storyLabel({ id: 'character:qq:1', character: '凌梦', platform: 'qq', status: 'active', entries: 128, participants: 3 }),
  '凌梦 · qq · 128 条 · 3 人 · 进行中',
)
assert.equal(
  storyLabel({ id: 'qq:1:2', character: '凌梦', platform: 'qq', status: 'archived', entries: 4, participants: 1 }),
  '凌梦 · qq · 4 条 · 已归档',
  '只有一条关系分支时不写人数',
)
// 角色名缺失时用 id 前缀兜底，别显示成空按钮。
assert.equal(storyLabel({ id: 'qq:1:2222222222', status: 'active' }).startsWith('qq:1:222222'), true)
// entries 缺失按 0，不能显示 NaN。
assert.equal(storyLabel({ id: 'x', character: '凌梦', status: 'active' }), '凌梦 · 0 条 · 进行中')

// 主剧本判定：认 `shared` 标记，也认 `character:` 前缀。
assert.equal(isSharedStory({ id: 'character:qq:1' }), true)
assert.equal(isSharedStory({ id: 'qq:1:2', shared: true }), true)
assert.equal(isSharedStory({ id: 'qq:1:2' }), false)
assert.equal(isSharedStory(null), false)

// 「并入主剧本」按钮的出现条件。
assert.equal(canMergeStory({ id: 'qq:1:2' }, 'character:qq:1'), true)
assert.equal(canMergeStory({ id: 'character:qq:1' }, 'character:qq:1'), false, '主剧本自己不显示并入按钮')
assert.equal(canMergeStory({ id: 'qq:1:2' }, ''), false, '不知道主剧本是谁时不显示')
assert.equal(canMergeStory(null, 'character:qq:1'), false)

console.log('story-label: 15 项断言通过')
