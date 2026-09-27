/**
 * `Note` 自带图标，里面再写一个 `<Icon>` 就会并排出现两个一样的记号（真机上看是「两个感叹号」）。
 * 这个检查扫源码，凡是 `<Note>…</Note>` 里出现 `<Icon` 就报出来。
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const roots = ['src/panels', 'src/components']
let checked = 0
const bad: string[] = []

for (const root of roots) {
  for (const name of readdirSync(root)) {
    if (!name.endsWith('.tsx')) continue
    const file = join(root, name)
    const source = readFileSync(file, 'utf8')
    const blocks = source.match(/<Note[\s>][\s\S]*?<\/Note>/g) ?? []
    for (const block of blocks) {
      checked += 1
      if (/<Icon\b/.test(block)) bad.push(`${file}: ${block.split('\n')[0].trim()}`)
    }
  }
}

if (bad.length) {
  console.error('Note 里又写了图标：')
  for (const line of bad) console.error('  ' + line)
  process.exit(1)
}
console.log(`note-icons ok（${checked} 个 Note 块都没有重复图标）`)
