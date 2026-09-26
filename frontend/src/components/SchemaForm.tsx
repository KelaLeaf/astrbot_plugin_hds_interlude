/**
 * 按 `_conf_schema.json` 渲染表单的通用控件。
 *
 * 为什么要自己写：AstrBot 自带的配置页把「列表」渲染成**字符串数组**控件
 * （`ListConfigItem`），`items` 里的行内字段定义完全不生效——对象行
 * （`qq_access.user_accounts` / `group_chats` / `model_center.providers`…）
 * 在那儿编辑会把整行压成一个字符串。所以控制台按 schema 自己画：
 * 递归对象、对象行表格、标量标签、JSON 兜底，一个都不少。
 */
import type { ComponentChildren } from 'preact'
import { useState } from 'preact/hooks'
import { Badge, Button, Input, Note, Select, Switch, Textarea } from './ui'

export type NoteLevel = 'warn' | 'info'

export interface SchemaNode {
  type?: string
  description?: string
  hint?: string
  default?: unknown
  options?: string[] | null
  items?: Record<string, SchemaNode> | null
  _special?: string
  invisible?: boolean
  [key: string]: unknown
}

export interface FieldNote {
  level: NoteLevel
  text: string
}

/** 与后端 `schema_row_fields()` 同一条规则：`items` 是「字段映射」才算对象行。 */
const SCALAR_ITEM_KEYS = new Set([
  'type', 'options', 'default', 'description', 'hint', 'slider', 'render_type',
  'editor_mode', 'editor_language', 'editor_theme', '_special', 'invisible',
])

export function rowFields(node: SchemaNode | undefined): Record<string, SchemaNode> | null {
  if (!node || node.type !== 'list' || !node.items || typeof node.items !== 'object') return null
  const entries = Object.entries(node.items).filter(([key]) => !SCALAR_ITEM_KEYS.has(key))
  if (!entries.length) return null
  const ok = entries.every(([, spec]) => spec && typeof spec === 'object' && 'type' in spec)
  return ok ? (Object.fromEntries(entries) as Record<string, SchemaNode>) : null
}

function isScalarList(node: SchemaNode): boolean {
  return node.type === 'list' && rowFields(node) === null
}

function itemType(node: SchemaNode): string {
  const items = node.items as SchemaNode | undefined
  return typeof items === 'object' && items !== null && 'type' in items ? String(items.type ?? '') : ''
}

function toNumber(kind: string, text: string): number | null {
  const trimmed = text.trim()
  if (trimmed === '') return null
  const parsed = kind === 'int' ? Number.parseInt(trimmed, 10) : Number.parseFloat(trimmed)
  return Number.isFinite(parsed) ? parsed : null
}

/** 一个字段的标签行：中文名 + 提示。
 *
 * `warn` 级别的 note（宿主配置页编不了那类）**不在这里显示**——用户明确要求不要在
 * 控制台里铺那段解释；该说的话写在 `_conf_schema.json` 的 hint 里，由宿主配置页展示。
 * `info` 级别的短提示（行为提醒 / "请在模型面板编辑"）照旧显示，它们是真的有用。 */
function Label({ node, value, note }: { node: SchemaNode; value: unknown; note?: FieldNote | null }) {
  // 指向「控制台 → 配置」的那句提示是给**宿主配置页**看的，在本页显示没有意义。
  const rawHint = String(node.hint ?? '')
  const hint = rawHint.includes('幕间控制台') ? '' : rawHint
  const short = note?.level === 'info' ? note.text : ''
  return (
    <div class="flex flex-wrap items-center gap-2">
      <span class="text-[11px] font-medium">{String(node.description ?? '')}</span>
      {value === undefined && <Badge tone="neutral">未设置</Badge>}
      {hint && <span class="text-[11px] text-muted">{hint}</span>}
      {short && <span class="text-[11px] text-muted">{short}</span>}
    </div>
  )
}

function NoteLine({ note }: { note?: FieldNote | null }) {
  // 只有"请在别处编辑"这类 info 提示才落成一行；warn 不再重复一遍说明。
  if (!note || note.level !== 'info') return null
  return <Note>{note.text}</Note>
}

export interface SchemaFieldProps {
  node: SchemaNode
  value: unknown
  onChange: (next: unknown) => void
  path: string
  note?: FieldNote | null
  delegated?: boolean
  choices?: Record<string, Array<{ value: string; label: string }>>
  /** 对象行列表可以提供一个"从已知来源填入"按钮（白名单用）。 */
  autofill?: { label: string; key: string; sourceKey: string; run: () => Promise<Record<string, unknown> | null> }
  depth?: number
  /** 行内字段（行本身已经是一张卡）不再套一层框。 */
  flat?: boolean
  /** 属于这个字段的「仅处理名单内」开关（`schema-pairs.ts` 配的对）。
   *
   * 为什么放在这里而不是让它自己渲染成一张卡：开关是**这张名单的**开关，平铺渲染会
   * 变成名单卡下面另一张独立的小卡，看着像不属于谁（用户指出"开关按钮不在它所属配置
   * 项目的背景框里"）。收进名单卡里，一眼就知道它管的是上面那张名单。 */
  attached?: { node: SchemaNode; value: unknown; note?: FieldNote | null; onChange: (next: unknown) => void } | null
}

/** 每个字段的容器样式：**统一**给一层边框 + 底色。
 *
 * 之前只有"对象行列表"里的行有框，标量字段和嵌套对象都没有（嵌套对象只有一条左边线），
 * 于是页面上有的地方有框、有的没有（用户直接指出"看着很乱"）。现在所有字段一律一张卡，
 * 嵌套层级用底色区分：顶层 `bg-raised/30`、里层 `bg-panel`。 */
function fieldCard(depth: number, flat = false): string {
  if (flat) return 'flex flex-col gap-2'
  // 交替底色：层级越深越"浅一层"，嵌套时一眼能看出从属关系（而不是一片同色）。
  return `flex flex-col gap-2 rounded-lg border border-line p-3 ${depth % 2 === 0 ? 'bg-raised/30' : 'bg-panel'}`
}

/** 递归渲染一个 schema 字段。 */
export function SchemaField(props: SchemaFieldProps) {
  const { node, value, onChange, path, note, delegated, choices, depth = 0, flat = false } = props
  const kind = String(node.type ?? 'string')

  if (kind === 'object') {
    const items = (node.items ?? {}) as Record<string, SchemaNode>
    const current = (value && typeof value === 'object' && !Array.isArray(value)) ? (value as Record<string, unknown>) : {}
    const children = Object.entries(items).filter(([, spec]) => !spec?.invisible)
    if (!children.length) return null
    return (
      <div class={`${fieldCard(depth, flat)} gap-3`}>
        <Label node={node} value={value} note={note} />
        <NoteLine note={note} />
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {children.map(([key, child]) => (
            <div key={key} class={child.type === 'object' || child.type === 'list' ? 'sm:col-span-2' : ''}>
              <SchemaField
                {...props}
                path={`${path}.${key}`}
                node={child}
                depth={depth + 1}
                value={current[key]}
                note={undefined}
                delegated={false}
                autofill={undefined}
                attached={undefined}
                onChange={(next) => {
                  const merged = { ...current }
                  if (next === null) delete merged[key]
                  else merged[key] = next
                  onChange(merged)
                }}
              />
            </div>
          ))}
        </div>
      </div>
    )
  }

  if (kind === 'list' && rowFields(node)) {
    return (
      <RowsField
        {...props}
        rows={rowFields(node) as Record<string, SchemaNode>}
        value={Array.isArray(value) ? (value as Array<Record<string, unknown>>) : []}
        onChange={onChange}
      />
    )
  }

  if (isScalarList(node)) {
    return <TagsField {...props} value={Array.isArray(value) ? value : []} kind={itemType(node)} onChange={onChange} />
  }

  if (kind === 'dict' || kind === 'file' || kind === 'template_list') {
    return <JsonField {...props} value={value} onChange={onChange} />
  }

  const literal = value === undefined || value === null ? '' : String(value)
  const options = (node.options ?? null) as string[] | null
  const special = String(node._special ?? '')
  const vendorOptions = special && choices ? choices[special] ?? [] : []

  return (
    <div class={fieldCard(depth, flat)}>
      <Label node={node} value={value} note={note} />
      {delegated ? (
        <Note>{note?.text ?? '此项请在专用面板里编辑。'}</Note>
      ) : (
        <div class="flex flex-col gap-2">
          {kind === 'bool' ? (
            <Switch checked={Boolean(value ?? node.default ?? false)} onChange={(next) => onChange(next)} />
          ) : kind === 'text' ? (
            <Textarea value={literal} onInput={(next) => onChange(next)} rows={literal.length > 120 ? 8 : 3} />
          ) : special && vendorOptions.length ? (
            <Select
              value={literal}
              onChange={(next) => onChange(next)}
              options={vendorOptions}
            />
          ) : options?.length ? (
            <Select
              value={literal}
              onChange={(next) => onChange(next)}
              // 候选项可能是数字（如 `vision.max_image_dimension: [0,512,768,1024]`）：
              // 值原样带类型传回去，标签字符串化给眼睛看。
              options={options.map((option) => ({ value: option, label: String(option) }))}
            />
          ) : (
            <Input
              value={literal}
              type={kind === 'int' || kind === 'float' ? 'number' : 'text'}
              placeholder={node.default === undefined || node.default === null ? '' : `默认 ${String(node.default)}`}
              onInput={(next) => {
                if (kind === 'int' || kind === 'float') onChange(toNumber(kind, next))
                else onChange(next)
              }}
            />
          )}
          <NoteLine note={note} />
        </div>
      )}
    </div>
  )
}

/** 对象行列表：一行一张卡片，字段按行 schema 渲染（白名单就是这一类）。 */
function RowsField({
  rows,
  value,
  onChange,
  node,
  note,
  autofill,
  attached,
  path,
}: Omit<SchemaFieldProps, 'value' | 'onChange'> & {
  rows: Record<string, SchemaNode>
  value: Array<Record<string, unknown>>
  onChange: (next: unknown) => void
}) {
  const rowKeys = Object.keys(rows)
  const add = () => {
    const blank: Record<string, unknown> = {}
    for (const [key, spec] of Object.entries(rows)) {
      if (spec.default !== undefined) blank[key] = spec.default
      else if (spec.type === 'bool') blank[key] = true
      else if (spec.type === 'int' || spec.type === 'float') blank[key] = 0
      else blank[key] = ''
    }
    onChange([...value, blank])
  }
  const patch = (index: number, key: string, next: unknown) => {
    const copy = value.map((row, i) => (i === index ? { ...row, [key]: next } : row))
    onChange(copy)
  }
  const remove = (index: number) => onChange(value.filter((_row, i) => i !== index))

  return (
    <div class={`${fieldCard(0)} gap-3`}>
      <Label node={node} value={value.length ? value : undefined} note={note} />
      {attached && (
        <div class="flex items-center gap-2">
          <Switch
            checked={Boolean(attached.value ?? attached.node.default ?? false)}
            onChange={(next) => attached.onChange(next)}
            label={String(attached.node.description ?? '')}
          />
          <span class="text-[11px]">{String(attached.node.description ?? '')}</span>
        </div>
      )}
      <NoteLine note={note} />
      {value.map((row, index) => (
        <div key={index} class="rounded-lg border border-line bg-panel p-3">
          <div class="mb-2 flex items-center gap-2">
            <span class="text-[11px] font-medium text-muted">第 {index + 1} 行</span>
            <div class="ml-auto flex items-center gap-1">
              {autofill && (
                <Button
                  onClick={async () => {
                    const filled = await autofill.run()
                    if (!filled) return
                    const next = { ...row }
                    for (const [key, item] of Object.entries(filled)) {
                      if (next[key] === undefined || next[key] === '' || next[key] === null) next[key] = item
                    }
                    onChange(value.map((item, i) => (i === index ? next : item)))
                  }}
                >
                  {autofill.label}
                </Button>
              )}
              <Button variant="danger" icon="close" onClick={() => remove(index)}>
                删除
              </Button>
            </div>
          </div>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {rowKeys.map((key) => (
              <div key={key} class={rows[key].type === 'text' || rows[key].type === 'object' ? 'sm:col-span-2' : ''}>
                <SchemaField
                  node={rows[key]}
                  path={`${path}[].${key}`}
                  value={row[key]}
                  onChange={(next) => patch(index, key, next)}
                  choices={undefined}
                  flat
                />
              </div>
            ))}
          </div>
        </div>
      ))}
      {!value.length && <p class="text-[11px] text-muted">还没有条目。</p>}
      <div>
        <Button icon="save" onClick={add}>
          添加一行
        </Button>
      </div>
    </div>
  )
}

/** 标量列表：标签 + 输入框（`int` / `float` 会转成数字）。 */
function TagsField({
  value,
  onChange,
  node,
  kind,
}: Omit<SchemaFieldProps, 'value' | 'onChange'> & {
  kind: string
  value: unknown[]
  onChange: (next: unknown) => void
}) {
  const [draft, setDraft] = useState('')
  const options = (node.options ?? null) as string[] | null
  const isNumber = kind === 'int' || kind === 'float'
  const commit = () => {
    const text = draft.trim()
    if (!text) return
    const next = isNumber ? toNumber(kind, text) : text
    if (next === null) return
    if (!value.includes(next)) onChange([...value, next])
    setDraft('')
  }
  return (
    <div class={fieldCard(0)}>
      <Label node={node} value={value.length ? value : undefined} />
      <div class="flex flex-wrap items-center gap-1.5">
        {value.map((item, index) => (
          <span key={`${String(item)}-${index}`} class="inline-flex items-center gap-1 rounded-md border border-line bg-raised px-1.5 py-0.5 text-[11px]">
            {String(item)}
            <button
              type="button"
              class="text-muted hover:text-danger"
              onClick={() => onChange(value.filter((_entry, i) => i !== index))}
            >
              ×
            </button>
          </span>
        ))}
        {!value.length && <span class="text-[11px] text-muted">空</span>}
      </div>
      {options?.length ? (
        <Select
          value=""
          onChange={(next) => {
            if (next && !value.includes(next)) onChange([...value, next])
          }}
          options={[{ value: '', label: '选择后添加…' }, ...options.map((option) => ({ value: option, label: option }))]}
        />
      ) : (
        <div class="flex items-center gap-1.5">
          <Input
            value={draft}
            type={isNumber ? 'number' : 'text'}
            placeholder={isNumber ? '输入数字后回车' : '输入后回车'}
            onInput={setDraft}
          />
          <Button onClick={commit} disabled={!draft.trim()}>添加</Button>
        </div>
      )}
    </div>
  )
}

/** JSON 兜底编辑器：`dict` / 复杂的列表 / 未来类型。 */
function JsonField({ value, onChange, node }: SchemaFieldProps): ComponentChildren {
  const text = JSON.stringify(value ?? node.default ?? null, null, 2)
  return (
    <div class={fieldCard(0)}>
      <Label node={node} value={value} />
      <Textarea
        mono
        rows={Math.min(18, Math.max(4, text.split('\n').length))}
        value={text}
        onInput={(next) => {
          try {
            onChange(JSON.parse(next))
          } catch {
            /* 还没写完整：保持上一次的有效值，不打断输入 */
          }
        }}
      />
    </div>
  )
}

/** 分组卡片：一个配置分组 = 一张卡。 */
export function SchemaGroupCard({
  title,
  description,
  actions,
  children,
}: {
  title: string
  description: string
  actions?: ComponentChildren
  children: ComponentChildren
}) {
  return (
    <section class="rounded-xl border border-line bg-panel">
      <header class="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <h3 class="text-sm font-semibold">{title}</h3>
        {description && <span class="prose-body text-[11px] text-muted">{description}</span>}
        <div class="ml-auto flex items-center gap-2">{actions}</div>
      </header>
      <div class="flex flex-col gap-4 p-4">{children}</div>
    </section>
  )
}
