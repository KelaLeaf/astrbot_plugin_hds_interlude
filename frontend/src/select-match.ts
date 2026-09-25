/**
 * 下拉（`components/ui.tsx` 的 `Select`）的两个纯判定，单独放一个**零依赖**模块。
 *
 * 为什么要单独拆出来：控制台前端没有测试框架，但这两个判定踩过一次真 bug——
 * schema 里有带候选项的 **int** 字段（`model_center.vision.max_image_dimension: [0,512,768,1024]`），
 * 自绘下拉写成 `option.value === value` 时 `1024 === '1024'` 为假 → 匹配不到候选项、
 * 按钮退回占位符，用户看到的就是「配置项不显示当前配置内容」。
 * 拆成纯函数后 `frontend/scripts/check-select.ts` 可以用 `node --experimental-strip-types`
 * 直接跑（见 `package.json` 的 `test:unit`），不引任何依赖。
 */

export type SelectValue = string | number

/** 候选项与当前值是否同一项：**按字符串比较**（数字选项 vs 字符串化当前值）。 */
export function selectMatches(optionValue: SelectValue, value: SelectValue | null | undefined): boolean {
  if (value === null || value === undefined) return false
  return String(optionValue) === String(value)
}

/** 按钮上显示什么：命中候选项用它的 label；否则显示当前值本身；真的没有值才用占位符。 */
export function selectDisplay(
  optionLabel: string | null | undefined,
  value: SelectValue | null | undefined,
  placeholder: string,
): string {
  if (optionLabel) return optionLabel
  if (value === '' || value === null || value === undefined) return placeholder
  return String(value)
}
