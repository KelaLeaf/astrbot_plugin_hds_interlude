"""Chunk2 mixin：`upstream/src/service.ts:1929-2572` 的全部成员。

成员清单（声明起始行，与上游顺序一致）：

===================================  ==================================================
上游行号                              成员
===================================  ==================================================
1929                                 `groupMessages`
1953                                 `groupCooldownActive`
1963                                 `groupChatCapabilities`
1977                                 `privateChatCapabilities`
1985                                 `executeGroupReactions`
2035                                 `resolveSticker`
2041                                 `get expressionThreshold`
2045                                 `resolveNativeFace`
2059                                 `sendSticker`
2107                                 `sendNativeFace`
2147                                 `sendGroupMessage`
2189                                 `bufferUserNarrative`
2209                                 `signalIncomingInterruption`
2221                                 `deliverEarlyPrivateReply`
2247                                 `get audioConfig`
2260                                 `get stickerConfig`
2276                                 `describeUserEvent`
2303                                 `scanStickerLibrary`
2386                                 `refreshStickerCatalog`
2392                                 `semanticStickerEmbeddingEnabled`
2398                                 `backfillStickerEmbeddings`
2410                                 `stickerCatalogForSession`
2422                                 `rankStickerAssets`
2429                                 `semanticTurnEmbeddingEnabled`
2440                                 `previousSceneSummaries`
2457                                 `pruneWorkingDetails`
2466                                 `recallHistory`
2539                                 `ensureHistoryVectors`
===================================  ==================================================

主题：群消息读取与群/私聊能力声明、聊天动作投递（表情包 / 原生表情 / 群消息）、
入站消息缓冲与实验性流式早发、贴纸库扫描与向量回填、历史语义召回（`recallHistory`
+ `ensureHistoryVectors`）。

键名约定（键名约定，本文件里逐处落实）：

* **数据库行**：上游 camelCase（`assetId` / `filePath` / `occurredAt` / `metadata`…）。
  `base.db_get()` 经 `helpers.normalize_database_row()` 后**保持 camelCase**，因此
  凡是读写库的行一律按 camelCase 取值；写库的 patch 也用列名（wire format）。
* **内部结构**：snake_case（`GroupMessageContext` / `RecallMoment` /
  `OutgoingMessageDraft`，与 `types.py` 一致）。
* **喂给模型的 payload**：上游 camelCase 逐字保持 ——
  `stickerCatalogForSession()` 的 `assetId`（`systemPrompt` 明文要求模型回
  `localMedia.assetId`）、`describeUserEvent().quote`（`helpers.describe_quoted_message`
  的输出）都属于这一类。
* **从外部读入**（模型输出 / 配置 / 旧数据）：一律 `pick()` 双读，优先 camelCase。

跨 mixin 调用（`self.其他方法()`，MRO 解析，见分解契约 §4）：`append_entry`、
`embed_text`、`send_outgoing_messages`、`confirm_outgoing_deliveries`、
`split_outgoing_message`、`record_platform_delivery_outcome`、
`update_script_delivery_outcome`、`describe_vision_event`、`image_bytes_to_native`。

**注意**：`audioConfig` / `stickerConfig` 两个 getter 在本移植版里同时被
`base.py` 以「通用配置缓存」形式占位（`_config_cache('cached_audio_config',
'resolve_audio_config', 'audio')`）。`config.py` 最终并没有落地 `resolve_audio_config`
/ `resolve_sticker_config`，base 的占位因此会**原样返回未归一化的配置段**，而
上游 getter 里带着真实的夹取/兜底逻辑（`outFormat` 白名单、`maxFileSizeMB` 夹到
[1,25]、`catalogLimit` 夹到 [1,80]、`descriptionMaxTokens` 夹到 [256,4096]…）。
MRO 里 `ServiceChunk2` 排在 `ServiceBase` 之前，故本文件按上游逐字实现这两个
getter，正好覆盖那条未归一化的占位路径。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from typing import Any, Optional

from ..bubbles import runtime_bubble_segments
from ..narrator_prompts import prompt_visible_message_content, recent_script_ownership
from ..script.episode_index import (
    build_episode_index,
    episode_excerpt,
    episode_tag_score,
    grounded_episode_tags,
)
from ..script.recall_navigation import index_original, recall_keys, score_original
from ..time import dt_ms, iso, parse_dt, utc_now
from .base import (
    ServiceBase,
    _config_section,
    _config_value,
    is_one_bot_platform,
    normalize_group_id,
    pick,
)
from .config import (
    RECALLABLE_ENTRY_KINDS,
    STICKER_DESCRIPTION_RETRY_COOLDOWN,
    is_history_entry_visible_to_participant,
    should_request_turn_embedding,
)
from .helpers import (
    COLLECTED_STICKER_DIR,
    GUESS_STICKER_KIND,
    SEMANTIC_STICKER_LIMIT,
    STICKER_FILE_SUFFIX,
    _turn_get,
    _turn_set,
    calibrated_native_face_willingness,
    clip,
    collected_sticker_asset_id,
    collectible_sticker_kind,
    cosine_similarity,
    describe_quoted_message,
    extract_session_audio_sources,
    extract_session_file_facts,
    format_group_speaker,
    guess_image_dimensions,
    normalize_allowed_native_faces,
    normalize_allowed_reactions,
    normalize_expression_threshold,
    normalize_quoted_message_context,
    rank_sticker_catalog,
    should_supersede_narrative_request,
    stable_sticker_asset_id,
    sticker_guess_candidate,
    sticker_guess_result,
    verify_sticker_image_bytes,
)
#: 上游 `normalizeVisibleMessageContent`（`src/service.ts:7649`，模块级导出函数）。
#: `helpers.py` 把它实现成下划线私有（同文件里由 `normalizeGroupVisibleReply` 使用），
#: 但 Chunk2 的流式早发路径同样需要它，这里按上游语义直接复用同一实现，避免两份漂移。
from .helpers import _normalize_visible_message_content as normalize_visible_message_content
from .transport import NullTransport, voice_kwargs

__all__ = [
    'ServiceChunk2',
    'STICKER_COLLECT_WARN_INTERVAL_MS',
    'STICKER_GUESS_MAX_PER_MESSAGE',
    'STICKER_GUESS_MAX_PER_MINUTE',
    'STICKER_GUESS_TIMEOUT_SECONDS',
    'STICKER_GUESS_WINDOW_MS',
    'group_message_ref',
    'sticker_mime',
    'targetable_message_id',
]

#: 同一个「拿不到字节」的能力缺失告警最短间隔（毫秒）。一次刷屏的消息里可能有十个表情包，
#: 每个都打一条 warn 等于把日志淹掉；节流到一条，用户仍然看得见"这条路走不通"。
STICKER_COLLECT_WARN_INTERVAL_MS = 10 * 60 * 1000

#: 入站表情包下载的软时限（秒）。上游 `fetchNativeAudio` 的 `withTimeout(..., 30_000)`
#: 是同类操作的口径；收藏是**旁路**，卡住一次网络不该拖住叙事回合。
STICKER_FETCH_TIMEOUT_SECONDS = 30.0

#: —— 第二层（模型判定普通图片）的节流常量（都是**防刷的启发式**，不是平台规则）——
#:
#: 一条消息里最多问几次：多张图的刷屏消息不该按图数线性烧 token，剩下的一律 debug 丢弃。
STICKER_GUESS_MAX_PER_MESSAGE = 2
#: 滑动窗口内最多问几次（跨消息、跨会话）：一次刷图潮不能变成 token 黑洞。
STICKER_GUESS_MAX_PER_MINUTE = 6
#: 上面那个窗口的长度（毫秒）。
STICKER_GUESS_WINDOW_MS = 60 * 1000
#: 一次判定的软时限（秒）：判定同样是**旁路**，卡住的请求不许拖着任务不放
#: （连接自己的 `timeout` 之外再加一道硬上限）。
STICKER_GUESS_TIMEOUT_SECONDS = 60.0


def _local_sticker_path(value: Any) -> str:
    """把适配器给的本地图片引用归一成文件系统路径（`file:///x` → `/x`）。

    与 `chunk3._local_image_path` 同一套规则；复制在这里而不是 import，
    是因为它只在这条冷路径上用，而且 `chunk2` 不依赖 `chunk3`（两个 mixin 平级）。
    """
    text = str(value if value is not None else '').strip()
    if text.lower().startswith('file://'):
        text = text[len('file://'):]
        if not text.startswith('/'):
            slash = text.find('/')
            text = text[slash:] if slash >= 0 else ''
        try:
            from urllib.parse import unquote  # noqa: PLC0415

            text = unquote(text)
        except Exception:  # noqa: BLE001 - 解不开就按原文用
            pass
    return text


def _read_local_bytes(path: str) -> bytes:
    """读一个本地文件（`asyncio.to_thread` 的落地实现；失败由调用方 catch）。"""
    with open(path, 'rb') as handle:
        return handle.read()
def _incoming_sticker_kinds(
    media: Any,
    sticker_rows: Any,
    media_by_source: Any = None,
) -> dict[str, str]:
    """入站附件的 `来源 → 种类` 查找表（三份来源合并，先到先得）。

    * `media`：`extract_session_media` 抽出来的 `[{source, kind, summary, label}]`；
    * `media_by_source`：调用方按来源去重后的同一张表（私聊缓冲回合已经做过一次）；
    * `sticker_rows`：`sticker_catalog_for_session()` 的输出（camelCase `assetId`）——
      上游 wire format 里它就是"本地库素材标识"，适配层可能直接把它当图片来源给出来。

    合并顺序固定（media → media_by_source → sticker_rows）：**先拿到的为准**，
    后面只补空位。同一个来源出现两种说法时不猜，按第一条落定。
    """
    kinds: dict[str, str] = {}
    for item in (media or []):
        if not isinstance(item, dict):
            continue
        source = str(item.get('source') if item.get('source') is not None else '').strip()
        if source:
            kinds.setdefault(source, _text_kind(item.get('kind')))
    if isinstance(media_by_source, dict):
        for source, item in media_by_source.items():
            key = str(source if source is not None else '').strip()
            if key and isinstance(item, dict):
                kinds.setdefault(key, _text_kind(item.get('kind')))
    for item in (sticker_rows or []):
        if not isinstance(item, dict):
            continue
        asset_id = pick(item, 'assetId', 'asset_id')
        if asset_id:
            kinds.setdefault(str(asset_id).strip(), _text_kind(pick(item, 'kind')))
    return kinds


def _text_kind(value: Any) -> str:
    """种类的原样文本（不做白名单判断——白名单在 `collectible_sticker_kind` 一处）。"""
    return str(value if value is not None else '').strip().lower()


def _has_collectible_media(media: Any, guess_enabled: bool = False) -> bool:
    """这条消息里有没有**值得为它建一个收藏任务**的附件（同步的廉价预筛）。

    只决定"要不要建任务"，真正的判据仍在 `collectible_sticker_kind` 一处——
    这里放宽（认得 `kind` 键就算）不会让不该收的进来，只会让少数消息多做一次空跑。

    `guess_enabled`（`stickers.auto_collect_guess`）打开时，**普通图片**也算"值得建任务"：
    第二层判据（模型判定）只在 `kind == 'image'` 上有意义，而"这条消息里有没有普通图片"
    只有这里能同步判出来（下载、解析图片头、调模型都在任务里做）。开关关着时行为与今天
    逐字一致——纯文字 / 只有普通照片的消息**一个任务都不建**。
    """
    for item in (media or []):
        if not isinstance(item, dict):
            continue
        if collectible_sticker_kind(item.get('kind')):
            return True
        if guess_enabled and _text_kind(item.get('kind')) == GUESS_STICKER_KIND:
            return True
    return False


def _media_sources(media: Any) -> list[str]:
    """`media` 里**带来源**的那几条 → 去重后的来源表（顺序保持）。

    群聊的收藏钩子用它当 `collect_incoming_stickers(media, sources)` 的来源表：
    `media` 与 `image_sources` 本来就是同一条链路（适配层写下的结构化媒体表里
    `source` 与 `extract_session_image_sources` 读出来的那个字符串逐字一致，§46），
    再抽一次来源只会多一处对齐点。没有来源的条目（小程序卡片）本来也收不了。
    """
    sources: list[str] = []
    for item in (media or []):
        if not isinstance(item, dict):
            continue
        source = str(item.get('source') if item.get('source') is not None else '').strip()
        if source and source not in sources:
            sources.append(source)
    return sources


def _collected_sticker_name(asset: Any) -> str:
    """给收藏进来的素材起一个**稳定**的短名（数据集前置词 + 哈希前缀）。

    名字是给人看的：控制台列表里 `sticker-collected-a1b2c3…` 比一串 URL 强。
    用户随时能在控制台改成"坏笑的猫"——那时 `name` 已经存在，这里不再覆盖。
    """
    digest = re.sub(r'[^a-fA-F0-9]', '', str(pick(asset, 'hash') or ''))[:8].lower() or 'unknown'
    return 'collected-%s' % digest


# =========================================================================== #
# 本文件需要的模块级纯函数
# =========================================================================== #

def _media_phrase(media: list[dict[str, Any]]) -> str:
    """把媒体种类折成一句人话（`1 个表情包` / `2 张图片、1 个表情包`）。"""
    counts: dict[str, int] = {}
    for item in media or []:
        label = pick(item, 'label') or '[图片]'
        counts[label] = counts.get(label, 0) + 1
    return '、'.join('%d %s' % (count, label) for label, count in counts.items())


def targetable_message_id(value: Any) -> Optional[str]:
    """上游 `targetableMessageId`（`src/service.ts:7300`）。

    ``/^-?\\d+$/.test(id) && id !== '0'`` —— 只有可寻址的十进制消息 id 才算数。
    """
    text = str('' if value is None else value).strip()
    if not re.fullmatch(r'-?\d+', text):
        return None
    return text if text != '0' else None


def group_message_ref(entry_id: Any) -> str:
    """上游 `groupMessageRef`（`src/service.ts:7305`）：`msg-<非负整数>`。"""
    number = entry_id if isinstance(entry_id, int) and not isinstance(entry_id, bool) else 0
    return 'msg-%d' % max(0, math.floor(number))


def sticker_mime(file_path: Any) -> str:
    """上游 `stickerMime`（`src/service.ts:7325`）：按扩展名给出图片 MIME。"""
    extension = os.path.splitext(str(file_path or ''))[1].lower()
    if extension == '.gif':
        return 'image/gif'
    if extension == '.webp':
        return 'image/webp'
    if extension in ('.jpg', '.jpeg'):
        return 'image/jpeg'
    return 'image/png'


def _provider_available(provider: Any) -> bool:
    """上游 `this.stickerDescriber.available()` 的安全等价物。

    `narrator.py` 由并行任务产出，缺失时 `ServiceBase.__init__` 把
    `sticker_describer` 留成 `None`（上游「视觉模型未配置」的降级态）。
    上游在这一步不做判空，本移植版必须做，否则扫描会抛 `AttributeError`。
    """
    if provider is None:
        return False
    available = getattr(provider, 'available', None)
    if not callable(available):
        return False
    try:
        return bool(available())
    except Exception:  # pragma: no cover - 提供者内部异常按"不可用"处理
        return False


def _reaction_api_available(transport: Any) -> bool:
    """上游 `typeof internal?.setMsgEmojiLike === 'function'` 的等价探测。

    本移植版把平台出站能力收敛到 `Transport`（分解契约 §7）。`NullTransport`
    表示"当前没有平台连接器"，此时**不能**把表态能力写进 prompt —— 声明一个
    永远投递失败的能力比不声明更糟。
    """
    if not callable(getattr(transport, 'react', None)):
        return False
    return not isinstance(transport, NullTransport)


def _visibility_probe(entry: Any) -> dict[str, Any]:
    """把 snake_case 的召回缓存条目投影成 `isHistoryEntryVisibleToParticipant` 的形状。

    裁决函数（`config.is_history_entry_visible_to_participant`）逐字移植自上游，
    按**数据库行**的 camelCase 读 `participantId`；而召回缓存条目按本移植版的内部
    结构约定用 `participant_id`（`episode_index.EpisodeSource` 也这么读）。这个
    两键投影是两种约定的唯一交界处，避免在缓存里同时存两份拼写。
    """
    if not isinstance(entry, dict):
        return {}
    return {'kind': entry.get('kind'), 'participantId': entry.get('participant_id')}


def _config_number(config: Any, camel: str, snake: str, fallback: float) -> float:
    """上游 `Number(x) || fallback` 的等价物（NaN / 0 / 非法值都回落 fallback）。"""
    raw = _config_value(config, camel, snake)
    if raw is None or isinstance(raw, bool):
        return float(fallback)
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return float(fallback)
    if math.isnan(number) or math.isinf(number) or not number:
        return float(fallback)
    return number


def _nullish_number(config: Any, camel: str, snake: str, fallback: float) -> float:
    """上游 `value ?? fallback` 的等价物：**只有 null/undefined/非法值**才回落。

    与 `_config_number`（`Number(x) || fallback`）的区别在 `0`：`??` 保留 0，
    `||` 把 0 当空值。调用点必须按上游写的是 `??` 还是 `||` 选择对应的助手。
    """
    raw = _config_value(config, camel, snake)
    if raw is None or isinstance(raw, bool):
        return float(fallback)
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return float(fallback)
    if math.isnan(number) or math.isinf(number):
        return float(fallback)
    return number


def _clamp_int(value: float, minimum: int, maximum: int) -> int:
    """上游 `Math.max(min, Math.min(max, Math.floor(Number(x) || fallback)))`。"""
    return max(minimum, min(maximum, int(math.floor(value))))


def _list_sticker_files(root: str) -> list[str]:
    """上游 `listStickerFiles(root)`（`src/service.ts:7309`）的宿主侧降级实现。

    适配器实现了 `Transport.list_sticker_files` 时一律走它；没实现时在宿主文件
    系统上按上游的深度（≤3 层）与扩展名白名单（png/jpe?g/webp/gif）列举并排序。
    目录不存在返回空列表，等价上游 `readdir` 抛错被吞掉。
    """
    found: list[str] = []
    if not root or not os.path.isdir(root):
        return found
    base_depth = root.rstrip(os.sep).count(os.sep)
    for current, directories, names in os.walk(root):
        if current.rstrip(os.sep).count(os.sep) - base_depth >= 3:
            directories[:] = []
        for name in names:
            if re.search(r'\.(?:png|jpe?g|webp|gif)$', name, re.IGNORECASE):
                found.append(os.path.join(current, name))
    return sorted(found)


def _now_ms(owner: Any) -> int:
    """`Date.now()` 的等价物，允许用 partial host 调用（测试与上游一致）。"""
    method = getattr(owner, 'now_ms', None)
    if callable(method):
        return int(method())
    return dt_ms(utc_now())


def _message_characters(runtime: Any) -> int:
    """上游 `normalizeVisibleMessageContent(text, this.config.runtime.maxMessageCharacters, ...)`。

    配置缺失时上游 `String.prototype.slice(0, undefined)` **不截断**，因此这里用
    `2**31-1`（等价"不截断"）而不是某个会静默砍掉回复的小默认值。
    """
    raw = _config_value(runtime, 'maxMessageCharacters', 'max_message_characters')
    if raw is None or isinstance(raw, bool):
        return 2 ** 31 - 1
    return int(_nullish_number(runtime, 'maxMessageCharacters', 'max_message_characters', 2 ** 31 - 1))


def _expires_in_future(value: Any, now: Any) -> bool:
    """上游 `!item.expiresAt || new Date(item.expiresAt) > now`。

    JS 里非法日期得到 `Invalid Date`，任何比较都是 `false` —— 因此解析失败
    等于**过期**（裁掉），而不是"永不过期"。
    """
    if not value:
        return True
    parsed = parse_dt(value)
    return parsed is not None and parsed > now


def _obsolete_request_ids(turn: dict[str, Any]) -> set:
    """缓冲回合的「已作废在途请求」集合，**两种拼写指向同一个 set**。

    写侧曾经只写一种拼写，而 `flushBufferedNarrative`（Chunk3）用
    `_turn_get(turn, 'obsoleteRequestIds', 'obsolete_request_ids')` 读、优先 camelCase——
    只要两者落在不同的键上，作废标记就会静默丢失（用户 2026-09-26 00:22 的日志里，
    连发消息因此没有被合并进同一回合）。这里保证两个键拿到的是**同一个对象**。
    """
    current = _turn_get(turn, 'obsoleteRequestIds', 'obsolete_request_ids')
    if not isinstance(current, set):
        current = set()
    _turn_set(turn, 'obsoleteRequestIds', 'obsolete_request_ids', current)
    return current


class ServiceChunk2(ServiceBase):
    """对应 upstream/src/service.ts 第 1929–2572 行的成员。"""

    # ------------------------------------------------------------------ #
    # 群消息读取与能力声明（`src/service.ts:1929-1983`）
    # ------------------------------------------------------------------ #

    async def group_messages(self, story_id: str, group_id: str, limit: int) -> list[dict[str, Any]]:
        """上游 `groupMessages(storyId, groupId, limit)`（`src/service.ts:1929`）。

        从最近 8×limit 条剧本条目里筛出该群的消息，取最新 `limit` 条后**反转**
        成时间正序（`GroupMessageContext` 的内部 snake_case 形状）。
        """
        bounded = max(1, int(limit))
        rows = await self.db_get(
            'interlude_script_entry', {'storyId': story_id},
            {'limit': max(20, min(200, bounded * 8)), 'sort': {'occurredAt': 'desc'}},
        )
        wanted_group = normalize_group_id(group_id)
        selected: list[dict[str, Any]] = []
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            if entry.get('kind') not in ('group-message', 'character-group-message'):
                continue
            metadata = entry.get('metadata') if isinstance(entry.get('metadata'), dict) else {}
            raw_group = pick(metadata, 'groupId', 'group_id')
            if normalize_group_id(str(raw_group if raw_group is not None else '')) != wanted_group:
                continue
            selected.append(entry)
        selected = selected[:bounded]
        selected.reverse()

        messages: list[dict[str, Any]] = []
        for entry in selected:
            metadata = entry.get('metadata') if isinstance(entry.get('metadata'), dict) else {}
            actor = entry.get('actor')
            default_id = 'character' if actor == 'character' else 'unknown'
            raw_sender_id = pick(metadata, 'senderId', 'sender_id')
            sender_id = str(raw_sender_id) if raw_sender_id is not None else default_id
            raw_sender_name = pick(metadata, 'senderName', 'sender_name')
            if raw_sender_name is not None:
                sender_name = str(raw_sender_name)
            elif actor == 'character':
                sender_name = '主角'
            else:
                sender_name = str(raw_sender_id) if raw_sender_id is not None else '群成员'
            message: dict[str, Any] = {
                'sender_id': sender_id,
                'sender_name': sender_name,
                'speaker': format_group_speaker(sender_name, sender_id),
            }
            message_id = targetable_message_id(pick(metadata, 'messageId', 'message_id'))
            if message_id:
                message['message_id'] = message_id
                message['message_ref'] = group_message_ref(entry.get('id'))
            quote = normalize_quoted_message_context(pick(metadata, 'quote'))
            if quote:
                message['quote'] = quote
            message['content'] = entry.get('content')
            message['occurred_at'] = entry.get('occurredAt')
            message['direction'] = 'character' if actor == 'character' else 'user'
            messages.append(message)
        return messages

    async def group_cooldown_active(self, story_id: str, group_id: str, cooldown_seconds: float) -> bool:
        """上游 `groupCooldownActive(...)`（`src/service.ts:1953`）。

        只看**主角自己**在群里最近的一次群消息/平台动作：仍落在冷却窗口内则 True。
        """
        if cooldown_seconds <= 0:
            return False
        rows = await self.db_get(
            'interlude_script_entry', {'storyId': story_id},
            {'limit': 100, 'sort': {'occurredAt': 'desc'}},
        )
        wanted_group = normalize_group_id(group_id)
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            if entry.get('kind') not in ('character-group-message', 'character-platform-action'):
                continue
            metadata = entry.get('metadata') if isinstance(entry.get('metadata'), dict) else {}
            raw_group = pick(metadata, 'groupId', 'group_id')
            if normalize_group_id(str(raw_group if raw_group is not None else '')) != wanted_group:
                continue
            occurred_at = parse_dt(entry.get('occurredAt'))
            if occurred_at is None:
                return False
            return _now_ms(self) - dt_ms(occurred_at) < cooldown_seconds * 1000
        return False

    def group_chat_capabilities(
        self, session: Any, messages: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """上游 `groupChatCapabilities(session, messages)`（`src/service.ts:1963`）。

        能力是**瞬时的**：Console 配置与实时平台连接器必须同时允许。QQ 群里的
        引用回复/表态/原生表情都要求至少有一条可寻址的入站消息。
        """
        config = _config_section(self.config, 'chatActions')
        if _config_value(config, 'enabled') is not True:
            return None
        if session is None or not is_one_bot_platform(pick(session, 'platform')):
            return None
        if 'qq' not in (_config_value(config, 'platforms') or []):
            return None
        if not any(
            pick(message, 'messageRef', 'message_ref') and pick(message, 'messageId', 'message_id')
            for message in (messages or [])
            if isinstance(message, dict)
        ):
            return None
        quote_reply = _config_value(config, 'quoteReply', 'quote_reply') is True
        reactions = (
            normalize_allowed_reactions(_config_value(config, 'allowedReactions', 'allowed_reactions'))
            if _config_value(config, 'messageReactions', 'message_reactions') is True
            and _reaction_api_available(self.transport)
            else []
        )
        native_faces = (
            normalize_allowed_native_faces(_config_value(config, 'allowedNativeFaces', 'allowed_native_faces'))
            if _config_value(config, 'nativeFaces', 'native_faces') is True
            else []
        )
        if not quote_reply and not reactions and not native_faces:
            return None
        return {
            'platform': 'qq',
            'quote_reply': quote_reply,
            'reactions': reactions,
            'native_faces': native_faces,
            'expression_threshold': normalize_expression_threshold(
                _config_value(config, 'expressionThreshold', 'expression_threshold'),
            ),
        }

    def private_chat_capabilities(self, session: Any) -> Optional[dict[str, Any]]:
        """上游 `privateChatCapabilities(session)`（`src/service.ts:1977`）。

        私聊没有引用回复（没有可寻址的群消息），只有原生表情通道。
        """
        config = _config_section(self.config, 'chatActions')
        if _config_value(config, 'enabled') is not True:
            return None
        if session is None or not is_one_bot_platform(pick(session, 'platform')):
            return None
        if 'qq' not in (_config_value(config, 'platforms') or []):
            return None
        native_faces = (
            normalize_allowed_native_faces(_config_value(config, 'allowedNativeFaces', 'allowed_native_faces'))
            if _config_value(config, 'nativeFaces', 'native_faces') is True
            else []
        )
        if not native_faces:
            return None
        return {
            'platform': 'qq',
            'quote_reply': False,
            'reactions': [],
            'native_faces': native_faces,
            'expression_threshold': normalize_expression_threshold(
                _config_value(config, 'expressionThreshold', 'expression_threshold'),
            ),
        }

    # ------------------------------------------------------------------ #
    # 聊天动作执行（`src/service.ts:1985-2145`）
    # ------------------------------------------------------------------ #

    async def execute_group_reactions(
        self,
        story: dict[str, Any],
        session: Any,
        group_id: str,
        reactions: list[dict[str, Any]],
        reference_for: Any = None,
    ) -> int:
        """上游 `executeGroupReactions(...)`（`src/service.ts:1985`）。

        当前实现**最多执行一条**表态（`reactions.slice(0, 1)`），并且投递结局
        一律记进投递账本（`recordPlatformDeliveryOutcome` /
        `updateScriptDeliveryOutcome`）。返回真正完成的条件数。
        """
        completed = 0
        for reaction in list(reactions or [])[:1]:
            if not isinstance(reaction, dict):
                continue
            reference = reference_for(reaction) if callable(reference_for) else None
            platform_delivered = False
            try:
                if not _reaction_api_available(self.transport):
                    if reference:
                        await self.record_platform_delivery_outcome(
                            story.get('id'), reference, 'cancelled', 'reaction-api-unavailable',
                        )
                    continue
                delivered = await self.transport.react(
                    pick(reaction, 'messageRef', 'message_ref'), pick(reaction, 'reaction'),
                )
                if delivered is not True:
                    if reference:
                        await self.record_platform_delivery_outcome(
                            story.get('id'), reference, 'cancelled', 'reaction-api-unavailable',
                        )
                    continue
                platform_delivered = True
                completed_at = self.now()
                message_ref = pick(reaction, 'messageRef', 'message_ref')
                reaction_name = pick(reaction, 'reaction')

                async def commit(
                    message_ref: Any = message_ref,
                    reaction_name: Any = reaction_name,
                    completed_at: Any = completed_at,
                    reference: Any = reference,
                ) -> None:
                    metadata: dict[str, Any] = {
                        'platform': 'qq', 'action': 'message-reaction', 'groupId': group_id,
                        'messageRef': message_ref, 'reaction': reaction_name,
                    }
                    if reference:
                        metadata.update(reference)
                        metadata['deliverySegmentIndex'] = pick(reference, 'segmentIndex', 'segment_index')
                    await self.append_entry(story.get('id'), {
                        'kind': 'character-platform-action', 'actor': 'character',
                        'content': '主角给群消息 %s 添加了 %s 表情回应。' % (message_ref, reaction_name),
                        'occurredAt': iso(completed_at),
                        'metadata': metadata,
                    }, completed_at)
                    if reference:
                        await self.update_script_delivery_outcome(
                            story.get('id'), reference, 'delivered', completed_at,
                        )

                await self.serial(story.get('id'), commit)
                completed += 1
                self.report_operation(
                    'standard', 'info', story, 'user-message',
                    '聊天动作完成 类型=消息表情 群=%s 目标=%s 表情=%s',
                    group_id, message_ref, reaction_name,
                )
            except Exception as error:
                if reference:
                    await self.record_platform_delivery_outcome(
                        story.get('id'), reference,
                        'delivered' if platform_delivered else 'failed',
                        None if platform_delivered else clip(str(error), 500),
                    )
                if platform_delivered:
                    completed += 1
                    self.report(
                        'warn', story, 'user-message',
                        '聊天动作已完成但结果记录不完整 类型=消息表情 群=%s 目标=%s 错误=%s',
                        (group_id, pick(reaction, 'messageRef', 'message_ref'), error),
                    )
                else:
                    self.report(
                        'warn', story, 'user-message',
                        '聊天动作失败 类型=消息表情 群=%s 目标=%s 错误=%s',
                        (group_id, pick(reaction, 'messageRef', 'message_ref'), error),
                    )
        return completed

    def resolve_sticker(
        self, draft: Any, catalog: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """上游 `resolveSticker(draft, catalog)`（`src/service.ts:2035`）。

        模型给的 `localMedia` 草稿只有在「素材确实在当前瞬时目录里」且
        「意愿过表达阈值」时才被解析成可发送的资产。
        """
        if not isinstance(draft, dict):
            return None
        asset_id = pick(draft, 'assetId', 'asset_id')
        willingness = pick(draft, 'willingness')
        if not isinstance(asset_id, str):
            return None
        if isinstance(willingness, bool) or not isinstance(willingness, (int, float)):
            return None
        if not any(
            pick(item, 'assetId', 'asset_id') == asset_id
            for item in (catalog or [])
            if isinstance(item, dict)
        ):
            return None
        if normalize_expression_threshold(willingness) < self.expression_threshold:
            return None
        return self.sticker_by_id.get(asset_id)

    @property
    def expression_threshold(self) -> float:
        """上游 `get expressionThreshold()`（`src/service.ts:2041`）。"""
        config = _config_section(self.config, 'chatActions')
        return normalize_expression_threshold(
            _config_value(config, 'expressionThreshold', 'expression_threshold'),
        )

    def resolve_native_face(
        self, decision: dict[str, Any], capabilities: Any,
    ) -> Optional[str]:
        """上游 `resolveNativeFace(decision, capabilities)`（`src/service.ts:2045`）。

        只认**显式声明**的原生表情草稿：语义必须在允许表里，且校准后的意愿
        达到阈值。旧的方括号标签由兼容层解析，但没有声明意愿，因此永远不能
        绕过表达阈值。
        """
        allowed = set(pick(capabilities, 'nativeFaces', 'native_faces') or [])
        if not allowed:
            return None
        draft = pick(decision, 'nativeFace', 'native_face')
        group_reply = pick(decision, 'groupReply', 'group_reply')
        interaction = pick(decision, 'interaction')
        # 上游用 `??`：空字符串**不**继续回落，只有 null/undefined 才回落。
        reply_content: Any = None
        if isinstance(group_reply, dict) and group_reply.get('content') is not None:
            reply_content = group_reply.get('content')
        elif isinstance(interaction, dict):
            reply = interaction.get('reply')
            if isinstance(reply, dict):
                reply_content = reply.get('content')
        reply_text = reply_content if reply_content is not None else ''
        threshold = pick(capabilities, 'expressionThreshold', 'expression_threshold')
        if threshold is None:
            threshold = self.expression_threshold
        if not isinstance(draft, dict):
            return None
        semantic = draft.get('semantic')
        if semantic in allowed and calibrated_native_face_willingness(
            semantic, draft.get('willingness'), reply_text,
        ) >= threshold:
            return semantic
        return None

    async def send_sticker(
        self,
        story: dict[str, Any],
        session: Any,
        channel_id: str,
        asset: dict[str, Any],
        group_id: Optional[str] = None,
        reference: Any = None,
    ) -> bool:
        """上游 `sendSticker(...)`（`src/service.ts:2059`）。

        路径解析与越界检查逐字照搬：素材文件必须真的落在表情库目录里。
        出站走 `Transport.send_sticker`（分解契约 §7）。
        """
        root = self.sticker_library_root()
        file_path = os.path.abspath(os.path.join(root, str(pick(asset, 'filePath', 'file_path') or '')))
        relative_path = os.path.relpath(file_path, root)
        if (
            not relative_path
            or relative_path == '..'
            or relative_path.startswith('..' + os.sep)
            or ':' in relative_path
        ):
            if reference:
                await self.record_platform_delivery_outcome(
                    story.get('id'), reference, 'cancelled', 'invalid-sticker-path',
                )
            return False
        platform_delivered = False
        try:
            send = getattr(self.transport, 'send_sticker', None)
            if not callable(send):
                raise RuntimeError('transport-unavailable')
            result = await send(channel_id, file_path, is_group=bool(group_id))
            if not isinstance(result, dict) or result.get('ok') is not True:
                reason = pick(result, 'error') if isinstance(result, dict) else None
                raise RuntimeError(str(reason or 'sticker-delivery-failed'))
            platform_delivered = True
            now = self.now()
            asset_id = pick(asset, 'assetId', 'asset_id')
            description = pick(asset, 'description')
            metadata: dict[str, Any] = {
                'platform': pick(session, 'platform'), 'action': 'local-sticker',
                'assetId': asset_id, 'group': pick(asset, 'group'),
                'animated': pick(asset, 'animated'),
            }
            if group_id:
                metadata['groupId'] = group_id
            if reference:
                metadata.update(reference)
                metadata['deliverySegmentIndex'] = pick(reference, 'segmentIndex', 'segment_index')

            async def commit() -> None:
                await self.append_entry(story.get('id'), {
                    'kind': 'character-platform-action', 'actor': 'character',
                    'content': '主角发送了本地表情包：%s' % description,
                    'occurredAt': iso(now),
                    'metadata': metadata,
                }, now)
                if reference:
                    await self.update_script_delivery_outcome(
                        story.get('id'), reference, 'delivered', now,
                    )

            await self.serial(story.get('id'), commit)
            # 用量计数（v1.8.0）：控制台按它排"哪些表情她真的在用"。
            # 计数失败**只 warn**——它不该把一次已经成功发出去的投递判成失败。
            await self.record_sticker_use(asset)
            self.report_operation(
                'standard', 'info', story, 'user-message',
                '聊天动作完成 类型=本地表情包 素材=%s', asset_id,
            )
            return True
        except Exception as error:
            if reference:
                await self.record_platform_delivery_outcome(
                    story.get('id'), reference,
                    'delivered' if platform_delivered else 'failed',
                    None if platform_delivered else clip(str(error), 500),
                )
            if platform_delivered:
                self.report(
                    'warn', story, 'user-message',
                    '聊天动作已完成但结果记录不完整 类型=本地表情包 素材=%s 错误=%s',
                    (pick(asset, 'assetId', 'asset_id'), error),
                )
                return True
            self.report(
                'warn', story, 'user-message',
                '聊天动作失败 类型=本地表情包 素材=%s 错误=%s',
                (pick(asset, 'assetId', 'asset_id'), error),
            )
            return False

    async def send_native_face(
        self,
        story: dict[str, Any],
        session: Any,
        channel_id: str,
        semantic: str,
        group_id: Optional[str] = None,
        reference: Any = None,
    ) -> bool:
        """上游 `sendNativeFace(...)`（`src/service.ts:2107`）。

        上游注释：OneBot 11 规范 `face.id` 是 int32，字符串 id 会被严格校验的
        实现直接拒绝；本移植版的 `Transport.send_native_face` 收 face id 字符串，
        由适配器负责转成平台要求的数字（`helpers.QQ_NATIVE_FACE_IDS`）。
        """
        platform_delivered = False
        try:
            from .helpers import QQ_NATIVE_FACE_IDS

            send = getattr(self.transport, 'send_native_face', None)
            if not callable(send):
                raise RuntimeError('transport-unavailable')
            result = await send(channel_id, str(QQ_NATIVE_FACE_IDS.get(semantic, '')), is_group=bool(group_id))
            if not isinstance(result, dict) or result.get('ok') is not True:
                reason = pick(result, 'error') if isinstance(result, dict) else None
                raise RuntimeError(str(reason or 'native-face-delivery-failed'))
            platform_delivered = True
            now = self.now()
            metadata: dict[str, Any] = {
                'platform': pick(session, 'platform'), 'action': 'native-face', 'semantic': semantic,
            }
            if group_id:
                metadata['groupId'] = group_id
            if reference:
                metadata.update(reference)
                metadata['deliverySegmentIndex'] = pick(reference, 'segmentIndex', 'segment_index')

            async def commit() -> None:
                await self.append_entry(story.get('id'), {
                    'kind': 'character-platform-action', 'actor': 'character',
                    'content': '主角发送了 %s 原生表情。' % semantic,
                    'occurredAt': iso(now), 'metadata': metadata,
                }, now)
                if reference:
                    await self.update_script_delivery_outcome(
                        story.get('id'), reference, 'delivered', now,
                    )

            await self.serial(story.get('id'), commit)
            self.report_operation(
                'standard', 'info', story, 'user-message',
                '聊天动作完成 类型=原生表情 语义=%s', semantic,
            )
            return True
        except Exception as error:
            if reference:
                await self.record_platform_delivery_outcome(
                    story.get('id'), reference,
                    'delivered' if platform_delivered else 'failed',
                    None if platform_delivered else clip(str(error), 500),
                )
            if platform_delivered:
                self.report(
                    'warn', story, 'user-message',
                    '聊天动作已完成但结果记录不完整 类型=原生表情 语义=%s 错误=%s',
                    (semantic, error),
                )
                return True
            self.report(
                'warn', story, 'user-message',
                '聊天动作失败 类型=原生表情 语义=%s 错误=%s', (semantic, error),
            )
            return False

    async def send_group_message(
        self,
        story: dict[str, Any],
        channel_id: str,
        content: str,
        reply_to_message_id: Optional[str] = None,
        session: Any = None,
    ) -> dict[str, Any]:
        """上游 `sendGroupMessage(...)`（`src/service.ts:2147`）。

        上游先按 `story.selfId` / `story.platform` 找一个可用机器人账号；本移植版
        把这条能力收敛到 `Transport.send_group`，因此「没有可用账号」等价于
        「没有可用的出站通道」。逐段投递并保留每段的结局，便于投递账本记账。
        """
        segments = runtime_bubble_segments(
            _config_section(self.config, 'runtime'), content, bool(self.voice_reply_enabled),
        )
        send = getattr(self.transport, 'send_group', None)
        if not callable(send):
            self.report(
                'warn', story, 'user-message',
                '没有可用机器人账号投递群消息 群频道=%s 故事平台=%s 故事账号=%s',
                (channel_id, pick(story, 'platform'), pick(story, 'selfId', 'self_id')),
            )
            return {
                'delivered_segments': [],
                'complete': False,
                'segment_outcomes': [
                    {'index': index, 'content': segment['content'], 'status': 'failed',
                     'reason': 'transport-unavailable'}
                    for index, segment in enumerate(segments)
                ],
            }
        all_delivered = True
        delivered_segments: list[str] = []
        segment_outcomes: list[dict[str, Any]] = []
        for index, segment in enumerate(segments):
            segment_content = str(segment['content'])
            reply_to = reply_to_message_id if index == 0 and reply_to_message_id else None
            try:
                # 正文 `<tts/>` 指定的分段以语音投递（`voice_kwargs`：非语音路径
                # 不传这个关键字，老实现的调用形状逐字不变）。
                result = await send(
                    channel_id, segment_content, reply_to,
                    **voice_kwargs(segment.get('voice') is True and not reply_to),
                )
                if not isinstance(result, dict) or result.get('ok') is not True:
                    reason = pick(result, 'error') if isinstance(result, dict) else None
                    raise RuntimeError(str(reason or 'group-delivery-failed'))
                delivered_segments.append(segment_content)
                segment_outcomes.append({'index': index, 'content': segment_content, 'status': 'delivered'})
            except Exception as error:
                all_delivered = False
                segment_outcomes.append({
                    'index': index, 'content': segment_content, 'status': 'failed',
                    'reason': clip(str(error), 500),
                })
                self.report(
                    'warn', story, 'user-message', '群消息投递失败 群频道=%s 错误=%s',
                    (channel_id, error),
                )
        return {'delivered_segments': delivered_segments, 'complete': all_delivered, 'segment_outcomes': segment_outcomes}

    # ------------------------------------------------------------------ #
    # 入站缓冲与流式早发（`src/service.ts:2189-2244`）
    # ------------------------------------------------------------------ #


    def buffer_user_narrative(
        self,
        story: dict[str, Any],
        participant: dict[str, Any],
        session: Any,
        now: Any,
        superseded_intents: list[dict[str, Any]],
        content: Any = None,
        image_sources: Optional[list[str]] = None,
        audio_sources: Optional[list[str]] = None,
        quote: Any = None,
        media: Optional[list[dict[str, Any]]] = None,
        endpoint_id: str = '',
    ) -> None:
        """上游 `bufferUserNarrative(...)`（`src/service.ts:2189`）。

        已持久化的消息在这里短暂停留后再进叙事器，让「你好 / 在吗 / 我有件事
        想问」合成一个事件，同时不以丢消息为代价。

        `turn` 的形状（本移植版内部结构，snake_case；`types.py` 未声明该类型，
        与 `OutgoingMessageDraft` 同一约定）：

        ``{'story_id', 'participant_id', 'messages': [{'content', 'occurred_at',
        'superseded_intents', 'image_sources', 'audio_sources', 'quote'}],
        'next_revision', 'timer', 'latest_session', 'in_flight_request_id',
        'first_message_committed_request_id', 'obsolete_request_ids'}``
        """
        if content is None:
            raw_content = pick(session, 'content')
            content = str(raw_content if raw_content is not None else '')
        key = pick(participant, 'id')
        existing = self.buffered_narrative_turns.get(key)
        turn: dict[str, Any] = existing if isinstance(existing, dict) else {
            'story_id': pick(story, 'id'),
            'participant_id': pick(participant, 'id'),
            'messages': [],
            'next_revision': 0,
            'obsolete_request_ids': set(),
        }
        if should_supersede_narrative_request(
            _turn_get(turn, 'inFlightRequestId', 'in_flight_request_id'),
            _turn_get(turn, 'firstMessageCommittedRequestId', 'first_message_committed_request_id'),
            _obsolete_request_ids(turn),
        ):
            _obsolete_request_ids(turn).add(
                _turn_get(turn, 'inFlightRequestId', 'in_flight_request_id'),
            )
            self.report_operation(
                'standard', 'info', story, 'user-message',
                '新消息到达且首条回复尚未提交，放弃旧请求 参与者=%s 请求=%d',
                pick(participant, 'id'),
                _turn_get(turn, 'inFlightRequestId', 'in_flight_request_id') or 0,
            )
        message: dict[str, Any] = {
            'content': content,
            'occurred_at': now,
            'superseded_intents': superseded_intents,
            'image_sources': list(image_sources or []),
            'audio_sources': list(audio_sources or []),
            'media': list(media or []),
        }
        if quote:
            message['quote'] = quote
        if endpoint_id:
            # 上游 1.0.1-rc28：逐条消息记住它的入站端点（M4 规则 1/2 的唯一依据）。
            message['endpoint_id'] = endpoint_id
        turn.setdefault('messages', []).append(message)
        if endpoint_id:
            turn['source_seq'] = int(turn.get('source_seq') or 0) + 1
            sources = turn.setdefault('sources', [])
            if not any(pick(item, 'endpointId', 'endpoint_id') == endpoint_id for item in sources):
                sources.append({'endpointId': endpoint_id, 'receivedSeq': turn['source_seq']})
            elif sources:
                for item in sources:
                    if pick(item, 'endpointId', 'endpoint_id') == endpoint_id:
                        item['receivedSeq'] = turn['source_seq']
            # 当前批次的端点集合（`activeBatchEndpointIds`）：flush 消费后清空。
            active = turn.setdefault('active_batch_endpoint_ids', [])
            if endpoint_id not in active:
                active.append(endpoint_id)
        turn['latest_session'] = session
        timer = turn.get('timer')
        if callable(timer):
            timer()
        revision = int(turn.get('next_revision') or 0) + 1
        turn['next_revision'] = revision
        # 上游是 `?? 2`（不是 `|| 2`）：显式的 0 表示"不防抖"，必须保留。
        debounce_seconds = _nullish_number(
            self.runtime_config, 'userMessageDebounceSeconds', 'user_message_debounce_seconds', 2,
        )
        delay_ms = max(0.0, debounce_seconds) * 1000
        turn['timer'] = self.ctx.set_timeout(
            lambda: self.flush_buffered_narrative(key, revision), delay_ms,
        )
        self.buffered_narrative_turns[key] = turn
        # 自动收藏（本移植版新增，受控偏离 §45）：**旁路**，不 await。
        # 为什么在入队时而不是叙事请求里：① 手上就有这一条消息的来源与种类，不必再对齐；
        # ② 收藏要下载 / 读盘 / 调视觉模型，把它放进关键路径就是给每回合加延迟。
        # 失败只记日志（`_spawn_sticker_collect` 里兜住），绝不影响叙事。
        # 纯文字消息（绝大多数）**不建任务**：这里先做一次同步判断，别给每条消息都挂一个
        # 空跑的 task（"旁路"也不该按消息量线性增加调度开销）。
        if _has_collectible_media(message.get('media'), self._sticker_guess_enabled()):
            self._spawn_sticker_collect(key, len(turn['messages']) - 1)
        self.report_operation(
            'diagnostic', 'debug', story, 'user-message',
            '短时消息合并 参与者=%s 待处理=%d 等待=%dms',
            pick(participant, 'id'), len(turn['messages']), int(delay_ms),
        )

    def signal_incoming_interruption(
        self, story: dict[str, Any], participant: dict[str, Any],
    ) -> None:
        """上游 `signalIncomingInterruption(...)`（`src/service.ts:2209`）。

        新消息到达时把在飞请求标记为过时，避免「用户已经补了新消息，旧请求
        还把首条回复发出去」。
        """
        participant_id = pick(participant, 'id')
        self.interrupted_typing_participants.add(participant_id)
        turn = self.buffered_narrative_turns.get(participant_id)
        if not isinstance(turn, dict):
            return
        in_flight = _turn_get(turn, 'inFlightRequestId', 'in_flight_request_id')
        if not should_supersede_narrative_request(
            in_flight,
            _turn_get(turn, 'firstMessageCommittedRequestId', 'first_message_committed_request_id'),
            _obsolete_request_ids(turn),
        ):
            return
        _obsolete_request_ids(turn).add(in_flight)
        self.report_operation(
            'standard', 'info', story, 'user-message',
            '新消息到达且首条回复尚未提交，放弃旧请求 参与者=%s 请求=%d',
            participant_id, in_flight or 0,
        )

    async def deliver_early_private_reply(
        self,
        story: dict[str, Any],
        participant: dict[str, Any],
        session: Any,
        turn: dict[str, Any],
        request_id: int,
        reply: dict[str, Any],
    ) -> Any:
        """上游 `deliverEarlyPrivateReply(...)`（`src/service.ts:2221`）。

        实验性流式路径：只有**完整且通过校验**的私聊回复才允许提前离开。
        它在与普通首条投递完全相同的时刻提交中断边界。
        """
        if pick(reply, 'kind') != 'private':
            return False
        interaction = pick(reply, 'interaction')
        if not isinstance(interaction, dict):
            return False
        reply_body = interaction.get('reply')
        if not isinstance(reply_body, dict) or reply_body.get('mode') != 'immediate':
            return False
        if _turn_get(turn, 'nextRevision', 'next_revision') != request_id:
            return False
        if request_id in _obsolete_request_ids(turn):
            return False
        if _turn_get(turn, 'firstMessageCommittedRequestId',
                     'first_message_committed_request_id') == request_id:
            return False
        if not self.can_handle_participant(participant):
            return False
        # 早发内容与常规投递共用同一可见文本合约：长度上限与括号表情标签清理一致。
        content = normalize_visible_message_content(
            reply_body.get('content'),
            _message_characters(self.runtime_config),
            _config_value(self.runtime_config, 'messageSeparator', 'message_separator', '<sep/>'),
        )
        # v1.7.7：这条**不走** `prepareOutgoingDelivery`（早发直接进投递），
        # 所以正文语音标记要在这里按同一口径处理：标记删掉（绝不进用户看到的字），
        # 整条只有一段时它就是语音。多段内容本来就会被下面的单段守卫拒掉。
        segments = runtime_bubble_segments(
            _config_section(self.config, 'runtime'), content, bool(self.voice_reply_enabled),
        )
        if not content or len(segments) != 1:
            return False
        early = segments[0]
        participant_id = pick(participant, 'id')
        delivered = await self.send_outgoing_messages(story, [{
            'participant_id': participant_id,
            'content': early['content'],
            'interaction': interaction,
            'user_initiated': True,
            **({'voice': True} if early['voice'] else {}),
        }], participant, session, typing_window=True)
        if not delivered:
            return False
        confirmed = await self.confirm_outgoing_deliveries(story, delivered)
        if not confirmed:
            return False
        _turn_set(turn, 'firstMessageCommittedRequestId', 'first_message_committed_request_id', request_id)
        self.report_operation(
            'standard', 'info', story, 'user-message',
            '实验性流式首条回复已提前投递 参与者=%s 请求=%d', participant_id, request_id,
        )
        return confirmed[0]

    # ------------------------------------------------------------------ #
    # 归一化的音频 / 贴纸配置（`src/service.ts:2247-2271`）
    # ------------------------------------------------------------------ #

    @property
    def audio_config(self) -> dict[str, Any]:
        """上游 `get audioConfig()`（`src/service.ts:2247`）。

        输出 snake_case（`out_format` / `max_file_size_mb` / `max_per_message`），
        与 `config.py` 的 `CONFIG_DEFAULTS['audio']` 一致；读取侧同时接受上游
        camelCase（`pick` 双读）。
        """
        if self.cached_audio_config:
            return self.cached_audio_config
        configured = _config_section(_config_section(self.config, 'model'), 'audio')
        formats = ('mp3', 'wav', 'ogg', 'm4a', 'flac', 'amr')
        out_format = _config_value(configured, 'outFormat', 'out_format')
        self.cached_audio_config = {
            'enabled': _config_value(configured, 'enabled') is True,
            'out_format': out_format if out_format in formats else 'mp3',
            'max_file_size_mb': _clamp_int(
                _config_number(configured, 'maxFileSizeMB', 'max_file_size_mb', 10), 1, 25,
            ),
            'max_per_message': _clamp_int(
                _config_number(configured, 'maxPerMessage', 'max_per_message', 1), 1, 3,
            ),
        }
        return self.cached_audio_config

    @property
    def sticker_config(self) -> dict[str, Any]:
        """上游 `get stickerConfig()`（`src/service.ts:2260`）。

        输出 snake_case，与 `config.py` 的 `CONFIG_DEFAULTS['stickers']` 一致。
        注意 `max_file_size_mb` 在 config.py 的 `StickerLibraryConfig` 里被写成
        `max_file_size_m_b`（笔误），这里以默认值表与 `_conf_schema.json` 实际使用
        的 `max_file_size_mb` 为准。
        """
        if self.cached_sticker_config:
            return self.cached_sticker_config
        configured = _config_section(self.config, 'stickers')
        directory = _config_value(configured, 'directory')
        self.cached_sticker_config = {
            'enabled': _config_value(configured, 'enabled') is True,
            # 本移植版新增（受控偏离 §45）：别人发来的表情包自动收进库。
            # **默认真**，但仍然受上面的 `enabled` 总闸约束（`enabled=false` 时整个库都不动）。
            # 双读：配置层写 snake_case，旧文件 / 上游名写 camelCase。
            'auto_collect': _config_value(configured, 'autoCollect', 'auto_collect') is not False,
            # 本移植版新增（受控偏离 §45.7）：第二层判据——让识图模型判断**普通图片**
            # 是不是表情包。**默认假**：用户要先主动打开才多花 token（`is True` 是刻意的，
            # 缺失 / NULL / 字符串一律当关）。同样受上面的 `enabled` 总闸约束。
            'auto_collect_guess': _config_value(
                configured, 'autoCollectGuess', 'auto_collect_guess',
            ) is True,
            'directory': str(directory if directory else 'data/hds-interlude/stickers').strip(),
            'max_file_size_mb': max(1.0, min(
                30.0, _config_number(configured, 'maxFileSizeMB', 'max_file_size_mb', 10),
            )),
            'catalog_limit': _clamp_int(
                _config_number(configured, 'catalogLimit', 'catalog_limit', 40), 1, 80,
            ),
            'description_max_tokens': _clamp_int(
                _config_number(configured, 'descriptionMaxTokens', 'description_max_tokens', 768), 256, 4096,
            ),
            'description_response_format': (
                'prompt-only'
                if _config_value(configured, 'descriptionResponseFormat', 'description_response_format') == 'prompt-only'
                else 'json-object'
            ),
        }
        return self.cached_sticker_config

    # ------------------------------------------------------------------ #
    # 入站事件折叠（`src/service.ts:2276`）
    # ------------------------------------------------------------------ #

    def describe_user_event(self, story: dict[str, Any], session: Any) -> dict[str, Any]:
        """上游 `describeUserEvent(story, session)`（`src/service.ts:2276`）。

        一个用户事件把打出的文字、图片与语音折成**一条事实**：附件走各自的
        原生通道，落库内容保留占位事实，让历史与桌面时间线仍然可读。

        `quote` 是喂给模型的 wire format，保持 `helpers.describe_quoted_message`
        的 camelCase 输出；其余键是内部结构，snake_case。
        """
        visual = self.describe_vision_event(session)
        if not isinstance(visual, dict):
            visual = {}
        sources = visual.get('sources') if isinstance(visual.get('sources'), list) else []
        audio_sources = extract_session_audio_sources(session)
        files = extract_session_file_facts(session)
        audio_files = [file for file in files if pick(file, 'audio')]
        plain_files = [file for file in files if not pick(file, 'audio')]
        media = visual.get('media') if isinstance(visual.get('media'), list) else []
        image_media = [item for item in media if pick(item, 'kind') != 'card']
        content = visual.get('content') or ''
        if content and sources:
            # 有文字时图片仍然进脚本：否则"他发了个表情包"这条事实会随回合消失，
            # 等她后面再提这张图时，记忆里只剩一句没头没尾的话。
            # 措辞与下面的音频/文件事实一致：**只报形式，不报画面**。
            content = '%s\n[用户同时发送了%s；内容以本轮视觉输入为准，未提供视觉内容时保持未知。]' % (
                content, _media_phrase(image_media) or '图片',
            )
        if not content:
            if sources:
                # 种类是**元数据**，不是画面内容：说清"收到的是表情包还是照片"
                # 不违反"没看到就别编"，反而让模型知道该用什么态度接。
                content = '[用户发送了%s；内容以本轮视觉输入为准，未提供视觉内容时保持未知。]' % (
                    _media_phrase(image_media) or '图片'
                )
            elif audio_sources:
                if audio_files:
                    names = '、'.join(str(pick(file, 'name') or '音频文件') for file in audio_files)
                    content = (
                        '[用户发送了音频文件：%s；音频内容以本轮原生音频输入为准，'
                        '未提供音频内容时保持未知。]' % names
                    )
                else:
                    content = '[用户发送了一条语音；语音内容以本轮原生音频输入为准，未提供音频内容时保持未知。]'
            elif plain_files:
                names = '、'.join(str(pick(file, 'name') or '未命名文件') for file in plain_files)
                content = '[用户发送了文件：%s；文件内容未知。]' % names
            else:
                content = ''
        elif plain_files:
            # 有文字时音频走原生通道（currentEvent.audioCount），普通文件没有
            # 独立通道，必须以文字事实补记，否则模型不知道有文件到达。
            names = '、'.join(str(pick(file, 'name') or '未命名文件') for file in plain_files)
            content = '%s\n[用户同时发送了文件：%s；文件内容未知。]' % (content, names)
        setting = pick(story, 'setting')
        character = pick(setting, 'character')
        character_name = pick(character, 'name') or '主角'
        return {
            'content': content,
            'sources': sources,
            'media': media,
            'audio_sources': audio_sources,
            'quote': describe_quoted_message(session, str(character_name)),
        }

    # ------------------------------------------------------------------ #
    # 贴纸库扫描与向量回填（`src/service.ts:2303-2434`）
    # ------------------------------------------------------------------ #

    async def scan_sticker_library(self) -> None:
        """上游 `scanStickerLibrary()`（`src/service.ts:2303`）。

        扫目录 → 建/更新 `pending` 资产 → 标记消失的文件 → 让视觉模型描述至多
        5 个新素材 → 刷新目录并回填向量。整个过程单飞（`sticker_scan_running`），
        且**单个坏文件不得饿死整库**。
        """
        config = self.sticker_config
        if not config.get('enabled') or self.sticker_scan_running:
            return
        self.sticker_scan_running = True
        try:
            root = self.sticker_library_root()
            lister = getattr(self.transport, 'list_sticker_files', None)
            files = await lister(root) if callable(lister) else _list_sticker_files(root)
            existing = await self.db_get('interlude_sticker', {})
            by_path = {
                row.get('filePath'): row for row in existing if isinstance(row, dict)
            }
            seen: set[str] = set()
            pending: list[dict[str, Any]] = []
            max_bytes = float(config.get('max_file_size_mb') or 10) * 1024 * 1024
            for file in files or []:
                file_path = os.path.relpath(file, root).replace('\\', '/')
                if not file_path or file_path.startswith('../'):
                    continue
                seen.add(file_path)
                try:
                    if os.path.getsize(file) > max_bytes:
                        continue
                    with open(file, 'rb') as handle:
                        payload = handle.read()
                    digest = hashlib.sha256(payload).hexdigest()
                    prior = by_path.get(file_path)
                    if isinstance(prior, dict) and prior.get('hash') == digest:
                        if prior.get('status') == 'active':
                            continue
                        # 用户**刻意停用**的资产（控制台勾掉 disabled）不归扫描管：
                        # 它只是不参与选图，不是"文件没了"，重扫不许把它复活成 pending。
                        if prior.get('status') == 'disabled':
                            continue
                        # 曾被标记 missing、文件又回来了：已经描述过的直接复活成 active，
                        # 不花一次视觉模型调用（只复活状态，不是重描述）。
                        if prior.get('status') == 'missing' and prior.get('description'):
                            await self.db_set(
                                'interlude_sticker', {'id': prior.get('id')},
                                {'status': 'active', 'updatedAt': self.now()},
                            )
                            continue
                        prior_updated = parse_dt(prior.get('updatedAt'))
                        age_ms = (
                            self.now_ms() - dt_ms(prior_updated)
                            if prior_updated is not None else float('inf')
                        )
                        if (
                            prior.get('status') == 'pending'
                            and age_ms < STICKER_DESCRIPTION_RETRY_COOLDOWN
                        ):
                            continue
                    group = file_path.split('/')[0] if '/' in file_path else 'default'
                    asset_id = stable_sticker_asset_id(file_path, digest)
                    now = self.now()
                    base: dict[str, Any] = {
                        'assetId': asset_id,
                        'filePath': file_path,
                        'group': group[:128],
                        'mimeType': sticker_mime(file_path),
                        'animated': bool(re.search(r'\.gif$', file_path, re.IGNORECASE)),
                        'size': len(payload),
                        'hash': digest,
                        # 名字由用户手工给（控制台可改）；扫描只维护一个稳定短名。
                        'name': ((prior or {}).get('name') if isinstance(prior, dict) else '') or '',
                        'source': 'manual',
                        'description': '',
                        'descriptionManual': False,
                        'aliases': [],
                        'status': 'pending',
                        'updatedAt': now,
                    }
                    if isinstance(prior, dict) and prior.get('id') is not None:
                        await self.db_set('interlude_sticker', {'id': prior.get('id')}, dict(base))
                        asset = {**prior, **base}
                    else:
                        created = await self.db_create('interlude_sticker', {**base, 'createdAt': now})
                        asset = created if isinstance(created, dict) else {**base, 'createdAt': now}
                    pending.append({'asset': asset, 'bytes': payload})
                except Exception as error:
                    # A malformed file, old duplicate row, or transient SQLite write must
                    # not starve the rest of the library for another five-minute cycle.
                    self.report_standalone_operation(
                        'standard', 'warn', '表情包素材跳过 文件=%s 错误=%s', file_path, error,
                    )
            for asset in existing:
                if not isinstance(asset, dict):
                    continue
                if asset.get('status') != 'missing' and asset.get('filePath') not in seen:
                    await self.db_set(
                        'interlude_sticker', {'id': asset.get('id')},
                        {'status': 'missing', 'updatedAt': self.now()},
                    )
            if pending and not _provider_available(self.sticker_describer):
                self.report_standalone(
                    'warn', '表情包库发现新素材，但没有配置 useForStickers 的视觉模型；已等待描述。',
                )
            for item in pending[:5]:
                if not _provider_available(self.sticker_describer):
                    break
                await self.describe_sticker_asset(item['asset'], item['bytes'], config)
            await self.refresh_sticker_catalog()
            await self.backfill_sticker_embeddings()
            await self.refresh_sticker_catalog()
        except Exception as error:
            self.report_standalone('warn', '表情包库扫描失败：%s', error)
        finally:
            self.sticker_scan_running = False

    def sticker_library_root(self) -> str:
        """表情库根目录的绝对路径（配置里的 `directory` 相对插件数据目录）。

        `scan_sticker_library()` 与自动收藏都从这一个地方取根 —— 两份路径推导迟早漂移，
        而"收藏写进了 A 目录、扫描看的是 B 目录"会表现为"收了但库里没有"。
        """
        return os.path.abspath(os.path.join(
            str(getattr(self.ctx, 'base_dir', '') or ''),
            str(self.sticker_config.get('directory') or ''),
        ))

    async def describe_sticker_asset(
        self, asset: Any, payload: bytes, config: Any = None,
    ) -> bool:
        """描述一个素材并立即登记 + 回填向量；成功返回 True。

        从 `scan_sticker_library()` 的循环体里**原样提取**（v1.8.0）：自动收藏要在写完
        文件后**立刻**触发同一个动作，而不是等下一个完整扫描周期。两条路径共用这一份，
        否则"补描述"和"扫描描述"会在冷却、告警文案、向量化上慢慢分家。
        """
        settings = config if isinstance(config, dict) else self.sticker_config
        asset_row = asset if isinstance(asset, dict) else {}
        item_id = asset_row.get('id')
        if item_id is None or not _provider_available(self.sticker_describer):
            return False
        if asset_row.get('descriptionManual') in (True, 1):
            # 用户手写的描述**压过**自动描述：不花模型调用，也不覆盖那一行
            # （受控偏离 §45.2；扫描与自动收藏都要走这条判定，所以放在这个方法里）。
            return False
        description: Any = None
        try:
            visual = await self.image_bytes_to_native(payload, asset_row.get('mimeType'))
            if visual:
                # `imageBytesToNative` 的移植版按内部结构约定输出
                # `mime_type` / `data_uri`（`types.NarrativeImage`），双读兼容。
                description = await self.sticker_describer.describe_sticker(
                    pick(visual, 'dataUri', 'data_uri'),
                    pick(visual, 'mimeType', 'mime_type'),
                    asset_row.get('filePath'),
                    asset_row.get('animated'), settings.get('description_response_format'),
                    settings.get('description_max_tokens'),
                )
        except Exception as error:
            self.report_standalone_operation(
                'standard', 'warn', '表情包描述失败，已冷却后重试 素材=%s 错误=%s',
                asset_row.get('assetId'), error,
            )
            await self.db_set('interlude_sticker', {'id': item_id}, {'updatedAt': self.now()})
            return False
        if not description:
            self.report_standalone_operation(
                'standard', 'warn', '表情包描述未返回可用 JSON，已冷却后重试 素材=%s',
                asset_row.get('assetId'),
            )
            await self.db_set('interlude_sticker', {'id': item_id}, {'updatedAt': self.now()})
            return False
        await self.db_set('interlude_sticker', {'id': item_id}, {
            'description': pick(description, 'description'),
            'aliases': pick(description, 'aliases'),
            'status': 'active',
            'updatedAt': self.now(),
        })
        await self._index_sticker_description(
            item_id, pick(description, 'description'), pick(description, 'aliases') or [],
        )
        self.report_standalone_operation(
            'standard', 'info', '表情包描述完成 素材=%s 分组=%s',
            asset_row.get('assetId'), asset_row.get('group'),
        )
        return True

    async def _index_sticker_description(
        self, item_id: Any, description: Any, aliases: Any = None,
    ) -> None:
        """给一条**已有描述**的素材立刻做向量回填（语义过滤下一回合就能考虑它）。

        从 `describe_sticker_asset()` 原样提取（§45.7）：第二层判据的描述来自**判定回执**，
        不走描述流程，但同样要进向量索引，否则"模型猜进来的"表情包在语义过滤里
        永远缺一条。没开语义过滤时什么都不做（不花 embedding 调用）。
        """
        if item_id is None or not self.semantic_sticker_embedding_enabled():
            return
        text = ('%s %s' % (description if description is not None else '',
                           ' '.join(str(alias) for alias in (aliases or [])))).strip()
        if not text:
            return
        embedding = await self.embed_text(text)
        if embedding:
            await self.db_set(
                'interlude_sticker', {'id': item_id},
                {'embedding': embedding, 'updatedAt': self.now()},
            )

    # ------------------------------------------------------------------ #
    # 自动收藏入站表情包（本移植版新增，受控偏离；见 `docs/PORTING_NOTES.md` §45）
    # ------------------------------------------------------------------ #

    def _spawn_sticker_collect(self, participant_key: Any, message_index: int) -> None:
        """把一次自动收藏挂到事件循环上（**不 await**）；失败只记日志。

        **私聊**的旁路入口（`buffer_user_narrative` 调用）。群聊走
        `_spawn_group_sticker_collect`：两个入口形状相同，只是"来源与种类从哪拿"不同
        （私聊回读缓冲回合、群聊直接用入站那一次解析的 `media`）。
        """
        self._spawn_sticker_task(
            self.collect_stickers_from_buffered_turn(participant_key, message_index),
        )

    def _spawn_group_sticker_collect(self, media: Any) -> None:
        """**群聊**的自动收藏旁路入口（`receive_group` 在通过全部入站闸门后调用）。

        为什么直接把这次解析出来的 `media` 带过来、而不是像私聊那样回读缓冲回合：
        群批次的 `turn['messages']` 会在 `flush_group_turn` 里被
        `del turn['messages'][:]` 清空，回读等于跟刷出抢时序（刷出先跑就静默丢收藏）。
        `media` 就是**同一次解析**的结果——群与私聊共用同一条
        `extract_session_media`（适配层写在 `SessionView.media` 上的结构化媒体表 →
        `[{source,kind,…}]`，§46），判据仍然只有 `helpers.collectible_sticker_kind()`
        一处，**不从 `[表情包]` 文本也不从 `<img>` 文本反推**。

        纯文字 / 只有普通照片的群消息**不建任务**（同私聊：旁路也不该按消息量线性增加调度）；
        `auto_collect_guess` 打开时普通照片也算"值得建任务"（第二层判据只对 `kind == 'image'`
        有意义，见 `_has_collectible_media`）。
        """
        if not _has_collectible_media(media, self._sticker_guess_enabled()):
            return
        self._spawn_sticker_task(
            self.collect_incoming_stickers(media, _media_sources(media)),
        )

    def _sticker_guess_enabled(self) -> bool:
        """第二层判据（模型判定普通图片）开了没？读不到配置一律按关处理。

        私聊与群聊两个旁路入口都要在**建任务之前**问这一句：开关关着时，
        普通图片连任务都不该建（与今天逐字一致）。
        """
        try:
            return self.sticker_config.get('auto_collect_guess') is True
        except Exception:  # noqa: BLE001 - 配置层异常不该影响入站路径
            return False

    def _spawn_sticker_task(self, coroutine: Any) -> None:
        """把一次收藏挂到事件循环上（**不 await**）；失败只记日志。

        收藏是旁路能力：读盘 / 下载 / 调视觉模型都可能慢或失败，而叙事回合不该等它。
        这里唯一要小心的是**别留下没人消费的异常**（`InvalidStateError` 之类会被
        asyncio 打成 "Task exception was never retrieved"，看起来像崩了）。
        """
        try:
            task = asyncio.ensure_future(coroutine)
        except RuntimeError:
            # 没有运行中的事件循环（同步调用路径 / 测试直接调 buffer）：跳过收藏。
            # 协程没进过循环，关掉它免得 Python 打 "coroutine was never awaited"。
            close = getattr(coroutine, 'close', None)
            if callable(close):
                close()
            return
        add_done = getattr(task, 'add_done_callback', None)
        if callable(add_done):
            add_done(self._consume_sticker_collect_result)

    def _consume_sticker_collect_result(self, task: Any) -> None:
        """消费收藏任务的异常（两个收藏协程自己都已 try/except 兜底）。"""
        try:
            error = task.exception()
        except Exception:  # noqa: BLE001 - 已取消 / 无结果都按无事发生
            return
        if error is not None:
            self.report_standalone('warn', '表情包自动收藏失败：%s', error)

    async def collect_stickers_from_buffered_turn(
        self, participant_key: Any, message_index: int,
    ) -> list[dict[str, Any]]:
        """从**已经入队的**那条消息里收表情包（`buffer_user_narrative` 的旁路入口）。

        为什么从回合里回读而不是让调用方传参：来源与种类必须来自**同一次解析**——
        调用方临时拼一份就又多一个"两处推导会漂移"的地方（坑 46 的老病）。
        消息还没进队列（索引对不上）时安静返回，不抛。
        """
        try:
            turn = self.buffered_narrative_turns.get(participant_key)
            messages = turn.get('messages') if isinstance(turn, dict) else None
            if not isinstance(messages, list):
                return []
            if not isinstance(message_index, int) or message_index < 0 or message_index >= len(messages):
                return []
            message = messages[message_index]
            if not isinstance(message, dict):
                return []
            return await self.collect_incoming_stickers(
                message.get('media'),
                _turn_get(message, 'imageSources', 'image_sources') or [],
            )
        except Exception as error:  # noqa: BLE001 - 旁路能力绝不打断回合
            self.report_standalone('warn', '表情包自动收藏异常：%s', error)
            return []

    async def collect_incoming_stickers(
        self,
        media: Any = None,
        sources: Any = None,
        media_by_source: Any = None,
        sticker_rows: Any = None,
    ) -> list[dict[str, Any]]:
        """把**确认是表情包**的入站附件收进本地表情库；返回新入库的资产行。

        判据分**两层，互不越权**：

        **第一层**（`helpers.collectible_sticker_kind()`，唯一入口，用户点名的红线）

        1. **种类必须是观测到的**：只有 `sticker` / `animated` / `market` 才考虑收藏。
           种类来自适配层从 OneBot 原始段捞出来的 `sub_type` / `summary`（见
           `astrbot_bridge.raw_media_hints`），**结构化**地随 `SessionView.media`
           下来，经 `extract_session_media` 成了每条媒体的 `kind`（§46；core **不读**
           正文里的 `<img>` 文本）。**缺失 / 未知 / `image` / `card` 一律不收**，记 debug。
           第一层认了的种类**直接收，永远不走模型**（行为与 v1.8.0 逐字一致）。
        2. **字节要真的验过是图片**：魔数嗅探（png/jpg/gif/webp）不过就跳过 + debug。
        3. **上限**：超过 `max_file_size_mb` 的字节在 `store_collected_sticker` 里被挡掉
           （字节已经拿到手才判，所以这一步不会因为"太大"而跳过下载）。
        4. **去重**：内容 sha256。同一个表情重复发、或者库里已经有同内容素材，都不再入库。

        **第二层**（`_sticker_guess_enabled()` + `guess_sticker_like()`，`§45.7`）

        只处理 `kind == 'image'`，且**只在 `stickers.auto_collect_guess` 打开时**才可能
        被调用——那些"被当成普通图片发过来"的表情包走这一层。**它永远不能否决第一层**，
        第一层也永远不该走模型。判定顺序刻意从便宜到贵：来源自带的体积（不下载就排除）
        → 图片头预筛（`sticker_guess_candidate`）→ 去重 → 节流 → 才调模型。

        拿不到字节时：**一条节流 warn**（这是能力缺失，用户该看见），本批不再重复报。

        只看 `media` / `sources` 里**适配器给出来的**来源——正文里手写的 URL 不在其中，
        所以这条路径不会变成"任意 URL 抓取器"。
        """
        try:
            config = self.sticker_config
        except Exception:  # noqa: BLE001 - 配置层异常不该影响叙事回合
            return []
        if not config.get('enabled') or not config.get('auto_collect'):
            return []
        guess_enabled = config.get('auto_collect_guess') is True
        rows = [row for row in (sticker_rows or []) if isinstance(row, dict)]
        kind_of = _incoming_sticker_kinds(media, rows, media_by_source)

        unique: list[str] = []
        seen: set[str] = set()
        for source in (sources or []):
            value = str(source if source is not None else '').strip()
            if value and value not in seen:
                seen.add(value)
                unique.append(value)

        collected: list[dict[str, Any]] = []
        missing_bytes = 0
        guessed_calls = 0
        for source in unique:
            kind = kind_of.get(source, '')
            first_layer = collectible_sticker_kind(kind)
            guessing = False
            if not first_layer:
                # 第二层只处理普通图片；`kind == 'image'` 且开关打开时才可能走到这里。
                # 走到这里就说明**第一层本来就不收**，所以第二层不可能否决第一层。
                if guess_enabled and kind == GUESS_STICKER_KIND:
                    if not self._sticker_guess_prefetch_ok(source, config):
                        self.report_standalone_operation(
                            'diagnostic', 'debug', '图片超过体积上限，未下载也未判定 来源=%s',
                            clip(source, 120),
                        )
                        continue
                    guessing = True
                else:
                    # ⚠️ 红线：`photo` / 空串（种类未知）一律不收藏，只留一条 debug。
                    self.report_standalone_operation(
                        'diagnostic', 'debug', '入站附件不是表情包，未收藏 种类=%s 来源=%s',
                        kind or 'unknown', clip(source, 120),
                    )
                    continue
            payload = await self._sticker_source_bytes(source)
            if not payload:
                # 种类确认是表情包、但字节拿不到 —— 这是**能力缺失**，按纪律用 warn
                # （节流；见坑 25：需要用户看见的东西不许走 diagnostic）。
                missing_bytes += 1
                continue
            mime = verify_sticker_image_bytes(payload)
            if not mime:
                self.report_standalone_operation(
                    'diagnostic', 'debug', '入站表情包字节不是图片，未收藏 种类=%s 来源=%s',
                    kind, clip(source, 120),
                )
                continue
            if guessing:
                if not sticker_guess_candidate(payload, mime):
                    # 记下尺寸：用户问"为什么这张没被判定"时，这一行就是答案
                    # （阈值是启发式，见 `helpers.GUESS_STICKER_*`）。
                    size = guess_image_dimensions(payload)
                    self.report_standalone_operation(
                        'diagnostic', 'debug',
                        '图片不像表情包（尺寸/形状预筛），未调模型 尺寸=%s 来源=%s',
                        ('%dx%d' % size) if size else 'unknown', clip(source, 120),
                    )
                    continue
                # 去重仍然**优先于**模型：同内容已经在库里（任何状态）就不问模型、不重复入库。
                prior = await self._sticker_prior_by_hash(hashlib.sha256(payload).hexdigest())
                if prior is not None:
                    await self._revive_missing_sticker(prior)
                    continue
                if guessed_calls >= STICKER_GUESS_MAX_PER_MESSAGE or not self._sticker_guess_budget():
                    self.report_standalone_operation(
                        'diagnostic', 'debug', '图片判定调用已达上限，本张跳过 来源=%s',
                        clip(source, 120),
                    )
                    continue
                guessed_calls += 1
                verdict = await self.guess_sticker_like(payload, mime, source)
                if not verdict:
                    continue
                asset = await self.store_collected_sticker(
                    payload, GUESS_STICKER_KIND, guessed=True,
                    description=str(verdict.get('description') or ''),
                )
                if asset:
                    collected.append(asset)
                continue
            asset = await self.store_collected_sticker(payload, kind)
            if asset:
                collected.append(asset)
        if missing_bytes:
            self._warn_sticker_collect_unavailable(missing_bytes)
        if collected:
            await self.refresh_sticker_catalog()
            for asset in collected:
                if asset.get('_described'):
                    # 判定回执里已经带了描述（§45.7）：直接用它入库，**不再花第二次模型调用**。
                    continue
                await self.describe_sticker_asset(asset, asset.get('_payload') or b'', config)
            await self.refresh_sticker_catalog()
        return collected

    async def store_collected_sticker(
        self, payload: bytes, kind: str = '', guessed: bool = False, description: str = '',
    ) -> Optional[dict[str, Any]]:
        """把一份**已验过是图片**的字节写进表情库；返回资产行（已入库的返回 `None`）。

        写盘 → 建档 → 返回。描述与向量化由调用方紧接着做（`describe_sticker_asset`），
        因为"立刻进目录"和"落盘"是两件事：先让资产可被扫描看到，再补描述。

        `guessed=True` 标记"这一条是**模型猜出来的**"（第二层判据，§45.7），落在
        `guessed` 列上；`source` 仍然是 `auto`（前端契约只有 `auto` / `manual` 两个取值，
        不加第三个）。`description` 是**判定回执里带回来的描述**：有值就直接入库并
        置 `active`（调用方据此跳过第二次描述调用），没有才走原来的描述流程。
        """
        if not payload:
            return None
        mime = verify_sticker_image_bytes(payload)
        if not mime:
            return None
        config = self.sticker_config
        max_bytes = float(config.get('max_file_size_mb') or 10) * 1024 * 1024
        if len(payload) > max_bytes:
            return None
        digest = hashlib.sha256(payload).hexdigest()
        # 去重：同内容已经在库里（任何状态）就不重复入库。`assetId` 有唯一索引，
        # 拿内容哈希当唯一键比"文件名不撞"可靠（对方可以把同一张图换个名字发过来）。
        prior = await self._sticker_prior_by_hash(digest)
        if prior is not None:
            if prior.get('status') == 'missing':
                # 文件曾被删、现在对方又发了一遍：把行复活（描述还在，不重花模型调用）。
                await self._revive_missing_sticker(prior)
                return {**prior, 'status': 'active' if prior.get('description') else 'pending'}
            return None

        root = self.sticker_library_root()
        file_path = '%s/%s%s' % (
            COLLECTED_STICKER_DIR, digest[:32], STICKER_FILE_SUFFIX.get(mime, '.png'),
        )
        target = os.path.join(root, file_path.replace('/', os.sep))
        # `filePath` 是**磁盘相对名**（相对表情库根目录），控制台与扫描都按它取值。
        # 落盘前不查"文件在不在"：`write` 本来就会覆盖，而内容一致时覆盖是幂等的。
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'wb') as handle:
                handle.write(payload)
        except OSError as error:
            self.report_standalone_operation(
                'standard', 'warn', '表情包收藏落盘失败 文件=%s 错误=%s', file_path, error,
            )
            return None

        asset_id = collected_sticker_asset_id(kind or 'sticker', digest)
        now = self.now()
        given = str(description or '').strip()
        base: dict[str, Any] = {
            'assetId': asset_id,
            'filePath': file_path,
            'group': COLLECTED_STICKER_DIR,
            'mimeType': mime,
            'animated': mime == 'image/gif',
            'size': len(payload),
            'hash': digest,
            'name': _collected_sticker_name({'hash': digest}),
            'source': 'auto',
            # 描述来自判定回执时**不置** `descriptionManual`：它不是人写的，
            # 用户手改之后仍由 `save_sticker_description` 接管（§45.2 的排序式标记）。
            'description': given,
            'descriptionManual': False,
            'aliases': [],
            # 已经有描述 = 可用了，直接进目录（`refresh_sticker_catalog` 只取 active）。
            'status': 'active' if given else 'pending',
            # 模型猜出来的（§45.7）；第一层收藏与磁盘扫描都是 False。
            'guessed': True if guessed else False,
            'updatedAt': now,
        }
        try:
            created = await self.db_create('interlude_sticker', {**base, 'createdAt': now})
        except Exception as error:  # noqa: BLE001 - 唯一索引冲突 / 旧库缺列都别炸叙事
            self.report_standalone_operation(
                'standard', 'warn', '表情包收藏建档失败 素材=%s 错误=%s', asset_id, error,
            )
            return None
        asset = created if isinstance(created, dict) else {**base, 'createdAt': now}
        self.report_standalone_operation(
            'standard', 'info', '已收藏入站表情包 素材=%s 大小=%dB 来源种类=%s 模型判定=%s',
            asset_id, len(payload), kind or 'sticker', 'yes' if guessed else 'no',
        )
        if given and isinstance(asset, dict) and asset.get('id') is not None:
            await self._index_sticker_description(asset.get('id'), given, [])
        return {**asset, '_payload': payload, '_described': bool(given)}

    async def _sticker_prior_by_hash(self, digest: str) -> Optional[dict[str, Any]]:
        """同内容已经在库里的那一行（**任何状态**）；没有回 `None`。

        抽出来是因为两条路径都要它，而且**判定的时机不同**：第一层在写盘前
        （`store_collected_sticker` 内部），第二层必须**在调模型之前**就问
        （§45.7："不要为同一张图付两次钱"）。
        """
        for row in await self.db_get('interlude_sticker', {}):
            if isinstance(row, dict) and row.get('hash') == digest:
                return row
        return None

    async def _revive_missing_sticker(self, prior: Any) -> None:
        """文件曾被删、同内容又回来了：只复活状态（描述还在，**不重花模型调用**）。

        `status != 'missing'` 时什么都不做——别的状态意味着"这一行好好的"，
        去重逻辑本来就会让调用方跳过它。
        """
        if not isinstance(prior, dict) or prior.get('status') != 'missing':
            return
        await self.db_set(
            'interlude_sticker', {'id': prior.get('id')},
            {'status': 'active' if prior.get('description') else 'pending', 'updatedAt': self.now()},
        )

    def _sticker_guess_prefetch_ok(self, source: str, config: Any) -> bool:
        """**不下载就能排除的**：来源自带的体积信息先卡一道（超过上限根本不下载）。

        **同步**（不是协程）：里面只有一次 `stat` 与一次长度乘法，没有 await 点。
        写成 `async def` 会让调用方拿到一个"永远为真"的协程对象——闸门静默失效
        （这个坑在实现时踩过一次，回归用例 `test_an_oversized_local_file_...` 钉着）。

        `http(s)` 来源在下载前拿不到体积（适配层给不出 content-length），只能等
        `_sticker_source_bytes()` 拿回来之后再判（那时 `store_collected_sticker`
        还有一道同样的上限）。这里能省的是**本地文件**与 **data URI** 这两种：
        前者一次 `stat`，后者按 base64 的上界算（每 4 字符解出 3 字节），不必解码。
        """
        max_bytes = float((config or {}).get('max_file_size_mb') or 10) * 1024 * 1024
        value = str(source if source is not None else '').strip()
        if not value:
            return False
        if value.startswith('data:image/'):
            return len(value) * 3 / 4 <= max_bytes
        if value.startswith('onebot-file:'):
            value = _local_sticker_path(value[len('onebot-file:'):])
        elif value.lower().startswith('file://'):
            value = _local_sticker_path(value)
        else:
            return True  # 网络来源：体积未知，交给下载后的校验
        try:
            return os.path.getsize(value) <= max_bytes
        except OSError:
            return True  # 读不到大小（文件没了 / 权限）→ 交给后面的字节校验去判

    def _sticker_guess_budget(self) -> bool:
        """这一分钟还能不能再花一次判定调用？（滑动窗口；超了回 `False` 且不记账）

        防的是刷图潮：一次发二十张图不该变成二十次识图调用（token 黑洞）。
        跨消息累计，与"每条消息最多几张"（`STICKER_GUESS_MAX_PER_MESSAGE`）正交。
        """
        now = self.now_ms()
        recent = [
            stamp for stamp in (getattr(self, '_sticker_guess_calls', None) or [])
            if now - stamp < STICKER_GUESS_WINDOW_MS
        ]
        if len(recent) >= STICKER_GUESS_MAX_PER_MINUTE:
            self._sticker_guess_calls = recent
            return False
        recent.append(now)
        self._sticker_guess_calls = recent
        return True

    def _warn_sticker_guess_unavailable(self) -> None:
        """节流 warn：开了第二层判据，却**没有**可用的识图模型。

        这是**能力缺失**（坑 25：需要用户看见的东西不许走 diagnostic）——用户打开开关
        是期待"表情包能自己进库"的，不报就等于静默失效。但一条接一条的图片不能各报一条。
        """
        now = self.now_ms()
        last = getattr(self, '_sticker_guess_warn_at', 0) or 0
        if now - last < STICKER_COLLECT_WARN_INTERVAL_MS:
            return
        self._sticker_guess_warn_at = now
        self.report_standalone(
            'warn', '已开启「识图模型判断普通图片」，但没有可用的识图模型；已跳过图片判定。',
        )

    async def guess_sticker_like(
        self, payload: bytes, mime_type: str, source: str = '',
    ) -> Optional[dict[str, Any]]:
        """**第二层判据**：问识图模型这张普通图片是不是表情包（`§45.7`）。

        调用前提（由 `collect_incoming_stickers` 保证）：入站种类是 `image`、
        `stickers.auto_collect_guess` 打开、预筛通过、去重没命中、节流还有额度。
        **它永远不能否决第一层**——第一层认的种类根本走不到这里。

        **任何失败一律回 `None`（= 不收）**：没有可用模型 / 超时 / 异常 / JSON 解析失败 /
        字段缺失 / 置信度不够。能力缺失打一条节流 warn；"判成照片 / 拿不准"只记 debug，
        不刷屏。收的充要条件在 `helpers.sticker_guess_result()` 一处。
        """
        describer = self.sticker_describer
        guesser = getattr(describer, 'guess_sticker', None)
        available = getattr(describer, 'guess_sticker_available', None)
        if not callable(guesser) or not callable(available):
            # 老描述器 / 没有这一层能力：按"能力缺失"处理（fail closed，绝不收）。
            self._warn_sticker_guess_unavailable()
            return None
        try:
            usable = bool(available())
        except Exception:  # noqa: BLE001 - 提供者内部异常按"不可用"处理
            usable = False
        if not usable:
            self._warn_sticker_guess_unavailable()
            return None
        try:
            visual = await self.image_bytes_to_native(payload, mime_type)
        except Exception as error:  # noqa: BLE001 - 转不成原生视觉输入 = 判不了
            self.report_standalone_operation(
                'diagnostic', 'debug', '图片判定跳过：图片转换失败 来源=%s 错误=%s',
                clip(source, 120), error,
            )
            return None
        if not visual:
            self.report_standalone_operation(
                'diagnostic', 'debug', '图片判定跳过：无法转成原生视觉输入 来源=%s',
                clip(source, 120),
            )
            return None
        try:
            raw = await asyncio.wait_for(
                guesser(
                    pick(visual, 'dataUri', 'data_uri'),
                    pick(visual, 'mimeType', 'mime_type'),
                    clip(source, 255),
                ),
                STICKER_GUESS_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 - 超时 / 网络 / 解析异常都按"不收"
            self.report_standalone_operation(
                'diagnostic', 'debug', '图片判定失败（按不收处理）来源=%s 错误=%s',
                clip(source, 120), error,
            )
            return None
        verdict = sticker_guess_result(raw)
        if not verdict:
            # 判成照片 / 截图 / 置信度不够 / 回执缺字段：**只记 debug，不刷屏**。
            self.report_standalone_operation(
                'diagnostic', 'debug', '图片判定不是表情包，未收藏 来源=%s 回执种类=%s',
                clip(source, 120), str(pick(raw if isinstance(raw, dict) else {}, 'kind') or ''),
            )
            return None
        self.report_standalone_operation(
            'standard', 'info', '图片判定为表情包 来源=%s 种类=%s 置信度=%.2f',
            clip(source, 120), verdict.get('kind'), float(verdict.get('confidence') or 0),
        )
        return verdict

    async def _sticker_source_bytes(self, source: str) -> Optional[bytes]:
        """按入站来源取原始字节：本地文件 → 适配器解析 → 远程抓取。

        读本地文件的口子**只对适配器给出来的来源开放**（`onebot-file:` / `file://`），
        与 `chunk3.fetch_native_image` 的信任边界同源；`onebot-url:` / `http(s)` 走
        `Transport.fetch_image`（`ctx.http_get` 是它的回退，由 `chunk3` 提供）。
        `text:`（正文里读出来的坐标，§46.8）一律不认：既不下载也不读盘。
        """
        value = source.strip()
        if not value:
            return None
        if value.startswith('text:') or value == 'text:':
            # 正文坐标的惰性前缀（与 `chunk3.TEXT_SOURCE_PREFIX` 同一个约定）。
            return None
        if value.startswith('data:image/'):
            match = re.match(r'^data:(image/[a-z0-9.+-]+);base64,([a-z0-9+/=\s]+)$', value, re.IGNORECASE)
            if not match:
                return None
            try:
                import base64 as _base64  # noqa: PLC0415 - 只在这条冷路径上需要

                return _base64.b64decode(match.group(2))
            except Exception:  # noqa: BLE001 - 坏 base64 按拿不到字节处理
                return None
        if value.startswith('onebot-file:'):
            return await self._read_sticker_local_file(_local_sticker_path(value[len('onebot-file:'):]))
        if value.lower().startswith('file://'):
            return await self._read_sticker_local_file(_local_sticker_path(value))
        if re.match(r'^https?://', value, re.IGNORECASE):
            fetcher = getattr(self.transport, 'fetch_image', None)
            if not callable(fetcher):
                return None
            try:
                data = await asyncio.wait_for(fetcher(value), STICKER_FETCH_TIMEOUT_SECONDS)
            except Exception as error:  # noqa: BLE001 - 网络失败 = 拿不到字节
                self.report_standalone_operation(
                    'diagnostic', 'debug', '入站表情包下载失败 错误=%s', error,
                )
                return None
            return bytes(data) if data else None
        # 既不是本地路径也不是 http —— 不猜（`onebot-file` 之外的自造前缀一律跳过）。
        return None

    async def _read_sticker_local_file(self, path: str) -> Optional[bytes]:
        """读本地图片文件；失败只记 debug（**绝不**把路径铺到标准频道）。"""
        if not path:
            return None
        try:
            return await asyncio.to_thread(_read_local_bytes, path)
        except Exception as error:  # noqa: BLE001 - 文件没了 / 权限不够都算拿不到
            self.report_standalone_operation(
                'diagnostic', 'debug', '入站表情包本地文件读取失败 错误=%s', error,
            )
            return None

    def _warn_sticker_collect_unavailable(self, count: int) -> None:
        """节流 warn：一批里有表情包但一个字节都没拿到。

        为什么不静默：这是**能力缺失**（没配 vision 之外的另一种缺失——拿不到字节），
        用户看不到就会以为"她怎么不收表情包"。但同一批十个表情包不能打十条。
        """
        now = self.now_ms()
        last = getattr(self, '_sticker_collect_warn_at', 0) or 0
        if now - last < STICKER_COLLECT_WARN_INTERVAL_MS:
            return
        self._sticker_collect_warn_at = now
        self.report_standalone(
            'warn', '收到 %d 个表情包，但拿不到图片字节（本地路径与 fetch_image 都不可用），已跳过收藏。',
            count,
        )

    # ------------------------------------------------------------------ #
    # 表情库资产的人工维护（本移植版新增 v1.8.0；控制台「表情库」页的写入路径）
    #
    # 这三条是**唯一**的写入路径：控制台不许自己拼 SQL / 自己写文件。
    # 纪律与 `console_api.set_config_value` 同源：白名单 + 先校验后写 + 写完即生效。
    # ------------------------------------------------------------------ #

    async def sticker_asset_row(self, row_id: Any) -> Optional[dict[str, Any]]:
        """按主键取一行素材；取不到回 `None`（调用方自己给 400）。"""
        if row_id is None:
            return None
        rows = await self.db_get('interlude_sticker', {'id': row_id}, {'limit': 1})
        for row in rows:
            if isinstance(row, dict):
                return row
        return None

    async def save_sticker_description(
        self, row_id: Any, description: Any, replace_aliases: bool = True,
    ) -> Optional[dict[str, Any]]:
        """人工写入一条描述，并**钉住**它不被自动扫描覆盖。

        为什么需要"钉住"：`scan_sticker_library()` 按"文件哈希没变 + 状态不是 active"
        重新登记。用户把一条已经描述过的素材**停用**再启用、或描述被清空，
        下一次扫描就会把它当"待描述"交给视觉模型 —— 于是用户手改的描述被模型顶掉。

        做法（受控偏离 §45.2，**排序式**而不是加锁标记）：

        * `descriptionManual = true` 是"这一条是人写的"的持久标记；
        * 扫描在描述前先看这个标记：已标记的素材**跳过模型调用**，直接按 `active` 登记；
        * 用户清空描述（传空串）时标记**保留** —— 清空也是人的意思，扫描不许替她编回去；
        * 想恢复自动描述就走 `restore_sticker_description()`（控制台按钮），
          它把标记摘掉并交给下一轮扫描。
        """
        row = await self.sticker_asset_row(row_id)
        if row is None:
            raise ValueError('找不到这条素材')
        text = str(description if description is not None else '').strip()
        patch: dict[str, Any] = {
            'description': text,
            'descriptionManual': True,
            'updatedAt': self.now(),
        }
        if replace_aliases:
            patch['aliases'] = []
        if text:
            # 有描述就说明它可用了：立刻进目录（`refresh_sticker_catalog` 只取 active）。
            patch['status'] = 'active' if row.get('status') != 'missing' else 'missing'
        await self.db_set('interlude_sticker', {'id': row_id}, patch)
        await self.refresh_sticker_catalog()
        return await self.sticker_asset_row(row_id)

    async def restore_sticker_description(self, row_id: Any) -> Optional[dict[str, Any]]:
        """摘掉"人写的"标记，让下一轮扫描用视觉模型重新描述。"""
        row = await self.sticker_asset_row(row_id)
        if row is None:
            raise ValueError('找不到这条素材')
        await self.db_set('interlude_sticker', {'id': row_id}, {
            'descriptionManual': False,
            'description': '',
            'aliases': [],
            'embedding': [],
            'status': 'pending',
            'updatedAt': self.now(),
        })
        await self.refresh_sticker_catalog()
        return await self.sticker_asset_row(row_id)

    async def rename_sticker(self, row_id: Any, name: Any) -> Optional[dict[str, Any]]:
        """改一个素材的短名（只影响界面，不进提示词的目录文本）。"""
        row = await self.sticker_asset_row(row_id)
        if row is None:
            raise ValueError('找不到这条素材')
        await self.db_set('interlude_sticker', {'id': row_id}, {
            'name': str(name if name is not None else '').strip(),
            'updatedAt': self.now(),
        })
        return await self.sticker_asset_row(row_id)

    async def set_sticker_disabled(self, row_id: Any, disabled: Any) -> Optional[dict[str, Any]]:
        """停用 / 启用一条素材：只改 `status`，**不动文件**。

        `disabled` 不在 `refresh_sticker_catalog` 的 `status='active'` 查询里，
        所以停用后它立刻从模型可见的目录消失；启用时按"有没有描述"回到
        `active` / `pending`（没描述的交给扫描补）。
        """
        row = await self.sticker_asset_row(row_id)
        if row is None:
            raise ValueError('找不到这条素材')
        if disabled is True:
            status = 'disabled'
        elif row.get('description'):
            status = 'active'
        else:
            status = 'pending'
        await self.db_set('interlude_sticker', {'id': row_id}, {
            'status': status, 'updatedAt': self.now(),
        })
        await self.refresh_sticker_catalog()
        return await self.sticker_asset_row(row_id)

    async def delete_sticker(
        self, row_id: Any, purge: bool = False, purge_file: bool = False,
    ) -> bool:
        """删除一条素材。

        **语义（控制台契约）：默认只标记不删文件。**

        * `purge=False`：行标成 `missing`（文件与描述都留着）。它立刻退出模型目录，
          重扫时如果文件还在会被复活 —— 这是"误删可恢复"的那条路。
        * `purge=True`：**连文件一起删**（行还是保留成 `missing`，留一个"这里曾经有东西"
          的痕迹，也让"同一个表情再被发一次"不会重新入库 —— 与参考插件的 orphan
          索引同一考虑）。

        文件删除由控制台侧（`console_api`）执行，因为它才是"路径必须落在表情库内"
        的那个校验点；服务层这里只维护数据。
        """
        row = await self.sticker_asset_row(row_id)
        if row is None:
            return False
        await self.db_set('interlude_sticker', {'id': row_id}, {
            'status': 'missing', 'updatedAt': self.now(),
        })
        await self.refresh_sticker_catalog()
        return True

    async def record_sticker_use(self, asset: Any) -> None:
        """投递成功后给素材的 `uses` 加一（控制台排序用）。

        计数失败**只 warn**：一次已经发出去的投递不该因为计数失败被判成失败。
        """
        asset_id = pick(asset, 'assetId', 'asset_id')
        if not asset_id:
            return
        try:
            rows = await self.db_get('interlude_sticker', {'assetId': asset_id}, {'limit': 1})
            row = rows[0] if rows and isinstance(rows[0], dict) else None
            if row is None:
                return
            current = row.get('uses')
            uses = int(current) + 1 if isinstance(current, int) and not isinstance(current, bool) else 1
            await self.db_set('interlude_sticker', {'id': row.get('id')}, {'uses': uses})
        except Exception as error:  # noqa: BLE001 - 计数是旁路
            self.report_standalone(
                'warn', '表情包用量计数失败 素材=%s 错误=%s', asset_id, error,
            )

    async def refresh_sticker_catalog(self) -> None:
        """上游 `refreshStickerCatalog()`（`src/service.ts:2386`）。"""
        rows = await self.db_get(
            'interlude_sticker', {'status': 'active'}, {'sort': {'updatedAt': 'desc'}},
        )
        self.sticker_catalog = [row for row in rows if isinstance(row, dict)]
        self.sticker_by_id = {
            row.get('assetId'): row for row in self.sticker_catalog if row.get('assetId')
        }

    def semantic_sticker_embedding_enabled(self) -> bool:
        """上游 `semanticStickerEmbeddingEnabled()`（`src/service.ts:2392`）。"""
        embedding = _config_section(_config_section(self.config, 'model'), 'embedding')
        return _config_value(embedding, 'semanticStickerFilter', 'semantic_sticker_filter') is True

    async def backfill_sticker_embeddings(self) -> None:
        """上游 `backfillStickerEmbeddings()`（`src/service.ts:2398`）。

        给「已描述但还没索引」的素材补向量，一批最多 8 条，避免一次扫描花掉
        一大把模型调用。
        """
        if not self.semantic_sticker_embedding_enabled():
            return
        pending = [
            asset for asset in self.sticker_catalog
            if isinstance(asset, dict)
            and asset.get('description')
            and not (asset.get('embedding') or [])
        ][:8]
        for asset in pending:
            text = ('%s %s' % (
                asset.get('description'), ' '.join(str(alias) for alias in (asset.get('aliases') or [])),
            )).strip()
            embedding = await self.embed_text(text)
            if embedding:
                await self.db_set(
                    'interlude_sticker', {'id': asset.get('id')},
                    {'embedding': embedding, 'updatedAt': self.now()},
                )

    async def sticker_catalog_for_session(
        self, session: Any, turn_query_embedding: Optional[list[float]] = None,
    ) -> list[dict[str, Any]]:
        """上游 `stickerCatalogForSession(...)`（`src/service.ts:2410`）。

        **输出键名逐字保持上游 camelCase**：这份目录会被原样塞进 prompt 的
        `stickerCatalog`，而 `systemPrompt` 明文要求模型回
        `localMedia: {"assetId": ...}`。改成 snake_case 会直接断掉协议。
        """
        config = self.sticker_config
        if not config.get('enabled') or session is None:
            return []
        if not is_one_bot_platform(pick(session, 'platform')):
            return []
        assets = await self.rank_sticker_assets(turn_query_embedding)
        return [
            {
                'assetId': asset.get('assetId'),
                'group': asset.get('group'),
                'description': asset.get('description'),
                'aliases': asset.get('aliases') if isinstance(asset.get('aliases'), list) else [],
                'animated': asset.get('animated'),
            }
            for asset in assets
            if isinstance(asset, dict)
        ]

    async def rank_sticker_assets(
        self, turn_query_embedding: Optional[list[float]] = None,
    ) -> list[dict[str, Any]]:
        """上游 `rankStickerAssets(...)`（`src/service.ts:2422`）。

        语义过滤关闭、这一回合没有查询向量、或目录本身没超过限额时，整份目录
        原样通过（**这是绝大多数回合的路径**）。
        """
        limit = _clamp_int(_config_number(self.sticker_config, 'catalogLimit', 'catalog_limit', 40), 1, 80)
        assets = [asset for asset in self.sticker_catalog[:limit] if isinstance(asset, dict)]
        embedding = _config_section(_config_section(self.config, 'model'), 'embedding')
        if _config_value(embedding, 'semanticStickerFilter', 'semantic_sticker_filter') is not True:
            return assets
        query = turn_query_embedding if isinstance(turn_query_embedding, list) else []
        if not query or len(assets) <= SEMANTIC_STICKER_LIMIT:
            return assets
        return rank_sticker_catalog(assets, query, SEMANTIC_STICKER_LIMIT)

    def semantic_turn_embedding_enabled(self) -> bool:
        """上游 `semanticTurnEmbeddingEnabled()`（`src/service.ts:2429`）。"""
        embedding = _config_section(_config_section(self.config, 'model'), 'embedding')
        return bool(should_request_turn_embedding(
            embedding,
            self.sticker_config.get('enabled') is True,
            len(self.sticker_catalog),
        ))

    # ------------------------------------------------------------------ #
    # 场景摘要与草稿裁剪（`src/service.ts:2440-2461`）
    # ------------------------------------------------------------------ #

    async def previous_scene_summaries(self, story_id: str) -> list[dict[str, Any]]:
        """上游 `previousSceneSummaries(storyId)`（`src/service.ts:2440`）。

        紧邻当前场景之前的几个已关闭场景的紧凑摘要：它们在原始上下文窗口与
        剧情弧之间搭桥，避免上一回合的细节从 prompt 里彻底消失。
        """
        memory = self.memory_config
        limit = _config_number(memory, 'previousSceneSummaries', 'previous_scene_summaries', 0)
        if not limit or _config_value(memory, 'enabled') is not True:
            return []
        rows = await self.db_get(
            'interlude_scene', {'storyId': story_id, 'status': 'closed'},
            {'limit': int(limit), 'sort': {'endedAt': 'desc'}},
        )
        summaries: list[dict[str, Any]] = []
        for scene in rows:
            if not isinstance(scene, dict):
                continue
            summary = scene.get('summary')
            ended_at = scene.get('endedAt')
            if not (isinstance(summary, str) and summary.strip()) or not ended_at:
                continue
            summaries.append({
                'started_at': iso(scene.get('startedAt')),
                'ended_at': iso(ended_at if ended_at is not None else scene.get('startedAt')),
                'summary': summary[:2000],
            })
        return summaries

    def prune_working_details(
        self, details: Any, now: Any,
    ) -> Optional[list[dict[str, Any]]]:
        """上游 `pruneWorkingDetails(details, now)`（`src/service.ts:2457`）。

        丢掉过期草稿并按 `slice(-10)` 保留最后 10 条；草稿只承载很小的在途事实，
        所以「过期即静默消失」是正确处理。
        """
        if not details:
            return None
        live = [
            item for item in details
            if isinstance(item, dict)
            and _expires_in_future(pick(item, 'expiresAt', 'expires_at'), now)
        ]
        return live[-10:] if live else None

    # ------------------------------------------------------------------ #
    # 历史语义召回（`src/service.ts:2466-2569`）
    # ------------------------------------------------------------------ #

    async def recall_history(
        self,
        story_id: str,
        participant_id: str,
        query: str,
        turn_query_embedding: Optional[list[float]],
        exclude_ids: Any,
        preferred_entry_ids: Any = None,
        max_results: int = 3,
    ) -> list[dict[str, Any]]:
        """上游 `recallHistory(...)`（`src/service.ts:2466`）。

        对整份不可变剧本做**多车道召回**：向量只提升排序，永远不是前置条件 ——
        字面措辞与来源链路让跨天记忆在向量回填尚未完成时依然可用。

        召回缓存条目的形状（内部结构，snake_case；`episode_index.EpisodeSource`
        也按这套键读）：``{'kind', 'participant_id', 'occurred_at', 'content',
        'vector', 'tags', 'spans', 'checkpoint', 'frame_id', 'embedding_identity'}``。
        """
        await self.ensure_history_vectors(story_id)
        cache = self.history_vectors.get(story_id)
        if not cache:
            return []
        exclude = exclude_ids if isinstance(exclude_ids, (set, frozenset)) else set(exclude_ids or [])
        preferred = (
            preferred_entry_ids
            if isinstance(preferred_entry_ids, (set, frozenset))
            else set(preferred_entry_ids or [])
        )
        query_embedding = turn_query_embedding if isinstance(turn_query_embedding, list) else []
        share_details = bool(participant_id) and bool(
            _config_value(self.shared_story_config, 'shareParticipantDetails', 'share_participant_details'),
        )
        cache_key = json.dumps(
            [participant_id, query, list(exclude), list(preferred), share_details],
            ensure_ascii=False,
        )
        recall_cache = getattr(self, 'automatic_recall_cache', None)
        memo = recall_cache.get(story_id) if isinstance(recall_cache, dict) else None
        if (
            max_results == 1
            and isinstance(memo, dict)
            and memo.get('source') is cache
            and memo.get('size') == len(cache)
            and memo.get('key') == cache_key
            and (memo.get('until') or 0) > _now_ms(self)
        ):
            return [
                {**item, 'source_entry_ids': list(item.get('source_entry_ids') or [])}
                for item in (memo.get('result') or [])
            ]

        scored: list[dict[str, Any]] = []
        keys = recall_keys(str(query))[:120]
        primary_keys = recall_keys(str(query).split('\n')[0])[:80]
        identity: Any = None
        embedder = getattr(self, 'embedder', None)
        identity_fn = getattr(embedder, 'identity', None)
        if callable(identity_fn):
            try:
                identity = identity_fn()
            except Exception:  # pragma: no cover - 提供者探测失败按"无身份"处理
                identity = None
        for entry_id, item in cache.items():
            if not isinstance(item, dict):
                continue
            if entry_id in exclude:
                continue
            if not is_history_entry_visible_to_participant(
                _visibility_probe(item), participant_id, share_details,
            ):
                continue
            embedding_identity = item.get('embedding_identity')
            if embedding_identity and embedding_identity != identity:
                semantic: Optional[float] = None
            else:
                semantic = cosine_similarity(query_embedding, item.get('vector') or [])
            spans = item.get('spans')
            if spans is None:
                spans = index_original(item.get('content') or '')
                item['spans'] = spans
            lexical = max(
                float(score_original(primary_keys, spans).get('score') or 0),
                float(score_original(keys, spans).get('score') or 0) * 0.8,
            )
            tagged = episode_tag_score(str(query), item.get('tags'))
            source = 1 if entry_id in preferred else 0
            if source == 0 and tagged == 0 and (semantic is None or semantic < 0.25) and lexical < 0.12:
                continue
            # Prose similarity alone often recalls a familiar gesture rather than
            # the event. Literal/source matches retain priority over writing style.
            scored.append({
                'id': entry_id,
                'score': (
                    source * 2
                    + tagged
                    + max(0.0, semantic if semantic is not None else 0.0)
                    * (0.45 if item.get('kind') == 'script' else 1)
                    + lexical
                ),
            })

        visible = [
            (entry_id, item) for entry_id, item in cache.items()
            if isinstance(item, dict)
            and entry_id not in exclude
            and is_history_entry_visible_to_participant(_visibility_probe(item), participant_id, share_details)
        ]
        visible.sort(key=lambda pair: (str(pair[1].get('occurred_at') or ''), pair[0]))
        episodes = build_episode_index(visible)
        positions = {entry_id: index for index, (entry_id, _item) in enumerate(visible)}
        used: set[int] = set()
        result: list[dict[str, Any]] = []
        for anchor in sorted(scored, key=lambda item: (-item['score'], -item['id'])):
            anchor_id = anchor['id']
            position = positions.get(anchor_id)
            if position is None or anchor_id in used:
                continue
            episode_ids = episodes.get(anchor_id) or [anchor_id]
            if len(episode_ids) > 1:
                neighborhood = [
                    (entry_id, item) for entry_id, item in visible if entry_id in episode_ids
                ]
            else:
                anchor_owner = (cache.get(anchor_id) or {}).get('participant_id')
                neighborhood = [
                    (entry_id, item) for entry_id, item in visible
                    if item.get('participant_id') == anchor_owner
                ]
            excerpt = episode_excerpt(
                neighborhood, anchor_id, 2400 if max_results == 1 else 4000, keys,
            )
            if not excerpt:
                continue
            source_entry_ids = list(excerpt.get('source_entry_ids') or [])
            for entry_id in source_entry_ids:
                used.add(entry_id)
            result.append({
                'id': anchor_id,
                'occurred_at': (cache.get(anchor_id) or {}).get('occurred_at'),
                'content': excerpt.get('content'),
                'source_entry_ids': source_entry_ids,
            })
            if len(result) >= max_results:
                break
        if max_results == 1:
            if not isinstance(recall_cache, dict):
                recall_cache = {}
                try:
                    self.automatic_recall_cache = recall_cache
                except Exception:  # pragma: no cover - 只读 host / 属性被占用
                    pass
            recall_cache[story_id] = {
                'source': cache,
                'size': len(cache),
                'key': cache_key,
                'until': _now_ms(self) + 60_000,
                'result': result,
            }
        return result

    async def ensure_history_vectors(self, story_id: str) -> None:
        """上游 `ensureHistoryVectors(storyId)`（`src/service.ts:2539`）。

        把某个剧本里所有可召回条目一次载入召回缓存。载入刻意是**整表**的（没有
        时间窗）：更早的记忆必须仍然可检索，而进程内缓存让代价变成每剧本一次。

        单飞：并发的冷启动调用共用同一个载入任务；载入期间新写进缓存的活动条目
        （`cache.has(row.id)`）不会被覆盖。
        """
        pending = self.history_vector_loads.get(story_id)
        if pending is not None:
            await pending
            return
        if story_id in self.history_vectors_ready:
            return
        cache: dict[int, dict[str, Any]] = {}
        self.history_vectors[story_id] = cache

        async def load() -> None:
            try:
                rows = await self.db_get('interlude_script_entry', {'storyId': story_id})
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if row.get('kind') not in RECALLABLE_ENTRY_KINDS:
                        continue
                    entry_id = row.get('id')
                    if entry_id in cache:
                        continue  # a live append/backfill won the race
                    metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
                    embedding_identity = pick(metadata, 'embeddingIdentity', 'embedding_identity')
                    frame_id = pick(metadata, 'frameId', 'frame_id')
                    tags: list[str] = []
                    for values in grounded_episode_tags(
                        row.get('content') or '',
                        pick(metadata, 'episodeTags', 'episode_tags') or {},
                    ).values():
                        tags.extend(values)
                    entry: dict[str, Any] = {
                        'embedding_identity': embedding_identity if isinstance(embedding_identity, str) else None,
                        'tags': tags,
                        'checkpoint': pick(metadata, 'sceneCheckpoint', 'scene_checkpoint'),
                        'frame_id': frame_id if isinstance(frame_id, str) else None,
                        'content': prompt_visible_message_content(
                            row.get('content'), recent_script_ownership(row),
                        ),
                        'occurred_at': iso(row.get('occurredAt')),
                        'participant_id': row.get('participantId'),
                        'kind': row.get('kind'),
                    }
                    embedding = row.get('embedding')
                    if isinstance(embedding, list) and embedding:
                        entry['vector'] = embedding
                    cache[entry_id] = entry
                if self.history_vectors.get(story_id) is cache:
                    self.history_vectors_ready.add(story_id)
            except Exception as error:
                self.history_vectors_ready.discard(story_id)
                self.report_standalone_operation(
                    'diagnostic', 'debug', '历史向量缓存加载失败 错误=%s', error,
                )

        task = asyncio.ensure_future(load())
        self.history_vector_loads[story_id] = task
        try:
            await task
        finally:
            if self.history_vector_loads.get(story_id) is task:
                self.history_vector_loads.pop(story_id, None)
