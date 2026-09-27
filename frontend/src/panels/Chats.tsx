import { useState } from 'preact/hooks'
import { useQuery } from '../query'
import type { PanelProps } from '../main'
import type { ChatConversation, ChatHistoryPayload, ChatsPayload } from '../types'
import { awaitingText, bubbleSide, clockOf, conversationOrder, sideLabel } from '../chat-log'
import { Badge, Empty, ErrorNote, Icon, Loading, LongList, Panel, Stack } from '../components/ui'

const LIMITS: Array<[number, string]> = [[100, '最近 100'], [300, '最近 300'], [1000, '最近 1000']]

/** 一条会话的摘要行：名字 + 最后一句 + 未回复徽标。 */
function ConversationRow({
  item,
  active,
  onPick,
}: {
  item: ChatConversation
  active: boolean
  onPick: (key: string) => void
}) {
  const badge = awaitingText(item.awaiting, item.unread)
  return (
    <button
      type="button"
      onClick={() => onPick(item.conversation)}
      class={`flex w-full flex-col gap-1 rounded-lg border px-3 py-2 text-left transition ${
        active ? 'border-accent bg-accent/10' : 'border-line hover:bg-raised'
      }`}
    >
      <span class="flex items-center gap-2">
        <span class="truncate text-xs font-medium">{item.name}</span>
        {item.failed > 0 && <Badge tone="danger">失败 {item.failed}</Badge>}
        {badge && <Badge tone={item.awaiting > 0 ? 'accent' : 'neutral'}>{badge}</Badge>}
        <span class="ml-auto shrink-0 text-[11px] text-muted">{clockOf(item.last_at)}</span>
      </span>
      <span class="truncate text-[11px] text-muted">
        {item.configured && !item.messages ? '还没说过话' : (item.last_text || '—')}
      </span>
      <span class="text-[11px] text-muted">
        来信 {item.incoming} · 她发 {item.outgoing}
        {item.account ? ` · ${item.account}` : ''}
      </span>
    </button>
  )
}

export function Chats({ storyId, refreshKey }: PanelProps) {
  const [selected, setSelected] = useState('')
  const [limit, setLimit] = useState(300)
  const list = useQuery<ChatsPayload>('console/chats', { story_id: storyId }, { nonce: refreshKey })
  const history = useQuery<ChatHistoryPayload>(
    'console/chat-history',
    { story_id: storyId, conversation: selected, limit },
    { enabled: Boolean(selected), nonce: refreshKey },
  )

  if (list.error) return <ErrorNote text={list.error} onRetry={list.reload} />
  if (list.loading && !list.data) return <Loading />
  if (!list.data) return <Empty text="没有拿到数据" />
  if (!list.data.story) return <Empty text="还没有任何剧本。" icon="user" />

  const privateRows = conversationOrder(list.data.private ?? [])
  const groupRows = conversationOrder(list.data.groups ?? [])
  const total = [...privateRows, ...groupRows].reduce((sum, item) => sum + item.messages, 0)

  return (
    <Stack>
      <div class="grid grid-cols-1 gap-4 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <div class="flex flex-col gap-4">
          <Panel title={`私聊（${privateRows.length}）`} icon="user">
            {privateRows.length === 0 ? (
              <Empty text="还没有人和她私聊过。" icon="user" />
            ) : (
              <LongList
                items={privateRows}
                limit={6}
                unit="个人"
                render={(item) => (
                  <ConversationRow
                    key={item.conversation}
                    item={item}
                    active={item.conversation === selected}
                    onPick={setSelected}
                  />
                )}
              />
            )}
          </Panel>

          <Panel title={`群聊（${groupRows.length}）`} icon="link">
            {groupRows.length === 0 ? (
              <Empty text="还没有群聊记录。" icon="link" />
            ) : (
              <LongList
                items={groupRows}
                limit={6}
                unit="个群"
                render={(item) => (
                  <ConversationRow
                    key={item.conversation}
                    item={item}
                    active={item.conversation === selected}
                    onPick={setSelected}
                  />
                )}
              />
            )}
          </Panel>
        </div>

        <Panel
          title={history.data?.title || '聊天记录'}
          icon="script"
          actions={
            <>
              {LIMITS.map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setLimit(value)}
                  class={`rounded-md border px-2 py-1 text-[11px] ${
                    limit === value ? 'border-accent text-accent' : 'border-line text-muted hover:text-fg'
                  }`}
                >
                  {label}
                </button>
              ))}
            </>
          }
        >
          {!selected ? (
            <Empty text="左边选一个人或一个群，这里显示那次对话的往来记录。" icon="script" />
          ) : history.error ? (
            <ErrorNote text={history.error} onRetry={history.reload} />
          ) : history.loading && !history.data ? (
            <Loading />
          ) : !history.data || history.data.messages.length === 0 ? (
            <Empty text="这段时间里没有消息。" icon="script" />
          ) : (
            <div class="flex max-h-[36rem] flex-col gap-3 overflow-y-auto pr-1">
              {history.data.has_more && (
                <p class="text-[11px] text-muted">
                  只显示最后 {history.data.messages.length} 条；更早的记录没有取出来。
                </p>
              )}
              {history.data.messages.map((message) => {
                const side = bubbleSide(message.kind)
                const mine = side === 'out'
                return (
                  <article
                    key={`${message.entry_id}-${message.at}`}
                    class={`flex flex-col gap-1 ${side === 'system' ? 'items-center' : mine ? 'items-end' : 'items-start'}`}
                  >
                    <span class="flex items-center gap-2 text-[11px] text-muted">
                      <span>{message.sender || sideLabel(side)}</span>
                      <span>{clockOf(message.at)}</span>
                      {side === 'system' && <Badge tone="danger">没有发出去</Badge>}
                    </span>
                    <div
                      class={`max-w-[36rem] whitespace-pre-wrap break-words rounded-xl border px-3 py-2 text-xs ${
                        side === 'system'
                          ? 'border-danger/40 bg-danger/10 text-danger'
                          : mine
                            ? 'border-accent/40 bg-accent/10'
                            : 'border-line bg-raised'
                      }`}
                    >
                      {message.quote && (
                        <div class="mb-1 border-l-2 border-line pl-2 text-[11px] text-muted">{message.quote}</div>
                      )}
                      {message.text || '（空消息）'}
                    </div>
                  </article>
                )
              })}
            </div>
          )}
        </Panel>
      </div>

      <p class="flex items-start gap-1 text-[11px] text-muted">
        <Icon name="info" class="mt-0.5 h-3 w-3 shrink-0" />
        这里只列真正的对话：私聊按参与者、群聊按群号。旁白、场景与投递账本不出现在对话里（它们在「剧本」
        与「投递」面板）。扫描了 {list.data.scanned} 条聊天条目、共 {total} 条消息
        {list.data.truncated ? '；条目窗口已满，更早的记录没有扫描到。' : '。'}
      </p>
    </Stack>
  )
}
