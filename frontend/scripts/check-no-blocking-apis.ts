/**
 * 浏览器能力红线：插件页跑在宿主 iframe 的 `sandbox` 里（没有 `allow-same-origin`/
 * `allow-modals`），下面这些 API 在真机上**要么被静默忽略、要么直接抛**：
 *
 * | API | 沙箱里的真实表现（实测） |
 * | --- | --- |
 * | `window.confirm` / `alert` / `prompt` | 被静默忽略：不弹框、返回 false → 按钮点了没反应 |
 * | `window.open` | 被拦（没有 `allow-popups`），返回 null |
 * | `localStorage` / `sessionStorage` | 访问就抛 `SecurityError`（opaque origin） |
 * | `document.cookie` | 写不进也读不到（opaque origin 没有 cookie 存储） |
 * | `navigator.clipboard.writeText` | 跨源 iframe 默认没有 `clipboard-write` 权限 → `NotAllowedError`（兜底见 `CopyFallback`） |
 * | `document.execCommand('copy')` | 已废弃，且同一套权限策略下同样可能被拒 → 不许用 |
 *
 * 所以危险操作一律用就地二次确认（`components/ui.tsx` 的 `ConfirmButton`），
 * 需要持久化就交给后端。这个脚本把"别写回去"钉住——写错一次就是线上死键。
 *
 *     cd plugin/frontend && pnpm test:unit
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')

const BANNED: Array<{ pattern: RegExp; why: string }> = [
  { pattern: /\bwindow\s*\.\s*confirm\s*\(/, why: '沙箱里被静默忽略（点了没反应）→ 用 ConfirmButton 就地二次确认' },
  { pattern: /\bwindow\s*\.\s*alert\s*\(/, why: '沙箱里被静默忽略 → 用 Note / ErrorNote 在页面里说' },
  { pattern: /\bwindow\s*\.\s*prompt\s*\(/, why: '沙箱里被静默忽略 → 用 Input / Textarea' },
  { pattern: /\bwindow\s*\.\s*open\s*\(/, why: '沙箱里被拦（没有 allow-popups）→ 用 <a target="_blank"> 或下载' },
  { pattern: /\b(?:local|session)Storage\b/, why: 'opaque origin 下访问就抛 SecurityError → 状态交给后端或 location.hash' },
  { pattern: /\bdocument\s*\.\s*cookie\b/, why: 'opaque origin 没有 cookie 存储，读不到也写不进' },
  {
    pattern: /\bexecCommand\s*\(/,
    why: '已废弃；沙箱里的剪贴板权限策略同样可能拒 → 用只读 textarea + 让用户 Ctrl+C（CopyFallback）',
  },
]

function sourceFiles(dir: string): string[] {
  const found: string[] = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) found.push(...sourceFiles(path))
    else if (/\.(ts|tsx)$/.test(name)) found.push(path)
  }
  return found
}

/** 去掉注释与字符串字面量：注释里讲到这些 API 是说明，不是调用。 */
function stripCommentsAndStrings(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1 ')
    .replace(/'(?:[^'\\\n]|\\.)*'/g, "''")
    .replace(/"(?:[^"\\\n]|\\.)*"/g, '""')
    .replace(/`(?:[^`\\]|\\.)*`/g, '``')
}

const problems: string[] = []
let checked = 0
for (const file of sourceFiles(join(root, 'src'))) {
  const code = stripCommentsAndStrings(readFileSync(file, 'utf8'))
  for (const rule of BANNED) {
    if (rule.pattern.test(code)) {
      problems.push(`${file.replace(root + '/', '')}: ${rule.pattern.source} —— ${rule.why}`)
    }
  }
  checked += 1
}

if (problems.length) {
  console.error('插件页里出现了沙箱里不好使的浏览器 API：')
  for (const line of problems) console.error('  ' + line)
  process.exit(1)
}
console.log(`no-blocking-apis ok（扫了 ${checked} 个源文件：confirm/alert/prompt/open/storage/cookie/execCommand 全清）`)
