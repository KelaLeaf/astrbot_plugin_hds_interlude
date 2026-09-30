/**
 * 「作品」面板纯逻辑的断言（`pnpm test:unit`）。
 *
 * 这里钉的都是"界面上看不出来的错"：把"她提议的"显示成"你自己改的"、把功能没开
 * 说成"她还什么都没写"、把一个灰按钮的原因留空、导出文件名带路径分隔符。
 */
import assert from 'node:assert/strict'
import {
  USER_ONLY_NOTE, authorLabel, authorTone, createBlocker, draftBlocker, exportFileName,
  exportSummary, jobStatusLabel, jobStatusTone, proposalStatusLabel, proposalStatusTone,
  worksEmptyHint,
} from '../src/works-view.ts'

// 作者：两种拼写与未知值
assert.equal(authorLabel('user'), '你')
assert.equal(authorLabel('protagonist'), '她')
assert.equal(authorTone('user'), 'ok')
assert.equal(authorTone('protagonist'), 'accent', 'protagonist 必须有区分度的徽章')
assert.notEqual(authorTone('user'), authorTone('protagonist'))
assert.equal(authorLabel(''), '未知作者')
assert.equal(authorLabel('someone'), 'someone', '不认识的作者照原样显示，不要编一个身份')
assert.equal(authorTone('someone'), 'neutral')

// 提案状态
assert.equal(proposalStatusLabel('pending'), '待你决定')
assert.equal(proposalStatusLabel('accepted'), '已接受')
assert.equal(proposalStatusLabel('rejected'), '已驳回')
assert.equal(proposalStatusTone('pending'), 'warn')
assert.equal(proposalStatusTone('accepted'), 'ok')
assert.equal(proposalStatusLabel('weird'), 'weird')

// 写手任务状态
assert.equal(jobStatusLabel('running'), '正在写')
assert.equal(jobStatusTone('running'), 'accent')
assert.equal(jobStatusTone('failed'), 'danger')
assert.equal(jobStatusTone('interrupted'), 'danger', '中断不是成功，别给中性的徽章')
assert.equal(jobStatusTone('cancelled'), 'neutral')
assert.match(jobStatusLabel('interrupted'), /不会自动重跑/)
assert.match(jobStatusLabel('interrupted'), /重启/, '要让人明白是插件重启过，不是她写失败了')

// 只有用户能接受 / 驳回：这句必须说清"她只能提议"
assert.match(USER_ONLY_NOTE, /只有你/)
assert.match(USER_ONLY_NOTE, /提议/)

// 空态：三种情况说法必须不同
const notReady = worksEmptyHint({ available: false, enabled: true, count: 0, hint: '服务层未就绪' })
const disabled = worksEmptyHint({ available: true, enabled: false, count: 0 })
const nothingYet = worksEmptyHint({ available: true, enabled: true, count: 0 })
const hasWork = worksEmptyHint({ available: true, enabled: true, count: 2 })
assert.equal(notReady, '服务层未就绪')
assert.match(disabled, /配置/)
assert.match(nothingYet, /她还什么都没写/)
assert.notEqual(disabled, nothingYet, '功能没开与"她还没写"不能是同一句话')
assert.equal(hasWork, '')
assert.match(worksEmptyHint({ available: false, enabled: false, count: 0 }), /服务层未就绪/)

// 起草按钮：能按 / 四种不能按的原因
assert.equal(draftBlocker({ available: true, enabled: true, hasWork: true, mayPropose: true }), '')
assert.match(draftBlocker({ available: false, enabled: true, hasWork: true, mayPropose: true }), /服务层未就绪/)
assert.match(draftBlocker({ available: true, enabled: false, hasWork: true, mayPropose: true }), /配置/)
assert.match(draftBlocker({ available: true, enabled: true, hasWork: false, mayPropose: true }), /还没有这件作品/)
assert.match(
  draftBlocker({ available: true, enabled: true, hasWork: true, mayPropose: false, reason: '已有一次写手任务在跑' }),
  /在跑/,
)
assert.match(
  draftBlocker({ available: true, enabled: true, hasWork: true, mayPropose: false }),
  /上一次写手任务/,
  '服务层没给原因时也要有一句人话',
)

// 新建作品：能建 / 五种不能建
{
  const ok = { available: true, enabled: true, participants: 1, title: '海边的信', content: '正文' }
  assert.equal(createBlocker(ok), '')
  assert.match(createBlocker({ ...ok, available: false, hint: '服务层未就绪' }), /服务层未就绪/)
  assert.match(createBlocker({ ...ok, enabled: false }), /配置/)
  assert.match(createBlocker({ ...ok, participants: 0 }), /参与者/)
  assert.match(createBlocker({ ...ok, title: '   ' }), /标题/)
  assert.match(createBlocker({ ...ok, content: '' }), /正文/)
  assert.match(createBlocker({ ...ok, titleLimit: 3, title: '四个字标题' }), /标题太长/)
  assert.match(createBlocker({ ...ok, contentLimit: 2, content: '三个字' }), /正文太长/)
}

// 导出
assert.equal(exportSummary(0, 0), '没有可导出的内容')
assert.match(exportSummary(3, 7000), /共 3 段/)
assert.match(exportSummary(3, 7000), /7000 字符/)
assert.equal(exportFileName('海边的信', 'abcdef1234'), 'hdsi-work-海边的信.json')
assert.equal(exportFileName('a/b:c*d?e"f<g>h|i', 'abcdef12'), 'hdsi-work-a-b-c-d-e-f-g-h-i.json')
assert.equal(exportFileName('', 'abcdef1234'), 'hdsi-work-abcdef12.json')
assert.equal(exportFileName('', ''), 'hdsi-work-work.json')
assert.ok(!exportFileName('x/y', 'z').includes('/'), '文件名里不能有路径分隔符')

console.log('works-view ok')
