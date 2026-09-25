/**
 * 控制台的基础组件。**刻意手写、刻意很小**：整套加起来几百行，
 * 换掉的是 React + react-aria + HeroUI 约 190KB（gzip）的依赖闭包
 * （实测：同样的面板外壳，HeroUI 版 JS 153KB + CSS 39KB，本方案 JS 6KB + CSS 2KB）。
 *
 * 组件只做「排版 + 语义色」，不带状态机、不引外部依赖；样式全走 Tailwind 工具类
 * 与 `style.css` 里的语义色板。
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import type { ComponentChildren } from 'preact'
import { Icon, type IconName } from './Icon'

export { Icon, type IconName }

/* ------------------------------------------------------------------ 容器 */

export function Panel({
  title,
  icon,
  actions,
  children,
  class: className = '',
}: {
  title?: string
  icon?: IconName
  actions?: ComponentChildren
  children: ComponentChildren
  class?: string
}) {
  return (
    <section class={`rounded-xl border border-line bg-panel shadow-sm ${className}`}>
      {(title || actions) && (
        <header class="flex items-center gap-2 border-b border-line px-4 py-3">
          {icon && <Icon name={icon} class="h-4 w-4 text-muted" />}
          {title && <h2 class="text-sm font-semibold">{title}</h2>}
          <div class="ml-auto flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div class="p-4">{children}</div>
    </section>
  )
}

export function Grid({
  cols = 2,
  children,
  class: className = '',
}: {
  cols?: 2 | 3 | 4
  children: ComponentChildren
  class?: string
}) {
  const map = { 2: 'sm:grid-cols-2', 3: 'sm:grid-cols-2 lg:grid-cols-3', 4: 'sm:grid-cols-2 lg:grid-cols-4' }
  return <div class={`grid grid-cols-1 gap-4 ${map[cols]} ${className}`}>{children}</div>
}

export function Stack({ children, class: className = '' }: { children: ComponentChildren; class?: string }) {
  return <div class={`flex flex-col gap-4 ${className}`}>{children}</div>
}

/* ------------------------------------------------------------------ 原子 */

export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ComponentChildren
  tone?: 'neutral' | 'accent' | 'ok' | 'warn' | 'danger'
}) {
  const tones = {
    neutral: 'border-line bg-raised text-muted',
    accent: 'border-accent/40 bg-accent/10 text-accent',
    ok: 'border-ok/40 bg-ok/10 text-ok',
    warn: 'border-warn/40 bg-warn/10 text-warn',
    danger: 'border-danger/40 bg-danger/10 text-danger',
  }
  return (
    <span
      class={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] leading-4 ${tones[tone]}`}
    >
      {children}
    </span>
  )
}

export function Button({
  children,
  onClick,
  variant = 'default',
  disabled,
  icon,
  type = 'button',
}: {
  children?: ComponentChildren
  onClick?: () => void
  variant?: 'default' | 'primary' | 'danger'
  disabled?: boolean
  icon?: IconName
  type?: 'button' | 'submit'
}) {
  const variants = {
    default: 'border-line bg-panel hover:bg-raised text-fg',
    primary: 'border-accent bg-accent text-accent-fg hover:opacity-90',
    danger: 'border-danger/50 bg-panel text-danger hover:bg-danger/10',
  }
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      class={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${variants[variant]}`}
    >
      {icon && <Icon name={icon} class="h-3.5 w-3.5" />}
      {children}
    </button>
  )
}

/** 开关：控制台里所有布尔设置都用它（受控，禁用态会变灰）。 */
export function Switch({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  label?: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      class={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition disabled:cursor-not-allowed disabled:opacity-50 ${
        checked ? 'border-accent bg-accent' : 'border-line bg-raised'
      }`}
    >
      <span
        class={`absolute h-3.5 w-3.5 rounded-full bg-panel shadow transition-all ${
          checked ? 'left-[1.15rem]' : 'left-[0.15rem]'
        }`}
      />
    </button>
  )
}

/** 受控文本框：连接池编辑表单里用。 */
export function Field({
  label,
  hint,
  children,
  class: className = '',
}: {
  label: string
  hint?: string
  children: ComponentChildren
  class?: string
}) {
  return (
    <label class={`flex flex-col gap-1 ${className}`}>
      <span class="text-[11px] font-medium text-muted">{label}</span>
      {children}
      {hint && <span class="text-[11px] text-muted">{hint}</span>}
    </label>
  )
}

/** 统一样式的输入控件。用原生元素，样式靠 Tailwind——不引表单库。 */
export function Input({
  value,
  onInput,
  type = 'text',
  placeholder,
  disabled,
}: {
  value: string | number
  onInput: (next: string) => void
  type?: 'text' | 'number' | 'password'
  placeholder?: string
  disabled?: boolean
}) {
  return (
    <input
      type={type}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      onInput={(event) => onInput((event.currentTarget as HTMLInputElement).value)}
      class="w-full rounded-lg border border-line bg-panel px-2 py-1.5 text-xs outline-none transition focus:border-accent disabled:opacity-50"
    />
  )
}

/** 多行文本（配置页的 `text` 字段用；原生 textarea，同样不引表单库）。 */
export function Textarea({
  value,
  onInput,
  rows = 4,
  placeholder,
  disabled,
  mono,
}: {
  value: string
  onInput: (next: string) => void
  rows?: number
  placeholder?: string
  disabled?: boolean
  mono?: boolean
}) {
  return (
    <textarea
      rows={rows}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      onInput={(event) => onInput((event.currentTarget as HTMLTextAreaElement).value)}
      class={`w-full resize-y rounded-lg border border-line bg-panel px-2 py-1.5 text-xs leading-5 outline-none transition focus:border-accent disabled:opacity-50 ${
        mono ? 'font-mono' : ''
      }`}
    />
  )
}

/**
 * 下拉选择：**自己画**，不用原生 `<select>`。
 *
 * 原生 select 展开的那层是操作系统画的，跟整套 UI 完全不搭（用户第一眼就指出了）。
 * 这里用按钮 + 绝对定位面板实现，键盘（↑↓ / Enter / Esc）与点外部关闭都支持；
 * props 与原来的 `Select` 一致，所以调用点不用改。
 */
export function Select({
  value,
  onChange,
  options,
  placeholder = '请选择…',
  disabled,
}: {
  value: string
  onChange: (next: string) => void
  options: Array<{ value: string; label: string }>
  placeholder?: string
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [cursor, setCursor] = useState(0)
  const box = useRef<HTMLDivElement>(null)
  const current = options.find((option) => option.value === value)

  useEffect(() => {
    if (!open) return
    const onPointer = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointer)
    return () => document.removeEventListener('mousedown', onPointer)
  }, [open])

  useEffect(() => {
    if (open) setCursor(Math.max(0, options.findIndex((option) => option.value === value)))
  }, [open, options, value])

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      setOpen(false)
      return
    }
    if (!open && (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown')) {
      event.preventDefault()
      setOpen(true)
      return
    }
    if (!open) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setCursor((index) => Math.min(options.length - 1, index + 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setCursor((index) => Math.max(0, index - 1))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      const picked = options[cursor]
      if (picked) {
        onChange(picked.value)
        setOpen(false)
      }
    }
  }

  return (
    <div ref={box} class="relative" onKeyDown={onKeyDown}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((prev) => !prev)}
        class={`flex w-full items-center gap-2 rounded-lg border px-2 py-1.5 text-left text-xs transition disabled:cursor-not-allowed disabled:opacity-50 ${
          open ? 'border-accent bg-panel' : 'border-line bg-panel hover:bg-raised'
        }`}
      >
        <span class={`min-w-0 flex-1 truncate ${current ? '' : 'text-muted'}`}>
          {current?.label ?? placeholder}
        </span>
        <svg
          viewBox="0 0 10 10"
          class={`h-3 w-3 shrink-0 text-muted transition ${open ? 'rotate-180' : ''}`}
          fill="currentColor"
          aria-hidden="true"
        >
          <path d="M2.2 3.9c.2-.2.5-.2.7 0L5 6l2.1-2.1a.5.5 0 0 1 .7.7L5.35 7.05a.5.5 0 0 1-.7 0L2.2 4.6a.5.5 0 0 1 0-.7" />
        </svg>
      </button>
      {open && (
        <div class="absolute left-0 right-0 z-30 mt-1 max-h-72 overflow-y-auto rounded-xl border border-line bg-panel p-1 shadow-xl">
          {options.length === 0 && <p class="px-2 py-1.5 text-[11px] text-muted">没有可选项</p>}
          {options.map((option, index) => (
            <button
              key={option.value}
              type="button"
              onMouseEnter={() => setCursor(index)}
              onClick={() => {
                onChange(option.value)
                setOpen(false)
              }}
              class={`flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs transition ${
                option.value === value
                  ? 'bg-accent/10 text-accent'
                  : index === cursor
                    ? 'bg-raised'
                    : 'hover:bg-raised'
              }`}
            >
              <span class="min-w-0 flex-1 truncate">{option.label}</span>
              {option.value === value && <Icon name="tick" class="h-3 w-3 shrink-0" />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * 文件选择：**不要**直接用 `<input type="file">`——浏览器原生控件跟整套 UI 格格不入
 * （实测用户第一眼就发现了）。这里把它藏起来，外面套一个能点、能拖的方块。
 */
export function FilePicker({
  onPick,
  accept = '.json,application/json',
  disabled,
  hint,
}: {
  onPick: (file: File) => void
  accept?: string
  disabled?: boolean
  hint?: string
}) {
  const [over, setOver] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => !disabled && input.current?.click()}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') input.current?.click()
      }}
      onDragOver={(event) => {
        event.preventDefault()
        if (!disabled) setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault()
        setOver(false)
        const file = event.dataTransfer?.files?.[0]
        if (file && !disabled) onPick(file)
      }}
      class={`flex cursor-pointer flex-col items-center gap-1 rounded-xl border-2 border-dashed px-4 py-6 text-center transition ${
        over ? 'border-accent bg-accent/5' : 'border-line hover:border-accent/60'
      } ${disabled ? 'cursor-not-allowed opacity-50' : ''}`}
    >
      <Icon name="upload" class="h-5 w-5 text-muted" />
      <span class="text-xs">点击选择文件，或拖到这里</span>
      {hint && <span class="text-[11px] text-muted">{hint}</span>}
      <input
        ref={input}
        type="file"
        accept={accept}
        class="hidden"
        onChange={(event) => {
          const file = (event.currentTarget as HTMLInputElement).files?.[0]
          if (file) onPick(file)
          // 同一个文件连选两次也要能触发 change
          ;(event.currentTarget as HTMLInputElement).value = ''
        }}
      />
    </div>
  )
}

export function Stat({
  label,
  value,
  hint,
  tone = 'neutral',
}: {
  label: string
  value: ComponentChildren
  hint?: string
  tone?: 'neutral' | 'ok' | 'warn' | 'danger'
}) {
  const tones = { neutral: 'text-fg', ok: 'text-ok', warn: 'text-warn', danger: 'text-danger' }
  return (
    <div class="rounded-xl border border-line bg-panel px-4 py-3">
      <div class="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</div>
      <div class={`mt-1 truncate text-lg font-semibold ${tones[tone]}`} title={typeof value === 'string' ? value : undefined}>
        {value}
      </div>
      {hint && <div class="mt-0.5 truncate text-[11px] text-muted" title={hint}>{hint}</div>}
    </div>
  )
}

export function KeyValue({ rows }: { rows: Array<[string, ComponentChildren]> }) {
  return (
    <dl class="grid grid-cols-[minmax(6rem,auto)_1fr] gap-x-4 gap-y-2 text-xs">
      {rows.map(([key, value]) => (
        <div class="contents" key={key}>
          <dt class="text-muted">{key}</dt>
          <dd class="min-w-0 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

/* ------------------------------------------------------------------ 表格 */

export interface Column<T> {
  key: string
  title: string
  render: (row: T) => ComponentChildren
  width?: string
  mono?: boolean
}

export function Table<T>({
  columns,
  rows,
  empty = '暂无数据',
  rowKey,
}: {
  columns: Array<Column<T>>
  rows: T[]
  empty?: string
  rowKey?: (row: T, index: number) => string
}) {
  if (!rows.length) return <Empty text={empty} />
  return (
    <div class="-mx-4 overflow-x-auto px-4">
      <table class="w-full border-collapse text-xs">
        <thead>
          <tr class="border-b border-line text-left text-muted">
            {columns.map((column) => (
              <th
                key={column.key}
                class="whitespace-nowrap px-2 py-2 font-medium"
                style={column.width ? { width: column.width } : undefined}
              >
                {column.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={rowKey ? rowKey(row, index) : index} class="border-b border-line/60 last:border-0 align-top">
              {columns.map((column) => (
                <td key={column.key} class={`px-2 py-2 ${column.mono ? 'font-mono text-[11px]' : ''}`}>
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ------------------------------------------------------------------ 状态 */

export function Empty({ text, icon = 'info' }: { text: string; icon?: IconName }) {
  return (
    <div class="flex flex-col items-center gap-2 py-8 text-center text-xs text-muted">
      <Icon name={icon} class="h-6 w-6 opacity-50" />
      {text}
    </div>
  )
}

export function Loading({ text = '加载中…' }: { text?: string }) {
  return (
    <div class="flex items-center justify-center gap-2 py-8 text-xs text-muted">
      <Icon name="refresh" class="spin h-4 w-4" />
      {text}
    </div>
  )
}

export function ErrorNote({ text, onRetry }: { text: string; onRetry?: () => void }) {
  return (
    <div class="flex items-start gap-2 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
      <Icon name="warning" class="mt-0.5 h-3.5 w-3.5" />
      <span class="prose-body min-w-0 flex-1">{text}</span>
      {onRetry && <Button icon="refresh" onClick={onRetry}>重试</Button>}
    </div>
  )
}

export function Note({ children, tone = 'neutral' }: { children: ComponentChildren; tone?: 'neutral' | 'warn' | 'ok' }) {
  const tones = {
    neutral: 'border-line bg-raised',
    warn: 'border-warn/40 bg-warn/10 text-warn',
    ok: 'border-ok/40 bg-ok/10',
  }
  return (
    <div class={`flex items-start gap-2 rounded-lg border px-3 py-2 text-xs ${tones[tone]}`}>
      <Icon name={tone === 'neutral' ? 'info' : tone === 'ok' ? 'tick' : 'warning'} class="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span class="prose-body min-w-0">{children}</span>
    </div>
  )
}

/** 横向占比条：用于重要度、置信度这类 0–1 的分数。 */
export function Meter({ value, max = 1, tone = 'accent' }: { value: number; max?: number; tone?: 'accent' | 'ok' | 'warn' }) {
  const ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0
  const tones = { accent: 'bg-accent', ok: 'bg-ok', warn: 'bg-warn' }
  return (
    <div class="flex items-center gap-2">
      <div class="h-1.5 w-16 overflow-hidden rounded-full bg-line">
        <div class={`h-full rounded-full ${tones[tone]}`} style={{ width: `${ratio * 100}%` }} />
      </div>
      <span class="font-mono text-[11px] text-muted">{value.toFixed(2)}</span>
    </div>
  )
}
