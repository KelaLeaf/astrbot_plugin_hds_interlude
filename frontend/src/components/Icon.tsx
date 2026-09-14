/**
 * 图标组件。图标数据来自 `src/icons.ts`（由 `scripts/fetch-icons.mjs` 生成，
 * 源：Game-Icon-Pack，CC0-1.0）。
 *
 * 图标本来就是 `fill="currentColor"` 的单色路径，所以尺寸交给 class、
 * 颜色继承文字色——明暗主题零成本跟随。
 */
import { ICONS, type IconName } from '../icons'

export function Icon({
  name,
  class: className = 'h-4 w-4',
}: {
  name: IconName
  class?: string
}) {
  return (
    <svg
      viewBox="0 0 10 10"
      fill="currentColor"
      aria-hidden="true"
      class={`shrink-0 ${className}`}
      dangerouslySetInnerHTML={{ __html: ICONS[name] }}
    />
  )
}

export type { IconName }
