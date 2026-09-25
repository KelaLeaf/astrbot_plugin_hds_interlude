/**
 * 配置面板。
 *
 * 两块：
 * 1. **配置**（`tab = edit`）：按 `_conf_schema.json` 渲染的**全量编辑器**。为什么要自己做：
 *    AstrBot 自带配置页把「列表」当字符串数组控件（`ListConfigItem`），对象行
 *    （`user_accounts` / `group_chats` / `providers`…）在那儿编辑会被压成字符串——
 *    所以控制台按 schema 自己画表，并在这些字段上挂"宿主页编不了"的提示。
 * 2. **备份**（`tab = backup`）：导出 / 导入（原 `pages/config-backup/` 的功能，一字未减）。
 */
import { useMemo, useState } from 'preact/hooks'
import { apiPost, downloadFile, uploadFile } from '../bridge'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { ConfigField, ConfigGroup, ConfigSchemaPayload, ImportPreview, ParticipantRow } from '../types'
import { Badge, Button, Empty, ErrorNote, FilePicker, Grid, Icon, Note, Panel, Select, Stack, Stat } from '../components/ui'
import { SchemaField, SchemaGroupCard, rowFields } from '../components/SchemaForm'
import type { SchemaNode } from '../components/SchemaForm'

interface ApplyResult {
  saved_via: string
  config_path: string
  format_version: number
  diff: { added: string[]; removed: string[]; changed: string[] }
}

function stamp() {
  const now = new Date()
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
}

/** 分组导航里的伪分组：只读总览。 */
const OVERVIEW = '__overview__'

/** 只读总览里怎么显示一个值（够看就行，不追求精确还原）。 */
function describeValue(field: ConfigField): string {
  const value = field.value
  if (value === undefined || value === null) return '—'
  if (field.type === 'bool') return value ? '开' : '关'
  if (Array.isArray(value)) {
    if (!value.length) return '（空）'
    if (field.rows) return `${value.length} 条`
    return value.map((item) => String(item)).join('、')
  }
  if (typeof value === 'object') {
    const text = JSON.stringify(value)
    return text.length > 90 ? `${text.slice(0, 90)}…` : text
  }
  const text = String(value)
  if (!text) return '（空）'
  return text.length > 90 ? `${text.slice(0, 90)}…` : text
}

/** 配置总览：**只读**，一眼看完全部设置现在是什么。 */
function ConfigOverview({ groups, edits }: { groups: ConfigGroup[]; edits: Record<string, unknown> }) {
  const total = groups.reduce((sum, item) => sum + item.fields.length, 0)
  const dirty = Object.keys(edits).length
  return (
    <Stack>
      <Note>
        只读总览：{groups.length} 个分组、{total} 项。要改哪一项，用上面的下拉切到对应分组。
        {dirty > 0 && ` 当前有 ${dirty} 项改动还没保存。`}
      </Note>
      {groups.map((item) => (
        <details key={item.key} class="rounded-xl border border-line bg-panel">
          <summary class="cursor-pointer px-4 py-3 text-sm font-semibold">
            {shortTitle(item.description) || item.key}
            <span class="ml-2 text-[11px] font-normal text-muted">
              {item.fields.length} 项
              {item.fields.some((field) => field.path in edits) ? ' · 有未保存改动' : ''}
            </span>
          </summary>
          <div class="border-t border-line px-4 py-3">
            <dl class="grid grid-cols-1 gap-x-6 gap-y-0.5 sm:grid-cols-2">
              {item.fields.map((field) => (
                <div key={field.path} class="flex items-baseline gap-2 border-b border-line/40 py-0.5">
                  <dt class="shrink-0 text-[11px] text-muted">
                    {field.description || field.key}
                    {!field.present && <span class="ml-1 opacity-70">未设置</span>}
                  </dt>
                  <dd class="ml-auto min-w-0 truncate text-right font-mono text-[11px]">
                    {describeValue(field.path in edits ? { ...field, value: edits[field.path] } : field)}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        </details>
      ))}
    </Stack>
  )
}

/** 白名单类列表可以从已知会话一键带出 QQ / 群号。 */
const AUTOFILL: Record<string, { label: string; keys: string[] }> = {
  'qq_access.user_accounts': { label: '从最近会话填入', keys: ['user_id', 'display_name'] },
  'qq_access.bot_accounts': { label: '从最近会话填入', keys: ['self_id'] },
  'qq_access.group_chats': { label: '从最近会话填入', keys: ['channel_id', 'display_name'] },
}

export function Config({ refreshKey }: PanelProps) {
  const [tab, setTab] = useState<'edit' | 'backup'>('edit')
  return (
    <Stack>
      <div class="flex items-center gap-2">
        <Button variant={tab === 'edit' ? 'primary' : 'default'} icon="config" onClick={() => setTab('edit')}>
          配置
        </Button>
        <Button variant={tab === 'backup' ? 'primary' : 'default'} icon="shield" onClick={() => setTab('backup')}>
          备份 / 导入
        </Button>
      </div>
      {tab === 'edit' ? <ConfigEditor refreshKey={refreshKey} /> : <ConfigBackup refreshKey={refreshKey} />}
    </Stack>
  )
}

/* ------------------------------------------------------------------ #
 * 全量配置编辑器
 * ------------------------------------------------------------------ */

function ConfigEditor({ refreshKey }: Pick<PanelProps, 'refreshKey'>) {
  const schema = useQuery<ConfigSchemaPayload>('console/config', {}, { nonce: refreshKey })
  const participants = useQuery<{ participants: ParticipantRow[] }>('console/participants', {}, { nonce: refreshKey })
  const [group, setGroup] = useState(OVERVIEW)
  const [edits, setEdits] = useState<Record<string, unknown>>({})
  const [message, setMessage] = useState('')
  const [failure, setFailure] = useState('')
  const [saving, setSaving] = useState(false)

  const groups = schema.data?.groups ?? []
  const active = useMemo(() => {
    if (group === OVERVIEW) return null
    return groups.find((item) => item.key === group) ?? groups[0]
  }, [groups, group])
  const overview = group === OVERVIEW
  const dirty = Object.keys(edits)
  const dirtyInGroup = (active?.fields ?? [])
    .filter((field) => field.path in edits)
    .map((field) => field.path)

  async function save(paths: string[]) {
    if (!paths.length) return
    setSaving(true)
    setFailure('')
    setMessage('')
    try {
      for (const path of paths) {
        await apiPost('console/config-set', { path, value: edits[path] ?? null })
      }
      setEdits((prev) => {
        const next = { ...prev }
        for (const path of paths) delete next[path]
        return next
      })
      setMessage(`已保存并生效（${paths.length} 项）：${paths.join('、')}`)
      schema.reload()
    } catch (problem) {
      setFailure(problem instanceof Error ? problem.message : String(problem))
    } finally {
      setSaving(false)
    }
  }

  if (schema.error) return <ErrorNote text={schema.error} onRetry={schema.reload} />
  if (schema.loading && !schema.data) return <Empty text="正在读取配置…" icon="config" />
  if (!active && !overview) return <Empty text="没有读到配置 schema" icon="warning" />

  const known = participants.data?.participants ?? []

  return (
    <Stack>
      {failure && <ErrorNote text={failure} onRetry={() => setFailure('')} />}
      {message && <Note tone="ok">{message}</Note>}
      {dirty.length > 0 && (
        <Note tone="warn">
          有 {dirty.length} 项改动还没保存：{dirty.slice(0, 6).join('、')}
          {dirty.length > 6 ? ' …' : ''}
          <span class="ml-2 inline-flex gap-2 align-middle">
            <Button variant="primary" icon="save" disabled={saving} onClick={() => void save(dirty)}>
              全部保存
            </Button>
            <Button onClick={() => setEdits({})}>丢弃改动</Button>
          </span>
        </Note>
      )}

      <div class="flex flex-wrap items-center gap-2">
        <div class="w-full sm:w-80">
          <Select
            value={group}
            onChange={setGroup}
            options={[
              { value: OVERVIEW, label: '配置总览（只读）' },
              ...groups.map((item) => ({
                value: item.key,
                label: `${shortTitle(item.description) || item.key}${
                  item.fields.some((field) => field.path in edits) ? '  · 有改动' : ''
                }`,
              })),
            ]}
          />
        </div>
        {!overview && <span class="text-[11px] text-muted">共 {groups.length} 个分组，选一个开始改</span>}
      </div>

      {overview ? (
        <ConfigOverview groups={groups} edits={edits} />
      ) : (
      <SchemaGroupCard
        title={shortTitle(active!.description) || active!.key}
        description={active!.description ?? ''}
        actions={
          <>
            <Button
              variant="primary"
              icon="save"
              disabled={saving || !dirtyInGroup.length}
              onClick={() => void save(dirtyInGroup)}
            >
              保存本组{dirtyInGroup.length ? `（${dirtyInGroup.length}）` : ''}
            </Button>
            <Button icon="refresh" onClick={schema.reload}>重新读取</Button>
          </>
        }
      >
        {active!.fields
          .filter((field) => !field.invisible)
          .map((field) => {
            const node = field.node as SchemaNode
            const value = field.path in edits ? edits[field.path] : field.value
            const rows = rowFields(node)
            const fill = AUTOFILL[field.path]
            return (
              <SchemaField
                key={field.path}
                path={field.path}
                node={node}
                value={value}
                note={field.note}
                delegated={field.delegated}
                choices={schema.data?.choices}
                autofill={
                  fill && rows
                    ? {
                        label: fill.label,
                        key: fill.keys[0],
                        sourceKey: fill.keys[0],
                        run: async () => {
                          const source = fill.keys.find((key) => known.some((row) => (row as never)[key]))
                          const row = known.find((item) => source && (item as never)[source])
                          if (!row || !source) return null
                          const filled: Record<string, unknown> = {}
                          if (source === 'user_id') filled.qq = row.user_id
                          if (source === 'self_id') filled.qq = row.self_id
                          if (source === 'channel_id') filled.group_id = row.channel_id
                          if (row.display_name && 'label' in rows) filled.label = row.display_name
                          participants.reload()
                          return filled
                        },
                      }
                    : undefined
                }
                onChange={(next) => setEdits((prev) => ({ ...prev, [field.path]: next }))}
              />
            )
          })}
      </SchemaGroupCard>
      )}
    </Stack>
  )
}

/** 分组标题：schema 的 description 前面挂着 `【必填 2】` 这类标记，导航里省掉。 */
function shortTitle(text: string | undefined): string {
  return String(text ?? '').replace(/^【[^】]*】\s*/, '')
}

/* ------------------------------------------------------------------ #
 * 备份 / 导入（原「配置备份」页）
 * ------------------------------------------------------------------ */

function ConfigBackup({ refreshKey }: Pick<PanelProps, 'refreshKey'>) {
  void refreshKey
  const [exporting, setExporting] = useState('')
  const [importing, setImporting] = useState('')
  const [message, setMessage] = useState('')
  const [fatal, setFatal] = useState('')
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [pending, setPending] = useState('')
  const [fileName, setFileName] = useState('')

  async function doExport() {
    setExporting('正在导出…')
    setFatal('')
    try {
      await downloadFile('config-export', {}, `hdsi-config-${stamp()}.json`)
      setExporting('已开始下载。')
    } catch (error) {
      setExporting('')
      setFatal(error instanceof Error ? error.message : String(error))
    }
  }

  async function doPreview(file: File) {
    setFileName(file.name)
    setImporting('正在解析…')
    setFatal('')
    setPreview(null)
    setPending('')
    try {
      const result = await uploadFile<ImportPreview>('config-import-preview', file)
      setPreview(result)
      setPending(result.payload)
      setImporting('')
    } catch (error) {
      setImporting('')
      setFatal(error instanceof Error ? error.message : String(error))
    }
  }

  async function doApply() {
    if (!pending) return
    setImporting('正在写入…')
    setFatal('')
    try {
      const result = await apiPost<ApplyResult>('config-import-apply', { payload: pending })
      setMessage(
        `已导入并生效（${result.saved_via}）：修改 ${result.diff.changed.length} 项、新增 ${result.diff.added.length} 项。`,
      )
      setPreview(null)
      setPending('')
      setFileName('')
    } catch (error) {
      setFatal(error instanceof Error ? error.message : String(error))
    } finally {
      setImporting('')
    }
  }

  function reset() {
    setPreview(null)
    setPending('')
    setFileName('')
    setMessage('')
  }

  return (
    <Stack>
      {fatal && <ErrorNote text={fatal} onRetry={reset} />}
      {message && <Note tone="ok">{message}</Note>}

      <Grid cols={2}>
        <Panel title="导出配置" icon="download">
          <p class="mb-3 text-xs text-muted">
            把当前配置的完整快照下载成一个 JSON 文件（同时落一份到插件数据目录的 <code class="font-mono">exports/</code>）。
            导出的是磁盘上那份原样配置，包含插件不认识的键；文件带格式版本号，升级插件后仍可导回。
          </p>
          <div class="flex items-center gap-3">
            <Button variant="primary" icon="download" onClick={doExport} disabled={Boolean(exporting) && exporting !== '已开始下载。'}>
              下载配置备份
            </Button>
            <span class="text-[11px] text-muted">{exporting}</span>
          </div>
        </Panel>

        <Panel title="导入配置" icon="upload">
          <p class="mb-3 text-xs text-muted">
            先选文件、看清会改哪些项，再确认导入。导入是「合并」：文件里没写的设置保持原值，不认识的键原样保留。
          </p>
          <FilePicker
            onPick={(file) => void doPreview(file)}
            disabled={Boolean(importing) && importing !== '正在解析…'}
            hint="只接受本插件导出的 JSON"
          />
          <div class="mt-2 text-[11px] text-muted">
            {importing || (fileName ? `已选择：${fileName}` : '还没有选择文件')}
          </div>
        </Panel>
      </Grid>

      {preview && (
        <Panel
          title="将要做的改动"
          icon="filter"
          actions={
            <>
              <Button variant="primary" icon="save" onClick={doApply} disabled={Boolean(importing)}>
                确认导入
              </Button>
              <Button icon="close" onClick={reset}>取消</Button>
            </>
          }
        >
          <div class="flex flex-col gap-4">
            <div class="flex flex-wrap items-center gap-2 text-[11px] text-muted">
              <Badge tone="accent">文件格式 v{preview.report.format_version}</Badge>
              <Badge>{preview.report.source}</Badge>
              <span>覆盖 {preview.report.section_count} 个分组</span>
              <span>未受影响 {preview.report.diff.same} 项</span>
            </div>

            <Grid cols={3}>
              <Stat label="将被覆盖" value={preview.report.diff.changed.length} />
              <Stat label="新增" value={preview.report.diff.added.length} />
              <Stat label="文件里没有（保持原值）" value={preview.report.diff.removed.length} tone={preview.report.diff.removed.length ? 'warn' : 'neutral'} />
            </Grid>

            {preview.report.diff.changed.length > 0 && (
              <div>
                <h3 class="mb-1 text-xs font-medium">将被覆盖的键</h3>
                <div class="flex flex-wrap gap-1">
                  {preview.report.diff.changed.slice(0, 60).map((key) => (
                    <code key={key} class="rounded bg-code px-1 py-0.5 font-mono text-[11px]">{key}</code>
                  ))}
                  {preview.report.diff.changed.length > 60 && (
                    <span class="text-[11px] text-muted">等 {preview.report.diff.changed.length} 项</span>
                  )}
                </div>
              </div>
            )}

            {preview.report.diff.added.length > 0 && (
              <div>
                <h3 class="mb-1 text-xs font-medium">新增的键</h3>
                <div class="flex flex-wrap gap-1">
                  {preview.report.diff.added.slice(0, 60).map((key) => (
                    <code key={key} class="rounded bg-code px-1 py-0.5 font-mono text-[11px]">{key}</code>
                  ))}
                </div>
              </div>
            )}

            {[...preview.report.notes, ...preview.report.warnings].map((text, index) => (
              <Note key={index} tone={preview.report.warnings.includes(text) ? 'warn' : 'neutral'}>
                {text}
              </Note>
            ))}
          </div>
        </Panel>
      )}

      {!preview && (
        <Panel title="兼容性" icon="shield">
          <ul class="flex flex-col gap-1 text-xs text-muted">
            <li class="flex gap-2">
              <Icon name="tick" class="mt-0.5 h-3.5 w-3.5 shrink-0 text-ok" />
              旧版本导出的文件、没有格式头的裸配置、手写的片段都能导入。
            </li>
            <li class="flex gap-2">
              <Icon name="tick" class="mt-0.5 h-3.5 w-3.5 shrink-0 text-ok" />
              更新版本插件导出的文件也会尽量导入：不认识的键原样保留，缺的键用默认值补齐。
            </li>
            <li class="flex gap-2">
              <Icon name="tick" class="mt-0.5 h-3.5 w-3.5 shrink-0 text-ok" />
              导入后立即生效，不需要重启 AstrBot。
            </li>
          </ul>
        </Panel>
      )}

      {!preview && !message && <Empty text="选一个之前导出的 JSON 文件就能看到变更预览。" icon="upload" />}
    </Stack>
  )
}
