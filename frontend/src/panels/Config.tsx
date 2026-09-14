/** 配置备份面板：从原来的 `pages/config-backup/` 页面迁移过来，功能一字未减。 */
import { useState } from 'preact/hooks'
import { downloadFile, uploadFile, apiPost } from '../bridge'
import type { PanelProps } from '../main'
import type { ImportPreview } from '../types'
import { Badge, Button, Empty, ErrorNote, Grid, Icon, Note, Panel, Stack, Stat } from '../components/ui'

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

export function Config({ refreshKey }: PanelProps) {
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
          <div class="flex items-center gap-3">
            <input
              type="file"
              accept=".json,application/json"
              class="text-xs"
              onChange={(event) => {
                const file = (event.currentTarget as HTMLInputElement).files?.[0]
                if (file) void doPreview(file)
              }}
            />
            <span class="text-[11px] text-muted">{importing || fileName}</span>
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
            <li class="flex gap-2">
              <Icon name="warning" class="mt-0.5 h-3.5 w-3.5 shrink-0 text-warn" />
              一个边界：AstrBot 的插件配置由 <code class="font-mono">_conf_schema.json</code> 定义，
              插件不认识的键写进去之后会在下次加载时被 AstrBot 清掉。这不影响你认识的任何设置。
            </li>
          </ul>
        </Panel>
      )}

      {!preview && !message && <Empty text="选一个之前导出的 JSON 文件就能看到变更预览。" icon="upload" />}
    </Stack>
  )
}
