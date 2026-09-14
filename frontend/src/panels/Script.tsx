import { useState } from 'preact/hooks'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { ScriptPayload } from '../types'
import { Badge, Button, Empty, ErrorNote, Grid, Icon, Loading, Panel, Stack, Stat, Table } from '../components/ui'

const ACTOR_TONE: Record<string, 'accent' | 'ok' | 'neutral'> = {
  user: 'accent',
  character: 'ok',
  system: 'neutral',
}

export function Script({ storyId, onStoryChange, refreshKey }: PanelProps) {
  const [limit, setLimit] = useState(60)
  const [offset, setOffset] = useState(0)
  const { data, error, loading, reload } = useQuery<ScriptPayload>(
    'console/script',
    { story_id: storyId, limit, offset },
    { nonce: refreshKey },
  )

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />
  if (!data) return <Empty text="没有拿到数据" />
  if (!data.story) return <Empty text="还没有任何剧本。" icon="script" />

  const { story, entries, scenes, arcs, total } = data
  const pages = Math.max(1, Math.ceil(total / limit))
  const page = Math.floor(offset / limit) + 1

  return (
    <Stack>
      <Grid cols={3}>
        <Stat label="剧本" value={story.character || story.id.slice(0, 8)} hint={story.scene || '尚无当前场景'} />
        <Stat label="条目总数" value={total} hint={`本次显示 ${entries.length} 条`} />
        <Stat label="场景 / 弧" value={`${scenes.length} / ${arcs.length}`} hint={story.cursor_at || '—'} />
      </Grid>

      <Panel
        title="剧本条目"
        icon="script"
        actions={
          <>
            <Button icon="refresh" onClick={reload}>刷新</Button>
            <Button disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - limit))}>上一页</Button>
            <span class="text-[11px] text-muted">{page} / {pages}</span>
            <Button disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>下一页</Button>
            <Button
              onClick={() => {
                setLimit(limit === 60 ? 200 : 60)
                setOffset(0)
              }}
            >
              {limit === 60 ? '显示 200' : '显示 60'}
            </Button>
          </>
        }
      >
        {entries.length === 0 ? (
          <Empty text="还没有剧本条目。" icon="script" />
        ) : (
          <div class="flex flex-col gap-2">
            {entries.map((entry) => (
              <article key={entry.id} class="rounded-lg border border-line px-3 py-2">
                <header class="flex flex-wrap items-center gap-2 text-[11px] text-muted">
                  <Badge tone={ACTOR_TONE[entry.actor] ?? 'neutral'}>{entry.actor || '未知'}</Badge>
                  <span>{entry.kind || 'entry'}</span>
                  <span class="font-mono">#{entry.id}</span>
                  <span>{entry.occurred_at}</span>
                  {entry.commit_id && <span class="font-mono">commit {entry.commit_id.slice(0, 8)}</span>}
                </header>
                <p class="prose-body mt-2 text-xs">{entry.content}{entry.truncated ? ' …' : ''}</p>
              </article>
            ))}
          </div>
        )}
      </Panel>

      <Grid cols={2}>
        <Panel title="场景" icon="compass">
          <Table
            columns={[
              { key: 'status', title: '状态', render: (row) => <Badge tone={row.status === 'active' ? 'ok' : 'neutral'}>{row.status || '—'}</Badge> },
              { key: 'hook', title: '地点 / 引子', render: (row) => row.hook || '—' },
              { key: 'entries', title: '条目', width: '4rem', render: (row) => row.entry_count },
              { key: 'started', title: '开始', mono: true, render: (row) => row.started_at || '—' },
            ]}
            rows={scenes}
            empty="还没有场景记录"
            rowKey={(row) => String(row.id)}
          />
        </Panel>
        <Panel title="弧" icon="star">
          <Table
            columns={[
              { key: 'status', title: '状态', render: (row) => <Badge>{row.status || '—'}</Badge> },
              { key: 'title', title: '标题', render: (row) => row.title || '—' },
              { key: 'scenes', title: '场景', width: '4rem', render: (row) => row.scene_count },
              { key: 'summary', title: '摘要', render: (row) => row.summary || '—' },
            ]}
            rows={arcs}
            empty="还没有弧记录"
            rowKey={(row) => String(row.id)}
          />
        </Panel>
      </Grid>

      <p class="flex items-center gap-1 text-[11px] text-muted">
        <Icon name="info" class="h-3 w-3" />
        条目按故事时间倒序显示。改看别的剧本：
        <button type="button" class="text-accent" onClick={() => onStoryChange('')}>回到最近更新的那个</button>
      </p>
    </Stack>
  )
}
