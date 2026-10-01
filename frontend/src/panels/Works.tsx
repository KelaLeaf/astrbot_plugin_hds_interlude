/**
 * 「作品」面板：和她一起写的那件按版本管理的文本。
 *
 * 上游 rc28 的 `works.ts` 只有纯逻辑 + 存储抽象，**service 层一行都没接**——配置组、
 * payload 与控制台入口由本移植版补。这一页是它的界面，有一条语义不能含糊：
 *
 * **只有你能接受 / 驳回她的修改提案。** 她（模型）只能"提议"；接受会立刻把提案变成
 * 一条新版本，驳回只留一条结论。所以提案卡上永远挂着那句话，界面里也不会出现任何
 * "她自己接受了"的措辞。
 *
 * 后端接口在 `adapters/console_api.py`（取数一律走服务层 `chunk14`），这里只负责显示与
 * 把用户动作发出去；写操作失败一律照实显示，绝不假装成功。
 */
import { useEffect, useState } from 'preact/hooks'
import { apiGet, apiPost, describeError } from '../bridge'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type {
  ParticipantRow, WorkDetailPayload, WorkExportPayload, WorkJobRow, WorkListRow,
  WorkProposalRow, WorksOverviewPayload,
} from '../types'
import {
  USER_ONLY_NOTE, authorLabel, authorTone, createBlocker, draftBlocker, exportFileName,
  exportSummary, jobStatusLabel, jobStatusTone, proposalStatusLabel, proposalStatusTone,
  worksEmptyHint,
} from '../works-view'
import {
  Badge, Button, ConfirmButton, CopyFallback, Empty, ErrorNote, Grid, Input, KeyValue, Loading,
  LongList, Note, Panel, Select, Stack, Stat, Table, Textarea,
} from '../components/ui'

/** 正文 / 提案正文的内滚高度（仓库约定：长文固定高度内滚，页面高度不跟着内容涨）。 */
const TEXT_SCROLL = 'prose-body max-h-[28rem] overflow-y-auto rounded-lg border border-line bg-raised p-3 font-serif text-xs'

export function Works({ storyId, refreshKey, onNavigate }: PanelProps) {
  const list = useQuery<WorksOverviewPayload>(
    'console/works',
    { story_id: storyId },
    { nonce: refreshKey },
  )
  const [workId, setWorkId] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [reason, setReason] = useState('')
  const [brief, setBrief] = useState('')
  const [exported, setExported] = useState<WorkExportPayload | null>(null)
  const [copied, setCopied] = useState(-1)
  // 剪贴板写不进去时的兜底：就地展开只读 textarea（内容已全选）让用户自己 Ctrl+C。
  // 沙箱 iframe 里 `clipboard-write` 默认不给跨源 iframe，这条路必须存在。
  const [manualCopy, setManualCopy] = useState<{ scope: 'all' | 'part'; index: number } | null>(null)
  const [creating, setCreating] = useState(false)
  const [newOwner, setNewOwner] = useState('')
  const [newTitle, setNewTitle] = useState('')
  const [newContent, setNewContent] = useState('')
  // 新建作品要挑一个参与者：只列剧本里真的登记过的账号（列表接口在「配置」面板也在用）。
  const people = useQuery<{ participants: ParticipantRow[] }>(
    'console/participants',
    { story_id: storyId },
    { enabled: creating, nonce: refreshKey },
  )
  const participants = people.data?.participants ?? []

  const rows = list.data?.works ?? []
  const selectedId = workId && rows.some((row) => row.work_id === workId)
    ? workId
    : (rows[0]?.work_id ?? '')
  const detail = useQuery<WorkDetailPayload>(
    'console/work',
    { work_id: selectedId },
    { enabled: Boolean(selectedId), nonce: refreshKey },
  )
  const work = detail.data

  // 选中的作品没了 / 换剧本：回到第一件，别让详情继续查一个不存在的 id。
  useEffect(() => {
    if (workId && !rows.some((row) => row.work_id === workId)) setWorkId('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workId, rows.map((row) => row.work_id).join('|')])

  // 换作品时收起编辑框与导出结果（它们属于上一件作品）。
  useEffect(() => {
    setEditing(false)
    setDraft('')
    setReason('')
    setBrief('')
    setExported(null)
    setCopied(-1)
  }, [selectedId])

  /** 写操作的统一收口：失败照实报（返回 `false`，调用方别把输入清掉），成功后刷新两面。 */
  async function run(key: string, action: () => Promise<unknown>, done: string): Promise<boolean> {
    setBusy(key)
    setError('')
    setNotice('')
    try {
      await action()
      setNotice(done)
      list.reload()
      detail.reload()
      return true
    } catch (failure) {
      setError(describeError(failure))
      return false
    } finally {
      setBusy('')
    }
  }

  async function saveEdit() {
    const id = selectedId
    if (!id) return
    if (!draft.trim()) {
      setError('正文不能为空')
      return
    }
    const ok = await run('edit', () => apiPost('console/work-edit', {
      work_id: id, content: draft, reason,
    }), '已保存为新版本')
    // 失败时**保留**输入框里的字：让他改一改再存，别把刚写的东西吞掉。
    if (ok) {
      setEditing(false)
      setDraft('')
      setReason('')
    }
  }

  async function createWork() {
    const blocker = createBlocker({
      available: Boolean(list.data?.available),
      enabled: Boolean(list.data?.enabled),
      participants: participants.length,
      title: newTitle,
      content: newContent,
      titleLimit: 120,
      contentLimit: 8000,
    })
    if (blocker) {
      setError(blocker)
      return
    }
    setBusy('create')
    setError('')
    setNotice('')
    try {
      const created = await apiPost<WorkDetailPayload>('console/work-create', {
        story_id: storyId,
        participant_id: newOwner || participants[0]?.participant_id || '',
        title: newTitle,
        content: newContent,
      })
      setWorkId(created.work_id)
      setCreating(false)
      setNewTitle('')
      setNewContent('')
      setNotice('已建好这件作品：之后的修改都是新版本，旧版本会留在时间线里')
      list.reload()
      detail.reload()
    } catch (failure) {
      // 重复创建时后端会把服务层那条"绝不覆盖"原样带回来，照着显示。
      setError(describeError(failure))
    } finally {
      setBusy('')
    }
  }

  function cancelJob(job: WorkJobRow) {
    return run(`cancel-${job.id}`, () => apiPost('console/work-cancel', {
      work_id: selectedId, job_id: job.id,
    }), '已取消这次起草：迟到的结果会被丢弃')
  }

  async function draftByHer() {
    const ok = await run('generate', () => apiPost('console/work-generate', {
      work_id: selectedId, brief,
    }), '已让她起草：结果会作为待决提案回来（只有你能决定要不要）')
    if (ok) setBrief('')
  }

  function resolve(proposal: WorkProposalRow, accept: boolean) {
    const id = selectedId
    const endpoint = accept ? 'console/work-accept' : 'console/work-reject'
    const verb = accept ? '接受' : '驳回'
    return run(`${verb}-${proposal.id}`, () => apiPost(endpoint, {
      work_id: id, proposal_id: proposal.id,
    }), accept ? '已接受：新版本已经生效' : '已驳回：正文没有改动')
  }

  async function loadExport() {
    setBusy('export')
    setError('')
    try {
      setExported(await apiGet<WorkExportPayload>('console/work-export', { work_id: selectedId }))
    } catch (failure) {
      setError(describeError(failure))
    } finally {
      setBusy('')
    }
  }

  /**
   * 复制一段：能直接写剪贴板就写，写不了（沙箱里没有 clipboard-write 权限 →
   * `NotAllowedError`）就**就地**展开只读 textarea 兜底，而不是丢一句错误了事。
   */
  async function copyPart(index: number, text: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(index)
      setManualCopy(null)
      setError('')
    } catch {
      setCopied(-1)
      setError('')
      setNotice('')
      setManualCopy({ scope: 'part', index })
    }
  }

  async function copyAll(text: string) {
    try {
      await navigator.clipboard.writeText(text)
      setNotice('全文已复制到剪贴板')
      setManualCopy(null)
      setError('')
    } catch {
      setNotice('')
      setError('')
      setManualCopy({ scope: 'all', index: -1 })
    }
  }

  function download() {
    if (!exported) return
    const blob = new Blob([exported.parts.join('')], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = exportFileName(exported.title, exported.work_id)
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  }

  if (list.error) return <ErrorNote text={list.error} onRetry={list.reload} />
  if (list.loading && !list.data) return <Loading />
  const payload = list.data
  if (!payload?.available) {
    return <ErrorNote text={payload?.hint || '共同作品尚未启用或服务层未就绪'} onRetry={list.reload} />
  }

  const pending = rows.reduce((sum, row) => sum + row.pending_count, 0)
  const running = rows.reduce((sum, row) => sum + row.jobs_running, 0)

  return (
    <Stack>
      <Grid cols={3}>
        <Stat label="作品" value={rows.length} hint="每个参与者一件" />
        <Stat
          label="待你决定"
          value={pending}
          tone={pending > 0 ? 'warn' : 'neutral'}
          hint={pending > 0 ? '她提了修改，等你接受或驳回' : '没有待决提案'}
        />
        <Stat
          label="写手任务"
          value={running}
          tone={running > 0 ? 'ok' : 'neutral'}
          hint={running > 0 ? '她正在写，写完会变成待决提案' : '当前没有在跑的任务'}
        />
      </Grid>

      {!payload.enabled ? (
        <div class="flex flex-wrap items-center gap-2 rounded-xl border border-warn/40 bg-warn/10 px-4 py-3 text-xs text-warn">
          <span class="min-w-0 flex-1">
            共同作品没启用（配置 <span class="font-mono">works.enabled</span>）。
            {payload.explain ? ` ${payload.explain}。` : ''}
            她现在不会写也不会提议，也不能新建作品；下面能看到的是之前已经写下的内容。
          </span>
          <Button icon="config" onClick={() => onNavigate('config')}>去配置页打开</Button>
        </div>
      ) : null}

      {error ? <ErrorNote text={error} /> : null}
      {notice ? <Note tone="ok">{notice}</Note> : null}

      {creating ? (
        <Panel title="新建共同作品" icon="save" actions={
          <Button icon="close" onClick={() => setCreating(false)}>收起</Button>
        }>
          <Stack>
            <Note>
              新建就是给定第一版正文（作者 = 你）。之后她只能<span class="text-fg">提议</span>修改，接受或驳回都由你来按。
              同一个私聊只能有一件作品——已经有了会拒绝，绝不覆盖。
            </Note>
            {people.error ? <ErrorNote text={people.error} onRetry={people.reload} /> : null}
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label class="flex flex-col gap-1">
                <span class="text-[11px] font-medium text-muted">属于哪个私聊（参与者）</span>
                {participants.length ? (
                  <Select
                    value={newOwner || participants[0].participant_id}
                    onChange={(next) => setNewOwner(String(next))}
                    options={participants.map((person) => ({
                      value: person.participant_id,
                      label: person.display_name
                        ? `${person.display_name}（${person.participant_id}）`
                        : person.participant_id,
                    }))}
                    placeholder="选择参与者…"
                  />
                ) : (
                  <span class="text-[11px] text-muted">
                    还没有已知参与者：先让她和这个账号说过话，再回来建作品。
                  </span>
                )}
              </label>
              <label class="flex flex-col gap-1">
                <span class="text-[11px] font-medium text-muted">标题</span>
                <Input value={newTitle} onInput={setNewTitle} placeholder="例如：海边的信" />
              </label>
            </div>
            <span class="text-[11px] font-medium text-muted">第一版正文</span>
            <Textarea
              value={newContent}
              rows={8}
              onInput={setNewContent}
              placeholder="写下这件作品的开头…（正文是创作素材，她会照着往下写）"
            />
            <div class="flex flex-wrap items-center gap-2">
              <Button variant="primary" icon="save" disabled={Boolean(busy)} onClick={createWork}>
                {busy === 'create' ? '创建中…' : '新建'}
              </Button>
              <Button icon="close" onClick={() => setCreating(false)}>取消</Button>
              <span class="text-[11px] text-muted">{newContent.length} / 8000 字</span>
            </div>
          </Stack>
        </Panel>
      ) : null}

      {rows.length === 0 ? (
        <Panel
          title="还没有共同作品"
          icon="code"
          actions={
            <span title={payload.enabled
              ? '新建一件共同作品（同一个私聊只能有一件）'
              : (payload.explain || '共同作品没启用：去「配置」面板打开它')}>
              <Button
                icon="save"
                disabled={!payload.enabled}
                onClick={() => { setCreating(true); setError('') }}
              >
                新建作品
              </Button>
            </span>
          }
        >
          <Empty text={worksEmptyHint({
            available: payload.available, enabled: payload.enabled, count: 0, hint: payload.hint,
          })} />
        </Panel>
      ) : (
        <div class="grid grid-cols-1 gap-4 lg:grid-cols-[20rem_1fr]">
          <Panel title="作品" icon="code" actions={
            <>
              <span title={payload.enabled
                ? '新建一件共同作品（同一个私聊只能有一件）'
                : (payload.explain || '共同作品没启用：去「配置」面板打开它')}>
                <Button
                  icon="save"
                  disabled={!payload.enabled}
                  onClick={() => { setCreating(true); setError('') }}
                >
                  新建作品
                </Button>
              </span>
              <Button icon="refresh" onClick={() => { list.reload(); detail.reload() }}>刷新</Button>
            </>
          }>
            <LongList
              items={rows}
              limit={8}
              unit="件"
              class="flex flex-col gap-2"
              render={(row) => (
                <WorkListItem
                  key={row.work_id}
                  row={row}
                  active={row.work_id === selectedId}
                  onPick={() => setWorkId(row.work_id)}
                />
              )}
            />
          </Panel>

          {detail.error ? (
            <ErrorNote text={detail.error} onRetry={detail.reload} />
          ) : !work || (detail.loading && !detail.data) ? (
            <Panel title="作品详情" icon="code"><Loading /></Panel>
          ) : !work.available ? (
            <ErrorNote text={work.hint || '共同作品尚未启用或服务层未就绪'} onRetry={detail.reload} />
          ) : (
            <Stack>
              <Panel
                title={work.title || '（没有标题）'}
                icon="code"
                actions={
                  <>
                    <Button icon="download" disabled={Boolean(busy)} onClick={loadExport}>导出</Button>
                    <Button
                      icon="save"
                      disabled={Boolean(busy) || work.broken}
                      onClick={() => {
                        setEditing(!editing)
                        setError('')
                        if (!editing) setDraft(work.content)
                      }}
                    >
                      {editing ? '取消编辑' : '编辑并保存'}
                    </Button>
                  </>
                }
              >
                <Stack>
                  {work.broken ? (
                    <Note tone="warn">{work.hint || '这件作品的数据形状不被识别：原数据已保持原样，控制台没有做任何写入。'}</Note>
                  ) : null}
                  <KeyValue rows={[
                    ['参与者', work.participant || work.participant_id],
                    ['当前版本', `#${work.revision} / ${work.revision_count} 版`],
                    ['起草模式', work.generation_mode === 'separate' ? '独立写手任务' : '主叙事回合里顺手写'],
                    ['正文长度', `${work.content_chars} 字（上限 ${work.limits.content}）`],
                    ['作品 id', <span class="font-mono text-[11px]">{work.work_id}</span>],
                  ]} />
                  <div class={TEXT_SCROLL} title="作品正文（创作素材，原样显示）">
                    {work.content || '（正文为空）'}
                  </div>
                  {editing ? (
                    <Stack class="rounded-xl border border-accent/40 bg-accent/5 p-3">
                      <span class="text-[11px] text-muted">
                        手动改会立刻变成一条新版本（作者 = 你），旧版本仍然留在时间线里。
                        这一段也会被当成创作素材给她看，别把命令写进去。
                      </span>
                      <Textarea
                        value={draft}
                        rows={12}
                        onInput={setDraft}
                        placeholder="作品正文…"
                      />
                      <div class="flex items-center gap-2 text-[11px] text-muted">
                        <span class={draft.length > work.limits.content ? 'text-danger' : ''}>
                          {draft.length} / {work.limits.content} 字
                        </span>
                      </div>
                      <Input value={reason} onInput={setReason} placeholder={`改了什么（可留空；最多 ${work.limits.reason} 字）`} />
                      <div class="flex items-center gap-2">
                        <Button variant="primary" icon="save" disabled={Boolean(busy)} onClick={saveEdit}>
                          {busy === 'edit' ? '保存中…' : '保存为新版本'}
                        </Button>
                        <Button icon="close" onClick={() => setEditing(false)}>取消</Button>
                      </div>
                    </Stack>
                  ) : null}
                </Stack>
              </Panel>

              {exported ? (
                <Panel title="导出" icon="download" actions={
                  <>
                    <Button icon="save" onClick={() => copyAll(exported.parts.join(''))}>复制全文</Button>
                    <Button icon="download" disabled={exported.count === 0} onClick={download}>下载 JSON</Button>
                  </>
                }>
                  <Stack>
                    <span class="text-[11px] text-muted">{exportSummary(exported.count, exported.chars)}</span>
                    {manualCopy?.scope === 'all' ? (
                      <CopyFallback
                        what="全文"
                        text={exported.parts.join('')}
                        onClose={() => setManualCopy(null)}
                      />
                    ) : null}
                    {exported.count === 0 ? <Empty text={exported.hint || '没有可导出的内容'} /> : (
                      <LongList
                        items={exported.parts}
                        limit={4}
                        unit="段"
                        class="flex flex-col gap-2"
                        render={(part, index) => (
                          <div class="flex flex-col gap-1">
                            <div class="flex items-center gap-2 text-[11px] text-muted">
                              <span class="font-mono">第 {index + 1} 段 · {part.length} 字符</span>
                              <Button
                                icon={copied === index ? 'tick' : 'script'}
                                onClick={() => copyPart(index, part)}
                              >
                                {copied === index ? '已复制' : '复制这一段'}
                              </Button>
                            </div>
                            <div class="prose-body max-h-40 overflow-y-auto rounded-lg border border-line bg-raised p-3 font-mono text-[11px]">
                              {part}
                            </div>
                            {manualCopy?.scope === 'part' && manualCopy.index === index ? (
                              <CopyFallback
                                what={`第 ${index + 1} 段`}
                                text={part}
                                onClose={() => setManualCopy(null)}
                              />
                            ) : null}
                          </div>
                        )}
                      />
                    )}
                  </Stack>
                </Panel>
              ) : null}

              <Panel title={`版本时间线（${work.revision_count}）`} icon="clock">
                <LongList
                  items={work.revisions}
                  limit={6}
                  unit="版"
                  class="flex flex-col gap-2"
                  render={(revision) => (
                    <div class="flex flex-col gap-1 rounded-lg border border-line px-3 py-2">
                      <div class="flex flex-wrap items-center gap-2">
                        <span class="font-mono text-[11px] text-muted">#{revision.ordinal}</span>
                        <Badge tone={authorTone(revision.author)}>{authorLabel(revision.author)}</Badge>
                        {revision.current ? <Badge tone="accent">当前版本</Badge> : null}
                        <span class="text-[11px] text-muted">{revision.created_at || '时间未知'}</span>
                        <span class="ml-auto text-[11px] text-muted">{revision.content_chars} 字</span>
                      </div>
                      {revision.current ? (
                        <span class="prose-body line-clamp-3 text-[11px] text-muted">{revision.preview}</span>
                      ) : (
                        <details>
                          <summary class="cursor-pointer text-[11px] text-muted">看这一版的开头</summary>
                          <div class="prose-body mt-1 max-h-40 overflow-y-auto rounded-lg bg-raised p-2 text-[11px]">
                            {revision.preview || '（空）'}
                          </div>
                        </details>
                      )}
                    </div>
                  )}
                />
              </Panel>

              <Panel title={`她的提案（${work.proposals.length}）`} icon="link">
                <Stack>
                  <Note>{USER_ONLY_NOTE}</Note>
                  {work.proposals.length === 0 ? (
                    <Empty text="她还没提过修改。聊到这件作品时，她会把想改的地方提出来。" />
                  ) : (
                    <LongList
                      items={work.proposals}
                      limit={4}
                      unit="条"
                      class="flex flex-col gap-2"
                      render={(proposal) => (
                        <div class="flex flex-col gap-2 rounded-xl border border-line p-3">
                          <div class="flex flex-wrap items-center gap-2">
                            <Badge tone={proposalStatusTone(proposal.status)}>
                              {proposalStatusLabel(proposal.status)}
                            </Badge>
                            <Badge tone={authorTone(proposal.author)}>{authorLabel(proposal.author)}</Badge>
                            <span class="text-[11px] text-muted">
                              基于 #{proposal.base_revision || '?'} · {proposal.created_at || '时间未知'} · {proposal.content_chars} 字
                            </span>
                          </div>
                          <span class="text-xs">理由：{proposal.reason || '（没写理由）'}</span>
                          <div class="prose-body max-h-48 overflow-y-auto rounded-lg border border-line bg-raised p-3 font-serif text-[11px]">
                            {proposal.content || '（空的提案）'}
                          </div>
                          {proposal.pending ? (
                            <div class="flex flex-wrap items-center gap-2">
                              <ConfirmButton
                                label={busy === `接受-${proposal.id}` ? '处理中…' : '接受（变成新版本）'}
                                confirmLabel="确认接受：正文会换成这一版"
                                warning="接受后正文立刻变成提案里那一版（成为一条新版本，旧版本仍然留在时间线里）。"
                                variant="primary"
                                icon="tick"
                                disabled={Boolean(busy)}
                                onConfirm={() => resolve(proposal, true)}
                              />
                              <ConfirmButton
                                label={busy === `驳回-${proposal.id}` ? '处理中…' : '驳回（正文不动）'}
                                confirmLabel="确认驳回这条提案"
                                warning="驳回只留一条结论，正文一个字都不动；她之后还能再提。"
                                variant="danger"
                                icon="close"
                                disabled={Boolean(busy)}
                                onConfirm={() => resolve(proposal, false)}
                              />
                              <span class="text-[11px] text-muted">她不会自己决定：只有你能按这两个键。</span>
                            </div>
                          ) : (
                            <span class="text-[11px] text-muted">结论已定，不能再改。</span>
                          )}
                        </div>
                      )}
                    />
                  )}
                </Stack>
              </Panel>

              <Panel title={`写手任务（${work.job_count}）`} icon="clock">
                <Stack>
                  {work.last_failure ? (
                    <Note tone="warn">
                      上一次提案没保存成功（剧本条目 #{work.last_failure.sourceEntryId ?? '?'}，
                      {work.last_failure.at || '时间未知'}）：她说想改，但那条没落库。
                      已经过去的那次不用管，聊到作品时她会重新提。
                    </Note>
                  ) : null}
                  {work.jobs.length > 0 && work.jobs_running > 0 ? (
                    <span class="text-[11px] text-muted">
                      任务在跑时她不会接新的起草请求；「中断」是插件重启过、结果永远不会回来，
                      可以直接取消（取消后就能让她重新写）。
                    </span>
                  ) : null}
                  {work.jobs.length === 0 ? (
                    <Empty text="还没有起草任务。" />
                  ) : (
                    <Table
                      columns={[
                        {
                          key: 'status',
                          title: '状态',
                          width: '14rem',
                          render: (job) => (
                            <Badge tone={jobStatusTone(job.status)}>{jobStatusLabel(job.status)}</Badge>
                          ),
                        },
                        {
                          key: 'brief',
                          title: '创作意图',
                          render: (job) => (
                            <span class="prose-body text-[11px] text-muted">{job.brief || '（没写意图）'}</span>
                          ),
                        },
                        {
                          key: 'model',
                          title: '模型',
                          width: '10rem',
                          render: (job) => <span class="font-mono text-[11px] text-muted">{job.model_id || '默认'}</span>,
                        },
                        {
                          key: 'at',
                          title: '时间',
                          width: '14rem',
                          render: (job) => <span class="text-[11px] text-muted">{job.created_at || '—'}</span>,
                        },
                        {
                          key: 'actions',
                          title: '操作',
                          width: '8rem',
                          render: (job) => (
                            job.status === 'running' || job.status === 'interrupted' ? (
                              <Button
                                variant="danger"
                                icon="close"
                                disabled={Boolean(busy)}
                                onClick={() => cancelJob(job)}
                              >
                                {busy === `cancel-${job.id}` ? '取消中…' : '取消'}
                              </Button>
                            ) : (
                              <span class="text-[11px] text-muted">已结束</span>
                            )
                          ),
                        },
                      ]}
                      rows={work.jobs}
                      empty="还没有起草任务"
                      rowKey={(job) => job.id}
                      maxRows={6}
                    />
                  )}
                </Stack>
              </Panel>

              <Panel title="让她起草" icon="fire">
                <Stack>
                  {(() => {
                    const blocker = draftBlocker({
                      available: work.available,
                      enabled: work.enabled,
                      hasWork: true,
                      mayPropose: work.may_propose,
                      reason: work.may_propose_reason,
                      hint: work.hint,
                    })
                    if (blocker) {
                      return (
                        <Note tone={work.enabled ? 'neutral' : 'warn'}>
                          现在不能起草：{blocker}
                        </Note>
                      )
                    }
                    return (
                      <>
                        <span class="text-[11px] text-muted">
                          给她一句创作意图（最多 {work.limits.brief} 字），她会在后台单独写一版；
                          写完的结果是一条<span class="text-fg">待决提案</span>，只有你能接受。
                        </span>
                        <Textarea
                          value={brief}
                          rows={3}
                          onInput={setBrief}
                          placeholder="例如：把结尾改成她第二天早上才读到那封信…"
                        />
                        <div class="flex items-center gap-2">
                          <Button
                            variant="primary"
                            icon="star"
                            disabled={Boolean(busy) || !brief.trim()}
                            onClick={draftByHer}
                          >
                            {busy === 'generate' ? '已经交给她了…' : '让她起草'}
                          </Button>
                          <span class="text-[11px] text-muted">{brief.length} / {work.limits.brief} 字</span>
                        </div>
                      </>
                    )
                  })()}
                </Stack>
              </Panel>
            </Stack>
          )}
        </div>
      )}
    </Stack>
  )
}

/** 清单里的一行：参与者 + 标题 + 待决徽章（点一下看详情）。 */
function WorkListItem({ row, active, onPick }: { row: WorkListRow; active: boolean; onPick: () => void }) {
  return (
    <button
      type="button"
      onClick={onPick}
      class={`flex w-full flex-col gap-1 rounded-lg border px-3 py-2 text-left text-xs transition ${
        active ? 'border-accent bg-accent/10' : 'border-line hover:bg-raised'
      }`}
    >
      <span class="flex items-center gap-2">
        <span class="min-w-0 flex-1 truncate font-medium">{row.participant || row.participant_id}</span>
        {row.pending_count > 0 ? <Badge tone="warn">待决 {row.pending_count}</Badge> : null}
        {row.jobs_running > 0 ? <Badge tone="accent">在写</Badge> : null}
      </span>
      <span class="truncate text-[11px] text-muted">{row.title || '（没有标题）'}</span>
      <span class="flex items-center gap-2 text-[11px] text-muted">
        <span>#{row.revision} / {row.revision_count} 版</span>
        {row.broken ? <Badge tone="danger">数据形状不识别</Badge> : null}
        <span class="ml-auto truncate">{row.updated_at || '—'}</span>
      </span>
    </button>
  )
}
