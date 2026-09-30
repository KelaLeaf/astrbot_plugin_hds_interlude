/**
 * 「动作」面板：她能对 QQ 做的全部动作 + 每一条的权限档位。
 *
 * 目录与档位语义的**唯一事实源**是 `core/platform_actions.py`（后端 `console/actions`
 * 原样下发），这里只负责显示与写档位。三件事必须在界面上说清：
 *
 * 1. 权限表（`action_permissions.json`）里的档位 = 用户在下拉里选的值；
 * 2. 与配置开关是**与**关系——开关在「配置」面板的 `actions_<类别>` / `actions_risks`
 *    组里，关掉时档位无论选什么都不生效（行里那枚状态徽章就是它的结果）；
 * 3. **后端**：`NapCat 专属` 的动作（含走 NapCat WebSocket 拿 cookie 打 QZone CGI 的
 *    那几条空间动作）单独打徽章，顶部一个开关把它们**聚在一起**看。
 */
import { useState } from 'preact/hooks'
import { apiPost, describeError } from '../bridge'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { ActionsCatalogPayload, PlatformActionRow } from '../types'
import { Badge, Button, Empty, ErrorNote, Grid, Loading, Panel, Select, Stack, Stat, Table } from '../components/ui'
import {
  backendBadges, backendNote, filterNapcatOnly, groupActions, napcatOnlyCount, paramSummary,
  riskLabel, riskTone, rowState, tierBreakdown, tierDescription, tierLabel, tierOptions,
} from '../actions-view'

const DEFAULT_MAX_ROWS = 12

export function Actions({ refreshKey }: PanelProps) {
  const { data, error, loading, reload } = useQuery<ActionsCatalogPayload>(
    'console/actions',
    undefined,
    { nonce: refreshKey },
  )
  // 正在保存的动作 id（禁用它的下拉，避免连点两次写出中间态）。
  const [saving, setSaving] = useState('')
  const [saveError, setSaveError] = useState('')
  // 「只看 NapCat 专属」：默认关（日常看全量），打开后只留那不装 NapCat 就用不了的几条。
  const [onlyNapcat, setOnlyNapcat] = useState(false)

  if (error) return <ErrorNote text={error} onRetry={reload} />
  if (loading && !data) return <Loading />

  const tiers = data?.tiers ?? []
  const options = tierOptions(tiers)
  const stats = data?.stats
  const allActions = data?.actions ?? []
  const napcatTotal = napcatOnlyCount(allActions, stats)
  const shownActions = filterNapcatOnly(allActions, onlyNapcat)
  const groups = groupActions(shownActions)
  // 危险动作**真的在跑**才提示（默认全是关的，天天挂一条红条只会训练用户无视它）。
  const riskyEnabled = stats?.risky_enabled ?? 0

  async function changeTier(row: PlatformActionRow, tier: string) {
    setSaving(row.id)
    setSaveError('')
    try {
      await apiPost('console/action-permission', { action: row.id, tier })
      reload()
    } catch (failure) {
      // 保存失败必须说出来：假装成功会让用户以为危险动作已经关掉了。
      setSaveError(`${row.label}（${row.id}）保存失败：${describeError(failure)}`)
    } finally {
      setSaving('')
    }
  }

  async function resetTable() {
    if (!window.confirm('把动作权限表清空？所有动作会回到目录默认档位（危险动作默认关闭）。')) return
    setSaving('__reset__')
    setSaveError('')
    try {
      await apiPost('console/action-permissions-reset', {})
      reload()
    } catch (failure) {
      setSaveError(`清空权限表失败：${describeError(failure)}`)
    } finally {
      setSaving('')
    }
  }

  return (
    <Stack>
      <Grid cols={4}>
        <Stat label="动作总数" value={stats?.total ?? 0} hint="目录里的全部平台动作" />
        <Stat
          label="已启用"
          value={stats?.enabled ?? 0}
          tone="ok"
          hint={`还有 ${stats?.disabled ?? 0} 条当前不可用`}
        />
        <Stat
          label="NapCat 专属"
          value={napcatTotal}
          hint="不装 NapCat 就用不了（含走 NapCat WebSocket 的空间动作）"
        />
        <Stat
          label="危险动作"
          value={`${riskyEnabled} / ${stats?.risky ?? 0}`}
          tone="danger"
          hint={riskyEnabled > 0 ? '有危险动作正在生效' : '默认全部关闭'}
        />
      </Grid>

      {riskyEnabled > 0 ? (
        <div class="flex items-start gap-2 rounded-xl border border-danger/50 bg-danger/10 px-4 py-3 text-xs text-danger">
          <span class="font-medium">
            {data?.risk_warning}：当前有 {riskyEnabled} 个危险动作处于启用状态
          </span>
        </div>
      ) : null}

      {saveError ? <ErrorNote text={saveError} /> : null}

      <div class="flex flex-wrap items-center gap-2 text-xs text-muted">
        <Button
          variant={onlyNapcat ? 'primary' : 'default'}
          icon="filter"
          onClick={() => setOnlyNapcat(!onlyNapcat)}
        >
          只看 NapCat 专属（{napcatTotal}）
        </Button>
        <span>
          {onlyNapcat
            ? `只列不装 NapCat 就用不了的动作：${shownActions.length} 条。`
            : '每条动作下方的徽章写的是它走哪个后端；带「NapCat 专属」的那些只列在筛选里。'}
        </span>
        <span class="ml-auto" title="所有人 / 群管 / 管理员 / 关闭">
          权限档位：{tierBreakdown(tiers, stats?.permissions)}
        </span>
      </div>

      {groups.length === 0 ? (
        <Empty
          text={onlyNapcat ? '没有 NapCat 专属动作（这个后端下确实一条都没有）。' : '目录里没有任何动作。'}
          icon="shield"
        />
      ) : (
        groups.map((group) => (
          <Panel key={group.category} title={group.label} icon="shield">
            <Table
              columns={[
                {
                  key: 'action',
                  title: '动作',
                  width: '13rem',
                  render: (row) => (
                    <div
                      class={`flex flex-col gap-0.5 border-l-2 pl-2 ${
                        row.risk === 'dangerous' ? 'border-danger bg-danger/5' : 'border-transparent'
                      }`}
                      title={row.summary}
                    >
                      <span class="font-mono text-[11px] text-muted">{row.id}</span>
                      <span class="font-medium">{row.label}</span>
                      <BackendBadges row={row} showNote={onlyNapcat} />
                    </div>
                  ),
                },
                {
                  key: 'summary',
                  title: '说明',
                  render: (row) => <span class="prose-body text-[11px] text-muted">{row.summary}</span>,
                },
                {
                  key: 'risk',
                  title: '风险',
                  width: '5rem',
                  render: (row) => (
                    <Badge tone={riskTone(row.risk)} title={row.summary}>
                      {riskLabel(data?.risk_labels, row.risk)}
                    </Badge>
                  ),
                },
                {
                  key: 'params',
                  title: '参数',
                  width: '16rem',
                  render: (row) => (
                    <span class="font-mono text-[11px] text-muted" title={paramsTitle(row)}>
                      {paramSummary(row.params)}
                    </span>
                  ),
                },
                {
                  key: 'permission',
                  title: '权限档位',
                  width: '13rem',
                  render: (row) => {
                    const state = rowState(row)
                    return (
                      <div class="flex flex-col gap-1">
                        <Select
                          value={row.permission}
                          disabled={Boolean(saving)}
                          onChange={(next) => changeTier(row, String(next))}
                          options={options}
                          placeholder={tierLabel(tiers, row.permission)}
                        />
                        <div class="flex items-center gap-1 text-[11px] text-muted">
                          <Badge tone={state.tone}>{state.text}</Badge>
                          <span title={tierDescription(tiers, row.permission)}>
                            默认 {tierLabel(tiers, row.default_permission)}
                          </span>
                        </div>
                      </div>
                    )
                  },
                },
              ]}
              rows={group.rows}
              empty="这一类里没有动作"
              rowKey={(row) => row.id}
              maxRows={DEFAULT_MAX_ROWS}
            />
          </Panel>
        ))
      )}

      <Panel title="说明" icon="info" actions={<Button icon="refresh" onClick={reload}>刷新</Button>}>
        <div class="space-y-2 text-xs leading-relaxed text-muted">
          <p>
            <Badge>权限表</Badge> 档位存在插件数据目录的
            <span class="font-mono"> {data?.permissions_path || 'action_permissions.json'}</span>
            （独立 JSON，不进插件配置；改一个档位就立刻写一次）。
          </p>
          <p>
            <Badge>与配置开关是「与」关系</Badge> 每个动作还有一枚配置开关，在「配置」面板的
            <span class="font-mono"> actions_&lt;类别&gt;</span> /
            <span class="font-mono"> actions_risks</span> 组里。
            <span class="text-fg">配置开关关掉时，档位无论选什么都不生效</span>
            ——下拉开着、这一行仍会显示「配置开关已关闭」，她也调不动这个动作。
          </p>
          <p>
            <Badge tone="danger">危险动作</Badge> 默认档位是「关闭」，且它们的开关集中在
            <span class="font-mono"> actions_risks</span> 组（{data?.risk_warning}）。
            打开之前先看清说明，有些操作不可逆。
          </p>
          <p>
            <Badge tone="accent">NapCat 专属</Badge> 这几条动作只有 NapCat 后端能做，
            标准 OneBot 实现里没有。QQ 空间那一组走的是 <span class="text-fg">NapCat WebSocket 方案</span>：
            先经已经连着的 NapCat 调 <span class="font-mono">get_cookies</span>
            （<span class="font-mono">domain=user.qzone.qq.com</span>）与
            <span class="font-mono">get_login_info</span> 拿到登录态，再用 cookie 里的
            <span class="font-mono">p_skey</span> 算出 <span class="font-mono">g_tk</span>
            直接打 QZone 的接口——不需要 SnowLuma、也不需要额外依赖；
            装了 SnowLuma 扩展时它只当回退通道（徽章写「回退：需要 SnowLuma 扩展」）。
            只读的「看空间说说 / 看好友动态」不占空间配额，写动作照旧受
            <span class="font-mono">qzone</span> 组的每日上限与最小间隔限制，
            转发计入评论类配额。顶上那个「只看 NapCat 专属」开关能把它们聚在一起看。
          </p>
          <p>
            <Badge>只读页面</Badge> 这一页管的是"她能不能对 QQ 做某件事"；
            「Token 统计」这类只读面板读的是本地账本，不属于权限表
            （免权限：<span class="font-mono">{data?.permissionless_panels?.join('、') || '—'}</span>）。
          </p>
          <div class="pt-1">
            <Button variant="danger" icon="close" disabled={Boolean(saving)} onClick={resetTable}>
              清空权限表（全部回默认档）
            </Button>
          </div>
        </div>
      </Panel>
    </Stack>
  )
}

/** 参数列的悬停说明：只写参数自己的 note（表格里放不下）。 */
function paramsTitle(row: PlatformActionRow): string {
  if (!row.params.length) return '这个动作没有参数'
  return row.params.map((param) => `${param.name}：${param.note || param.label}`).join('\n')
}

/**
 * 后端徽章：首选通道（`backends[0]`）醒目、回退通道中性；标准 OneBot 不显示。
 *
 * `showNote`（打开「只看 NapCat 专属」时）额外把那句通道说明铺在徽章下面——
 * 都聚在一起了，正好一次把"走哪条通道、怎么认证"讲清，不用逐条悬停。
 * 关掉时同样的文字挂在容器的 `title` 上。
 */
function BackendBadges({ row, showNote }: { row: PlatformActionRow; showNote?: boolean }) {
  const badges = backendBadges(row)
  const note = backendNote(row)
  if (!badges.length && !note) return null
  return (
    <div class="flex flex-col gap-0.5" title={note || undefined}>
      {badges.length ? (
        <div class="flex flex-wrap items-center gap-1">
          {badges.map((badge) => (
            <Badge key={badge.label} tone={badge.tone} title={note || badge.title}>
              {badge.label}
            </Badge>
          ))}
        </div>
      ) : null}
      {showNote && note ? (
        <span class="max-w-[16rem] text-[10px] leading-3 text-muted">{note}</span>
      ) : null}
    </div>
  )
}
