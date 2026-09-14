import { useState } from 'preact/hooks'
import { apiPost } from '../bridge'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { OverviewPayload } from '../types'
import { Badge, Empty, ErrorNote, Grid, Icon, KeyValue, Loading, Note, Panel, Stack, Stat, Switch, Table } from '../components/ui'

/** 开关的中文名 + 分组。key 必须与后端 `ConsoleApi.FLAG_KEYS` 一致。 */
const FLAGS: Array<{ key: string; label: string; note: string }> = [
  { key: 'allow_proactive_messages', label: '主动可见消息', note: '关掉她就只能回应、不会主动找你' },
  { key: 'agency', label: 'Agency 行动窗口', note: '按日程/隐私/设备决定能不能联系' },
  { key: 'urge', label: 'Urge 弹性推进', note: '用真实消息热度决定下次推进时机' },
  { key: 'schedule_preplan', label: '日程预排', note: '后台维护近期日程结构' },
  { key: 'timeline_director', label: '时间导演', note: '自动回合的相对时间账本' },
  { key: 'compaction', label: '后台压缩', note: '场景摘要与事实提取' },
  { key: 'embedding', label: '语义检索', note: '长期事实的向量召回' },
  { key: 'vision', label: '图片理解', note: '原图或侧端识图' },
  { key: 'audio', label: '语音理解', note: '语音转写或原生音频' },
  { key: 'browser', label: '网页观察', note: '模型提出浏览意图后取回文本' },
]

export function Overview({ storyId, onStoryChange, refreshKey }: PanelProps) {
  const { data, error, loading, reload } = useQuery<OverviewPayload>(
    'console/overview',
    { story_id: storyId },
    { nonce: refreshKey },
  )
  const [saving, setSaving] = useState('')
  const [failure, setFailure] = useState('')
  const [local, setLocal] = useState<Record<string, boolean>>({})

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />
  if (!data) return <Empty text="没有拿到数据" />

  const { plugin, service, story, stories, routing, capability } = data
  // 本地覆盖：点完开关立刻反映，后端确认后以服务端值为准
  const flags = { ...data.flags, ...local }
  const on = FLAGS.filter((item) => flags[item.key]).length

  async function toggle(key: string, next: boolean) {
    setLocal((prev) => ({ ...prev, [key]: next }))
    setSaving(key)
    setFailure('')
    try {
      const result = await apiPost<{ flags: Record<string, boolean> }>('console/flags', {
        name: key,
        value: next,
      })
      setLocal(result.flags ?? {})
      reload()
    } catch (problem) {
      // 失败就回滚，别让界面停在一个假的成功状态
      setLocal((prev) => ({ ...prev, [key]: !next }))
      setFailure(problem instanceof Error ? problem.message : String(problem))
    } finally {
      setSaving('')
    }
  }

  return (
    <Stack>
      {failure && <ErrorNote text={failure} onRetry={() => setFailure('')} />}
      {capability.image && <Note tone="warn">{capability.image}</Note>}
      {capability.audio && <Note tone="warn">{capability.audio}</Note>}

      <Grid cols={4}>
        <Stat label="插件版本" value={plugin.version} hint={`上游 ${plugin.upstream_version}`} />
        <Stat label="剧本数" value={service.story_count} hint={`条目 ${service.entry_count}`} />
        <Stat label="参与者" value={service.participant_count} hint={story ? story.character || '未命名主角' : '还没有剧本'} />
        <Stat label="已启用能力" value={`${on} / ${FLAGS.length}`} hint={service.started ? '服务已就绪' : '等待首次事件'} />
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
        <Panel title="运行开关" icon="config" actions={<span class="text-[11px] text-muted">点一下即刻生效</span>}>
          <ul class="flex flex-col divide-y divide-line/60">
            {FLAGS.map((item) => (
              <li key={item.key} class="flex items-center gap-3 py-2">
                <div class="min-w-0 flex-1">
                  <div class="text-xs font-medium">{item.label}</div>
                  <div class="truncate text-[11px] text-muted">{item.note}</div>
                </div>
                {saving === item.key && <Icon name="refresh" class="spin h-3.5 w-3.5 text-muted" />}
                <Switch
                  label={item.label}
                  checked={Boolean(flags[item.key])}
                  disabled={Boolean(saving)}
                  onChange={(next) => void toggle(item.key, next)}
                />
              </li>
            ))}
          </ul>
          <Note tone="warn">
            开关会写进插件配置并立即生效（不需要重启）。写入用的是跟配置导入同一条路径。
          </Note>
        </Panel>

        <Stack>
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
                  render: (row) =>
                    row.available ? (
                      <Icon name="tick" class="h-3.5 w-3.5 text-ok" />
                    ) : (
                      <Icon name="close" class="h-3.5 w-3.5 text-muted" />
                    ),
                },
              ]}
              rows={routing}
              rowKey={(row) => row.task}
            />
          </Panel>

          <Panel title="数据与能力" icon="database">
            <KeyValue
              rows={[
                ['数据目录', <span class="font-mono text-[11px]">{plugin.data_dir}</span>],
                ['配置文件', <span class="font-mono text-[11px]">{plugin.config_path}</span>],
                ['数据库行数', Object.values(data.counts).reduce((sum, value) => sum + value, 0)],
              ]}
            />
          </Panel>
        </Stack>
      </Grid>
    </Stack>
  )
}
