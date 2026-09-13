"""`Transport` 协议与 `NullTransport` 空实现。

上游 `src/service.ts` 里所有平台出站动作都挂在 `session.bot` 上；本移植版
统一收敛到 `self.transport`（见 `docs/PORT_PLAN_SERVICE.md` §7）：

| 上游调用点 | 上游写法 | Transport 方法 |
| --- | --- | --- |
| `:2076` | `session.bot.sendMessage(channelId, h('img', {src}))` | `send_image` |
| `:2119` | `session.bot.sendMessage(channelId, h('face', {id}))` | `send_native_face` |
| `:5156` | `session.send(outgoingContent)` | `send_session` |
| 私聊主动投递（Chunk6 出站发送） | `findBotForParticipant` + `sendPrivateMessage` | `send_private` |
| 群投递（Chunk2/Chunk6） | `sendGroupMessage(story, channelId, ...)` | `send_group` |
| 消息表态（`ExecutableMessageReaction`） | `bot.internal._request('set_msg_emoji_like')` | `react` |
| 群成员显示名（Chunk2 `groupMessages`） | `bot.getGroupMemberInfo` | `fetch_member_name` |
| 原生视觉（Chunk2 `resolveNativeImage`） | `this.ctx.http.get(url, {responseType: 'arraybuffer'})` | `fetch_image` |
| 原生音频（Chunk2 `resolveNativeAudio`） | `this.ctx.http('GET', url, {responseType: 'arraybuffer'})` | `fetch_audio` |
| 表情包文件列表（Chunk2 `listStickerFiles`） | `fs.readdir(root)` | `list_sticker_files` |
| 表情包投递（Chunk1/Chunk2 `sendSticker`） | `session.bot.sendMessage(channelId, h('img'))` | `send_sticker` |
| 网页观察（Chunk5，上游 Puppeteer） | `ctx.puppeteer.page()` | `search_web` / `visit_web` |
| typ-0 后台投递出口（Chunk0 `desktop_delivery_handler`） | bridge handler | `deliver_background` |

**降级原则**（`docs/PORT_PLAN_SERVICE.md` §8）：确实无法在 AstrBot 复现的能力
（Puppeteer 截图、sharp 抽帧、Satori 原生表情）必须返回失败而不是抛异常，
由调用方走既有的"投递失败"分支。`NullTransport` 就是那条降级路径的默认实现。
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .base import log_fallback

__all__ = ['BackgroundDelivery', 'NullTransport', 'Transport']

#: `send_private` / `send_group` 的返回结构（上游 `SendMessageResult` 的等价物）。
#: `ok=False` 时调用方按投递失败处理，`message_ids` 用于拆分投递的账本记录。
SendResult = dict[str, Any]


@runtime_checkable
class BackgroundDelivery(Protocol):
    """上游 `desktopDeliveryHandler` 的入参（`src/service.ts:712-715`）。"""

    participant_id: str
    self_id: str
    platform: str
    channel_id: str
    kind: str
    content: str
    quote_message_id: Optional[str]


@runtime_checkable
class Transport(Protocol):
    """平台出站能力的抽象。适配器侧（`plugin/adapters/astrbot_bridge.py`）实现。"""

    # ---- 私聊 / 群聊投递 ----

    async def send_private(
        self,
        participant: dict[str, Any],
        content: str,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """给一条关系分支发私聊消息。上游 `sendPrivateMessage(participant...)`。

        `participant` 是 `InterludeParticipant` 的 dict（snake_case 内部字段，
        数据库行则是 camelCase；实现里用 `pick()` 双读）。
        """
        ...

    async def send_group(
        self,
        channel_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """向群频道发送。上游 `sendGroupMessage(story, channelId, content, replyToMessageId)`。"""
        ...

    async def send_session(self, session: Any, content: str) -> SendResult:
        """对当前入站会话原路回复。上游 `session.send(outgoingContent)`（`:5156`）。

        等价于"给触发本回合的私聊/群聊回一条"，`session` 是 `SessionView`。
        """
        ...

    async def send_image(self, channel_id: str, file_path: str, is_group: bool = False) -> SendResult:
        """发送本地图片。上游 `session.bot.sendMessage(channelId, h('img'))`（`:2076`）。"""
        ...

    async def send_sticker(self, channel_id: str, file_path: str, is_group: bool = False) -> SendResult:
        """发送表情包资产。上游 Chunk1/Chunk2 的本地表情投递路径。"""
        ...

    async def send_native_face(
        self,
        channel_id: str,
        face_id: str,
        is_group: bool = False,
    ) -> SendResult:
        """发送平台原生表情。上游 `h('face', {id: Number(QQ_NATIVE_FACE_IDS[semantic])})`（`:2119`）。"""
        ...

    async def react(self, message_ref: str, reaction: str) -> bool:
        """给一条消息加表态。上游 `ExecutableMessageReaction` 的执行路径。

        `message_ref` 是 `NarrativeDecision.messageReactions[].messageRef` 的原样值；
        返回 `False` 表示平台不支持或投递失败（调用方按"未执行"记账）。
        """
        ...

    # ---- 读取辅助 ----

    async def fetch_member_name(self, channel_id: str, user_id: str) -> str:
        """取群成员显示名。上游 `bot.getGroupMemberInfo`（Chunk2 `groupMessages`）。"""
        ...

    async def fetch_image(self, url: str) -> Optional[bytes]:
        """下载图片原始字节。上游 `this.ctx.http.get(url, {responseType: 'arraybuffer'})`（`:2698`/`:2806`）。"""
        ...

    async def fetch_audio(self, url: str) -> Optional[bytes]:
        """下载音频原始字节。上游原生音频通道（Chunk2）。"""
        ...

    async def list_sticker_files(self, root: str) -> list[str]:
        """列出表情包目录下的文件。上游 `listStickerFiles(root)`（`:7309`）。"""
        ...

    # ---- 网页观察（上游 Puppeteer 的抽象替身） ----

    async def search_web(self, query: str, timeout_ms: int) -> list[dict[str, Any]]:
        """执行一次搜索。上游 `mode: 'search'` 的网页观察路径（Chunk5）。"""
        ...

    async def visit_web(self, url: str, timeout_ms: int) -> Optional[dict[str, Any]]:
        """访问一个 URL 并返回 `{url, title, excerpt, text}`。上游 `mode: 'visit'`（Chunk5）。"""
        ...

    # ---- typ-0 后台投递出口 ----

    async def deliver_background(self, delivery: dict[str, Any]) -> SendResult:
        """后台（无实时 Session）投递。上游 `desktopDeliveryHandler`（`:712-715`）。"""
        ...


class NullTransport:
    """全部方法安全降级的 `Transport`。

    默认返回失败/空值并记一条日志：调用方按既有的"投递失败/无观察"分支继续，
    绝不因为平台缺能力而中断叙事回合。
    """

    # ---- 私聊 / 群聊投递 ----

    async def send_private(
        self,
        participant: dict[str, Any],
        content: str,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        log_fallback('debug', 'Transport 未安装：私聊投递已跳过')
        return {'ok': False, 'error': 'transport-unavailable'}

    async def send_group(
        self,
        channel_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        log_fallback('debug', 'Transport 未安装：群投递已跳过')
        return {'ok': False, 'error': 'transport-unavailable'}

    async def send_session(self, session: Any, content: str) -> SendResult:
        log_fallback('debug', 'Transport 未安装：会话回复已跳过')
        return {'ok': False, 'error': 'transport-unavailable'}

    async def send_image(self, channel_id: str, file_path: str, is_group: bool = False) -> SendResult:
        log_fallback('debug', 'Transport 未安装：图片发送已跳过')
        return {'ok': False, 'error': 'transport-unavailable'}

    async def send_sticker(self, channel_id: str, file_path: str, is_group: bool = False) -> SendResult:
        log_fallback('debug', 'Transport 未安装：表情包发送已跳过')
        return {'ok': False, 'error': 'transport-unavailable'}

    async def send_native_face(
        self,
        channel_id: str,
        face_id: str,
        is_group: bool = False,
    ) -> SendResult:
        log_fallback('debug', 'Transport 未安装：原生表情发送已跳过')
        return {'ok': False, 'error': 'transport-unavailable'}

    async def react(self, message_ref: str, reaction: str) -> bool:
        log_fallback('debug', 'Transport 未安装：消息表态已跳过')
        return False

    # ---- 读取辅助 ----

    async def fetch_member_name(self, channel_id: str, user_id: str) -> str:
        return ''

    async def fetch_image(self, url: str) -> Optional[bytes]:
        return None

    async def fetch_audio(self, url: str) -> Optional[bytes]:
        return None

    async def list_sticker_files(self, root: str) -> list[str]:
        return []

    # ---- 网页观察 ----

    async def search_web(self, query: str, timeout_ms: int) -> list[dict[str, Any]]:
        log_fallback('debug', 'Transport 未安装：网页搜索已跳过')
        return []

    async def visit_web(self, url: str, timeout_ms: int) -> Optional[dict[str, Any]]:
        log_fallback('debug', 'Transport 未安装：网页访问已跳过')
        return None

    # ---- typ-0 后台投递出口 ----

    async def deliver_background(self, delivery: dict[str, Any]) -> SendResult:
        log_fallback('debug', 'Transport 未安装：后台投递已跳过')
        return {'ok': False, 'error': 'transport-unavailable'}
