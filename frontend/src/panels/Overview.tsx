import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { OverviewPayload } from '../types'
import { Badge, Empty, ErrorNote, Grid, Icon, KeyValue, Loading, Note, Panel, Stack, Stat, Table } from '../components/ui'

const FLAG_LABELS: Array<[string, string]> = [
  ['allow_proactive_messages', '主动可见消息'],
  ['agency', 'Agency 行动窗口'],
  ['urge', 'Urge 弹性推进'],
  ['schedule_preplan', '日程预排'],
  ['timeline_director', '时间导演'],
  ['compaction', '后台压缩'],
  ['embedding', '语义检索'],
  ['vision', '图片理解'],
  ['audio', '语音理解'],
  ['browser', '网页观察'],
  ['blind_mode', '盲区模式'],
]

export function Overview({ storyId, onStoryChange, refreshKey }: PanelProps) {
  const { data, error, loading, reload } = useQuery<OverviewPayload>(
    'console/overview',
    { story_id: storyId },
    { nonce: refreshKey },
  )

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />
  if (!data) return <Empty text="没有拿到数据" />

  const { plugin, service, story, stories, flags, routing, capability } = data
  const on = FLAG_LABELS.filter(([key]) => flags[key]).length

  return (
    <Stack>
      {capability.image && <Note tone="warn">{capability.image}</Note>}
      {capability.audio && <Note tone="warn">{capability.audio}</Note>}

      <Grid cols={4}>
        <Stat label="插件版本" value={plugin.version} hint={`上游 ${plugin.upstream_version}`} />
        <Stat label="剧本数" value={service.story_count} hint={`条目 ${service.entry_count}`} />
        <Stat label="参与者" value={service.participant_count} hint={story ? story.character || '未命名主角' : '还没有剧本'} />
        <Stat label="已启用能力" value={`${on} / ${FLAG_LABELS.length}`} hint={service.started ? '服务已就绪' : '服务未启动'} />
      </Grid>

      <Panel title="当前剧本" icon="script">
        {!story ? (
          <Empty text="还没有任何剧本。先在聊天里发一条消息，插件会自动建剧本。" icon="script" />
        ) : (
          <div class="flex flex-col gap-3">
            <KeyValue
              rows={[
                ['故事 ID', <span class="font-mono text-[11px]">{story.id}</span>],
                ['主角', story.character || '—'],
                ['状态', <Badge tone={story.status === 'active' ? 'ok' : 'warn'}>{story.status || '未知'}</Badge>],
                ['平台', story.platform || '—'],
                ['时区', story.timezone || '（未设置）'],
                ['故事时间', story.cursor_at || '—'],
                ['当前场景', story.scene || '（尚未建立）'],
              ]}
            />
            {stories.length > 1 && (
              <div class="flex flex-wrap gap-1">
                {stories.slice(0, 12).map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => onStoryChange(item.id)}
                    class={`rounded-md border px-2 py-1 text-[11px] ${
                      item.id === story.id ? 'border-accent text-accent' : 'border-line text-muted hover:text-fg'
                    }`}
                  >
                    {item.character || item.id.slice(0, 8)}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </Panel>

      <Grid cols={2}>
        <Panel title="模型路由" icon="models">
          <Table
            columns={[
              { key: 'task', title: '任务', render: (row) => row.label },
              {
                key: 'source',
                title: '来源',
                render: (row) =>
                  row.source === 'astrbot' ? (
                    <Badge tone="accent">AstrBot</Badge>
                  ) : row.source === 'connection' ? (
                    <Badge>连接行</Badge>
                  ) : (
                    <Badge tone="warn">未配置</Badge>
                  ),
              },
              { key: 'model', title: '模型', render: (row) => row.model || '—' },
              {
                key: 'available',
                title: '可用',
                render: (row) => (row.available ? <Icon name="tick" class="h-3.5 w-3.5 text-ok" /> : <Icon name="close" class="h-3.5 w-3.5 text-muted" />),
              },
            ]}
            rows={routing}
            rowKey={(row) => row.task}
          />
        </Panel>

        <Panel title="数据与能力" icon="database">
          <div class="flex flex-col gap-3">
            <div class="flex flex-wrap gap-1">
              {FLAG_LABELS.map(([key, label]) => (
                <Badge key={key} tone={flags[key] ? 'ok' : 'neutral'}>
                  {label}
                </Badge>
              ))}
            </div>
            <KeyValue
              rows={[
                ['数据目录', <span class="font-mono text-[11px]">{plugin.data_dir}</span>],
                ['配置文件', <span class="font-mono text-[11px]">{plugin.config_path}</span>],
                ['数据库行数', Object.values(data.counts).reduce((sum, value) => sum + value, 0)],
              ]}
            />
          </div>
        </Panel>
      </Grid>
    </Stack>
  )
}
