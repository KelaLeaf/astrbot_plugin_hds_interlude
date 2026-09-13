"""`SessionView`：上游 Koishi `Session` 在 service 层的最小等价物。

上游 `src/service.ts` 全程只通过 `Session` 读取下面这些字段：

- `session.platform` / `session.selfId` / `session.userId`（33/29/22 次）
- `session.content`（9 次）、`session.channelId`（8 次）
- `session.username`（3 次）、`session.messageId`（3 次）
- `session.isDirect`（2 次）、`session.quote`（1 次，见 `describeQuotedMessage`）
- `session.bot`（`sendMessage`，2 次，见 `src/service.ts:2076` / `:2119`）
- `(session as any).guildId`（`sessionGroupId`，`src/service.ts:7246`）
- `session.send()`（1 次，`src/service.ts:5156`）
- `session.event`（适配器侧的可选原始事件，service 只做透传）

本移植版把这一组读取需求固化成**纯数据视图**：它是 `plugin/adapters/astrbot_bridge.py`
要构造的对象，`core/` 里任何位置都不得 `import astrbot`。

键名法（`docs/PORT_PLAN.md` §2）：本类字段是 Python 标识符，一律 snake_case；
`elements` / `quote` / `event` 里承载的是**外部读入**的适配器数据，既可能是上游
camelCase 也可能是 snake_case，因此一律通过 `pick()` 双读，优先 camelCase。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .base import normalize_group_id, pick

__all__ = ['SessionView']


#: 上游 `Session` 的 camelCase 键 → `SessionView` 的 snake_case 属性。
#: 键名法要求：内部结构用 snake_case，但从外部/上游读入时两种拼写都要认。
SESSION_VIEW_KEYS = {
    'platform': 'platform',
    'selfId': 'self_id', 'self_id': 'self_id',
    'userId': 'user_id', 'user_id': 'user_id',
    'channelId': 'channel_id', 'channel_id': 'channel_id',
    'guildId': 'guild_id', 'guild_id': 'guild_id',
    'isDirect': 'is_direct', 'is_direct': 'is_direct',
    'content': 'content',
    'elements': 'elements',
    'quote': 'quote',
    'messageId': 'message_id', 'message_id': 'message_id',
    'username': 'username',
    'event': 'event',
    'bot': 'bot',
}



def _pick_int(value: Any) -> Optional[int]:
    """读取可选整数（上游 `messageIndex` 这类字段）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


@dataclass
class SessionView:
    """一条入站事件的只读视图。

    上游 `Session` 由 Koishi 提供；这里只保留 service 真正读到的字段。
    适配器负责填好 `platform` / `self_id` / `user_id` / `content`，
    其余字段缺失时保持 `None` / 空容器，行为与上游弱读一致。
    """

    platform: str = ''
    self_id: str = ''
    user_id: str = ''
    channel_id: str = ''
    guild_id: str = ''
    is_direct: bool = False
    content: str = ''
    # 上游 `h.parse(session.content)` 的段列表：`{'type': ..., 'attrs': {...}, 'children': [...]}`。
    # 刻意不在这里解析 content —— 解析是 helpers 的纯函数（`extractSessionAudioSources` 等）。
    elements: list[dict[str, Any]] = field(default_factory=list)
    # 上游 `session.quote`（Koishi 的引用消息段）。`describeQuotedMessage` 读
    # `quote.user.id` / `quote.content` / `quote.elements`，因此这里存原始 dict。
    quote: Optional[dict[str, Any]] = None
    # 上游 `session.messageId`：宿主用来做表情表态/引用回复的定位。
    message_id: Optional[str] = None
    # 上游 `session.username`：`ensureParticipant` 的 displayName 兜底来源。
    username: str = ''
    # 适配器侧的原始事件对象（AstrBot 的 AstrMessageEvent）。service 只透传，
    # 解析一律留在 adapters 层。
    event: Any = None
    # 上游 `session.bot`。在 AstrBot 侧由 `self.transport` 承担全部出站动作，
    # 这里保留占位以兼容 `session.bot.selfId` 这类读取（`canHandleSession`
    # 只用 session.selfId，故默认 None 是安全的）。
    bot: Any = None

    # ---- Koishi `Session` 上 service 层用到的读取入口 ----

    def __getitem__(self, key: str) -> Any:
        """让 `pick(session, 'selfId', 'self_id')` 这类**双读**直接可用。

        键名法要求"从外部读入的值两种拼写都认"，而 service 层的解析工具
        （`sameParticipantEndpoint` / `sessionGroupId` / `canHandle*`）统一走
        `pick()` —— 因此视图必须同时接受上游 camelCase 与内部 snake_case 键，
        就像 dict 一样。未知键抛 `KeyError`，与 dict 语义一致。
        """
        attribute = SESSION_VIEW_KEYS.get(key)
        if attribute is None:
            raise KeyError(key)
        return getattr(self, attribute)

    def get(self, key: str, default: Any = None) -> Any:
        """dict 式读取（`session.get('username')`）。"""
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: str) -> bool:
        return key in SESSION_VIEW_KEYS

    def session_group_id(self) -> str:
        """上游 `sessionGroupId(session)`（`src/service.ts:7246`）。

        `String(session.guildId || session.channelId || '')` 的逐字等价物：
        JS 的 `||` 会把空字符串当假值，故空串要继续看 `channel_id`。
        """
        raw = self.guild_id or self.channel_id or ''
        return normalize_group_id(raw)

    def quote_user_id(self) -> str:
        """上游 `describeQuotedMessage` 读到的 `quote.user.id`。"""
        quote = self.quote if isinstance(self.quote, dict) else None
        if not quote:
            return ''
        user = quote.get('user')
        if not isinstance(user, dict):
            return ''
        return str(pick(user, 'id', 'user_id') or '')

    def quote_message_id(self) -> str:
        """引用消息自身的 id（上游 `quote.id` / `quote.messageId`）。"""
        quote = self.quote if isinstance(self.quote, dict) else None
        if not quote:
            return ''
        return str(pick(quote, 'id', 'message_id') or pick(quote, 'messageId', 'message_id') or '')

    def quote_content(self) -> Any:
        """引用消息的原始内容（可能是消息链，由 helpers 归一化）。"""
        quote = self.quote if isinstance(self.quote, dict) else None
        if not quote:
            return None
        return quote.get('content')

    def quoted_bot(self) -> bool:
        """上游 `quotesBot(session)`（`src/service.ts:7696`）。

        上游把两边都套了 `String(...)`：没有引用时是 `'' === '<selfId>'`，
        因此**空引用永远不等于自己**；这里保持同样的非空判定。
        """
        quote_user = self.quote_user_id()
        self_id = str(self.self_id or '')
        return bool(quote_user) and bool(self_id) and quote_user == self_id

    def mentioned_bot(self) -> bool:
        """上游 `mentionsBot(session)`（`src/service.ts:7578`）。

        `session.elements.some(e => e.type === 'at' && e.attrs.id === session.selfId)`。
        """
        self_id = str(self.self_id or '')
        for element in self.elements or []:
            if not isinstance(element, dict):
                continue
            if str(pick(element, 'type') or '') != 'at':
                continue
            attrs = element.get('attrs') if isinstance(element.get('attrs'), dict) else {}
            data = element.get('data') if isinstance(element.get('data'), dict) else {}
            target = pick(attrs, 'id', 'user_id') or pick(data, 'id', 'user_id')
            if str(target or '') == self_id:
                return True
        return False

    def message_index(self) -> Optional[int]:
        """上游 `IndexedQuotedMessageContext.messageIndex` 的读取口。"""
        return _pick_int(pick(self.quote, 'messageIndex', 'message_index')) if isinstance(self.quote, dict) else None
