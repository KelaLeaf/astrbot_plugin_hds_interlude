import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { DatabasePayload } from '../types'
import { Badge, Empty, ErrorNote, Grid, Icon, KeyValue, Loading, Meter, Panel, Stack, Stat, Table } from '../components/ui'

function bytes(value: number) {
  if (!value) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let size = value
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

export function Database({ refreshKey }: PanelProps) {
  const { data, error, loading, reload } = useQuery<DatabasePayload>(
    'console/database',
    {},
    { nonce: refreshKey },
  )

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />
  if (!data) return <Empty text="没有拿到数据" />

  const max = Math.max(1, ...data.tables.map((table) => table.rows))

  return (
    <Stack>
      <Grid cols={3}>
        <Stat label="表数量" value={data.tables.length} hint="上游 13 张表，含增量补列" />
        <Stat label="总行数" value={data.total_rows.toLocaleString()} />
        <Stat label="数据库大小" value={bytes(data.size_bytes)} hint={data.path || '内存库'} />
      </Grid>

      <Panel title="各表行数" icon="database">
        <Table
          columns={[
            { key: 'name', title: '表', render: (row) => <span class="font-mono text-[11px]">{row.name}</span> },
            { key: 'rows', title: '行数', width: '6rem', render: (row) => row.rows.toLocaleString() },
            { key: 'columns', title: '列', width: '4rem', render: (row) => row.columns },
            {
              key: 'added',
              title: '来源',
              width: '7rem',
              render: (row) => (row.added_later ? <Badge tone="accent">移植版新增</Badge> : <Badge>上游</Badge>),
            },
            { key: 'meter', title: '相对占比', render: (row) => <Meter value={row.rows} max={max} /> },
          ]}
          rows={data.tables}
          empty="数据库里还没有表"
          rowKey={(row) => row.name}
        />
      </Panel>

      <Panel title="位置" icon="disk">
        <KeyValue
          rows={[
            ['数据库文件', <span class="font-mono text-[11px]">{data.path || '（内存库，未落盘）'}</span>],
            ['单位', 'MB'],
          ]}
        />
        <p class="mt-3 flex items-start gap-1 text-[11px] text-muted">
          <Icon name="warning" class="mt-0.5 h-3 w-3" />
          控制台只读。要清理数据请用管理命令（<code class="font-mono">hdsi_database_clear</code> /
          <code class="font-mono"> hdsi_purge_range</code>），它们带 y/n 确认，不会误触。
        </p>
      </Panel>
    </Stack>
  )
}
