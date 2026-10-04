"""上游 1.0.1-rc31：群聊历史图片证据回流。

上游语义（逐条对账 `upstream/src/service.ts:2007-2100`（落库）、`2454-2491`
（`groupMessages` → `imageRefs`）、`2498-2517`（`loadHistoricalGroupImages`）、
`2328-2364`（群回合接线）、`9780-9791`（`groupImageRefsForStorage`）、
`upstream/src/narrator.ts:1923-1931`（`currentEvent` 字段）与
`upstream/docs/GROUP_HISTORICAL_IMAGE_CONTEXT_DESIGN.md`）：

* 群聊图片引用写进 `interlude_script_entry.metadata.groupImageRefs`
  （`{source, ordinal, sourceType}`），**不再只留 `[图片]` 占位符**；
* **只存 URL / OneBot file 引用**，`data:image/…` 永不入库；
* `mention-only` 下未 @ 机器人的图片**落库但不触发主叙事**（不进 debounce 队列、
  不耗意愿、不查冷却、不调模型）；
* 下一次真实进入群聊主叙事时，从**同一群**历史条目按新到旧扫，默认恢复最近
  `historicalImageLimit` 张（默认 3，**0 = 关闭**）；
* 当前回合图片与历史图片**按来源去重**，历史图片用**低细节**提示；
* 历史引用按时间**正序**返回（模型从较早读到较新）；
* 单张加载失败只记一条可行动 warn，**不阻塞本回合**；
* 私聊行为不变；历史图片只作为**证据**（`currentEvent.historicalImageCount` +
  来源元数据），不构成新消息或新指令。

运行：`python3 -m unittest plugin.tests.test_group_historical_images -v`
"""

from __future__ import annotations

import json
import unittest
from datetime import timedelta
from typing import Any

from plugin.core.narrator_prompts import to_prompt_payload
from plugin.core.service.chunk1 import ServiceChunk1
from plugin.core.service.helpers import (
    DEFAULT_HISTORICAL_IMAGE_LIMIT,
    group_image_refs_for_storage,
    normalize_stored_group_image_ref,
)
from plugin.tests.test_narrator_prompts import request as prompt_request
from plugin.tests.test_service_chunk1 import (  # noqa: E402 - 复用共享夹具（既有惯例）
    PHOTO_MEDIA,
    PHOTO_TAG,
    PRIVATE_STORY_ID,
    SHARED_STORY_ID,
    STORY_TIME,
    ServiceHarness,
    group_rule_stub,
    group_session,
    image_media,
    onebot_config,
)

DATA_URI = 'data:image/png;base64,' + 'A' * 64
IMAGE_A = 'https://cdn.example.com/photo.png'  # 与共享夹具 PHOTO_MEDIA / PHOTO_TAG 同一张图
IMAGE_B = 'https://cdn.example.com/b.png'
IMAGE_C = 'https://cdn.example.com/c.png'


def _ref(source: str, ordinal: int = 0) -> dict:
    return {'source': source, 'ordinal': ordinal, 'sourceType': 'url'}


class Harness(ServiceHarness):
    """群聊夹具：把 `receive_group` / `flush_group_turn` 之间的那条链拼起来。"""

    def _config(self, rule: dict[str, Any] | None = None, vision: bool = False) -> dict[str, Any]:
        return onebot_config(
            onebot={'groupChats': [rule or group_rule_stub(responseMode='always')]},
            model={'vision': {'enabled': vision}},
        )

    def _group_entries(self, story_id: str = SHARED_STORY_ID) -> list[dict[str, Any]]:
        return [
            row for row in self.rows('interlude_script_entry', {'storyId': story_id})
            if row['kind'] == 'group-message'
        ]

    def _history_entry(
        self, sources: list[str], occurred_at: Any = None, entry_id: int | None = None,
        group_id: str = '9', sender_id: str = '2', sender_name: str = '成员',
        message_id: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            'groupId': group_id, 'senderId': sender_id, 'senderName': sender_name,
            'imageCount': len(sources),
            'groupImageRefs': [_ref(source, index) for index, source in enumerate(sources)],
        }
        if message_id:
            metadata['messageId'] = message_id
        return self.make_entry(
            story_id=SHARED_STORY_ID, kind='group-message', content='[图片]',
            occurred_at=occurred_at or STORY_TIME, entry_id=entry_id, metadata=metadata,
        )

    def _prepare_turn(
        self, rule: dict[str, Any], seen: dict[str, Any], batch: list[dict[str, Any]] | None = None,
        vision: bool = False,
    ) -> Any:
        """一个待刷出的群回合 + 只捕获 `try_decide` 参数的一圈替身。"""
        service = self.make_service(self._config(rule, vision=vision))
        self.make_story(SHARED_STORY_ID)
        service.buffered_group_turns['key'] = {
            'story_id': SHARED_STORY_ID, 'group_id': '9', 'rule': rule,
            'channel_id': '9', 'latest_session': group_session(channel_id='9'),
            'messages': list(batch if batch is not None else [{'content': '看这个'}]),
            'revision': 3, 'mentioned_bot': True, 'quoted_bot': False,
        }
        service.schedule_compaction = lambda _story_id: None
        service.schedule_conversation_follow_ups_after_turn = lambda *_a, **_k: None
        service.update_script_delivery_outcome = lambda *_a, **_k: None
        service.persist_decision = _none_result
        service.send_group_message = _no_segments
        service.semantic_turn_embedding_enabled = lambda: False
        service.sticker_catalog_for_session = _empty
        service.group_chat_capabilities = lambda _session, _messages: None
        service.main_model_label = lambda: 'test'
        service.group_cooldown_active = _false

        async def decide(*args: Any, **_kwargs: Any) -> dict[str, Any]:
            seen['args'] = args
            return {'decision': {'groupReply': {'mode': 'none'}}, 'succeeded': True}

        service.try_decide = decide
        return service


async def _none_result(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {'messages': [], 'commit': None, 'scriptEntry': None, 'script_entry': None}


async def _no_segments(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {'deliveredSegments': [], 'complete': True, 'segmentOutcomes': []}


async def _empty(*_args: Any, **_kwargs: Any) -> list[Any]:
    return []


async def _false(*_args: Any, **_kwargs: Any) -> bool:
    return False


# =========================================================================== #
# ① 落库：metadata.groupImageRefs（不存 data URI）
# =========================================================================== #

class LedgerTests(Harness):
    """① 群聊收到图片 → 落 `metadata.groupImageRefs`（断言具体字段与值）。"""

    async def test_a_group_image_is_persisted_as_a_reloadable_reference(self) -> None:
        service = self.make_service(self._config())
        self.make_story(SHARED_STORY_ID)
        self.assertTrue(await service.receive_group(
            group_session(content=PHOTO_TAG, media=[PHOTO_MEDIA]), STORY_TIME,
        ))
        entries = self._group_entries()
        self.assertEqual(len(entries), 1)
        metadata = entries[0]['metadata']
        self.assertEqual(metadata['imageCount'], 1)
        self.assertEqual(metadata['groupImageRefs'], [
            {'source': IMAGE_A, 'ordinal': 0, 'sourceType': 'url'},
        ])
        # 正文仍保留 `[图片]` 占位（上游 `describeGroupAttachments` 没变）：证据是
        # **额外的 metadata**，不是把占位符删掉。
        self.assertEqual(entries[0]['content'], '[图片]')

    async def test_an_onebot_file_reference_is_stored_as_a_file_source(self) -> None:
        service = self.make_service(self._config())
        self.make_story(SHARED_STORY_ID)
        await service.receive_group(
            group_session(content=PHOTO_TAG, media=[image_media('onebot-file:cache-key.png')]),
            STORY_TIME,
        )
        refs = self._group_entries()[0]['metadata']['groupImageRefs']
        self.assertEqual(refs, [
            {'source': 'onebot-file:cache-key.png', 'ordinal': 0, 'sourceType': 'file'},
        ])

    async def test_a_data_uri_is_never_written_to_the_persistent_layer(self) -> None:
        """持久化层只存 URL / OneBot file 引用：`data:image/…` 一个字节都不落库。"""
        service = self.make_service(self._config())
        self.make_story(SHARED_STORY_ID)
        await service.receive_group(
            group_session(content=PHOTO_TAG, media=[image_media(DATA_URI, 'image', raw={'file': 'x'})]),
            STORY_TIME,
        )
        metadata = self._group_entries()[0]['metadata']
        # 张数照数（模型仍要知道"他发了 N 张"），但引用表里没有它。
        self.assertEqual(metadata['imageCount'], 1)
        self.assertEqual(metadata['groupImageRefs'], [])
        self.assertNotIn('data:image/', json.dumps(metadata))

    async def test_the_storage_filter_skips_empty_and_oversized_values(self) -> None:
        oversized = 'https://cdn.example.com/' + 'x' * (8 * 1024 * 1024)
        self.assertEqual(group_image_refs_for_storage([
            '', '   ', DATA_URI, oversized, IMAGE_A, 'onebot-file:',
        ]), [{'source': IMAGE_A, 'ordinal': 4, 'sourceType': 'url'}])
        # 读端用**同一套**判据再收一遍（老库 / 手改过的 metadata）。
        self.assertIsNone(normalize_stored_group_image_ref({'source': DATA_URI, 'sourceType': 'url'}))
        self.assertIsNone(normalize_stored_group_image_ref({'source': IMAGE_A}))
        self.assertEqual(
            normalize_stored_group_image_ref({'source': IMAGE_A, 'sourceType': 'url'}),
            {'source': IMAGE_A, 'ordinal': None, 'sourceType': 'url'},
        )

    async def test_no_image_means_no_refs_key_at_all(self) -> None:
        service = self.make_service(self._config())
        self.make_story(SHARED_STORY_ID)
        await service.receive_group(group_session(content='纯文本'), STORY_TIME)
        metadata = self._group_entries()[0]['metadata']
        self.assertNotIn('imageCount', metadata)
        self.assertNotIn('groupImageRefs', metadata)


# =========================================================================== #
# ②③④⑤ 选择器与群回合接线
# =========================================================================== #

class SelectorTests(Harness):
    """②-⑤：单选器（`load_historical_group_images`）逐条对账上游 `service.ts:2498`。"""

    async def test_history_is_scanned_newest_first_and_returned_oldest_first(self) -> None:
        """② 下一回合进主叙事 → 历史图片真的进上下文（具体条数 + 正序 + 低细节）。"""
        service = self.make_service(self._config(vision=True))
        self.make_story(SHARED_STORY_ID)
        self._history_entry([IMAGE_A], STORY_TIME - timedelta(hours=3), entry_id=1, message_id='-1')
        self._history_entry([IMAGE_B], STORY_TIME - timedelta(hours=2), entry_id=2)
        self._history_entry([IMAGE_C], STORY_TIME - timedelta(hours=1), entry_id=3)

        fetched: list[str] = []

        async def fetch(source: str, _bot: Any = None) -> dict[str, Any]:
            fetched.append(source)
            return {'mimeType': 'image/png', 'dataUri': 'data:image/png;base64,%s' % source[-5:]}

        service.fetch_native_image = fetch
        # 选择器拿到的引用是**新到旧**（`group_messages_snapshot` 的排序）。
        images = await service.load_historical_group_images(
            {'id': SHARED_STORY_ID}, [{'source': IMAGE_C}, {'source': IMAGE_B}, {'source': IMAGE_A}],
            DEFAULT_HISTORICAL_IMAGE_LIMIT, None, [],
        )
        # 上游 `service.ts:2498-2517`：先按"新到旧"逐张取（id 按取用序 1..N），
        # 返回前整体 `reverse()` → **由旧到新**交给模型（它从较早的图读到较新的）。
        self.assertEqual(fetched, [IMAGE_C, IMAGE_B, IMAGE_A])
        self.assertEqual(len(images), 3)
        self.assertEqual([item['dataUri'] for item in images],
                         ['data:image/png;base64,%s' % IMAGE_A[-5:],
                          'data:image/png;base64,%s' % IMAGE_B[-5:],
                          'data:image/png;base64,%s' % IMAGE_C[-5:]])
        self.assertEqual([item['id'] for item in images], [
            'group-history-image-3', 'group-history-image-2', 'group-history-image-1',
        ])
        self.assertTrue(all(item['detail'] == 'low' for item in images),
                        '历史图片是旧证据：低细节提示')

    async def test_the_selector_takes_exactly_the_configured_count(self) -> None:
        service = self.make_service(self._config(vision=True))
        self.make_story(SHARED_STORY_ID)

        async def fetch(_source: str, _bot: Any = None) -> dict[str, Any]:
            return {'mimeType': 'image/png', 'dataUri': 'data:image/png;base64,QQ=='}

        service.fetch_native_image = fetch
        refs = [{'source': IMAGE_A}, {'source': IMAGE_B}, {'source': IMAGE_C}]
        for limit, expected in ((1, 1), (2, 2), (5, 3)):
            with self.subTest(limit=limit):
                images = await service.load_historical_group_images(
                    {'id': SHARED_STORY_ID}, refs, limit, None, [],
                )
                self.assertEqual(len(images), expected)
        # v1.9.7：**不替用户拍上界** —— 上游的 `Math.min(6, …)` 已撤掉，配多少读多少。
        more = [{'source': 'https://cdn.example.com/%d.png' % index} for index in range(9)]
        self.assertEqual(
            len(await service.load_historical_group_images(
                {'id': SHARED_STORY_ID}, more, 9, None, [],
            )),
            9,
        )

    async def test_the_current_turn_images_win_and_history_is_deduplicated(self) -> None:
        """③ 与当前回合图片重复 → 去重（只剩一份）。"""
        service = self.make_service(self._config(vision=True))
        self.make_story(SHARED_STORY_ID)
        fetched: list[str] = []

        async def fetch(source: str, _bot: Any = None) -> dict[str, Any]:
            fetched.append(source)
            return {'mimeType': 'image/png', 'dataUri': 'data:image/png;base64,QQ=='}

        service.fetch_native_image = fetch
        images = await service.load_historical_group_images(
            {'id': SHARED_STORY_ID},
            [{'source': IMAGE_A}, {'source': IMAGE_B}, {'source': IMAGE_B}],
            3, None, [IMAGE_A],
        )
        # A 已经是当前回合的图 → 不回流；B 重复引用两次 → 只取一份。
        self.assertEqual(fetched, [IMAGE_B])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]['id'], 'group-history-image-1')

    async def test_zero_means_off(self) -> None:
        """④ `historicalImageLimit=0` → 不回流（一张都不取，连候选都不扫）。"""
        service = self.make_service(self._config(vision=True))
        self.make_story(SHARED_STORY_ID)
        fetched: list[str] = []

        async def fetch(source: str, _bot: Any = None) -> dict[str, Any]:
            fetched.append(source)
            return {'mimeType': 'image/png', 'dataUri': 'data:image/png;base64,QQ=='}

        service.fetch_native_image = fetch
        self.assertEqual(
            await service.load_historical_group_images(
                {'id': SHARED_STORY_ID},
                [{'source': IMAGE_A}], 0, None, [],
            ),
            [],
        )
        self.assertEqual(fetched, [], '0 是"关闭回流"，不是"取 0 张再往下走"')

    async def test_vision_being_off_means_no_history_at_all(self) -> None:
        service = self.make_service(self._config(vision=False))
        self.make_story(SHARED_STORY_ID)
        self.assertEqual(
            await service.load_historical_group_images(
                {'id': SHARED_STORY_ID}, [{'source': IMAGE_A}], 3, None, [],
            ),
            [],
        )

    async def test_a_failed_load_warns_and_never_blocks_the_turn(self) -> None:
        """⑤ 历史图片加载失败 → 本回合照常跑完 + 一条可行动的 warn。"""
        service = self.make_service(self._config(vision=True))
        self.make_story(SHARED_STORY_ID)

        async def explode(_source: str, _bot: Any = None) -> dict[str, Any]:
            raise RuntimeError('getImage 超时')

        service.fetch_native_image = explode
        images = await service.load_historical_group_images(
            {'id': SHARED_STORY_ID}, [{'source': IMAGE_A}], 3, None, [],
        )
        self.assertEqual(images, [])
        text = self.sink.text()
        self.assertIn('历史群聊图片读取失败', text)
        self.assertIn('本回合照常写作', text)
        self.assertIn('getImage 超时', text)

    async def test_a_missing_result_also_warns(self) -> None:
        """取回结果是空（端点不可用 / 图过期 / 坐标不可信）也要留痕，不许静默跳过。"""
        service = self.make_service(self._config(vision=True))
        self.make_story(SHARED_STORY_ID)

        async def nothing(_source: str, _bot: Any = None) -> None:
            return None

        service.fetch_native_image = nothing
        self.assertEqual(
            await service.load_historical_group_images(
                {'id': SHARED_STORY_ID}, [{'source': IMAGE_A}], 3, None, [],
            ),
            [],
        )
        self.assertIn('历史群聊图片读取失败', self.sink.text())


class TurnWiringTests(Harness):
    """②-④ 回合级：选择器真的接在 `flush_group_turn` → `try_decide` 这条线上。"""

    async def test_history_reaches_the_group_narrative_request(self) -> None:
        seen: dict[str, Any] = {}
        service = self._prepare_turn(
            group_rule_stub(responseMode='always'), seen, vision=True,
        )
        self._history_entry([IMAGE_A], STORY_TIME - timedelta(hours=2), entry_id=1, message_id='-7')
        self._history_entry([IMAGE_B], STORY_TIME - timedelta(hours=1), entry_id=2)

        async def fetch(_source: str, _bot: Any = None) -> dict[str, Any]:
            return {'mimeType': 'image/png', 'dataUri': 'data:image/png;base64,QQ=='}

        service.fetch_native_image = fetch
        await service.flush_group_turn('key', 3)

        historical = seen['args'][19]
        # 新到旧取（entry 2 先取 → id 1；entry 1 后取 → id 2），返回前反转 → 由旧到新。
        self.assertEqual([item['id'] for item in historical],
                         ['group-history-image-2', 'group-history-image-1'])
        self.assertEqual([item['sourceEntryId'] for item in historical], [1, 2])
        self.assertEqual(historical[0]['sourceEntryId'], 1)
        self.assertEqual(historical[0]['messageId'], '-7')
        self.assertEqual(historical[0]['senderId'], '2')
        self.assertEqual(historical[0]['senderName'], '成员')
        self.assertTrue(all(item['detail'] == 'low' for item in historical))
        # 群上下文的可见消息也只有这批行（同一次读取）。
        self.assertEqual(seen['args'][8]['groupId'], '9')
        self.assertEqual(seen['args'][9], [], '本回合没有当前图片 → images 为空')

    async def test_the_limit_is_read_from_the_group_rule(self) -> None:
        seen: dict[str, Any] = {}
        service = self._prepare_turn(
            group_rule_stub(responseMode='always', historicalImageLimit=1), seen,
            vision=True,
        )
        self._history_entry([IMAGE_A], STORY_TIME - timedelta(hours=2), entry_id=1)
        self._history_entry([IMAGE_B], STORY_TIME - timedelta(hours=1), entry_id=2)

        async def fetch(_source: str, _bot: Any = None) -> dict[str, Any]:
            return {'mimeType': 'image/png', 'dataUri': 'data:image/png;base64,QQ=='}

        service.fetch_native_image = fetch
        await service.flush_group_turn('key', 3)
        self.assertEqual(len(seen['args'][19]), 1)
        self.assertEqual(seen['args'][19][0]['sourceEntryId'], 2, '新到旧取最近一张')

    async def test_limit_zero_keeps_the_turn_running_without_history(self) -> None:
        seen: dict[str, Any] = {}
        service = self._prepare_turn(
            group_rule_stub(responseMode='always', historicalImageLimit=0), seen,
            vision=True,
        )
        self._history_entry([IMAGE_A], STORY_TIME - timedelta(hours=1), entry_id=1)

        async def fetch(_source: str, _bot: Any = None) -> dict[str, Any]:
            raise AssertionError('0 = 关闭回流，不许去取图')

        service.fetch_native_image = fetch
        await service.flush_group_turn('key', 3)
        self.assertEqual(seen['args'][19], [])
        self.assertIn('群聊消息准备进入主叙事', self.sink.text())

    async def test_a_missing_key_falls_back_to_the_default_three(self) -> None:
        """缺键 = 上游默认 3（AstrBot 的 schema 不给 list 元素补默认值，所以要有 fallback）。"""
        rule = group_rule_stub(responseMode='always')
        rule.pop('historicalImageLimit', None)
        seen: dict[str, Any] = {}
        service = self._prepare_turn(rule, seen, vision=True)
        for index in range(4):
            self._history_entry(
                ['https://cdn.example.com/%d.png' % index],
                STORY_TIME - timedelta(hours=4 - index), entry_id=index + 1,
            )

        async def fetch(_source: str, _bot: Any = None) -> dict[str, Any]:
            return {'mimeType': 'image/png', 'dataUri': 'data:image/png;base64,QQ=='}

        service.fetch_native_image = fetch
        await service.flush_group_turn('key', 3)
        self.assertEqual(len(seen['args'][19]), DEFAULT_HISTORICAL_IMAGE_LIMIT)
        self.assertEqual(DEFAULT_HISTORICAL_IMAGE_LIMIT, 3)

    async def test_history_comes_only_from_the_same_group(self) -> None:
        """同一群之外的历史（别的群 / 私聊）一张都不许回流。"""
        service = self.make_service(self._config(vision=True))
        self.make_story(SHARED_STORY_ID)
        self._history_entry([IMAGE_A], STORY_TIME - timedelta(hours=1), entry_id=1, group_id='77')
        self.make_entry(
            story_id=SHARED_STORY_ID, kind='user-message', content='私聊图',
            metadata={'groupImageRefs': [_ref(IMAGE_B)]}, occurred_at=STORY_TIME,
        )
        self.make_entry(
            story_id=PRIVATE_STORY_ID, kind='group-message', content='[图片]',
            metadata={'groupId': '9', 'groupImageRefs': [_ref(IMAGE_C)]}, occurred_at=STORY_TIME,
        )
        snapshot = await service.group_messages_snapshot(SHARED_STORY_ID, '9', 20)
        self.assertEqual(snapshot['imageRefs'], [], '别的群 / 别的剧本 / 非群消息都不算')
        self.assertEqual(snapshot['messages'], [])


# =========================================================================== #
# ⑥ mention-only：落库但不触发
# =========================================================================== #

class MentionOnlyEvidenceTests(Harness):
    async def test_an_unmentioned_image_is_persisted_but_never_triggers(self) -> None:
        rule = group_rule_stub(responseMode='mention-only')
        service = self.make_service(self._config(rule))
        self.make_story(SHARED_STORY_ID)
        self.assertTrue(await service.receive_group(
            group_session(content=PHOTO_TAG, media=[PHOTO_MEDIA]), STORY_TIME,
        ))
        entries = self._group_entries()
        self.assertEqual(len(entries), 1, '未 @ 的图片要作为历史证据落库')
        self.assertEqual(
            entries[0]['metadata']['groupImageRefs'], [_ref(IMAGE_A)],
        )
        # 但不进 debounce 队列 → 不耗意愿、不查冷却、不调模型。
        self.assertEqual(service.buffered_group_turns, {})
        self.assertNotIn('群聊消息准备进入主叙事', self.sink.text())

    async def test_an_unmentioned_plain_text_message_is_still_ignored(self) -> None:
        """上一条只对**图片**成立：没 @ 的纯文本照旧整条忽略（零入库）。"""
        rule = group_rule_stub(responseMode='mention-only')
        service = self.make_service(self._config(rule))
        self.make_story(SHARED_STORY_ID)
        self.assertFalse(await service.receive_group(group_session(content='随便聊'), STORY_TIME))
        self.assertEqual(self._group_entries(), [])
        self.assertEqual(service.buffered_group_turns, {})

    async def test_a_mentioned_image_still_triggers_normally(self) -> None:
        """@ 了机器人的图片是正常回合：当前图走 `images`，不落历史引用表。"""
        rule = group_rule_stub(responseMode='mention-only')
        service = self.make_service(self._config(rule))
        self.make_story(SHARED_STORY_ID)
        session = group_session(
            content='@bot ' + PHOTO_TAG, media=[PHOTO_MEDIA],
            elements=[{'type': 'at', 'attrs': {'id': '1'}}],
        )
        self.assertTrue(await service.receive_group(session, STORY_TIME))
        turn = service.buffered_group_turns['%s:9' % SHARED_STORY_ID]
        message = turn['messages'][0]
        self.assertEqual(message['imageSources'], [IMAGE_A])
        self.assertIs(message['imageSession'], session)
        self.assertEqual(
            self._group_entries()[0]['metadata']['groupImageRefs'], [_ref(IMAGE_A)],
            '同一条消息既进当前回合的 images，也进持久化的历史引用',
        )

    async def test_a_message_without_images_keeps_the_old_buffer_shape(self) -> None:
        """没有图片的消息**不写** `imageSources` / `imageSession`（上游是展开空对象）。"""
        rule = group_rule_stub(responseMode='mention-only')
        service = self.make_service(self._config(rule))
        self.make_story(SHARED_STORY_ID)
        await service.receive_group(
            group_session(content='@bot 在吗', elements=[{'type': 'at', 'attrs': {'id': '1'}}]),
            STORY_TIME,
        )
        message = service.buffered_group_turns['%s:9' % SHARED_STORY_ID]['messages'][0]
        self.assertNotIn('imageSources', message)
        self.assertNotIn('imageSession', message)


# =========================================================================== #
# ⑦ 私聊不变 + ⑧ 提示词那一跳
# =========================================================================== #

class PrivateChatUnchangedTests(Harness):
    """⑦ 私聊不变：私聊那半边**一个字都不改**（私聊入站的落库反向断言在
    `test_service_chunk1.ReceiveTests.test_incoming_images_and_audio_are_counted_in_metadata`，
    与私聊自己的契约放在一起；这里管的是"群聊这条新通道不碰私聊"）。"""

    def test_the_private_prompt_has_no_group_history_fields(self) -> None:
        """私聊请求的 `currentEvent` 不长出群聊那两个字段。"""
        payload = to_prompt_payload(prompt_request([], '你好'))
        event = payload['incomingEvent']['event']
        self.assertEqual(event['type'], 'private-message-batch')
        self.assertNotIn('historicalImageCount', event)
        self.assertNotIn('historicalGroupImages', event)

    def test_the_group_prompt_declares_history_with_source_metadata(self) -> None:
        """⑧ ②：历史图片进模型上下文——条数 + 来源元数据（ISO 时间），且不是新消息。"""
        occurred = STORY_TIME
        payload = to_prompt_payload(prompt_request([], '看这个', overrides={
            'group_context': {'groupId': '9', 'messages': []},
            'images': [{'id': 'turn-image-1', 'mimeType': 'image/png', 'dataUri': DATA_URI}],
            'historicalGroupImages': [
                {
                    'id': 'group-history-image-1', 'mimeType': 'image/png', 'dataUri': DATA_URI,
                    'sourceEntryId': 7, 'senderId': '2', 'senderName': '成员',
                    'occurredAt': occurred, 'messageId': '-9', 'detail': 'low',
                },
                {
                    'id': 'group-history-image-2', 'mimeType': 'image/png', 'dataUri': DATA_URI,
                    'sourceEntryId': 8, 'senderId': '3', 'senderName': '另一个人',
                    'occurredAt': occurred, 'detail': 'low',
                },
            ],
        }))
        event = payload['incomingEvent']['event']
        self.assertEqual(event['type'], 'group-message-batch')
        self.assertEqual(event['imageCount'], 1, '当前回合的图照旧只数自己的')
        self.assertEqual(event['historicalImageCount'], 2)
        self.assertEqual([item['id'] for item in event['historicalGroupImages']], [
            'group-history-image-1', 'group-history-image-2',
        ])
        self.assertEqual(event['historicalGroupImages'][0]['sourceEntryId'], 7)
        self.assertEqual(event['historicalGroupImages'][0]['messageId'], '-9')
        self.assertEqual(event['historicalGroupImages'][1]['senderName'], '另一个人')
        self.assertTrue(event['historicalGroupImages'][0]['occurredAt'].startswith('2026-01-01'))
        # ⚠️ 图片本体（dataUri）**不进 JSON payload**：只走 multipart 那一跳。
        self.assertNotIn('data:image/', json.dumps(payload))

    def test_a_group_without_history_still_declares_zero(self) -> None:
        payload = to_prompt_payload(prompt_request([], '看这个', overrides={
            'group_context': {'groupId': '9', 'messages': []},
        }))
        self.assertEqual(payload['incomingEvent']['event']['historicalImageCount'], 0)
        self.assertNotIn('historicalGroupImages', payload['incomingEvent']['event'])


# =========================================================================== #
# 判据一处 / schema 对照
# =========================================================================== #

class SingleJudgementSiteTests(unittest.TestCase):
    def test_the_storage_and_read_validators_are_one_judgement(self) -> None:
        """写库与读库用的是**同一套**判据（都住在 `helpers`），没有第二份过滤。"""
        import plugin.core.service.chunk1 as chunk1
        import plugin.core.service.chunk2 as chunk2
        import plugin.core.service.helpers as helpers

        with open(helpers.__file__, encoding='utf-8') as handle:
            text = handle.read()
        self.assertEqual(text.count('def group_image_refs_for_storage'), 1)
        self.assertEqual(text.count('def normalize_stored_group_image_ref'), 1)
        self.assertEqual(text.count('GROUP_IMAGE_DATA_URI_RE = '), 1)
        # 写入方 / 读取方各自**只是调用方**，不许在别处再定义一份（两个判据漂了就会
        # 一边过滤 data URI、一边不过滤）。
        with open(chunk1.__file__, encoding='utf-8') as handle:
            body = handle.read()
        self.assertIn('group_image_refs_for_storage', body, 'chunk1 落库要走这一处')
        self.assertNotIn('def group_image_refs_for_storage', body)
        self.assertNotIn('def normalize_stored_group_image_ref', body)
        with open(chunk2.__file__, encoding='utf-8') as handle:
            body = handle.read()
        self.assertIn('normalize_stored_group_image_ref', body, 'chunk2 读回要走同一套校验')
        self.assertNotIn('def normalize_stored_group_image_ref', body)

    def test_the_default_is_declared_in_exactly_one_core_place(self) -> None:
        self.assertEqual(DEFAULT_HISTORICAL_IMAGE_LIMIT, 3)
        self.assertEqual(ServiceChunk1.load_historical_group_images.__name__,
                         'load_historical_group_images')


if __name__ == '__main__':
    unittest.main(verbosity=2)
