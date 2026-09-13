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

键名约定（`docs/PORT_PLAN.md` §2，本文件里逐处落实）：

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
    SEMANTIC_STICKER_LIMIT,
    calibrated_native_face_willingness,
    clip,
    cosine_similarity,
    describe_quoted_message,
    extract_session_audio_sources,
    extract_session_file_facts,
    format_group_speaker,
    normalize_allowed_native_faces,
    normalize_allowed_reactions,
    normalize_expression_threshold,
    normalize_quoted_message_context,
    rank_sticker_catalog,
    should_supersede_narrative_request,
    stable_sticker_asset_id,
)
#: 上游 `normalizeVisibleMessageContent`（`src/service.ts:7649`，模块级导出函数）。
#: `helpers.py` 把它实现成下划线私有（同文件里由 `normalizeGroupVisibleReply` 使用），
#: 但 Chunk2 的流式早发路径同样需要它，这里按上游语义直接复用同一实现，避免两份漂移。
from .helpers import _normalize_visible_message_content as normalize_visible_message_content
from .transport import NullTransport

__all__ = [
    'ServiceChunk2',
    'group_message_ref',
    'sticker_mime',
    'targetable_message_id',
]


# =========================================================================== #
# 本文件需要的模块级纯函数
# =========================================================================== #

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
        root = os.path.abspath(os.path.join(
            str(getattr(self.ctx, 'base_dir', '') or ''), str(self.sticker_config.get('directory') or ''),
        ))
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
        segments = self.split_outgoing_message(content)
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
                    {'index': index, 'content': segment, 'status': 'failed', 'reason': 'transport-unavailable'}
                    for index, segment in enumerate(segments)
                ],
            }
        all_delivered = True
        delivered_segments: list[str] = []
        segment_outcomes: list[dict[str, Any]] = []
        for index, segment in enumerate(segments):
            reply_to = reply_to_message_id if index == 0 and reply_to_message_id else None
            try:
                result = await send(channel_id, segment, reply_to)
                if not isinstance(result, dict) or result.get('ok') is not True:
                    reason = pick(result, 'error') if isinstance(result, dict) else None
                    raise RuntimeError(str(reason or 'group-delivery-failed'))
                delivered_segments.append(segment)
                segment_outcomes.append({'index': index, 'content': segment, 'status': 'delivered'})
            except Exception as error:
                all_delivered = False
                segment_outcomes.append({
                    'index': index, 'content': segment, 'status': 'failed',
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
            turn.get('in_flight_request_id'),
            turn.get('first_message_committed_request_id'),
            turn.get('obsolete_request_ids') or set(),
        ):
            turn.setdefault('obsolete_request_ids', set()).add(turn.get('in_flight_request_id'))
            self.report_operation(
                'standard', 'info', story, 'user-message',
                '新消息到达且首条回复尚未提交，放弃旧请求 参与者=%s 请求=%d',
                pick(participant, 'id'), turn.get('in_flight_request_id') or 0,
            )
        message: dict[str, Any] = {
            'content': content,
            'occurred_at': now,
            'superseded_intents': superseded_intents,
            'image_sources': list(image_sources or []),
            'audio_sources': list(audio_sources or []),
        }
        if quote:
            message['quote'] = quote
        turn.setdefault('messages', []).append(message)
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
        if not should_supersede_narrative_request(
            turn.get('in_flight_request_id'),
            turn.get('first_message_committed_request_id'),
            turn.get('obsolete_request_ids') or set(),
        ):
            return
        turn.setdefault('obsolete_request_ids', set()).add(turn.get('in_flight_request_id'))
        self.report_operation(
            'standard', 'info', story, 'user-message',
            '新消息到达且首条回复尚未提交，放弃旧请求 参与者=%s 请求=%d',
            participant_id, turn.get('in_flight_request_id') or 0,
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
        if turn.get('next_revision') != request_id:
            return False
        if request_id in (turn.get('obsolete_request_ids') or set()):
            return False
        if turn.get('first_message_committed_request_id') == request_id:
            return False
        if not self.can_handle_participant(participant):
            return False
        # 早发内容与常规投递共用同一可见文本合约：长度上限与括号表情标签清理一致。
        content = normalize_visible_message_content(
            reply_body.get('content'),
            _message_characters(self.runtime_config),
            _config_value(self.runtime_config, 'messageSeparator', 'message_separator', '<sep/>'),
        )
        if not content or len(self.split_outgoing_message(content)) != 1:
            return False
        participant_id = pick(participant, 'id')
        delivered = await self.send_outgoing_messages(story, [{
            'participant_id': participant_id,
            'content': content,
            'interaction': interaction,
            'user_initiated': True,
        }], participant, session)
        if not delivered:
            return False
        confirmed = await self.confirm_outgoing_deliveries(story, delivered)
        if not confirmed:
            return False
        turn['first_message_committed_request_id'] = request_id
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
        content = visual.get('content') or ''
        if not content:
            if sources:
                content = '[用户发送了图片；图片内容以本轮视觉输入为准，未提供视觉内容时保持未知。]'
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
            root = os.path.abspath(os.path.join(
                str(getattr(self.ctx, 'base_dir', '') or ''), str(config.get('directory') or ''),
            ))
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
                        'description': '',
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
                item_asset = item['asset']
                item_id = item_asset.get('id')
                description: Any = None
                try:
                    visual = await self.image_bytes_to_native(item['bytes'], item_asset.get('mimeType'))
                    if visual:
                        # `imageBytesToNative` 的移植版按内部结构约定输出
                        # `mime_type` / `data_uri`（`types.NarrativeImage`），双读兼容。
                        description = await self.sticker_describer.describe_sticker(
                            pick(visual, 'dataUri', 'data_uri'),
                            pick(visual, 'mimeType', 'mime_type'),
                            item_asset.get('filePath'),
                            item_asset.get('animated'), config.get('description_response_format'),
                            config.get('description_max_tokens'),
                        )
                except Exception as error:
                    self.report_standalone_operation(
                        'standard', 'warn', '表情包描述失败，已冷却后重试 素材=%s 错误=%s',
                        item_asset.get('assetId'), error,
                    )
                    await self.db_set('interlude_sticker', {'id': item_id}, {'updatedAt': self.now()})
                    continue
                if not description:
                    self.report_standalone_operation(
                        'standard', 'warn', '表情包描述未返回可用 JSON，已冷却后重试 素材=%s',
                        item_asset.get('assetId'),
                    )
                    await self.db_set('interlude_sticker', {'id': item_id}, {'updatedAt': self.now()})
                    continue
                updated = await self.db_set('interlude_sticker', {'id': item_id}, {
                    'description': pick(description, 'description'),
                    'aliases': pick(description, 'aliases'),
                    'status': 'active',
                    'updatedAt': self.now(),
                })
                if updated and self.semantic_sticker_embedding_enabled():
                    # Index a freshly described asset immediately so the semantic filter
                    # can consider it on the very next turn instead of the next scan.
                    aliases = pick(description, 'aliases') or []
                    embedding = await self.embed_text(
                        ('%s %s' % (pick(description, 'description'), ' '.join(str(a) for a in aliases))).strip(),
                    )
                    if embedding:
                        await self.db_set(
                            'interlude_sticker', {'id': item_id},
                            {'embedding': embedding, 'updatedAt': self.now()},
                        )
                self.report_standalone_operation(
                    'standard', 'info', '表情包描述完成 素材=%s 分组=%s',
                    item_asset.get('assetId'), item_asset.get('group'),
                )
            await self.refresh_sticker_catalog()
            await self.backfill_sticker_embeddings()
            await self.refresh_sticker_catalog()
        except Exception as error:
            self.report_standalone('warn', '表情包库扫描失败：%s', error)
        finally:
            self.sticker_scan_running = False

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
