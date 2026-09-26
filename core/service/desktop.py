"""`upstream/src/desktop-bridge.ts` 的移植：可选桌面（typ-0）桥。

上游把桌面端（Tauri worker）经 Node 子进程通道接进 `InterludeService`：
`installDesktopBridge(service)` 在 `HDSI_DESKTOP_BRIDGE=1` 且 `process.send`
可用时注册 `process.on('message')`，处理宿主发来的 `hdsi-desktop` 报文，
并用 `process.send` 把事件推回宿主。

本移植版没有 Koishi Console、没有 Node 子进程通道，改为**可选 HTTP 桥**：

* `DesktopBridge` 把上游的 **8 条 `hdsi-desktop` 命令**逐条实现成方法
  （`phase` / `inbound` / `replay-inbox` / `snapshot` / `timeline-range` /
  `delivery-result` / `purge-range` / `cursor-set`），由 AstrBot 侧（HTTP 路由或
  管理命令）按需调用；
* 事件出口是一个可注入的 `sink`（等价上游 `sendToDesktop`），HTTP 模式下
  同步返回的事件还可以用 `drain_events()` 取走；
* 上游 `installDesktopBridge(service)` 的语义由模块级
  `install_desktop_bridge(service, sink, ...)` 保留，差别是它返回**桥对象**
  （调 `.stop()` 卸载）而不是一个 disposer 闭包。

## 命令 → 方法对照（`desktop-bridge.ts:206-266`）

| 命令 | 方法 | 成功事件 | 校验失败文案 |
| --- | --- | --- | --- |
| `phase` | `phase()` | `phase-result` | 无效 typ-0 运行状态请求。 |
| `inbound` | `inbound()` | `inbound-result` | 无效 typ-0 入站事件。 |
| `replay-inbox` | `replay_inbox()` | `replay-result` | 回放请求缺少 requestId。 |
| `snapshot` | `snapshot()` | `snapshot-result` | 快照请求缺少 requestId。 |
| `timeline-range` | `timeline_range()` | `timeline-range-result` | 无效时间线范围请求。 |
| `delivery-result` | `settle_delivery()` | （回执，无响应事件） | — |
| `purge-range` | `purge_range()` | `purge-range-result` | 选区删除请求缺少 requestId。／选区删除时间范围无效。 |
| `cursor-set` | `cursor_set()` | `cursor-set-result` | 无效 typ-0 游标设置请求。／游标时间无法解析。 |

失败一律按上游 `handle()` 的 catch 分支：先发**同名的 `*-result`**
（`{requestId, accepted: false, error}`，`replay-inbox` 额外带 `results: []`），
再发一条 `error` 事件。**`cursor-set` 是上游三元链上的漏网之鱼**：它的失败响应
事件名落到默认值 `error`，于是校验失败时会连发**两条都叫 `error`** 的事件
（先失败信封、再通用错误）。这不是笔误，是逐字保留的上游行为。
未知命令与上游一致：**静默忽略**，不发任何事件。

## 安全降级分支（移植约定）

1. **`process.send` → `sink`**：上游 `sendToDesktop()` 在没有 `process.send` 时
   直接返回；本移植版 `emit()` 在没有 sink 时仍把事件记进缓冲区
   （`drain_events()` 可取），有 sink 时再推给它，且 sink 抛异常被吞掉。
2. **`HDSI_DESKTOP_BRIDGE` / `typeof process.send === 'function'` 双闸门**：
   本移植版只保留环境变量闸门（`enabled=`），因为 HTTP 模式不要求 sink 存在。
3. **`console-port` 上报（`desktop-bridge.ts:172-190`）**：上游探测
   `ctx.server.port` 把 worker 真实绑定的 loopback 端口回传宿主，宿主据此登记
   Console endpoint。本移植版**没有 Koishi Console / loopback server**（AstrBot
   用自身 WebUI 路由），`_read_server_port()` 只会返回 `None`：
   `report_console_port()` 因此返回 `False` 并且**不启动 1s 轮询**。这条路径保留
   是为了让"宿主自己提供了 `ctx.server.port`"的嵌入场景（例如把 AstrBot 的
   HTTP 端口塞进 ctx）仍能工作，而不是留一个空占位。
4. **`setInterval` 心跳 → `ctx.set_interval`**：上游每 10s 发 `heartbeat`。
   没有可用计时器（无运行中的事件循环）时心跳不启动，桥的其余能力不受影响。
5. **上游 disposer 不重置 event sink**；本移植版的 `stop()` 额外
   `set_desktop_event_sink(None)`，避免停桥后仍向已死的 sink 推送。

## 键名约定

桌面报文是**外部不可信输入**，因此所有读取都走 `pick(value, 'camelCase',
'snake_case')` 双读、优先上游 camelCase（比上游只认 camelCase 更宽容，不会漏读）。
事件出口（`emit`）与响应 payload 的键名**逐字保持上游 camelCase**
（`requestId` / `accept` 之外的 `cursorAt` / `storyId` / `messageIds` …），
因为那是发给桌面端的 wire format。

不 import `astrbot`，不依赖 Koishi 运行时。
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import uuid
from collections import deque
from typing import Any, Callable, Optional

from ..time import iso, parse_dt, utc_now
from .base import log_fallback, pick
from .session import SessionView

__all__ = [
    'DESKTOP_BRIDGE_ENV',
    'DESKTOP_PHASE_ENV',
    'DesktopBridge',
    'DesktopInboundEvent',
    'DesktopRuntimePhase',
    'DesktopSessionView',
    'DesktopTimelineRangeRequest',
    'desktop_session',
    'escape_attribute',
    'install_desktop_bridge',
    'is_inbound_event',
    'is_phase',
    'is_request_id',
    'is_timeline_range_request',
]

#: 上游闸门环境变量：`process.env.HDSI_DESKTOP_BRIDGE !== '1'` 时不安装桥。
DESKTOP_BRIDGE_ENV = 'HDSI_DESKTOP_BRIDGE'
#: 上游 `process.env.HDSI_PHASE`：worker 启动时的初始运行状态。
DESKTOP_PHASE_ENV = 'HDSI_PHASE'

#: 上游 `DesktopRuntimePhase`。
DesktopRuntimePhase = str
#: 上游 `DesktopDeliveryStatus`。
DesktopDeliveryStatus = str
#: 上游 `DesktopInboundEvent`（运行期是 dict）。
DesktopInboundEvent = dict
#: 上游 `DesktopTimelineRangeRequest`（运行期是 dict）。
DesktopTimelineRangeRequest = dict

_RUNTIME_PHASES = ('running', 'muted', 'paused')

#: 入站事件正文长度上限（上游 `value.content.length <= 128_000`）。
MAX_INBOUND_CONTENT_LENGTH = 128_000
#: 时间线范围一页的上限（上游 `query.limit > 500` 判非法）。
MAX_TIMELINE_RANGE_LIMIT = 500
#: 时间线轨道数上限（上游 `query.tracks.length > 8` 判非法）。
MAX_TIMELINE_TRACKS = 8
#: 时间线游标长度上限（上游 `query.cursor.length > 80` 判非法）。
MAX_TIMELINE_CURSOR_LENGTH = 80

#: 上游 `escapeAttribute()` 的替换表。
_ESCAPE_ATTRIBUTE = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}
_ESCAPE_PATTERN = re.compile(r'[&<>"\']')

#: 命令名 → 处理方法名（`_dispatch` 的静态路由表）。
_COMMAND_HANDLERS = {
    'phase': 'phase',
    'inbound': 'inbound',
    'replay-inbox': 'replay_inbox',
    'snapshot': 'snapshot',
    'timeline-range': 'timeline_range',
    'purge-range': 'purge_range',
    'cursor-set': 'cursor_set',
}

#: 命令名 → 失败响应的 `*-result` 事件名（上游 `handle()` catch 分支的三元链）。
_RESPONSE_BY_COMMAND = {
    'phase': 'phase-result',
    'inbound': 'inbound-result',
    'replay-inbox': 'replay-result',
    'snapshot': 'snapshot-result',
    'timeline-range': 'timeline-range-result',
    'purge-range': 'purge-range-result',
}

#: 各命令缺失 `requestId` 时的专属文案（其余命令共用 `error` 事件，无专属文案）。
_MISSING_REQUEST_ID_MESSAGE = {
    'replay-inbox': '回放请求缺少 requestId。',
    'snapshot': '快照请求缺少 requestId。',
    'purge-range': '选区删除请求缺少 requestId。',
}


def _js_string(value: Any) -> str:
    """JS `String(value)`（`String(undefined)` → `'undefined'`，与 Python `str` 不同）。"""
    if value is None:
        return 'undefined'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    return str(value)


def _request_id(value: Any) -> Any:
    return pick(value, 'requestId', 'request_id') if isinstance(value, dict) else None


def _is_finite_number(value: Any) -> bool:
    """JS `Number.isFinite(value)`：真数值且非 NaN/±Infinity（布尔不算）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _parse_date(value: Any) -> Any:
    """`new Date(String(value ?? ''))`：解析失败返回 `None`（JS 的 `NaN` 日期）。"""
    if value is None:
        text = ''
    elif isinstance(value, str):
        text = value
    else:
        text = _js_string(value)
    return parse_dt(text)


def escape_attribute(value: Any) -> str:
    """上游 `escapeAttribute(value)`：`[&<>"']` 全量实体化。"""
    return _ESCAPE_PATTERN.sub(lambda match: _ESCAPE_ATTRIBUTE[match.group(0)], _js_string(value))


# =========================================================================== #
# 参数校验（上游 desktop-bridge.ts:63-88 的四个 type guard）
# =========================================================================== #

def is_phase(value: Any) -> bool:
    """上游 `isPhase(value)`（`:63`）。"""
    return value in _RUNTIME_PHASES


def is_request_id(value: Any) -> bool:
    """上游 `isRequestId(value)`（`:67`）：8 ≤ 长度 ≤ 128 的字符串。"""
    return isinstance(value, str) and 8 <= len(value) <= 128


def is_inbound_event(value: Any) -> bool:
    """上游 `isInboundEvent(value)`（`:71`）。

    `accountKey` / `platform` / `selfId` / `senderId` 必须是字符串，`content`
    必须是不超过 128000 字符的字符串，`kind` 二选一，群聊必须有 `channelId`，
    `occurredAt` 必须能被 `Date.parse` 解析。
    """
    if not isinstance(value, dict):
        return False
    for key, snake in (
        ('accountKey', 'account_key'),
        ('platform', 'platform'),
        ('selfId', 'self_id'),
        ('senderId', 'sender_id'),
    ):
        if not isinstance(pick(value, key, snake), str):
            return False
    content = pick(value, 'content')
    if not isinstance(content, str) or len(content) > MAX_INBOUND_CONTENT_LENGTH:
        return False
    kind = pick(value, 'kind')
    if kind not in ('private', 'group'):
        return False
    if kind == 'group' and not isinstance(pick(value, 'channelId', 'channel_id'), str):
        return False
    occurred_at = pick(value, 'occurredAt', 'occurred_at')
    # 上游用 `Date.parse`：非字符串（数字/布尔/对象）一律 NaN → 非法。
    if not isinstance(occurred_at, str):
        return False
    return _parse_date(occurred_at) is not None


def is_timeline_range_request(value: Any) -> bool:
    """上游 `isTimelineRangeRequest(value)`（`:81`）。`None` 是合法的（全默认）。"""
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    for key in ('from', 'to'):
        item = value.get(key)
        if item is None:
            continue
        if not isinstance(item, str) or _parse_date(item) is None:
            return False
    tracks = value.get('tracks')
    if tracks is not None:
        if not isinstance(tracks, list) or len(tracks) > MAX_TIMELINE_TRACKS:
            return False
        if any(not isinstance(track, str) for track in tracks):
            return False
    cursor = value.get('cursor')
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > MAX_TIMELINE_CURSOR_LENGTH:
            return False
    limit = value.get('limit')
    if limit is not None:
        if not _is_finite_number(limit) or limit < 1 or limit > MAX_TIMELINE_RANGE_LIMIT:
            return False
    detail_level = pick(value, 'detailLevel', 'detail_level')
    if detail_level is None:
        return True
    return detail_level in ('summary', 'full')


# =========================================================================== #
# 入站事件 → SessionView（上游 `desktopSession()`，`:96-124`）
# =========================================================================== #

class DesktopSessionView(SessionView):
    """上游 `desktopSession()` 造出的 Koishi `Session` 子集。

    比普通 `SessionView` 只多一个 `send()`：上游把它接成
    `requestDelivery({...})`，本移植版接到同一个 `request_delivery` 回调上。
    """

    def __init__(
        self,
        *,
        request_delivery: Callable[[dict[str, Any]], Any],
        delivery_payload: dict[str, Any],
        **fields: Any,
    ):
        super().__init__(**fields)
        self._request_delivery = request_delivery
        self._delivery_payload = dict(delivery_payload)

    async def send(self, outgoing: Any) -> list[str]:
        """上游 `session.send(outgoing)`：走宿主渠道投递并等待回执。"""
        payload = dict(self._delivery_payload)
        payload['content'] = _js_string(outgoing)
        return await self._request_delivery(payload)


def desktop_session(
    event: Any,
    request_delivery: Callable[[dict[str, Any]], Any],
) -> DesktopSessionView:
    """上游 `desktopSession(event, requestDelivery)`（`:96`）逐条移植。

    `channelId` 群聊取 `event.channelId`，私聊取 `event.channelId ||
    'private:' + event.senderId`；正文是 `event.content` 拼上每个
    `imageSources` 的 `<img src="...">`（空串被丢掉，与 JS `.filter(Boolean)` 一致）。
    """
    record = event if isinstance(event, dict) else {}
    kind = pick(record, 'kind')
    sender_id = pick(record, 'senderId', 'sender_id') or ''
    channel_id = pick(record, 'channelId', 'channel_id')
    if kind == 'group':
        channel_id = channel_id or ''
    else:
        channel_id = channel_id or 'private:%s' % sender_id
    image_sources = pick(record, 'imageSources', 'image_sources')
    if not isinstance(image_sources, list):
        image_sources = []
    pieces = [_js_string(pick(record, 'content') or '')]
    pieces.extend('<img src="%s">' % escape_attribute(source) for source in image_sources)
    content = ''.join(piece for piece in pieces if piece)
    raw_message_id = pick(record, 'rawMessageId', 'raw_message_id')
    delivery_payload = {
        'accountKey': pick(record, 'accountKey', 'account_key'),
        'transport': pick(record, 'transport'),
        'platform': pick(record, 'platform'),
        'selfId': pick(record, 'selfId', 'self_id'),
        'channelId': channel_id,
        'kind': kind,
        'replyTo': raw_message_id,
    }
    return DesktopSessionView(
        request_delivery=request_delivery,
        delivery_payload=delivery_payload,
        platform=pick(record, 'platform') or '',
        self_id=pick(record, 'selfId', 'self_id') or '',
        user_id=sender_id,
        channel_id=channel_id or '',
        guild_id=(channel_id or '') if kind == 'group' else '',
        is_direct=kind == 'private',
        content=content,
        quote=pick(record, 'quote'),
        message_id=raw_message_id,
        username=pick(record, 'senderName', 'sender_name') or sender_id,
    )


class _PendingDelivery:
    """一条等待渠道回执的投递（上游 `PendingDelivery`）。"""

    __slots__ = ('future', 'timer')

    def __init__(self, future: 'asyncio.Future[list[str]]', timer: Any):
        self.future = future
        self.timer = timer


# =========================================================================== #
# DesktopBridge
# =========================================================================== #

class DesktopBridge:
    """可选桌面桥：把 `hdsi-desktop` 命令接到 `InterludeService`。

    `sink(event, payload)` 是上游 `sendToDesktop()` 的等价出口（AstrBot 侧通常
    接到 WebSocket / 长轮询队列）。`enabled=None` 时读环境变量
    `HDSI_DESKTOP_BRIDGE == '1'`（上游闸门）。
    """

    #: 上游 `bridge-ready` 里上报的协议版本（`:283`）。
    PROTOCOL = 4
    #: 上游 `requestDelivery` 的 45s 回执超时。
    DELIVERY_TIMEOUT_MS = 45_000
    #: 上游 `heartbeat` 的 10s 周期。
    HEARTBEAT_INTERVAL_MS = 10_000
    #: `console-port` 未就绪时的轮询周期与总窗口（上游 `:186-189`）。
    CONSOLE_PORT_POLL_MS = 1_000
    CONSOLE_PORT_POLL_WINDOW_MS = 60_000
    #: 事件缓冲区上限（HTTP 模式下用 `drain_events()` 取走，避免无界增长）。
    EVENT_BUFFER_LIMIT = 256

    def __init__(
        self,
        service: Any,
        sink: Optional[Callable[[str, Any], None]] = None,
        *,
        enabled: Optional[bool] = None,
        heartbeat_interval_ms: Optional[int] = None,
        delivery_timeout_ms: Optional[int] = None,
    ):
        self.service = service
        self._sink = sink
        self._enabled = (os.environ.get(DESKTOP_BRIDGE_ENV) == '1') if enabled is None else bool(enabled)
        self._heartbeat_interval_ms = (
            self.HEARTBEAT_INTERVAL_MS if heartbeat_interval_ms is None else int(heartbeat_interval_ms)
        )
        self._delivery_timeout_ms = (
            self.DELIVERY_TIMEOUT_MS if delivery_timeout_ms is None else int(delivery_timeout_ms)
        )
        self._pending_deliveries: dict[str, _PendingDelivery] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=self.EVENT_BUFFER_LIMIT)
        self._collector: Optional[list[dict[str, Any]]] = None
        self._heartbeat: Any = None
        self._started = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        """上游 `HDSI_DESKTOP_BRIDGE !== '1'` 的反面。"""
        return self._enabled

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> 'DesktopBridge':
        """上游 `installDesktopBridge(service)` 的就绪段（`:126-283`）逐条移植。

        顺序与上游一致：挂 event sink → 挂后台投递通道 → 读初始 phase →
        启动心跳 → 探测 console-port → 应用初始 phase → 发 `bridge-ready`。
        `bridge-ready` 在 `finally` 里发，任何失败都不影响它。
        """
        self.service.set_desktop_event_sink(self._emit_to_sink)
        self.service.set_desktop_delivery_handler(self._background_delivery)
        self._started = True
        self._start_heartbeat()
        self.report_console_port()
        initial_phase = os.environ.get(DESKTOP_PHASE_ENV)
        if not is_phase(initial_phase):
            initial_phase = 'running'
        try:
            await self.service.set_desktop_runtime_phase(initial_phase)
        except Exception as error:
            self.emit('error', {'command': 'initial-phase', 'message': str(error)})
        finally:
            self.emit('bridge-ready', {
                'protocol': self.PROTOCOL,
                'phase': self.service.get_desktop_runtime_phase(),
            })
        return self

    def stop(self) -> None:
        """上游 disposer（`:284-295`）逐条移植 + 一处安全加固。

        上游清心跳、卸投递通道、摘消息监听、拒绝全部挂起投递；**不**重置
        event sink。本移植版额外把 event sink 置空（见模块 docstring 第 5 条）。
        """
        if self._heartbeat is not None:
            cancel = getattr(self._heartbeat, 'cancel', None)
            if callable(cancel):
                cancel()
            self._heartbeat = None
        self.service.set_desktop_delivery_handler(None)
        self.service.set_desktop_event_sink(None)
        self._started = False
        for pending in list(self._pending_deliveries.values()):
            timer_cancel = getattr(pending.timer, 'cancel', None)
            if callable(timer_cancel):
                timer_cancel()
            if not pending.future.done():
                pending.future.set_exception(RuntimeError('typ-0 bridge 已关闭。'))
        self._pending_deliveries.clear()

    def __enter__(self) -> 'DesktopBridge':
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    # 事件出口（上游 `sendToDesktop`，`:90-94`）
    # ------------------------------------------------------------------ #

    def emit(self, event: str, payload: Any) -> dict[str, Any]:
        """上游 `sendToDesktop(event, payload)`：`{type:'hdsi-desktop', event, payload}`。

        事件永远先记进缓冲区（HTTP 模式可取）；有 sink 时再推给 sink，sink 的
        异常被吞掉（上游 `try { process.send(...) } catch {}`）。
        """
        envelope = {'type': 'hdsi-desktop', 'event': event, 'payload': payload}
        self._events.append(envelope)
        if self._collector is not None:
            self._collector.append(envelope)
        if self._sink is not None:
            try:
                self._sink(event, payload)
            except Exception:  # pragma: no cover - 宿主可以在关闭过程中消失
                pass
        return envelope

    def drain_events(self) -> list[dict[str, Any]]:
        """取走缓冲区里的事件（HTTP 桥的轮询出口；上游没有对应物，纯新增）。"""
        events = list(self._events)
        self._events.clear()
        return events

    def _emit_to_sink(self, event: str, payload: Any) -> None:
        """`service.set_desktop_event_sink(...)` 用的回调（上游直接传 `sendToDesktop`）。"""
        self.emit(event, payload)

    # ------------------------------------------------------------------ #
    # 后台投递通道（上游 `setDesktopDeliveryHandler` 段，`:146-171`）
    # ------------------------------------------------------------------ #

    async def request_delivery(self, payload: dict[str, Any]) -> list[str]:
        """上游 `requestDelivery(payload)`（`:131`）：发 `delivery` 事件等 45s 回执。"""
        delivery_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: 'asyncio.Future[list[str]]' = loop.create_future()
        timer = loop.call_later(
            max(0.0, self._delivery_timeout_ms / 1000.0),
            self._expire_delivery,
            delivery_id,
            future,
        )
        self._pending_deliveries[delivery_id] = _PendingDelivery(future, timer)
        request = dict(payload)
        request['deliveryId'] = delivery_id
        request['occurredAt'] = iso(utc_now())
        self.emit('delivery', request)
        return await future

    def _expire_delivery(self, delivery_id: str, future: 'asyncio.Future[list[str]]') -> None:
        """上游 `setTimeout` 分支：超时后从挂起表摘掉并拒绝。"""
        if self._pending_deliveries.pop(delivery_id, None) is None:
            return
        if not future.done():
            future.set_exception(RuntimeError('等待 typ-0 渠道投递确认超时。'))

    def settle_delivery(self, value: Any) -> bool:
        """上游 `settleDelivery(value)`（`:136`）：`delivery-result` 回执结算。

        返回 `True` 表示确实结算了一条挂起投递（上游同样返回布尔）。
        """
        if not isinstance(value, dict):
            return False
        delivery_id = value.get('deliveryId')
        if not isinstance(delivery_id, str):
            return False
        pending = self._pending_deliveries.pop(delivery_id, None)
        if pending is None:
            return False
        timer_cancel = getattr(pending.timer, 'cancel', None)
        if callable(timer_cancel):
            timer_cancel()
        status = value.get('status')
        if status == 'sent':
            message_ids = value.get('messageIds')
            ids = [item for item in message_ids if isinstance(item, str)] if isinstance(message_ids, list) else []
            if not pending.future.done():
                pending.future.set_result(ids)
        else:
            error = value.get('error') or ('渠道投递失败：%s' % status)
            if not pending.future.done():
                pending.future.set_exception(RuntimeError(str(error)))
        return True

    async def _background_delivery(self, delivery: Any) -> dict[str, Any]:
        """上游 `setDesktopDeliveryHandler(...).catch(...)`（`:147-171`）。

        后台投递（delayed/split/advance）没有实时 Session，走同一条 `delivery`
        事件；失败时把异常包成 `{ok: False, error}`，绝不让宿主回合崩掉。
        """
        try:
            message_ids = await self.request_delivery({
                'accountKey': 'desktop:%s' % (pick(delivery, 'selfId', 'self_id') or ''),
                'transport': 'onebot-external',
                'platform': pick(delivery, 'platform'),
                'selfId': pick(delivery, 'selfId', 'self_id'),
                'channelId': pick(delivery, 'channelId', 'channel_id'),
                'kind': pick(delivery, 'kind'),
                'replyTo': pick(delivery, 'quoteMessageId', 'quote_message_id'),
                'content': pick(delivery, 'content'),
            })
        except Exception as error:
            return {'ok': False, 'error': str(error)}
        return {'ok': True, 'messageIds': message_ids}

    # ------------------------------------------------------------------ #
    # 心跳与 console-port（上游 `:172-190`）
    # ------------------------------------------------------------------ #

    def heartbeat_once(self) -> dict[str, Any]:
        """上游 `setInterval(() => sendToDesktop('heartbeat', ...), 10_000)`（`:173`）。"""
        return self.emit('heartbeat', {
            'phase': self.service.get_desktop_runtime_phase(),
            'at': iso(utc_now()),
        })

    def _start_heartbeat(self) -> None:
        """挂心跳；没有可用计时器（无运行中的事件循环）时静默跳过。"""
        if self._heartbeat_interval_ms <= 0:
            return
        ctx = getattr(self.service, 'ctx', None)
        setter = getattr(ctx, 'set_interval', None)
        if not callable(setter):
            return
        try:
            self._heartbeat = setter(self.heartbeat_once, self._heartbeat_interval_ms)
        except RuntimeError:  # pragma: no cover - 无运行中的事件循环
            self._heartbeat = None

    def _read_server_port(self) -> Optional[int]:
        """上游 `readServerPort()`（`:176`）：探测 `ctx.server.port`。"""
        ctx = getattr(self.service, 'ctx', None)
        server = getattr(ctx, 'server', None)
        port = getattr(server, 'port', None)
        if isinstance(port, bool) or not isinstance(port, int) or port <= 0:
            return None
        return port

    def report_console_port(self) -> bool:
        """上游 `reportConsolePort()`（`:181`）：把 loopback 端口回传宿主。

        本移植版没有 Koishi Console（见模块 docstring 第 3 条）：没有
        `ctx.server.port` 时返回 `False`，并**不**启动上游那段 1s 轮询
        （轮询的是不存在的东西，只会白烧计时器）。
        """
        port = self._read_server_port()
        if not port:
            return False
        self.emit('console-port', {'port': port, 'uiPath': '/console/'})
        return True

    # ------------------------------------------------------------------ #
    # 报文入口（上游 `handle`，`:205-266`）
    # ------------------------------------------------------------------ #

    async def handle(self, message: Any) -> Optional[dict[str, Any]]:
        """上游 `process.on('message', handle)`（`:205`）的等价入口。

        返回 `{'command': ..., 'events': [...]}` —— 本次调用产生的全部桌面事件
        （HTTP 桥据此同步回包）；非 `hdsi-desktop` 报文返回 `None`。
        `delivery-result` 与上游一样只做结算、不发响应事件。
        """
        command = message if isinstance(message, dict) else None
        if not command or command.get('type') != 'hdsi-desktop':
            return None
        name = command.get('command')
        value = command.get('value')
        if name == 'delivery-result':
            self.settle_delivery(value)
            return None
        self._collector = []
        try:
            await self._dispatch(name, value)
        except Exception as error:
            self.report_command_failure(name, value, error)
        finally:
            events = self._collector or []
            self._collector = None
        return {'command': name, 'events': events}

    async def _dispatch(self, name: Any, value: Any) -> None:
        """按命令名路由（上游 `handle` 里的 if 链；未知命令静默忽略）。"""
        handler_name = _COMMAND_HANDLERS.get(name)
        if handler_name is None:
            return
        handler = getattr(self, handler_name)
        await handler(value if isinstance(value, dict) else {})

    def report_command_failure(self, name: Any, value: Any, error: Any) -> None:
        """上游 `handle()` 的 catch 分支（`:257-265`）逐条移植。"""
        request_id = _request_id(value)
        response_event = _RESPONSE_BY_COMMAND.get(name, 'error')
        payload: dict[str, Any] = {
            'requestId': request_id,
            'accepted': False,
            'error': str(error),
        }
        if name == 'replay-inbox':
            payload['results'] = []
        self.emit(response_event, payload)
        self.emit('error', {'command': name, 'requestId': request_id, 'message': str(error)})

    # ------------------------------------------------------------------ #
    # 命令实现
    # ------------------------------------------------------------------ #

    async def phase(self, value: dict[str, Any]) -> dict[str, Any]:
        """`phase`：切换运行状态（`desktop-bridge.ts:211-216`）。"""
        request_id = _request_id(value)
        target = pick(value, 'phase')
        if not is_request_id(request_id) or not is_phase(target):
            raise ValueError('无效 typ-0 运行状态请求。')
        await self.service.set_desktop_runtime_phase(target)
        payload = {'requestId': request_id, 'accepted': True, 'phase': target}
        self.emit('phase-result', payload)
        return payload

    async def inbound(self, value: dict[str, Any]) -> dict[str, Any]:
        """`inbound`：投递一条实时入站事件（`:218-223`）。"""
        request_id = _request_id(value)
        event = pick(value, 'event')
        if not is_request_id(request_id) or not is_inbound_event(event):
            raise ValueError('无效 typ-0 入站事件。')
        accepted = self.service.get_desktop_runtime_phase() == 'running' and bool(
            await self.service.receive_desktop_event(event, desktop_session(event, self.request_delivery)),
        )
        payload: dict[str, Any] = {'requestId': request_id, 'accepted': bool(accepted)}
        if not accepted:
            payload['error'] = '当前剧本未接收该入站事件。'
        self.emit('inbound-result', payload)
        return payload

    async def cursor_set(self, value: dict[str, Any]) -> dict[str, Any]:
        """`cursor-set`：设置故事游标时间（`:226-233`）。"""
        request_id = _request_id(value)
        cursor_at = pick(value, 'cursorAt', 'cursor_at')
        if not is_request_id(request_id) or not isinstance(cursor_at, str):
            raise ValueError('无效 typ-0 游标设置请求。')
        parsed = parse_dt(cursor_at)
        if parsed is None:
            raise ValueError('游标时间无法解析。')
        await self.service.set_desktop_cursor_at(parsed)
        payload = {'requestId': request_id, 'accepted': True, 'cursorAt': iso(parsed)}
        self.emit('cursor-set-result', payload)
        return payload

    async def replay_inbox(self, value: dict[str, Any]) -> dict[str, Any]:
        """`replay-inbox`：批量回放离线收件箱（`:235-253`）。

        单条记录失败不中断整批：写进该条的 `error` 后继续（上游同样如此）。
        """
        request_id = _request_id(value)
        if not is_request_id(request_id):
            raise ValueError(_MISSING_REQUEST_ID_MESSAGE['replay-inbox'])
        records = pick(value, 'records')
        if not isinstance(records, list):
            records = []
        results: list[dict[str, Any]] = []
        for record in records:
            records_entry = record if isinstance(record, dict) else {}
            record_id = pick(records_entry, 'id')
            event = pick(records_entry, 'event')
            if not record_id or not is_inbound_event(event):
                results.append({
                    'id': _js_string(record_id or ''),
                    'accepted': False,
                    'error': '无效收件箱记录。',
                })
                continue
            try:
                accepted = self.service.get_desktop_runtime_phase() == 'running' and bool(
                    await self.service.receive_desktop_event(
                        event, desktop_session(event, self.request_delivery),
                    ),
                )
            except Exception as error:
                results.append({'id': record_id, 'accepted': False, 'error': str(error)})
                continue
            entry: dict[str, Any] = {'id': record_id, 'accepted': bool(accepted)}
            if not accepted:
                entry['error'] = '当前剧本未接收该入站事件。'
            results.append(entry)
        payload = {'requestId': request_id, 'results': results}
        self.emit('replay-result', payload)
        return payload

    async def snapshot(self, value: dict[str, Any]) -> dict[str, Any]:
        """`snapshot`：只读故事时间线快照（`:255-259`）。"""
        request_id = _request_id(value)
        if not is_request_id(request_id):
            raise ValueError(_MISSING_REQUEST_ID_MESSAGE['snapshot'])
        payload = {'requestId': request_id, 'snapshot': await self.service.desktop_timeline_snapshot()}
        self.emit('snapshot-result', payload)
        return payload

    async def timeline_range(self, value: dict[str, Any]) -> dict[str, Any]:
        """`timeline-range`：窗口化的时间线投影（`:261-265`）。"""
        request_id = _request_id(value)
        query = pick(value, 'query')
        if not is_request_id(request_id) or not is_timeline_range_request(query):
            raise ValueError('无效时间线范围请求。')
        payload = {
            'requestId': request_id,
            'projection': await self.service.desktop_timeline_range(query),
        }
        self.emit('timeline-range-result', payload)
        return payload

    async def purge_range(self, value: dict[str, Any]) -> dict[str, Any]:
        """`purge-range`：桌面 GUI 化的选区删除（`:267-276`）。

        `desktop_purge_range` 内部走 serial 队列（与写作回合互斥）并复用
        `purge_story_range` 的软删语义。
        """
        request_id = _request_id(value)
        if not is_request_id(request_id):
            raise ValueError(_MISSING_REQUEST_ID_MESSAGE['purge-range'])
        from_value = _parse_date(value.get('from'))
        to_value = _parse_date(value.get('to'))
        if from_value is None or to_value is None or from_value > to_value:
            raise ValueError('选区删除时间范围无效。')
        result = await self.service.desktop_purge_range(from_value, to_value)
        payload = {
            'requestId': request_id,
            'accepted': True,
            'storyId': pick(result, 'storyId', 'story_id'),
        }
        self.emit('purge-range-result', payload)
        return payload


async def install_desktop_bridge(
    service: Any,
    sink: Optional[Callable[[str, Any], None]] = None,
    *,
    enabled: Optional[bool] = None,
    **options: Any,
) -> Optional[DesktopBridge]:
    """上游 `installDesktopBridge(service)`（`:126`）的等价入口。

    闸门关闭时返回 `None`（上游返回 `undefined`）；否则返回**已就绪**的
    `DesktopBridge`，调用方持有它并在卸载时 `bridge.stop()`
    （上游返回的是 disposer 闭包，语义等价）。

    `sink` 是事件出口；AstrBot 侧若用 HTTP 拉取，可以传 `None` 并改用
    `bridge.drain_events()` 或 `await bridge.handle(payload)` 的同步回包。
    """
    bridge = DesktopBridge(service, sink, enabled=enabled, **options)
    if not bridge.enabled:
        log_fallback('debug', '桌面桥未启用（%s != 1）', DESKTOP_BRIDGE_ENV)
        return None
    await bridge.start()
    return bridge
