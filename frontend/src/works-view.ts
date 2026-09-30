/**
 * 「作品」面板的纯逻辑（零依赖，可在 Node 里直接断言：`scripts/check-works-view.ts`）。
 *
 * 面板上有几条"看不出来的错"必须钉住：
 * 1. **只有用户能接受 / 驳回**——她只能提议。界面上任何"她自己接受了"的暗示都是错的，
 *    所以这句说明是常量、提案卡必挂（上游 `works.ts` 的明确语义）。
 * 2. **不可用要说原因**：功能没开 / 上一次任务还在跑 / 数据坏了 / 还没有这件作品，
 *    四种情况的说法不一样，别只留一个灰按钮。
 * 3. **两种空态不是一回事**：`works.enabled` 没开 vs 开了但她还什么都没写。
 */

/** 徽章色调（与 `components/ui.tsx` 的 `Badge` 同一组）。 */
export type BadgeTone = 'neutral' | 'accent' | 'ok' | 'warn' | 'danger'

/** 版本 / 提案的作者：上游只有 `user` 与 `protagonist` 两种。 */
export function authorLabel(author: string): string {
  if (author === 'user') return '你'
  if (author === 'protagonist') return '她'
  return author || '未知作者'
}

export function authorTone(author: string): BadgeTone {
  if (author === 'user') return 'ok'
  if (author === 'protagonist') return 'accent'
  return 'neutral'
}

export function proposalStatusLabel(status: string): string {
  if (status === 'pending') return '待你决定'
  if (status === 'accepted') return '已接受'
  if (status === 'rejected') return '已驳回'
  return status || '未知状态'
}

export function proposalStatusTone(status: string): BadgeTone {
  if (status === 'pending') return 'warn'
  if (status === 'accepted') return 'ok'
  return 'neutral'
}

export function jobStatusLabel(status: string): string {
  switch (status) {
    case 'running':
      return '正在写'
    case 'completed':
      return '已写完（变成待决提案）'
    case 'failed':
      return '失败'
    case 'cancelled':
      return '已取消'
    case 'interrupted':
      return '中断（插件重启过，这次没写成，也不会自动重跑）'
    default:
      return status || '未知状态'
  }
}

export function jobStatusTone(status: string): BadgeTone {
  if (status === 'running') return 'accent'
  if (status === 'completed') return 'ok'
  if (status === 'failed' || status === 'interrupted') return 'danger'
  return 'neutral'
}

/** 提案卡的固定说明：接受 / 驳回只有用户能做（不是"你们俩都能定"）。 */
export const USER_ONLY_NOTE =
  '只有你能接受或驳回：她只能提议。接受后立刻变成新版本，驳回只留一条结论（正文不动）。'

/** 空态说明：`available`（服务层在不在）→ `enabled`（功能开没开）→ 有没有作品。 */
export function worksEmptyHint(input: {
  available: boolean
  enabled: boolean
  count: number
  hint?: string
}): string {
  if (!input.available) return input.hint || '共同作品尚未启用或服务层未就绪'
  if (!input.enabled) return '共同作品还没打开：去「配置」面板的「共同作品」把「启用共同作品」打开。'
  if (input.count <= 0) return '她还什么都没写，聊到相关话题时你可以让她写一段。'
  return ''
}

/** 「让她起草」为什么不能按（空串 = 可以按）。 */
export function draftBlocker(input: {
  available: boolean
  enabled: boolean
  hasWork: boolean
  mayPropose: boolean
  reason?: string
  hint?: string
}): string {
  if (!input.available) return input.hint || '共同作品尚未启用或服务层未就绪'
  if (!input.enabled) return '共同作品没启用：去「配置」面板打开「共同作品」'
  if (!input.hasWork) return '还没有这件作品：聊到相关话题时让她写第一版'
  if (!input.mayPropose) return input.reason || '现在不能起草：上一次写手任务还没结束'
  return ''
}

/**
 * 「新建作品」为什么不能按（空串 = 可以按）。
 *
 * 起点只有一个：`SharedWorks.create`。已有作品时再建会被服务层拒（**绝不覆盖**），
 * 所以这里的规则只判"按钮该不该亮"，重复创建那条错误由后端原样透出来。
 */
export function createBlocker(input: {
  available: boolean
  enabled: boolean
  participants: number
  title: string
  content: string
  titleLimit?: number
  contentLimit?: number
  hint?: string
}): string {
  if (!input.available) return input.hint || '共同作品尚未启用或服务层未就绪'
  if (!input.enabled) return '共同作品没启用：去「配置」面板打开「共同作品」'
  if (input.participants <= 0) return '还没有已知参与者：先让她和这个账号说过话，再回来建作品'
  if (!input.title.trim()) return '给这件作品起个标题'
  if ((input.titleLimit ?? 0) > 0 && input.title.length > (input.titleLimit as number)) {
    return `标题太长（上限 ${input.titleLimit} 字）`
  }
  if (!input.content.trim()) return '初始正文不能为空'
  if ((input.contentLimit ?? 0) > 0 && input.content.length > (input.contentLimit as number)) {
    return `正文太长（上限 ${input.contentLimit} 字）`
  }
  return ''
}

/** 导出的文件名（标题里不能出现路径分隔符那种字符）。 */
export function exportFileName(title: string, workId: string): string {
  const cleaned = (title || '').replace(/[\\/:*?"<>|\s]+/g, '-').replace(/^-+|-+$/g, '')
  const stamp = (workId || '').slice(0, 8)
  return `hdsi-work-${cleaned || stamp || 'work'}.json`
}

/** 导出面板的一句话说明（分段数 + 字符数）。 */
export function exportSummary(count: number, chars: number): string {
  if (count <= 0) return '没有可导出的内容'
  return `共 ${count} 段，${chars} 字符：每段都在单条消息的安全长度内，可以逐段粘进 QQ`
}
