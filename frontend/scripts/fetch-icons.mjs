#!/usr/bin/env node
/**
 * 从 Game-Icon-Pack 抓取控制台用到的图标，生成 `src/icons.ts`。
 *
 * - 图标包：https://github.com/Nieobie/Game-Icon-Pack
 * - 许可：**CC0-1.0（公共领域）** —— 可以放心 vendor 进本仓库，无署名义务。
 * - 只取用到的几十个（整包 815 个约 3.4MB），每个 SVG 都是
 *   `viewBox="0 0 10 10"` + `fill="currentColor"` 的单色图标，480–1700 字节，
 *   合起来不到 30KB（gzip 后个位数 KB）。
 * - 生成的是**内联组件**而不是 sprite 文件：少一次请求，且产物里没有
 *   需要宿主重写的外链。
 *
 * 用法：`node scripts/fetch-icons.mjs`（需要联网；只在改图标时跑）
 */
import { writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const RAW = 'https://raw.githubusercontent.com/Nieobie/game-icon-pack/main'

/** 控制台需要的图标：本地键名 → 图标包里的路径。 */
const ICONS = {
  // 侧边导航
  overview: 'svg/padding/6-buildings/house.svg',
  models: 'svg/padding/9-media/microchip.svg',
  script: 'svg/padding/2-items/book.svg',
  memory: 'svg/padding/1-game/heart.svg',
  database: 'svg/padding/9-media/pc-host.svg',
  logs: 'svg/padding/9-media/document.svg',
  config: 'svg/padding/8-ui/settings-02.svg',
  // 状态与动作
  refresh: 'svg/padding/8-ui/refresh.svg',
  clock: 'svg/padding/9-media/clock.svg',
  calendar: 'svg/padding/9-media/calendar.svg',
  upload: 'svg/padding/9-media/upload.svg',
  download: 'svg/padding/9-media/cloud-download.svg',
  save: 'svg/padding/8-ui/save.svg',
  tick: 'svg/padding/8-ui/tick.svg',
  info: 'svg/padding/8-ui/info-02.svg',
  warning: 'svg/padding/8-ui/exclamation-02.svg',
  close: 'svg/padding/8-ui/cross.svg',
  search: 'svg/padding/8-ui/search.svg',
  filter: 'svg/padding/2-items/funnel.svg',
  link: 'svg/padding/9-media/link.svg',
  code: 'svg/padding/9-media/code.svg',
  disk: 'svg/padding/9-media/flash-disk.svg',
  // 叙事相关
  user: 'svg/padding/8-ui/user.svg',
  star: 'svg/padding/4-nature/star.svg',
  fire: 'svg/padding/4-nature/fire.svg',
  compass: 'svg/padding/2-items/compass.svg',
  tag: 'svg/padding/9-media/tag.svg',
  light: 'svg/padding/2-items/light.svg',
  shield: 'svg/padding/3-gear/shield.svg',
  image: 'svg/padding/9-media/image.svg',
}

const here = dirname(fileURLToPath(import.meta.url))
const target = join(here, '..', 'src', 'icons.ts')

/** 取 SVG 的内层标记（丢掉最外层 `<svg>`，只留 children）。 */
function inner(markup) {
  const match = markup.match(/<svg[^>]*>([\s\S]*?)<\/svg>/i)
  if (!match) throw new Error('不是合法 SVG')
  return match[1].replace(/\s+/g, ' ').trim()
}

const entries = []
for (const [name, path] of Object.entries(ICONS)) {
  const response = await fetch(`${RAW}/${path}`)
  if (!response.ok) throw new Error(`${path} → HTTP ${response.status}`)
  entries.push([name, inner(await response.text())])
  process.stdout.write('.')
}
process.stdout.write('\n')

const body = entries
  .map(([name, markup]) => `  ${name}: '${markup.replace(/'/g, "\\'")}',`)
  .join('\n')

const banner = `/**
 * 由 \`scripts/fetch-icons.mjs\` 生成，**不要手改**。
 *
 * 图标来源：Game-Icon-Pack（https://github.com/Nieobie/Game-Icon-Pack）
 * 许可：CC0-1.0（公共领域，无署名义务）
 *
 * 每个值都是 <svg viewBox="0 0 10 10" fill="currentColor"> 的内层标记，
 * 由 \`components/Icon.tsx\` 包成组件。
 */
`

await writeFile(
  target,
  `${banner}export const ICONS = {\n${body}\n} as const\n\nexport type IconName = keyof typeof ICONS\n`,
  'utf8',
)
console.log(`已写入 ${target}（${entries.length} 个图标）`)
