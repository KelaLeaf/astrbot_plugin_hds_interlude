/**
 * 连接池编辑表单。
 *
 * 两条硬约定（后端 `ConsoleApi.save_connection` 也按这个实现）：
 * 1. **密钥永不回显**：编辑时输入框是空的，留空就保留原值；要看得出"已经填过了"，
 *    用「已配置」标签表示。想清空就点「清除密钥」。
 * 2. **只提交白名单字段**：用途勾选 + 少量文本/数字，其余配置键后端会原样保留。
 */
import { useState } from 'preact/hooks'
import type { ConnectionRow } from '../types'
import { apiPost } from '../bridge'
import { Badge, Button, Field, Grid, Input, Note, Select, Switch } from './ui'

const MODES = [
  'openai-compatible', 'zhipu-official', 'openai-official', 'deepseek-official',
  'moonshot-official', 'dashscope-official', 'siliconflow-official', 'openrouter', 'gemini-openai',
]

const TASKS: Array<{ field: string; label: string }> = [
  { field: 'use_for_main', label: '主叙事' },
  { field: 'use_for_compaction', label: '后台压缩' },
  { field: 'use_for_alter', label: 'Alter 分析' },
  { field: 'use_for_embedding', label: 'Embedding' },
  { field: 'use_for_stickers', label: '表情包描述' },
  { field: 'use_for_vision', label: '侧端识图' },
]

/** 表单里的内部状态：数字用字符串存，方便用户中途清空。 */
interface Draft {
  label: string
  enabled: boolean
  mode: string
  endpoint: string
  model: string
  api_key: string
  clear_key: boolean
  response_format: string
  temperature: string
  top_p: string
  max_tokens: string
  timeout: string
  price_input: string
  price_output: string
  tasks: Record<string, boolean>
}

function draftFrom(row: ConnectionRow | null): Draft {
  return {
    label: row?.label ?? '',
    enabled: row?.enabled ?? true,
    mode: row?.mode ?? 'openai-compatible',
    endpoint: row?.endpoint ?? '',
    model: row?.model ?? '',
    api_key: '',
    clear_key: false,
    response_format: row?.response_format ?? 'json-object',
    temperature: row?.temperature === null || row?.temperature === undefined ? '0.8' : String(row.temperature),
    top_p: row?.top_p === null || row?.top_p === undefined ? '1' : String(row.top_p),
    max_tokens: row?.max_tokens === null || row?.max_tokens === undefined ? '4096' : String(row.max_tokens),
    timeout: row?.timeout === null || row?.timeout === undefined ? '60000' : String(row.timeout),
    price_input: String(row?.prices?.input ?? 0),
    price_output: String(row?.prices?.output ?? 0),
    tasks: {
      use_for_main: row?.tasks.includes('主叙事') ?? false,
      use_for_compaction: row?.tasks.includes('后台压缩') ?? false,
      use_for_alter: row?.tasks.includes('Alter 分析') ?? false,
      use_for_embedding: row?.tasks.includes('Embedding') ?? false,
      use_for_stickers: row?.tasks.includes('表情包描述') ?? false,
      use_for_vision: row?.tasks.includes('侧端识图') ?? false,
    },
  }
}

export function ConnectionEditor({
  index,
  row,
  onDone,
  onCancel,
}: {
  index: number | null
  row: ConnectionRow | null
  onDone: (message: string) => void
  onCancel: () => void
}) {
  const [draft, setDraft] = useState<Draft>(draftFrom(row))
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState('')
  const update = (patch: Partial<Draft>) => setDraft((prev) => ({ ...prev, ...patch }))

  async function submit() {
    setBusy(true)
    setFailure('')
    const payload: Record<string, unknown> = {
      index: index === null ? '' : index,
      label: draft.label,
      enabled: draft.enabled,
      mode: draft.mode,
      endpoint: draft.endpoint,
      model: draft.model,
      response_format: draft.response_format,
      temperature: draft.temperature,
      top_p: draft.top_p,
      max_tokens: draft.max_tokens,
      timeout: draft.timeout,
      price_input: draft.price_input,
      price_output: draft.price_output,
      ...draft.tasks,
    }
    // 密钥：留空 = 不传（保留原值）；点了清除 = 传 null
    if (draft.clear_key) payload.api_key = null
    else if (draft.api_key.trim()) payload.api_key = draft.api_key.trim()

    try {
      const result = await apiPost<{ changed: string }>('console/connections', payload)
      onDone(result.changed)
    } catch (problem) {
      setFailure(problem instanceof Error ? problem.message : String(problem))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div class="rounded-lg border border-accent/40 bg-raised p-3">
      <div class="mb-3 flex items-center gap-2">
        <span class="text-xs font-medium">{index === null ? '新增连接' : `编辑连接 #${index}`}</span>
        {row?.has_key && <Badge tone="ok">已配置密钥</Badge>}
      </div>

      {failure && <div class="mb-3 rounded border border-danger/40 bg-danger/10 px-2 py-1 text-[11px] text-danger">{failure}</div>}

      <Grid cols={2}>
        <Field label="连接名称">
          <Input value={draft.label} onInput={(value) => update({ label: value })} placeholder="Primary model" />
        </Field>
        <Field label="接口预设" hint="官方预设会自动补上 endpoint">
          <Select
            value={draft.mode}
            onChange={(value) => update({ mode: String(value) })}
            options={MODES.map((mode) => ({ value: mode, label: mode }))}
          />
        </Field>
        <Field label="地址" hint="完整的 Chat Completions 地址；留空则走 AstrBot 的默认模型" >
          <Input
            value={draft.endpoint}
            onInput={(value) => update({ endpoint: value })}
            placeholder="https://api.example.com/v1/chat/completions"
          />
        </Field>
        <Field label="模型名">
          <Input value={draft.model} onInput={(value) => update({ model: value })} placeholder="gpt-4o-mini" />
        </Field>
        <Field
          label="密钥"
          hint={row?.has_key ? '留空表示不修改现有密钥' : '还没有填过'}
        >
          <Input
            value={draft.api_key}
            type="password"
            disabled={draft.clear_key}
            onInput={(value) => update({ api_key: value })}
            placeholder={row?.has_key ? '••••••（留空即不改）' : 'sk-…'}
          />
        </Field>
        <Field label="输出格式" hint="模型在 JSON 模式下空回复时改成 prompt-only">
          <Select
            value={draft.response_format}
            onChange={(value) => update({ response_format: String(value) })}
            options={[
              { value: 'json-object', label: 'json-object' },
              { value: 'prompt-only', label: 'prompt-only' },
            ]}
          />
        </Field>
      </Grid>

      <div class="mt-3">
        <div class="mb-1 text-[11px] font-medium text-muted">用途（决定这条连接参与哪些任务的路由）</div>
        <div class="flex flex-wrap gap-3">
          {TASKS.map((task) => (
            <label key={task.field} class="flex items-center gap-1.5 text-[11px]">
              <Switch
                label={task.label}
                checked={Boolean(draft.tasks[task.field])}
                onChange={(next) => update({ tasks: { ...draft.tasks, [task.field]: next } })}
              />
              {task.label}
            </label>
          ))}
        </div>
      </div>

      <div class="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field label="temperature">
          <Input value={draft.temperature} onInput={(value) => update({ temperature: value })} />
        </Field>
        <Field label="top_p">
          <Input value={draft.top_p} onInput={(value) => update({ top_p: value })} />
        </Field>
        <Field label="max_tokens">
          <Input value={draft.max_tokens} onInput={(value) => update({ max_tokens: value })} />
        </Field>
        <Field label="timeout (ms)">
          <Input value={draft.timeout} onInput={(value) => update({ timeout: value })} />
        </Field>
        <Field label="输入单价 / 百万 token">
          <Input value={draft.price_input} onInput={(value) => update({ price_input: value })} />
        </Field>
        <Field label="输出单价 / 百万 token">
          <Input value={draft.price_output} onInput={(value) => update({ price_output: value })} />
        </Field>
        <Field label="启用">
          <div class="flex h-[30px] items-center gap-2">
            <Switch label="启用" checked={draft.enabled} onChange={(next) => update({ enabled: next })} />
            <span class="text-[11px] text-muted">{draft.enabled ? '参与路由' : '被跳过'}</span>
          </div>
        </Field>
        {row?.has_key && (
          <Field label="清除密钥">
            <div class="flex h-[30px] items-center gap-2">
              <Switch
                label="清除密钥"
                checked={draft.clear_key}
                onChange={(next) => update({ clear_key: next, api_key: '' })}
              />
              <span class="text-[11px] text-muted">{draft.clear_key ? '保存后清空' : '保留现有'}</span>
            </div>
          </Field>
        )}
      </div>

      {draft.endpoint.trim() === '' && (
        <div class="mt-3">
          <Note>地址留空 = 这条连接不直连，请求会走 AstrBot 里配好的模型（或该任务的「指名模型」）。</Note>
        </div>
      )}

      <div class="mt-4 flex items-center gap-2">
        <Button variant="primary" icon="save" disabled={busy} onClick={() => void submit()}>
          {busy ? '保存中…' : '保存'}
        </Button>
        <Button icon="close" disabled={busy} onClick={onCancel}>
          取消
        </Button>
      </div>
    </div>
  )
}
