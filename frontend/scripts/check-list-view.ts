/**
 * 长列表折叠规则的断言（`pnpm test:unit`）。
 */
import assert from 'node:assert/strict'
import { countLabel, listWindow, moreLabel } from '../src/list-view.ts'

// 条目数不超过上限：全显、不滚动、没有折叠按钮。
assert.deepEqual(listWindow(3, 10, false), { shown: 3, hidden: 0, scroll: false })
assert.equal(countLabel(3, 10), '')
assert.equal(countLabel(10, 10), '')

// 收起：只显示前 limit 条，其余记账。
assert.deepEqual(listWindow(120, 10, false), { shown: 10, hidden: 110, scroll: false })
assert.equal(moreLabel(110), '还有 110 条')
assert.equal(countLabel(120, 10), '共 120 条')
assert.equal(countLabel(20, 8, '个人'), '共 20 个人')

// 展开：全部渲染，但套滚动框，页面不再变长。
assert.deepEqual(listWindow(120, 10, true), { shown: 120, hidden: 0, scroll: true })

// limit <= 0 = 不折叠（固定短表用）。
assert.deepEqual(listWindow(13, 0, false), { shown: 13, hidden: 0, scroll: false })
assert.deepEqual(listWindow(500, -1, false), { shown: 500, hidden: 0, scroll: false })

// 脏数据不炸：总数非数/负数、上限小数都归一化。
assert.deepEqual(listWindow(Number.NaN, 10, false), { shown: 0, hidden: 0, scroll: false })
assert.deepEqual(listWindow(-5, 10, false), { shown: 0, hidden: 0, scroll: false })
assert.deepEqual(listWindow(9, 4.7, false), { shown: 4, hidden: 5, scroll: false })
assert.equal(moreLabel(-3), '还有 0 条')

console.log('list-view ok')
