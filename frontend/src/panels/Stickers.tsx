/**
 * 「表情库」面板：她攒下来的本地表情包素材、这些素材的**分组**，以及"这张图是什么"那句描述。
 *
 * 为什么这一页值得存在：描述**会进模型可见的素材目录**（`stickerCatalog`）——她挑表情包
 * 靠的就是这句话。所以这一页的核心不是"看"，是**改那句描述**与**管分组**，改完立刻生效。
 *
 * 四条界线在界面上必须分得开：
 *
 * 1. **人工写的 vs 自动描述的**：`manual` 为真就是"你手写的"，自动描述不会覆盖它；
 *    「交回自动描述」只在人工写的那条上出现（这条的按钮不是装饰，是唯一的退路）。
 * 2. **行内只有两个动作**：`disabled` 开关说「停用 / 启用」（可逆），
 *    「删除」= 真删磁盘上的图（不可逆，所以走就地二次确认）。
 * 3. **这一页 vs 整个库**：后端一次最多扫 500 行、一次最多回 200 条，所以
 *    `total` / `truncated` / 分页都摆在明面上。
 * 4. **删分组 ≠ 删素材**：删组时组内素材**先挪走**（默认挪到后端给的 `defaultGroupId`），
 *    所以删除态里必须先看见"素材挪到"这个选择；内置组一个删除入口都不给。
 *
 * v1.8.4 起 `groupId` 的字面量就是**磁盘目录名**（见 `sticker-groups.ts` 的模块注释）：
 * 组名按**字节**校验（100 字节 ≈ 33 个汉字）、内置组连目录名都不许改（只给「改描述」）、
 * 改名是"重命名目录 + 批量改素材行"所以必须重拉。
 *
 * 写操作成功后**不整页重拉**（控制台既有做法）：用响应里的 `item` 就地更新那一行。
 * 组名/计数这类跨行的东西才重拉分组清单。失败一律照实显示——把 400 吞成空白页是这一页
 * 最不该犯的错。
 *
 * 后端接口在 `adapters/console_api.py`（取数）+ `main.py`（路由）；这里只管显示与发请求。
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { apiGet, apiPost, describeError, downloadFile, endpointUrl, uploadFile } from '../bridge'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type {
  StickerCounts, StickerDeleteResult, StickerGroup, StickerGroupDeleteResult,
  StickerGroupListPayload, StickerGroupSaveResult, StickerItem, StickerListPayload,
  StickerMoveResult, StickerRescanResult, StickerRestoreResult, StickerUpdateResult,
  StickerUploadResult,
} from '../types'
import {
  Badge, Button, Empty, ErrorNote, Field, FilePicker, Grid, Icon, Input, Loading, LongList,
  Note, Panel, Select, Stack, Stat, Switch, Textarea,
} from '../components/ui'
import {
  UnsupportedStickerImage, createStickerImageCache, parseImageEnvelope, revokeStickerImageUrl,
  stickerImageObjectUrl, type StickerImageCache,
} from '../sticker-images'
import {
  DEFAULT_PAGE_SIZE, DELETE_CONFIRM_LABEL, DELETE_LABEL, DESCRIPTION_LIMIT, DISABLE_LABEL,
  EMPTY_FILTERS, ENABLE_LABEL, PAGE_SIZE_MAX, activeFilterCount, brokenTitle, deleteDoneNote,
  deletePayload, descriptionOwner, disabledPayload, emptyHint, fileLabel, kindLabel,
  mergeStickerItems, overrideMap, pageWindow, rescanNote, restorePayload, resultSummary,
  saveBlocker, sourceLabel, staleOverridesNote, statusLabel, statusTone, stickerParams,
  thumbnailEndpoint, updateNote, updatePayload, type StickerDraft, type StickerFilters,
} from '../stickers-view'
import {
  FILTER_ALL_LABEL, GROUP_CANCEL_LABEL, GROUP_DELETE_CONFIRM_LABEL, GROUP_DELETE_LABEL,
  GROUP_DESCRIBE_LABEL, GROUP_DESCRIPTION_LIMIT, GROUP_FILTER_LABEL, GROUP_MOVE_LABEL,
  GROUP_MOVE_PLACEHOLDER, GROUP_MOVE_TO_LABEL, GROUP_NAME_HINT, GROUP_NEW_LABEL,
  GROUP_RENAME_LABEL, GROUP_SAVE_LABEL, UPLOADING_LABEL, UPLOAD_LABEL,
  canFilterByGroup, canRename, defaultGroupId, defaultTarget, deleteMoveOptions, filterGroupOptions,
  groupCountText, groupDeleteBlocker, groupDeleteNote, groupDeletePayload, groupLabel,
  groupSaveBlocker, groupSaveNote, groupSavePayload, groupsTitle, itemGroupLabel, moveNote,
  movePayload, normalizeGroups, targetGroupOptions, uploadBlocker, uploadEndpoint, uploadOutcome,
  type GroupDraft, type GroupSaveMode,
} from '../sticker-groups'

/** 状态筛选项（值与后端 `status` 参数逐字一致）。 */
const STATUS_CHIPS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部' },
  { value: 'active', label: '在用' },
  { value: 'pending', label: '待自动描述' },
  { value: 'missing', label: '缺失' },
  { value: 'disabled', label: '已停用' },
]

const EMPTY_DRAFT: GroupDraft = { name: '', description: '' }

export function Stickers({ refreshKey, onNavigate }: PanelProps) {
  const [filters, setFilters] = useState<StickerFilters>(EMPTY_FILTERS)
  const [limit, setLimit] = useState<number>(DEFAULT_PAGE_SIZE)
  const [offset, setOffset] = useState(0)
  // 列表里刚被就地更新过的行（assetId → 写操作响应里的 item）。点「刷新」清掉。
  const [overrides, setOverrides] = useState<Record<string, StickerItem>>({})
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [noticeTone, setNoticeTone] = useState<'ok' | 'neutral' | 'warn'>('ok')
  // 搜索框自己的草稿：每次敲键就发一次请求太吵，停 300ms 再进筛选条件。
  const [needle, setNeedle] = useState('')

  // ---- 分组区 ---- #
  const [creating, setCreating] = useState(false)
  const [draft, setDraft] = useState<GroupDraft>(EMPTY_DRAFT)
  const [editing, setEditing] = useState<(GroupDraft & { groupId: string }) | null>(null)
  const [removing, setRemoving] = useState<{ groupId: string; moveTo: string } | null>(null)
  // ---- 上传 ---- #
  const [file, setFile] = useState<File | null>(null)
  const [uploadGroup, setUploadGroup] = useState('')
  const [uploadDraft, setUploadDraft] = useState<GroupDraft>(EMPTY_DRAFT)
  // ---- 多选批量移动 ---- #
  const [picked, setPicked] = useState<string[]>([])
  const [batchTo, setBatchTo] = useState('')

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
  const groupQuery = useQuery<StickerGroupListPayload>(
    'console/sticker-groups',
    undefined,
    { nonce: refreshKey },
  )
  const payload = list.data
  const total = payload?.total ?? 0
  const pager = pageWindow(total, limit, offset)

  const groups = normalizeGroups(groupQuery.data)
  // 默认分组由后端给（`defaultGroupId`），前端不写死；它不可用时退回第一个可用目标。
  const fallbackTarget = defaultTarget(groups, defaultGroupId(groupQuery.data))
  const targets = targetGroupOptions(groups)
  const uploadTarget = uploadGroup || fallbackTarget

  // 偏移超出范围（删了几条之后）时收敛到最后一页：别停在一个永远空的页上。
  useEffect(() => {
    if (pager.start !== offset) setOffset(pager.start)
  }, [pager.start, offset])

  // 换筛选 / 换页 / 点了顶栏刷新：就地更新过的那几行不再覆盖服务器的新数据。
  const filterKey = `${filters.status}|${filters.kind}|${filters.source}|${filters.group}|${filters.q}`
  useEffect(() => {
    setOverrides({})
    setPicked([])
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

  function flash(text: string, tone: 'ok' | 'neutral' | 'warn' = 'ok') {
    setNotice(text)
    setNoticeTone(tone)
  }

  /** 写操作的统一收口：成功就地更新那一行 + 一句人话，失败照实报（返回是否成功）。 */
  async function write<T>(
    key: string,
    endpoint: string,
    body: unknown,
    done: (result: T) => string,
    tone: 'ok' | 'neutral' = 'ok',
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
      if (text) flash(text, tone)
      return result
    } catch (failure) {
      setError(describeError(failure))
      return null
    } finally {
      setBusy('')
    }
  }

  async function saveDescription(item: StickerItem, item_draft: StickerDraft): Promise<StickerItem | null> {
    const blocker = saveBlocker(item_draft)
    if (blocker) {
      setError(blocker)
      return null
    }
    const body = updatePayload(item, item_draft)
    if (!body) {
      setError('描述和名字都没改')
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
      setError('这条没有人工描述可交回')
      return false
    }
    const result = await write<StickerRestoreResult>(
      `restore-${item.assetId}`, 'console/sticker-restore-description', body,
      () => '已交回自动描述',
    )
    return Boolean(result)
  }

  async function toggleDisabled(item: StickerItem, disabled: boolean): Promise<boolean> {
    const result = await write<StickerUpdateResult>(
      `disable-${item.assetId}`, 'console/sticker-update', disabledPayload(item, disabled),
      (value) => (value.item?.disabled ? '已停用' : '已启用'),
    )
    return Boolean(result)
  }

  /** 行内「删除」= 真删磁盘文件（`purge=true`）；就地二次确认由卡片自己把控。 */
  async function remove(item: StickerItem): Promise<boolean> {
    const result = await write<StickerDeleteResult>(
      `delete-${item.assetId}`, 'console/sticker-delete', deletePayload(item, true), deleteDoneNote,
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
   */
  async function saveOriginal(item: StickerItem): Promise<void> {
    setBusy(`file-${item.assetId}`)
    setError('')
    setNotice('')
    try {
      await downloadFile('console/sticker-file', { assetId: item.assetId }, fileLabel(item))
      flash(`已把原图交给浏览器下载：${fileLabel(item)}`)
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

  /* ------------------------------------------------------------ 分组写操作 */

  /**
   * 新建 / 改名 / 改描述：**只认 groupId / name / description 三个键**。
   *
   * 三种模式（`mode`）与后端的语义一一对应：
   * * 新建（`groupId` 空）：`name` **就是**新目录名，后端建目录；
   * * 改名（`name` ≠ `groupId`）：后端要**重命名目录 + 批量改该组素材的行**——所以这不是
   *   "改一个显示名"，回来之后**必须重拉素材列表与分组**（就地改一行 `name` 会立刻说谎：
   *   磁盘上那批素材的归属与路径都变了）；
   * * 只写描述（`name` 与 `groupId` 相同，或内置组）：目录没动，但描述会进模型目录，
   *   同样重拉一次最省心（分组计数与描述都在那一份里）。
   *
   * 当前正筛着这一组时，筛选条件要跟着搬到新目录名上：不然改完名那一页会变成空的
   * （旧 id 已经不存在了）。
   */
  async function saveGroup(groupId: string, groupDraft: GroupDraft): Promise<boolean> {
    const mode: GroupSaveMode = !groupId || groupDraft.name.trim() !== groupId ? 'full' : 'description'
    const blocker = groupSaveBlocker(groupDraft, mode)
    if (blocker) {
      setError(blocker)
      return false
    }
    setBusy(`group-save-${groupId || 'new'}`)
    setError('')
    setNotice('')
    try {
      const result = await apiPost<StickerGroupSaveResult>(
        'console/sticker-group-save', groupSavePayload(groupDraft, groupId),
      )
      const savedId = result?.groupId || groupId
      flash(groupSaveNote(!groupId ? 'create' : mode === 'full' ? 'rename' : 'description'))
      setCreating(false)
      setDraft(EMPTY_DRAFT)
      setEditing(null)
      // 改名搬的是目录：当前筛选跟着搬，否则筛出来的那一页会空掉。
      if (groupId && savedId && filters.group === groupId) patchFilters({ group: savedId })
      groupQuery.reload()
      list.reload()
      return true
    } catch (failure) {
      setError(describeError(failure))
      return false
    } finally {
      setBusy('')
    }
  }

  /** 删分组：组内素材**先挪走**（`moveTo` 是必选的目标），再删注册行。 */
  async function removeGroup(groupId: string, moveTo: string): Promise<boolean> {
    setBusy(`group-delete-${groupId}`)
    setError('')
    setNotice('')
    try {
      const result = await apiPost<StickerGroupDeleteResult>(
        'console/sticker-group-delete', groupDeletePayload(groupId, moveTo),
      )
      flash(groupDeleteNote(result?.moved))
      setRemoving(null)
      // 正筛着这一组时把筛选让开：那一组已经不存在了，留着只会筛出一个永远空的页面。
      if (filters.group === groupId) patchFilters({ group: '' })
      // 组内那批素材的归属变了（可能几十条）：这里只能整页重拉。
      setOverrides({})
      groupQuery.reload()
      list.reload()
      return true
    } catch (failure) {
      setError(describeError(failure))
      return false
    } finally {
      setBusy('')
    }
  }

  /** 批量改归属：一条不合法后端就一条都不写（先校验后写），失败照样照实报。 */
  async function moveAssets(assetIds: string[], groupId: string, key: string): Promise<boolean> {
    const body = movePayload(assetIds, groupId)
    if (!body) {
      setError(GROUP_MOVE_PLACEHOLDER)
      return false
    }
    setBusy(key)
    setError('')
    setNotice('')
    try {
      const result = await apiPost<StickerMoveResult>('console/sticker-move', body)
      setOverrides((prev) => ({ ...prev, ...overrideMap(result?.item ?? []) }))
      flash(moveNote(result?.moved))
      setPicked([])
      setBatchTo('')
      // 组里张数变了：分组的计数得重新取（素材行用上面的 item 就地更新）。
      groupQuery.reload()
      return true
    } catch (failure) {
      setError(describeError(failure))
      return false
    } finally {
      setBusy('')
    }
  }

  /* ------------------------------------------------------------ 上传 */

  async function upload(): Promise<boolean> {
    const blocker = uploadBlocker({ enabled: payload?.enabled === true, hasFile: Boolean(file) })
    if (blocker) {
      setError(blocker)
      return false
    }
    setBusy('upload')
    setError('')
    setNotice('')
    try {
      // 宿主 `upload(endpoint, file)` 只发 `file` 一个字段，三个可选参数只能挂查询串。
      const result = await uploadFile<StickerUploadResult>(
        uploadEndpoint({
          groupId: uploadTarget,
          description: uploadDraft.description,
          name: uploadDraft.name,
        }),
        file as File,
      )
      const outcome = uploadOutcome(result)
      flash(outcome.text, outcome.tone)
      setFile(null)
      setUploadDraft(EMPTY_DRAFT)
      groupQuery.reload()
      list.reload()
      return true
    } catch (failure) {
      setError(describeError(failure))
      return false
    } finally {
      setBusy('')
    }
  }

  function refreshAll() {
    setOverrides({})
    setError('')
    setNotice('')
    setPicked([])
    list.reload()
    groupQuery.reload()
  }

  if (list.loading && !payload && !list.error) return <Loading />

  const counts: StickerCounts = payload?.counts ?? { total: 0, active: 0, pending: 0, missing: 0, disabled: 0 }
  const items = mergeStickerItems(payload?.items ?? [], overrides)
  const pendingOverrides = Object.keys(overrides).length
  const locked = Boolean(busy)
  const filtersOn = activeFilterCount(filters)
  const enabled = payload?.enabled === true

  return (
    <Stack>
      <Grid cols={4}>
        <Stat label="素材总数" value={counts.total} />
        <Stat label="在用" value={counts.active} tone="ok" />
        <Stat label="待自动描述" value={counts.pending} tone={counts.pending > 0 ? 'warn' : 'neutral'} />
        <Stat label="已停用" value={counts.disabled} />
      </Grid>

      {payload && !payload.enabled ? (
        <div class="flex flex-wrap items-center gap-2 rounded-xl border border-warn/40 bg-warn/10 px-4 py-3 text-xs text-warn">
          <span class="min-w-0 flex-1">本地表情包库未启用</span>
          <Button icon="config" onClick={() => onNavigate('config')}>去配置页打开</Button>
        </div>
      ) : payload && !payload.auto_collect ? (
        <Note tone="warn">自动收藏已关闭</Note>
      ) : null}

      {error ? (
        <div data-note="error"><ErrorNote text={error} /></div>
      ) : null}
      {notice ? (
        <div data-note="notice"><Note tone={noticeTone}>{notice}</Note></div>
      ) : null}

      <Panel
        title={groupsTitle(groups.length)}
        icon="filter"
        actions={
          <>
            <Button icon="refresh" disabled={locked} onClick={() => groupQuery.reload()}>刷新</Button>
            <Button
              variant="primary"
              disabled={locked || creating}
              onClick={() => {
                setDraft(EMPTY_DRAFT)
                setCreating(true)
                setEditing(null)
              }}
            >
              {GROUP_NEW_LABEL}
            </Button>
          </>
        }
      >
        <Stack>
          {groupQuery.error ? (
            <ErrorNote text={groupQuery.error} onRetry={groupQuery.reload} />
          ) : groupQuery.loading && !groupQuery.data ? (
            <Loading />
          ) : groups.length === 0 ? (
            <Empty text="还没有分组" icon="filter" />
          ) : (
            <LongList
              items={groups}
              limit={8}
              unit="组"
              class="flex flex-col gap-2"
              render={(group) => (
                <GroupRow
                  key={group.groupId || 'ungrouped'}
                  group={group}
                  groups={groups}
                  targetDefault={fallbackTarget}
                  busy={busy}
                  locked={locked}
                  active={filters.group === group.groupId}
                  editing={editing}
                  removing={removing}
                  onEdit={setEditing}
                  onRemove={setRemoving}
                  onSave={saveGroup}
                  onDelete={removeGroup}
                  onFilter={(groupId) => patchFilters({ group: groupId })}
                />
              )}
            />
          )}

          {groupQuery.data?.truncated ? (
            <span class="text-[11px] text-muted">只统计了最近一批</span>
          ) : null}

          {creating ? (
            <div class="flex flex-col gap-2 rounded-xl border border-accent/40 bg-accent/5 p-3" data-form="group-new">
              <div class="flex flex-wrap items-end gap-2">
                <Field label="名称" class="min-w-[10rem] flex-1">
                  <Input
                    value={draft.name}
                    disabled={locked}
                    onInput={(next) => setDraft((prev) => ({ ...prev, name: next }))}
                    placeholder="分组名"
                  />
                </Field>
                <Button
                  variant="primary"
                  icon="save"
                  disabled={locked || busy === 'group-save-new'}
                  onClick={() => saveGroup('', draft)}
                >
                  {GROUP_SAVE_LABEL}
                </Button>
                <Button icon="close" disabled={locked} onClick={() => setCreating(false)}>
                  {GROUP_CANCEL_LABEL}
                </Button>
              </div>
              <Field label="描述">
                <Textarea
                  rows={2}
                  value={draft.description}
                  disabled={locked}
                  onInput={(next) => setDraft((prev) => ({ ...prev, description: next }))}
                  placeholder="这一组什么风格、什么场合用"
                />
              </Field>
              <span class={`text-[11px] ${overLimit(draft.description) ? 'text-danger' : 'text-muted'}`}>
                {draft.description.trim().length} / {GROUP_DESCRIPTION_LIMIT}
              </span>
            </div>
          ) : null}
        </Stack>
      </Panel>

      <Panel title={UPLOAD_LABEL} icon="upload">
        <div class="flex flex-col gap-4" data-form="sticker-upload">
          <div class="flex flex-wrap items-end gap-2">
            <Field label="分组" class="w-44">
              <Select
                value={uploadTarget}
                disabled={locked || !targets.length}
                onChange={(next) => setUploadGroup(String(next))}
                options={targets}
                placeholder={GROUP_MOVE_PLACEHOLDER}
              />
            </Field>
            <Field label="名字" class="w-44">
              <Input
                value={uploadDraft.name}
                disabled={locked}
                onInput={(next) => setUploadDraft((prev) => ({ ...prev, name: next }))}
                placeholder="留空就自动起名"
              />
            </Field>
          </div>
          <Field label="描述">
            <Textarea
              rows={2}
              value={uploadDraft.description}
              disabled={locked}
              onInput={(next) => setUploadDraft((prev) => ({ ...prev, description: next }))}
              placeholder="这张图是什么"
            />
          </Field>
          <FilePicker
            accept="image/png,image/jpeg,image/gif,image/webp"
            disabled={locked || !enabled}
            hint={file ? `${file.name} · ${fileSize(file.size)}` : undefined}
            onPick={(pickedFile) => {
              setError('')
              setNotice('')
              setFile(pickedFile)
            }}
          />
          <div class="flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              icon="upload"
              disabled={locked || !enabled || !file}
              onClick={upload}
            >
              {busy === 'upload' ? UPLOADING_LABEL : UPLOAD_LABEL}
            </Button>
            {file ? (
              <Button icon="close" disabled={locked} onClick={() => setFile(null)}>
                {GROUP_CANCEL_LABEL}
              </Button>
            ) : null}
            {!enabled && payload ? <span class="text-[11px] text-warn">库未启用</span> : null}
          </div>
        </div>
      </Panel>

      <Panel
        title={`素材（${counts.total}）`}
        icon="image"
        actions={
          <>
            <Button icon="refresh" disabled={locked} onClick={refreshAll}>刷新</Button>
            <Button
              variant="primary"
              icon="search"
              disabled={locked || !enabled}
              title={enabled ? '扫描表情库目录' : '本地表情包库未启用'}
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
              <span class="text-[11px] font-medium text-muted">搜索</span>
              <Input
                value={needle}
                onInput={setNeedle}
                placeholder="例如：猫、挥手、sticker-1a2b"
              />
            </label>
            <label class="flex w-36 flex-col gap-1" data-filter="group">
              <span class="text-[11px] font-medium text-muted">分组</span>
              <Select
                value={filters.group}
                disabled={!groups.length}
                onChange={(next) => patchFilters({ group: String(next) })}
                options={filterGroupOptions(groups)}
                placeholder={FILTER_ALL_LABEL}
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

          {picked.length ? (
            <div class="flex flex-wrap items-center gap-2 rounded-xl border border-accent/40 bg-accent/5 px-3 py-2 text-xs" data-bar="batch">
              <span>已选 {picked.length} 条</span>
              <span class="w-44">
                <Select
                  value={batchTo}
                  disabled={locked || !targets.length}
                  onChange={(next) => setBatchTo(String(next))}
                  options={targets}
                  placeholder={GROUP_MOVE_PLACEHOLDER}
                />
              </span>
              <Button
                variant="primary"
                disabled={locked || !batchTo}
                onClick={() => moveAssets(picked, batchTo, 'move-batch')}
              >
                {busy === 'move-batch' ? '移动中…' : GROUP_MOVE_LABEL}
              </Button>
              <Button icon="close" disabled={locked} onClick={() => setPicked([])}>清空选择</Button>
            </div>
          ) : null}

          <div class="flex flex-wrap items-center gap-2 text-[11px] text-muted">
            <span>{resultSummary({
              total, shown: items.length, limit, offset: pager.start, truncated: payload?.truncated === true,
            })}</span>
            {staleOverridesNote(pendingOverrides) ? (
              <span class="text-warn">{staleOverridesNote(pendingOverrides)}</span>
            ) : null}
          </div>

          {list.error ? (
            <ErrorNote text={list.error} onRetry={list.reload} />
          ) : !payload ? (
            <Loading />
          ) : items.length === 0 ? (
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
                  groups={groups}
                  busy={busy}
                  picked={picked.includes(row.assetId)}
                  onPick={(assetId, next) => setPicked((prev) => (
                    next ? [...prev, assetId] : prev.filter((id) => id !== assetId)
                  ))}
                  onMove={moveAssets}
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
            </div>
          ) : null}
        </Stack>
      </Panel>
    </Stack>
  )
}

/** 描述超限（只用来把计数染红）。 */
function overLimit(value: string): boolean {
  return value.trim().length > GROUP_DESCRIPTION_LIMIT
}

function fileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 KB'
  return bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

/**
 * 一行分组：名字 + 描述 + 张数 + 三个动作（筛选 / 改名或改描述 / 删除分组）。
 *
 * 三条界线：
 * 1. **内置组没有删除入口**（后端也会 400）：`groupDeleteBlocker` 说了算，不是这里再判一次；
 * 2. **内置组的目录名不许改**（`canRename`）：它的动作是「改描述」——只写描述那条路；
 * 3. **组名就是目录名**：改名 = 重命名目录，校验按字节（见 `sticker-groups.ts`）。
 *
 * 删除态**必须先看见素材挪到哪**：就地展开一个目标下拉（默认后端给的 `defaultGroupId`），
 * 按「确认删除分组」才发请求。
 */
function GroupRow({
  group,
  groups,
  targetDefault,
  busy,
  locked,
  active,
  editing,
  removing,
  onEdit,
  onRemove,
  onSave,
  onDelete,
  onFilter,
}: {
  group: StickerGroup
  groups: StickerGroup[]
  targetDefault: string
  busy: string
  locked: boolean
  active: boolean
  editing: (GroupDraft & { groupId: string }) | null
  removing: { groupId: string; moveTo: string } | null
  onEdit: (value: (GroupDraft & { groupId: string }) | null) => void
  onRemove: (value: { groupId: string; moveTo: string } | null) => void
  onSave: (groupId: string, groupDraft: GroupDraft) => Promise<boolean>
  onDelete: (groupId: string, moveTo: string) => Promise<boolean>
  onFilter: (groupId: string) => void
}) {
  const blocked = groupDeleteBlocker(group)
  // 内置组的目录名不许改（后端也 400）：它的编辑表单只有描述，动作词也不同。
  const renamable = canRename(group)
  const isEditing = editing?.groupId === group.groupId
  const isRemoving = removing?.groupId === group.groupId
  return (
    <div class="flex flex-col gap-2 rounded-xl border border-line p-3" data-group-id={group.groupId}>
      <div class="flex flex-wrap items-center gap-2 text-xs">
        <span class="font-medium">{groupLabel(group)}</span>
        {group.builtin ? <Badge tone="accent">内置</Badge> : null}
        <span class="text-muted">{groupCountText(group.count)}</span>
        <span class="min-w-0 flex-1 truncate text-muted">{group.description}</span>
        <span class="ml-auto flex flex-wrap items-center gap-1.5">
          {canFilterByGroup(group.groupId) ? (
            <Button
              icon="filter"
              variant={active ? 'primary' : 'default'}
              disabled={locked}
              onClick={() => onFilter(group.groupId)}
            >
              {GROUP_FILTER_LABEL}
            </Button>
          ) : null}
          <Button
            icon={renamable ? 'save' : 'info'}
            disabled={locked}
            onClick={() => {
              onRemove(null)
              onEdit({ groupId: group.groupId, name: group.name, description: group.description })
            }}
          >
            {renamable ? GROUP_RENAME_LABEL : GROUP_DESCRIBE_LABEL}
          </Button>
          {blocked ? null : (
            <Button
              variant="danger"
              icon="close"
              disabled={locked}
              onClick={() => {
                onEdit(null)
                onRemove({ groupId: group.groupId, moveTo: targetDefault })
              }}
            >
              {GROUP_DELETE_LABEL}
            </Button>
          )}
        </span>
      </div>

      {isEditing && editing ? (
        <div class="flex flex-col gap-2 rounded-lg border border-line bg-raised p-2" data-form="group-edit">
          <div class="flex flex-wrap items-end gap-2">
            {renamable ? (
              <>
                <Field label="名称" class="min-w-[10rem] flex-1">
                  <Input
                    value={editing.name}
                    disabled={locked}
                    onInput={(next) => onEdit({ ...editing, name: next })}
                    placeholder="分组名"
                  />
                </Field>
                <span class="text-[11px] text-muted">{GROUP_NAME_HINT}</span>
              </>
            ) : (
              <span class="min-w-[10rem] flex-1 text-[11px] text-muted">目录名 {group.groupId}</span>
            )}
            <Button
              variant="primary"
              icon="save"
              disabled={locked || busy === `group-save-${group.groupId}`}
              onClick={() => onSave(group.groupId, editing)}
            >
              {GROUP_SAVE_LABEL}
            </Button>
            <Button icon="close" disabled={locked} onClick={() => onEdit(null)}>
              {GROUP_CANCEL_LABEL}
            </Button>
          </div>
          <Field label="描述">
            <Textarea
              rows={2}
              value={editing.description}
              disabled={locked}
              onInput={(next) => onEdit({ ...editing, description: next })}
              placeholder="这一组什么风格、什么场合用"
            />
          </Field>
          <span class={`text-[11px] ${overLimit(editing.description) ? 'text-danger' : 'text-muted'}`}>
            {editing.description.trim().length} / {GROUP_DESCRIPTION_LIMIT}
          </span>
        </div>
      ) : null}

      {isRemoving && removing ? (
        <div class="flex flex-wrap items-center gap-2 rounded-lg border border-danger/40 bg-danger/10 p-2 text-xs" data-form="group-delete">
          <span class="text-muted">{GROUP_MOVE_TO_LABEL}</span>
          <span class="w-44">
            <Select
              value={removing.moveTo}
              disabled={locked}
              onChange={(next) => onRemove({ ...removing, moveTo: String(next) })}
              options={deleteMoveOptions(groups, group.groupId)}
              placeholder={GROUP_MOVE_PLACEHOLDER}
            />
          </span>
          <Button
            variant="danger"
            icon="warning"
            disabled={locked || !removing.moveTo || busy === `group-delete-${group.groupId}`}
            onClick={() => onDelete(group.groupId, removing.moveTo)}
          >
            {GROUP_DELETE_CONFIRM_LABEL}
          </Button>
          <Button icon="close" disabled={locked} onClick={() => onRemove(null)}>
            {GROUP_CANCEL_LABEL}
          </Button>
        </div>
      ) : null}
    </div>
  )
}

/**
 * 一条素材：缩略图 + 状态/形式/来源/分组/描述作者徽章 + 行内编辑描述与名字 +
 * 「移到分组」「停用/启用」「删除」+ 多选（批量移动用）。
 *
 * 删除与「交回自动描述」都走**就地二次确认**（按钮变成确认键），不用浏览器原生确认框：
 * 插件页跑在宿主 iframe 的 `sandbox` 里，没带 `allow-modals` 时 confirm 会被静默忽略——
 * 那样按钮看起来点了没反应，危险操作反而变成"点了不知道有没有生效"。
 */
function StickerCard({
  item,
  images,
  groups,
  busy,
  picked,
  onPick,
  onMove,
  onSave,
  onRestore,
  onToggle,
  onRemove,
  onSaveFile,
}: {
  item: StickerItem
  images: StickerImageCache
  groups: StickerGroup[]
  busy: string
  picked: boolean
  onPick: (assetId: string, next: boolean) => void
  onMove: (assetIds: string[], groupId: string, key: string) => Promise<boolean>
  onSave: (item: StickerItem, draft: StickerDraft) => Promise<StickerItem | null>
  onRestore: (item: StickerItem) => Promise<boolean>
  onToggle: (item: StickerItem, disabled: boolean) => Promise<boolean>
  onRemove: (item: StickerItem) => Promise<boolean>
  onSaveFile: (item: StickerItem) => Promise<void>
}) {
  const [description, setDescription] = useState(item.description)
  const [name, setName] = useState(item.name)
  const [armedPurge, setArmedPurge] = useState(false)
  const [armedRestore, setArmedRestore] = useState(false)
  const [moveTo, setMoveTo] = useState('')
  const owner = descriptionOwner(item)
  const saving = busy === `save-${item.assetId}`
  const moving = busy === `move-${item.assetId}`
  const locked = Boolean(busy)
  const dirty = updatePayload(item, { description, name }) !== null
  const targets = targetGroupOptions(groups)

  // 换了一条素材才重置草稿：正在输入时不要被别的刷新顶掉。
  useEffect(() => {
    setDescription(item.description)
    setName(item.name)
    setArmedPurge(false)
    setArmedRestore(false)
    setMoveTo('')
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
          <label class="flex items-center gap-1">
            <input
              type="checkbox"
              class="h-3.5 w-3.5 accent-accent"
              aria-label="选择"
              checked={picked}
              onChange={() => onPick(item.assetId, !picked)}
            />
            选择
          </label>
          <Badge tone={statusTone(item.status)}>{statusLabel(item.status)}</Badge>
          <Badge>{kindLabel(item.kind)}</Badge>
          <Badge>{sourceLabel(item.source)}</Badge>
          <Badge tone={item.groupId === '' ? 'warn' : 'neutral'}>{itemGroupLabel(item)}</Badge>
          <Badge tone={owner.tone}>{owner.label}</Badge>
          <span class="max-w-[16rem] truncate" title={item.assetId}>{item.name || '（没有名字）'}</span>
          <span class="ml-auto">加入 {item.addedAt || '时间未知'}</span>
        </div>

        <Textarea
          value={description}
          rows={2}
          disabled={locked}
          onInput={setDescription}
          placeholder="这张图是什么"
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
              placeholder="名字"
            />
          </span>
          <Button variant="primary" icon="save" disabled={locked || !dirty} onClick={save}>
            {saving ? '保存中…' : '保存'}
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
                  确认交回
                </Button>
                <Button icon="close" disabled={locked} onClick={() => setArmedRestore(false)}>取消</Button>
              </>
            ) : (
              <Button icon="refresh" disabled={locked} onClick={() => setArmedRestore(true)}>
                交回自动描述
              </Button>
            )
          ) : null}
          <label class="ml-auto flex items-center gap-2 text-[11px] text-muted">
            <Switch
              checked={item.disabled}
              disabled={locked}
              label={item.disabled ? ENABLE_LABEL : DISABLE_LABEL}
              onChange={(next) => onToggle(item, next)}
            />
            {item.disabled ? ENABLE_LABEL : DISABLE_LABEL}
          </label>
        </div>

        <div class="flex flex-wrap items-center gap-2 text-[11px] text-muted">
          <span>被她选中 {item.uses} 次</span>
          <span class="w-40" data-move="row">
            <Select
              value={moveTo}
              disabled={locked || !targets.length}
              onChange={(next) => setMoveTo(String(next))}
              options={targets}
              placeholder={GROUP_MOVE_PLACEHOLDER}
            />
          </span>
          <Button
            disabled={locked || !moveTo}
            onClick={() => onMove([item.assetId], moveTo, `move-${item.assetId}`).then((ok) => {
              if (ok) setMoveTo('')
            })}
          >
            {moving ? '移动中…' : GROUP_MOVE_LABEL}
          </Button>
          {item.aliases?.length ? <span>别名 {item.aliases.length} 个</span> : null}
          <span class="max-w-[14rem] truncate font-mono" title={item.file || '没有文件'}>
            {item.file || '没有文件'}
          </span>
          <Button
            icon="download"
            disabled={locked}
            onClick={() => onSaveFile(item)}
          >
            {busy === `file-${item.assetId}` ? '取图中…' : '保存原图'}
          </Button>
          <span class="ml-auto" />
          {armedPurge ? (
            <>
              <Button
                variant="danger"
                icon="warning"
                disabled={locked}
                onClick={async () => {
                  if (await onRemove(item)) setArmedPurge(false)
                }}
              >
                {DELETE_CONFIRM_LABEL}
              </Button>
              <Button icon="close" disabled={locked} onClick={() => setArmedPurge(false)}>取消</Button>
            </>
          ) : (
            <Button
              variant="danger"
              icon="close"
              disabled={locked}
              onClick={() => setArmedPurge(true)}
            >
              {DELETE_LABEL}
            </Button>
          )}
        </div>

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
  //: 取不到的**理由**（后端 404 的 message 里带着"已找过哪儿"）。有它就显示它——
  //: 只写"取不到图"的话，用户与维护者都看不出后端到底去哪儿找了（真机就是这么瞎的）。
  const [reason, setReason] = useState('')
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    const cached = images.peek(item.assetId)
    setUrl(cached)
    setDirect(false)
    setBroken(false)
    setReason('')
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
      else {
        setBroken(true)
        setReason(images.reason(item.assetId))
      }
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
      // 提示里带上后端给的理由（它找的是哪儿）：`brokenTitle` 只在有理由时才追加，
      // 直连失败（`direct`）没有后端 message，就还是那句状态词。
      title={broken || direct ? brokenTitle(broken ? reason : '') : '正在取图…'}
    >
      <Icon name="image" class="h-4 w-4 opacity-60" />
      <span class="px-1 text-center">{fileLabel(item)}</span>
    </div>
  )
}
