import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { AgencyPayload } from '../types'
import { Badge, Empty, ErrorNote, Grid, Icon, KeyValue, Loading, Note, Panel, Stack, Stat, Table } from '../components/ui'

const LOAD_LABEL: Record<string, string> = {
  light: '轻松', normal: '一般', busy: '繁忙', overloaded: '超载',
  low: '低', medium: '中', high: '高',
}
const PRIVACY_LABEL: Record<string, string> = {
  public: '公开场合', semi: '半私密', private: '私密', alone: '独处',
}
const DEVICE_LABEL: Record<string, string> = {
  full: '完全可用', limited: '受限', unavailable: '不可用', none: '不可用',
}

function label(map: Record<string, string>, value: string) {
  if (!value) return '未建立'
  return map[value] ?? value
}

/** 三档容量的可视化：每一档画成一段进度，值越低越"受限"。 */
function Capacity({ name, value, options, label }: { name: string; value: string; options: string[]; label: string }) {
  const index = Math.max(0, options.indexOf(value))
  const ratio = options.length > 1 ? index / (options.length - 1) : 1
  const tone = ratio >= 0.66 ? 'bg-ok' : ratio >= 0.33 ? 'bg-warn' : 'bg-danger'
  return (
    <div class="rounded-lg border border-line px-3 py-2">
      <div class="flex items-center justify-between text-xs">
        <span class="text-muted">{name}</span>
        <span class="font-medium">{label}</span>
      </div>
      <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-line">
        <div class={`h-full rounded-full ${tone}`} style={{ width: `${Math.round(ratio * 100)}%` }} />
      </div>
      <div class="mt-1 flex justify-between text-[10px] text-muted">
        {options.map((item) => (
          <span key={item}>{item}</span>
        ))}
      </div>
    </div>
  )
}

export function Agency({ storyId, refreshKey }: PanelProps) {
  const { data, error, loading, reload } = useQuery<AgencyPayload>(
    'console/agency',
    { story_id: storyId },
    { nonce: refreshKey },
  )

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />
  if (!data) return <Empty text="没有拿到数据" />
  if (!data.story) return <Empty text="还没有任何剧本。" icon="compass" />

  const { window: win, plan, config } = data

  return (
    <Stack>
      {!win && (
        <Note>
          <Icon name="info" class="mr-1 inline h-3 w-3" />
          还没有建立行动窗口。它由后台按日程与你的近期作息推断出来，跑过一轮自动推进后就有了。
        </Note>
      )}

      <Grid cols={3}>
        <Stat
          label="日程负荷"
          value={label(LOAD_LABEL, win?.activity_load ?? '')}
          hint="越忙，主动联系越难发生"
          tone={win?.activity_load === 'overloaded' ? 'danger' : 'neutral'}
        />
        <Stat label="隐私环境" value={label(PRIVACY_LABEL, win?.privacy ?? '')} hint="是否方便说话" />
        <Stat label="设备可用" value={label(DEVICE_LABEL, win?.device_access ?? '')} hint="手机/电脑是否在手" />
      </Grid>

      <Panel title="行动容量" icon="compass">
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Capacity
            name="日程负荷"
            value={win?.activity_load ?? ''}
            options={['light', 'normal', 'busy', 'overloaded']}
            label={label(LOAD_LABEL, win?.activity_load ?? '')}
          />
          <Capacity
            name="隐私环境"
            value={win?.privacy ?? ''}
            options={['public', 'semi', 'private', 'alone']}
            label={label(PRIVACY_LABEL, win?.privacy ?? '')}
          />
          <Capacity
            name="设备可用"
            value={win?.device_access ?? ''}
            options={['unavailable', 'limited', 'full']}
            label={label(DEVICE_LABEL, win?.device_access ?? '')}
          />
        </div>
        <div class="mt-4">
          <KeyValue
            rows={[
              ['下次机会', win?.next_opportunity_at || '未限定'],
              ['窗口有效至', win?.valid_until || '—'],
              ['最后更新', win?.updated_at || '—'],
              [
                '推断依据',
                <span class="text-[11px] text-muted">{win?.basis || '—'}</span>,
              ],
              [
                '来源条目',
                win?.source_entry_ids?.length
                  ? win.source_entry_ids.map((id) => <code key={id} class="mr-1 font-mono text-[11px]">#{id}</code>)
                  : '—',
              ],
            ]}
          />
        </div>
      </Panel>

      <Panel title="日程预排" icon="calendar">
        {!plan ? (
          <Empty text="还没有日程预排数据（该功能默认关闭）。" icon="calendar" />
        ) : (
          <div class="flex flex-col gap-3">
            <Grid cols={3}>
              <Stat label="修订号" value={plan.revision} hint={`时区 ${plan.timezone || '—'}`} />
              <Stat label="有效期" value={`${plan.valid_from || '—'} → ${plan.valid_through || '—'}`} />
              <Stat label="最近复盘" value={plan.last_reviewed || '—'} hint={plan.review_reason || ''} />
            </Grid>
            <div class="flex flex-wrap gap-1">
              <Badge tone="accent">规律 {plan.regimes.length}</Badge>
              <Badge tone="warn">例外 {plan.exceptions.length}</Badge>
              <Badge>已物化 {plan.materialized_days.length} 天</Badge>
            </div>
            {plan.regimes.length > 0 && (
              <div>
                <h3 class="mb-1 text-xs font-medium">规律</h3>
                <pre class="prose-body max-h-56 overflow-auto rounded-lg bg-code p-2 font-mono text-[11px]">
                  {JSON.stringify(plan.regimes, null, 2)}
                </pre>
              </div>
            )}
            {plan.exceptions.length > 0 && (
              <div>
                <h3 class="mb-1 text-xs font-medium">例外</h3>
                <pre class="prose-body max-h-56 overflow-auto rounded-lg bg-code p-2 font-mono text-[11px]">
                  {JSON.stringify(plan.exceptions, null, 2)}
                </pre>
              </div>
            )}
          </div>
        )}
      </Panel>

      <Panel title="Agency 配置" icon="config">
        <KeyValue
          rows={[
            ['启用', config.enabled ? <Badge tone="ok">开</Badge> : <Badge>关</Badge>],
            ['默认日程负荷', String(config.activity_load ?? '—')],
            ['默认隐私', String(config.privacy ?? '—')],
            ['默认设备', String(config.device_access ?? '—')],
            ['窗口时长', config.window_hours ? `${config.window_hours} 小时` : '—'],
          ]}
        />
        <p class="mt-3 text-[11px] text-muted">
          Agency Window 只约束「她能不能联系你」，不影响文风与情绪——那是 Alter 的事。
        </p>
      </Panel>

      <Panel title="相关条目" icon="script">
        {(win?.source_entry_ids?.length ?? 0) === 0 ? (
          <Empty text="窗口没有登记来源条目。" icon="script" />
        ) : (
          <Table
            columns={[
              { key: 'id', title: '条目', width: '5rem', mono: true, render: (row) => `#${row}` },
              { key: 'note', title: '说明', render: () => <span class="text-muted">该条目参与推断了当前行动窗口</span> },
            ]}
            rows={win?.source_entry_ids ?? []}
            rowKey={(row) => String(row)}
          />
        )}
      </Panel>
    </Stack>
  )
}
