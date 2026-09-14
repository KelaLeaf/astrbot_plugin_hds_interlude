import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { AlterPayload } from '../types'
import { Badge, Empty, ErrorNote, Grid, Icon, KeyValue, Loading, Meter, Note, Panel, Stack, Stat, Table } from '../components/ui'

/** 极简折线：只画一条序列，不引图表库（省下 40–70KB）。 */
function Sparkline({
  values,
  height = 120,
  zero = true,
  tone = 'var(--app-accent)',
}: {
  values: number[]
  height?: number
  zero?: boolean
  tone?: string
}) {
  if (values.length < 2) return <Empty text="历史还不够画曲线（至少两轮）" icon="star" />
  const min = Math.min(...values, zero ? 0 : Math.min(...values))
  const max = Math.max(...values, zero ? 0 : Math.max(...values))
  const span = max - min || 1
  const width = 600
  const step = width / (values.length - 1)
  const y = (value: number) => height - ((value - min) / span) * (height - 12) - 6
  const points = values.map((value, index) => `${(index * step).toFixed(1)},${y(value).toFixed(1)}`).join(' ')
  const zeroLine = zero && min <= 0 && max >= 0 ? y(0) : null
  return (
    <svg viewBox={`0 0 ${width} ${height}`} class="h-32 w-full" preserveAspectRatio="none">
      {zeroLine !== null && (
        <line x1="0" y1={zeroLine} x2={width} y2={zeroLine} stroke="var(--app-line)" stroke-dasharray="4 4" />
      )}
      <polyline points={points} fill="none" stroke={tone} stroke-width="2" vector-effect="non-scaling-stroke" />
    </svg>
  )
}

const DIRECTION: Record<number, { label: string; tone: 'ok' | 'warn' | 'neutral' }> = {
  1: { label: '偏 relaxed', tone: 'ok' },
  [-1]: { label: '偏 serious', tone: 'warn' },
  0: { label: '无位移', tone: 'neutral' },
}

export function Alter({ storyId, refreshKey }: PanelProps) {
  const { data, error, loading, reload } = useQuery<AlterPayload>(
    'console/alter',
    { story_id: storyId },
    { nonce: refreshKey },
  )

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />
  if (!data) return <Empty text="没有拿到数据" />
  if (!data.story) return <Empty text="还没有任何剧本。" icon="star" />

  const { state, history, pending, config } = data
  const offset = state?.offset ?? null
  const direction = DIRECTION[state?.direction ?? 0] ?? DIRECTION[0]
  const curve = history.map((item) => item.alter_value)
  const deltas = history.map((item) => item.alter)

  return (
    <Stack>
      {!state && (
        <Note>
          <Icon name="info" class="mr-1 inline h-3 w-3" />
          这份剧本里还没有 Alter 状态。它要等第一轮叙事跑过之后才会建立。
        </Note>
      )}

      <Grid cols={4}>
        <Stat
          label="氛围位移"
          value={(state?.value ?? 0).toFixed(2)}
          hint={`方向：${direction.label}`}
          tone={Math.abs(state?.value ?? 0) > 0.5 ? 'warn' : 'neutral'}
        />
        <Stat label="累积权重" value={(state?.weight ?? 0).toFixed(2)} hint="达到阈值才触发侧端分析" />
        <Stat label="待处理桶" value={pending.length} hint={pending.length ? '按关系分开累计' : '无'} />
        <Stat label="历史记录" value={history.length} hint={state?.updated_at || '—'} />
      </Grid>

      <Panel title="当前的内心天气" icon="star">
        {!offset ? (
          <Empty text="还没有生成情绪偏移——位移到阈值后才会出现一句描述。" icon="light" />
        ) : (
          <div class="flex flex-col gap-3">
            <div class="flex items-center gap-2">
              <Badge tone={offset.direction === 'relaxed' ? 'ok' : 'warn'}>
                {offset.direction === 'relaxed' ? '松弛' : '严肃'}
              </Badge>
              <Meter value={offset.intensity} />
              <span class="text-[11px] text-muted">{offset.generated_at}</span>
            </div>
            <p class="prose-body text-xs">{offset.description}</p>
          </div>
        )}
      </Panel>

      <Panel title="氛围位移曲线" icon="compass">
        <div class="flex flex-col gap-4">
          <div>
            <div class="mb-1 flex items-center justify-between text-[11px] text-muted">
              <span>累计位移（alter_value）</span>
              <span>
                {curve.length ? `${Math.min(...curve).toFixed(2)} → ${Math.max(...curve).toFixed(2)}` : '—'}
              </span>
            </div>
            <Sparkline values={curve} />
          </div>
          <div>
            <div class="mb-1 flex items-center justify-between text-[11px] text-muted">
              <span>单轮增量（alter）</span>
              <span>正=放松，负=紧绷</span>
            </div>
            <Sparkline values={deltas} tone="var(--app-warn)" />
          </div>
        </div>
      </Panel>

      <Grid cols={2}>
        <Panel title="按来源分桶" icon="user">
          <Table
            columns={[
              { key: 'who', title: '来源', render: (row) => row.participant_id || '主角自身 / 全局' },
              {
                key: 'value',
                title: '位移',
                render: (row) => (
                  <span class={row.value >= 0 ? 'text-ok' : 'text-warn'}>{row.value.toFixed(2)}</span>
                ),
              },
              { key: 'attempt', title: '上次分析', mono: true, render: (row) => row.last_attempt_at || '从未' },
            ]}
            rows={pending}
            empty="没有待处理的桶"
            rowKey={(row) => row.participant_id || 'global'}
          />
        </Panel>

        <Panel title="触发配置" icon="config">
          <KeyValue
            rows={[
              ['启用', config.enabled ? <Badge tone="ok">开</Badge> : <Badge>关</Badge>],
              ['触发阈值', String(config.threshold ?? '—')],
              ['强度上限', String(config.max_intensity ?? '—')],
              ['衰减', String(config.decay ?? '—')],
              ['冷却', `${config.cooldown_minutes ?? '—'} 分钟`],
              ['权重步长', String(config.weight_step ?? '—')],
            ]}
          />
        </Panel>
      </Grid>

      <Panel title="位移历史" icon="clock">
        <Table
          columns={[
            { key: 'turn', title: '轮次', width: '4rem', render: (row) => row.turn },
            { key: 'phase', title: '阶段', render: (row) => <Badge>{row.phase || '—'}</Badge> },
            {
              key: 'alter',
              title: '本论增量',
              render: (row) => <span class={row.alter >= 0 ? 'text-ok' : 'text-warn'}>{row.alter.toFixed(2)}</span>,
            },
            { key: 'value', title: '累计', render: (row) => row.alter_value.toFixed(2) },
            { key: 'who', title: '来源', render: (row) => row.participant_id || '全局' },
            { key: 'at', title: '时间', mono: true, render: (row) => row.timestamp },
          ]}
          rows={[...history].reverse()}
          empty="还没有历史记录"
        />
      </Panel>
    </Stack>
  )
}
