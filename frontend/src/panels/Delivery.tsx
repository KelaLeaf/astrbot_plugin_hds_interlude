import { useState } from 'preact/hooks'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { DeliveryPayload } from '../types'
import { Badge, Empty, ErrorNote, Grid, Icon, Loading, Panel, Stack, Stat } from '../components/ui'

const TONE: Record<string, 'ok' | 'warn' | 'danger' | 'neutral' | 'accent'> = {
  delivered: 'ok',
  partial: 'warn',
  failed: 'danger',
  cancelled: 'neutral',
  pending: 'accent',
}

const SEGMENT_LABEL: Record<string, string> = {
  text: '正文', image: '图片', face: '原生表情', reaction: '表态',
  record: '语音', sticker: '贴图', file: '文件', reply: '引用',
}

const FILTERS: Array<[string, string]> = [
  ['', '全部'],
  ['delivered', '已投递'],
  ['partial', '部分'],
  ['failed', '失败'],
  ['pending', '待投递'],
  ['cancelled', '已取消'],
]

const STEPS = ['pending', 'delivered'] as const

export function Delivery({ storyId, refreshKey }: PanelProps) {
  const [status, setStatus] = useState('')
  const [open, setOpen] = useState('')
  const { data, error, loading, reload } = useQuery<DeliveryPayload>(
    'console/delivery',
    { story_id: storyId, limit: 300, status },
    { nonce: refreshKey },
  )

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />
  if (!data) return <Empty text="没有拿到数据" />
  if (!data.story) return <Empty text="还没有任何剧本。" icon="link" />

  const totals = data.totals ?? {}
  const all = Object.values(totals).reduce((sum, value) => sum + value, 0)

  return (
    <Stack>
      <Grid cols={4}>
        <Stat label="行动总数" value={all} hint={`扫描 ${data.scanned} 条剧本条目`} />
        <Stat label="已投递" value={totals.delivered ?? 0} tone={(totals.delivered ?? 0) ? 'ok' : 'neutral'} />
        <Stat label="部分 / 失败" value={(totals.partial ?? 0) + (totals.failed ?? 0)} tone={totals.failed ? 'danger' : 'neutral'} />
        <Stat label="待投递" value={totals.pending ?? 0} />
      </Grid>

      <Panel
        title="投递账本"
        icon="link"
        actions={
          <>
            {FILTERS.map(([key, label]) => (
              <button
                key={key || 'all'}
                type="button"
                onClick={() => setStatus(key)}
                class={`rounded-md border px-2 py-1 text-[11px] ${
                  status === key ? 'border-accent text-accent' : 'border-line text-muted hover:text-fg'
                }`}
              >
                {label}
                {key && totals[key] ? ` ${totals[key]}` : ''}
              </button>
            ))}
          </>
        }
      >
        {data.actions.length === 0 ? (
          <Empty text="没有符合条件的投递记录。" icon="link" />
        ) : (
          <div class="flex flex-col gap-2">
            {data.actions.map((action) => {
              const key = `${action.entry_id}-${action.commit_id}-${action.event_id}`
              const expanded = open === key
              return (
                <article key={key} class="rounded-lg border border-line px-3 py-2">
                  <header
                    class="flex cursor-pointer flex-wrap items-center gap-2 text-[11px] text-muted"
                    onClick={() => setOpen(expanded ? '' : key)}
                  >
                    <Badge tone={TONE[action.status] ?? 'neutral'}>{action.status || '未知'}</Badge>
                    <span class="font-mono">#{action.entry_id}</span>
                    <span>{action.occurred_at}</span>
                    {action.target && <Badge>{action.target}</Badge>}
                    <span class="ml-auto flex items-center gap-2">
                      {action.done}/{action.segments.length} 分段
                      {action.attempts > 1 && <Badge tone="warn">重试 {action.attempts}</Badge>}
                      <Icon name={expanded ? 'close' : 'search'} class="h-3 w-3" />
                    </span>
                  </header>

                  {/* 进度轨迹：pending → delivered，失败/部分直接标出来 */}
                  <div class="mt-2 flex items-center gap-1">
                    {STEPS.map((step) => (
                      <span
                        key={step}
                        class={`h-1.5 flex-1 rounded-full ${
                          action.status === 'failed'
                            ? 'bg-danger/60'
                            : action.status === 'partial'
                              ? 'bg-warn/60'
                              : action.status === step || (step === 'pending' && action.status !== 'pending')
                                ? 'bg-ok/70'
                                : 'bg-line'
                        }`}
                      />
                    ))}
                  </div>

                  {action.commit_id && (
                    <div class="mt-1 font-mono text-[11px] text-muted">
                      commit {action.commit_id.slice(0, 12)} · event {action.event_id.slice(0, 12) || '—'}
                    </div>
                  )}

                  {expanded && (
                    <div class="mt-2 flex flex-col gap-1">
                      {action.segments.length === 0 ? (
                        <span class="text-[11px] text-muted">这条行动没有分段记录。</span>
                      ) : (
                        action.segments.map((segment) => (
                          <div
                            key={segment.index}
                            class="flex flex-wrap items-center gap-2 rounded border border-line/60 px-2 py-1 text-[11px]"
                          >
                            <span class="font-mono text-muted">#{segment.index}</span>
                            <Badge>{SEGMENT_LABEL[segment.kind] ?? (segment.kind || '—')}</Badge>
                            <Badge tone={TONE[segment.status] ?? 'neutral'}>{segment.status || '—'}</Badge>
                            {segment.attempts > 1 && <span class="text-muted">尝试 {segment.attempts}</span>}
                            {segment.error && <span class="text-danger">{segment.error}</span>}
                          </div>
                        ))
                      )}
                      {action.updated_at && (
                        <span class="text-[11px] text-muted">最后更新 {action.updated_at}</span>
                      )}
                    </div>
                  )}
                </article>
              )
            })}
          </div>
        )}
      </Panel>

      <p class="flex items-start gap-1 text-[11px] text-muted">
        <Icon name="info" class="mt-0.5 h-3 w-3 shrink-0" />
        账本是只增不减的：已 delivered 是终态，晚到的平台回执不会把它降级。点一条可以展开看每个分段的
        结果——"消息发了但对方没收到"这类问题就看这里。
      </p>
    </Stack>
  )
}
