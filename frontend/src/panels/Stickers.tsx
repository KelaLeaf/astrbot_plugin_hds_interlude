/**
 * 「表情库」面板：她攒下来的本地表情包素材，以及"这张图是什么"那句描述。
 *
 * 为什么这一页值得存在：描述**会进模型可见的素材目录**（`stickerCatalog`）——她挑表情包
 * 靠的就是这句话。所以这一页的核心不是"看"，是**改那句描述**，并且改完立刻生效。
 *
 * 三条界线在界面上必须分得开：
 *
 * 1. **人工写的 vs 自动描述的**：`manual` 为真就是"你手写的"，自动描述不会覆盖它；
 *    「交回自动描述」只在人工写的那条上出现（这条的按钮不是装饰，是唯一的退路）。
 * 2. **移除 vs 彻底删除**：默认那一步只标记（文件和描述都留着、重扫能复活、可恢复）；
 *    `purge=true` 才真删磁盘上的图。两段各有自己的按钮与警告文案，
 *    按钮上就写着「可恢复 / 不可恢复」。
 * 3. **这一页 vs 整个库**：后端一次最多扫 500 行、一次最多回 200 条，所以
 *    `total` / `truncated` / 分页都摆在明面上，别让用户以为库里就这 60 条。
 *
 * 写操作成功后**不整页重拉**（控制台既有做法）：用响应里的 `item` 就地更新那一行。
 * 失败一律照实显示——把 400 吞成空白页是这一页最不该犯的错。
 *
 * 后端接口在 `adapters/console_api.py`（取数）+ `main.py`（路由）；这里只管显示与发请求。
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { apiGet, apiPost, describeError, downloadFile, endpointUrl } from '../bridge'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type {
  StickerCounts, StickerDeleteResult, StickerItem, StickerListPayload, StickerRescanResult,
  StickerRestoreResult, StickerUpdateResult,
} from '../types'
import {
  Badge, Button, Empty, ErrorNote, Grid, Icon, Input, Loading, LongList, Note, Panel, Select,
  Stack, Stat, Switch, Textarea,
} from '../components/ui'
import {
  UnsupportedStickerImage, createStickerImageCache, parseImageEnvelope, revokeStickerImageUrl,
  stickerImageObjectUrl, type StickerImageCache,
} from '../sticker-images'
import {
  DEFAULT_PAGE_SIZE, DESCRIPTION_LIMIT, EMPTY_FILTERS, MARK_DELETE_LABEL, NAME_LIMIT,
  PAGE_SIZE_MAX, PURGE_DELETE_LABEL, activeFilterCount, deleteDoneNote, deletePayload,
  deleteWarning, descriptionOwner, disabledPayload, emptyHint, fileLabel, kindLabel,
  mergeStickerItems, pageWindow, rescanNote, restorePayload, resultSummary, saveBlocker,
  sourceHint, sourceLabel, staleOverridesNote, statusHint, statusLabel, statusTone,
  stickerParams, thumbnailEndpoint, updateNote, updatePayload,
  type StickerDraft, type StickerFilters,
} from '../stickers-view'

/** 状态筛选项（值与后端 `status` 参数逐字一致）。 */
const STATUS_CHIPS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部' },
  { value: 'active', label: '在用' },
  { value: 'pending', label: '待自动描述' },
  { value: 'missing', label: '已移除' },
  { value: 'disabled', label: '已停用' },
]

export function Stickers({ refreshKey, onNavigate }: PanelProps) {
  const [filters, setFilters] = useState<StickerFilters>(EMPTY_FILTERS)
  const [limit, setLimit] = useState<number>(DEFAULT_PAGE_SIZE)
  const [offset, setOffset] = useState(0)
  // 列表里刚被就地更新过的行（assetId → 写操作响应里的 item）。点「刷新」清掉。
  const [overrides, setOverrides] = useState<Record<string, StickerItem>>({})
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  // 搜索框自己的草稿：每次敲键就发一次请求太吵，停 300ms 再进筛选条件。
  const [needle, setNeedle] = useState('')
  // 缩略图字节只能经宿主 bridge 取（沙箱里 <img> 直连必然 401）：见 sticker-images.ts。
  // 缓存随面板生命周期：卸载时把 objectURL 全释放掉。
  const cacheRef = useRef<StickerImageCache | null>(null)
  if (!cacheRef.current) {
    cacheRef.current = createStickerImageCache({
      load: async (assetId) => {
        const value = await apiGet<unknown>('console/sticker-file', { assetId, inline: 1 })
        const payload = parseImageEnvelope(value)
        // 不是 JSON 信封 = 后端还没这条取图路（老后端直接回字节，axios 会解成乱码）。
        if (!payload) throw new UnsupportedStickerImage()
        return payload
      },
      toUrl: stickerImageObjectUrl,
      release: revokeStickerImageUrl,
      concurrency: 4,
    })
  }
  const images = cacheRef.current
  useEffect(() => () => {
    cacheRef.current?.dispose()
  }, [])

  const list = useQuery<StickerListPayload>(
    'console/stickers',
    stickerParams(filters, limit, offset),
    { nonce: refreshKey },
  )
  const payload = list.data
  const total = payload?.total ?? 0
  const pager = pageWindow(total, limit, offset)

  // 偏移超出范围（删了几条之后）时收敛到最后一页：别停在一个永远空的页上。
  useEffect(() => {
    if (pager.start !== offset) setOffset(pager.start)
  }, [pager.start, offset])

  // 换筛选 / 换页 / 点了顶栏刷新：就地更新过的那几行不再覆盖服务器的新数据。
  const filterKey = `${filters.status}|${filters.kind}|${filters.source}|${filters.q}`
  useEffect(() => {
    setOverrides({})
  }, [filterKey, limit, offset, refreshKey])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (needle !== filters.q) patchFilters({ q: needle }, false)
    }, 300)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needle, filters.q])

  function patchFilters(next: Partial<StickerFilters>, resetOffset = true) {
    setFilters((prev) => ({ ...prev, ...next }))
    if (resetOffset) setOffset(0)
  }

  /** 写操作的统一收口：成功就地更新那一行 + 一句人话，失败照实报（返回是否成功）。 */
  async function write<T>(
    key: string,
    endpoint: string,
    body: unknown,
    done: (result: T) => string,
  ): Promise<T | null> {
    setBusy(key)
    setError('')
    setNotice('')
    try {
      const result = await apiPost<T>(endpoint, body)
      // 写操作响应里的 `item` 就是那一行的新样子：就地换掉它，不整页重拉。
      const fresh = (result as { item?: StickerItem } | null)?.item
      if (fresh && fresh.assetId) setOverrides((prev) => ({ ...prev, [fresh.assetId]: fresh }))
      const text = done(result)
      if (text) setNotice(text)
      return result
    } catch (failure) {
      setError(describeError(failure))
      return null
    } finally {
      setBusy('')
    }
  }

  async function saveDescription(item: StickerItem, draft: StickerDraft): Promise<StickerItem | null> {
    const blocker = saveBlocker(draft)
    if (blocker) {
      setError(blocker)
      return null
    }
    const body = updatePayload(item, draft)
    if (!body) {
      setError('没有改动：描述和名字都和现在一样。')
      return null
    }
    const result = await write<StickerUpdateResult>(
      `save-${item.assetId}`, 'console/sticker-update', body, (value) => updateNote(value.changed),
    )
    return result?.item ?? null
  }

  async function restoreDescription(item: StickerItem): Promise<boolean> {
    const body = restorePayload(item)
    if (!body) {
      setError('这条不是人工写的描述，没有东西可以交回。')
      return false
    }
    const result = await write<StickerRestoreResult>(
      `restore-${item.assetId}`, 'console/sticker-restore-description', body,
      (value) => value.hint || '已交回自动描述：下次「重新扫描」会用视觉模型重新描述它',
    )
    return Boolean(result)
  }

  async function toggleDisabled(item: StickerItem, disabled: boolean): Promise<boolean> {
    const result = await write<StickerUpdateResult>(
      `disable-${item.assetId}`, 'console/sticker-update', disabledPayload(item, disabled),
      (value) => (value.item?.disabled
        ? '已停用：她挑素材时不会再选它（文件和描述都留着）'
        : '已启用：她又可以在素材目录里挑到它了'),
    )
    return Boolean(result)
  }

  async function remove(item: StickerItem, purge: boolean): Promise<boolean> {
    const result = await write<StickerDeleteResult>(
      `delete-${item.assetId}`, 'console/sticker-delete', deletePayload(item, purge), deleteDoneNote,
    )
    return Boolean(result)
  }

  /**
   * 把原图交给宿主下载（存到本地）。
   *
   * 缩略图走的是 bridge 取字节（见 `sticker-images.ts`），这个按钮是**另一件事**：
   * 用户想把原图存下来。它走宿主的 `download`——宿主在它自己的页面里带登录态取 blob
   * 再落盘；沙箱 iframe 自己发 `<a download href="…扩展路由">` 是过不了鉴权的
   * （`SameSite=Strict` 的登录 cookie 在 opaque origin 里不会带上 → 401）。
   * 后端还没加 `inline=1` 那条 JSON 取图路时，它也是唯一能看到图的办法。
   */
  async function saveOriginal(item: StickerItem): Promise<void> {
    setBusy(`file-${item.assetId}`)
    setError('')
    setNotice('')
    try {
      await downloadFile('console/sticker-file', { assetId: item.assetId }, fileLabel(item))
      setNotice(`已把原图交给浏览器下载：${fileLabel(item)}`)
    } catch (failure) {
      setError(describeError(failure))
    } finally {
      setBusy('')
    }
  }

  async function rescan(): Promise<void> {
    const result = await write<StickerRescanResult>(
      'rescan', 'console/sticker-rescan', {}, rescanNote,
    )
    if (result) {
      setOverrides({})
      list.reload()
    }
  }

  function refreshAll() {
    setOverrides({})
    setError('')
    setNotice('')
    list.reload()
  }

  if (list.error) return <ErrorNote text={list.error} onRetry={list.reload} />
  if (list.loading && !payload) return <Loading />
  if (!payload) return <Empty text="没有拿到表情库数据" icon="image" />

  const counts: StickerCounts = payload.counts ?? { total: 0, active: 0, pending: 0, missing: 0, disabled: 0 }
  const items = mergeStickerItems(payload.items ?? [], overrides)
  const pendingOverrides = Object.keys(overrides).length
  const locked = Boolean(busy)
  const filtersOn = activeFilterCount(filters)

  return (
    <Stack>
      <Grid cols={4}>
        <Stat label="素材总数" value={counts.total} hint={payload.directory || payload.root || '—'} />
        <Stat
          label="在用"
          value={counts.active}
          tone="ok"
          hint="进了素材目录：她挑表情包时能选到的"
        />
        <Stat
          label="待自动描述"
          value={counts.pending}
          tone={counts.pending > 0 ? 'warn' : 'neutral'}
          hint="还没有描述：重扫时由视觉模型补上"
        />
        <Stat
          label="已移除 / 已停用"
          value={`${counts.missing} / ${counts.disabled}`}
          hint="移除能恢复；停用只是不参与挑选"
        />
      </Grid>

      {!payload.enabled ? (
        <div class="flex flex-wrap items-center gap-2 rounded-xl border border-warn/40 bg-warn/10 px-4 py-3 text-xs text-warn">
          <span class="min-w-0 flex-1">
            本地表情包库没启用：自动收藏与「重新扫描」都不会动，下面看到的是之前收进来的内容。
          </span>
          <Button icon="config" onClick={() => onNavigate('config')}>去配置页打开</Button>
        </div>
      ) : !payload.auto_collect ? (
        <Note tone="warn">
          自动收藏关着：别人发来的表情包不会自动进库，只有你放进表情库目录的文件会被扫进来。
        </Note>
      ) : null}

      {error ? <ErrorNote text={error} /> : null}
      {notice ? <Note tone="ok">{notice}</Note> : null}

      <Panel
        title={`素材（${counts.total}）`}
        icon="image"
        actions={
          <>
            <Button icon="refresh" disabled={locked} onClick={refreshAll}>刷新</Button>
            <Button
              variant="primary"
              icon="search"
              disabled={locked || !payload.enabled}
              title={payload.enabled
                ? '扫一遍表情库目录：新文件入库，缺描述的交给视觉模型'
                : '库总闸关着，重扫会被拒'}
              onClick={rescan}
            >
              {busy === 'rescan' ? '扫描中…' : '重新扫描'}
            </Button>
          </>
        }
      >
        <Stack>
          <div class="flex flex-wrap items-end gap-2">
            <label class="flex min-w-[14rem] flex-1 flex-col gap-1">
              <span class="text-[11px] font-medium text-muted">搜描述 / 名字 / assetId</span>
              <Input
                value={needle}
                onInput={setNeedle}
                placeholder="例如：猫、挥手、sticker-1a2b"
              />
            </label>
            <label class="flex w-32 flex-col gap-1">
              <span class="text-[11px] font-medium text-muted">形式</span>
              <Select
                value={filters.kind}
                onChange={(next) => patchFilters({ kind: String(next) })}
                options={[
                  { value: '', label: '全部形式' },
                  { value: 'image', label: '静止图' },
                  { value: 'animated', label: '动图' },
                ]}
              />
            </label>
            <label class="flex w-36 flex-col gap-1">
              <span class="text-[11px] font-medium text-muted">来源</span>
              <Select
                value={filters.source}
                onChange={(next) => patchFilters({ source: String(next) })}
                options={[
                  { value: '', label: '全部来源' },
                  { value: 'auto', label: '自动收藏' },
                  { value: 'manual', label: '扫盘入库' },
                ]}
              />
            </label>
            <Button
              icon="close"
              disabled={!filtersOn && !needle}
              onClick={() => {
                setNeedle('')
                setFilters(EMPTY_FILTERS)
                setOffset(0)
              }}
            >
              重置筛选
            </Button>
          </div>

          <div class="flex flex-wrap items-center gap-1.5">
            {STATUS_CHIPS.map((chip) => (
              <Button
                key={chip.value || 'all'}
                variant={filters.status === chip.value ? 'primary' : 'default'}
                disabled={locked && chip.value !== filters.status}
                onClick={() => patchFilters({ status: chip.value })}
                title={chip.value ? statusHint(chip.value) : '不筛状态：库里全部素材'}
              >
                {chip.label}（{counts[(chip.value || 'total') as keyof StickerCounts] ?? 0}）
              </Button>
            ))}
            <label class="ml-auto flex items-center gap-2 text-[11px] text-muted">
              每页
              <span class="w-24">
                <Select
                  value={limit}
                  onChange={(next) => {
                    setLimit(Number(next) || DEFAULT_PAGE_SIZE)
                    setOffset(0)
                  }}
                  options={[
                    { value: 30, label: '30 条' },
                    { value: 60, label: '60 条' },
                    { value: PAGE_SIZE_MAX, label: '200 条' },
                  ]}
                />
              </span>
            </label>
          </div>

          <div class="flex flex-wrap items-center gap-2 text-[11px] text-muted">
            <span>{resultSummary({
              total, shown: items.length, limit, offset: pager.start, truncated: payload.truncated,
            })}</span>
            {staleOverridesNote(pendingOverrides) ? (
              <span class="text-warn">{staleOverridesNote(pendingOverrides)}</span>
            ) : null}
          </div>

          {items.length === 0 ? (
            <Empty
              text={emptyHint({
                enabled: payload.enabled,
                autoCollect: payload.auto_collect,
                hasFilters: filtersOn > 0,
                total,
              })}
              icon="image"
            />
          ) : (
            <LongList
              items={items}
              limit={12}
              unit="条"
              alwaysScroll
              class="flex flex-col gap-3"
              render={(row) => (
                <StickerCard
                  key={row.assetId}
                  item={row}
                  images={images}
                  busy={busy}
                  onSave={saveDescription}
                  onRestore={restoreDescription}
                  onToggle={toggleDisabled}
                  onRemove={remove}
                  onSaveFile={saveOriginal}
                />
              )}
            />
          )}

          {total > 0 ? (
            <div class="flex flex-wrap items-center gap-2 text-[11px] text-muted">
              <Button
                disabled={!pager.hasPrev || locked}
                onClick={() => setOffset(Math.max(0, pager.start - pager.size))}
              >
                上一页
              </Button>
              <span>第 {pager.page} / {pager.pages} 页</span>
              <Button
                disabled={!pager.hasNext || locked}
                onClick={() => setOffset(pager.start + pager.size)}
              >
                下一页
              </Button>
              {payload.truncated ? (
                <span class="text-warn">这一批统计只覆盖了最近的素材，更早的没算进来</span>
              ) : null}
            </div>
          ) : null}
        </Stack>
      </Panel>

      <Panel title="这一页在改什么" icon="info">
        <div class="space-y-2 text-xs leading-relaxed text-muted">
          <p>
            <Badge tone="accent">人工写的</Badge> 描述是你手写的，自动描述不会覆盖它；
            <Badge>自动描述</Badge> 是视觉模型写的，你一改它就归你。
            想让它重新自动描述，用「交回自动描述」（只在人工写的那条上出现）。
          </p>
          <p>
            <Badge>移除（可恢复）</Badge> 只把素材从她的可用清单里摘掉，文件和描述都留着，
            重扫能找回来；<Badge tone="danger">彻底删除（不可恢复）</Badge> 会连磁盘上的图片一起删掉。
          </p>
          <p>
            缩略图取不到时用「保存原图」：新版宿主把插件页放在沙箱里，
            页面自己发的图片请求不带登录态，这条取图路会被后端拒掉。
          </p>
          <p>
            描述改完立刻生效：下一次她挑素材时看到的目录就是新的。
            素材目录：<span class="font-mono">{payload.directory || payload.root || '—'}</span>
          </p>
        </div>
      </Panel>
    </Stack>
  )
}

/**
 * 一条素材：缩略图 + 状态/形式/来源/描述作者徽章 + 行内编辑描述与名字 + 停用 / 两段删除。
 *
 * 删除与「交回自动描述」都走**就地二次确认**（按钮变成确认键），不用浏览器原生确认框：
 * 插件页跑在宿主 iframe 的 `sandbox` 里，没带 `allow-modals` 时 confirm 会被静默忽略——
 * 那样按钮看起来点了没反应，危险操作反而变成"点了不知道有没有生效"。
 */
function StickerCard({
  item,
  images,
  busy,
  onSave,
  onRestore,
  onToggle,
  onRemove,
  onSaveFile,
}: {
  item: StickerItem
  images: StickerImageCache
  busy: string
  onSave: (item: StickerItem, draft: StickerDraft) => Promise<StickerItem | null>
  onRestore: (item: StickerItem) => Promise<boolean>
  onToggle: (item: StickerItem, disabled: boolean) => Promise<boolean>
  onRemove: (item: StickerItem, purge: boolean) => Promise<boolean>
  onSaveFile: (item: StickerItem) => Promise<void>
}) {
  const [description, setDescription] = useState(item.description)
  const [name, setName] = useState(item.name)
  const [armedPurge, setArmedPurge] = useState(false)
  const [armedRestore, setArmedRestore] = useState(false)
  const owner = descriptionOwner(item)
  const saving = busy === `save-${item.assetId}`
  const locked = Boolean(busy)
  const dirty = updatePayload(item, { description, name }) !== null

  // 换了一条素材才重置草稿：正在输入时不要被别的刷新顶掉。
  useEffect(() => {
    setDescription(item.description)
    setName(item.name)
    setArmedPurge(false)
    setArmedRestore(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.assetId])

  async function save() {
    const saved = await onSave(item, { description, name })
    // 成功就按服务器回来的那一份对齐（后端会 strip 首尾空白）。
    if (saved) {
      setDescription(saved.description)
      setName(saved.name)
    }
  }

  return (
    <article class="flex flex-col gap-3 rounded-xl border border-line p-3 sm:flex-row">
      <div class="shrink-0">
        <StickerThumb item={item} images={images} />
      </div>

      <div class="flex min-w-0 flex-1 flex-col gap-2">
        <div class="flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
          <Badge tone={statusTone(item.status)} title={statusHint(item.status)}>
            {statusLabel(item.status)}
          </Badge>
          <Badge>{kindLabel(item.kind)}</Badge>
          <Badge title={sourceHint(item.source)}>{sourceLabel(item.source)}</Badge>
          <Badge tone={owner.tone} title={owner.hint}>{owner.label}</Badge>
          <span class="max-w-[16rem] truncate" title={item.assetId}>{item.name || '（没有名字）'}</span>
          <span class="ml-auto">加入 {item.addedAt || '时间未知'}</span>
        </div>

        <Textarea
          value={description}
          rows={2}
          disabled={locked}
          onInput={setDescription}
          placeholder="写一句她会看到的话：这张图是什么（留空 = 不要描述）"
        />

        <div class="flex flex-wrap items-center gap-2">
          <span class={`text-[11px] ${description.trim().length > DESCRIPTION_LIMIT ? 'text-danger' : 'text-muted'}`}>
            {description.trim().length} / {DESCRIPTION_LIMIT}
          </span>
          <span class="w-48">
            <Input
              value={name}
              disabled={locked}
              onInput={setName}
              placeholder={`名字（最多 ${NAME_LIMIT} 字）`}
            />
          </span>
          <Button variant="primary" icon="save" disabled={locked || !dirty} onClick={save}>
            {saving ? '保存中…' : '保存描述'}
          </Button>
          {owner.manual ? (
            armedRestore ? (
              <>
                <Button
                  variant="danger"
                  icon="warning"
                  disabled={locked}
                  onClick={async () => {
                    if (await onRestore(item)) setArmedRestore(false)
                  }}
                >
                  确认交回：这句会清掉
                </Button>
                <Button icon="close" disabled={locked} onClick={() => setArmedRestore(false)}>取消</Button>
              </>
            ) : (
              <Button
                icon="refresh"
                disabled={locked}
                title="清掉你写的那句、回到「待自动描述」，下次重扫由视觉模型重新描述"
                onClick={() => setArmedRestore(true)}
              >
                交回自动描述
              </Button>
            )
          ) : null}
          <label class="ml-auto flex items-center gap-2 text-[11px] text-muted">
            <Switch
              checked={item.disabled}
              disabled={locked}
              label={item.disabled ? '启用这条素材' : '停用这条素材'}
              onChange={(next) => onToggle(item, next)}
            />
            {item.disabled ? '已停用（不参与挑选）' : '在用'}
          </label>
        </div>

        <div class="flex flex-wrap items-center gap-2 text-[11px] text-muted">
          <span>被她选中 {item.uses} 次</span>
          {item.group ? <span>分组 {item.group}</span> : null}
          {item.aliases?.length ? <span>别名 {item.aliases.length} 个</span> : null}
          <span class="max-w-[14rem] truncate font-mono" title={item.file || '没有文件'}>
            {item.file || '没有文件'}
          </span>
          <Button
            icon="download"
            disabled={locked}
            title="通过宿主自己的登录态把原图存下来（沙箱页自己取图带不上登录态）"
            onClick={() => onSaveFile(item)}
          >
            {busy === `file-${item.assetId}` ? '取图中…' : '保存原图'}
          </Button>
          <span class="ml-auto" />
          <Button
            variant="danger"
            icon="close"
            disabled={locked}
            title={deleteWarning(item, false)}
            onClick={() => onRemove(item, false)}
          >
            {MARK_DELETE_LABEL}
          </Button>
          {armedPurge ? (
            <>
              <Button
                variant="danger"
                icon="warning"
                disabled={locked}
                onClick={async () => {
                  if (await onRemove(item, true)) setArmedPurge(false)
                }}
              >
                {PURGE_DELETE_LABEL}
              </Button>
              <Button icon="close" disabled={locked} onClick={() => setArmedPurge(false)}>取消</Button>
            </>
          ) : (
            <Button
              variant="danger"
              icon="warning"
              disabled={locked}
              title="先点一下，确认之后才会真删文件"
              onClick={() => setArmedPurge(true)}
            >
              彻底删除…
            </Button>
          )}
        </div>

        {armedPurge ? <Note tone="warn">{deleteWarning(item, true)}</Note> : null}
      </div>
    </article>
  )
}

/**
 * 一张缩略图。
 *
 * 三级取图（沙箱里唯一能成的是第一级）：
 * 1. **宿主 bridge**：`apiGet('console/sticker-file', {assetId, inline: 1})` 拿 JSON 信封 →
 *    base64 → Blob → `objectURL`。缓存/并发闸/重试都在 `sticker-images.ts` 里。
 * 2. **直连 `<img>`**：后端没有那条 JSON 路时退回老办法——宿主没把插件页放进沙箱时能成。
 * 3. **占位框**：都取不到就显示文件名 + 「保存原图」（走宿主的 download，一定带登录态）。
 *
 * 懒加载：只有真的滚到视口附近才发起请求（收起时是 12 张，展开后滚到哪取哪），
 * 一屏之外的行不会白白轰后端。
 */
function StickerThumb({ item, images }: { item: StickerItem; images: StickerImageCache }) {
  const [url, setUrl] = useState<string | null>(() => images.peek(item.assetId))
  const [direct, setDirect] = useState(false)
  const [broken, setBroken] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    const cached = images.peek(item.assetId)
    setUrl(cached)
    setDirect(false)
    setBroken(false)
    if (cached) return () => { alive = false }
    if (!images.supported()) {
      setDirect(true)
      return () => { alive = false }
    }
    async function load() {
      const got = await images.get(item.assetId)
      if (!alive) return
      if (got) setUrl(got)
      else if (!images.supported()) setDirect(true) // 后端没有 JSON 取图路：退回直连
      else setBroken(true)
    }
    const node = box.current
    if (node && typeof IntersectionObserver !== 'undefined') {
      const observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          observer.disconnect()
          void load()
        }
      }, { rootMargin: '160px 0px' })
      observer.observe(node)
      return () => {
        alive = false
        observer.disconnect()
      }
    }
    void load()
    return () => { alive = false }
  }, [item.assetId, images])

  const directSrc = endpointUrl(thumbnailEndpoint(item))
  if (url) {
    return (
      <img
        src={url}
        alt={item.description || item.name || item.assetId}
        class="h-20 w-20 rounded-lg border border-line bg-raised object-cover"
      />
    )
  }
  if (direct && !broken && directSrc) {
    return (
      <img
        src={directSrc}
        alt={item.description || item.name || item.assetId}
        loading="lazy"
        onError={() => setBroken(true)}
        class="h-20 w-20 rounded-lg border border-line bg-raised object-cover"
      />
    )
  }
  return (
    <div
      ref={box}
      class="flex h-20 w-20 flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-line bg-raised text-[10px] text-muted"
      title={broken || direct
        ? '图片没取回来：文件不在了，或者宿主没让这次请求带上登录态——用右边的「保存原图」'
        : '正在取图…'}
    >
      <Icon name="image" class="h-4 w-4 opacity-60" />
      <span class="px-1 text-center">{fileLabel(item)}</span>
    </div>
  )
}
