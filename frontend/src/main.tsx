/**
 * 控制台外壳：侧边导航 + 顶栏 + 面板区。
 *
 * 用 Preact（3KB）而不是 React：这个页面每装一个 AstrBot 用户就多一份静态资源，
 * 实测同样的面板外壳 HeroUI/React 版是 153KB JS + 39KB CSS（gzip），
 * 本方案 6KB + 2KB。组件复杂度用不上组件库。
 */
import { render } from 'preact'
import { useEffect, useState } from 'preact/hooks'
import './style.css'
import { apiPost, bootstrap, onContext, translate, type BridgeContext } from './bridge'
import { useQuery } from './query'
import { Icon, type IconName } from './components/Icon'
import { Button, Select } from './components/ui'
import { canMergeStory, storyLabel } from './story-label'
import type { StoryListPayload } from './types'
import { Overview } from './panels/Overview'
import { Models } from './panels/Models'
import { Script } from './panels/Script'
import { Memory } from './panels/Memory'
import { Database } from './panels/Database'
import { Logs } from './panels/Logs'
import { Config } from './panels/Config'
import { Alter } from './panels/Alter'
import { Agency } from './panels/Agency'
import { Delivery } from './panels/Delivery'

type PanelKey =
  | 'overview' | 'models' | 'script' | 'memory'
  | 'alter' | 'agency' | 'delivery'
  | 'database' | 'logs' | 'config'

const NAV: Array<{ key: PanelKey; icon: IconName; label: string; fallback: string }> = [
  { key: 'overview', icon: 'overview', label: '总览', fallback: '总览' },
  { key: 'models', icon: 'models', label: '模型', fallback: '模型' },
  { key: 'script', icon: 'script', label: '剧本', fallback: '剧本' },
  { key: 'memory', icon: 'memory', label: '记忆', fallback: '记忆' },
  { key: 'alter', icon: 'star', label: '情绪', fallback: '情绪' },
  { key: 'agency', icon: 'compass', label: '日程', fallback: '日程' },
  { key: 'delivery', icon: 'link', label: '投递', fallback: '投递' },
  { key: 'database', icon: 'database', label: '数据库', fallback: '数据库' },
  { key: 'logs', icon: 'logs', label: '日志', fallback: '日志' },
  { key: 'config', icon: 'config', label: '配置', fallback: '配置' },
]

/** 面板之间共享的两个状态：当前故事、当前面板。 */
export interface PanelProps {
  storyId: string
  onStoryChange: (id: string) => void
  onNavigate: (panel: PanelKey) => void
  /** 顶栏「刷新」的计数：面板把它交给 `useQuery` 的 nonce 就会重新取数。 */
  refreshKey: number
}

function App() {
  const [panel, setPanel] = useState<PanelKey>(() => (location.hash.slice(1) as PanelKey) || 'overview')
  const [storyId, setStoryId] = useState('')
  const [theme, setTheme] = useState<string>('light')
  const [ready, setReady] = useState(false)
  const [fatal, setFatal] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const [merging, setMerging] = useState(false)
  const [mergeError, setMergeError] = useState('')
  // 顶栏的剧本切换器：库里的全部剧本（含归档）。面板默认显示"最近更新的那一部"，
  // 没有这个入口时，新用户一发消息界面就跳走，看起来像"前面的剧本没了"。
  const storyList = useQuery<StoryListPayload>('console/stories', undefined, { nonce: refreshKey })
  const stories = storyList.data?.stories ?? []
  const canonical = storyList.data?.canonical ?? ''
  const selected = stories.find((item) => item.id === storyId) ?? stories[0]
  const storyKey = stories.map((item) => item.id).join('|')

  // 选中的剧本被清理/合并掉之后，回到"最近更新的那一部"，别让面板继续查一个不存在的 id。
  useEffect(() => {
    if (!storyId || !storyKey) return
    if (!stories.some((item) => item.id === storyId)) setStoryId('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storyKey, storyId])

  async function mergeSelected() {
    if (!selected || !canonical || !canMergeStory(selected, canonical)) return
    const ok = window.confirm(
      `把「${storyLabel(selected)}」并入共享主剧本？\n\n`
      + '它的剧本条目、记忆、事实、场景会搬进主剧本，原剧本转为「已归档」（内容不会删除）。',
    )
    if (!ok) return
    setMerging(true)
    setMergeError('')
    try {
      await apiPost<StoryListPayload>('console/story-merge', {
        source_story_id: selected.id,
        target_story_id: canonical,
      })
      setStoryId(canonical)
      setRefreshKey((value) => value + 1)
    } catch (error) {
      setMergeError(error instanceof Error ? error.message : String(error))
    } finally {
      setMerging(false)
    }
  }

  useEffect(() => {
    let unsubscribe = () => {}
    bootstrap()
      .then((context: BridgeContext) => {
        applyTheme(context)
        unsubscribe = onContext(applyTheme)
        setReady(true)
      })
      .catch((error: unknown) => setFatal(error instanceof Error ? error.message : String(error)))
    return () => unsubscribe()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    location.hash = panel
  }, [panel])

  function applyTheme(context: BridgeContext) {
    setTheme(context?.isDark ? 'dark' : 'light')
    // 宿主已经改了 <html data-theme>，这里只是让顶栏的图标跟着换
  }

  const active = NAV.find((item) => item.key === panel) ?? NAV[0]
  const props: PanelProps = {
    storyId,
    onStoryChange: setStoryId,
    onNavigate: setPanel,
    refreshKey,
  }

  return (
    <div class="flex min-h-screen bg-bg text-fg">
      <aside class="flex w-48 shrink-0 flex-col border-r border-line bg-panel">
        <div class="flex items-center gap-2 border-b border-line px-4 py-3">
          <Icon name="star" class="h-5 w-5 text-accent" />
          <div class="min-w-0">
            <div class="truncate text-sm font-semibold">HDS Interlude</div>
            <div class="truncate text-[11px] text-muted">幕间控制台</div>
          </div>
        </div>
        <nav class="flex flex-1 flex-col gap-1 p-2">
          {NAV.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => setPanel(item.key)}
              class={`flex items-center gap-2 rounded-lg px-3 py-2 text-left text-xs transition ${
                item.key === panel ? 'bg-accent/10 font-medium text-accent' : 'text-muted hover:bg-raised hover:text-fg'
              }`}
            >
              <Icon name={item.icon} class="h-4 w-4" />
              {translate(`pages.console.nav.${item.key}`, item.label)}
            </button>
          ))}
        </nav>
        <div class="border-t border-line px-4 py-2 text-[11px] text-muted">
          <Icon name={theme === 'dark' ? 'shield' : 'light'} class="mr-1 inline h-3 w-3" />
          {theme === 'dark' ? '深色' : '浅色'}主题
        </div>
      </aside>

      <main class="flex min-w-0 flex-1 flex-col">
        <header class="flex items-center gap-3 border-b border-line bg-panel px-6 py-3">
          <Icon name={active.icon} class="h-4 w-4 text-muted" />
          <h1 class="text-sm font-semibold">{translate(`pages.console.nav.${active.key}`, active.fallback)}</h1>
          <div class="ml-auto flex items-center gap-2">
            {stories.length > 1 ? (
              <div class="w-72">
                <Select
                  value={selected ? selected.id : ''}
                  onChange={(next) => setStoryId(String(next))}
                  options={stories.map((item) => ({ value: item.id, label: storyLabel(item) }))}
                  placeholder="选择剧本…"
                />
              </div>
            ) : null}
            {canMergeStory(selected, canonical) ? (
              <Button icon="link" disabled={merging} onClick={mergeSelected}>
                {merging ? '并入中…' : '并入主剧本'}
              </Button>
            ) : null}
            <Button icon="refresh" onClick={() => setRefreshKey((value) => value + 1)}>
              刷新
            </Button>
          </div>
        </header>

        {mergeError ? (
          <div class="border-b border-danger/40 bg-danger/10 px-6 py-2 text-[11px] text-danger">
            {mergeError}
          </div>
        ) : null}

        <div class="min-w-0 flex-1 p-6">
          {fatal ? (
            <div class="rounded-xl border border-danger/40 bg-danger/10 px-4 py-3 text-xs text-danger">{fatal}</div>
          ) : !ready ? (
            <div class="text-xs text-muted">正在连接宿主…</div>
          ) : panel === 'overview' ? (
            <Overview {...props} />
          ) : panel === 'models' ? (
            <Models {...props} />
          ) : panel === 'script' ? (
            <Script {...props} />
          ) : panel === 'memory' ? (
            <Memory {...props} />
          ) : panel === 'alter' ? (
            <Alter {...props} />
          ) : panel === 'agency' ? (
            <Agency {...props} />
          ) : panel === 'delivery' ? (
            <Delivery {...props} />
          ) : panel === 'database' ? (
            <Database {...props} />
          ) : panel === 'logs' ? (
            <Logs {...props} />
          ) : (
            <Config {...props} />
          )}
        </div>
      </main>
    </div>
  )
}

render(<App />, document.getElementById('app')!)
