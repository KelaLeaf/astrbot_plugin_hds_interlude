/**
 * Token 统计：按模型 / 按任务看账，可按天、周、月或自选范围查看。
 *
 * 数据来自 `console/token-stats`（后端 `console_api.token_stats` → 纯函数
 * `core/token_stats.py`），账本落在 `interlude_token_usage` 表，**跨重启可查**。
 * 日期按服务器本地时间切分（页面上写明了这一点，避免跨时区看账时误判）。
 */
import { useState } from 'preact/hooks'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { TokenStatsPayload } from '../types'
import { Badge, Button, Empty, ErrorNote, Field, Grid, Input, Loading, Meter, Panel, Select, Stack, Stat, Table } from '../components/ui'

type RangeKey = 'day' | 'week' | 'month' | 'custom'

const RANGE_OPTIONS: Array<{ value: RangeKey; label: string }> = [
  { value: 'day', label: '今天' },
  { value: 'week', label: '最近 7 天' },
  { value: 'month', label: '最近 30 天' },
  { value: 'custom', label: '自选范围' },
]

function compact(value: number) {
  if (!value) return '0'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`
  if (value >= 10_000) return `${(value / 1000).toFixed(1)}K`
  return value.toLocaleString()
}

function percent(value: number) {
  return `${(value * 100).toFixed(1)}%`
}

/** 本地今天的 `YYYY-MM-DD`（自选范围的默认值）。 */
function today() {
  const now = new Date()
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 10)
}

export function TokenStats({ refreshKey }: PanelProps) {
  const [range, setRange] = useState<RangeKey>('week')
  const [from, setFrom] = useState(today())
  const [to, setTo] = useState(today())
  const { data, error, loading, reload } = useQuery<TokenStatsPayload>(
    'console/token-stats',
    range === 'custom' ? { range, from, to } : { range },
    { nonce: refreshKey },
  )

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />

  const totals = data?.totals
  const series = data?.series ?? []
  const peak = Math.max(1, ...series.map((item) => item.inputTokens + item.outputTokens))

  return (
    <Stack>
      <Panel title="统计范围" icon="logs">
        <div class="flex flex-wrap items-end gap-3">
          <Field label="范围">
            <Select
              value={range}
              onChange={(next) => setRange(String(next) as RangeKey)}
              options={RANGE_OPTIONS}
            />
          </Field>
          {range === 'custom' ? (
            <>
              <Field label="开始">
                <Input type="date" value={from} onInput={setFrom} />
              </Field>
              <Field label="结束">
                <Input type="date" value={to} onInput={setTo} />
              </Field>
            </>
          ) : null}
          <Button onClick={reload}>刷新</Button>
          {data ? (
            <span class="text-[11px] text-muted pb-2">
              {data.from} ~ {data.to} · 按服务器本地时间切分
            </span>
          ) : null}
        </div>
      </Panel>

      {!data || !totals ? (
        <Empty text="还没有拿到数据" />
      ) : (
        <>
          <Grid cols={4}>
            <Stat label="输入 tokens" value={compact(totals.inputTokens)} hint="含缓存命中部分" />
            <Stat label="输出 tokens" value={compact(totals.outputTokens)} />
            <Stat label="缓存命中率" value={percent(totals.hitRate)} hint={`缓存 ${compact(totals.cachedTokens)}`} />
            <Stat label="调用次数" value={totals.calls.toLocaleString()} hint="含压缩 / 识图 / 播种等侧端任务" />
          </Grid>

          <Panel title="按天" icon="script">
            <Table
              columns={[
                { key: 'day', title: '日期', width: '8rem', render: (row) => <span class="font-mono text-[11px]">{row.day}</span> },
                { key: 'input', title: '输入', width: '7rem', render: (row) => compact(row.inputTokens) },
                { key: 'output', title: '输出', width: '7rem', render: (row) => compact(row.outputTokens) },
                { key: 'cached', title: '缓存', width: '7rem', render: (row) => compact(row.cachedTokens) },
                { key: 'rate', title: '命中率', width: '6rem', render: (row) => percent(row.hitRate) },
                { key: 'calls', title: '次数', width: '5rem', render: (row) => row.calls },
                {
                  key: 'meter',
                  title: '相对占比',
                  render: (row) => <Meter value={row.inputTokens + row.outputTokens} max={peak} />,
                },
              ]}
              rows={series}
              empty="这个范围里没有调用"
              rowKey={(row) => row.day}
              maxRows={0}
            />
          </Panel>

          <Panel title="按模型" icon="models">
            <Table
              columns={[
                { key: 'model', title: '模型', render: (row) => <span class="font-mono text-[11px]">{row.model ?? '—'}</span> },
                { key: 'provider', title: '连接', width: '10rem', render: (row) => row.provider || '—' },
                { key: 'input', title: '输入', width: '7rem', render: (row) => compact(row.inputTokens) },
                { key: 'output', title: '输出', width: '7rem', render: (row) => compact(row.outputTokens) },
                { key: 'cached', title: '缓存', width: '7rem', render: (row) => compact(row.cachedTokens) },
                { key: 'rate', title: '命中率', width: '6rem', render: (row) => percent(row.hitRate) },
                { key: 'calls', title: '次数', width: '5rem', render: (row) => row.calls },
              ]}
              rows={data.byModel}
              empty="这个范围里没有调用"
              rowKey={(row) => `${row.model}|${row.provider}`}
              maxRows={0}
            />
          </Panel>

          <Panel title="按情况" icon="compass">
            <Table
              columns={[
                { key: 'task', title: '任务', render: (row) => row.task ?? '—' },
                { key: 'input', title: '输入', width: '7rem', render: (row) => compact(row.inputTokens) },
                { key: 'output', title: '输出', width: '7rem', render: (row) => compact(row.outputTokens) },
                { key: 'cached', title: '缓存', width: '7rem', render: (row) => compact(row.cachedTokens) },
                { key: 'rate', title: '命中率', width: '6rem', render: (row) => percent(row.hitRate) },
                { key: 'calls', title: '次数', width: '5rem', render: (row) => row.calls },
                {
                  key: 'share',
                  title: '占输入比例',
                  render: (row) => <Meter value={row.inputTokens} max={Math.max(1, totals.inputTokens)} />,
                },
              ]}
              rows={data.byTask}
              empty="这个范围里没有调用"
              rowKey={(row) => row.task ?? '—'}
              maxRows={0}
            />
          </Panel>

          <Panel title="说明" icon="info">
            <div class="text-xs text-muted leading-relaxed space-y-1">
              <p>
                <Badge>命中率</Badge> = 缓存输入 ÷ 总输入。网关不回缓存字段时这一列是 0，
                不代表没有命中缓存。
              </p>
            </div>
          </Panel>
        </>
      )}
    </Stack>
  )
}
