import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { ModelsPayload } from '../types'
import { Badge, Empty, ErrorNote, Grid, Icon, KeyValue, Loading, Meter, Note, Panel, Stack, Stat, Table } from '../components/ui'

function fmt(value: number | null | undefined, unit = '') {
  return value === null || value === undefined ? '—' : `${value}${unit}`
}

export function Models({ storyId, refreshKey }: PanelProps) {
  const { data, error, loading, reload } = useQuery<ModelsPayload>(
    'console/models',
    { story_id: storyId },
    { nonce: refreshKey },
  )

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />
  if (!data) return <Empty text="没有拿到数据" />

  const { tasks, connections, astrbot_providers, main, embedding, vision, audio, failover, usage } = data
  const bound = Object.entries(data.task_models).filter(([, item]) => item.astrbot_provider)

  return (
    <Stack>
      {data.capability.image && <Note tone="warn">{data.capability.image}</Note>}
      {data.capability.audio && <Note tone="warn">{data.capability.audio}</Note>}

      <Grid cols={4}>
        <Stat label="主叙事连接" value={main.provider_label || '未指定'} hint={`${main.response_format || '默认输出格式'}`} />
        <Stat label="失败切换" value={failover.enabled ? `${failover.strategy} · ${failover.max_attempts} 次` : '已关闭'} hint={`冷却 ${failover.cooldown_minutes} 分钟`} />
        <Stat label="本次会话用量" value={usage.sum.total_tokens.toLocaleString()} hint={`${usage.sum.calls} 次调用（内存统计，重启清零）`} />
        <Stat label="指名的 AstrBot 模型" value={bound.length} hint={bound.length ? bound.map(([key]) => key).join(' / ') : '全部走默认 Provider'} />
      </Grid>

      <Panel title="任务 → 模型" icon="models">
        <Table
          columns={[
            { key: 'task', title: '任务', render: (row) => row.label, width: '7rem' },
            {
              key: 'source',
              title: '来源',
              width: '6rem',
              render: (row) =>
                row.source === 'astrbot' ? <Badge tone="accent">AstrBot</Badge> : row.source === 'connection' ? <Badge>连接行</Badge> : <Badge tone="warn">未配置</Badge>,
            },
            { key: 'provider', title: '连接 / Provider', render: (row) => row.provider_label || '—' },
            { key: 'model', title: '模型', render: (row) => row.model || '—' },
            {
              key: 'modalities',
              title: '能力声明',
              render: (row) => {
                const item = data.task_models[row.task]
                const mods = item?.modalities ?? []
                return mods.length ? mods.map((m) => <Badge key={m}>{m}</Badge>) : <span class="text-muted">未声明</span>
              },
            },
            { key: 'candidates', title: '候选', width: '4rem', render: (row) => row.candidates },
            { key: 'reason', title: '判定', render: (row) => <span class="font-mono text-[11px] text-muted">{row.reason || '—'}</span> },
          ]}
          rows={tasks}
          rowKey={(row) => row.task}
        />
      </Panel>

      <Grid cols={2}>
        <Panel title={`模型连接池（${connections.length}）`} icon="link">
          {connections.length === 0 ? (
            <Empty text="连接池是空的。只用了 AstrBot 的模型时这是正常的。" icon="link" />
          ) : (
            <div class="flex flex-col gap-3">
              {connections.map((row, index) => (
                <div key={index} class="rounded-lg border border-line px-3 py-2">
                  <div class="flex items-center gap-2">
                    <span class="text-xs font-medium">{row.label || '(未命名)'}</span>
                    {row.enabled ? <Badge tone="ok">启用</Badge> : <Badge tone="warn">停用</Badge>}
                    <Badge>{row.mode}</Badge>
                    <span class="ml-auto text-[11px] text-muted">{row.model || '模型未填'}</span>
                  </div>
                  <div class="mt-2">
                    <KeyValue
                      rows={[
                        ['地址', row.endpoint ? <span class="font-mono text-[11px]">{row.endpoint}</span> : <span class="text-warn">留空（走 AstrBot）</span>],
                        ['密钥', row.has_key ? '已填' : '未填'],
                        ['用途', row.tasks.length ? row.tasks.join('、') : '未勾选'],
                        ['输出格式', row.response_format || '—'],
                        ['采样', `${fmt(row.temperature)} / top_p — / ${fmt(row.max_tokens, ' tokens')}`],
                        ['单价', row.prices.input || row.prices.output ? `入 ${row.prices.input} / 出 ${row.prices.output}` : '未配置'],
                      ]}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <Panel title={`AstrBot 模型（${astrbot_providers.length}）`} icon="models">
          {astrbot_providers.length === 0 ? (
            <Empty text="AstrBot 里还没有配置模型。" icon="models" />
          ) : (
            <Table
              columns={[
                { key: 'id', title: 'ID', render: (row) => <span class="font-mono text-[11px]">{row.id}</span> },
                { key: 'model', title: '模型', render: (row) => row.model || '—' },
                {
                  key: 'modalities',
                  title: '模型能力',
                  render: (row) => (row.modalities.length ? row.modalities.map((m) => <Badge key={m}>{m}</Badge>) : <span class="text-muted">未勾选</span>),
                },
                {
                  key: 'used_by',
                  title: '被哪些任务使用',
                  render: (row) => (row.used_by.length ? row.used_by.join('、') : <span class="text-muted">—</span>),
                },
              ]}
              rows={astrbot_providers}
              rowKey={(row) => row.id}
            />
          )}
        </Panel>
      </Grid>

      <Grid cols={3}>
        <Panel title="Embedding" icon="disk">
          <KeyValue
            rows={[
              ['状态', embedding.enabled ? <Badge tone="ok">启用</Badge> : <Badge>关闭</Badge>],
              ['Provider', embedding.provider_id || <span class="text-muted">默认（第一个）</span>],
              ['历史召回', embedding.semantic_history ? '开' : '关'],
              ['实时问答向量', embedding.live_query ? '开' : '关'],
              ['维度', embedding.dimensions || '自动'],
              ['地址', embedding.endpoint || '从连接推导'],
            ]}
          />
        </Panel>
        <Panel title="图片理解" icon="image">
          <KeyValue
            rows={[
              ['状态', vision.enabled ? <Badge tone="ok">启用</Badge> : <Badge>关闭</Badge>],
              ['识图方式', vision.mode === 'sidecar' ? '独立视觉连接' : '原生多模态'],
              ['解析模型', vision.provider_id || <span class="text-muted">默认</span>],
              ['细节档位', vision.detail || 'auto'],
              ['最长边', vision.max_image_dimension || '原图'],
            ]}
          />
        </Panel>
        <Panel title="语音理解" icon="fire">
          <KeyValue
            rows={[
              ['状态', audio.enabled ? <Badge tone="ok">启用</Badge> : <Badge>关闭</Badge>],
              ['转写模型', audio.provider_id || <span class="text-muted">未指定（交给主模型）</span>],
              ['转码格式', audio.out_format],
              ['单条上限', `${audio.max_file_size_mb} MB`],
            ]}
          />
        </Panel>
      </Grid>

      <Panel title={`用量（最近 ${usage.recent.length} 次）`} icon="clock">
        {usage.totals.length === 0 ? (
          <Empty text="还没有记录到用量。多数网关会在响应里带 usage，没有就统计不到。" icon="clock" />
        ) : (
          <div class="flex flex-col gap-4">
            <Table
              columns={[
                { key: 'task', title: '任务', render: (row) => row.task || '未标注' },
                { key: 'calls', title: '调用', render: (row) => row.calls },
                { key: 'prompt', title: '输入 token', render: (row) => row.prompt_tokens.toLocaleString() },
                { key: 'completion', title: '输出 token', render: (row) => row.completion_tokens.toLocaleString() },
                { key: 'total', title: '合计', render: (row) => row.total_tokens.toLocaleString() },
                {
                  key: 'meter',
                  title: '占比',
                  render: (row) => <Meter value={usage.sum.total_tokens ? row.total_tokens / usage.sum.total_tokens : 0} max={1} />,
                },
              ]}
              rows={usage.totals}
              rowKey={(row) => row.task}
            />
            <Table
              columns={[
                { key: 'at', title: '时间', mono: true, render: (row) => row.at },
                { key: 'task', title: '任务', render: (row) => row.task || '未标注' },
                { key: 'model', title: '模型', render: (row) => row.model || '—' },
                { key: 'prompt', title: '入', render: (row) => row.prompt_tokens },
                { key: 'completion', title: '出', render: (row) => row.completion_tokens },
                { key: 'total', title: '合计', render: (row) => row.total_tokens },
              ]}
              rows={usage.recent.slice(0, 20)}
            />
          </div>
        )}
      </Panel>

      <Panel title="主叙事参数" icon="compass">
        <KeyValue
          rows={[
            ['temperature', fmt(main.temperature)],
            ['top_p', fmt(main.top_p)],
            ['max_tokens', fmt(main.max_tokens)],
            ['timeout', fmt(main.timeout, ' ms')],
            ['输出格式', main.response_format || '—'],
            ['首泡加速', main.streaming_mode === 'experimental' ? '实验性开启' : '关闭'],
          ]}
        />
      </Panel>
    </Stack>
  )
}
