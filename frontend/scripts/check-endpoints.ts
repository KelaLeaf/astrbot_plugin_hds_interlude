/**
 * 端点对账：前端写死的 `console/<名字>` 必须在 `main.py` 的路由表里真的注册过。
 *
 * 控制台每个面板都是"字符串拼端点"，写错一个字母不会报错，只会在运行时 404
 * （而且要等用户点开那个面板才发现）。这里把两侧的字符串对上，改一处忘了另一处就会红。
 *
 *     cd plugin/frontend && pnpm test:unit
 */
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')

function sourceFiles(dir: string): string[] {
  const found: string[] = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) found.push(...sourceFiles(path))
    else if (/\.(ts|tsx)$/.test(name)) found.push(path)
  }
  return found
}

const used = new Set<string>()
for (const file of sourceFiles(join(root, 'src'))) {
  const text = readFileSync(file, 'utf8')
  for (const match of text.matchAll(/['"`]console\/([a-z0-9-]+)/g)) used.add(match[1])
}

const backend = readFileSync(resolve(root, '..', 'main.py'), 'utf8')
const registered = new Set<string>()
for (const match of backend.matchAll(/console\/([a-z0-9-]+)/g)) registered.add(match[1])

assert.ok(used.size >= 8, `只找到 ${used.size} 个前端端点，正则大概失效了`)
const missing = [...used].filter((name) => !registered.has(name)).sort()
assert.deepEqual(missing, [], `这些端点在 main.py 里没有注册：${missing.join(', ')}`)

// 反向：注册了却没人用的控制台只读端点，往往意味着面板改名后忘了删。
const unused = [...registered].filter((name) => !used.has(name)).sort()
assert.deepEqual(unused, [], `这些端点注册了但前端没用：${unused.join(', ')}`)

console.log(`endpoints ok（${used.size} 个端点在两侧对上）`)
