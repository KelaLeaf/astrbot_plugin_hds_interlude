"""上游 `src/service.ts` 第 1–623 行的移植：配置接口 + 桌面时间线只读投影。

本模块是 移植约定 分解契约里的 `config.py`：

* **1–258 行**：桌面（desktop）协议的只读投影纯函数 +
  `desktopTimelineEntryView` / `isHistoryEntryVisibleToParticipant` /
  `shouldRequestTurnEmbedding` 等模块级函数与常量。
* **260–623 行**：全部 Console 配置接口（`Config`、`BlindModeConfig`、
  `OneBotAccountRule`、`OneBotNapCatConfig`、`ChatActionsConfig`、
  `StickerLibraryConfig`、`GroupChatRule`、`MemoryConfig`、`RuntimeConfig`、
  `BrowserConfig`、`ParticipantPreset`、`SharedStoryConfig`、`RestWindow`、
  `StoryDefaults`、`LoggingConfig`、`StoryStartReadiness`），以及只在本文件内
  使用的中转形状（`AutoAdvanceConfig` / `BufferedUserMessage` /
  `BufferedNarrativeTurn` / `BufferedGroupTurn` / `ExecutableMessageReaction` /
  `ExecutableGroupChatActions` / `GroupDeliveryResult` / `DueIntentWake` /
  `PreparedCompaction*` / `CompactionBackoff` / `HistoryVectorEntry`）。

## 键名约定

| 位置 | 键名 | 理由 |
| --- | --- | --- |
| `Config` 及其全部配置子接口的字段 | **snake_case** | 配置是 AstrBot 的落盘格式（`plugin/_conf_schema.json`）；上游 Console 的 camelCase 由 `normalize_config()` 在边界归一 |
| 桌面协议输出（`desktop_timeline_entry_view` / `*_delivery_reality_summary` / `*_scene_checkpoint` / `normalize_desktop_timeline_range_request`） | **上游 camelCase 原文** | 桌面桥的协议（`entryId` / `commitId` / `deliveryActions` / `detailLevel` …），是跨进程 wire format |
| 读取外部输入 | **camelCase 与 snake_case 双读**（camelCase 优先） | 旧数据 / Koishi 侧数据可能是任一种拼写 |

TypedDict 不做运行期校验，键名表只表达**语义归属**；运行期读写的真实键名由
上面这张表决定，并在每个函数里逐字对准上游。

命名映射：`camelCase` → `snake_case`（函数 / 变量）；类型名保持上游原名。
时间统一用 `from ..time import ...`。
"""

from __future__ import annotations

import copy
import io
import json
import math
import os
import re
from typing import Any, Literal, TypedDict

from ..time import dt_ms, iso, parse_dt, utc_now

__all__ = [
    # ---- 1–258 行：常量与桌面投影纯函数 ----
    'DESKTOP_TIMELINE_TRACKS',
    'RECALLABLE_ENTRY_KINDS',
    'SCHEDULE_PREPLAN_RETRY_BACKOFF',
    'COMPACTION_RETRY_BACKOFF',
    'TIMELINE_RETRY_BACKOFF_BASE',
    'TIMELINE_DIRECTOR_FUSE',
    'TIMELINE_DIRECTOR_FUSE_COOLDOWN',
    'STICKER_DESCRIPTION_RETRY_COOLDOWN',
    'parse_desktop_timeline_date',
    'normalize_desktop_timeline_range_request',
    'desktop_range_overlaps',
    'desktop_delivery_reality_summary',
    'desktop_scene_checkpoint',
    'desktop_timeline_entry_view',
    'desktop_delivery_reality_summary',
    'desktop_scene_checkpoint',
    'empty_desktop_timeline_range',
    'is_history_entry_visible_to_participant',
    'should_request_turn_embedding',
    'is_trusted_image_host',
    'empty_desktop_timeline_range',
    'desktop_range_overlaps',
    'normalize_desktop_timeline_range_request',
    'desktop_timeline_track_for_entry',
    'parse_desktop_timeline_date',
    # ---- 260–623 行：配置 / 中转形状 TypedDict ----
    'DesktopTimelineTrack',
    'DesktopTimelineRangeRequest',
    'DesktopRuntimePhase',
    'DesktopInboundEvent',
    'Config',
    'BlindModeConfig',
    'OneBotAccountRule',
    'OneBotNapCatConfig',
    'ChatActionsConfig',
    'StickerLibraryConfig',
    'GroupChatRule',
    'MemoryConfig',
    'RuntimeConfig',
    'BrowserConfig',
    'ParticipantPreset',
    'SharedStoryConfig',
    'RestWindow',
    'AutoAdvanceConfig',
    'BufferedUserMessage',
    'BufferedNarrativeTurn',
    'BufferedGroupTurn',
    'ExecutableMessageReaction',
    'ExecutableGroupChatActions',
    'GroupDeliveryResult',
    'DueIntentWake',
    'PreparedCompactionSkip',
    'PreparedCompactionRun',
    'CompactionBackoff',
    'PreparedCompaction',
    'HistoryVectorEntry',
    'SessionFileFact',
    'StoryDefaults',
    'LoggingConfig',
    'StoryStartReadiness',
    'StoryStartReadinessPreview',
    # ---- 配置归一 ----
    'normalize_config',
    'CONFIG_DEFAULTS',
    'CONFIG_SECTION_ALIASES',
    'CONFIG_SECTION_ALIASES_REVERSE',
    'LEGACY_SECTION_ALIASES',
    'LEGACY_SECTION_MERGES',
    'LEGACY_KEY_MERGES',
    'LEGACY_MERGE_TARGETS',
    'VOICE_SECTION',
    'VOICE_LEGACY_SECTION',
    'VOICE_FIELD_KEYS',
    'merge_legacy_section_values',
    'fold_legacy_section_merges',
    'read_section_path',
    'write_section_path',
    'write_merge_target',
    'schema_group_defaults',
    'apply_section_aliases',
    'to_schema_shape',
    'PROMPT_SECTION',
    'PROMPT_FIELD_KEYS',
    'resolve_prompt_fields',
    'snapshot_defaults',
    'resolve_blind_mode_config',
    'resolve_black_box_config',
]


# =========================================================================== #
# 1–258 行：桌面时间线投影 + 会话级纯判定
# =========================================================================== #

#: 上游 `DESKTOP_TIMELINE_TRACKS`。
DESKTOP_TIMELINE_TRACKS: list['DesktopTimelineTrack'] = [
    'script', 'messages', 'system', 'scenes', 'facts', 'preplan',
]

#: 原始内容可参与语义历史召回的条目类型（上游 `RECALLABLE_ENTRY_KINDS`）。
RECALLABLE_ENTRY_KINDS: list[str] = [
    'user-message', 'character-message', 'script', 'group-message', 'character-group-message',
]

#: 日程预排生成失败后的退避（上游 `SCHEDULE_PREPLAN_RETRY_BACKOFF = 2 * Time.hour`，单位毫秒）。
SCHEDULE_PREPLAN_RETRY_BACKOFF = 2 * 60 * 60 * 1000
#: 一次失败的压缩不得在下一个叙事回合对同一未变场景重试（上游 `COMPACTION_RETRY_BACKOFF`）。
COMPACTION_RETRY_BACKOFF = 2 * 60 * 60 * 1000
#: 失败的时间线窗口在游标未变时的重试退避基数（上游 `TIMELINE_RETRY_BACKOFF_BASE`）。
TIMELINE_RETRY_BACKOFF_BASE = 10 * 60 * 1000
#: 连续失败达到该次数后熔断：自动推进降级为无账本守恒推进（上游 `TIMELINE_DIRECTOR_FUSE`）。
TIMELINE_DIRECTOR_FUSE = 6
#: 熔断后的冷却，到期后自动解除并重试一次（上游 `TIMELINE_DIRECTOR_FUSE_COOLDOWN`）。
TIMELINE_DIRECTOR_FUSE_COOLDOWN = 2 * 60 * 60 * 1000
#: 贴纸描述失败后的重试冷却（上游 `STICKER_DESCRIPTION_RETRY_COOLDOWN`）。
STICKER_DESCRIPTION_RETRY_COOLDOWN = 30 * 60 * 1000

#: 上游 `isTrustedImageHost` 允许的 QQ / OneBot CDN 域（根域 + 子域匹配）。
_TRUSTED_IMAGE_DOMAINS: tuple[str, ...] = (
    'gchat.qpic.cn', 'c2cpicdw.qpic.cn', 'multimedia.nt.qq.com.cn',
    'thirdqq.qlogo.cn', 'q.qlogo.cn',
)

#: 上游 `desktopTimelineTrackForEntry` 的 `/message|chat|reply/u`。
_MESSAGE_KIND_RE = re.compile(r'message|chat|reply', re.UNICODE)
#: 上游桌面游标 `^entry:\d+$`。
_DESKTOP_CURSOR_RE = re.compile(r'^entry:\d+$', re.UNICODE)
#: 上游 `DesktopTimelineTrack = 'script' | ...`。
DesktopTimelineTrack = Literal['script', 'messages', 'system', 'scenes', 'facts', 'preplan']

#: 上游 `import type { DesktopInboundEvent, DesktopRuntimePhase } from './desktop-bridge'`。
#: 这两个类型由 `desktop_bridge` 移植任务定义；本模块只按名引用（`from __future__ import
#: annotations` 使注解不求值），故不硬依赖其落地。
DesktopRuntimePhase = Any
DesktopInboundEvent = Any


# 上游 `interface DesktopTimelineRangeRequest`：字段名保持上游 camelCase，
# 这是**桌面桥的协议 wire format**。
# 上游字段 `from` 是 Python 保留字，无法写进类体；改用函数式 TypedDict 语法，
# 把运行期键名原样保留为 `'from'`（与 `types.py` 对 `SchedulePreplanRegime` 的做法一致）。
DesktopTimelineRangeRequest = TypedDict('DesktopTimelineRangeRequest', {
    'from': 'str',
    'to': 'str',
    'tracks': 'list[DesktopTimelineTrack]',
    'cursor': 'str',
    'detailLevel': "Literal['summary', 'full']",
    'limit': 'int',
}, total=False)


def _is_record(value: Any) -> bool:
    """上游 `isRecord`：非 None 的对象且不是数组。

    注意：JS 的 `!!{}` 为 **true**，空对象也是 record（`{}` 与 `null` 语义不同），
    所以这里不能用 `bool(value)` —— 那会把空字典判成非 record。
    """
    return value is not None and isinstance(value, dict)


def _str(value: Any) -> str:
    """上游 `String(value ?? '')`：null/undefined 变空串，其余走字符串化。"""
    if value is None:
        return ''
    return str(value)


def _number(value: Any) -> float:
    """上游 `Number(value)`。

    JS 的 `Number(undefined)` / `Number(null)` / `Number('')` 分别为 NaN / 0 / 0，
    `Number(True)` 为 1。Python 没有 NaN 隐式语义，这里显式对齐：
    None → NaN（未提供），布尔 → 0/1，其余交给 `float()`，失败给 NaN。
    """
    if value is None:
        return float('nan')
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return float('nan')


def _finite(value: float) -> bool:
    """上游 `Number.isFinite`。"""
    return value == value and value not in (float('inf'), float('-inf'))


def _is_safe_integer(value: float) -> bool:
    """上游 `Number.isSafeInteger`。"""
    return _finite(value) and float(value).is_integer() and abs(value) <= 2 ** 53 - 1


def _desktop_timeline_track_for_entry(entry: Any) -> DesktopTimelineTrack:
    """上游 `desktopTimelineTrackForEntry`。

    注意参数是条目（wire format），故读 camelCase 的 `kind` / `actor`。
    """
    kind = entry.get('kind') if isinstance(entry, dict) else None
    actor = entry.get('actor') if isinstance(entry, dict) else None
    if kind == 'script' or actor == 'narrator':
        return 'script'
    if isinstance(kind, str) and _MESSAGE_KIND_RE.search(kind):
        return 'messages'
    return 'system'


def parse_desktop_timeline_date(value: Any) -> Any:
    """上游 `parseDesktopTimelineDate`：只接受字符串，非法时间返回 `None`。"""
    if not isinstance(value, str):
        return None
    return parse_dt(value)


def _truthy_timeline_window(value: Any) -> bool:
    """上游 JS 真值判定（用于 `parseDesktopTimelineDate(window?.from) ?? entry.occurredAt`）。

    JS 侧 `window?.from` 若为空串 / null / undefined，解析结果一律落到 `entry.occurredAt`；
    Python 侧若直接传空串给 `parse_dt` 同样返回 `None`，语义一致。
    """
    return bool(value)


def is_trusted_image_host(hostname: str) -> bool:
    """上游 `isTrustedImageHost`：只抓 QQ / OneBot CDN 主机。

    这样任意用户提供的 URL 不会变成内网抓取代理。
    """
    host = _str(hostname).lower()
    if host.endswith('.'):
        host = host[:-1]
    return any(host == domain or host.endswith('.' + domain) for domain in _TRUSTED_IMAGE_DOMAINS)


def desktop_delivery_reality_summary(entry: Any) -> list[dict[str, Any]] | None:
    """上游 `desktopDeliveryRealitySummary`（Protocol 4）。

    由 V2 提交账本投影出的确定性、只读注解；桌面端无法改写它们，
    也不取代任何散文。条目没有可执行投递动作时返回 `None`。
    """
    metadata = entry.get('metadata') if isinstance(entry, dict) else None
    actions = metadata.get('deliveryActions') if isinstance(metadata, dict) else None
    if not isinstance(actions, list) or not actions:
        return None
    items: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get('eventId'), str):
            continue
        if not isinstance(action.get('segments'), list):
            continue
        segments = [
            {
                'kind': _str(segment.get('kind') if 'kind' in segment else 'message'),
                'status': _str(segment.get('status')),
                'content': _str(segment.get('content')),
            }
            for segment in action['segments']
            if isinstance(segment, dict)
            and isinstance(segment.get('content'), str)
            and isinstance(segment.get('status'), str)
        ]
        if not segments:
            continue
        items.append({
            'eventId': _str(action.get('eventId')),
            'eventKind': action.get('eventKind') if isinstance(action.get('eventKind'), str) else 'outgoing-message',
            'status': action.get('status') if isinstance(action.get('status'), str) else 'pending',
            'segments': segments,
        })
    return items or None


def desktop_scene_checkpoint(entry: Any) -> dict[str, Any] | None:
    """上游 `desktopSceneCheckpoint`：把场景检查点元数据投影成桌面可读形状。"""
    metadata = entry.get('metadata') if isinstance(entry, dict) else None
    checkpoint = metadata.get('sceneCheckpoint') if isinstance(metadata, dict) else None
    if not isinstance(checkpoint, dict):
        return None
    if not _finite(_number(checkpoint.get('sceneId'))):
        return None
    started_at = parse_desktop_timeline_date(checkpoint.get('startedAt'))
    if started_at is None:
        return None
    result: dict[str, Any] = {
        'sceneId': int(_number(checkpoint.get('sceneId'))),
        'startedAt': iso(started_at),
    }
    ended_at = parse_desktop_timeline_date(checkpoint.get('endedAt'))
    if ended_at is not None:
        result['endedAt'] = iso(ended_at)
    if isinstance(checkpoint.get('reason'), str):
        result['reason'] = checkpoint['reason']
    # 上游用 `Number.isSafeInteger(Number(x))`：null / undefined → Number() 为 0 → 通过。
    first_entry_id = _number(checkpoint.get('firstEntryId'))
    if _is_safe_integer(first_entry_id):
        result['firstEntryId'] = int(first_entry_id)
    last_entry_id = _number(checkpoint.get('lastEntryId'))
    if _is_safe_integer(last_entry_id):
        result['lastEntryId'] = int(last_entry_id)
    boundary = checkpoint.get('boundarySourceEntryIds')
    if isinstance(boundary, list):
        result['boundarySourceEntryIds'] = [
            int(_number(item)) for item in boundary if _is_safe_integer(_number(item))
        ]
    return result


def desktop_timeline_entry_view(entry: Any) -> dict[str, Any]:
    """上游 `desktopTimelineEntryView`：一条剧本条目的桌面只读视图。

    Protocol 4 注解：跨引用日志 / 发件箱的身份，以及投递账本中的**执行真相**。
    旧条目上这些字段缺失。输出键名逐字保持上游 camelCase（桌面桥 wire format）。
    """
    metadata = entry.get('metadata') if isinstance(entry, dict) else None
    window = metadata.get('timelineWindow') if isinstance(metadata, dict) else None
    window = window if isinstance(window, dict) else None
    started_at = parse_desktop_timeline_date(window.get('from') if window else None)
    if started_at is None:
        started_at = entry.get('occurredAt')
    ended_at = parse_desktop_timeline_date(window.get('to') if window else None)
    view: dict[str, Any] = {
        'entityId': 'entry:%s' % entry.get('id'),
        'id': entry.get('id'),
        'storyId': entry.get('storyId'),
        'participantId': entry.get('participantId'),
        'kind': entry.get('kind'),
        'actor': entry.get('actor'),
        'track': _desktop_timeline_track_for_entry(entry),
        'content': entry.get('content'),
        'occurredAt': iso(entry.get('occurredAt')),
        'startedAt': iso(started_at),
        'endedAt': iso(ended_at),
    }
    if isinstance(metadata, dict) and isinstance(metadata.get('commitId'), str):
        view['commitId'] = metadata['commitId']
    delivery = desktop_delivery_reality_summary(entry)
    if delivery:
        view['deliveryActions'] = delivery
    checkpoint = desktop_scene_checkpoint(entry)
    if checkpoint:
        view['sceneCheckpoint'] = checkpoint
    return view


def empty_desktop_timeline_range() -> dict[str, Any]:
    """上游 `emptyDesktopTimelineRange`。"""
    now = iso(utc_now())
    return {
        'protocol': 4, 'storyId': '', 'revision': 'empty',
        'range': {'from': now, 'to': now}, 'entries': [], 'scenes': [], 'facts': [],
    }


def normalize_desktop_timeline_range_request(value: Any) -> dict[str, Any]:
    """上游 `normalizeDesktopTimelineRangeRequest`。

    缺省窗口为「现在前 2 小时 → 现在后 12 小时」；倒置时自动交换；单次窗口
    上限 14 天（桌面视口不需要意外拉出多年数据，翻页才是历史的正规通道）。
    输出键名为**内部**形状（`cursorId` / `detailLevel`）；`tracks` 为去重后的
    track 名集合（上游是 `Set<T>`，Python 用 `list`，顺序与上游迭代顺序一致）。
    """
    now = utc_now()
    now_ms = dt_ms(now)
    raw = value if isinstance(value, dict) else {}

    start = parse_desktop_timeline_date(raw.get('from'))
    from_dt = start if start is not None else parse_dt(now_ms - 2 * 60 * 60 * 1000)
    end = parse_desktop_timeline_date(raw.get('to'))
    to_dt = end if end is not None else parse_dt(now_ms + 12 * 60 * 60 * 1000)
    if dt_ms(to_dt) < dt_ms(from_dt):
        from_dt, to_dt = to_dt, from_dt
    # 一个桌面视口永远不该顺手倾倒多年历史；翻页是历史的正规通道，这个上限保护 worker 数据库。
    max_window = 14 * 24 * 60 * 60 * 1000
    if dt_ms(to_dt) - dt_ms(from_dt) > max_window:
        to_dt = parse_dt(dt_ms(from_dt) + max_window)

    raw_tracks = raw.get('tracks') if isinstance(raw.get('tracks'), list) else DESKTOP_TIMELINE_TRACKS
    tracks: list[DesktopTimelineTrack] = []
    for track in raw_tracks:
        if track in DESKTOP_TIMELINE_TRACKS and track not in tracks:
            tracks.append(track)
    if not tracks:
        tracks = list(DESKTOP_TIMELINE_TRACKS)

    cursor = raw.get('cursor')
    cursor_id = int(cursor[len('entry:'):]) if isinstance(cursor, str) and _DESKTOP_CURSOR_RE.match(cursor) else None

    raw_limit = _number(raw.get('limit'))
    # 上游 `Number(value.limit) || 240`：NaN 与 0 都退化为 240。
    limit_source = raw_limit if _finite(raw_limit) and raw_limit != 0 else 240.0
    limit = max(1, min(int(limit_source // 1), 500))

    return {
        'from': from_dt,
        'to': to_dt,
        'tracks': tracks,
        'cursorId': cursor_id,
        'limit': limit,
        'detailLevel': 'full' if raw.get('detailLevel') == 'full' else 'summary',
    }


def desktop_range_overlaps(from_dt: Any, to_dt: Any, start: Any, end: Any = None) -> bool:
    """上游 `desktopRangeOverlaps`（`end` 缺省时等于 `start`）。"""
    final = end if end is not None else start
    return dt_ms(start) <= dt_ms(to_dt) and dt_ms(final) >= dt_ms(from_dt)


def is_history_entry_visible_to_participant(
    entry: Any, participant_id: str, share_participant_details: bool,
) -> bool:
    """上游 `isHistoryEntryVisibleToParticipant`。

    语义召回是原始历史的另一种视图，因此必须服从与 `recentScript` 相同的
    私聊分支边界。除非主人显式开启共享细节，群聊记录刻意排除在私聊回合之外。
    """
    if share_participant_details:
        return True
    kind = entry.get('kind') if isinstance(entry, dict) else None
    if kind == 'group-message' or kind == 'character-group-message':
        return False
    owner = entry.get('participantId') if isinstance(entry, dict) else None
    return not owner or owner == participant_id


def should_request_turn_embedding(
    embedding: Any, sticker_library_enabled: bool, sticker_count: int,
) -> bool:
    """上游 `shouldRequestTurnEmbedding`。

    只有真正用得上实时查询向量的功能才值得一次网络请求：未启用或规模很小的
    贴纸库不得悄悄把普通事实向量化变成每条消息一次网络往返。

    `SEMANTIC_STICKER_LIMIT` 定义在 `helpers.py`（上游 8284 行）；此处按上游的
    函数级引用延迟导入，避免两个模块间的循环 import。
    """
    from .helpers import SEMANTIC_STICKER_LIMIT

    if not (_is_record(embedding) and embedding.get('enabled')):
        return False

    def pick(snake: str, camel: str) -> Any:
        # 配置层裁决：配置键 snake_case（`normalize_config` 已归一）；
        # 同时接受上游 camelCase 键，便于未过归一的旧调用方。
        if snake in embedding:
            return embedding[snake]
        return embedding.get(camel)

    return (
        pick('live_query', 'liveQuery') is True
        or pick('semantic_history', 'semanticHistory') is True
        or (
            pick('semantic_sticker_filter', 'semanticStickerFilter') is True
            and bool(sticker_library_enabled)
            and sticker_count > SEMANTIC_STICKER_LIMIT
        )
    )


# =========================================================================== #
# 260–623 行：Console 配置接口
# =========================================================================== #
#
# **字段名一律 snake_case**（配置层裁决，见本节末尾 `normalize_config` 的说明）：
# 配置不是「发给模型的 wire format」，而是 AstrBot 的落盘格式——`plugin/_conf_schema.json`
# 按 snake_case 生成（AstrBot 生态惯例，也是本插件 v0.1.0 的既有形状）。
# 上游 Console 的 camelCase 键由 `normalize_config()` 在边界上归一。
# 依赖的兄弟类型用 `Any` 占位（避免硬依赖尚未落地的并行模块）；
# 运行期一律传 dict，`Any` 不损失任何行为。


class BlindModeConfig(TypedDict, total=False):
    """盲区模式：压制 HDSI 可见性与 Koishi 命令的沉浸式运行（上游 287 行）。"""

    enabled: bool
    #: 其余 HDSI 日志全部隐藏时，周期性、刻意最小的健康信号。
    health_report_minutes: int


class OneBotAccountRule(TypedDict, total=False):
    """Console 账号表里的一行（上游 295 行）。

    QQ id 是字符串：QQ 号可能超过 JS 安全整数范围，且 Koishi 以文本暴露它。
    """

    qq: str
    label: str
    enabled: bool
    #: 白名单私聊用户的可选身份字段。
    person_id: str
    profile: str
    relationship: str


class GroupChatRule(TypedDict, total=False):
    """一条群聊规则（上游 340 行）。"""

    group_id: str
    label: str
    enabled: bool
    purpose: str
    character_role: str
    response_mode: Literal['mention-only', 'always']
    context_limit: int
    debounce_seconds: int
    cooldown_seconds: int
    #: 上游 `Partial<GroupWillingnessConfig>`；本移植版 `GroupWillingnessConfig`
    #: 的键是 snake_case，读取方 `resolve_group_willingness()` 两种拼写都认。
    willingness: Any


class OneBotNapCatConfig(TypedDict, total=False):
    """可选的 OneBot / NapCat 账号闸门，只影响 onebot 平台（上游 305 行）。"""

    #: 为 false（或旧配置缺省）时 OneBot 访问不受影响。
    enabled: bool
    #: 允许发送角色消息的 NapCat 账号。
    bot_accounts: list[OneBotAccountRule]
    #: @deprecated 只为旧 YAML 还能加载；运行期只认白名单。
    user_mode: Literal['allowlist', 'blocklist']
    user_accounts: list[OneBotAccountRule]
    #: 显式 OneBot 群白名单；群成员不需要私聊白名单权限。
    group_chats: list[GroupChatRule]
    #: 阻止回声的自发消息进入叙事。
    ignore_self_messages: bool


class ChatActionsConfig(TypedDict, total=False):
    """跨平台聊天手势（上游 319 行）；运行期连接器可用性仍是权威。"""

    enabled: bool
    platforms: list[Literal['qq', 'wechat']]
    quote_reply: bool
    message_reactions: bool
    allowed_reactions: list[str]
    native_faces: bool
    expression_threshold: float
    allowed_native_faces: list[str]


class StickerLibraryConfig(TypedDict, total=False):
    """本地表情库（上游 330 行）。"""

    enabled: bool
    directory: str
    max_file_size_mb: float
    catalog_limit: int
    description_max_tokens: int
    #: API JSON 模式可选；prompt-only 仍要求紧凑 JSON 合约。
    description_response_format: Literal['json-object', 'prompt-only']


class MemoryConfig(TypedDict, total=False):
    """记忆 / 压缩 / 设定演化配置（上游 353 行）。"""

    enabled: bool
    background_interval_minutes: int
    max_stories_per_compaction_run: int
    scene_entry_threshold: int
    scene_character_threshold: int
    compaction_entry_limit: int
    compaction_character_limit: int
    scene_hook_characters: int
    scene_summary_characters: int
    arc_summary_characters: int
    #: 有多少个紧邻其前的已关闭场景摘要加入 prompt。
    previous_scene_summaries: int
    recent_entry_limit: int
    fact_limit: int
    fact_content_characters: int
    fact_importance_weight: float
    fact_confidence_weight: float
    fact_recency_weight: float
    semantic_weight: float
    unresolved_weight: float
    state_patch_confidence_threshold: float
    major_state_patch_confidence_threshold: float
    state_patch_min_evidence: int
    #: 一次次要 overlay 变更所需的最少独立叙事回合数。
    state_patch_min_turns: int
    #: 次要补丁证据所覆盖的最少不同日历天数。
    state_patch_min_days: int
    #: 同一 target/path 上稳定 overlay 变更之间的冷却。
    state_patch_cooldown_hours: float
    auto_apply_state_patches: bool
    allow_major_state_changes: bool
    max_facts_per_story: int
    #: 把短命的戏剧性余波留作后续写作的上下文。
    active_consequences_enabled: bool
    #: 一次主叙事 prompt 最多携带的活跃余波条数。
    active_consequence_prompt_limit: int
    #: 单条余波允许的最长寿命，保护 canon 不漂移。
    active_consequence_max_days: float
    #: 叙事器省略了有效余波的具体强度时使用的默认强度。
    active_consequence_default_strength: float
    overlay_compression_enabled: bool
    overlay_recent_days: float
    overlay_monthly_after_days: float
    overlay_weekly_window_days: float
    overlay_monthly_window_days: float
    overlay_weekly_summary_characters: int
    overlay_monthly_summary_characters: int


class RestWindow(TypedDict, total=False):
    """休息窗口（上游 501 行）：窗口内的自动推进间隔另算。"""

    enabled: bool
    label: str
    start: str
    end: str
    min_interval_minutes: int
    max_interval_minutes: int


class RuntimeConfig(TypedDict, total=False):
    """运行期节奏 / 上下文预算（上游 403 行）。"""

    capture_direct_messages: bool
    auto_create: bool
    ignore_command_messages: bool
    allow_proactive_messages: bool
    #: 后台主动联系所需的叙事器自报意愿下限。
    proactive_willingness_threshold: float
    sweep_interval_minutes: int
    minimum_advance_minutes: int
    max_stories_per_sweep: int
    context_entry_limit: int
    #: 除条数下限之外，还保留这个近期时间窗内的原始条目。
    context_time_window_minutes: float
    memory_limit: int
    max_script_characters: int
    max_message_characters: int
    minimum_delayed_reply_seconds: float
    maximum_delayed_reply_minutes: float
    cancel_delayed_replies_on_user_message: bool
    #: 叙事 provider 瞬时失败后重试一个用户回合。
    narrative_retry_delay_seconds: float
    #: 每个失败用户回合的最大自动重试次数；0 关闭重试。
    narrative_retry_max_attempts: int
    #: 按配置的分隔符把模型回复拆成多条消息。
    split_reply_messages: bool
    message_separator: str
    typing_base_delay_seconds: float
    typing_characters_per_second: float
    typing_max_delay_seconds: float
    #: 模拟打字延迟上的随机抖动；0 保持确定性时序。
    typing_jitter_ratio: float
    #: 最新一条用户消息之后、启动写作请求之前的等待。
    user_message_debounce_seconds: float
    #: @deprecated 自 0.1.2 起忽略；请求在第一条回复提交之前始终可替换。
    stale_narrative_request_window_seconds: float
    #: 新版自动推进调度；旧版 minimumAdvanceMinutes 仍保留兼容。
    auto_advance_enabled: bool
    auto_advance_interval_minutes: float
    auto_advance_jitter_minutes: float
    #: 一次对话之后的短程生活补写，单位分钟。
    conversation_follow_up_minutes: list[float]
    #: 施加到每个短程连续性补写上的小幅随机偏移。
    conversation_follow_up_jitter_minutes: float
    rest_windows: list[RestWindow]


class BrowserConfig(TypedDict, total=False):
    """只读网页观察（上游 449 行）。"""

    enabled: bool
    #: 立即浏览是显式选择加入，因为它刻意多加一次模型 / 浏览器往返。
    mode: Literal['deferred-only', 'allow-immediate']
    allow_search: bool
    allow_visit: bool
    search_url_template: str
    allowed_domains: list[str]
    blocked_domains: list[str]
    max_concurrent_pages: int
    #: 每次后台 sweep 的工作上限，避免积压把故事队列握住几分钟。
    max_research_per_sweep: int
    navigation_timeout: float
    wait_until: Literal['domcontentloaded', 'networkidle2']
    max_text_characters: int
    max_excerpt_characters: int
    max_observations_in_prompt: int
    cache_minutes: float
    allow_group_triggered_research: bool
    log_observation_preview: bool


class ParticipantPreset(TypedDict, total=False):
    """Console 预设：把 QQ 账号变成具名的关系分支（上游 472 行）。"""

    qq: str
    person_id: str
    label: str
    profile: str
    relationship: str
    enabled: bool


class SharedStoryConfig(TypedDict, total=False):
    """一个 bot 账号一条主剧本的共享设定（上游 481 行）。"""

    #: 每个 bot 账号一条主剧本；保留开关以便安全回滚。
    enabled: bool
    #: 允许的账号首次私聊时自动加入既有主剧本。
    auto_enroll_participants: bool
    #: 允许一条入站消息引起一条有明确理由的、发往另一账号的消息。
    allow_cross_conversation_messages: bool
    #: 把其它参与者的关系 / 人设细节发给模型 provider。
    share_participant_details: bool
    #: 单个叙事回合产出的跨账号消息硬上限。
    max_cross_conversation_actions: int
    #: 发送给主叙事器的其它关系摘要条数。
    participant_context_limit: int
    #: 为空保持旧行为；否则只有这些 QQ 可以执行全局管理命令。
    manager_accounts: list[str]
    #: 可选的 QQ → 人物预设；同 personId 的账号共享身份注记。
    #: @deprecated 新配置请用 onebot.userAccounts 的身份字段。
    participant_presets: list[ParticipantPreset]


class StoryDefaults(TypedDict, total=False):
    """初始剧本默认值（上游 578 行）。"""

    character_name: str
    character_profile: str
    perspective: str
    user_profile: str
    relationship: str
    world: str
    supporting_cast: str
    location: str
    style: str
    timezone: str


class LoggingConfig(TypedDict, total=False):
    """日志配置（上游 591 行）。"""

    level: Literal['silent', 'error', 'warn', 'info', 'debug']
    #: 控制 info 级别下写入多少常规运行活动。
    verbosity: Literal['summary', 'standard', 'diagnostic']
    format: Literal['compact', 'detailed', 'layered']
    #: 施加语义化 ANSI 颜色；Koishi Console 与普通终端都能渲染。
    colors: bool
    #: 为深色 / 浅色 Console 主题选择高对比 ANSI 调色板。
    color_theme: Literal['dark', 'light']
    #: 显示固定动作颜文字；false 使用紧凑符号。
    kaomoji: bool
    log_script_preview: bool
    #: 把用户可见的收发消息正文写进插件日志。
    log_message_content: bool
    preview_length: int


class StoryStartReadiness(TypedDict, total=False):
    """「开始故事」就绪度检查（上游 608 行）。字段为运行期归一结果，用 snake_case。"""

    ready: bool
    existing: Any
    blockers: list[str]
    warnings: list[str]
    preview: dict[str, Any]


class StoryStartReadinessPreview(TypedDict, total=False):
    """`StoryStartReadiness.preview` 的内联对象形状。"""

    character_name: str
    character_profile: bool
    perspective: bool
    world: bool
    timezone: str
    model: str
    auto_create: bool


class Config(TypedDict, total=False):
    """插件 Console 配置根（上游 260 行）。

    字段名 snake_case：这是 AstrBot 的配置落盘格式（`plugin/_conf_schema.json`）。
    上游 Console 的 camelCase 键由 `normalize_config()` 在边界上归一。
    依赖的兄弟模块配置类型用 `Any` 占位，避免硬依赖尚未落地的并行模块。
    """

    #: 沉浸式运行：压制 HDSI 可见性与 Koishi 命令。
    blind_mode: BlindModeConfig
    #: @deprecated 已重命名为 blindMode；为既有 Console YAML 保留。
    black_box: BlindModeConfig
    model: Any
    runtime: RuntimeConfig
    story_defaults: StoryDefaults
    logging: LoggingConfig
    memory: MemoryConfig
    shared_story: SharedStoryConfig
    #: 可选的、由 koishi-plugin-puppeteer 支撑的只读网页观察。
    browser: BrowserConfig
    #: 可选的 OneBot/NapCat 账号闸门，只影响 onebot 平台。
    onebot: OneBotNapCatConfig
    #: 可选的跨平台聊天手势；运行期连接器可用性仍是权威。
    chat_actions: ChatActionsConfig
    stickers: StickerLibraryConfig
    alter_system: Any
    chat_rhythm: Any
    #: 自动窗口的时间导演；关闭时自动回合以无账本方式运行。
    timeline_director: dict[str, Any]
    agency: Any
    urge: Any
    schedule_preplan: Any


# =========================================================================== #
# 519–576 行：只在本文件内使用的中转形状
# =========================================================================== #
#
# 这些形状只在本移植版的 service 内部流转，故字段名一律 snake_case
# （键名约定 键名法）。
# 例外：`ExecutableMessageReaction` / `ExecutableGroupChatActions` /
# `GroupDeliveryResult` / `SessionFileFact` 是发给适配层的 wire format，保 camelCase。


class AutoAdvanceConfig(TypedDict, total=False):
    """自动推进调度配置（上游 510 行，由 runtime 配置解出）。"""

    enabled: bool
    interval_minutes: float
    jitter_minutes: float
    follow_up_minutes: list[float]
    follow_up_jitter_minutes: float
    rest_windows: list[RestWindow]


class BufferedUserMessage(TypedDict, total=False):
    """缓冲区里的一条用户消息（上游 519 行）。"""

    content: str
    occurred_at: Any
    superseded_intents: list[Any]
    quote: Any
    #: 只作短命来源链接；永不写入 HDSI 存储。
    image_sources: list[str]
    #: 只作短命语音 token / URL；永不写入 HDSI 存储。
    audio_sources: list[str]


class BufferedNarrativeTurn(TypedDict, total=False):
    """按关系建立的输入缓冲（上游 532 行）。

    消息会立即持久化，而叙事器会短暂等待用户把一小段话说完。
    """

    story_id: str
    participant_id: str
    messages: list[BufferedUserMessage]
    latest_session: Any
    #: 上下文计时器返回 disposer，而不是 Node 原生 Timeout。
    timer: Any
    next_revision: int
    in_flight_request_id: int
    first_message_committed_request_id: int
    obsolete_request_ids: set[int]


class BufferedGroupTurn(TypedDict, total=False):
    """按群建立的输入缓冲（上游 545 行）。"""

    story_id: str
    group_id: str
    rule: GroupChatRule
    channel_id: str
    latest_session: Any
    messages: list[Any]
    timer: Any
    revision: int
    mentioned_bot: bool
    quoted_bot: bool


class ExecutableMessageReaction(TypedDict, total=False):
    """可执行的消息表态（上游 558 行：`MessageReactionDraft & { messageId }`）。"""

    messageRef: str
    reaction: str
    messageId: str


class ExecutableGroupChatActions(TypedDict, total=False):
    """群聊可执行动作（上游 562 行）。"""

    replyTo: dict[str, str]
    reactions: list[ExecutableMessageReaction]


class GroupDeliveryResult(TypedDict, total=False):
    """群投递结果（上游 567 行）。"""

    deliveredSegments: list[str]
    complete: bool
    segmentOutcomes: list[dict[str, Any]]


class DueIntentWake(TypedDict, total=False):
    """到期的意图唤醒句柄（上游 573 行）。"""

    cancel: Any
    due_at: float


class PreparedCompactionSkip(TypedDict, total=False):
    """压缩准备：跳过（上游 225 行）。"""

    phase: Literal['skip']
    overlay_compacted: bool


class PreparedCompactionRun(TypedDict, total=False):
    """压缩准备：执行（上游 230 行）。"""

    phase: Literal['run']
    overlay_compacted: bool
    scene: Any
    scene_entries: list[Any]
    chars: int
    scene_compaction_due: bool
    current: Any
    participants: list[Any]
    visible_compaction_entries: list[Any]
    visible_compaction_facts: list[Any]
    compact_request: Any
    fingerprint: str


#: 上游 `type PreparedCompaction = PreparedCompactionSkip | PreparedCompactionRun`。
PreparedCompaction = Any


class CompactionBackoff(TypedDict, total=False):
    """压缩退避记录（上游 245 行）。"""

    fingerprint: str
    until: float


class HistoryVectorEntry(TypedDict, total=False):
    """历史向量缓存条目（上游 175 行）。"""

    spans: list[Any]
    embedding_identity: str
    tags: list[str]
    frame_id: str
    checkpoint: dict[str, int]
    vector: list[float]
    content: str
    occurred_at: str
    participant_id: str
    kind: str


class SessionFileFact(TypedDict, total=False):
    """入站 `<file>` 元素抽出的事实（上游 7151 行）。

    键名保持上游 camelCase：它是发给适配层 / 音频通道的 wire format。
    """

    name: str
    url: str
    size: float
    audio: bool


# =========================================================================== #
# 配置归一：camelCase → snake_case + 默认值补全
# =========================================================================== #

#: `_conf_schema.json` / AstrBot 配置里本来就该保持原样的键（适配层私有命名空间）。
#: 它们不是上游 Console 字段，`normalize_config` 不得改写（否则适配层读不到自己的数据）。
_LITERAL_KEY_PREFIXES = ('custom_', 'extra_', 'x_')

#: 这些键底下的**内容**是模型 / API 的 JSON 参数，不是配置字段名，禁止递归改名。
_OPAQUE_VALUE_KEYS = frozenset({'custom_extra_body'})

#: AstrBot 适配层自己的 provider 字段名（本来就是 snake_case，不是上游 Console 字段）。
#: `normalize_config` 必须原样保留，否则适配层读不到 API Key / 模型 / 自定义请求体。
_LITERAL_KEYS = frozenset({
    'api_key', 'api_base', 'provider_type', 'provider_source_id', 'custom_extra_body',
    'custom_headers', 'modalities', 'enable',
})

#: `plugin/_conf_schema.json` 的顶层分组名 → 上游 Console 的分组名。
#: 这两个名字是给用户在配置页看的（比 `model` / `onebot` 直观），但配置的**读取侧**
#: （`CONFIG_DEFAULTS` / `ServiceBase.cachedXxxConfig` / `_config_section(config, 'model')`）
#: 一律按上游名。别名在 `apply_section_aliases` 里统一补齐，只在目标键缺失时生效。
CONFIG_SECTION_ALIASES: dict[str, str] = {
    'model_center': 'model',
    'qq_access': 'onebot',
}

#: 反向：上游名 → schema 名。**持久化**时用（见 `to_schema_shape`）。
CONFIG_SECTION_ALIASES_REVERSE: dict[str, str] = {
    upstream: schema for schema, upstream in CONFIG_SECTION_ALIASES.items()
}

#: **只用于读取**的历史别名：旧版本里这些键存在过，老导出文件/老配置文件里有值。
#:
#: 与 `CONFIG_SECTION_ALIASES` 分开是刻意的：那张表会被**反向**用于持久化，
#: 若把 `qzone_compat → qzone` 混进去，用户配好的 QQ 空间会被写回那个已经作废的
#: 隐藏键（下次宿主按 schema 重建配置就清掉）——读得对、写回错，比不迁移更糟。
LEGACY_SECTION_ALIASES: dict[str, str] = {
    # v1.5.x 的 P3 隐藏兼容位 → v1.6.0 起 QQ 空间转正（见 PORTING_NOTES §32）。
    'qzone_compat': 'qzone',
    # 合并转发同理：v1.7.0 起 `forward_message_compat` → `forward_message`。
    'forward_message_compat': 'forward_message',
}

#: **N:1 的历史分组归并（只用于读取）**：`新分组 → 一组旧分组`。
#:
#: **v1.7.4 起点分路径**：键是**目标路径**，可以是嵌套的（`robot_actions.chat` 指
#: schema 里 `robot_actions.items.chat`；`runtime.input_status` 同理）。源永远是**顶层**
#: 旧分组名（宿主按 schema 重建配置，旧组只能留在顶层）。语义是：
#:
#: * **用户写过的新分组值优先**——"写过"的判定是"不等于 schema 默认值"，理由见下；
#: * 新分组里**没写过**的键，按顺序从这些旧分组里补，旧分组里也"没写过"（= 默认值）就跳过；
#: * 只在**读**方向生效。写方向只写新路径（`to_schema_shape` / 控制台）——旧名写回去
#:   等于把用户配置塞进一堆作废的隐藏键，下次宿主按 schema 重建时还要再迁一遍。
#:
#: 为什么旧分组必须继续留在 `_conf_schema.json` 里（**并且 `invisible: true`**）：
#: 宿主每次加载插件都按 schema 重建配置，schema 里没有的分组会被**直接删掉**（AGENTS 坑 22）。
#: 所以"把 `actions_interaction` 改名成 `actions_chat`"这种做法，会在用户**下次启动前**
#: 就把他设过的开关全清空——旧组留着 + 读取侧归并，才是升级不丢配置的唯一走法。
#: （与 `LEGACY_SECTION_ALIASES` 的 1:1 别名同一套思路，只是这里是 N:1。）
#:
#: 为什么"新分组的值优先"要加"等于默认值不算写过"这个前提（**真机行为，别删**）：
#: 宿主 `AstrbotConfig.check_config_integrity()` 在每次加载时把 schema 里**缺的键连默认值
#: 一起补进配置文件**并落盘。升级后的第一次加载，`robot_actions` 就是这样被整组补上默认值
#: （全 true）的——而用户真正的选择还留在旧分组里。若按字面"新分组一律优先"，宿主补的默认值
#: 会顶掉用户的选择：关掉的开关全部自己打开（静默丢配置，正是本次收敛最要避免的事）。
#: 规则与坑 33 的提示词搬迁同源：**等于内置默认值 = 视为没写过**。
#:
#: v1.7.3 曾为"`actions_risks` 同时供给三个新组"加过两条细化规则（按键分流 / 按源分组
#: 默认值判定）与写方向的 `_sync_shared_legacy_switches`。**v1.7.4 起全部删掉**：
#: `actions_risks` 不再参与归并（用户判断那些配置目前没人用，见 `docs/PORTING_NOTES.md` §37），
#: 于是每个旧组只供给**一个**目标，源键集合本来就是目标的子集、"共用源"也不存在了。
LEGACY_SECTION_MERGES: dict[str, tuple[str, ...]] = {
    'robot_actions.chat': (
        'actions_chat',
        'actions_interaction', 'actions_message', 'actions_history', 'actions_status',
        'actions_profile', 'actions_voice', 'actions_contact',
    ),
    'robot_actions.group': ('actions_group',),
    'robot_actions.qzone': ('actions_qzone',),
    # v1.7.4：输入状态从顶层组挪进「运行时」当子配置（键、默认值、hint 逐字未动）。
    'runtime.input_status': ('input_status',),
}

#: **键级搬迁**表（v1.7.5）：`目标路径 → ((目标键, 源路径, 源键), …)`。
#:
#: 与 `LEGACY_SECTION_MERGES` 的区别：那个是**整组** N:1 归并（源永远是顶层旧组名，
#: 组里每个键都往目标走）。这里搬的是"**同一个可见组**里的几个键 → 另一个组的几个键"，
#: 组级那套表达不了（源不是一整组，目标也不是一整组），所以单开一张表；规则完全同源
#: （"用户写过 = 不等于 schema 默认值"），并且**三个读取点共用它**：
#: `normalize_config`（core 侧）、`AstrbotBridge.section()`（适配层）、
#: 控制台配置页（`console_api.config_schema` 用 `LEGACY_MERGE_TARGETS` 判"这段要归并"）。
#: 一处实现、三处生效，不会出现"运行期读了、配置页没读"的漂移（坑 34）。
#:
#: v1.7.5 的第一条：语音的 `tts_provider_id` / `default_voice` 从「机器人动作 → 会话动作」
#: （`robot_actions.chat`，**可见组**）搬进「模型中心 → 语音 / 音频理解设置」
#: （`model_center.audio`）。旧位置**不能**就地删掉：宿主每次加载都按 schema 重建配置，
#: schema 里没有的键会被连值一起删（坑 22/72）——所以旧键留在 schema 里、标
#: **字段级 `invisible: true`**（宿主配置页与控制台都按这个字段隐藏，实测两者都认），
#: 值就不会丢；读取侧按本表优先读新位置、旧位置兜底，写方向只写新位置
#: （`fold_legacy_section_merges` 顺手把旧键清回默认值，避免"改了没反应"，坑 72）。
LEGACY_KEY_MERGES: dict[str, tuple[tuple[str, str, str], ...]] = {
    'model_center.audio': (
        ('tts_provider_id', 'robot_actions.chat', 'tts_provider_id'),
        ('default_voice', 'robot_actions.chat', 'default_voice'),
    ),
}

#: 语音两项的**权威分组**（点分路径）与旧位置。
VOICE_SECTION = 'model_center.audio'
VOICE_LEGACY_SECTION = 'robot_actions.chat'
VOICE_FIELD_KEYS: tuple[str, ...] = ('tts_provider_id', 'default_voice')

#: 所有"读取侧要归并"的目标路径（组级 + 键级）：控制台配置页据此判断某个字段/子组
#: 的值该从"运行期真正生效的那一份"取（`console_api.config_schema`）。
LEGACY_MERGE_TARGETS: frozenset[str] = frozenset(LEGACY_SECTION_MERGES) | frozenset(LEGACY_KEY_MERGES)

#: `plugin/_conf_schema.json` 的 `items.<键>.default` 缓存（懒加载一次，按**点分路径**）。
_SCHEMA_DEFAULTS: dict[str, dict[str, Any]] | None = None


def read_section_path(config: Any, path: str) -> Any:
    """按**点分路径**读一个嵌套配置值（读不到回 `None`）。

    `path` 既可以是顶层组名（`runtime`），也可以是嵌套段（`robot_actions.chat` /
    `runtime.input_status`）。core 侧所有"按分组名读配置"的地方都走它，别各写一套
    `dict.get` 链（漏一层就是静默读空，AGENTS 坑 66 那类事故）。
    """
    if not isinstance(config, dict):
        return None
    node: Any = config
    for step in str(path).split('.'):
        if not step:
            continue
        if not isinstance(node, dict):
            return None
        node = node.get(step)
        if node is None:
            return None
    return node


def write_section_path(config: dict[str, Any], path: str, value: Any) -> None:
    """按点分路径写一个嵌套值，**沿途浅拷贝父字典**（不改调用方手里的嵌套对象）。

    "只在顶层换引用"是折叠函数的契约（`fold_legacy_section_merges` 的调用方可能还
    拿着原始配置），写 `runtime.input_status` 这种两层路径时必须逐层复制，否则会连带
    改掉 `raw['runtime']`——那份可能是 `raw_config()` 的返回值。
    """
    parts = [step for step in str(path).split('.') if step]
    if not parts:
        return
    node = config
    for step in parts[:-1]:
        child = node.get(step)
        node[step] = dict(child) if isinstance(child, dict) else {}
        node = node[step]
    node[parts[-1]] = value


def write_merge_target(config: dict[str, Any], path: str, value: Any) -> None:
    """把归并结果写回归并目标，**顺带写它的分组别名拼写**。

    为什么不能只写一条：`model_center` 与它的别名 `model` 在 `apply_section_aliases`
    里是**同一个对象**，而 `write_section_path` 沿途浅拷贝——一旦动
    `model_center.audio`，两边的共享就断了，core 读的 `model.audio` 会停在旧值上
    （v1.7.5 搬迁语音键时实测：新位置有值、`model.audio` 是空的）。所以凡是
    "目标路径的第一段有别名"的情况，两条拼写都要写；别名那一段在配置里不存在就
    不凭空建（上游形状的配置只有 `model`，schema 形状的只有 `model_center`）。
    """
    head, _, tail = str(path).partition('.')
    alias = CONFIG_SECTION_ALIASES.get(head)
    paths = [path]
    if alias and tail and alias in config:
        paths.append('%s.%s' % (alias, tail))
    for item in paths:
        write_section_path(config, item, dict(value) if isinstance(value, dict) else value)


def _schema_default_map(schema: Any, prefix: str = '') -> dict[str, dict[str, Any]]:
    """递归收集 `路径 → {键: schema 默认值}`（只沿 `type: object` 的 `items` 往下走）。"""
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(schema, dict):
        return found
    for name, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        items = spec.get('items')
        if not isinstance(items, dict):
            continue
        path = f'{prefix}{name}'
        found[path] = {
            key: item.get('default')
            for key, item in items.items() if isinstance(item, dict)
        }
        if spec.get('type') == 'object':
            found.update(_schema_default_map(items, f'{path}.'))
    return found


def schema_group_defaults(group: str) -> dict[str, Any]:
    """某个分组的「键 → schema 默认值」（懒加载 + 缓存，读不到就回空 dict）。

    `group` 是**点分路径**：顶层组名（`runtime`）与嵌套段（`robot_actions.chat` /
    `runtime.input_status`）都认——宿主补默认值的规则就写在这份 schema 里
    （`AstrbotConfig` 用 `_config_schema_to_default_config` 读同一个文件，对
    `type: object` 的子项是**递归展开**的），所以这里也必须按路径取，拿顶层组名去比
    嵌套段的默认值会永远对不上（归并规则会因此失效）。

    唯一用途见 `merge_legacy_section_values`：区分"用户写过的值"与"宿主按 schema
    补进来的默认值"。
    """
    global _SCHEMA_DEFAULTS
    if _SCHEMA_DEFAULTS is None:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            '_conf_schema.json',
        )
        try:
            with io.open(path, encoding='utf-8-sig') as handle:
                schema = json.load(handle)
        except (OSError, ValueError):
            # 读不到 schema（打包异常等）：退回"新分组一律优先"的老行为，绝不因为
            # 读不到默认值就不归并（旧配置读不到才是更糟的那头）。
            schema = {}
        _SCHEMA_DEFAULTS = _schema_default_map(schema)
    return _SCHEMA_DEFAULTS.get(group, {})


def _legacy_value_is_written(source_defaults: dict[str, Any], key: Any, value: Any) -> bool:
    """旧分组里的这个值是不是"用户写过的"。

    判据用**源分组自己的** schema 默认值（宿主给旧组补的就是它自己的默认值）：
    键在源里也查不到默认值就当成"写过"（宁可保守地把用户的值带过来）。
    """
    if key not in source_defaults:
        return True
    return value != source_defaults[key]


def merge_legacy_section_values(
    raw: Any, name: str, values: Any = None,
) -> dict[str, Any]:
    """读一个分组：**用户写过的新分组值优先，其余从旧分组补**（`LEGACY_SECTION_MERGES`）。

    `name` 是点分路径（`robot_actions.chat` / `runtime.input_status` 这类嵌套目标也认）。
    `values` 是调用方**已经读到**的"新分组"内容（宿主 `section()` 的返回、
    上一级归并的产物、`read_section_path()` 的结果）——本函数不替调用方去
    `raw[name]` 取，各读取路径的兜底各有各的写法（见 `apply_section_aliases` /
    `AstrbotBridge.section` / `chunk12.action_group_values`）。
    返回值永远是**新 dict**，不会改到 `raw` 里任何一个分组（调用方可能还拿着它）。

    逐键规则（`defaults` = `schema_group_defaults(name)`）：

    * 新分组的值**不等于** schema 默认值 → 用户写过，用它；
    * 新分组的值**等于**默认值（含键不存在）→ 看旧分组：旧分组**写过**（不等于**它自己**的
      默认值）且值也不是目标默认值 → 用旧分组的；
    * 两边都"没写过" → 就是默认值，谁说话都一样（没写过的默认值照搬也无害，保留旧行为）。

    不是归并目标的分组原样返回 `values`（浅拷贝一份，调用方拿去随便改）。

    `LEGACY_KEY_MERGES` 的**键级搬迁**共用本函数（`name` 是键级目标路径时，
    `sources` 为空、`moves` 非空）：规则逐字相同，只是"旧位置"是**某组里的某几个键**
    （`源路径 + 源键`），而不是整组。
    """
    merged = dict(values) if isinstance(values, dict) else {}
    sources = LEGACY_SECTION_MERGES.get(name)
    moves = LEGACY_KEY_MERGES.get(name)
    if not sources and not moves:
        return merged
    if not isinstance(raw, dict):
        return merged
    defaults = schema_group_defaults(name)
    for source in sources or ():
        section = raw.get(source)
        if not isinstance(section, dict):
            continue
        source_defaults = schema_group_defaults(source)
        for key, value in section.items():
            if key not in merged:
                merged[key] = value
                continue
            # 新分组里是"没写过"的默认值、而旧分组里是用户的选择 → 旧分组说了算。
            if (key in defaults and merged[key] == defaults[key]
                    and _legacy_value_is_written(source_defaults, key, value)
                    and value != defaults[key]):
                merged[key] = value
    for target_key, source_path, source_key in moves or ():
        section = read_section_path(raw, source_path)
        if source_path in LEGACY_SECTION_MERGES:
            # 源本身可能就是**组级归并的目标**（`robot_actions.chat` 就是）：只看它自己在
            # 配置里那一份，会漏掉还留在更旧分组（`actions_voice`）里、尚未折过来的值
            # ——用户升级后"我明明配了"就会变成静默回落。
            section = merge_legacy_section_values(raw, source_path, section)
        # 源键**不存在**（或显式 null）不算"写过"：`_legacy_value_is_written` 对
        # "键不在源默认值表里"是保守判真的，这里先排除缺键，否则会把 `None` 搬过去。
        if not isinstance(section, dict) or source_key not in section:
            continue
        value = section.get(source_key)
        if value is None:
            continue
        source_defaults = schema_group_defaults(source_path)
        # 新位置写过 → 新位置说了算（本轮不动它）。
        if target_key in merged and target_key in defaults and merged[target_key] != defaults[target_key]:
            continue
        if _legacy_value_is_written(source_defaults, source_key, value) and value != defaults.get(target_key):
            merged[target_key] = value
    return merged

#: 「提示词四件套」的**权威分组**（`plugin/_conf_schema.json` 的顶层 `prompts` 组）。
#:
#: 上游把四个提示词放在 `model` 组里（`src/index.ts` 的 `ModelConfig`），本移植版
#: 给它们在配置页单开了一组，编辑起来清爽得多；core 与上游一致，读的仍然是
#: `model.main_prompt` / `format_prompt` / `fixed_prompt` / `style_prompt`。
#: 两边**键名逐字相同**，差的只是分组名，所以搬运不需要改名。
#: 见 `resolve_prompt_fields`（读）与 `to_schema_shape`（写）。
PROMPT_SECTION = 'prompts'

#: 提示词四件套的键名（上游 `mainPrompt` / `formatPrompt` / `fixedPrompt` / `stylePrompt`）。
PROMPT_FIELD_KEYS: tuple[str, ...] = (
    'main_prompt', 'format_prompt', 'fixed_prompt', 'style_prompt',
)


def resolve_prompt_fields(config: Any) -> dict[str, Any]:
    """把 `prompts` 组里的提示词搬进 core 读的 `model` 段（**读**，见 `PROMPT_SECTION`）。

    规则（逐键独立判定）：

    * `prompts.<键>` 是**非空且不等于内置默认值**的字符串 → 用它（用户在提示词页写了东西）；
    * 其余情况 → 保留 `model` / `model_center` 里的值（旧版本配置的兼容位、或用户压根没动过）。

    "等于内置默认值视为没写"这条是必须的：老配置（≤ v1.1.0）在 `model_center` 里
    也有一份四件套，默认值是同一串英文；如果只按"非空"判优，`model_center` 里
    用户改过的内容会被 `prompts` 组的默认值顶掉。

    这份函数是**唯一实现源**：`normalize_config` 收尾调用它，所以服务层、适配层、
    控制台拿到的配置里，提示词都已经在 `model` 段就位（上游键位不变）。
    """
    if not isinstance(config, dict):
        return config
    prompts = config.get(PROMPT_SECTION)
    model = config.get('model')
    if not isinstance(model, dict):
        model = config.get(CONFIG_SECTION_ALIASES_REVERSE.get('model', 'model_center'))
    if not isinstance(prompts, dict) or not isinstance(model, dict):
        return config
    defaults = CONFIG_DEFAULTS.get('model')
    defaults = defaults if isinstance(defaults, dict) else {}
    for key in PROMPT_FIELD_KEYS:
        value = prompts.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if value == defaults.get(key):
            continue
        model[key] = value
    return config


def to_schema_shape(config: Any) -> dict[str, Any]:
    """把内部（上游分组名）配置转回 `_conf_schema.json` 的分组名，**写盘用**。

    为什么必须转：AstrBot 的插件配置页是按 `_conf_schema.json` 渲染的。如果落盘的是
    上游分组名（`model` / `onebot`），配置页读不到 `model_center` / `qq_access`
    就会显示默认值——用户会以为"导入把配置弄丢了"（实际上 core 还能读，但观感是灾难）。

    读取侧**不需要**反向处理：`normalize_config` 会在加载时用
    `apply_section_aliases` 把 schema 名补成上游名，所以写 schema 形状是安全的。

    只转这两个已知别名；其余键原样保留（含未知键）。

    提示词四件套是个例外：core 读 `model.*`，用户在「提示词」组里编辑，两边同名。
    写盘时**只留 `prompts` 组那一份**——把 `model_center` 里的副本摘掉，否则
    AstrBot 会按 `_conf_schema.json` 把它当未知键删掉（日志刷 `Config key removed`），
    而且将来 schema 若再改动，两处会漂移。

    搬的是 `model` 段里的**生效值**（`normalize_config` 已经用
    `resolve_prompt_fields` 裁决过：提示词组写了就以它为准，没写就沿用模型中心的兼容位），
    所以不会丢用户内容。摘副本时**复制**分组而不是原地 `pop`：入参与出参的嵌套
    dict 目前是共享引用（浅拷贝），原地改会连带改掉调用方手里那份配置。

    v1.7.4 起收尾再跑一次 **旧分组折叠**（`fold_legacy_section_merges`）：写盘时
    把旧分组里用户写过的值折进新路径（含嵌套的 `robot_actions.chat` /
    `runtime.input_status`）、清空旧分组。这也是唯一的"迁移入口"——控制台
    改任意一项、配置导入，都会顺手把动作开关配置迁到新分组名下（见该函数的说明）。
    """
    if not isinstance(config, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in config.items():
        out[CONFIG_SECTION_ALIASES_REVERSE.get(key, key)] = value
    model_key = CONFIG_SECTION_ALIASES_REVERSE.get('model', 'model_center')
    effective = config.get('model')
    if not isinstance(effective, dict):
        effective = config.get(model_key)
    model_section = out.get(model_key)
    if isinstance(model_section, dict) and isinstance(effective, dict):
        if any(key in model_section for key in PROMPT_FIELD_KEYS):
            out[model_key] = {
                key: value for key, value in model_section.items()
                if key not in PROMPT_FIELD_KEYS
            }
        prompts = out.get(PROMPT_SECTION)
        if not isinstance(prompts, dict):
            prompts = {}
            out[PROMPT_SECTION] = prompts
        for key in PROMPT_FIELD_KEYS:
            if key in effective:
                prompts[key] = effective[key]
    return fold_legacy_section_merges(out)


def _to_snake_key(key: str) -> str:
    """`camelCase` / `PascalCase` → `snake_case`。

    已经 snake_case 或全小写的键原样返回；`_conf_schema.json` 的适配层私有键
    （`custom_*` / `extra_*` / `x_*`）原样保留——它们是 AstrBot 侧的扩展位，
    改名会让适配层读不到自己的数据。
    """
    if not isinstance(key, str) or not key:
        return key
    if key in _LITERAL_KEYS or key.startswith(_LITERAL_KEY_PREFIXES):
        return key
    out: list[str] = []
    for index, char in enumerate(key):
        if char.isupper():
            if index and (not key[index - 1].isupper() or (index + 1 < len(key) and key[index + 1].islower())):
                out.append('_')
            out.append(char.lower())
        else:
            out.append(char)
    return ''.join(out)


def snapshot_defaults() -> dict[str, Any]:
    """`CONFIG_DEFAULTS` 的**深拷贝**。

    `normalize_config` 会把默认值嵌进结果，直接返回共享对象会让调用方
    （或 `_conf_schema.json` 的 UI 层）改一处污染全局；Nest 的 `Schema.default()`
    语义也要求每次产出一份独立对象。
    """
    return copy.deepcopy(CONFIG_DEFAULTS)


def _normalize_config_value(value: Any, depth: int, key: str | None = None) -> Any:
    """递归把 dict 的键归一为 snake_case；list / 标量原样返回。

    `key` 是**父键**：当它是 `custom_extra_body` 这类「内容是模型 JSON 参数」的
    容器时，整块子树原样保留，避免把写好的 `temperature` 之外的参数名改坏。
    """
    if isinstance(value, dict):
        if key in _OPAQUE_VALUE_KEYS:
            return value
        if depth > 6:
            return value
        return {
            _to_snake_key(item_key): _normalize_config_value(item_value, depth + 1, _to_snake_key(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_normalize_config_value(item, depth + 1, None) for item in value]
    return value


def fold_legacy_section_merges(config: Any) -> dict[str, Any]:
    """把旧分组里用户写过的值**折进新分组**（**写盘目标用**，幂等）。

    目标可以是**嵌套路径**（`robot_actions.chat` / `runtime.input_status`）：写回时
    沿途浅拷贝父字典（`write_section_path`），所以调用方手里那份配置的嵌套 dict
    不会被连带改掉。

    与 `merge_legacy_section_values` 共用同一套规则，差别是这里改的是"要落盘的那份"：

    * 新分组拿到的是**生效值**（读取侧现在会给出什么，磁盘上就固化什么）；
    * 旧分组折完**清空** —— 宿主下次加载会给它们补默认值（`check_config_integrity`），
      而归并规则里"等于默认值不算写过"让新分组说了算。

    为什么非折不可：不折的话，旧分组里那个非默认的旧值会**永远**压着新分组。
    用户在新分组里把开关改回默认值（比如重新打开一个升级前关掉的语音开关）时，
    读取侧会判"新值 == 默认值 = 没写过"，于是又读回旧分组的旧值——界面上看起来
    就是"改了没反应"。折一次之后新旧两处不再打架。

    **幂等**：只有"折一下真的会改动新分组"时才折（即旧组里存在"与新组当前值不一样、
    且是用户写过"的键）；宿主每次加载都会把空掉的旧组补成默认值
    （`check_config_integrity`），那不算用户写过，于是后续启动不会反复写盘。

    v1.7.3 的"共用源不清空"分支随 `actions_risks` 退出归并一起删掉了（v1.7.4）：
    现在每个旧组只供给一个目标，清空它不会影响别的目标（见 `LEGACY_SECTION_MERGES`）。

    v1.7.5 起**先折组、再折键**：键级搬迁（`LEGACY_KEY_MERGES`）的源是
    `robot_actions.chat` 这种**可见组**，而那一组本身可能是上一轮组级归并的目标——
    顺序反了会从"还没合并的旧位置"取值（`actions_chat` 里那份）。键级折叠的写法与
    组级同源：值折进新位置、旧键**清回 schema 默认值**（不是删键——键还在 schema 里，
    删了宿主下次加载也会补回来，写默认值才让"没写过"这条判定稳定成立）。
    """
    if not isinstance(config, dict):
        return config
    for target, sources in LEGACY_SECTION_MERGES.items():
        defaults = schema_group_defaults(target)
        own = read_section_path(config, target)
        own = dict(own) if isinstance(own, dict) else {}
        pending = False
        for source in sources:
            section = config.get(source)
            if not isinstance(section, dict) or not section:
                continue
            source_defaults = schema_group_defaults(source)
            for key, value in section.items():
                if _legacy_value_is_written(source_defaults, key, value):
                    # 旧组里"用户写过"的东西：清空本身就是要做的事
                    # （否则旧值会永远压着新组）。
                    pending = True
                    break
            if pending:
                break
        if not pending:
            continue
        write_merge_target(config, target, merge_legacy_section_values(config, target, own))
        for source in sources:
            section = config.get(source)
            if isinstance(section, dict) and section:
                # 值已经（按规则）折进新分组了，旧分组留着只会继续产生歧义。
                config[source] = {}
    for target, moves in LEGACY_KEY_MERGES.items():
        own = read_section_path(config, target)
        own = dict(own) if isinstance(own, dict) else {}
        pending = False
        for _target_key, source_path, source_key in moves:
            section = read_section_path(config, source_path)
            if not isinstance(section, dict) or source_key not in section:
                continue
            if section.get(source_key) is None:
                continue
            if _legacy_value_is_written(
                schema_group_defaults(source_path), source_key, section.get(source_key),
            ):
                pending = True
                break
        if not pending:
            continue
        write_merge_target(config, target, merge_legacy_section_values(config, target, own))
        # 旧键清回默认值（沿途浅拷贝：不改调用方手里那份嵌套 dict）。
        for _target_key, source_path, source_key in moves:
            section = read_section_path(config, source_path)
            if not isinstance(section, dict) or source_key not in section:
                continue
            default = schema_group_defaults(source_path).get(source_key)
            if section.get(source_key) == default:
                continue
            patched = dict(section)
            patched[source_key] = default
            write_section_path(config, source_path, patched)
    return config


def apply_section_aliases(raw: Any) -> dict[str, Any]:
    """把 AstrBot schema 的顶层分组别名补成上游分组名（本模块是唯一实现源）。

    `plugin/_conf_schema.json` 面向用户，分组叫 `model_center` / `qq_access`
    （比 `model` / `onebot` 直观）；但本模块的 `CONFIG_DEFAULTS`、`ServiceBase`
    的 `cachedXxxConfig` 与 `_config_section(config, 'model' | 'onebot')` 一律按
    **上游名**读取。缺了这层别名，用户在「模型中心 / QQ 接入」页填的连接、
    API Key 与白名单会被静默忽略。

    规则（`CONFIG_SECTION_ALIASES`）：

    * 只在**目标键缺失**时补一份别名键；
    * 原键**保留**，所以 `normalize_config` 仍然"不丢未知键"，schema 将来改回
      上游名也不会跟残留的别名打架；
    * `raw` 非 dict 时返回空 dict（调用方自行兜底）。

    最后再跑一遍 **N:1 归并**（`LEGACY_SECTION_MERGES`）：把旧动作分组里剩下的键
    补进新路径（`actions_interaction` … → `robot_actions.chat`；
    `actions_chat` / `actions_group` / `actions_qzone` → 同名子组；
    `input_status` → `runtime.input_status`）。目标可以是嵌套路径，沿途浅拷贝父字典
    （`write_section_path`），所以 `raw` 里已有的 `runtime` 不会被原地改。
    旧分组本身**不动**，所以"未知键不丢"照旧；新路径只在有东西可补时才建出来。

    组级归并之后是 **键级搬迁**（`LEGACY_KEY_MERGES`，v1.7.5：语音两项 →
    `model_center.audio`）。顺序不能反：键级的源（`robot_actions.chat`）本身可能是
    组级归并的目标，得先让组级那一步把它填好。此刻 `model_center` 与它的别名 `model`
    还是**同一个 dict 对象**，所以补一次两处都到位（之后 `merge()` 才会拆成两份）。
    适配层（`plugin/adapters/astrbot_bridge.py`）复用本函数，不另抄一份表。
    """
    if not isinstance(raw, dict):
        return {}
    source = dict(raw)
    for table in (CONFIG_SECTION_ALIASES, LEGACY_SECTION_ALIASES):
        for alias, target in table.items():
            if alias in source and target not in source:
                source[target] = source[alias]
    for target in LEGACY_SECTION_MERGES:
        merged = merge_legacy_section_values(source, target, read_section_path(source, target))
        if merged:
            write_merge_target(source, target, merged)
    for target in LEGACY_KEY_MERGES:
        merged = merge_legacy_section_values(source, target, read_section_path(source, target))
        if merged:
            write_merge_target(source, target, merged)
    return source


def normalize_config(raw: Any) -> dict[str, Any]:
    """递归把 camelCase 键归一为 snake_case，并按上游默认值补齐缺失项。

    AstrBot 适配层把 `context` 传来的配置过一遍这个函数，新旧两种拼写的配置
    就都能读：上游 Koishi Console 的 YAML 是 camelCase，本插件
    `plugin/_conf_schema.json` 是 snake_case。

    签名：`normalize_config(raw: Any) -> dict[str, Any]`

    * **分组别名**：AstrBot schema 的两个顶层分组名与上游不同
      （`model_center` / `qq_access` ↔ 上游 `model` / `onebot`），见
      `apply_section_aliases` / `CONFIG_SECTION_ALIASES`。别名只在目标键缺失时
      补一份，原键保留，因此"未知键不丢"的性质不变。
    * `raw` 非 dict（None / 字符串 / 列表）→ 只返回一份默认值深拷贝。
    * 显式给出的值**优先于**默认值；`None` 也视为「显式给出」以外的空值，
      此时沿用默认值（与 AstrBot schema 未填即用 default 的行为一致）。
    * 未知键（上游有、本移植版未声明）原样保留，不静默丢弃用户配置。
    * list 里的 dict 同样归一（`onebot.user_accounts` / `group_chats` 等表格）。
    * **提示词四件套**：`prompts` 组里写了东西（非空且非默认值）就以它为准，搬进
      core 读的 `model` 段；没写就保留 `model` / `model_center` 的兼容位值。
      见 `resolve_prompt_fields`。
    * 默认值补全清单见 `CONFIG_DEFAULTS`：顶层分组全部补齐，
      `runtime` / `blind_mode` / `logging` / `memory` / `browser` /
      `chat_actions` / `stickers` / `shared_story` / `onebot` /
      `timeline_director` / `agency` / `schedule_preplan` / `story_defaults`
      的标量字段一并补齐（取值逐字取自上游 `src/index.ts` 的 Console 默认值）。
    """
    defaults = snapshot_defaults()
    if not isinstance(raw, dict):
        return defaults

    def merge(default_value: Any, value: Any, parent_key: str | None = None) -> Any:
        if isinstance(value, dict):
            # 内容是模型 / API 的 JSON 参数：键名原样保留，不做递归改名。
            if parent_key in _OPAQUE_VALUE_KEYS:
                return value
            normalized_items = [
                (_to_snake_key(key), merge(
                    default_value.get(_to_snake_key(key)) if isinstance(default_value, dict) else None,
                    item, _to_snake_key(key),
                ))
                for key, item in value.items()
            ]
            merged = dict(default_value) if isinstance(default_value, dict) else {}
            merged.update(normalized_items)
            return merged
        if isinstance(value, list):
            # 表格式值（providers / group_chats / rest_windows …）整体替换，
            # 但列表里的 dict 仍要归一。
            return [merge(None, item) for item in value]
        return value

    return resolve_prompt_fields(merge(defaults, apply_section_aliases(raw)))


def resolve_shared_story_config(value: Any = None) -> dict[str, Any]:
    """上游 `get sharedStoryConfig`（`service.ts:5570`）：共享主剧本的归一配置。

    ⚠️ **`enabled` 硬编码为 `True`**，这是上游 1.0.1-beta6-rebuild 的刻意选择：

    ```ts
    const { enabled: _legacyEnabled, ...overrides } = this.config.sharedStory ?? {}
    // Beta2 deliberately keeps the single-story guard hard-enabled. Older
    // builds exposed a rollback switch here, but turning it off could create
    // fresh per-account stories that a later background sweep would revive.
    ```

    也就是说：**一个角色只有一条时间线**，所有私聊的人共用同一部剧本，各自是
    这部剧本里的一条"关系分支"（participant）；旧版"每个 QQ 一部剧本"的行为
    由 `migrate_legacy_story` / `migrate_legacy_branch_into_shared` 惰性合并进来。
    配置文件里写 `enabled: false` **同样不生效**（`_legacyEnabled` 被丢弃），
    这与上游逐字一致——别把这个键实现成可关，否则会退回"每人一部剧本"，
    也就是本移植版 v1.2.6 及更早的实际行为（用户报的"怎么不是所有人共用一个剧本"）。

    输出键名 snake_case（本移植版配置层裁决），读取侧兼容上游 camelCase；
    `enabled` 之外的字段缺省值逐字取自上游该 getter。
    """
    config = value if isinstance(value, dict) else {}

    def flag(name: str, default: bool) -> bool:
        raw = config.get(name)
        if raw is None:
            raw = config.get(_to_snake_key(name))
        return default if raw is None else bool(raw)

    def number(name: str, default: int) -> int:
        raw = config.get(name)
        if raw is None:
            raw = config.get(_to_snake_key(name))
        if raw is None:
            return default
        try:
            return max(0, int(float(raw)))
        except (TypeError, ValueError):
            return default

    presets = config.get('participant_presets')
    if presets is None:
        presets = config.get('participantPresets')
    managers = config.get('manager_accounts')
    if managers is None:
        managers = config.get('managerAccounts')
    return {
        # 见上：上游把回滚开关丢掉，这里也不能让它变成可关。
        'enabled': True,
        'auto_enroll_participants': flag('autoEnrollParticipants', True),
        'allow_cross_conversation_messages': flag('allowCrossConversationMessages', True),
        'share_participant_details': flag('shareParticipantDetails', False),
        'max_cross_conversation_actions': number('maxCrossConversationActions', 1),
        'participant_context_limit': number('participantContextLimit', 6),
        'manager_accounts': list(managers) if isinstance(managers, list) else [],
        'participant_presets': list(presets) if isinstance(presets, list) else [],
    }


def resolve_blind_mode_config(value: Any = None) -> dict[str, Any]:
    """上游 `resolveBlindModeConfig`（`service.ts:7715`）：盲区模式的归一配置。

    上游 `Math.max(1, Math.min(1_440, Math.floor(value?.healthReportMinutes ?? 10)))`：
    缺省与非法值都按 10 分钟，再夹到 [1, 1440]。

    输出键名 snake_case（本移植版的配置层裁决）；读取侧同时接受上游 camelCase
    键名 `healthReportMinutes`，因此旧 YAML 与旧调用方都能直接用。
    """
    config = value if isinstance(value, dict) else {}
    raw_minutes = config.get('health_report_minutes')
    if raw_minutes is None:
        raw_minutes = config.get('healthReportMinutes')
    if raw_minutes is None:
        raw_minutes = 10
    try:
        minutes = float(raw_minutes)
    except (TypeError, ValueError):
        minutes = float('nan')
    if minutes != minutes:  # NaN
        minutes = 10.0
    return {
        'enabled': config.get('enabled') is True,
        'health_report_minutes': max(1, min(1_440, math.floor(minutes))),
    }


#: 上游 `export const resolveBlackBoxConfig = resolveBlindModeConfig`（@deprecated 别名）。
resolve_black_box_config = resolve_blind_mode_config

# =========================================================================== #
# `normalize_config` 的默认值表
# =========================================================================== #
#
# 取值全部逐字取自上游 `src/index.ts` 的 Console 默认值（`Schema.*.default(...)`
# 与其 `default({...})` 内联对象），逐组标注上游行号便于审计。
# 只在本移植版仍会读取的字段上补默认值；未列出的字段保持「未提供」缺失。

CONFIG_DEFAULTS: dict[str, Any] = {
    # ---- 【必填 1】storyDefaults（index.ts StoryDefaults） ----
    'story_defaults': {
        'character_name': 'Unnamed character',
        'character_profile': '',
        'perspective': '',
        'user_profile': '',
        'relationship': '',
        'world': '',
        'supporting_cast': '',
        'location': '',
        'style': 'Realistic, restrained, and centered on ordinary life.',
        'timezone': 'Asia/Shanghai',
        # AstrBot 适配层扩展位（上游没有）。
        'persona_id': '',
        'extra_setting': '',
    },
    # ---- 【必填 2】model（index.ts Model） ----
    'model': {
        'vision': {'enabled': False, 'mode': 'native', 'detail': 'auto', 'max_image_dimension': 1024},
        'audio': {
            'enabled': False, 'out_format': 'mp3', 'max_file_size_mb': 10, 'max_per_message': 1,
            # v1.7.7：文字转语音总开关（正文 `<tts/>` 标记 + `send_voice` 动作共用）。
            # 默认 True = 保持今天的行为（发语音这条动作从 v1.7.2 起就是可用的）。
            # 注意它**不进** `resolve_audio_config` 的输出形状：那个 dict 是上游
            # `audioConfig` 的逐字对应物，多一个键会让 `audio_config` 的相等断言漂移；
            # 读它的是 `ServiceBase.voice_reply_enabled`。
            'tts_enabled': True,
        },
        'providers': [],
        'main_temperature': 0.8,
        'main_top_p': 1.0,
        'main_max_tokens': 4096,
        'main_timeout': 60_000,
        'main_response_format': 'json-object',
        'main_streaming_mode': 'off',
        'main_payload_order': 'legacy',
        'failover': {
            'enabled': True, 'strategy': 'priority',
            'max_attempts_per_provider': 1, 'cooldown_minutes': 5,
        },
        'main_prompt': (
            'Continue the character-centered life script with grounded actions, motives, '
            'relationships, and ordinary time passing.'
        ),
        'format_prompt': '',
        'fixed_prompt': '',
        'style_prompt': (
            'Use restrained, realistic prose with concrete daily details, natural pauses, '
            'and no forced drama.'
        ),
        'embedding': {
            'enabled': False, 'semantic_history': False, 'live_query': False,
            'endpoint': '', 'dimensions': 0, 'timeout': 10_000,
            'max_input_characters': 4_000, 'backfill_batch_size': 5,
            'semantic_sticker_filter': True,
        },
        'compaction': {
            'enabled': True, 'temperature': 0.3, 'top_p': 1.0, 'max_tokens': 2048,
            'timeout': 60_000, 'response_format': 'json-object',
            'main_prompt': (
                'Compress completed scenes into concise continuity notes while preserving '
                'causality, promises, unresolved matters, and gradual character change.'
            ),
            'fixed_prompt': '',
            'style_prompt': 'Concise, factual, chronological, and concrete.',
        },
        # v1.4.0：模型调用治理（`docs/MEMORY_MAINTENANCE.md` §5.6）。默认关闭。
        'governor_enabled': False,
        'governor_max_concurrency': 4,
        'governor_max_requests_per_minute': 0,
        'governor_min_call_interval_ms': 0,
        'governor_breaker_failures': 5,
        'governor_breaker_cooldown_seconds': 60,
    },
    # ---- 【必填 3】onebot（index.ts OneBot） ----
    'onebot': {
        'enabled': False,
        'bot_accounts': [],
        'user_accounts': [],
        'group_chats': [],
        'ignore_self_messages': True,
    },
    # ---- 【结构 4】sharedStory（index.ts SharedStory） ----
    'shared_story': {
        'auto_enroll_participants': True,
        'allow_cross_conversation_messages': True,
        'share_participant_details': False,
        'max_cross_conversation_actions': 1,
        'participant_context_limit': 6,
        'manager_accounts': [],
    },
    # ---- 【节奏 5】runtime（index.ts Runtime） ----
    'runtime': {
        'split_reply_messages': True,
        'message_separator': '<sep/>',
        'typing_base_delay_seconds': 1,
        'typing_characters_per_second': 8,
        'typing_max_delay_seconds': 12,
        'typing_jitter_ratio': 0.3,
        'user_message_debounce_seconds': 2,
        'narrative_retry_delay_seconds': 60,
        'narrative_retry_max_attempts': 6,
        'capture_direct_messages': True,
        'auto_create': False,
        'ignore_command_messages': True,
        'allow_proactive_messages': False,
        'proactive_willingness_threshold': 0.65,
        'sweep_interval_minutes': 5,
        'minimum_advance_minutes': 30,
        'max_stories_per_sweep': 20,
        'context_entry_limit': 50,
        'context_time_window_minutes': 60,
        'memory_limit': 20,
        'max_script_characters': 8_000,
        'max_message_characters': 2_000,
        'minimum_delayed_reply_seconds': 10,
        'maximum_delayed_reply_minutes': 1_440,
        'cancel_delayed_replies_on_user_message': True,
        'auto_advance_enabled': True,
        'auto_advance_interval_minutes': 40,
        'auto_advance_jitter_minutes': 5,
        'conversation_follow_up_minutes': [10, 20],
        'conversation_follow_up_jitter_minutes': 1,
        'rest_windows': [{
            'enabled': True, 'label': 'night sleep', 'start': '23:00', 'end': '07:00',
            'min_interval_minutes': 120, 'max_interval_minutes': 240,
        }],
    },
    # ---- 【节奏 6】urge（index.ts urge） ----
    'urge': {
        'enabled': False,
        'frequency': 'medium',
        'proactive_willingness_threshold': 0.4,
    },
    # ---- 【节奏 7】schedulePreplan（index.ts SchedulePreplan） ----
    'schedule_preplan': {
        'enabled': True,
        'horizon_days': 14,
        'variation_level': 'stable',
        'candidate_activation_probability': 0.25,
        'candidate_reveal_minutes': 120,
        'review_after_local_hour': 3,
        'anchor_auto_advance': True,
    },
    # ---- 【节奏 8】timelineDirector（index.ts timelineDirector 内联对象） ----
    'timeline_director': {'enabled': True},
    # ---- 【节奏 9】agency（index.ts Agency） ----
    'agency': {
        'enabled': True,
        'max_window_minutes': 240,
        'minimum_proactive_interval_minutes': 60,
        'max_candidate_hours': 24,
    },
    # ---- 【表达 10】chatActions（index.ts ChatActions） ----
    'chat_actions': {
        'enabled': False,
        'platforms': ['qq'],
        'quote_reply': True,
        'message_reactions': True,
        'allowed_reactions': ['like', 'smile', 'laugh', 'heart'],
        'native_faces': True,
        'expression_threshold': 0.7,
        'allowed_native_faces': ['smile', 'laugh', 'sweat', 'awkward'],
    },
    # ---- 【表达 11】stickers（index.ts Stickers） ----
    'stickers': {
        'enabled': False,
        'directory': 'data/hds-interlude/stickers',
        'max_file_size_mb': 10,
        'catalog_limit': 40,
        'description_max_tokens': 768,
        'description_response_format': 'json-object',
    },
    # ---- 【内在 12】memory（index.ts Memory） ----
    'memory': {
        'enabled': True,
        'background_interval_minutes': 10,
        'max_stories_per_compaction_run': 20,
        'scene_entry_threshold': 16,
        'scene_character_threshold': 10_000,
        'compaction_entry_limit': 80,
        'compaction_character_limit': 32_000,
        'scene_hook_characters': 2_000,
        'scene_summary_characters': 8_000,
        'arc_summary_characters': 12_000,
        'previous_scene_summaries': 2,
        'recent_entry_limit': 30,
        'fact_limit': 20,
        'fact_content_characters': 4_000,
        'fact_importance_weight': 0.5,
        'fact_confidence_weight': 0.35,
        'fact_recency_weight': 0.15,
        'semantic_weight': 0.55,
        'unresolved_weight': 0.2,
        'state_patch_confidence_threshold': 0.82,
        'major_state_patch_confidence_threshold': 0.95,
        'state_patch_min_evidence': 3,
        'state_patch_min_turns': 3,
        'state_patch_min_days': 2,
        'state_patch_cooldown_hours': 72,
        'auto_apply_state_patches': True,
        'allow_major_state_changes': True,
        'max_facts_per_story': 200,
        'active_consequences_enabled': True,
        'active_consequence_prompt_limit': 6,
        'active_consequence_max_days': 7,
        'active_consequence_default_strength': 0.55,
        'overlay_compression_enabled': True,
        'overlay_recent_days': 2,
        'overlay_monthly_after_days': 10,
        'overlay_weekly_window_days': 5,
        'overlay_monthly_window_days': 10,
        'overlay_weekly_summary_characters': 1_600,
        'overlay_monthly_summary_characters': 3_200,
        'facts_dedupe_enabled': True,
        'facts_contradiction_enabled': True,
        'temporal_anchor_enabled': True,
        'forgetting_enabled': False,
        'forgetting_threshold': 0.25,
        'forgetting_retention_days': 14,
        'forgetting_half_life_days': 30,
        'maintenance_max_llm_calls': 12,
        'maintenance_max_runtime_minutes': 10,
        'maintenance_min_call_interval_ms': 500,
        'hybrid_retrieval_enabled': True,
        'hybrid_rrf_k': 60,
        'query_rewrite_enabled': True,
        'context_metrics_enabled': True,
    },
    # ---- 【内在 13】alterSystem（index.ts AlterSystem） ----
    'alter_system': {
        'enabled': True,
        'base_threshold': 10,
        'density_factor': 0.3,
        'same_direction_boost': 0.05,
        'opposite_decay': 0.15,
        'min_weight': 0.2,
        'max_intensity': 2,
        'temperature': 0.3,
        'top_p': 1,
        'max_tokens': 400,
        'timeout': 30_000,
        'prompt': '',
    },
    # ---- 【扩展 14】browser（index.ts Browser） ----
    'browser': {
        'enabled': False,
        'mode': 'deferred-only',
        'allow_search': True,
        'allow_visit': True,
        'search_url_template': 'https://html.duckduckgo.com/html/?q={query}',
        'allowed_domains': [],
        'blocked_domains': [],
        'max_concurrent_pages': 1,
        'max_research_per_sweep': 1,
        'navigation_timeout': 15_000,
        'wait_until': 'domcontentloaded',
        'max_text_characters': 12_000,
        'max_excerpt_characters': 3_000,
        'max_observations_in_prompt': 4,
        'cache_minutes': 30,
        'allow_group_triggered_research': False,
        'log_observation_preview': False,
    },
    # ---- 【维护 15】blindMode（index.ts BlindMode） ----
    'blind_mode': {
        'enabled': False,
        'health_report_minutes': 10,
    },
    # ---- 【维护 16】logging（index.ts Logging） ----
    'logging': {
        'level': 'info',
        'verbosity': 'standard',
        'format': 'layered',
        'colors': True,
        'color_theme': 'dark',
        'kaomoji': True,
        'log_script_preview': False,
        'log_message_content': False,
        'preview_length': 500,
    },
}
