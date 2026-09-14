import { useState } from 'preact/hooks'
import { useInterval, useQuery } from '../query'
import type { PanelProps } from '../main'
import type { LogPayload } from '../types'
import { Badge, Button, Empty, ErrorNote, Grid, Loading, Panel, Stack, Stat } from '../components/ui'

const LEVELS: Array<[string, string]> = [
  ['', '全部'],
  ['error', '错误'],
  ['warn', '警告'],
  ['info', '信息'],
  ['debug', '调试'],
]

const TONE: Record<string, 'danger' | 'warn' | 'ok' | 'neutral'> = {
  error: 'danger',
  warn: 'warn',
  info: 'ok',
  debug: 'neutral',
}

export function Logs({ refreshKey }: PanelProps) {
  const [level, setLevel] = useState('')
  const [live, setLive] = useState(true)
  const [tick, setTick] = useState(0)
  const { data, error, loading, reload } = useQuery<LogPayload>(
    'console/logs',
    { limit: 300, level },
    { nonce: refreshKey + tick },
  )

  // 3 秒一次自动刷新；页面切到后台时 useInterval 会自己停。
  useInterval(() => setTick((value) => value + 1), live ? 3000 : null)

  if (error) return <ErrorNote text={error} onRetry={reload} />

  const records = data?.records ?? []
  const counts = records.reduce<Record<string, number>>((acc, item) => {
    acc[item.level] = (acc[item.level] ?? 0) + 1
    return acc
  }, {})

  return (
    <Stack>
      <Grid cols={4}>
        <Stat label="缓冲条数" value={`${data?.total ?? 0} / ${data?.capacity ?? 0}`} hint="内存环形缓冲，重启清空" />
        <Stat label="错误" value={counts.error ?? 0} tone={counts.error ? 'danger' : 'neutral'} />
        <Stat label="警告" value={counts.warn ?? 0} tone={counts.warn ? 'warn' : 'neutral'} />
        <Stat label="自动刷新" value={live ? '开启' : '关闭'} hint="每 3 秒，页面不可见时暂停" />
      </Grid>

      <Panel
        title="运行日志"
        icon="logs"
        actions={
          <>
            {LEVELS.map(([key, label]) => (
              <button
                key={key || 'all'}
                type="button"
                onClick={() => setLevel(key)}
                class={`rounded-md border px-2 py-1 text-[11px] ${
                  level === key ? 'border-accent text-accent' : 'border-line text-muted hover:text-fg'
                }`}
              >
                {label}
              </button>
            ))}
            <Button icon={live ? 'close' : 'refresh'} onClick={() => setLive(!live)}>
              {live ? '暂停' : '继续'}
            </Button>
          </>
        }
      >
        {loading && !data ? (
          <Loading />
        ) : records.length === 0 ? (
          <Empty text="缓冲里还没有日志。插件跑起来之后就会出现。" icon="logs" />
        ) : (
          <ol class="flex flex-col gap-1 font-mono text-[11px]">
            {records.map((record, index) => (
              <li key={index} class="flex gap-2 rounded border border-transparent px-2 py-1 hover:border-line">
                <span class="shrink-0 text-muted">{record.at.slice(11, 19)}</span>
                <span class="shrink-0">
                  <Badge tone={TONE[record.level] ?? 'neutral'}>{record.level}</Badge>
                </span>
                <span class="prose-body min-w-0 flex-1">{record.text}</span>
              </li>
            ))}
          </ol>
        )}
      </Panel>
    </Stack>
  )
}
