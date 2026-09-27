import { useState } from 'preact/hooks'
import { apiPost } from '../bridge'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { MemoryPayload } from '../types'
import { Badge, Button, Empty, ErrorNote, Grid, Loading, LongList, Meter, Note, Panel, Stack, Stat, Table } from '../components/ui'

const KNOWLEDGE_LABEL: Record<string, string> = {
  observation: '观察',
  hearsay: '转述',
  thought: '想法',
  proposal: '提议',
  condition: '条件',
  confirmation: '确认',
}

export function Memory({ storyId, refreshKey }: PanelProps) {
  const [showInternal, setShowInternal] = useState(false)
  const [deciding, setDeciding] = useState(0)
  const [decideError, setDecideError] = useState('')
  const [note, setNote] = useState('')
  const [localKey, setLocalKey] = useState(0)
  const { data, error, loading, reload } = useQuery<MemoryPayload>(
    'console/memory',
    { story_id: storyId },
    { nonce: refreshKey + localKey },
  )

  // 审批 / 回滚：写操作回整页数据，所以这里不用再拉一次，只把本地 nonce 顶一下。
  async function decidePatch(patchId: number, action: 'approve' | 'reject' | 'rollback') {
    setDeciding(patchId)
    setDecideError('')
    try {
      const endpoint = action === 'rollback' ? 'console/patch-rollback' : 'console/patch-decide'
      await apiPost(endpoint, { story_id: storyId, patch_id: patchId, action, note })
      setNote('')
      setLocalKey((value) => value + 1)
    } catch (failure) {
      setDecideError(failure instanceof Error ? failure.message : String(failure))
    } finally {
      setDeciding(0)
    }
  }

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />
  if (!data) return <Empty text="没有拿到数据" />
  if (!data.story) return <Empty text="还没有任何剧本。" icon="memory" />

  const { facts, memories, intents, patches, overlays, participants } = data
  // split-message / narrative-retry 是宿主自己的调度账（气泡节拍、失败重试），
  // 不是用户眼里的承诺——默认折叠，点一下才展开。
  const visibleIntents = intents.filter((item) => !item.internal)
  const internalIntents = intents.filter((item) => item.internal)
  const unresolved = facts.filter((item) => item.unresolved).length
  const pending = patches.filter((item) => item.status !== 'applied').length

  return (
    <Stack>
      <Grid cols={4}>
        <Stat label="长期事实" value={facts.length} hint={unresolved ? `其中 ${unresolved} 条未解决` : '均已解决'} />
        <Stat label="记忆条目" value={memories.length} />
        <Stat
          label="承诺 / 意图"
          value={visibleIntents.length}
          hint={visibleIntents.filter((item) => item.status === 'pending').length + ' 条待履行'}
        />
        <Stat label="设定演化候选" value={patches.length} hint={pending ? `${pending} 条待生效` : '无待处理'} tone={pending ? 'warn' : 'neutral'} />
      </Grid>

      <Panel title="长期事实" icon="memory">
        {facts.length === 0 ? (
          <Empty text="还没有提取到长期事实。压缩跑过之后才会出现。" icon="memory" />
        ) : (
          <LongList
            items={facts}
            limit={5}
            render={(fact) => (
              <article key={fact.id} class="rounded-lg border border-line px-3 py-2">
                <header class="flex flex-wrap items-center gap-2 text-[11px] text-muted">
                  <Badge tone="accent">{KNOWLEDGE_LABEL[fact.knowledge_kind] ?? fact.knowledge_kind ?? '事实'}</Badge>
                  <Badge>{fact.scope || 'scope?'}</Badge>
                  {fact.unresolved && <Badge tone="warn">未解决</Badge>}
                  <span class="ml-auto flex items-center gap-2">
                    重要度 <Meter value={fact.importance} />
                    置信度 <Meter value={fact.confidence} tone={fact.confidence >= 0.7 ? 'ok' : 'warn'} />
                  </span>
                </header>
                <p class="prose-body mt-2 text-xs">{fact.content}</p>
                {fact.quote && (
                  <p class="mt-1 border-l-2 border-line pl-2 text-[11px] italic text-muted">原文：{fact.quote}</p>
                )}
              </article>
            )}
          />
        )}
      </Panel>

      <Grid cols={2}>
        <Panel
          title="承诺与意图"
          icon="clock"
          actions={
            internalIntents.length > 0 ? (
              <label class="flex cursor-pointer items-center gap-1 text-[11px] text-muted">
                <input
                  type="checkbox"
                  checked={showInternal}
                  onChange={(event) => setShowInternal((event.target as HTMLInputElement).checked)}
                />
                显示内部调度（{internalIntents.length}）
              </label>
            ) : null
          }
        >
          <Table
            columns={[
              { key: 'type', title: '类型', render: (row) => <Badge>{row.type || '—'}</Badge> },
              { key: 'summary', title: '内容', render: (row) => row.summary || '—' },
              {
                key: 'status',
                title: '状态',
                render: (row) => <Badge tone={row.status === 'pending' ? 'warn' : 'ok'}>{row.status || '—'}</Badge>,
              },
              { key: 'not_before', title: '到期', mono: true, render: (row) => row.not_before || '—' },
            ]}
            rows={showInternal ? intents : visibleIntents}
            empty={showInternal ? '没有任何意图记录' : '没有待履行的承诺'}
            rowKey={(row) => String(row.id)}
          />
        </Panel>

        <Panel title="设定演化（Overlay）" icon="star">
          <Table
            columns={[
              { key: 'target', title: '对象', render: (row) => row.target || '—' },
              { key: 'tier', title: '层级', render: (row) => <Badge>{row.tier || '—'}</Badge> },
              { key: 'summary', title: '摘要', render: (row) => row.summary || '—' },
              { key: 'period_end', title: '截止', mono: true, render: (row) => row.period_end || '—' },
            ]}
            rows={overlays}
            empty="还没有 overlay 快照"
            rowKey={(row) => String(row.id)}
          />
        </Panel>
      </Grid>

      <Panel
        title="设定改写候选"
        icon="filter"
        actions={
          <input
            class="w-56 rounded-md border border-line bg-panel px-2 py-1 text-[11px]"
            placeholder="备注（可选，随裁决一起留档）"
            value={note}
            onInput={(event) => setNote((event.target as HTMLInputElement).value)}
          />
        }
      >
        {decideError && <ErrorNote text={decideError} onRetry={() => setDecideError('')} />}
        <Table
          columns={[
            { key: 'target', title: '对象', render: (row) => `${row.target || '?'} · ${row.path || '?'}` },
            { key: 'value', title: '建议值', render: (row) => row.proposed_value || '—' },
            { key: 'confidence', title: '置信度', render: (row) => <Meter value={row.confidence} /> },
            { key: 'impact', title: '影响', render: (row) => <Badge tone={row.impact === 'high' ? 'warn' : 'neutral'}>{row.impact || '—'}</Badge> },
            { key: 'status', title: '状态', render: (row) => <PatchStatus row={row} /> },
            { key: 'created', title: '提出时间', mono: true, render: (row) => row.created_at || '—' },
            {
              key: 'act',
              title: '操作',
              render: (row) => (
                <span class="flex items-center gap-1">
                  {row.status === 'proposed' && (
                    <Button icon="tick" disabled={deciding === row.id} onClick={() => decidePatch(row.id, 'approve')}>
                      采用
                    </Button>
                  )}
                  {(row.status === 'proposed' || row.status === 'applied') && (
                    <Button
                      icon="close"
                      disabled={deciding === row.id}
                      onClick={() => decidePatch(row.id, row.status === 'applied' ? 'rollback' : 'reject')}
                    >
                      {row.status === 'applied' ? '回滚' : '驳回'}
                    </Button>
                  )}
                  {row.status === 'compacted' && <span class="text-[11px] text-muted">已并入摘要</span>}
                </span>
              ),
            },
          ]}
          rows={patches}
          empty="没有待处理的设定改写"
          rowKey={(row) => String(row.id)}
        />
      </Panel>

      <Grid cols={2}>
        <Panel title="记忆条目（压缩产物）" icon="logs">
          <Table
            columns={[
              { key: 'category', title: '类别', render: (row) => <Badge>{row.category || '—'}</Badge> },
              { key: 'content', title: '内容', render: (row) => row.content || '—' },
              { key: 'importance', title: '重要度', render: (row) => <Meter value={row.importance} /> },
            ]}
            rows={memories}
            empty="还没有压缩出来的记忆"
            rowKey={(row) => String(row.id)}
          />
        </Panel>

        <Panel title="参与者" icon="user">
          <Table
            columns={[
              { key: 'name', title: '名称', render: (row) => row.display_name || row.id.slice(0, 8) },
              { key: 'platform', title: '平台', render: (row) => row.platform || '—' },
              {
                key: 'status',
                title: '状态',
                render: (row) => <Badge tone={row.status === 'active' ? 'ok' : 'neutral'}>{row.status || '—'}</Badge>,
              },
              { key: 'relationship', title: '关系', render: (row) => row.relationship || '—' },
            ]}
            rows={participants}
            empty="没有参与者"
            rowKey={(row) => row.id}
          />
        </Panel>
      </Grid>

      <Note>
        这里显示的是已经落库的记忆。压缩是后台增量的，刚聊完的内容要等一轮压缩才会出现在上面。
      </Note>
    </Stack>
  )
}


const PATCH_TONE: Record<string, 'ok' | 'warn' | 'danger' | 'neutral'> = {
  proposed: 'warn',
  applied: 'ok',
  rejected: 'neutral',
  'rolled-back': 'danger',
  compacted: 'neutral',
}

/** 设定候选的状态：把裁决留痕挂在 title 上，界面不额外占地方。 */
function PatchStatus({ row }: { row: { status: string; decided_at?: string; decision_note?: string } }) {
  const label = row.status === 'rolled-back' ? '已回滚' : row.status || '—'
  const trail = row.decided_at ? `裁决于 ${row.decided_at}${row.decision_note ? `：${row.decision_note}` : ''}` : ''
  return <Badge tone={PATCH_TONE[row.status] ?? 'neutral'} title={trail || undefined}>{label}</Badge>
}
