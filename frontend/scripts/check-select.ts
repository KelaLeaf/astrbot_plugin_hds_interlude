/**
 * 下拉判定的回归测试（无需任何依赖）：
 *
 *     cd plugin/frontend && pnpm test:unit
 *
 * 用的是 Node 自带的 TS 剥离（`--experimental-strip-types`，Node ≥ 22.6），
 * 所以这里只 import 纯函数模块，不碰 preact / DOM。
 */
import assert from 'node:assert/strict'
import { selectMatches, selectDisplay } from '../src/select-match.ts'

// 起因：`vision.max_image_dimension` 的候选项是数字，而当前值字符串化后是 '1024'。
// 旧实现 `option.value === value` 在这里为 false → 下拉显示成占位符（用户实测发现）。
assert.equal(selectMatches(1024, '1024'), true, '数字选项要能匹配字符串化的当前值')
assert.equal(selectMatches(1024, 1024), true)
assert.equal(selectMatches('1024', 1024), true)
assert.equal(selectMatches(0, '0'), true, '0 也是合法值，不能被当成"没值"')
assert.equal(selectMatches(0, ''), false)
assert.equal(selectMatches('a', null), false)
assert.equal(selectMatches('a', undefined), false)
assert.equal(selectMatches('detail', 'auto'), false)

// 显示文案：命中显示 label；没命中但有值就显示值；彻底没值才显示占位符。
assert.equal(selectDisplay('1024', '1024', '请选择…'), '1024')
assert.equal(selectDisplay(undefined, 1024, '请选择…'), '1024', '值不在候选项里也要显示出来')
assert.equal(selectDisplay(undefined, '', '请选择…'), '请选择…')
assert.equal(selectDisplay(undefined, null, '请选择…'), '请选择…')
assert.equal(selectDisplay(undefined, 0, '请选择…'), '0', '0 要显示成 0 而不是占位符')

console.log('select-match: 13 项断言通过')
