"""service Chunk1 mixin —— `upstream/src/service.ts:1248-1928` 的逐条移植。

范围（成员声明起始行，顺序与上游一致）：

| 上游行 | 成员 | 主题 |
| --- | --- | --- |
| 1248 | `recentEntriesForPrompt` | 条数 × 近日时间窗**叠加**双取，按 id 去重合并（rc35 起无硬地板） |
| 1264 | `memories` | 长期记忆读取（参与者过滤 + 相关性排序） |
| 1277 | `adminFacts` | 管理视图：活跃长期事实 |
| 1284 | `adminPendingIntents` | 管理视图：待办意图 |
| 1291 | `adminStatePatches` | 管理视图：设定演化提案 |
| 1299 | `addAdminScriptNote` | 管理员注记条目 |
| 1312 | `addAdminFact` | 管理员高置信事实 |
| 1325 | `forgetAdminFact` | 事实软删（保留审计行） |
| 1332 | `cancelAdminIntent` | 意图取消 |
| 1339 | `rejectAdminStatePatch` | 提案驳回 |
| 1347 | `clearSettingOverlay` | 设定覆盖清理（串行入口） |
| 1355 | `rebaseTimeline` | 宿主时间线重置 |
| 1383 | `clearSettingOverlayUnlocked` | 覆盖 / 关系覆盖 / 提案 / 快照一并失效 |
| 1431 | `purgeAllStoryData` | 单剧本全量清除（软删墓碑兜底） |
| 1456 | `purgeAllData` | 全平台重置，只留一部 canonical 空剧本 |
| 1471 | `purgePlatformData` | 单平台（含 OneBot 别名族）清除 |
| 1486 | `clearDatabase` | 只清 HDSI 自有表（物理删除 + 逻辑清空兜底） |
| 1543 | `purgeStoryRange` | 按时间区间清除剧本与派生记忆 |
| 1605 | `receiveGroup` | **群聊入站主入口** |
| 1645 | `receive` | **私聊入站主入口** |
| 1720 | `groupSenderName` | 群成员显示名（账号规则 → 观察值 → 12h 缓存） |
| 1735 | `lookupGroupMemberName` | 群成员名查询（走 `transport.fetch_member_name`） |
| 1750 | `bufferGroupMessage` | 群回合缓冲（debounce 计时器 + revision 闸门） |
| 1769 | `flushGroupTurn` | 群回合刷出（意愿判定 → 冷却 → 主叙事 → 投递） |

界限说明（重要）
----------------
任务清单里提到的「群冷却 / 群表态执行 / 贴纸与原生表情解析与发送 /
`bufferUserNarrative` / `signalIncomingInterruption` / `deliverEarlyPrivateReply` /
`describeUserEvent`」**声明起始行都落在 1929-2572（Chunk2）**，
按移植约定「每个 mixin 文件只包含本行范围内的成员」，
它们不在本文件里；本文件通过 `self.xxx()` 跨 mixin 调用（Python MRO 解析），
`receive` / `receiveGroup` / `flushGroupTurn` 因此能照上游顺序调用它们。

模块级辅助函数
--------------
上游在 `service.ts` 模块作用域定义了 `samePlatformFamily` / `mentionsBot` /
`quotesBot` / `targetableMessageId` / `groupMessageRef` / `normalizeGroupDisplayName`
（`service.ts:7300-7710`），它们归 移植约定 的 Helpers 块
（`helpers.py`）。`helpers.py` 目前只导出了私有版 `_normalize_group_display_name`，
其余五个尚未落地，因此这里用 base.py 同款的「**优先 helpers、缺失时本文件等价实现**」
模式（`_prefer` 系列）；`helpers.py` 一旦补上同名导出，本文件的本地实现自动让位。

键名约定
-------------------------------
- 内部中转结构（缓冲回合、`accepted` / `result`）一律 snake_case；
- **发给模型的 payload**（`groupContext` 及其 messages）与**数据库列名 / metadata**
  保持上游 camelCase；
- 从模型输出、旧数据、跨 mixin 读入的 dict 一律 `pick` 双读。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Optional

from ..time import dt_ms, format_log_time, iso, parse_dt
from ..group_willingness import consume_willingness_gate, evaluate_willingness_gate
from ..script.commit_builder import find_group_script_event
from ..script.contract import message_event_reference
from ..script.delivery_ledger import platform_action_reference
from ..story_state import decode_story_state, encode_story_state
from ..types import empty_participant_state, empty_story_state
from .base import (
    GROUP_SKIP_NOTE_INTERVAL_MS,
    ServiceBase,
    _config_section,
    _config_value,
    is_one_bot_platform,
    normalize_account_id,
    normalize_group_id,
    pick,
)
from .helpers import (
    DEFAULT_HISTORICAL_IMAGE_LIMIT,
    _turn_get,
    clip,
    describe_group_attachments,
    describe_quoted_message,
    extract_session_audio_sources,
    extract_session_file_facts,
    extract_session_voice_count,
    format_group_speaker,
    group_image_refs_for_storage,
    mask_qq_ids,
    narrative_cursor,
    normalize_group_chat_actions,
    normalize_group_visible_reply,
    normalize_participant_state,
    visible_reply_text,
)
# 群音频的批次预算（上游 2197-2215）住在 chunk3：那里有 `audioConfig` 的解析与
# `load_native_audio`。chunk3 **不** import chunk1，所以这条模块级依赖不成环。
# `_extract_session_media` 同源：群聊入站的自动收藏要**与私聊同一份**结构化媒体表
# （`SessionView.media` → `[{source, kind, summary, label}]`），不能另外推一份（见 §45.6/§46）。
# `_extract_session_image_sources` 同源：群聊图片来源与私聊视觉走**同一处判据**（§46.7），
# 不另造一套"群聊专用图片解析"（1.0.1-rc31 的历史图片证据靠它）。
from .chunk3 import (
    _extract_session_image_sources, _extract_session_media, _image_budget,
    _load_group_batch_audio, _member, _unique, _vision_config,
    audio_turn_budget_note, audio_turn_slice, note_audio_budget_skip,
)
# 视频理解（v1.9.1 起接进群回合；v1.9.9 起**画面帧也进模型**）：群回合这一跳的接线
# （判据在 `video_understanding.collect_group_video_media`，它复用私聊那条链的
# `collect_video_sources`），本文件只负责"帧喂进群聊图片通道、音轨并进群音频批次"。
from ..video_understanding import (
    VideoMedia,
    audio_clip_seconds,
    collect_group_video_media,
    note_group_frames_without_channel,
    video_config,
    video_fact_note,
)
# 每回合图片预算（v1.9.4）：默认值 / 解析 / 截断线索只住在 `core/vision_budget.py`；
# 有效值的解析走 `chunk3._image_budget`（与私聊那条路**同一个**函数，判据只有一处）。
from ..vision_budget import image_budget_note, note_image_budget_skip

__all__ = ['ServiceChunk1', 'resolve_script_context_budget']

#: JS `Time.hour` / `Time.second`（Koishi `Time` 的单位毫秒）。
_HOUR_MS = 3_600_000
_SECOND_MS = 1_000

#: 侧端识图没给出观察结果、而这段视频已经抽出了帧时，那句**诚实**的视频事实的降级原因
#: （v1.9.9）。与 `video_understanding.GROUP_FRAMES_NO_CHANNEL_REASON` 同一条尺子
#: （抽了帧没人看不许静默，也不许把"没看到"写成"看到了"），但原因不同：那条是"没有
#: 原生视觉通道"，这条是"侧端识图这一跳没成"——两条不许混用同一个原因串（节流 warn 的
#: 键就是原因本身，混用等于把两种故障报成一种）。
_SIDECAR_FRAMES_WITHOUT_OBSERVATION = (
    '这是群里发来的一段视频，但侧端识图没有给出观察结果，抽到的画面帧没有交给模型'
)

#: 上游 `clearDatabase` 里按顺序清空的表（`src/service.ts:1493-1496`）。
_CLEAR_DATABASE_TABLES = (
    'interlude_script_entry', 'interlude_memory', 'interlude_intent',
    'interlude_scene', 'interlude_arc', 'interlude_fact', 'interlude_state_patch',
    'interlude_overlay_snapshot', 'interlude_web_observation', 'interlude_schedule_preplan',
    'interlude_participant', 'interlude_story',
    # 上游 1.0.1-rc23：世界播种事件表也随清库/清剧本一起清。
    'interlude_seeded_event',
)

_MISSING = object()


# =========================================================================== #
# 模块级辅助：优先 helpers.py 的移植版，缺失时本文件的等价实现
# =========================================================================== #

try:  # pragma: no cover - 取决于 helpers.py 的导出进度
    from .helpers import same_platform_family as _same_platform_family  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    def _same_platform_family(left: Any, right: Any) -> bool:
        """上游 `samePlatformFamily`（`src/service.ts:7676`）。

        OneBot 的传输别名（`onebot` / `onebot:x` / `napcat` / `qq:onebot`）在管理员
        视角下是同一个平台族；其余按小写全等比较。
        """
        if is_one_bot_platform(left) and is_one_bot_platform(right):
            return True
        return str(left if left is not None else '').strip().lower() == \
            str(right if right is not None else '').strip().lower()

try:  # pragma: no cover
    from .helpers import normalize_group_display_name as _normalize_group_display_name  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    try:
        # helpers.py 目前只有私有版（`helpers.py:724`，与上游逐字对应）。
        from .helpers import _normalize_group_display_name  # type: ignore[attr-defined]
    except ImportError:
        import re as _re

        _NEWLINE_RE = _re.compile(r'[\r\n]')

        def _normalize_group_display_name(*candidates: Any) -> str:
            """上游 `normalizeGroupDisplayName`（`src/service.ts:7570`）：首个非空候选，截断 80。"""
            for candidate in candidates:
                name = _NEWLINE_RE.sub(' ', str(candidate if candidate is not None else '')).strip()
                if name:
                    return name[:80]
            return ''

try:  # pragma: no cover
    from .helpers import targetable_message_id as _targetable_message_id  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    import re as _re2

    _INTEGER_ID_RE = _re2.compile(r'^-?\d+$')

    def _targetable_message_id(value: Any) -> Optional[str]:
        """上游 `targetableMessageId`（`src/service.ts:7300`）。

        只有十进制（可负）且非 `0` 的 id 才能作为平台动作目标；
        其余（含空串）返回 `None`（上游 `undefined`，Python 侧统一用 `None`）。
        """
        text = str(value if value is not None else '').strip()
        return text if _INTEGER_ID_RE.match(text) and text != '0' else None

try:  # pragma: no cover
    from .helpers import group_message_ref as _group_message_ref  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    def _group_message_ref(entry_id: Any) -> str:
        """上游 `groupMessageRef`（`src/service.ts:7305`）：`msg-<非负整数>`。"""
        try:
            value = int(entry_id)
        except (TypeError, ValueError):
            value = 0
        return 'msg-%d' % max(0, value)

try:  # pragma: no cover
    from .helpers import mentions_bot as _mentions_bot  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    def _mentions_bot(session: Any) -> bool:
        """上游 `mentionsBot`（`src/service.ts:7578`）。

        `SessionView`（适配层提供的等价 session）自带 `mentioned_bot()`，
        它按 AstrBot 的段模型判定 `at` 段；其它形状（dict / 原生 Koishi session）
        回落到上游的**内容匹配**实现：正文包含机器人账号 id 或 `<at ... id=...>`。
        """
        method = getattr(session, 'mentioned_bot', None)
        if callable(method):
            return bool(method())
        self_id = normalize_account_id(_session_read(session, 'selfId', 'self_id'))
        if not self_id:
            return False
        content = str(_session_read(session, 'content') or '')
        if self_id in content:
            return True
        import re as _re3
        return bool(_re3.search(r'<at[^>]+id=["\']?%s' % _re3.escape(self_id), content, _re3.IGNORECASE))

try:  # pragma: no cover
    from .helpers import quotes_bot as _quotes_bot  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    def _quotes_bot(session: Any) -> bool:
        """上游 `quotesBot`（`src/service.ts:7696`）：引用消息的作者就是机器人自己。"""
        method = getattr(session, 'quoted_bot', None)
        if callable(method):
            return bool(method())
        quote = _session_read(session, 'quote')
        user = pick(quote, 'user') if isinstance(quote, dict) else None
        quote_user = pick(user, 'id', 'user_id') if isinstance(user, dict) else None
        return str(quote_user if quote_user is not None else '') == \
            str(_session_read(session, 'selfId', 'self_id') or '')


def _session_read(session: Any, camel: str, snake: Optional[str] = None) -> Any:
    """读 Koishi `Session` 字段：同时支持 `SessionView`（snake_case 属性）与 dict。

    `plugin/adapters/astrbot_bridge.py` 提供的是 `plugin.core.service.session.SessionView`
    （移植约定），它的字段是 snake_case；测试与桌面桥可能直接给
    dict（camelCase 或 snake_case）。这里统一成一个读取口。
    """
    if session is None:
        return None
    if snake is None:
        # `selfId` → `self_id` 这类常规命名；`author` / `content` / `quote` 两写同形。
        import re as _re4
        snake = _re4.sub(r'(?<!^)(?=[A-Z])', '_', camel).lower()
    if isinstance(session, dict):
        return pick(session, camel, snake)
    value = getattr(session, snake, _MISSING)
    if value is not _MISSING:
        return value
    return getattr(session, camel, None)


def _spawn(awaitable: Any) -> None:
    """把后台协程挂成任务（等价 Chunk0 的 `_spawn` / 上游 `void this.xxx()`）。

    放在模块级是为了让本 mixin 能独立测试（Chunk0 未混入时 `self._spawn` 不存在），
    同时不新增上游没有的类成员。
    """
    if asyncio.iscoroutine(awaitable):
        asyncio.ensure_future(awaitable)


def _clear_database_fallback(table: str) -> dict[str, Any]:
    """上游 `clearDatabase` 里那串嵌套三元（`src/service.ts:1504-1521`）的静态分支。

    `interlude_story` / `interlude_participant` 两个分支需要实例方法
    （`initialStorySetting`）或 types 工厂，直接写在 `clear_database` 里。
    """
    if table == 'interlude_script_entry':
        return {'kind': 'redacted', 'actor': 'system', 'content': '[HDSI 数据库已清空]', 'metadata': {'redacted': True}}
    if table == 'interlude_memory':
        return {'status': 'deleted', 'content': '[HDSI 数据库已清空]'}
    if table == 'interlude_intent':
        return {'status': 'cancelled', 'summary': '[HDSI 数据库已清空]'}
    if table in ('interlude_scene', 'interlude_arc'):
        return {'status': 'closed', 'hook': '', 'summary': '', 'entryCount': 0, 'sceneCount': 0}
    if table == 'interlude_fact':
        return {'status': 'superseded', 'content': '[HDSI 数据库已清空]'}
    if table == 'interlude_web_observation':
        return {'status': 'deleted', 'url': '', 'title': '', 'excerpt': '', 'summary': '[HDSI 数据库已清空]'}
    if table == 'interlude_schedule_preplan':
        return {
            'regimes': [], 'exceptions': [], 'materializedDays': [],
            'validFrom': '1970-01-01', 'validThrough': '1970-01-01',
            'lastReviewedLocalDate': '', 'reviewReason': '[HDSI 数据库已清空]',
        }
    if table == 'interlude_seeded_event':
        return {
            'status': 'expired', 'summary': '[HDSI 数据库已清空]',
            'sourcePayload': {}, 'subjects': [],
        }
    return {'status': 'rejected', 'proposedValue': '[HDSI 数据库已清空]', 'evidence': ''}


def _config_limit(section: Any, camel: str, snake: str, default: Any) -> Any:
    """读一个可空配置项：缺失（`None`）时用上游 schema 的默认值。"""
    raw = _config_value(section, camel, snake, None)
    return default if raw is None else raw


def _message_characters(runtime: Any) -> int:
    """上游 `normalizeGroupVisibleReply(..., this.config.runtime.maxMessageCharacters, ...)`。

    配置缺失时上游是 `String.prototype.slice(0, undefined)` —— **不截断**，
    因此这里用 `2**31-1`（"不截断"）而不是某个会静默砍掉群回复的小默认值
    （与 Chunk2 的 `_message_characters` 同一处理）。
    """
    raw = _config_value(runtime, 'maxMessageCharacters', 'max_message_characters', None)
    if raw is None or isinstance(raw, bool):
        return 2 ** 31 - 1
    return int(raw)


def _group_context_messages(messages: Any) -> list[dict[str, Any]]:
    """把 `groupMessages()` 的输出归一成上游 `GroupMessageContext` 的 wire 形状（camelCase）。

    上游的群上下文对象逐字是 `{ senderId, senderName, speaker, messageRef,
    messageId, quote, content, occurredAt, direction }`（键名法：发给模型/适配层的
    payload 保持上游拼写）。Chunk2 的 `group_messages()` 内部按 snake_case 造这个结构
    （`sender_id` / `message_ref` / `occurred_at`），而 `helpers.normalize_group_chat_actions`
    与 `to_prompt_payload` 读的引用键是 camelCase —— 因此这里在**调用点**把它折回上游形状：
    `replyTo` / `messageReactions` 才不会被静默丢弃，prompt 里也和上游一字不差。
    读取用 `pick` 双读，所以两种拼写都能接受。
    """
    result: list[dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        item: dict[str, Any] = {
            'senderId': pick(message, 'senderId', 'sender_id'),
            'senderName': pick(message, 'senderName', 'sender_name'),
            'speaker': pick(message, 'speaker'),
            'content': pick(message, 'content'),
            'occurredAt': pick(message, 'occurredAt', 'occurred_at'),
            'direction': pick(message, 'direction'),
        }
        message_id = pick(message, 'messageId', 'message_id')
        if message_id:
            item['messageId'] = message_id
        message_ref = pick(message, 'messageRef', 'message_ref')
        if message_ref:
            item['messageRef'] = message_ref
        quote = pick(message, 'quote')
        if quote:
            item['quote'] = quote
        result.append(item)
    return result


def _chat_capabilities_wire(capabilities: Any) -> Any:
    """把能力声明折回上游 `ChatActionCapabilities` 的 wire 形状（camelCase）。

    同上：`group_chat_capabilities()` 产出 snake_case（`quote_reply` / `native_faces` /
    `expression_threshold`），而 `normalize_group_chat_actions` 用 camelCase 读
    `quoteReply`。这里补齐 camelCase 别名并移除 snake_case 别名，保证送给模型与
    适配层的对象与上游逐字一致（`resolve_native_face` / `narrator_prompts` 都是双读，不受影响）。
    """
    if not isinstance(capabilities, dict):
        return capabilities
    wire = dict(capabilities)
    for camel, snake in (
        ('quoteReply', 'quote_reply'),
        ('nativeFaces', 'native_faces'),
        ('expressionThreshold', 'expression_threshold'),
    ):
        value = pick(capabilities, camel, snake)
        if value is not None:
            wire[camel] = value
        wire.pop(snake, None)
    return wire


async def _group_video_media(
    service: Any, story: Any, session: Any, group_id: Any = '', offset: int = 0,
) -> tuple[list[Any], str, VideoMedia]:
    """群回合的视频：音轨并进群音频批次，帧留给调用方喂进**群聊图片通道**。

    判据**只有一处**：`video_understanding.collect_group_video_media`（它自己复用
    `collect_video_sources`），这里只做群回合特有的三件事——把音轨交给**同一条**
    `load_native_audio`（与群里语音、私聊语音完全同一条通道）、正文事实、以及
    "附加能力失败不许带崩回合、也绝不静默"的兜底（坑 25）。

    返回 `(音轨附件, 要并进正文的视频事实, 这次视频理解的产物)`。**第三项里的
    `image_sources` 是帧**：调用方读完帧字节（`load_native_images`）之前**不许**
    调 `cleanup()`——临时目录一删，帧就永远取不回来了。

    群里没有视频 / 总开关关着 / 群开关关着时：音轨与帧都是空的（**一个 ffmpeg 都不调**），
    但"群里发来一条视频却没人识别"会由第三项带着一句事实（`explain_skips`）。
    """
    empty = VideoMedia()
    try:
        media = await collect_group_video_media(service, story, session)
    except Exception as error:  # noqa: BLE001 - 视频理解绝不许带崩群回合
        service.report_standalone(
            'warn', '群聊视频理解失败，本回合按"没有视频"继续 群=%s 错误=%s', group_id, error,
        )
        return [], '', empty
    try:
        note = media.note
        if not media.audio_sources:
            return [], note, media
        # 音轨截断**可数**（v1.9.5）：多段视频的音轨走同一条语音通道、被同一个
        # 「每个事件音频数上限」切片；线索与 warn 与私聊那条路共用同一处实现。
        audio_to_take, audio_available, audio_granted = audio_turn_slice(
            service, media.audio_sources)
        audio_note = audio_turn_budget_note(audio_available, audio_granted)
        if audio_note:
            note = '\n'.join(part for part in (note, audio_note) if part)
            note_audio_budget_skip(service, session, audio_available, audio_granted)
        loaded = await service.load_native_audio(story, audio_to_take, session)
        # 与群语音批次同一套附件编号（`group-audio-N`），序列接在批次后面。
        return (
            [
                {**item, 'id': 'group-audio-%d' % (offset + index + 1)}
                for index, item in enumerate(loaded)
            ],
            note,
            media,
        )
    except Exception as error:  # noqa: BLE001 - 音轨取不到不该吞掉那条视频事实
        service.report_standalone(
            'warn', '群聊视频音轨读取失败，本回合只保留视频事实 群=%s 错误=%s', group_id, error,
        )
        return [], media.note, media


def _sidecar_frames_without_observation_note(service: Any, media: VideoMedia) -> str:
    """群聊抽出了帧、而侧端识图这一跳没给出观察结果：**重写**那句视频事实（v1.9.9）。

    与 `video_understanding.note_group_frames_without_channel()` 同一条尺子、同一个
    **造句子**（`video_fact_note`），只有两处不同：

    * 原因串是自己的（`_SIDECAR_FRAMES_WITHOUT_OBSERVATION`）——"侧端识图没成"与
      "没有原生的视觉通道"是两种故障，混用同一个原因串会让节流 warn 把两件事报成一件；
    * **不再打一条 warn**：这一跳的可见 warn 由 `describe_current_images(visible=True)`
      那一处发出（它知道真正的失败原因：没连接 / 报错或超时 / 没返回内容），
      两处都发等于同一次故障刷两条日志。

    为什么是"重写"而不是"再加一句"：`collect_video_sources` 那句会声称"抽了 N 帧画面"，
    在"帧没交给任何模型"时它就是假话（会让模型以为她看见了画面）——用同一个
    `video_fact_note` 造一句诚实的：帧数照报（可数），降级原因顶在最前面。
    """
    frames = len(media.image_sources)
    settings = video_config(service)
    return video_fact_note(
        degrade_reason='%s（抽到的 %d 帧没有交给模型）' % (
            _SIDECAR_FRAMES_WITHOUT_OBSERVATION, frames,
        ),
        has_audio=bool(media.audio_sources),
        # 音轨那半句由 `has_audio` 决定（有降级原因时 `audio_attempted` 不参与造句）。
        audio_seconds=audio_clip_seconds(settings),
        frame_mode=settings['frame_mode'],
        interval_seconds=settings['frame_interval_seconds'],
        average_frames=settings['frame_average_count'],
        timeout_seconds=settings['timeout_seconds'],
    )


# =========================================================================== #
# 剧本历史注入预算（上游 `src/service.ts:10465` 的模块级纯函数）
# =========================================================================== #

def _bounded_budget(raw: Any, default: int, low: int, high: int) -> int:
    """配置项 → `Math.floor` + 上下界钳制；取值不可解析时回落 `default`。

    上游是 `Math.max(low, Math.min(Math.floor(value ?? default), high))`。
    """
    try:
        number = float(raw)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(low, min(math.floor(number), high))


def resolve_script_context_budget(runtime: Any) -> tuple[int, int]:
    """上游 `resolveScriptContextBudget(runtime)`（`src/service.ts:10465`）逐条移植。

    rc35：**`Math.max(35, …)` 的硬地板已整条移除**——M4.1 为连续性加的这道地板，
    副作用是 Console 里把 `contextEntryLimit` 调到 35 以下完全无效（小模型没法收缩
    历史），连 schema 默认 35 都被抬成 50。现在尊重用户设置：默认 35 只保留在
    **默认值**里，不再参与判定。

    条数与时间窗是**叠加**关系：时间窗只**加**不**减**——窗口内的条目全部并入
    （即使条数已经取满），所以收缩上下文要两个值配合（`contextTimeWindowMinutes`
    设 0 即关掉窗口）。默认值取 schema 默认（35 条 / 45 分钟）。
    """
    count = _bounded_budget(
        _config_limit(runtime, 'contextEntryLimit', 'context_entry_limit', 35), 35, 1, 200,
    )
    minutes = _bounded_budget(
        _config_limit(runtime, 'contextTimeWindowMinutes', 'context_time_window_minutes', 45),
        45, 0, 1_440,
    )
    return count, minutes


# =========================================================================== #
# ServiceChunk1
# =========================================================================== #

class ServiceChunk1(ServiceBase):
    """对应 `upstream/src/service.ts` 第 1248–1928 行的成员。"""

    # ------------------------------------------------------------------ #
    # 记忆读取（`src/service.ts:1248-1274`）
    # ------------------------------------------------------------------ #

    async def recent_entries_for_prompt(self, story_id: str, now: Any) -> list[Any]:
        """上游 `recentEntriesForPrompt(storyId, now)`（`src/service.ts:1248`）。

        **条数 × 时间窗是叠加关系**：先按条数上限取最近 N 条，再并上时间窗内的全部条目
        （两者按 id 去重）；窗口只会加、不会减——窗口内条目数超过条数上限时，结果就是
        窗口内的全部条目。收缩上下文要两个值配合（窗口设 0 即关掉窗口）。
        预算判定在 `resolve_script_context_budget`（rc35 起**没有 35 条硬地板**）。

        移植说明：上游用 `occurredAt: { $gte }` 做范围查询，本移植版的
        `Database.all()` 只支持等值 where（`base.py:db_get` 对算子显式报错），
        因此改成「先按 occurredAt 倒序取 500 行，再在 Python 侧按 cutoff 过滤」——
        与上游取到的是同一批行（窗口内最新的至多 500 行）。
        """
        count, minutes = resolve_script_context_budget(self.runtime_config)
        moment = parse_dt(now) or self.now()
        cutoff_ms = dt_ms(moment) - int(minutes * 60_000)

        async def count_rows() -> list[Any]:
            return await self.db_get(
                'interlude_script_entry', {'storyId': story_id},
                {'limit': count, 'sort': {'occurredAt': 'DESC'}},
            )

        async def time_rows() -> list[Any]:
            if minutes <= 0:
                return []
            rows = await self.db_get(
                'interlude_script_entry', {'storyId': story_id},
                {'limit': 500, 'sort': {'occurredAt': 'DESC'}},
            )
            selected = []
            for row in rows:
                occurred = parse_dt(pick(row, 'occurredAt', 'occurred_at'))
                if occurred is not None and dt_ms(occurred) >= cutoff_ms:
                    selected.append(row)
            return selected

        count_result, time_result = await asyncio.gather(count_rows(), time_rows())
        by_id: dict[Any, Any] = {}
        for entry in [*count_result, *time_result]:
            by_id[pick(entry, 'id')] = entry
        # 上游：`left.occurredAt.getTime() - right.occurredAt.getTime() || left.id - right.id`。
        return sorted(
            by_id.values(),
            key=lambda entry: (
                dt_ms(parse_dt(pick(entry, 'occurredAt', 'occurred_at')) or moment),
                int(pick(entry, 'id') or 0),
            ),
        )

    async def memories(
        self,
        story_id: str,
        limit: Optional[int] = None,
        participant_id: Optional[str] = None,
    ) -> list[Any]:
        """上游 `memories(storyId, limit = config.runtime.memoryLimit, participantId?)`
        （`src/service.ts:1264`）。

        读取上界是 `max(1, min(limit * 4, 500))`（先多取再按关系过滤/截断），
        排序 `importance` 降序、`updatedAt` 降序，最后 `slice(0, limit)`。
        """
        if limit is None:
            limit = int(_config_limit(self.runtime_config, 'memoryLimit', 'memory_limit', 20))
        limit = int(limit)
        bounded = max(1, min(limit * 4, 500))
        rows = await self.db_get(
            'interlude_memory', {'storyId': story_id, 'status': 'active'},
            {'limit': bounded, 'sort': {'importance': 'DESC', 'updatedAt': 'DESC'}},
        )
        filtered = [
            row for row in rows
            if participant_id is None
            or not pick(row, 'participantId', 'participant_id')
            or pick(row, 'participantId', 'participant_id') == participant_id
        ]
        filtered.sort(
            key=lambda row: (
                float(pick(row, 'importance') or 0),
                dt_ms(parse_dt(pick(row, 'updatedAt', 'updated_at')) or self.now()),
            ),
            reverse=True,
        )
        return filtered[:limit]

    # ------------------------------------------------------------------ #
    # 管理视图（`src/service.ts:1277-1296`）
    # ------------------------------------------------------------------ #

    async def admin_facts(self, story_id: str, limit: int = 20) -> list[Any]:
        """上游 `adminFacts(storyId, limit = 20)`（`src/service.ts:1277`）：全局 + 关系专属事实。"""
        return await self.db_get(
            'interlude_fact', {'storyId': story_id, 'status': 'active'},
            {'limit': max(1, min(int(limit), 100)), 'sort': {'updatedAt': 'DESC'}},
        )

    async def admin_pending_intents(self, story_id: str, limit: int = 20) -> list[Any]:
        """上游 `adminPendingIntents(storyId, limit = 20)`（`src/service.ts:1284`）。"""
        return await self.db_get(
            'interlude_intent', {'storyId': story_id, 'status': 'pending'},
            {'limit': max(1, min(int(limit), 100)), 'sort': {'notBefore': 'ASC'}},
        )

    async def admin_state_patches(self, story_id: str, limit: int = 20) -> list[Any]:
        """上游 `adminStatePatches(storyId, limit = 20)`（`src/service.ts:1291`）。"""
        return await self.db_get(
            'interlude_state_patch', {'storyId': story_id},
            {'limit': max(1, min(int(limit), 100)), 'sort': {'createdAt': 'DESC'}},
        )

    # ------------------------------------------------------------------ #
    # 管理写入（`src/service.ts:1299-1345`）
    # ------------------------------------------------------------------ #

    async def add_admin_script_note(self, story: Any, content: Any) -> bool:
        """上游 `addAdminScriptNote(story, content)`（`src/service.ts:1299`）。

        追加一条审计可见的系统注记，不假装它来自模型。空文本直接返回 `False`。
        """
        story_id = pick(story, 'id')
        limit = int(_config_limit(self.runtime_config, 'maxScriptCharacters', 'max_script_characters', 8_000))
        text = clip(content, limit)
        if not text:
            return False
        now = self.now()
        await self.append_entry(story_id, {
            'kind': 'admin-note', 'actor': 'system', 'content': '[管理员注记] %s' % text,
            'occurredAt': iso(now), 'metadata': {'source': 'administrator'},
        }, now)
        self.schedule_compaction(story_id)
        return True

    async def add_admin_fact(self, story: Any, scope: str, content: Any) -> bool:
        """上游 `addAdminFact(story, scope, content)`（`src/service.ts:1312`）。

        为「必须活过压缩」的修正写入一条高置信事实（`importance=0.8`、`confidence=1`）。
        """
        story_id = pick(story, 'id')
        limit = int(_config_limit(self.memory_config, 'factContentCharacters', 'fact_content_characters', 4_000))
        text = clip(content, limit)
        if not text:
            return False
        now = self.now()
        await self.db_create('interlude_fact', {
            'storyId': story_id, 'participantId': '', 'scope': scope, 'content': text,
            'importance': 0.8, 'confidence': 1, 'unresolved': False,
            'embedding': await self.embed_text(text),
            'status': 'active', 'sourceEntryIds': [], 'lastSeenAt': now,
            'createdAt': now, 'updatedAt': now,
        })
        return True

    async def forget_admin_fact(self, story_id: str, fact_id: int) -> bool:
        """上游 `forgetAdminFact(storyId, id)`（`src/service.ts:1325`）。

        可逆删除：事实保留为 `superseded` 行以便审计。
        """
        rows = await self.db_get(
            'interlude_fact', {'id': fact_id, 'storyId': story_id, 'status': 'active'},
        )
        if not rows:
            return False
        await self.db_set('interlude_fact', {'id': fact_id}, {'status': 'superseded', 'updatedAt': self.now()})
        return True

    async def cancel_admin_intent(self, story_id: str, intent_id: int) -> bool:
        """上游 `cancelAdminIntent(storyId, id)`（`src/service.ts:1332`）。"""
        rows = await self.db_get(
            'interlude_intent', {'id': intent_id, 'storyId': story_id, 'status': 'pending'},
        )
        if not rows:
            return False
        await self.db_set('interlude_intent', {'id': intent_id}, {'status': 'cancelled', 'updatedAt': self.now()})
        return True

    async def reject_admin_state_patch(self, story_id: str, patch_id: int) -> bool:
        """上游 `rejectAdminStatePatch(storyId, id)`（`src/service.ts:1339`）。"""
        rows = await self.db_get(
            'interlude_state_patch', {'id': patch_id, 'storyId': story_id, 'status': 'proposed'},
        )
        if not rows:
            return False
        # 上游只改 status：拒绝不是一次内容变更，故不刷新 updatedAt（该表也没有这一列）。
        await self.db_set('interlude_state_patch', {'id': patch_id}, {'status': 'rejected'})
        return True

    # ------------------------------------------------------------------ #
    # 设定覆盖清理与时间线重置（`src/service.ts:1347-1429`）
    # ------------------------------------------------------------------ #

    async def clear_setting_overlay(self, story: Any, target: str) -> dict[str, Any]:
        """上游 `clearSettingOverlay(story, target)`（`src/service.ts:1347`）。

        只清演化的 overlay，保留 Canon、剧本与记忆；先让缓冲叙事失效，再按故事串行执行。
        """
        story_id = pick(story, 'id')
        self.invalidate_buffered_narratives(story_id)

        async def task() -> dict[str, Any]:
            current = await self.get_story(story_id)
            return await self.clear_setting_overlay_unlocked(current, target)

        return await self.serial(story_id, task)

    async def rebase_timeline(self, story: Any) -> dict[str, Any]:
        """上游 `rebaseTimeline(story)`（`src/service.ts:1355`）。

        在不删除历史归档的前提下开启一段干净的、宿主拥有的时间线：用于从
        「散文即权威」的旧版本升级后，清理可能已经混进 active scene / 草稿的未来污染。
        """
        story_id = pick(story, 'id')
        self.invalidate_buffered_narratives(story_id)

        async def task() -> dict[str, Any]:
            current = await self.get_story(story_id)
            if not current:
                raise RuntimeError('剧本不存在，无法重置时间线。')
            current_id = pick(current, 'id')
            now = self.now()
            latest = await self.db_get(
                'interlude_script_entry', {'storyId': current_id},
                {'limit': 1, 'sort': {'id': 'DESC'}},
            )
            active_scene = await self.active_scene(current_id)
            if active_scene:
                latest_id = pick(latest[0], 'id') if latest else None
                if latest_id is None:
                    latest_id = pick(active_scene, 'lastEntryId', 'last_entry_id')
                await self.db_set('interlude_scene', {'id': pick(active_scene, 'id')}, {
                    'hook': 'Host timeline rebased at %s.' % format_log_time(
                        now, pick(pick(current, 'setting'), 'timezone'),
                    ),
                    'summary': (
                        'The host resumed the current timeline here. Earlier script remains '
                        'archived context; no future statement from it is an event after this point.'
                    ),
                    'lastEntryId': latest_id,
                    'entryCount': 0,
                    'updatedAt': now,
                })
            state = dict(decode_story_state(pick(current, 'state')))
            # 上游 `{ ...state, workingDetails: [], timelineCarry: [],
            # continuitySnapshot: undefined, continuityDirty: true }`：
            # `undefined` ≡ 删除该键（Python 侧不用 `None` 顶替）。
            state.pop('continuity_snapshot', None)
            state.pop('continuitySnapshot', None)
            state['working_details'] = []
            state['timeline_carry'] = []
            state['continuity_dirty'] = True
            await self.db_set('interlude_story', {'id': current_id}, {
                'state': encode_story_state(state), 'cursorAt': now, 'updatedAt': now,
            })
            await self.append_entry(current_id, {
                'kind': 'timeline-rebase', 'actor': 'system',
                'content': (
                    'Host timeline rebased. Earlier narrative prose remains an archive '
                    'and no longer defines future events.'
                ),
                'occurredAt': iso(now), 'metadata': {'timelineRebase': True},
            }, now)
            return {'at': now, 'sceneReset': bool(active_scene)}

        return await self.serial(story_id, task)

    async def clear_setting_overlay_unlocked(self, story: Any, target: str) -> dict[str, Any]:
        """上游 `clearSettingOverlayUnlocked(story, target)`（`src/service.ts:1383`）。

        清理 overlay、关系覆盖，并把**活跃 overlay 行与待定候选**一并失效：
        提案本身保留作审计，否则清理前创建的候选日后仍会被应用，悄悄复活旧人格/旧关系。
        `target` 取值 `character` / `perspective` / `relationship` / `world` / `all`。
        """
        if not story:
            raise RuntimeError('剧本不存在，无法清理设定覆盖。')
        story_id = pick(story, 'id')
        now = self.now()
        state_value = pick(story, 'state')
        overlay = dict(pick(state_value, 'settingOverlay', 'setting_overlay') or {})
        if target in ('character', 'all'):
            overlay.pop('characterProfile', None)
            overlay.pop('character_profile', None)
            overlay['character_traits'] = []
            overlay.pop('characterTraits', None)
        if target in ('perspective', 'all'):
            overlay.pop('perspective', None)
        if target in ('relationship', 'all'):
            overlay.pop('relationship', None)
        if target in ('world', 'all'):
            overlay.pop('world', None)
        merged_state = dict(decode_story_state(state_value))
        merged_state['setting_overlay'] = overlay
        await self.db_set('interlude_story', {'id': story_id}, {
            'state': encode_story_state(merged_state), 'updatedAt': now,
        })

        participant_count = 0
        if target in ('relationship', 'all'):
            participants = await self.participants(story_id, True)
            for participant in participants:
                # helpers 的 normalize_participant_state 输出 camelCase（持久化 wire format）。
                state = normalize_participant_state(pick(participant, 'state'))
                if not pick(state, 'relationshipOverlay', 'relationship_overlay'):
                    continue
                participant_count += 1
                state.pop('relationshipOverlay', None)
                state.pop('relationship_overlay', None)
                await self.db_set('interlude_participant', {'id': pick(participant, 'id')}, {
                    'state': state, 'updatedAt': now,
                })

        patches = await self.db_get('interlude_state_patch', {'storyId': story_id})
        for patch in patches:
            if pick(patch, 'status') not in ('proposed', 'applied', 'compacted'):
                continue
            if target != 'all' and pick(patch, 'target') != target:
                continue
            await self.db_set('interlude_state_patch', {'id': pick(patch, 'id')}, {'status': 'cleared'})

        snapshots = await self.db_get(
            'interlude_overlay_snapshot', {'storyId': story_id, 'status': 'active'},
        )
        for snapshot in snapshots:
            if target != 'all' and pick(snapshot, 'target') != target:
                continue
            await self.db_set('interlude_overlay_snapshot', {'id': pick(snapshot, 'id')}, {
                'status': 'superseded', 'updatedAt': now,
            })
        return {'participantCount': participant_count}

    # ------------------------------------------------------------------ #
    # 清库与区间清除（`src/service.ts:1431-1601`）
    # ------------------------------------------------------------------ #

    async def purge_all_story_data(self, story_id: str) -> None:
        """上游 `purgeAllStoryData(storyId)`（`src/service.ts:1431`）。

        破坏性管理操作（调用方必须先校验确认短语）。全量清除后还会用**当前 Console
        配置**重建 Canon，旧档案因此不可能在后续 prompt 里复活。
        """
        self.invalidate_story_tasks(story_id)
        self.invalidate_buffered_narratives(story_id)
        await self.purge_table('interlude_script_entry', {'storyId': story_id}, {
            'kind': 'redacted', 'actor': 'system', 'content': '[管理员已删除剧本内容]',
            'metadata': {'redacted': True},
        })
        await self.purge_table('interlude_memory', {'storyId': story_id}, {
            'status': 'deleted', 'content': '[管理员已删除记忆]',
        })
        await self.purge_table('interlude_intent', {'storyId': story_id}, {
            'status': 'cancelled', 'summary': '[管理员已取消意图]',
        })
        await self.purge_table('interlude_scene', {'storyId': story_id}, {
            'status': 'closed', 'hook': '', 'summary': '', 'entryCount': 0,
        })
        await self.purge_table('interlude_arc', {'storyId': story_id}, {
            'status': 'closed', 'summary': '', 'sceneCount': 0,
        })
        await self.purge_table('interlude_fact', {'storyId': story_id}, {
            'status': 'superseded', 'content': '[管理员已删除事实]',
        })
        await self.purge_table('interlude_state_patch', {'storyId': story_id}, {
            'status': 'rejected', 'proposedValue': '[管理员已删除提案]', 'evidence': '',
        })
        await self.purge_table('interlude_overlay_snapshot', {'storyId': story_id}, {
            'status': 'superseded', 'summary': '[管理员已删除 overlay 归档]',
            'majorEvents': [], 'sourcePatchIds': [],
        })
        await self.purge_table('interlude_web_observation', {'storyId': story_id}, {
            'status': 'deleted', 'url': '', 'title': '', 'excerpt': '',
            'summary': '[管理员已删除网页观察]',
        })
        await self.purge_table('interlude_schedule_preplan', {'storyId': story_id}, {
            'regimes': [], 'exceptions': [], 'materializedDays': [],
            'validFrom': '1970-01-01', 'validThrough': '1970-01-01',
            'lastReviewedLocalDate': '', 'reviewReason': '[管理员已删除 Schedule Preplan]',
        })
        await self.purge_table('interlude_seeded_event', {'storyId': story_id}, {
            'status': 'expired', 'summary': '[管理员已删除世界事件]',
            'sourcePayload': {}, 'subjects': [],
        })
        now = self.now()
        story = await self.get_story(story_id)
        setting = self.initial_story_setting()
        await self.db_set('interlude_story', {'id': story_id}, {
            'setting': setting, 'state': empty_story_state(), 'cursorAt': now, 'updatedAt': now,
        })
        await self.reset_participant_canon(story_id, now)
        rebuilt = dict(story or {})
        rebuilt.update({'setting': setting, 'state': empty_story_state(), 'cursorAt': now})
        await self.ensure_continuity(rebuilt, now)

    async def purge_all_data(self, preferred_story_id: Optional[str] = None) -> Optional[str]:
        """上游 `purgeAllData(preferredStoryId?)`（`src/service.ts:1456`）。

        重置所有平台，最终只保留**恰好一部**空白的全局 canonical 剧本，并返回它的 id。
        """
        all_stories = await self.db_get('interlude_story', {}, {'sort': {'updatedAt': 'DESC'}})
        active = [story for story in all_stories if pick(story, 'status') == 'active']
        if not active:
            return None
        canonical = None
        if preferred_story_id:
            for story in active:
                if pick(story, 'id') == preferred_story_id:
                    canonical = story
                    break
        if canonical is None:
            canonical = active[0]
        canonical_id = pick(canonical, 'id')
        for story in all_stories:
            await self.purge_all_story_data(pick(story, 'id'))
        now = self.now()
        for story in all_stories:
            if pick(story, 'id') == canonical_id:
                continue
            await self.db_set('interlude_story', {'id': pick(story, 'id')}, {
                'status': 'archived', 'updatedAt': now,
            })
        return canonical_id

    async def purge_platform_data(self, platform: str) -> int:
        """上游 `purgePlatformData(platform)`（`src/service.ts:1471`）。

        只删某个适配器/平台族的记录，不碰其它平台；返回被处理的剧本数。
        """
        all_stories = await self.db_get('interlude_story', {}, {'sort': {'updatedAt': 'DESC'}})
        targets = [
            story for story in all_stories
            if _same_platform_family(pick(story, 'platform'), platform)
        ]
        for story in targets:
            await self.purge_all_story_data(pick(story, 'id'))
            await self.db_set('interlude_story', {'id': pick(story, 'id')}, {
                'status': 'archived', 'updatedAt': self.now(),
            })
        return len(targets)

    async def clear_database(self) -> dict[str, Any]:
        """上游 `clearDatabase()`（`src/service.ts:1486`）。

        只清 HDSI 自有的表：Koishi 的 users/channels 与其它插件刻意不动；在驱动仍打开的
        情况下从命令里删物理 SQLite 文件是不安全的。单次清空有重入闸门。
        """
        if self.database_resetting:
            raise RuntimeError('HDSI 数据库清空已经在进行中。')
        self.database_resetting = True
        # 上游 1.0.1-rc26：先让全局代际失效，再等在途回合（最多 30 秒/每个 key）。
        # 迟到的模型结果会因代际失配被丢弃，所以超时也照常继续清库。
        self.invalidate_story_tasks()
        await self.await_inflight_turns()
        self.invalidate_buffered_narratives()
        self.invalidate_history_vectors()
        try:
            removed = 0
            logically_cleared = 0
            for table in _CLEAR_DATABASE_TABLES:
                rows = await self.db_get(table, {})
                if not rows:
                    continue
                removed += len(rows)
                try:
                    await self.db_remove(table, {})
                except Exception as error:
                    # 保留既有的磁盘 I/O 兜底：内容被涂掉、剧本被归档，
                    # 于是被锁住的 sqlite 文件无法让旧故事复活。
                    self.report_standalone('warn', 'SQLite 清空表失败，改用逻辑清空 表=%s 错误=%s', table, error)
                    for row in rows:
                        key = (
                            {'storyId': pick(row, 'storyId', 'story_id')}
                            if table == 'interlude_schedule_preplan'
                            else {'id': pick(row, 'id')}
                        )
                        if table == 'interlude_story':
                            fallback = {
                                'status': 'archived', 'setting': self.initial_story_setting(),
                                'state': empty_story_state(),
                            }
                        elif table == 'interlude_participant':
                            fallback = {
                                'status': 'paused', 'profile': '', 'relationship': '',
                                'state': empty_participant_state(),
                            }
                        else:
                            fallback = _clear_database_fallback(table)
                        await self.db_set(table, key, fallback)
                        logically_cleared += 1
            return {'removed': removed, 'logicallyCleared': logically_cleared}
        finally:
            self.database_resetting = False

    async def await_inflight_turns(self, timeout_ms: int = 30_000, poll_ms: int = 500) -> int:
        """上游 `clearDatabase` 的 30 秒在途屏障。

        等待的是"某个回合的 `inFlightRequestId` 不再是当初那个"（模型回来了、或换成了新
        请求）；**每个 key 各自一个 deadline**，超时不报错、不中断——代际与
        `database_resetting` 已经保证了迟到结果被判废。返回仍在途的 key 数（排障用）。
        """
        turns = getattr(self, 'buffered_narrative_turns', None)
        if not isinstance(turns, dict):
            return 0
        pending: list[tuple[str, Any]] = [
            (key, _turn_get(turn, 'inFlightRequestId', 'in_flight_request_id'))
            for key, turn in list(turns.items())
            if isinstance(turn, dict)
            and _turn_get(turn, 'inFlightRequestId', 'in_flight_request_id') is not None
        ]
        for key, request_id in pending:
            self.report_operation(
                'standard', 'warn', None, 'advance',
                '清空前等待在途回合完成 参与者=%s 请求=%s', key, request_id,
            )
            deadline = self.now_ms() + timeout_ms
            while self.now_ms() < deadline:
                current = turns.get(key)
                if not isinstance(current, dict) or _turn_get(
                    current, 'inFlightRequestId', 'in_flight_request_id',
                ) != request_id:
                    break
                await asyncio.sleep(poll_ms / 1000)
        remaining = 0
        for key, request_id in pending:
            current = turns.get(key)
            if isinstance(current, dict) and _turn_get(
                current, 'inFlightRequestId', 'in_flight_request_id',
            ) == request_id:
                remaining += 1
        return remaining

    async def purge_story_range(self, story_id: str, from_value: Any, to_value: Any) -> None:
        """上游 `purgeStoryRange(storyId, from, to)`（`src/service.ts:1543`）。

        删除时间戳与区间重叠的剧本行与派生记忆记录（共同退化为软删墓碑）。
        """
        self.invalidate_story_tasks(story_id)
        start = parse_dt(from_value)
        end = parse_dt(to_value)
        self.invalidate_buffered_narratives(story_id)
        self.invalidate_history_vectors(story_id)

        def in_range(value: Any) -> bool:
            parsed = parse_dt(value)
            return parsed is not None and start is not None and end is not None and start <= parsed <= end

        entries = await self.db_get('interlude_script_entry', {'storyId': story_id})
        entry_ids = {
            pick(entry, 'id') for entry in entries
            if in_range(pick(entry, 'occurredAt', 'occurred_at'))
        }
        for entry in entries:
            if pick(entry, 'id') in entry_ids:
                await self.purge_table('interlude_script_entry', {'id': pick(entry, 'id')}, {
                    'kind': 'redacted', 'actor': 'system', 'content': '[管理员已删除剧本内容]',
                    'metadata': {'redacted': True},
                })

        memories = await self.db_get('interlude_memory', {'storyId': story_id})
        for memory in memories:
            source_entry_id = pick(memory, 'sourceEntryId', 'source_entry_id')
            sourced = source_entry_id is not None and source_entry_id in entry_ids
            if in_range(pick(memory, 'createdAt', 'created_at')) or sourced:
                await self.purge_table('interlude_memory', {'id': pick(memory, 'id')}, {
                    'status': 'deleted', 'content': '[管理员已删除记忆]',
                })

        facts = await self.db_get('interlude_fact', {'storyId': story_id})
        for fact in facts:
            source_ids = pick(fact, 'sourceEntryIds', 'source_entry_ids') or []
            sourced = any(item in entry_ids for item in source_ids)
            if (
                in_range(pick(fact, 'createdAt', 'created_at'))
                or in_range(pick(fact, 'updatedAt', 'updated_at'))
                or in_range(pick(fact, 'lastSeenAt', 'last_seen_at'))
                or sourced
            ):
                await self.purge_table('interlude_fact', {'id': pick(fact, 'id')}, {
                    'status': 'superseded', 'content': '[管理员已删除事实]',
                })

        intents = await self.db_get('interlude_intent', {'storyId': story_id})
        for intent in intents:
            if (
                in_range(pick(intent, 'createdAt', 'created_at'))
                or in_range(pick(intent, 'notBefore', 'not_before'))
                or in_range(pick(intent, 'updatedAt', 'updated_at'))
            ):
                await self.purge_table('interlude_intent', {'id': pick(intent, 'id')}, {
                    'status': 'cancelled', 'summary': '[管理员已取消意图]',
                })

        start_ms = dt_ms(start) if start is not None else 0
        end_ms = dt_ms(end) if end is not None else 0
        scenes = await self.db_get('interlude_scene', {'storyId': story_id})
        for scene in scenes:
            # 上游：`scene.startedAt <= to && (!scene.endedAt || scene.endedAt >= from)`。
            started = parse_dt(pick(scene, 'startedAt', 'started_at'))
            ended = parse_dt(pick(scene, 'endedAt', 'ended_at'))
            overlaps = (dt_ms(started) if started is not None else 0) <= end_ms \
                and (ended is None or dt_ms(ended) >= start_ms)
            if overlaps:
                await self.purge_table('interlude_scene', {'id': pick(scene, 'id')}, {
                    'status': 'closed', 'hook': '', 'summary': '', 'entryCount': 0,
                })

        arcs = await self.db_get('interlude_arc', {'storyId': story_id})
        for arc in arcs:
            if in_range(pick(arc, 'createdAt', 'created_at')) or in_range(pick(arc, 'updatedAt', 'updated_at')):
                await self.purge_table('interlude_arc', {'id': pick(arc, 'id')}, {
                    'status': 'closed', 'summary': '', 'sceneCount': 0,
                })

        patches = await self.db_get('interlude_state_patch', {'storyId': story_id})
        for patch in patches:
            if in_range(pick(patch, 'createdAt', 'created_at')) or in_range(pick(patch, 'appliedAt', 'applied_at')):
                await self.purge_table('interlude_state_patch', {'id': pick(patch, 'id')}, {
                    'status': 'rejected', 'proposedValue': '[管理员已删除提案]', 'evidence': '',
                })

        observations = await self.db_get('interlude_web_observation', {'storyId': story_id})
        for observation in observations:
            if in_range(pick(observation, 'createdAt', 'created_at')) \
                    or in_range(pick(observation, 'accessedAt', 'accessed_at')):
                await self.purge_table('interlude_web_observation', {'id': pick(observation, 'id')}, {
                    'status': 'deleted', 'url': '', 'title': '', 'excerpt': '',
                    'summary': '[管理员已删除网页观察]',
                })

        # 上游 1.0.1-rc23：区间清剧本时，落在区间内的世界事件一并作废（事件表按 occursAt 计时）。
        for row in await self.db_get('interlude_seeded_event', {'storyId': story_id}):
            if in_range(pick(row, 'occursAt', 'occurs_at')):
                await self.purge_table('interlude_seeded_event', {'id': pick(row, 'id')}, {
                    'status': 'expired', 'summary': '[管理员已删除世界事件]',
                    'sourcePayload': {}, 'subjects': [],
                })

        if entry_ids:
            await self.db_set('interlude_schedule_preplan', {'storyId': story_id}, {
                'lastReviewedLocalDate': '', 'validThrough': '1970-01-01',
                'reviewReason': 'Source range was purged; Schedule Preplan requires review.',
                'updatedAt': self.now(),
            })

        story = await self.get_story(story_id)
        await self.ensure_continuity(story, self.now())

    # ------------------------------------------------------------------ #
    # 入站主入口（`src/service.ts:1603-1718`）
    # ------------------------------------------------------------------ #

    async def receive_group(self, session: Any, received_at: Any = None) -> bool:
        """上游 `receiveGroup(session, receivedAt = new Date())`（`src/service.ts:1605`）。

        已配置的 OneBot 群聊入口：群成员**不需要**私聊授权，访问由群白名单控制。
        `mention-only` 模式下未被 @ 的消息直接丢弃。消息先按故事串行落库，
        再进入群回合缓冲（debounce 后由 `flush_group_turn` 处理）。
        """
        if self.database_resetting:
            return False
        allowed, reason = self.explain_group_access(session)
        if not allowed:
            # 群聊进不来时必须**看得见**：以前这里直接 return，日志里一个字都没有
            # （用户 2026-09-26 的日志里群里 @ 了机器人、Kela 也说了话，HDSI 全程沉默，
            # 看起来就是"群聊功能完全没生效"）。节流 10 分钟，避免群消息刷屏。
            self.note_group_skip(session, reason)
            return False
        group_id = self._session_group_id(session)
        # 名单里的群用那条规则；名单外的群（`group_chats_only` 关闭时才会走到这里）
        # 用默认群规则——与 schema 里群规则的默认值一致，不 @ 就不说话。
        rule = self.group_rule_or_default(group_id)
        mentioned_bot = _mentions_bot(session)
        quoted_bot = _quotes_bot(session)
        # v1.7.6：群里的语音也是**音频证据**（上游 1900）。语音消息没法带 @，所以
        # `mention-only` 下它也算"叫了她"——这条与私聊那条 `session.content?.trim()`
        # 的例外同源。没开语音理解时它仍然只留一条占位事实（见 `load_group_batch_audio`）。
        audio_sources = extract_session_audio_sources(session)
        # 1.0.1-rc31：群聊图片是**可被后续叙事读取的视觉证据**，不是隐式指令。
        # 有了它，`mention-only` 下"发给别人看的图"也能落库，供下一次真实回合回流。
        image_sources = _extract_session_image_sources(session)
        if (
            pick(rule, 'responseMode', 'response_mode') == 'mention-only'
            and not mentioned_bot and not audio_sources and not image_sources
        ):
            self.note_group_skip(
                session,
                '这条群消息没 @ 机器人，而该群的 response_mode=mention-only（引用机器人不算）',
            )
            return False
        story = await self.find_story(session)
        if not story and bool(_config_value(self.runtime_config, 'autoCreate', 'auto_create', False)):
            story = await self.create_story(session)
        if not story:
            self.note_group_skip(
                session, '找不到这部剧本，且 runtime.auto_create 关着（群聊不能自己建剧本）',
            )
            return False
        if pick(story, 'status') != 'active':
            self.note_group_skip(
                session, '剧本状态=%s（不是 active）' % (pick(story, 'status') or '?'),
            )
            return False
        now = parse_dt(received_at) or self.now()
        story_id = pick(story, 'id')
        sender_id = normalize_account_id(_session_read(session, 'userId', 'user_id'))
        sender_name = await self.group_sender_name(group_id, sender_id, session)
        character = pick(pick(story, 'setting'), 'character') or {}
        quote = describe_quoted_message(session, pick(character, 'name') or '主角')
        # 自动收藏入站表情包（群聊覆盖，受控偏离 §45.6）：种类必须在**文本化之前**拿到。
        # `describe_group_attachments` 会把 `<img kind=…>` 翻成 `[表情包]` / `[动画表情]`
        # 文本，从那种文本反推种类就是"两种拼写、两处判据"的老病（坑 46）。
        # 这里用的是与私聊**同一条** `extract_session_media`：同一份**结构化媒体表**
        # （适配层从观测到的 OneBot 原始段写下的 `SessionView.media`，§46）、同一个
        # `kind`，判据仍然只有 `helpers.collectible_sticker_kind()` 一处 ——
        # **不读正文里的任何 `<img>` 文本**（用户手打的标签不算数）。
        group_media = _extract_session_media(session)
        message_content = describe_group_attachments(_session_read(session, 'content'))

        async def task() -> Any:
            current = await self.get_story(story_id)
            if not current:
                raise RuntimeError('剧本不存在，无法记录群消息。')
            metadata: dict[str, Any] = {
                'groupId': group_id, 'senderId': sender_id, 'senderName': sender_name,
                'channelId': _session_read(session, 'channelId', 'channel_id'),
                'messageId': _session_read(session, 'messageId', 'message_id'),
            }
            if quote:
                metadata['quote'] = quote
            if image_sources:
                # 1.0.1-rc31：图片证据**落库**（不再只有正文里的 `[图片]` 占位符）。
                # 只存可重新获取的引用（URL / OneBot file），data URI 永不入库。
                # 形状逐字 = 上游 `{imageCount, groupImageRefs:[{source,ordinal,sourceType}]}`。
                metadata['imageCount'] = len(image_sources)
                metadata['groupImageRefs'] = group_image_refs_for_storage(image_sources)
            return await self.append_entry(story_id, {
                'kind': 'group-message', 'actor': 'user', 'content': message_content,
                'occurredAt': iso(now), 'metadata': metadata,
            }, now)

        accepted = await self.serial(story_id, task)
        # `mention-only` 下没 @ 机器人的图片：它现在已经**落库**（上面那条 entry），
        # 但绝不能自己触发主叙事 —— 不进 debounce 队列、不消耗意愿、不查冷却、
        # 不调模型（上游 `receiveGroup` 的那句 `return true`）。真正的回流发生在
        # 下一次真实进入群聊主叙事的回合（`load_historical_group_images`）。
        if (
            pick(rule, 'responseMode', 'response_mode') == 'mention-only'
            and not mentioned_bot and not audio_sources
        ):
            self.report_operation(
                'diagnostic', 'debug', story, 'user-message',
                '群图片已作为历史证据落库（未 @ 机器人，不触发主叙事）群=%s 张数=%d',
                group_id, len(image_sources),
            )
            return True
        message_id = _targetable_message_id(_session_read(session, 'messageId', 'message_id'))
        message: dict[str, Any] = {
            'senderId': sender_id, 'senderName': sender_name,
            'speaker': format_group_speaker(sender_name, sender_id),
        }
        if message_id:
            message['messageId'] = message_id
            message['messageRef'] = _group_message_ref(pick(accepted, 'id'))
        if quote:
            message['quote'] = quote
        message['content'] = message_content
        message['occurredAt'] = now
        message['direction'] = 'user'
        self.buffer_group_message(
            story, rule, session, message, mentioned_bot, quoted_bot, audio_sources,
            image_sources,
        )
        # 群聊收藏钩子挂在**真正的入站处理点**（所有闸门之后）：白名单外的群、
        # 关掉的群、`mention-only` 下没 @ 的消息、找不到 / 非 active 的剧本，
        # 上面都已经 return 了 —— 那些消息既不进叙事，也不该顺手收藏别人的图。
        self._spawn_group_sticker_collect(group_media)
        # 上游 `service.ts:2076` 逐字：summary 级关不掉，所以**只在这一行**把 QQ 号脱敏
        # （`maskQqIds`）。入站之外的用途（出站 UMO / 名单 / 主键 / 发给模型的 payload）
        # 一律保持原文，脱敏不许外溢。
        self.report_operation(
            'summary', 'info', story, 'user-message',
            '收到群聊消息 群=%s 发送者=%s', mask_qq_ids(group_id), mask_qq_ids(sender_id),
        )
        return True

    async def receive(self, session: Any, received_at: Any = None) -> bool:
        """上游 `receive(session, receivedAt = new Date())`（`src/service.ts:1645`）。

        **私聊入站主入口**。权限在 find/create **之前**判定，因此未授权的 QQ 仅靠发一条
        私聊既不能触发模型，也无法创建持久剧本。

        时间在等待故事队列**之前**同步标记（`signal_incoming_interruption`），
        这样刚到达的消息可以让「即将落库」的模型请求失效，也能让到期的拆分投递在
        真正发送前停住。
        """
        if self.database_resetting:
            return False
        if not self.can_handle_session(session):
            return False
        story = await self.find_story(session)
        if not story and bool(_config_value(self.runtime_config, 'autoCreate', 'auto_create', False)):
            story = await self.create_story(session)
        if not story or pick(story, 'status') != 'active':
            self.report_standalone_operation(
                'diagnostic', 'debug',
                '私聊未处理：故事不存在或已暂停 平台=%s 机器人ID=%s 用户ID=%s',
                _session_read(session, 'platform'), _session_read(session, 'selfId', 'self_id'),
                _session_read(session, 'userId', 'user_id'),
            )
            return False
        story_id = pick(story, 'id')
        now = parse_dt(received_at) or self.now()
        participant = await self.find_participant(session, story)
        if participant:
            # 白名单行可能在这个 QQ 首次加入共享剧本之后被编辑过：
            # 组装模型上下文之前先刷新当前关系分支（无变化时 ensureParticipant 不写库）。
            participant = await self.ensure_participant(story, session, now, participant)
        elif bool(_config_value(self.runtime_config, 'autoCreate', 'auto_create', False)) \
                or bool(pick(self.shared_story_config, 'autoEnrollParticipants', 'auto_enroll_participants')):
            participant = await self.ensure_participant(story, session)
        if not participant or pick(participant, 'status') != 'active':
            self.report_operation(
                'diagnostic', 'debug', story, 'user-message',
                '私聊未处理：参与者不存在或已暂停 用户ID=%s', _session_read(session, 'userId', 'user_id'),
            )
            return False
        content = _session_read(session, 'content')
        if not str(content or '').strip() and not extract_session_voice_count(session):
            return False
        observed = self.describe_vision_event(session)
        if (
            not str(pick(observed, 'content') or '').strip()
            and not (pick(observed, 'sources') or [])
            and not extract_session_voice_count(session)
            and not extract_session_file_facts(session)
        ):
            return False
        self.signal_incoming_interruption(story, participant)
        user_input = self.describe_user_event(story, session)
        self.report_operation(
            'summary', 'info', story, 'user-message',
            '收到参与者私聊消息 参与者=%s', mask_qq_ids(pick(participant, 'id')),
        )
        logging_config = _config_section(self.config, 'logging')
        if pick(logging_config, 'logMessageContent', 'log_message_content'):
            preview_length = int(_config_limit(logging_config, 'previewLength', 'preview_length', 500))
            self.report_operation(
                'diagnostic', 'info', story, 'user-message', '用户消息内容：%s',
                str(pick(user_input, 'content') or '')[:preview_length],
            )

        async def task() -> Optional[dict[str, Any]]:
            current = await self.get_story(story_id)
            current_participant = await self.get_participant(pick(participant, 'id'))
            if not current_participant or pick(current_participant, 'status') != 'active':
                return None
            incoming_participant = await self.record_incoming_message(current_participant, now)
            superseded = await self.cancel_pending_outgoing_messages(
                pick(current, 'id'),
                pick(incoming_participant, 'id'),
                now,
                bool(_config_value(
                    self.runtime_config,
                    'cancelDelayedRepliesOnUserMessage', 'cancel_delayed_replies_on_user_message', False,
                )),
            )
            user_content = pick(user_input, 'content')
            images = pick(user_input, 'sources') or []
            audio = pick(user_input, 'audioSources', 'audio_sources') or []
            quote = pick(user_input, 'quote')
            # 通道上下文在下面的条目 metadata 里解析；buffer 调用在更外层也要用它，
            # 因此先绑定到 None（`receive` 的分支很多，别让名字只在某个分支里存在）。
            channel_metadata: Any = None
            metadata: dict[str, Any] = {
                'platform': _session_read(session, 'platform'),
                'messageId': _session_read(session, 'messageId', 'message_id'),
                'personId': pick(incoming_participant, 'personId', 'person_id'),
            }
            # 上游 1.0.1-rc28（M4 §十）：给条目打上通道上下文（注册表命中才打）。
            # 它是 `lastEntryChannel` 的来源——没有它，私↔群与同人异端两条标注规则
            # 在真实运行里永远比较不到真正的"前一条"。单平台/未迁移时返回 None。
            resolve_channel = getattr(self, 'channel_metadata_for', None)
            if callable(resolve_channel):
                channel_metadata = await resolve_channel({
                    'platform': _session_read(session, 'platform'),
                    'selfId': _session_read(session, 'selfId', 'self_id'),
                    'userId': _session_read(session, 'userId', 'user_id'),
                })
                if channel_metadata:
                    metadata['channel_context'] = channel_metadata
            if images:
                metadata['imageCount'] = len(images)
            if audio:
                metadata['audioCount'] = len(audio)
            if quote:
                metadata['quote'] = quote
            await self.append_entry(pick(current, 'id'), {
                'kind': 'user-message', 'actor': 'user', 'content': user_content,
                'occurredAt': iso(now), 'metadata': metadata,
            }, now, pick(incoming_participant, 'id'))
            # 消息在到达时即持久化；模型请求本身在下面被 debounce，
            # 因此一串消息可以合成一个连贯的写作回合。
            await self.pause_automatic_advance_after_user_message(pick(current, 'id'), now)
            return {
                'story': current, 'participant': incoming_participant,
                'now': now, 'superseded': superseded,
            }

        accepted = await self.serial(story_id, task)
        if not accepted:
            return False
        # 上游 1.0.1-rc28：逐条消息记住入站端点（M4 规则 1/2 的唯一依据）。
        # 这里单独解析一次通道上下文——它必须在 buffer 调用点可见（条目 metadata
        # 那份在另一个分支里解析），注册表未命中时返回 None → 不记端点。
        resolve_channel = getattr(self, 'channel_metadata_for', None)
        buffer_channel: Any = None
        # 入站触达端点状态：连接在线 + 可投递（v3 §四；注册表未命中时是 no-op）。
        touch_inbound = getattr(self, 'touch_endpoint_state_inbound', None)
        if callable(touch_inbound):
            await touch_inbound({
                'platform': _session_read(session, 'platform'),
                'selfId': _session_read(session, 'selfId', 'self_id'),
                'userId': _session_read(session, 'userId', 'user_id'),
            })
        if callable(resolve_channel):
            buffer_channel = await resolve_channel({
                'platform': _session_read(session, 'platform'),
                'selfId': _session_read(session, 'selfId', 'self_id'),
                'userId': _session_read(session, 'userId', 'user_id'),
            })
        self.buffer_user_narrative(
            accepted['story'], accepted['participant'], session, accepted['now'],
            accepted['superseded'], pick(user_input, 'content'),
            pick(user_input, 'sources') or [],
            pick(user_input, 'audioSources', 'audio_sources') or [],
            pick(user_input, 'quote'),
            pick(user_input, 'media') or [],
            pick(buffer_channel or {}, 'endpoint_id', 'endpointId') or '',
        )
        images = pick(user_input, 'sources') or []
        audio = pick(user_input, 'audioSources', 'audio_sources') or []
        if images:
            vision_config = _config_section(_config_section(self.config, 'model'), 'vision')
            self.report_operation(
                'standard', 'info', accepted['story'], 'user-message',
                '当前事件包含图片附件 数量=%d 原生识图=%s', len(images),
                '开启' if pick(vision_config, 'enabled') else '关闭',
            )
        if audio:
            self.report_operation(
                'standard', 'info', accepted['story'], 'user-message',
                '当前事件包含语音附件 数量=%d 原生音频=%s', len(audio),
                '开启' if pick(self.audio_config, 'enabled') else '关闭',
            )
        self.report_operation(
            'standard', 'info', accepted['story'], 'user-message',
            '用户回合已入队 参与者=%s 已取消旧计划=%d',
            pick(accepted['participant'], 'id'), len(accepted['superseded'] or []),
        )
        return True

    # ------------------------------------------------------------------ #
    # 群成员名缓存（`src/service.ts:1720-1748`）
    # ------------------------------------------------------------------ #

    async def group_sender_name(self, group_id: str, user_id: str, session: Any) -> str:
        """上游 `groupSenderName(groupId, userId, session)`（`src/service.ts:1720`）。

        顺序：账号规则 label → 会话观察到的昵称 → 12 小时缓存 → 平台查询 → 回落 userId。
        同一 key 的并发查询共用一个 in-flight 任务（等价上游 `Map<string, Promise>`）。
        """
        account = self.user_account_rule(user_id)
        author = _session_read(session, 'author')
        author = author if isinstance(author, dict) else {}
        observed = _normalize_group_display_name(
            pick(account, 'label'), pick(author, 'nick'),
            _session_read(session, 'username'), pick(author, 'name'), pick(author, 'username'),
        )
        if observed:
            return observed

        key = '%s:%s' % (normalize_group_id(group_id), user_id)
        cached = self.group_member_name_cache.get(key)
        if cached and float(pick(cached, 'expiresAt', 'expires_at') or 0) > self.now_ms():
            return pick(cached, 'name') or ''

        pending = self.group_member_name_lookups.get(key)
        if pending is None:
            # 用 Task 而不是裸协程：上游那个 Promise 可以被多个并发调用者 await。
            pending = asyncio.ensure_future(self.lookup_group_member_name(
                key, group_id, user_id, _session_read(session, 'selfId', 'self_id'),
            ))
        self.group_member_name_lookups[key] = pending
        try:
            name = await pending
            return name or user_id
        finally:
            self.group_member_name_lookups.pop(key, None)

    async def lookup_group_member_name(self, cache_key: str, group_id: str, user_id: str, self_id: str) -> str:
        """上游 `lookupGroupMemberName(cacheKey, groupId, userId, selfId)`（`src/service.ts:1735`）。

        上游在这里直接找 OneBot 机器人并调 `bot.getGuildMember`；本移植版按移植约定 把平台调用收敛到 `transport.fetch_member_name`
        （「本平台到底有没有群成员查询能力」由适配器回答：不支持时返回 `''`）。
        查询成功则写入 12 小时缓存。
        """
        fetch = getattr(self.transport, 'fetch_member_name', None)
        if not callable(fetch):
            return ''
        try:
            name = _normalize_group_display_name(
                await fetch(normalize_group_id(group_id), user_id),
            )
        except Exception:
            return ''
        if not name:
            return ''
        self.group_member_name_cache[cache_key] = {
            'name': name, 'expires_at': self.now_ms() + 12 * _HOUR_MS,
        }
        return name

    # ------------------------------------------------------------------ #
    # 群回合缓冲与刷出（`src/service.ts:1750-1927`）
    # ------------------------------------------------------------------ #

    def buffer_group_message(
        self,
        story: Any,
        rule: Any,
        session: Any,
        message: dict[str, Any],
        mentioned_bot: bool,
        quoted_bot: bool,
        audio_sources: Any = None,
        image_sources: Any = None,
    ) -> None:
        """上游 `bufferGroupMessage(story, rule, session, message, mentionedBot, quotedBot)`
        （`src/service.ts:1750`）。

        按 `故事:群` 聚合一批消息，用 `debounceSeconds` 计时器 + 单调递增的 `revision`
        保证只有最后一次安排会真正刷出；@ 与引用机器人的事实是**累积**的。

        v1.7.6：带上这条消息的语音来源（上游 2099 的 `audioSources` + `audioSession`）
        ——群音频的**批次预算**靠它逐条算（见 `load_group_batch_audio`）。

        1.0.1-rc31：同时带上这条消息的图片来源（上游 `imageSources` + `imageSession`）。
        它只活在这个**内存批次**里（"Transport sources belong only to this fresh batch,
        never to durable history"）：当前回合的图是 `images`，落库的历史证据是
        `metadata.groupImageRefs`，两者不是同一份东西。
        """
        story_id = pick(story, 'id')
        group_id = normalize_group_id(pick(rule, 'groupId', 'group_id'))
        key = '%s:%s' % (story_id, group_id)
        existing = self.buffered_group_turns.get(key)
        turn = existing if existing is not None else {
            'story_id': story_id, 'group_id': group_id, 'rule': rule,
            'channel_id': _session_read(session, 'channelId', 'channel_id'),
            'messages': [], 'revision': 0, 'mentioned_bot': False, 'quoted_bot': False,
        }
        if turn.get('timer'):
            turn['timer']()
        turn['channel_id'] = _session_read(session, 'channelId', 'channel_id')
        turn['latest_session'] = session
        sources = [str(item) for item in (audio_sources or []) if str(item or '').strip()]
        if sources:
            # 上游同一形状：有语音才写这两个键，没有就**不出现**（别留空数组）。
            message = {**message, 'audioSources': sources, 'audioSession': session}
        images = [str(item) for item in (image_sources or []) if str(item or '').strip()]
        if images:
            # 上游同一形状：有图才写这两个键。
            message = {**message, 'imageSources': images, 'imageSession': session}
        turn.setdefault('messages', []).append(message)
        turn['mentioned_bot'] = bool(turn.get('mentioned_bot')) or bool(mentioned_bot)
        turn['quoted_bot'] = bool(turn.get('quoted_bot')) or bool(quoted_bot)
        revision = int(turn.get('revision') or 0) + 1
        turn['revision'] = revision
        debounce = _config_limit(rule, 'debounceSeconds', 'debounce_seconds', 1)
        delay = max(0.0, float(debounce if debounce is not None else 1)) * _SECOND_MS
        turn['timer'] = self.ctx.set_timeout(
            lambda: _spawn(self.flush_group_turn(key, revision)), delay,
        )
        self.buffered_group_turns[key] = turn

    async def load_historical_group_images(
        self,
        story: Any,
        refs: Any,
        limit: Any,
        session: Any = None,
        current_sources: Any = None,
    ) -> list[dict[str, Any]]:
        """上游 `loadHistoricalGroupImages(story, refs, limit, session?, currentSources)`
        （`src/service.ts:2498`）。

        从**同一群**的历史条目里按新到旧取最近 `limit` 张图，作为视觉证据附在**下一次**
        真实进入群聊主叙事的回合里：

        * 原生视觉（`model.vision.enabled`）关着 / `limit <= 0` / 没有引用 → 一张都不取
          （`historicalImageLimit = 0` 就是"关闭回流"，不是"取 0 张再往下走"）；
        * 与**当前回合**的图片按来源去重（`current_sources`）——同一张图不会既算当前图
          又算历史图；重复引用也只取一份；
        * 返回**由旧到新**（上游 `selected.reverse()`）：模型从较早的图读到较新的；
        * 单张取不回（端点不可用 / 图过期 / `getImage` 失败）只记一条**可行动**的 warn
          并跳过，**绝不阻塞文字主叙事**；
        * `id` 前缀 `group-history-image-N`，与当前回合的 `turn-image-N` 不重名。

        受控偏离（见 `docs/PORTING_NOTES.md` §78）：上游这里是
        `Math.min(6, Math.max(0, Math.floor(limit)))` —— 本项目 v1.9.7 起不再替用户拍上界，
        配多少读多少；默认 3 仍是最省成本那一侧，"0 = 关闭"逐字照上游。
        """
        if not bool(_config_value(_vision_config(self), 'enabled', False)):
            return []
        try:
            bound = int(float(limit))
        except (TypeError, ValueError):
            bound = DEFAULT_HISTORICAL_IMAGE_LIMIT
        if bound <= 0 or not isinstance(refs, list) or not refs:
            return []
        selected: list[dict[str, Any]] = []
        seen = {str(source) for source in (current_sources or [])}
        for ref in refs:
            if len(selected) >= bound:
                break
            source = str(pick(ref, 'source') or '').strip()
            if not source or source in seen:
                continue
            seen.add(source)
            try:
                image = await self.fetch_native_image(source, _member(session, 'bot'))
            except Exception as error:  # noqa: BLE001 - 附加证据取不回，不许带崩回合
                self.report(
                    'warn', story, 'user-message',
                    '历史群聊图片读取失败，已跳过这张图（本回合照常写作）来源=%s 错误=%s',
                    source, error,
                )
                continue
            if not image:
                self.report(
                    'warn', story, 'user-message',
                    '历史群聊图片读取失败，已跳过这张图（本回合照常写作）来源=%s 错误=%s',
                    source, '取回结果为空（端点不可用 / 图已过期 / 坐标不可信）',
                )
                continue
            entry: dict[str, Any] = {
                'id': 'group-history-image-%d' % (len(selected) + 1),
                **image,
                'sourceEntryId': pick(ref, 'source_entry_id', 'source_entry_id'),
                'senderId': pick(ref, 'sender_id', 'sender_id'),
                'senderName': pick(ref, 'sender_name', 'sender_name'),
                'occurredAt': pick(ref, 'occurred_at', 'occurred_at'),
                # 历史图片是**旧证据**，不是当前指令：低细节发送（上游在 multipart 那一跳
                # 硬编码 `detail:'low'`）。随条目带上是这个意图的唯一落点。
                'detail': 'low',
            }
            message_id = pick(ref, 'message_id', 'message_id')
            if message_id:
                entry['messageId'] = message_id
            selected.append(entry)
        selected.reverse()
        return selected

    async def flush_group_turn(self, key: str, revision: int) -> None:
        """上游 `flushGroupTurn(key, revision)`（`src/service.ts:1769`）。

        与私聊回合不同，群批次在模型请求开始之后**仍然可投递**：新到的群消息构成下一批，
        于是繁忙的群聊不会永久饿死主角的回复。整段流程：
        意愿判定 → 冷却检查 → 串行快照 → 主叙事 `try_decide` → 串行落库 `persist_decision`
        → 表态 / 群投递 / 贴纸 / 原生表情 → 消耗意愿分数 → 排期压缩。
        """
        turn = self.buffered_group_turns.get(key)
        if turn is None or int(turn.get('revision') or 0) != revision:
            return
        group_id_hint = turn.get('group_id')
        if self.database_resetting:
            return
        if self.desktop_runtime_phase == 'paused':
            self.note_group_skip_reason(group_id_hint, '桌面端把运行阶段切成 paused，群回合暂停推进')
            return
        if turn.get('story_id') in self.narrating_stories:
            turn['timer'] = self.ctx.set_timeout(
                lambda: _spawn(self.flush_group_turn(key, revision)), 250,
            )
            return
        turn['timer'] = None
        batch = list(turn.get('messages') or [])
        del turn['messages'][:]
        if not batch:
            self.buffered_group_turns.pop(key, None)
            return

        story_id = turn.get('story_id')
        try:
            story = await self.get_story(story_id)
        except Exception as error:
            self.report_standalone('warn', '群聊回合读取剧本失败，已放弃本批消息 故事=%s 错误=%s', story_id, error)
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return
        if pick(story, 'status') != 'active':
            self.note_group_skip_reason(
                turn.get('group_id'),
                '剧本状态=%s（不是 active），群回合不会推进' % (pick(story, 'status') or '?'),
            )
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return

        rule = turn.get('rule') or {}
        group_id = turn.get('group_id')
        # 上游 1.0.1-rc23：走档位解析层（五档 / auto 按生活状态 / 旧数值门按 custom）。
        life_status = decode_story_state(pick(story, 'state')).get('life_status')
        willingness = evaluate_willingness_gate(
            self.group_willingness.get(key),
            pick(rule, 'willingnessPreset', 'willingness_preset'),
            pick(rule, 'willingnessAuto', 'willingness_auto'),
            life_status,
            pick(rule, 'willingness'),
            {
                'now': self.now_ms(),
                'message_count': len(batch),
                'content': '\n'.join(str(pick(item, 'content') or '') for item in batch),
                'mentioned_bot': bool(turn.get('mentioned_bot')),
                'quoted_bot': bool(turn.get('quoted_bot')),
            },
            rng=self.rng,
        )
        self.group_willingness[key] = willingness['state']
        turn['mentioned_bot'] = False
        turn['quoted_bot'] = False
        if not willingness['should_call']:
            self.report_operation(
                'diagnostic', 'debug', story, 'user-message',
                '群聊意愿未触发模型调用 群=%s 分数=%s 概率=%s 原因=%s', group_id,
                '%.3f' % willingness['state']['score'],
                '%.3f' % willingness['probability'],
                willingness['reason'],
            )
            # 「配好了但一直不开口」最常停在这一步，所以除了 diagnostic 报告，
            # 再留一条节流的可见说明（见 `note_group_skip_reason`）。
            self.note_group_skip_reason(
                group_id, '群聊意愿没到阈值，这一批不调用模型',
                '分数=%.3f 概率=%.3f 原因=%s' % (
                    willingness['state']['score'], willingness['probability'], willingness['reason'],
                ),
            )
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return
        if await self.group_cooldown_active(
            pick(story, 'id'), group_id, _config_limit(rule, 'cooldownSeconds', 'cooldown_seconds', 1),
        ):
            self.report_operation(
                'diagnostic', 'debug', story, 'user-message',
                '群聊仍在冷却期，跳过群发言 群=%s', group_id,
            )
            self.note_group_skip_reason(group_id, '群聊仍在冷却期，跳过本次群发言')
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return
        self.report_operation(
            'standard', 'info', story, 'user-message',
            '群聊消息准备进入主叙事 群=%s 模式=%s 意愿=%s', group_id,
            pick(rule, 'responseMode', 'response_mode'), '%.3f' % willingness['state']['score'],
        )

        self.narrating_stories.add(story_id)
        try:
            async def snapshot_task() -> dict[str, Any]:
                current = await self.get_story(pick(story, 'id'))
                if not current:
                    raise RuntimeError('剧本不存在，无法刷出群回合。')
                # 1.0.1-rc31：一次 `groupMessages` 同时给出**可见上下文**与**历史图片引用**
                # （上游 `GroupMessagesSnapshot = {messages, imageRefs}`）——上下文里看得见的
                # 消息和能回流的图必须来自同一批行，否则两份判据会漂。
                group_snapshot = await self.group_messages_snapshot(
                    pick(current, 'id'), group_id, _config_limit(rule, 'contextLimit', 'context_limit', 20),
                )
                current_now = self.now()
                return {
                    'story': current,
                    'from': narrative_cursor(current, current_now),
                    'now': current_now,
                    'contextMessages': group_snapshot['messages'],
                    'imageRefs': group_snapshot['imageRefs'],
                }

            snapshot = await self.serial(pick(story, 'id'), snapshot_task)
            # 上游这里构造的就是模型 payload 的形状 → camelCase（键名法）。
            context_messages = _group_context_messages(snapshot['contextMessages'])
            group_context: dict[str, Any] = {
                'groupId': group_id,
                'channelId': turn.get('channel_id'),
                'label': pick(rule, 'label'),
                'purpose': pick(rule, 'purpose'),
                'characterRole': pick(rule, 'characterRole', 'character_role'),
                'messages': context_messages,
            }
            chat_capabilities = _chat_capabilities_wire(
                self.group_chat_capabilities(turn.get('latest_session'), context_messages),
            )
            user_message = '\n\n'.join(
                '[群聊连续消息 %d｜%s]\n%s' % (index + 1, pick(item, 'speaker'), pick(item, 'content'))
                for index, item in enumerate(batch)
            )
            if self.semantic_turn_embedding_enabled():
                embedding_config = _config_section(_config_section(self.config, 'model'), 'embedding')
                embedding_limit = int(_config_limit(
                    embedding_config, 'maxInputCharacters', 'max_input_characters', 4_000,
                ))
                turn_query_embedding: Optional[list[float]] = await self.embed_text(
                    user_message[:embedding_limit],
                )
            else:
                turn_query_embedding = None
            # 两级表情选择（§48 甲）：`selection` 一处判"这一回合平铺条目还是只给分组目录"。
            sticker_selection = await self.sticker_selection_for_session(
                turn.get('latest_session'), turn_query_embedding,
            )
            sticker_catalog = sticker_selection['assets']
            sticker_groups = sticker_selection['groups']
            # 上游 2197-2215：群音频走**批次预算**（条数 = `maxPerMessage×4`、
            # 字节 = `maxFileSizeMB×4`），超出的延后 / 跳过并各留一条 warn。
            # v1.7.6 之前这里恒传 `[]`，于是"群里发的语音"从来没有作为音频证据进过 payload。
            group_audio = await _load_group_batch_audio(
                self, snapshot['story'], batch, turn.get('latest_session'),
            )
            # 视频理解（v1.9.1 起接进群回合；v1.9.9 起**画面帧也进模型**）：群里有人发视频
            # 时，按既有能力判断（视频理解总开关 + 群聊开关 + 抽帧模式 / 帧数 / 间隔 / 超时），
            # **帧**并进下面的群聊图片通道（1.0.1-rc31 那条：与群图共用「每回合图片数上限」），
            # **音轨**并进这批群音频。群里没有视频 = 零影响；没人识别 = 一句事实 + 一条
            # 可行动 warn（`explain_skips`，绝不静默，见 `video_understanding`）。
            video_audio, video_note, video_media = await _group_video_media(
                self, snapshot['story'], turn.get('latest_session'),
                group_id, len(group_audio),
            )
            group_audio = list(group_audio) + list(video_audio)
            # 群聊视觉通道（1.0.1-rc31）：本批消息带过的图片是**当前回合的图**，
            # 历史条目落的 `groupImageRefs` 是**可回流的旧证据**。两者都只在本回合存在，
            # 按上游口径：当前图 `images`、历史图 `historicalGroupImages`，去重后一起交给
            # 主叙事；历史图由选择器按"同群、新到旧、最近 N 张"取（`historicalImageLimit`，
            # 0 = 关闭）。视觉关着时 `load_native_images` 与选择器各自早退，一个字节都不取。
            #
            # **本回合视频抽出的帧排在这两者之间（v1.9.9）**：当前图 → 当前视频帧 → 历史图
            # ——帧与当前图同属"这一回合她眼前的东西"（一起抢「每回合图片数上限」，
            # 与私聊那条路**同一个** `_image_budget()`，见 `core/vision_budget.py`）；
            # 历史图是**旧证据**，走它自己的 `historicalImageLimit`，不占这份预算。
            image_session = next(
                (item.get('imageSession') for item in batch
                 if isinstance(item, dict) and item.get('imageSession') is not None),
                turn.get('latest_session'),
            )
            current_image_sources = _unique([
                str(source) for item in batch
                for source in (_turn_get(item, 'imageSources', 'image_sources') or [])
                if str(source or '').strip()
            ])
            # 这个回合她有没有**视觉通道**——判据就是下面给 `try_decide` 的那两处
            # （原生图片附件 `images` / 侧端识图的观察结果 `visualObservations`），
            # 所以"帧有没有去处"也在这里判、不另读一遍配置。
            #
            # v1.9.9：**侧端识图（`mode = sidecar`）也是通道**。群里有人发图 / 发视频，
            # 她应当能看到内容（用户第一原则：按真人用 QQ 的直觉），而侧端模式此前只在
            # 私聊接了 —— 群回合的 `visualObservations` 恒为 `None`，群图与本回合视频帧
            # 于是**没有任何去处**。判据一处：通道 = 图片理解总开关 ⊗ 识图方式
            # （`native` 原生 / `sidecar` 侧端）；侧端那一跳复用私聊**同一个**
            # `describe_current_images()`，预算也复用同一个 `_image_budget()`
            # （见 `core/vision_budget.py`）——不新造开关、不新造预算。
            vision_section = _vision_config(self)
            vision_enabled = bool(_config_value(vision_section, 'enabled', False))
            vision_mode = _config_value(vision_section, 'mode', 'native') or 'native'
            native_vision = bool(vision_enabled and vision_mode == 'native')
            sidecar_vision = bool(vision_enabled and vision_mode == 'sidecar')
            frame_sources = list(video_media.image_sources)
            if frame_sources and not (native_vision or sidecar_vision):
                # 帧抽出来了、却**一条视觉通道都没有**（图片理解关着 / 识图方式认不出来）：
                # 丢掉，但**用一句诚实的事实换掉原来那句"抽了 N 帧画面"** + 一条可行动 warn
                # （坑 25：丢内容必须让人看见，而且不许把"没看到"写成"看到了"）。
                #
                # 事实行的顺序（与文档 §85 一致）：视频事实 → 图片预算线索；两句都在下面
                # **知道侧端识图通没通**之后才拼进正文（见 `for line in (video_note,
                # image_note)`），否则侧端那一跳失败时正文里会留着"抽了 N 帧画面"这句假话。
                video_note = note_group_frames_without_channel(self, snapshot['story'], video_media)
                frame_sources = []
            image_budget = _image_budget(self)
            available_image_sources = _unique(list(current_image_sources) + frame_sources)
            granted_image_sources = available_image_sources[:image_budget]
            try:
                current_images = await self.load_native_images(
                    snapshot['story'], granted_image_sources, image_session,
                )
            finally:
                # 帧字节已经在 `load_native_images` 里读进内存（音轨是 `data:` URI，
                # 也不依赖它）→ 临时目录现在就能删。放 finally：那一跳抛异常也不留垃圾。
                video_media.cleanup()
            historical_group_images = await self.load_historical_group_images(
                snapshot['story'],
                snapshot['imageRefs'],
                _config_limit(
                    rule, 'historicalImageLimit', 'historical_image_limit',
                    DEFAULT_HISTORICAL_IMAGE_LIMIT,
                ),
                image_session,
                available_image_sources,
            )
            # 每回合图片预算的截断线索（v1.9.4）：与私聊同一条判据、同一句话、同一条 warn
            # ——**候选总数只有这里知道**（当前群图 + 本回合视频帧，侧端模式下还包括
            # 历史群图，见下面），所以线索也在这里写。
            # 两道前置（照 `chunk3.flush_buffered_narrative`）：只在真的截了时说话；
            # 视觉关着时一个字都不说（那时一张都不取，说"取了前 N 张"是假话）。
            # 线索文本本身在下面与视频事实**一起**拼进正文（顺序：视频事实 → 预算线索）。
            images = current_images if native_vision else []
            historical_images = historical_group_images if native_vision else []
            # 侧端识图（v1.9.9）：群回合也走**私聊那一条**链 —— 同一个
            # `describe_current_images()`（→ 同一个 `vision_describer`、同一个
            # `vision.detail`、同一份"最近识过的图不重复识"台账），吃的是**同一份**预算内的
            # 候选：当前群图 → 本回合视频帧 → **历史群图**（旧证据）。
            # 观察结果按**与私聊逐字相同的形状**交给 `try_decide` 的
            # `visualObservations`（`list[str]`），群回合因此与私聊同一套语义、同一个字段。
            #
            # v1.9.9 补上 §85.13 第 1 条：历史群图在侧端模式下**不再静默丢** ——
            # 它走同一条识图链、同一份去重台账、同一套失败可见 warn，并且吃同一份
            # 「每回合图片数上限」（当前图与帧先占，剩下的额度才轮到历史图）；
            # 被预算挡在外面的那些由正文里的可数线索 + 节流 warn 点名闸门（坑 25：
            # 丢内容必须看得见，不许静默）。
            sidecar_images: list[Any] = []
            historical_granted: list[Any] = []
            if sidecar_vision:
                room = max(0, image_budget - len(current_images))
                historical_granted = list(historical_group_images)[:room]
                sidecar_images = list(current_images) + historical_granted
            # `visible=True`：失败 / 超时 / 没返回内容在群里必须**看得见**（坑 25）——
            # 丢的是画面内容，用户只会在剧本里看到她"没反应"。私聊那条路不传它，
            # 日志逐字不变（反向用例钉着）。
            visual_observations: Optional[list[str]] = None
            if sidecar_vision:
                visual_observations = await self.describe_current_images(
                    snapshot['story'], sidecar_images, user_message, visible=True,
                )
                if not visual_observations and frame_sources:
                    # 帧抽出来了、侧端识图却没给出观察结果：正文里那句"抽了 N 帧画面"
                    # 在群里**是假话**（帧没交给任何模型）→ 换成诚实的同一句形态
                    # （帧数照报、降级原因顶在最前）。与没有原生通道那一档同一条尺子，
                    # 但原因串不同：那一条是"没有通道"，这一条是"侧端这一跳没成"。
                    video_note = _sidecar_frames_without_observation_note(self, video_media)
            budget_candidates = len(available_image_sources)
            budget_granted = len(granted_image_sources)
            if sidecar_vision:
                # 侧端这条链的候选里还有历史群图（它自己也进了同一条识图调用），
                # 线索照实报这条链的候选数与实际交给模型的张数。
                budget_candidates += len(historical_group_images)
                budget_granted += len(historical_granted)
            image_note = ''
            if vision_enabled:
                image_note = image_budget_note(budget_candidates, budget_granted)
            if image_note:
                note_image_budget_skip(
                    self, image_session, budget_candidates, budget_granted, image_budget,
                )
            # 视频事实 → 图片预算线索（文档 §85 的顺序）：两句都在**知道侧端识图通没通**
            # 之后才拼进正文，所以上面那句诚实替换真的生效。
            for line in (video_note, image_note):
                if line:
                    user_message = '%s\n%s' % (user_message, line)
            decision_result = await self.try_decide(
                snapshot['story'], None, 'user-message', snapshot['from'], snapshot['now'],
                user_message, [], [], group_context, images, group_audio, chat_capabilities, [],
                sticker_catalog, turn_query_embedding, visual_observations, None, None, sticker_groups,
                historical_images,
            )
            decision = pick(decision_result, 'decision') or {}
            succeeded = bool(pick(decision_result, 'succeeded'))
            chat_actions = normalize_group_chat_actions(decision, chat_capabilities, group_context)
            # 每回合一个**新的**追问预算（同回合最多多问一次，铁律见 §48 兜底表）。
            sticker_follow_up: dict[str, Any] = {}
            sticker = await self.resolve_sticker_selection(
                decision, sticker_selection, sticker_follow_up,
            )
            native_face = None if sticker else self.resolve_native_face(decision, chat_capabilities)

            async def persist_task() -> dict[str, Any]:
                if self.database_resetting or not succeeded:
                    return {
                        'content': '', 'messages': [], 'chat_actions': {'reactions': []},
                        'sticker': None, 'native_face': None, 'commit': None, 'script_entry': None,
                    }
                current = await self.get_story(pick(story, 'id'))
                if not current:
                    raise RuntimeError('剧本不存在，无法落库群回合决策。')
                patched = dict(decision)
                patched['messageReactions'] = [
                    {'messageRef': item.get('messageRef'), 'reaction': item.get('reaction')}
                    for item in (chat_actions.get('reactions') or [])
                ]
                # 上游 `localMedia: sticker ? decision.localMedia : undefined`：
                # 没有选中贴纸时该键必须消失，否则承诺了不存在的投递。
                if sticker:
                    patched['localMedia'] = pick(decision, 'localMedia', 'local_media')
                else:
                    patched.pop('localMedia', None)
                    patched.pop('local_media', None)
                if native_face:
                    patched['nativeFace'] = pick(decision, 'nativeFace', 'native_face')
                else:
                    patched.pop('nativeFace', None)
                    patched.pop('native_face', None)
                persisted = await self.persist_decision(
                    current, None, patched, snapshot['from'], snapshot['now'], False, 'user-message',
                )
                content = normalize_group_visible_reply(
                    pick(decision, 'groupReply', 'group_reply'),
                    pick(decision, 'interaction'),
                    _message_characters(self.runtime_config),
                    str(_config_limit(self.runtime_config, 'messageSeparator', 'message_separator', '<sep/>')),
                )
                await self.db_set('interlude_story', {'id': pick(current, 'id')}, {
                    'cursorAt': snapshot['now'], 'updatedAt': self.now(),
                })
                if succeeded:
                    await self.schedule_conversation_follow_ups_after_turn(
                        pick(current, 'id'), snapshot['now'], pick(decision, 'interaction'),
                    )
                return {
                    'content': content,
                    'messages': pick(persisted, 'messages') or [],
                    'chat_actions': chat_actions,
                    'sticker': sticker,
                    'native_face': native_face,
                    'commit': pick(persisted, 'commit'),
                    'script_entry': pick(persisted, 'scriptEntry', 'script_entry'),
                }

            result = await self.serial(pick(story, 'id'), persist_task)
            platform_entry_id = pick(result['script_entry'], 'id')

            def reference_for(reaction: dict[str, Any]) -> Any:
                return platform_action_reference(
                    result['commit'], platform_entry_id, 'message-reaction',
                    '%s:%s' % (reaction.get('messageRef'), reaction.get('reaction')),
                )

            completed_reactions = (
                await self.execute_group_reactions(
                    snapshot['story'], turn.get('latest_session'), group_id,
                    result['chat_actions'].get('reactions') or [], reference_for,
                )
                if (result['chat_actions'].get('reactions') and turn.get('latest_session'))
                else 0
            )
            # 共同作品（works）：模型提的修改稿 / 起草请求，回合落库之后才处理
            # （只调 chunk14 的方法，不在本文件新增成员——Chunk1 与 Chunk3 同源）。
            work_saver = getattr(self, 'apply_work_proposal', None)
            proposal = pick(decision, 'workProposal', 'work_proposal')
            if callable(work_saver) and proposal and turn.get('latest_session'):
                await work_saver(
                    snapshot['story'], turn.get('latest_session'), proposal,
                    source_entry_id=platform_entry_id,
                )
            work_starter = getattr(self, 'start_work_generation', None)
            work_request = pick(decision, 'workRequest', 'work_request')
            if callable(work_starter) and work_request and turn.get('latest_session'):
                await work_starter(
                    snapshot['story'], turn.get('latest_session'), work_request,
                    source_entry_id=platform_entry_id,
                )
            dispatcher = getattr(self, 'dispatch_platform_actions', None)
            if callable(dispatcher) and turn.get('latest_session'):
                # 动作教学两段式的**第二段**（§86.4）：她只写了动作 id（或整条没写 `params`）时，
                # 补问一次"这条动作怎么填"并把参数回填进决策——**第一段文本仍是最终有效的**。
                # 每回合一个**新的**空预算 = 最多一次额外调用；失败 / 超时 / 回执不可用
                # 都由 chunk12 留可见 warn，本回合照跑（判据与回填都在 chunk12 一处，
                # 这里只负责那一跳——照 `dispatch_platform_actions` 的跨 chunk 调用形状）。
                action_params = getattr(self, 'resolve_platform_action_params', None)
                if callable(action_params):
                    await action_params(
                        decision, follow_up_budget={}, message=visible_reply_text(decision),
                    )
                await dispatcher(
                    snapshot['story'], decision,
                    session=turn.get('latest_session'), channel_id=str(turn.get('channel_id') or ''),
                )
            if result['content']:
                group_delivery = await self.send_group_message(
                    snapshot['story'], turn.get('channel_id'), result['content'],
                    pick(result['chat_actions'].get('replyTo'), 'messageId', 'message_id'),
                    turn.get('latest_session'),
                )
            else:
                group_delivery = {'deliveredSegments': [], 'complete': False, 'segmentOutcomes': []}
            delivered_segments = pick(group_delivery, 'deliveredSegments', 'delivered_segments') or []
            segment_outcomes = pick(group_delivery, 'segmentOutcomes', 'segment_outcomes') or []
            if segment_outcomes or delivered_segments:
                async def record_task() -> None:
                    current = await self.get_story(story_id)
                    if not current:
                        return
                    current_id = pick(current, 'id')
                    recorded_at = self.now()
                    group_event = find_group_script_event(result['commit']) if result['commit'] else None
                    script_event = (
                        message_event_reference(group_event, 0, platform_entry_id)
                        if group_event else None
                    )
                    if script_event:
                        for outcome in segment_outcomes:
                            await self.update_script_delivery_outcome(
                                current_id,
                                {**script_event, 'segment_index': pick(outcome, 'index')},
                                pick(outcome, 'status'), recorded_at, pick(outcome, 'reason'),
                            )
                    if delivered_segments:
                        metadata: dict[str, Any] = {
                            'groupId': group_id, 'channelId': turn.get('channel_id'),
                        }
                        if script_event:
                            metadata.update(script_event)
                        metadata['deliverySegmentIndexes'] = [
                            pick(item, 'index') for item in segment_outcomes
                            if pick(item, 'status') == 'delivered'
                        ]
                        if not pick(group_delivery, 'complete'):
                            metadata['partialDelivery'] = True
                            metadata['deliveredSegments'] = len(delivered_segments)
                        reply_to = pick(result['chat_actions'].get('replyTo'), 'messageRef', 'message_ref')
                        if reply_to:
                            metadata['replyTo'] = reply_to
                        await self.append_entry(current_id, {
                            'kind': 'character-group-message', 'actor': 'character',
                            'content': '<sep/>'.join(str(item) for item in delivered_segments),
                            'occurredAt': iso(recorded_at), 'metadata': metadata,
                        }, recorded_at)

                await self.serial(story_id, record_task)

            sticker_delivered = False
            if result['sticker'] and turn.get('latest_session'):
                sticker_delivered = await self.send_sticker(
                    snapshot['story'], turn.get('latest_session'), turn.get('channel_id'),
                    result['sticker'], group_id,
                    platform_action_reference(
                        result['commit'], platform_entry_id, 'local-media',
                        pick(result['sticker'], 'assetId', 'asset_id'),
                    ),
                )
            native_face_delivered = False
            if result['native_face'] and turn.get('latest_session'):
                native_face_delivered = await self.send_native_face(
                    snapshot['story'], turn.get('latest_session'), turn.get('channel_id'),
                    result['native_face'], group_id,
                    platform_action_reference(
                        result['commit'], platform_entry_id, 'native-face', result['native_face'],
                    ),
                )
            if delivered_segments or completed_reactions or sticker_delivered or native_face_delivered:
                self.group_willingness[key] = consume_willingness_gate(
                    self.group_willingness.get(key),
                    pick(rule, 'willingnessPreset', 'willingness_preset'),
                    pick(rule, 'willingnessAuto', 'willingness_auto'),
                    life_status,
                    pick(rule, 'willingness'),
                    self.now_ms(),
                )
            # 其他会话走与私聊回合**同一套**投递账本（上游 `src/service.ts:2306`，rc36 `:2450`）：
            # 群回合里她写下的跨会话动作（含"去另一个群说话"）同样要出站。不接这一跳就是
            # "她在群里决定了、却没有任何人去发"。`messages` 里只有跨会话动作（群回复走上面
            # 的 `send_group_message`，群回合没有私聊参与者，立即回复那一支进不来），所以
            # **没有跨群动作时 `messages` 为空、群路既有行为一个字都不变**（反向用例钉着）。
            # 上游这里同样**不**调 `confirm_outgoing_deliveries`：群目标的回执由
            # `send_cross_group_message` 自己落账（见 chunk7）。
            if result['messages']:
                await self.send_outgoing_messages(snapshot['story'], result['messages'])
            self.schedule_compaction(story_id)
        except Exception as error:
            self.report('warn', story, 'user-message', '群聊主叙事失败，保持静默 群=%s 错误=%s', group_id, error)
        finally:
            self.narrating_stories.discard(story_id)
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
