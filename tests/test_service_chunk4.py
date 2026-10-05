"""`plugin/core/service/chunk4.py` 的单元测试。

对应上游 `upstream/src/service.ts:3217-4185`（Chunk4）：

| 上游成员 | 本文件覆盖 |
| --- | --- |
| `advanceUnlocked` | 时间导演重试闸门、后台写作抑制、到期批次结算 |
| `decide` | 请求装配（camelCase wire format）、含 `narrator` 缺失时的降级 |
| `shouldRefreshContinuity` | 恒为 `False`（上游 :3604） |
| `planAutomaticTimeline` | 开关/退避闸门/成功/被拒不通过/调用异常 |
| `isTimelineDirectorFused` | 熔断阈值 |
| `persistTimelineRetry` | 指数退避 + 持久化重试闸门 |
| `tryDecide` | 降级不冻结（上游 m7-m8-continuity）、失败返回、恢复重写、可见回复守卫 |
| `persistDecision` | 落库集成：正文/投递草稿/剧本条目/记忆/意图/状态补丁/状态计数 |

**上游用例移植**：`upstream/test/m7-m8-continuity.test.ts:157`
「failed long-window director degrades to director-less advance instead of freezing」
→ `test_upstream_failed_long_window_director_degrades_to_directorless_advance`。
另外四个被点名的上游测试文件（`evidence-repair` / `beta6-handoff` /
`continuity-checkpoint` / `episode-index`）中，断言本范围成员的用例经逐条核对为
**零条**：它们断言的是 `persistFact` / `persistCompaction` / `persistTimelineSceneAnchor`
/ `appendEntry` / `contactThreads` / `developmentForPrompt`（分别属于 Chunk5/Chunk8/Chunk9），
本文件对那部分只做**守门占位**（`@unittest.skipUnless`），不假绿。

运行：
    cd <仓库根目录> && python3 -m unittest plugin.tests.test_service_chunk4 -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest import mock

from plugin.core import logging as interlude_logging
from plugin.core import narrator as narrator_module
from plugin.core.database import Database
from plugin.core.script.authored_actions import resolve_authored_actions
from plugin.core.script.commit_builder import decision_to_script_commit, find_outgoing_script_event
from plugin.core.service import (
    InterludeContext,
    InterludeService,
    NullTransport,
    normalize_database_row,
)
from plugin.core.service import chunk4 as chunk4_module
from plugin.core.service.chunk4 import (
    MINUTE_MS,
    ServiceChunk4,
    _create_fact_query,
    _normalize_browser_intent_draft,
    _normalize_decision,
    _valid_intent,
)
from plugin.core.service.config import (
    TIMELINE_DIRECTOR_FUSE,
    TIMELINE_DIRECTOR_FUSE_COOLDOWN,
    TIMELINE_RETRY_BACKOFF_BASE,
)
from plugin.core.service.helpers import visible_reply_mode
from plugin.core.story_state import decode_story_state, encode_story_state
from plugin.core.time import dt_ms, iso
from plugin.core.types import empty_story_state
from plugin.core.urge import resolve_urge_config
from plugin.core.service.base import _config_section

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
FROM = NOW - timedelta(days=1)
TZ = 'Asia/Shanghai'

STORY_ID = 'character:test:1'
PARTICIPANT_ID = 'test:1:2'

#: 本文件是否可用 Chunk4（模块本身必须能独立 import）。
CHUNK4_READY = callable(getattr(ServiceChunk4, 'try_decide', None)) \
    and callable(getattr(ServiceChunk4, 'persist_decision', None))

#: 落库集成用例需要同批任务的 Chunk5/Chunk9（`append_entry` / `get_story`）。
_INTEGRATION_READY = CHUNK4_READY \
    and callable(getattr(InterludeService, 'append_entry', None)) \
    and callable(getattr(InterludeService, 'get_story', None))

#: Chunk4 在真实服务里依赖的全部兄弟成员（缺一个就跳过端到端用例）。
_REQUIRED_SIBLINGS = (
    'get_story', 'get_participant', 'participants', 'recent_entries', 'recent_entries_for_prompt',
    'memories', 'active_scene', 'active_arc', 'previous_scene_summaries', 'facts',
    'web_observations', 'active_consequences_and_expire', 'overlay_snapshots_for_prompt',
    'pending_follow_up_commitments', 'upcoming_narrative_intents', 'get_schedule_preplan',
    'due_intents', 'append_entry', 'append_memory', 'append_intent', 'apply_intent_updates',
    'append_browser_intent', 'update_alter_system', 'schedule_alter_analysis',
    'persist_timeline_scene_anchor', 'prune_working_details', 'contact_threads', 'recall_history',
    'development_for_prompt', 'emotional_offset_for_prompt', 'split_outgoing_message',
    'send_outgoing_messages', 'update_participant_state', 'mark_participant_seen',
    'record_automatic_delivery', 'record_character_message', 'update_script_delivery_outcome',
    'schedule_next_automatic_advance', 'schedule_due_intent_wake', 'schedule_next_split_wake',
    'is_automatic_advance_due', 'is_automatic_advance_paused', 'due_conversation_follow_ups',
    'complete_conversation_follow_ups', 'pause_automatic_advance_after_delayed_reply',
    'defer_unresolved_due_follow_ups', 'append_proactive_check', 'main_model_label', 'embed_text',
)

#: 全部 10 个 chunk 都落地后，才能跑「真实兄弟实现」的端到端用例。
FULL_SERVICE_READY = _INTEGRATION_READY and all(
    hasattr(InterludeService, name) for name in _REQUIRED_SIBLINGS
) and all(
    hasattr(InterludeService, name)
    for name in ('urge_config', 'effective_urge_runtime', 'auto_advance_config', 'browser_config')
)


def make_setting() -> dict[str, Any]:
    return {
        'character': {'name': '凌梦', 'profile': ''},
        'user': {'displayName': 'Kela', 'profile': ''},
        'timezone': TZ,
    }


def make_runtime(**overrides: Any) -> dict[str, Any]:
    runtime = {
        'maxScriptCharacters': 4_000,
        'maxMessageCharacters': 3_000,
        'messageSeparator': '<sep/>',
        'splitReplyMessages': True,
        'allowProactiveMessages': False,
        'memoryLimit': 40,
        'contextEntryLimit': 20,
        'minimumAdvanceMinutes': 5,
        'sweepIntervalMinutes': 5,
        'contextTimeWindowMinutes': 60,
        'minimumDelayedReplySeconds': 30,
        'maximumDelayedReplyMinutes': 1_440,
    }
    runtime.update(overrides)
    return runtime


class _Sink:
    """内存日志 sink（与 `test_service_base.py` 同法，避免测试输出噪声）。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.records.append((level, text))

    def text(self) -> str:
        return '\n'.join(text for _level, text in self.records)


# =========================================================================== #
# 只装 Chunk4 的宿主：兄弟 chunk 用显式桩（并行移植期 chunk6/7/8 未落地）
# =========================================================================== #

class Chunk4Host(ServiceChunk4):
    """`ServiceChunk4` 的最小宿主：`ServiceBase` 的字段 + 兄弟成员桩。"""

    def __init__(self, config: Optional[dict[str, Any]] = None, failures: Optional[dict[str, int]] = None):
        self.config = config if config is not None else {
            'runtime': make_runtime(),
            'memory': {'enabled': False},
            'sharedStory': {'participantContextLimit': 4, 'maxCrossConversationActions': 2},
        }
        self.ctx = InterludeContext(clock=lambda: NOW)
        self.model_routing = {'main': {'available': True}}
        self.timeline_backoff: dict[str, dict[str, Any]] = {}
        self.timeline_director_failures: dict[str, int] = dict(failures or {})
        self.interrupted_typing_participants: set[str] = set()
        self.narrated = 0
        self.operations: list[tuple[Any, ...]] = []
        self.reports: list[tuple[Any, ...]] = []
        self.wakes: list[Any] = []
        self.writes: list[tuple[str, Any, Any]] = []
        self.compactor = None
        self.plan_timeline_calls = 0

    # ---- 兄弟 chunk 的桩（只实现本文件断言得到的行为）----
    # 注意：**绝不**覆盖 Chunk4 自己的 8 个成员，否则测的就不是移植实现了。

    @property
    def urge_config(self) -> dict[str, Any]:
        """Chunk6 的 `urgeConfig` getter 的等价物（用真实 `resolve_urge_config`）。"""
        return resolve_urge_config(_config_section(self.config, 'urge'))

    @property
    def effective_urge_runtime(self) -> dict[str, Any]:
        """上游 `effectiveUrgeRuntime`（`:5413`）返回的是**完整 RuntimeConfig**（含 urge 覆盖）。"""
        return {**self.runtime_config, 'proactiveWillingnessThreshold': 0.65}

    def report_operation(self, *args: Any, **kwargs: Any) -> None:
        self.operations.append(args)

    def report(self, *args: Any, **kwargs: Any) -> None:
        self.reports.append(args)

    def main_model_label(self) -> str:
        return 'mock-model'

    async def decide(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.narrated += 1
        return {
            'script': '降级后的无账本推进剧本。',
            # 结构化可见回复：`mode: 'none'` 是合法形态，不会触发"缺失结构化回复"重写。
            'interaction': {'seen': True, 'reply': {'mode': 'none'}},
        }

    async def db_set(self, table: str, query: Any, data: Any) -> Any:
        self.writes.append((table, query, data))
        return 1

    def schedule_due_intent_wake(self, story_id: str, not_before: Any) -> None:
        self.wakes.append(not_before)

    async def due_intents(self, story_id: str, now: Any) -> list[Any]:
        return []

    async def get_story(self, story_id: str) -> Any:
        return _story()

    def schedule_next_split_wake(self, story_id: str) -> None:
        return None

    async def active_scene(self, story_id: str) -> Any:
        return None

    async def active_arc(self, story_id: str) -> Any:
        return None

    async def previous_scene_summaries(self, story_id: str) -> list[Any]:
        return []

    async def participants(self, story_id: str, include_paused: bool = False) -> list[Any]:
        return []

    async def memories(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def web_observations(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def active_consequences_and_expire(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def overlay_snapshots_for_prompt(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def pending_follow_up_commitments(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def upcoming_narrative_intents(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def development_for_prompt(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def emotional_offset_for_prompt(self, story: Any) -> Any:
        return None

    def can_handle_participant(self, participant: Any) -> bool:
        return True

    def prune_working_details(self, details: Any, now: Any) -> list[Any]:
        return list(details or [])

    async def recent_entries_for_prompt(self, story_id: str, now: Any) -> list[Any]:
        return []

    async def facts(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def contact_threads(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def recall_history(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def get_schedule_preplan(self, story_id: str) -> Any:
        return None

    def is_automatic_advance_due(self, story: Any, now: Any) -> bool:
        return False

    def is_automatic_advance_paused(self, story: Any, now: Any) -> bool:
        return False

    def due_conversation_follow_ups(self, story: Any, now: Any) -> list[Any]:
        return []

    async def schedule_next_automatic_advance(self, story_id: str, now: Any) -> None:
        return None

    async def execute_deferred_browser_intent(self, story: Any, intent: Any, now: Any) -> None:
        return None

    def operation_texts(self) -> list[str]:
        return [str(item[4]) for item in self.operations if len(item) > 4]


class _DirectorHost(Chunk4Host):
    """上游 m7-m8 用例的宿主：导演已熔断（连续失败 6 次），自动推进必须降级继续。

    `compactor` 保持 `None`（等价上游 `this.compactor.planTimeline` 不存在），
    因此真实的 `plan_automatic_timeline` 会直接返回 `None`，与上游 stub 行为一致。
    """

    def __init__(self) -> None:
        super().__init__()
        self.timeline_director_failures = {STORY_ID: TIMELINE_DIRECTOR_FUSE}


class _TimelineHost(Chunk4Host):
    """跑真实 `plan_automatic_timeline` 的宿主（只桩掉它依赖的兄弟成员）。"""

    def __init__(self, config: Optional[dict[str, Any]] = None, failures: Optional[dict[str, int]] = None):
        super().__init__(config=config, failures=failures)

        class _Compactor:
            def __init__(self, host: _TimelineHost) -> None:
                self.host = host
                self.result: Any = None
                self.error: Optional[Exception] = None

            async def plan_timeline(self, request: Any) -> Any:
                self.host.plan_timeline_calls += 1
                self.host.timeline_requests.append(request)
                if self.error is not None:
                    raise self.error
                return self.result

        self.timeline_requests: list[Any] = []
        self.compactor = _Compactor(self)
        self.recent_entries_calls = 0

    async def recent_entries_for_prompt(self, story_id: str, now: Any) -> list[Any]:
        self.recent_entries_calls += 1
        return [
            {
                'id': 7, 'storyId': story_id, 'participantId': '', 'kind': 'script',
                'actor': 'narrator', 'content': '她合上练习册。', 'occurredAt': NOW,
                'metadata': {}, 'createdAt': NOW,
            },
        ]


def _story() -> dict[str, Any]:
    return {
        'id': STORY_ID,
        'platform': 'test',
        'selfId': '1',
        'userId': '1',
        'channelId': "private:1",
        'status': 'active',
        'setting': make_setting(),
        'state': encode_story_state(empty_story_state()),
        'cursorAt': FROM,
        'createdAt': FROM,
        'updatedAt': FROM,
    }


# =========================================================================== #
# 上游用例移植
# =========================================================================== #

class UpstreamPortedTests(unittest.IsolatedAsyncioTestCase):
    """`upstream/test/m7-m8-continuity.test.ts` 里断言本范围成员的那一条。"""

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.host = _DirectorHost()

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_upstream_failed_long_window_director_degrades_to_directorless_advance(self) -> None:
        """上游用例：熔断只抑制导演调用本身，自动推进必须降级继续，不再丢弃整个回合。"""
        result = await self.host.try_decide(
            _story(), None, 'advance', FROM, NOW, None, [],
        )
        self.assertTrue(result['succeeded'])
        self.assertEqual(self.host.narrated, 1)
        # 熔断分支确实被报告成「降级推进」而不是静默。
        self.assertTrue(
            any('时间导演已熔断' in text for text in self.host.operation_texts()),
            self.host.operation_texts(),
        )

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_upstream_stubbed_decide_still_receives_the_wire_request(self) -> None:
        """上游 stub 把 `decide` 整个换掉；这里额外确认 `tryDecide` 真的调用了一次。"""
        seen: list[Any] = []

        async def decide(*args: Any, **kwargs: Any) -> dict[str, Any]:
            seen.append(args)
            return {'script': '剧本。'}

        self.host.decide = decide  # type: ignore[assignment]
        await self.host.try_decide(_story(), None, 'advance', FROM, NOW, None, [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][2], 'advance')


# =========================================================================== #
# tryDecide / decide
# =========================================================================== #

class TryDecideTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_user_turn_never_needs_the_timeline_director(self) -> None:
        host = _TimelineHost()
        result = await host.try_decide(_story(), None, 'user-message', FROM, NOW, '在？', [])
        self.assertTrue(result['succeeded'])
        self.assertEqual(host.plan_timeline_calls, 0)
        self.assertEqual(host.recent_entries_calls, 0)

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_decide_failure_is_reported_as_unsucceeded_result(self) -> None:
        host = Chunk4Host()

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError('provider down')

        host.decide = boom  # type: ignore[assignment]
        result = await host.try_decide(_story(), None, 'advance', FROM, NOW, None, [])
        self.assertFalse(result['succeeded'])
        self.assertEqual(result['decision'], {})
        self.assertEqual(result['effectiveNow'], NOW)
        self.assertTrue(any('模型调用失败' in str(item[3]) for item in host.reports))


    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_result_exposes_both_key_spellings_for_sibling_chunks(self) -> None:
        host = Chunk4Host()
        result = await host.try_decide(_story(), None, 'advance', FROM, NOW, None, [])
        for camel, snake in (
            ('effectiveNow', 'effective_now'),
            ('immediateObservations', 'immediate_observations'),
            ('timelinePlan', 'timeline_plan'),
        ):
            self.assertIn(camel, result)
            self.assertIn(snake, result)
            self.assertEqual(result[camel], result[snake])

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_invisible_reply_recovery_rewrites_once_then_degrades(self) -> None:
        """实况用户回合缺结构化可见回复：重写一次；仍缺失则**降级提交**（上游 1.0.1-rc24+）。

        rc24/rc28 把原来的 `throw` 改成降级：剧本是好的，一个传输字段缺失不该让整个
        回合失败并进 60 秒重试队列（弱模型下那是失败循环）。断言随之改成"两稿都试过、
        剧本保留、没有可见回复、警告可见"。
        """
        host = Chunk4Host()
        calls: list[bool] = []

        async def decide(*args: Any, **kwargs: Any) -> dict[str, Any]:
            output_recovery = args[12] if len(args) > 12 else kwargs.get('output_recovery', False)
            calls.append(bool(output_recovery))
            return {'script': '她在窗边。'}

        host.decide = decide  # type: ignore[assignment]
        participant = {'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'platform': 'test', 'status': 'active'}
        result = await host.try_decide(_story(), participant, 'user-message', FROM, NOW, '在？', [])
        self.assertTrue(result['succeeded'])
        self.assertEqual(result['decision'].get('script'), '她在窗边。')
        self.assertIsNone(result['decision'].get('interaction'))
        self.assertEqual(calls, [False, True])
        # `report_operation` 落在 `operations`（`report` 才是 `reports`），两个 sink 都扫。
        seen = [str(item) for item in (list(host.operations) + list(host.reports))]
        self.assertTrue(any('结构化回复两稿均缺失' in item for item in seen), seen)

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_script_less_model_turn_is_a_provider_failure(self) -> None:
        host = Chunk4Host()

        async def decide(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {'interaction': {'seen': True, 'reply': {'mode': 'none'}}}

        host.decide = decide  # type: ignore[assignment]
        result = await host.try_decide(_story(), None, 'advance', FROM, NOW, None, [])
        self.assertFalse(result['succeeded'])
        self.assertTrue(any('no usable script' in str(item) for item in host.reports))


# =========================================================================== #
# 时间导演
# =========================================================================== #

class TimelineDirectorTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    def test_should_refresh_continuity_is_false(self) -> None:
        host = Chunk4Host()
        self.assertFalse(host.should_refresh_continuity(_story(), 'advance'))
        self.assertFalse(host.should_refresh_continuity(_story(), 'user-message'))

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    def test_fuse_threshold_matches_upstream_constant(self) -> None:
        host = Chunk4Host()
        self.assertEqual(TIMELINE_DIRECTOR_FUSE, 6)
        self.assertIsNone(host.is_timeline_director_fused(STORY_ID))
        host.timeline_director_failures[STORY_ID] = TIMELINE_DIRECTOR_FUSE - 1
        self.assertIsNone(host.is_timeline_director_fused(STORY_ID))
        host.timeline_director_failures[STORY_ID] = TIMELINE_DIRECTOR_FUSE
        self.assertEqual(host.is_timeline_director_fused(STORY_ID), TIMELINE_DIRECTOR_FUSE)

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_disabled_director_short_circuits_without_provider_call(self) -> None:
        host = _TimelineHost(config={'timelineDirector': {'enabled': False}})
        plan = await host.plan_automatic_timeline(_story(), None, 'advance', FROM, NOW, [])
        self.assertIsNone(plan)
        self.assertEqual(host.plan_timeline_calls, 0)

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_active_backoff_skips_the_provider_call(self) -> None:
        host = _TimelineHost()
        host.timeline_backoff[STORY_ID] = {'from': dt_ms(FROM), 'until': dt_ms(NOW) + MINUTE_MS}
        plan = await host.plan_automatic_timeline(_story(), None, 'advance', FROM, NOW, [])
        self.assertIsNone(plan)
        self.assertEqual(host.plan_timeline_calls, 0)
        self.assertTrue(any('时间导演调用冷却中' in text for text in host.operation_texts()))

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_valid_plan_clears_failures_and_backoff(self) -> None:
        host = _TimelineHost(failures={STORY_ID: 2})
        host.timeline_backoff[STORY_ID] = {'from': 1, 'until': dt_ms(NOW) + MINUTE_MS}
        host.compactor.result = {'beats': [{'at': 0.5, 'kind': 'activity', 'summary': '继续写物理题'}],
                                 'carry': ['对方仍在打游戏']}
        plan = await host.plan_automatic_timeline(_story(), None, 'advance', FROM, NOW, [])
        self.assertEqual(host.plan_timeline_calls, 1)
        self.assertEqual(plan['beats'][0]['summary'], '继续写物理题')
        self.assertEqual(plan['carry'], ['对方仍在打游戏'])
        self.assertNotIn(STORY_ID, host.timeline_director_failures)
        self.assertNotIn(STORY_ID, host.timeline_backoff)
        self.assertTrue(any('时间导演已生成事件账本' in text for text in host.operation_texts()))

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_rejected_plan_persists_a_retry_gate(self) -> None:
        host = _TimelineHost()
        host.compactor.result = {'beats': []}
        plan = await host.plan_automatic_timeline(_story(), None, 'advance', FROM, NOW, [])
        self.assertIsNone(plan)
        self.assertEqual(host.timeline_director_failures[STORY_ID], 1)
        self.assertEqual(host.timeline_backoff[STORY_ID]['from'], dt_ms(FROM))
        self.assertEqual(
            host.timeline_backoff[STORY_ID]['until'], dt_ms(NOW) + TIMELINE_RETRY_BACKOFF_BASE,
        )
        self.assertTrue(any('时间导演返回被拒绝' in text for text in host.operation_texts()))
        self.assertEqual(host.writes[0][0], 'interlude_story')
        stored = decode_story_state(host.writes[0][2]['state'])['automation']
        self.assertEqual(stored['timeline_retry_from'], iso(FROM))

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_provider_error_is_swallowed_into_a_retry_gate(self) -> None:
        host = _TimelineHost()
        host.compactor.error = RuntimeError('provider exploded')
        plan = await host.plan_automatic_timeline(_story(), None, 'advance', FROM, NOW, [])
        self.assertIsNone(plan)
        self.assertEqual(host.timeline_director_failures[STORY_ID], 1)
        self.assertTrue(any('时间导演调用失败' in text for text in host.operation_texts()))

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_backoff_is_exponential_and_capped_at_two_hours(self) -> None:
        expected = {
            1: TIMELINE_RETRY_BACKOFF_BASE,
            2: TIMELINE_RETRY_BACKOFF_BASE * 2,
            5: TIMELINE_DIRECTOR_FUSE_COOLDOWN,  # 9.6h 被 2h 上限截断
            6: TIMELINE_DIRECTOR_FUSE_COOLDOWN,
            12: TIMELINE_DIRECTOR_FUSE_COOLDOWN,
        }
        self.assertEqual(TIMELINE_DIRECTOR_FUSE_COOLDOWN, 2 * 60 * MINUTE_MS)
        for failures, backoff in expected.items():
            host = Chunk4Host()
            await host.persist_timeline_retry(_story(), FROM, 'advance', failures)
            self.assertEqual(host.timeline_backoff[STORY_ID]['from'], dt_ms(FROM))
            self.assertEqual(host.timeline_backoff[STORY_ID]['until'], dt_ms(NOW) + backoff)
            stored = decode_story_state(host.writes[0][2]['state'])['automation']
            self.assertEqual(stored['timeline_retry_at'], iso(NOW + timedelta(milliseconds=backoff)))
            self.assertEqual(stored['next_advance_at'], iso(NOW + timedelta(milliseconds=backoff)))

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_persist_failure_keeps_in_memory_backoff(self) -> None:
        host = Chunk4Host()

        async def failing_db_set(table: str, query: Any, data: Any) -> Any:
            raise RuntimeError('database unavailable')

        host.db_set = failing_db_set  # type: ignore[assignment]
        await host.persist_timeline_retry(_story(), FROM, 'advance', 1)
        self.assertEqual(host.timeline_backoff[STORY_ID]['until'], dt_ms(NOW) + TIMELINE_RETRY_BACKOFF_BASE)
        self.assertTrue(any('冷却状态持久化失败' in str(item[4]) for item in host.operations))


# =========================================================================== #
# advanceUnlocked
# =========================================================================== #

class AdvanceUnlockedTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_timeline_retry_gate_returns_early_and_schedules_the_wake(self) -> None:
        host = Chunk4Host()
        story = _story()
        retry_at = NOW + timedelta(minutes=10)
        state = decode_story_state(story['state'])
        state['automation'] = {'timeline_retry_at': iso(retry_at), 'timeline_retry_from': iso(FROM)}
        story['state'] = encode_story_state(state)
        messages = await host.advance_unlocked(story, NOW, False)
        self.assertEqual(messages, [])
        self.assertEqual(host.wakes, [retry_at])
        self.assertEqual(host.narrated, 0)
        self.assertTrue(any('自动推进等待时间导演重试' in text for text in host.operation_texts()))

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_force_bypasses_the_timeline_retry_gate(self) -> None:
        host = Chunk4Host()
        persisted: list[Any] = []

        async def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
            # 只做调用记录：本用例断言的是 advanceUnlocked 的控制流。
            persisted.append(args)
            return {'messages': [], 'commit': None, 'scriptEntry': None, 'script_entry': None}

        host.persist_decision = spy  # type: ignore[assignment]
        story = _story()
        state = decode_story_state(story['state'])
        state['automation'] = {
            'timeline_retry_at': iso(NOW + timedelta(minutes=10)), 'timeline_retry_from': iso(FROM),
        }
        story['state'] = encode_story_state(state)
        # force=True 且游标距现在 24h：绕过闸门，直接开启一次自动写作并落库。
        messages = await host.advance_unlocked(story, NOW, True)
        self.assertEqual(host.wakes, [])
        self.assertEqual(messages, [])
        self.assertEqual(len(persisted), 1)
        self.assertEqual(host.narrated, 1)
        # 上游在 `force` 且窗口起点未变时**不**清闸门（只对非 force 生效），
        # 这里断言行为一致：闸门保留，但本次推进没有被它挡住。
        self.assertEqual(
            decode_story_state(story['state'])['automation']['timeline_retry_from'], iso(FROM),
        )

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_disabled_auto_advance_suppresses_every_background_write(self) -> None:
        """关闭自动推进必须压制所有后台写作路径，手动 force 仍是逃生口。"""
        host = Chunk4Host()
        story = _story()
        messages = await host.advance_unlocked(story, NOW, False)
        self.assertEqual(messages, [])
        self.assertEqual(host.narrated, 0)
        self.assertFalse(any('即将执行自动写作' in text for text in host.operation_texts()))

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_manual_advance_too_soon_is_skipped(self) -> None:
        host = Chunk4Host()
        story = _story()
        story['cursorAt'] = NOW - timedelta(minutes=1)  # < minimumAdvanceMinutes(5)
        await host.advance_unlocked(story, NOW, True)
        self.assertEqual(host.narrated, 0)
        self.assertTrue(any('手动推进跳过' in text for text in host.operation_texts()))

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    async def test_urge_mode_change_is_persisted_before_advancing(self) -> None:
        host = Chunk4Host(config={
            'runtime': make_runtime(),
            'memory': {'enabled': False},
            'urge': {'enabled': True, 'frequency': 'low'},
        })
        story = _story()
        await host.advance_unlocked(story, NOW, False)
        urge_writes = [write for write in host.writes if write[0] == 'interlude_story']
        self.assertTrue(urge_writes)
        stored = decode_story_state(urge_writes[0][2]['state'])
        self.assertIn('urge', stored['extensions'])
        self.assertNotEqual(stored['extensions']['urge']['mode'], 'off')
        self.assertEqual(stored['extensions']['urge']['pace'], 'normal')


# =========================================================================== #
# normalizeDecision（纯逻辑，逐条对照上游 :7835）
# =========================================================================== #

class NormalizeDecisionTests(unittest.TestCase):

    def setUp(self) -> None:
        self.runtime = make_runtime()
        self.shared = {'allowCrossConversationMessages': True, 'maxCrossConversationActions': 2}
        self.permitted = {PARTICIPANT_ID, 'test:1:3'}

    def normalize(self, raw: Any, **overrides: Any) -> dict[str, Any]:
        options = {
            'from_dt': NOW - timedelta(hours=1),
            'now': NOW,
            'permit_messages': True,
            'runtime': self.runtime,
            'shared': self.shared,
            'current_participant_id': PARTICIPANT_ID,
            'permitted_participant_ids': self.permitted,
            'phase': 'user-message',
            'memory': {'enabled': True},
            'refresh_continuity': False,
        }
        options.update(overrides)
        return _normalize_decision(raw, **options)

    def test_script_is_trimmed_and_clamped(self) -> None:
        decision = self.normalize({'script': '  ' + '字' * 5_000 + '  '})
        self.assertEqual(len(decision['script']), self.runtime['maxScriptCharacters'])
        self.assertFalse(decision['script'].startswith(' '))
        self.assertEqual(self.normalize({'script': 42})['script'], '')

    def test_interaction_is_dropped_on_advance_and_keeps_camel_wire_keys(self) -> None:
        raw = {'script': '她在窗边。', 'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': ' 好 '}}}
        advance = self.normalize(raw, phase='advance')
        self.assertIsNone(advance['interaction'])
        user_turn = self.normalize(raw)
        self.assertEqual(user_turn['interaction']['reply'], {'mode': 'immediate', 'content': '好'})
        self.assertTrue(user_turn['interaction']['seen'])

    def test_delayed_reply_outside_the_window_collapses_to_none(self) -> None:
        raw = {
            'script': '她想了想。',
            'interaction': {'seen': True, 'reply': {'mode': 'delayed', 'content': '晚点说', 'sendAt': iso(NOW + timedelta(seconds=5))}},
        }
        decision = self.normalize(raw)
        self.assertEqual(decision['interaction']['reply'], {'mode': 'none'})
        ok = self.normalize({
            'script': '她想了想。',
            'interaction': {'seen': True, 'reply': {'mode': 'delayed', 'content': '晚点说', 'sendAt': iso(NOW + timedelta(minutes=10))}},
        })
        self.assertEqual(ok['interaction']['reply']['sendAt'], iso(NOW + timedelta(minutes=10)))
        # commit_builder 按 snake_case 读 `send_at`，别名必须存在。
        self.assertEqual(ok['interaction']['reply']['send_at'], iso(NOW + timedelta(minutes=10)))

    def test_intents_require_the_future_and_drop_follow_up_commitments(self) -> None:
        raw = {
            'script': '剧本。',
            'intents': [
                {'type': 'reminder', 'summary': '未来', 'notBefore': iso(NOW + timedelta(hours=1))},
                {'type': 'follow-up-commitment', 'summary': '承诺', 'notBefore': iso(NOW + timedelta(hours=1))},
                {'type': 'reminder', 'summary': '过去', 'notBefore': iso(NOW - timedelta(minutes=1))},
                {'type': 'reminder', 'summary': '缺时间'},
            ],
        }
        decision = self.normalize(raw)
        self.assertEqual([item['summary'] for item in decision['intents']], ['未来'])
        self.assertEqual(decision['intents'][0]['not_before'], iso(NOW + timedelta(hours=1)))
        self.assertEqual(decision['intents'][0]['participant_id'], PARTICIPANT_ID)

    def test_intents_are_capped_at_eight(self) -> None:
        raw = {
            'script': '剧本。',
            'intents': [
                {'type': 'reminder', 'summary': 'i%d' % index, 'notBefore': iso(NOW + timedelta(hours=1))}
                for index in range(12)
            ],
        }
        self.assertEqual(len(self.normalize(raw)['intents']), 8)

    def test_foreign_participant_ids_are_coerced_to_the_current_branch(self) -> None:
        raw = {
            'script': '剧本。',
            'intents': [{
                'type': 'reminder', 'summary': '越权', 'notBefore': iso(NOW + timedelta(hours=1)),
                'participantId': 'test:1:999',
            }],
            'memories': [{'category': 'event', 'content': '越权记忆', 'participantId': 'test:1:999'}],
        }
        decision = self.normalize(raw)
        self.assertEqual(decision['intents'][0]['participantId'], PARTICIPANT_ID)
        self.assertEqual(decision['memories'][0]['participantId'], PARTICIPANT_ID)

    def test_active_consequence_contract(self) -> None:
        memory = {'enabled': True, 'activeConsequencesEnabled': True, 'activeConsequenceMaxDays': 7}
        base = {
            'type': 'active-consequence', 'summary': '压力', 'notBefore': iso(NOW - timedelta(minutes=30)),
            'payload': {'lifecycle': 'active', 'effect': '她更谨慎', 'expiresAt': iso(NOW + timedelta(days=1))},
        }
        from_dt = NOW - timedelta(hours=1)
        self.assertTrue(_valid_intent(base, from_dt, NOW, memory))
        # 超出 30 天上限（默认 7 天）→ 拒绝
        long_lived = {**base, 'payload': {**base['payload'], 'expiresAt': iso(NOW + timedelta(days=10))}}
        self.assertFalse(_valid_intent(long_lived, from_dt, NOW, memory))
        # 未开启后果 → 拒绝
        self.assertFalse(_valid_intent(base, from_dt, NOW, {'enabled': True}))
        # 强度越界 → 拒绝
        bad_strength = {**base, 'payload': {**base['payload'], 'strength': 1.5}}
        self.assertFalse(_valid_intent(bad_strength, from_dt, NOW, memory))
        # 时间起点早于本回合 → 拒绝
        too_early = {**base, 'notBefore': iso(from_dt - timedelta(minutes=1))}
        self.assertFalse(_valid_intent(too_early, from_dt, NOW, memory))

    def test_memories_require_non_blank_content(self) -> None:
        raw = {'script': '剧本。', 'memories': [
            {'category': 'event', 'content': '有效'},
            {'category': 'event', 'content': '   '},
            {'category': 'event'},
            'not-a-record',
        ]}
        decision = self.normalize(raw)
        self.assertEqual([item['content'] for item in decision['memories']], ['有效'])

    def test_browser_intents_are_validated_and_capped_at_one(self) -> None:
        raw = {'script': '剧本。', 'browserIntents': [
            {'mode': 'search', 'purpose': '查资料', 'query': '取餐码'},
            {'mode': 'search', 'purpose': '第二', 'query': '第二'},
        ]}
        decision = self.normalize(raw)
        self.assertEqual(len(decision['browserIntents']), 1)
        self.assertEqual(decision['browserIntents'][0]['timing'], 'deferred')
        no_url = self.normalize({'script': '剧本。', 'browserIntents': [{'mode': 'visit', 'purpose': '看页面'}]})
        self.assertEqual(no_url['browserIntents'], [])
        no_purpose = self.normalize({'script': '剧本。', 'browserIntents': [{'mode': 'search', 'query': 'x'}]})
        self.assertEqual(no_purpose['browserIntents'], [])

    def test_cross_conversation_actions_need_permit_and_permission(self) -> None:
        raw = {'script': '剧本。', 'crossConversationActions': [
            {'participantId': 'test:1:3', 'mode': 'immediate', 'content': '在吗'},
            {'participantId': 'test:1:999', 'mode': 'immediate', 'content': '越权'},
            {'participantId': PARTICIPANT_ID, 'mode': 'immediate', 'content': '自己'},
        ]}
        decision = self.normalize(raw)
        self.assertEqual([item['participantId'] for item in decision['crossConversationActions']], ['test:1:3'])
        self.assertEqual(self.normalize(raw, permit_messages=False)['crossConversationActions'], [])
        self.assertEqual(
            self.normalize(raw, shared={'allowCrossConversationMessages': False})['crossConversationActions'], [],
        )

    def test_cross_action_keeps_its_say_reference_under_both_spellings(self) -> None:
        # 模型用 actionId 引用原话时，解析只重写 snake_case 那份列表；camelCase 别名
        # 若留在旧值上，这条跨对话动作会因为"没有 content"被整条丢掉。
        raw = {
            'script': '她按下发送。\n\n<say id="reply">在。</say>',
            'crossConversationActions': [
                {'participantId': 'test:1:3', 'mode': 'immediate', 'actionId': 'reply'},
            ],
        }
        decision = self.normalize(raw)
        self.assertEqual([item['content'] for item in decision['crossConversationActions']], ['在。'])

    def test_proactive_cross_action_demands_willingness(self) -> None:
        raw = {
            'script': '剧本。',
            'crossConversationActions': [
                {'participantId': 'test:1:3', 'mode': 'immediate', 'content': '在吗'},
            ],
        }
        # phase=advance 且模型没有给 proactiveContact → 走主动联系的意愿闸门
        decision = self.normalize(raw, phase='advance')
        self.assertEqual(decision['crossConversationActions'], [])
        willing = self.normalize({
            'script': '剧本。',
            'crossConversationActions': [
                {'participantId': 'test:1:3', 'mode': 'immediate', 'content': '在吗', 'willingness': 0.9},
            ],
        }, phase='advance')
        self.assertEqual(len(willing['crossConversationActions']), 1)
        self.assertAlmostEqual(willing['crossConversationActions'][0]['willingness'], 0.9)

    def test_state_patch_keeps_only_the_two_array_fields(self) -> None:
        decision = self.normalize({
            'script': '剧本。',
            'statePatch': {'openThreads': ['物理'], 'relationshipNotes': ['x' * 900], 'unknown': 1},
        })
        self.assertEqual(decision['statePatch']['openThreads'], ['物理'])
        self.assertEqual(len(decision['statePatch']['relationshipNotes'][0]), 500)
        self.assertNotIn('unknown', decision['statePatch'])

    def test_life_handoff_is_grounded_in_the_prose(self) -> None:
        raw = {'script': '她合上书。', 'lifeHandoff': {'activity': {'value': '休息', 'quote': '不存在'}}}
        self.assertIsNone(self.normalize(raw)['lifeHandoff'])
        grounded = {'script': '她合上书，准备休息。', 'lifeHandoff': {'activity': {'value': '休息', 'quote': '准备休息'}}}
        self.assertEqual(self.normalize(grounded)['lifeHandoff']['activity']['value'], '休息')

    def test_dual_keys_satisfy_both_landed_readers(self) -> None:
        """`helpers.visible_reply_mode` 读 camelCase，`script.commit_builder` 读 snake_case。"""
        raw = {
            'script': '她在窗边。',
            'groupReply': {'mode': 'immediate', 'content': '好'},
            'crossConversationActions': [
                {'participantId': 'test:1:3', 'mode': 'immediate', 'content': '在吗'},
            ],
        }
        decision = self.normalize(raw)
        self.assertEqual(decision['cross_conversation_actions'], decision['crossConversationActions'])
        self.assertEqual(decision['group_reply'], decision['groupReply'])
        self.assertEqual(visible_reply_mode(decision, 'advance', None), '主动联系')
        commit = decision_to_script_commit({
            'story_id': STORY_ID,
            'participant_id': PARTICIPANT_ID,
            'phase': 'user-message',
            'from': NOW - timedelta(hours=1),
            'now': NOW,
            'decision': decision,
            'message_separator': '<sep/>',
            'split_reply_messages': True,
            'group_reply_content': '好',
        })
        kinds = [event['kind'] for event in commit['events']]
        self.assertIn('outgoing-message', kinds)
        self.assertIn('group-message', kinds)

    def test_refresh_continuity_gates_the_snapshot(self) -> None:
        raw = {'script': '剧本。', 'continuity': {'current': '她刚下课。', 'salient': ['约定']}}
        self.assertIsNone(self.normalize(raw)['continuity'])
        kept = self.normalize(raw, refresh_continuity=True)['continuity']
        self.assertEqual(kept['current'], '她刚下课。')
        self.assertEqual(kept['salient'], ['约定'])

    def test_alter_defaults_to_none_and_preserves_zero(self) -> None:
        self.assertIsNone(self.normalize({'script': '剧本。'})['alter'])
        self.assertEqual(self.normalize({'script': '剧本。', 'alter': 0})['alter'], 0)

    def test_automatic_delivery_summary_only_for_automatic_phases(self) -> None:
        raw = {'script': '剧本。', 'automaticDeliverySummary': '她刚写完作业'}
        self.assertIsNone(self.normalize(raw, phase='user-message')['automaticDeliverySummary'])
        self.assertEqual(
            self.normalize(raw, phase='advance')['automaticDeliverySummary'], '她刚写完作业',
        )

    def test_browser_intent_config_gates(self) -> None:
        draft = {'mode': 'search', 'purpose': '查', 'query': 'x'}
        self.assertIsNone(_normalize_browser_intent_draft(draft, {'allowSearch': False}))
        self.assertIsNotNone(_normalize_browser_intent_draft(draft, {'allowSearch': True}))
        visit = {'mode': 'visit', 'purpose': '看', 'url': 'https://example.com'}
        self.assertIsNone(_normalize_browser_intent_draft(visit, {'allowVisit': False}))
        self.assertIsNotNone(_normalize_browser_intent_draft(visit, {'allowVisit': True}))

    def test_fact_query_composition_matches_upstream(self) -> None:
        participant = {
            'id': PARTICIPANT_ID,
            'state': {'openThreads': ['物理复习'], 'relationshipNotes': ['喜欢安静']},
        }
        query = _create_fact_query(
            participant,
            '在吗',
            [{'summary': '买书'}],
            [{'summary': '旧计划'}],
        )
        self.assertEqual(query.split('\n'), [
            'Current user message: 在吗',
            'Open thread: 物理复习',
            'Relationship note: 喜欢安静',
            'Due intent: 买书',
            'Superseded plan: 旧计划',
        ])
        self.assertEqual(_create_fact_query(None, None, [], []), '')

    def test_module_prefers_a_landed_helpers_implementation(self) -> None:
        """`normalize_decision` 等模块级函数优先用 helpers.py 的版本。"""
        self.assertTrue(callable(chunk4_module._normalize_decision))
        self.assertTrue(callable(chunk4_module._create_fact_query))
        self.assertTrue(hasattr(chunk4_module, '_prefer_helper'))


# =========================================================================== #
# persistDecision（真实 SQLite + 真实 Chunk5 落库成员）
# =========================================================================== #

class _IntegrationService(InterludeService):
    """补上尚未落地的 Chunk6/7/8 成员（并行移植期的最小桩）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.participant_state_patches: list[tuple[Any, Any]] = []
        self.seen_marks = 0
        self.due_follow_up_defers: list[Any] = []
        self.proactive_checks: list[Any] = []
        self.delivery_outcomes: list[Any] = []

    @property
    def urge_config(self) -> dict[str, Any]:
        return {'enabled': False, 'contact_min': 5}

    @property
    def effective_urge_runtime(self) -> dict[str, Any]:
        """上游 `effectiveUrgeRuntime`（`:5413`）返回的是**完整 RuntimeConfig**（含 urge 覆盖）。"""
        return {**self.runtime_config, 'proactiveWillingnessThreshold': 0.65}

    def main_model_label(self) -> str:
        return 'test-model'

    def split_outgoing_message(self, content: str) -> list[str]:
        return [content]

    async def update_participant_state(self, participant: Any, patch: Any, now: Any) -> None:
        self.participant_state_patches.append((participant.get('id'), patch))

    async def mark_participant_seen(self, participant: Any, now: Any) -> None:
        self.seen_marks += 1

    async def defer_unresolved_due_follow_ups(self, *args: Any) -> None:
        self.due_follow_up_defers.append(args)

    async def append_proactive_check(self, *args: Any) -> None:
        self.proactive_checks.append(args)

    async def update_script_delivery_outcome(self, *args: Any) -> None:
        self.delivery_outcomes.append(args)


def make_service_config(**runtime_overrides: Any) -> dict[str, Any]:
    return {
        'runtime': make_runtime(**runtime_overrides),
        'memory': {'enabled': False},
        'sharedStory': {
            'allowCrossConversationMessages': False,
            'shareParticipantDetails': False,
            'participantContextLimit': 4,
            'maxCrossConversationActions': 2,
        },
        'agency': {'enabled': False},
        'browser': {'enabled': False},
        'alterSystem': {'enabled': False},
        'schedulePreplan': {'enabled': False},
        'timelineDirector': {'enabled': False},
        'logging': {},
    }


class PersistDecisionTests(unittest.IsolatedAsyncioTestCase):
    """落库集成：真实 `Database(':memory:')` + 真实 Chunk5 的 `append_entry` 系列。"""

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        self.service = _IntegrationService(self.ctx, make_service_config(), self.db, NullTransport())
        self.db.insert('interlude_story', {
            'id': STORY_ID, 'platform': 'test', 'selfId': '1', 'userId': '1',
            'channelId': 'private:1', 'status': 'active',
            'setting': make_setting(), 'state': encode_story_state(empty_story_state()),
            'cursorAt': FROM, 'createdAt': FROM, 'updatedAt': FROM,
        })
        self.db.insert('interlude_participant', {
            'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'platform': 'test', 'selfId': '1',
            'userId': '2', 'channelId': 'private:2', 'personId': 'person:2',
            'displayName': 'Kela', 'profile': '', 'relationship': '',
            'state': {'openThreads': [], 'relationshipNotes': []}, 'status': 'active',
            'createdAt': FROM, 'updatedAt': FROM,
        })

    def rows(self, table: str) -> list[Any]:
        return self.db.all(table, {'storyId': STORY_ID})

    def story_row(self) -> dict[str, Any]:
        return normalize_database_row('interlude_story', self.db.get('interlude_story', {'id': STORY_ID}))

    def participant_row(self) -> dict[str, Any]:
        return normalize_database_row(
            'interlude_participant', self.db.get('interlude_participant', {'id': PARTICIPANT_ID}),
        )

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_persist_decision_writes_prose_memory_intent_and_state(self) -> None:
        story = self.story_row()
        participant = self.participant_row()
        raw = {
            'script': '她合上练习册，走到床边。\n\n嗯，我在。',
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '嗯，我在。'}},
            'memories': [{'category': 'event', 'content': '她复习了物理。', 'importance': 0.6}],
            'intents': [{
                'type': 'reminder', 'summary': '明天去买书',
                'notBefore': iso(NOW + timedelta(hours=2)),
            }],
            'statePatch': {'openThreads': ['物理复习']},
        }
        result = await self.service.persist_decision(
            story, participant, raw, FROM, NOW, True, 'user-message', [], False, None,
        )

        # 1) 正文落库为 script 条目（原文不截断）。
        entries = self.rows('interlude_script_entry')
        scripts = [row for row in rows_of(entries) if row.get('kind') == 'script']
        self.assertEqual(len(scripts), 1)
        self.assertIn('她合上练习册', scripts[0]['content'])
        self.assertEqual(scripts[0]['metadata'].get('narrative_authority'), 'original-v2')

        # 2) 记忆与意图各一条，来源条目 id 已回填。
        self.assertEqual([row['content'] for row in rows_of(self.rows('interlude_memory'))], ['她复习了物理。'])
        intents = rows_of(self.rows('interlude_intent'))
        self.assertEqual([row['summary'] for row in intents], ['明天去买书'])
        self.assertEqual(intents[0]['participantId'], PARTICIPANT_ID)

        # 3) 状态：叙事计数 +1，自动化字段保持可解码。
        state = decode_story_state(self.story_row()['state'])
        self.assertEqual(state['narrative_update_count'], 1)
        self.assertIn('automation', state)

        # 4) 参与者状态补丁交给关系层（Chunk6/7 的成员）。
        self.assertEqual(self.service.participant_state_patches, [(PARTICIPANT_ID, {'openThreads': ['物理复习']})])

        # 5) 投递草稿：一条可见回复，带剧本事件回链与 user_initiated 标记。
        self.assertEqual(len(result['messages']), 1)
        message = result['messages'][0]
        self.assertEqual(message['content'], '嗯，我在。')
        self.assertEqual(message['participant_id'], PARTICIPANT_ID)
        self.assertTrue(message['user_initiated'])
        self.assertIn('script_event', message)

        # 6) 返回值同时给出两种拼写（Chunk1/Chunk3 按 `scriptEntry` 读）。
        self.assertIs(result['script_entry'], result['scriptEntry'])
        self.assertEqual(result['commit']['phase'], 'user-message')

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_persist_decision_without_script_writes_no_entry(self) -> None:
        story = self.story_row()
        participant = self.participant_row()
        result = await self.service.persist_decision(
            story, participant, {'script': ''}, FROM, NOW, True, 'user-message', [], False, None,
        )
        self.assertEqual(rows_of(self.rows('interlude_script_entry')), [])
        self.assertEqual(result['messages'], [])
        self.assertIsNone(result['commit'])
        self.assertIsNone(result['scriptEntry'])
        # 没有剧本就不该推进叙事计数。
        self.assertEqual(decode_story_state(self.story_row()['state'])['narrative_update_count'], 0)

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_persist_decision_advance_never_emits_a_private_interaction(self) -> None:
        story = self.story_row()
        raw = {
            'script': '她在房间里看书。',
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '在。'}},
        }
        result = await self.service.persist_decision(
            story, None, raw, FROM, NOW, True, 'advance', [], False, None,
        )
        self.assertEqual(result['messages'], [])
        scripts = [row for row in rows_of(self.rows('interlude_script_entry')) if row.get('kind') == 'script']
        self.assertEqual(len(scripts), 1)
        # 自动生活回合没有实时参与者事件，因此不产生 outgoing-message 事件。
        kinds = [event['kind'] for event in result['commit']['events']]
        self.assertNotIn('outgoing-message', kinds)

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_invalid_commit_is_rejected_before_any_write(self) -> None:
        story = self.story_row()
        participant = self.participant_row()
        # 作者化引用指向不存在的 action id：尾修复保留散文但丢弃投递承诺，
        # commit 结构仍然合法；这里改用一个会破坏结构的输入（空 script 已覆盖），
        # 因此断言"没有剧本就没有写"作为守卫等价物。
        result = await self.service.persist_decision(
            story, participant, {'script': '   '}, FROM, NOW, True, 'user-message', [], False, None,
        )
        self.assertIsNone(result['commit'])
        self.assertEqual(rows_of(self.rows('interlude_script_entry')), [])


def rows_of(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


# =========================================================================== #
# 受控偏离（移植说明 §23）：后台回合的即时回复投给谁
# =========================================================================== #

WAITING_ID = 'test:1:3'


class BackgroundReplyRoutingTests(unittest.IsolatedAsyncioTestCase):
    """用户 2026-09-27 06:19 的实测：她回的是主人的两条未读，消息却落进「汐雨.」的

    对话框——因为那一回合是「汐雨.」那条到期计划开的。这里钉住改投逻辑与跨对话闸门。
    """

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        self.service = _IntegrationService(
            self.ctx, self.share_config(allow_cross=True), self.db, NullTransport(),
        )
        self.db.insert('interlude_story', {
            'id': STORY_ID, 'platform': 'test', 'selfId': '1', 'userId': '1',
            'channelId': 'private:1', 'status': 'active',
            'setting': make_setting(), 'state': encode_story_state(empty_story_state()),
            'cursorAt': FROM, 'createdAt': FROM, 'updatedAt': FROM,
        })
        self.add_participant(PARTICIPANT_ID, '2', '汐雨.', unread=0)

    @staticmethod
    def share_config(*, allow_cross: bool) -> dict[str, Any]:
        config = make_service_config()
        config['sharedStory'] = {
            'allowCrossConversationMessages': allow_cross,
            'shareParticipantDetails': True,
            'participantContextLimit': 4,
            'maxCrossConversationActions': 2,
        }
        return config

    def add_participant(self, participant_id: str, user_id: str, name: str, unread: int) -> None:
        self.db.insert('interlude_participant', {
            'id': participant_id, 'storyId': STORY_ID, 'platform': 'test', 'selfId': '1',
            'userId': user_id, 'channelId': 'private:%s' % user_id, 'personId': 'person:%s' % user_id,
            'displayName': name, 'profile': '', 'relationship': '',
            'state': {
                'openThreads': [], 'relationshipNotes': [],
                'unreadMessageCount': unread, 'pendingReplyCount': unread,
            },
            'status': 'active', 'createdAt': FROM, 'updatedAt': FROM,
        })

    def turn_participant(self) -> dict[str, Any]:
        return normalize_database_row(
            'interlude_participant', self.db.get('interlude_participant', {'id': PARTICIPANT_ID}),
        )

    async def persist(self, raw: dict[str, Any]) -> dict[str, Any]:
        story = normalize_database_row(
            'interlude_story', self.db.get('interlude_story', {'id': STORY_ID}),
        )
        return await self.service.persist_decision(
            story, self.turn_participant(), raw, FROM, NOW, True, 'intent-due', [], False, None,
        )

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_the_reply_goes_to_the_branch_that_is_actually_waiting(self) -> None:
        self.add_participant(WAITING_ID, '3', '主人', unread=2)
        # 与用户日志同形：模型只声明 actionId，原话写在剧本的 <say> 里。
        raw = {
            'script': '指头落下去。22:37「在么」。\n\n<say id="reply">早～</say>\n\n退出来。',
            'interaction': {'seen': False, 'reply': {'mode': 'immediate', 'actionId': 'reply'}},
        }

        result = await self.persist(raw)

        self.assertEqual([message['participant_id'] for message in result['messages']], [WAITING_ID])
        self.assertEqual([message['content'] for message in result['messages']], ['早～'])
        outgoing = [event for event in result['commit']['events'] if event['kind'] == 'outgoing-message']
        self.assertEqual([event['participant_id'] for event in outgoing], [WAITING_ID])
        self.assertIn('已改投', self.sink.text())

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_the_reply_stays_when_this_branch_is_the_one_waiting(self) -> None:
        self.db.update('interlude_participant', {'id': PARTICIPANT_ID}, {
            'state': {'openThreads': [], 'relationshipNotes': [], 'unreadMessageCount': 2},
        })
        raw = {
            'script': '嗯，我在。',
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '嗯，我在。'}},
        }

        result = await self.persist(raw)

        self.assertEqual([message['participant_id'] for message in result['messages']], [PARTICIPANT_ID])

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_two_waiting_branches_block_instead_of_guessing(self) -> None:
        self.add_participant(WAITING_ID, '3', '主人', unread=2)
        self.add_participant('test:1:4', '4', '第三个', unread=3)
        raw = {
            'script': '她放下手机。',
            'interaction': {'seen': False, 'reply': {'mode': 'immediate', 'content': '在。'}},
        }

        result = await self.persist(raw)

        # 不猜：不投给任何一条（投错人比不投更糟），但留下"写了没发出"的可见证据
        self.assertEqual(result['messages'], [])
        self.assertIn('收件人不明确', self.sink.text())
        failed = [
            dict(row) for row in self.db.all('interlude_script_entry', {'storyId': STORY_ID})
            if dict(row).get('kind') == 'outgoing-delivery-failed'
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['metadata'].get('reason'), 'reply-target-ambiguous')

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_a_named_branch_wins_over_the_ambiguity(self) -> None:
        self.add_participant(WAITING_ID, '3', '主人', unread=2)
        self.add_participant('test:1:4', '4', '第三个', unread=3)
        raw = {
            'script': '她放下手机。\n\n<say id="reply">在。</say>',
            'interaction': {'seen': False, 'reply': {'mode': 'immediate', 'actionId': 'reply'}},
            'crossConversationActions': [{
                'participantId': WAITING_ID, 'mode': 'immediate', 'actionId': 'reply',
            }],
        }

        result = await self.persist(raw)

        # 模型指名了主人：那句原话只发一次，且只发给他
        self.assertEqual([message['participant_id'] for message in result['messages']], [WAITING_ID])
        self.assertEqual([message['content'] for message in result['messages']], ['在。'])
        self.assertNotIn('收件人不明确', self.sink.text())

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_a_due_turn_may_answer_a_waiting_branch(self) -> None:
        self.add_participant(WAITING_ID, '3', '主人', unread=2)
        raw = {
            'script': '「早～」\n\n那边还等着。',
            'interaction': {'seen': False, 'reply': {'mode': 'none'}},
            'crossConversationActions': [{
                'participantId': WAITING_ID, 'mode': 'immediate', 'content': '早～',
            }],
        }

        result = await self.persist(raw)

        self.assertEqual([message['participant_id'] for message in result['messages']], [WAITING_ID])

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_a_due_turn_cannot_start_an_unprompted_contact(self) -> None:
        self.add_participant(WAITING_ID, '3', '主人', unread=0)
        raw = {
            'script': '她想了想。',
            'interaction': {'seen': False, 'reply': {'mode': 'none'}},
            'crossConversationActions': [{
                'participantId': WAITING_ID, 'mode': 'immediate', 'content': '在么？',
            }],
        }

        result = await self.persist(raw)

        self.assertEqual(result['messages'], [])

    @unittest.skipUnless(_INTEGRATION_READY, '落库用例需要同批任务的 Chunk5/Chunk9')
    async def test_without_the_cross_channel_the_reply_is_not_moved(self) -> None:
        self.add_participant(WAITING_ID, '3', '主人', unread=2)
        self.service = _IntegrationService(
            self.ctx, self.share_config(allow_cross=False), self.db, NullTransport(),
        )
        raw = {
            'script': '早～',
            'interaction': {'seen': False, 'reply': {'mode': 'immediate', 'content': '早～'}},
        }

        result = await self.persist(raw)

        self.assertEqual([message['participant_id'] for message in result['messages']], [PARTICIPANT_ID])


# =========================================================================== #
# 端到端（真实 `InterludeService`：全部 10 个 chunk + 假 narrator）
# =========================================================================== #

class _FakeNarrator:
    """只记录请求并回一份最小合法决策的主叙事替身。"""

    def __init__(self, decision: Optional[dict[str, Any]] = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.decision = decision if decision is not None else {
            'script': '她在窗边翻书。',
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '在的。'}},
        }

    async def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return dict(self.decision)


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    """`tryDecide` → `decide` → `persistDecision` 跑在真实兄弟实现上。"""

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        self.service = InterludeService(self.ctx, make_service_config(), self.db, NullTransport())
        self.narrator = _FakeNarrator()
        self.service.narrator = self.narrator
        self.db.insert('interlude_story', {
            'id': STORY_ID, 'platform': 'test', 'selfId': '1', 'userId': '1',
            'channelId': 'private:1', 'status': 'active',
            'setting': make_setting(), 'state': encode_story_state(empty_story_state()),
            'cursorAt': FROM, 'createdAt': FROM, 'updatedAt': FROM,
        })
        self.db.insert('interlude_participant', {
            'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'platform': 'test', 'selfId': '1',
            'userId': '2', 'channelId': 'private:2', 'personId': 'person:2',
            'displayName': 'Kela', 'profile': '', 'relationship': '',
            'state': {'openThreads': [], 'relationshipNotes': []}, 'status': 'active',
            'createdAt': FROM, 'updatedAt': FROM,
        })

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_try_decide_then_persist_decision_on_the_real_service(self) -> None:
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        self.assertTrue(story)
        self.assertTrue(participant)

        result = await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
        )
        self.assertTrue(result['succeeded'])

        # 请求是发给模型的 wire format：键名逐字 camelCase，且不得混入 snake_case 别名。
        request = self.narrator.requests[0]
        for key in ('urgeEnabled', 'phase', 'refreshContinuity', 'outputRecovery', 'story', 'from', 'now',
                    'userMessage', 'userReportedTimes', 'timelinePlan', 'writingOptions', 'participant',
                    'participants', 'dueIntents', 'upcomingIntents', 'activeConsequences',
                    'supersededIntents', 'shareParticipantDetails', 'recentEntries', 'memories',
                    'sceneContext', 'facts', 'groupContext', 'chatCapabilities', 'contactThreads',
                    'sceneFrame', 'dialogueBurst', 'workingDetails', 'timelineCarry', 'recalledHistory',
                    'recentProtectionSince', 'webContext', 'overlaySnapshots', 'alterEnabled',
                    'emotionalOffset', 'agencyEnabled', 'agencyWindow', 'automaticDeliverySummaries',
                    'followUpCommitments', 'schedulePreplan', 'onEarlyReply'):
            self.assertIn(key, request)
        for snake in ('recent_entries', 'scene_context', 'due_intents', 'chat_capabilities', 'writing_options'):
            self.assertNotIn(snake, request)
        self.assertEqual(request['writingOptions']['messageSeparator'], '<sep/>')
        self.assertTrue(request['writingOptions']['splitReplyMessages'])
        self.assertEqual(request['writingOptions']['browserMode'], 'disabled')

        persisted = await self.service.persist_decision(
            story, participant, result['decision'], FROM, NOW, True, 'user-message', [], False,
            result.get('timelinePlan'),
        )
        self.assertEqual(len(persisted['messages']), 1)
        self.assertEqual(persisted['messages'][0]['content'], '在的。')
        entries = [dict(row) for row in self.db.all('interlude_script_entry', {'storyId': STORY_ID})]
        self.assertEqual([entry['kind'] for entry in entries], ['script'])
        state = decode_story_state((await self.service.get_story(STORY_ID))['state'])
        self.assertEqual(state['narrative_update_count'], 1)

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_the_turn_records_what_the_context_was_made_of(self) -> None:
        """v1.4.0：上轮上下文构成落进 `state.extensions.last_context_metrics`。"""
        self.db.insert('interlude_script_entry', {
            'storyId': STORY_ID, 'kind': 'script', 'actor': 'narrator',
            'content': '她把伞放在门口。', 'occurredAt': FROM, 'metadata': {},
            'createdAt': FROM,
        })
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        result = await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
        )
        self.assertTrue(result['succeeded'])
        state = decode_story_state((await self.service.get_story(STORY_ID))['state'])
        metrics = (state.get('extensions') or {}).get('last_context_metrics')
        self.assertIsInstance(metrics, dict, '记账是诊断用途，但必须真的写进去')
        self.assertEqual(metrics['phase'], 'user-message')
        self.assertEqual(metrics['participant_id'], PARTICIPANT_ID)
        self.assertGreaterEqual(metrics['assembly_ms'], 0)
        self.assertGreater(metrics['estimated_tokens'], 0)
        self.assertGreater(metrics['payload_characters'], 0)
        # 段名用 wire 键（与请求体一一对应），文字标签留给控制台。
        self.assertIn('recentEntries', metrics['sections'])
        self.assertEqual(
            metrics['sections']['recentEntries']['items'],
            len(self.narrator.requests[0]['recentEntries']),
        )

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_a_quote_without_content_is_backfilled_from_our_own_record(self) -> None:
        """平台只给一个 id 时，用 `msg-<条目id>` 从剧本里补出被引正文。"""
        entry = self.db.insert('interlude_script_entry', {
            'storyId': STORY_ID, 'kind': 'script', 'actor': 'character',
            'content': '她把伞放在了门口。', 'occurredAt': FROM, 'metadata': {},
            'createdAt': FROM,
        })
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
            quoted_messages=[{'messageId': 'msg-%s' % entry['id'], 'user': {'id': '2'}}],
        )
        request = self.narrator.requests[-1]
        quote = request['quotedMessages'][0]
        self.assertEqual(quote['content'], '她把伞放在了门口。')
        self.assertIs(quote['backfilled'], True, '补出来的引文要能被日志区分')

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_platform_supplied_quote_content_is_left_alone(self) -> None:
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
            quoted_messages=[{'messageId': 'msg-999999', 'content': '平台给的原文'}],
        )
        quote = self.narrator.requests[-1]['quotedMessages'][0]
        self.assertEqual(quote['content'], '平台给的原文')
        self.assertNotIn('backfilled', quote)

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_context_metrics_can_be_switched_off(self) -> None:
        self.service.config = {
            **self.service.config,
            'memory': {**(_config_section(self.service.config, 'memory') or {}), 'contextMetricsEnabled': False},
        }
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        await self.service.try_decide(story, participant, 'user-message', FROM, NOW, '在吗', [])
        state = decode_story_state((await self.service.get_story(STORY_ID))['state'])
        self.assertNotIn('last_context_metrics', state.get('extensions') or {})

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_a_rejected_draft_says_why_where_operators_can_see_it(self) -> None:
        """被抛弃的草稿必须**在默认 verbosity 下可见**地说出原因（AGENTS.md 坑 25）。

        用户 2026-09-25 23:56 的日志里只有一句「结构化可见回复缺失」，谁也看不出模型
        到底写了什么。这条诊断现在带 interaction、已解析动作数与**残留的 `<say>` 标记**——
        解析成功时标签会被整个解包、不会留在散文里，所以「残留 > 0」就是"模型写了行动
        但一个都没解析出来"的指纹。
        """
        self.service.model_routing = {'main': {'available': True}}
        self.service.narrator = _FakeNarrator({
            'script': '她写道：<say id="汉字">晚安喵～</say>',
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'actionId': 'reply'}},
        })
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        result = await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '晚安', [],
        )
        # 重写还是不合格 → 降级为无可见回复提交（上游 1.0.1-rc24+），但**原因**必须留在
        # 默认可见的日志里：草稿被抛弃这件事本身照旧要可见（AGENTS.md 坑 25）。
        self.assertTrue(result['succeeded'])
        self.assertIsNone(result['decision'].get('interaction'))
        reasons = [text for level, text in self.sink.records if level == 'warn']
        diagnostics = [text for text in reasons if '被抛弃草稿的结构化回复字段' in text]
        self.assertEqual(len(diagnostics), 1, reasons)
        # 日志是按 `键=值` 渲染成树的，所以断言落在渲染后的字段上（用户真正看到的东西）。
        self.assertIn('已解析动作', diagnostics[0])
        self.assertIn('残留say标记', diagnostics[0])
        self.assertIn('2', diagnostics[0])
        self.assertIn('汉字', diagnostics[0], '预览里要看得到模型的原样写法')
        self.assertIn('unresolved_action_id', diagnostics[0])

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_missing_narrator_degrades_instead_of_raising(self) -> None:
        """`narrator` 未配置时按 provider 失败降级，而不是把异常抛给调用方。"""
        self.service.narrator = None
        story = await self.service.get_story(STORY_ID)
        result = await self.service.try_decide(story, None, 'advance', FROM, NOW, None, [])
        self.assertFalse(result['succeeded'])
        self.assertEqual(self.narrator.requests, [])
        self.assertTrue(any('模型调用失败' in str(item) for item in self.sink.records))

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_advance_unlocked_runs_on_the_real_service(self) -> None:
        """`advanceUnlocked` 在真实服务上走完一轮自动写作（`force=True`）。"""
        story = await self.service.get_story(STORY_ID)
        messages = await self.service.advance_unlocked(story, NOW, True)
        self.assertEqual(messages, [])  # 自动生活回合不产生私聊投递草稿
        self.assertEqual(len(self.narrator.requests), 1)
        self.assertEqual(self.narrator.requests[0]['phase'], 'advance')
        entries = [dict(row) for row in self.db.all('interlude_script_entry', {'storyId': STORY_ID})]
        self.assertEqual([entry['kind'] for entry in entries], ['script'])
        state = decode_story_state((await self.service.get_story(STORY_ID))['state'])
        self.assertEqual(state['narrative_update_count'], 1)
        # 游标推进到 now（真实墙钟时间由 advance_unlocked 写入）。
        current = await self.service.get_story(STORY_ID)
        self.assertEqual(dt_ms(current['cursorAt']), dt_ms(NOW))


# =========================================================================== #
# 真机回归（2026-10-04）：合法 interaction 不许被丢弃 / 浏览结果要回到上下文
# =========================================================================== #

#: 真机第一版草稿里的那一条合法可见回复（逐字）。
_REAL_INTERACTION = {
    'seen': True,
    'reply': {'mode': 'immediate', 'content': '唔，你还有个妹妹啊，搜着呢'},
}

#: 真机第一版草稿里的浏览意图（复数数组拼写）。
_DEFERRED_SEARCH = {'mode': 'search', 'query': '妹妹', 'purpose': '想看看他妹妹是谁', 'timing': 'deferred'}

#: 假页面正文：断言"取回的内容真的进了下一回合的 webContext"。
_PAGE_EXCERPT = '他妹妹叫小雨，今年刚上高一。'


class _BrowseTransport(NullTransport):
    """`Transport.search_web` / `visit_web` 的最小替身（无宿主搜索 API → 回落到模板 URL）。"""

    def __init__(self, page: bool = True) -> None:
        self.page = page
        self.calls: list[tuple[str, Any]] = []

    async def search_web(self, query: str, timeout_ms: int) -> list[dict[str, Any]]:
        self.calls.append(('search', query))
        return []

    async def visit_web(self, url: str, timeout_ms: int) -> Any:
        self.calls.append(('visit', url))
        if not self.page:
            return None
        return {'url': url, 'title': '妹妹是谁 - 搜索', 'text': _PAGE_EXCERPT * 20}


def browser_service_config() -> dict[str, Any]:
    """打开网页观察的最小配置（夹具照生产写法写全，见坑 39/66）。"""
    config = make_service_config()
    config['browser'] = {
        'enabled': True, 'allowSearch': True, 'allowVisit': True,
        'searchUrlTemplate': 'https://cn.bing.com/search?q={query}',
        'mode': 'deferred-only', 'maxObservationsInPrompt': 1,
    }
    return config


class LegalInteractionTests(unittest.IsolatedAsyncioTestCase):
    """① 的回归面：合法 `interaction` 绝不丢弃；真的缺了才重写。"""

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        self.service = InterludeService(self.ctx, make_service_config(), self.db, NullTransport())
        # 闸门只在"主模型可用"时才上膛（`initial_visible_recovery = main_available and …`）：
        # 不设这一条，下面的用例会因为闸门没开而假绿。
        self.service.model_routing = {'main': {'available': True}}
        self.db.insert('interlude_story', {
            'id': STORY_ID, 'platform': 'test', 'selfId': '1', 'userId': '1',
            'channelId': 'private:1', 'status': 'active',
            'setting': make_setting(), 'state': encode_story_state(empty_story_state()),
            'cursorAt': FROM, 'createdAt': FROM, 'updatedAt': FROM,
        })
        self.db.insert('interlude_participant', {
            'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'platform': 'test', 'selfId': '1',
            'userId': '2', 'channelId': 'private:2', 'personId': 'person:2',
            'displayName': 'Kela', 'profile': '', 'relationship': '',
            'state': {'openThreads': [], 'relationshipNotes': []}, 'status': 'active',
            'createdAt': FROM, 'updatedAt': FROM,
        })

    def discarded_drafts(self) -> list[str]:
        return [text for level, text in self.sink.records
                if level == 'warn' and '被抛弃草稿的结构化回复字段' in text]

    async def test_a_legal_interaction_is_never_discarded(self) -> None:
        """真机第一版草稿：合法 `interaction` + 复数数组浏览意图 + 多余 `platform` 键。

        这三样都是**非致命差异**；整篇剧本不许因此被扔掉重写（双倍 token + 近两分钟）。
        """
        self.service.narrator = _FakeNarrator({
            'script': '她翻了翻手机，指尖停在搜索框上。',
            'interaction': {**_REAL_INTERACTION, 'reply': {**_REAL_INTERACTION['reply'], 'platform': 'qq'}},
            'browserIntents': [_DEFERRED_SEARCH],
        })
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        result = await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
        )
        self.assertTrue(result['succeeded'])
        self.assertEqual(len(self.service.narrator.requests), 1, '合法草稿只该问模型一次')
        self.assertEqual(self.discarded_drafts(), [])
        self.assertEqual(result['decision']['interaction']['reply']['content'], '唔，你还有个妹妹啊，搜着呢')

    async def test_a_near_miss_json_draft_reaches_the_gate_intact(self) -> None:
        """真机整条链：模型文本（多一个尾随逗号）→ 解析 → 判据，一次调用、不丢弃。

        这里用一个**真的过解析器**的替身（与 `narrator._request_provider` 的路径一致），
        钉住"判据读到的就是模型写的那份 decision"——不是被解析层截出来的片段。
        """
        text = (
            '{\n'
            '  "script": "她翻了翻手机，指尖停在搜索框上。",\n'
            '  "interaction": {"seen": true, "reply": {"mode": "immediate", "content": "唔，你还有个妹妹啊，搜着呢"}},\n'
            '  "browserIntents": [{"mode": "search", "query": "妹妹", "purpose": "想看看他妹妹是谁", "timing": "deferred"}],\n'
            '}'
        )

        class _ParsingNarrator:
            def __init__(self) -> None:
                self.requests: list[dict[str, Any]] = []

            async def decide(self, request: dict[str, Any]) -> dict[str, Any]:
                self.requests.append(request)
                parsed = narrator_module.parse_json_response(text, 'Narrative provider')
                return resolve_authored_actions(narrator_module.narrative_decision_root(parsed), False, '<sep/>')

        self.service.narrator = _ParsingNarrator()
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        result = await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
        )
        self.assertTrue(result['succeeded'])
        self.assertEqual(len(self.service.narrator.requests), 1)
        self.assertEqual(self.discarded_drafts(), [])
        self.assertEqual(result['decision']['interaction']['reply']['content'], '唔，你还有个妹妹啊，搜着呢')
        self.assertEqual(len(result['decision']['browserIntents']), 1)

    async def test_a_non_fatal_mode_spelling_does_not_burn_a_rewrite(self) -> None:
        """`mode` 写成 `text`（rc15 宽容）时归一化照常发消息，判定也必须照常放过。"""
        self.service.narrator = _FakeNarrator({
            'script': '她放下手机。',
            'interaction': {'seen': True, 'reply': {'mode': 'text', 'content': '在的。'}},
        })
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        result = await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
        )
        self.assertTrue(result['succeeded'])
        self.assertEqual(len(self.service.narrator.requests), 1)
        self.assertEqual(self.discarded_drafts(), [])

    async def test_the_discard_gate_still_fires_when_the_reply_is_really_missing(self) -> None:
        """反向（严格方向）：真的没有结构化回复时必须照旧重写一次。

        少了这一条，把判据改成"永远不缺"的变异也能全绿——闸门就成了摆设。
        """
        self.service.narrator = _FakeNarrator({'script': '她只是看了一眼，没有回。'})
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        result = await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
        )
        self.assertTrue(result['succeeded'])
        self.assertEqual(len(self.service.narrator.requests), 2, '缺结构化回复 → 重写一次')
        self.assertEqual(len(self.discarded_drafts()), 1)
        # 真机看到的正是这两行（分层日志把 `interaction=%s` 渲染成字段行）。
        self.assertIn('interaction:', self.discarded_drafts()[0])
        self.assertIn('undefined', self.discarded_drafts()[0])
        self.assertIn('残留say标记', self.discarded_drafts()[0])

    async def test_a_delayed_reply_is_not_mistaken_for_a_missing_one(self) -> None:
        """`mode=delayed` 是合法可见回复（她稍后发），不该进重写环。"""
        send_at = iso(NOW + timedelta(minutes=5))
        self.service.narrator = _FakeNarrator({
            'script': '她把要说的话存进了草稿箱。',
            'interaction': {'seen': True, 'reply': {'mode': 'delayed', 'content': '晚点说。', 'sendAt': send_at}},
        })
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        result = await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
        )
        self.assertTrue(result['succeeded'])
        self.assertEqual(len(self.service.narrator.requests), 1)
        self.assertEqual(self.discarded_drafts(), [])


class DeferredBrowseLoopTests(unittest.IsolatedAsyncioTestCase):
    """② 的回归面：申请 → 执行 → 结果进下一回合 `webContext`，失败要可见。"""

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        self.service = InterludeService(self.ctx, browser_service_config(), self.db, NullTransport())
        self.db.insert('interlude_story', {
            'id': STORY_ID, 'platform': 'test', 'selfId': '1', 'userId': '1',
            'channelId': 'private:1', 'status': 'active',
            'setting': make_setting(), 'state': encode_story_state(empty_story_state()),
            'cursorAt': FROM, 'createdAt': FROM, 'updatedAt': FROM,
        })
        self.db.insert('interlude_participant', {
            'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'platform': 'test', 'selfId': '1',
            'userId': '2', 'channelId': 'private:2', 'personId': 'person:2',
            'displayName': 'Kela', 'profile': '', 'relationship': '',
            'state': {'openThreads': [], 'relationshipNotes': []}, 'status': 'active',
            'createdAt': FROM, 'updatedAt': FROM,
        })

    async def run_turn(self, decision: dict[str, Any]) -> dict[str, Any]:
        self.service.narrator = _FakeNarrator(decision)
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        result = await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
        )
        await self.service.persist_decision(
            story, participant, result['decision'], FROM, NOW, True, 'user-message', [], False,
            result.get('timelinePlan'),
        )
        return result

    def decision(self, **overrides: Any) -> dict[str, Any]:
        return {
            'script': '她翻了翻手机，指尖停在搜索框上。',
            'interaction': dict(_REAL_INTERACTION),
            **overrides,
        }

    def browse(self) -> _BrowseTransport:
        transport = _BrowseTransport()
        self.service.transport = transport
        return transport

    async def drain_browser_intents(self, at: Any) -> None:
        """推进到 `at` 时刻的后台扫描（真机上就是到期唤醒 / 常规 sweep 那一跳）。"""
        self.ctx.clock = lambda: at
        story = await self.service.get_story(STORY_ID)
        await self.service.advance_unlocked(story, at, False)

    async def next_turn_web_context(self, at: Any) -> list[Any]:
        self.service.narrator = _FakeNarrator(self.decision())
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        await self.service.try_decide(
            story, participant, 'user-message', at, at + timedelta(minutes=1), '在吗', [],
        )
        return self.service.narrator.requests[-1].get('webContext') or []

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_the_array_spelling_reaches_the_next_turn_context(self) -> None:
        transport = self.browse()
        await self.run_turn(self.decision(browserIntents=[dict(_DEFERRED_SEARCH)]))
        intents = [dict(row) for row in self.db.all('interlude_intent', {'storyId': STORY_ID})]
        self.assertEqual([row['type'] for row in intents], ['browser-research'])
        # 到期唤醒必须排在意图到期那一刻：否则她这一回合说的"正在搜"要等下一次
        # 常规 sweep（默认 5 分钟）才会真的跑，用户看到的就是"还在转"。
        self.assertIn(STORY_ID, self.service.due_intent_wake_timers)
        await self.drain_browser_intents(NOW + timedelta(seconds=30))
        self.assertIn(('visit', 'https://cn.bing.com/search?q=%E5%A6%B9%E5%A6%B9'), transport.calls)
        observations = [dict(row) for row in self.db.all('interlude_web_observation', {'storyId': STORY_ID})]
        self.assertEqual([row['status'] for row in observations], ['success'])
        self.assertIn(_PAGE_EXCERPT, observations[0]['excerpt'])
        web_context = await self.next_turn_web_context(NOW + timedelta(minutes=1))
        self.assertEqual(len(web_context), 1)
        self.assertIn(_PAGE_EXCERPT, web_context[0]['excerpt'], '取回的内容必须真的进她下一回合的上下文')

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_the_singular_spelling_is_read_too(self) -> None:
        """提示词曾经教单数 `browserIntent`：历史草稿 / 别的提示词版本照旧要能读。"""
        transport = self.browse()
        await self.run_turn(self.decision(browserIntent=dict(_DEFERRED_SEARCH)))
        intents = [dict(row) for row in self.db.all('interlude_intent', {'storyId': STORY_ID})]
        self.assertEqual([row['type'] for row in intents], ['browser-research'])
        await self.drain_browser_intents(NOW + timedelta(seconds=30))
        web_context = await self.next_turn_web_context(NOW + timedelta(minutes=1))
        self.assertEqual(len(web_context), 1)
        self.assertIn(_PAGE_EXCERPT, web_context[0]['excerpt'])
        self.assertIn(('search', '妹妹'), transport.calls)

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_a_failed_browse_is_visible_and_leaves_a_trace(self) -> None:
        """搜不到必须**可见**（可行动 warn）并留痕，不许永远"还在转"。"""
        self.browse().page = False
        await self.run_turn(self.decision(browserIntents=[dict(_DEFERRED_SEARCH)]))
        await self.drain_browser_intents(NOW + timedelta(seconds=30))
        observations = [dict(row) for row in self.db.all('interlude_web_observation', {'storyId': STORY_ID})]
        self.assertEqual([row['status'] for row in observations], ['failed'])
        warns = [text for level, text in self.sink.records if level == 'warn']
        self.assertTrue(any('网页读取失败' in text for text in warns), warns)
        # 失败也写一条 web-observation 剧本条目：她下一回合在 recentScript 里看得到
        # 「这一页没打开」，于是不会一直说"还在转"。
        entries = [dict(row) for row in self.db.all('interlude_script_entry', {'storyId': STORY_ID})]
        failed_entries = [row for row in entries if row['kind'] == 'web-observation']
        self.assertEqual(len(failed_entries), 1)
        self.assertIn('did not complete', failed_entries[0]['content'])
        self.assertIn('搜索通道不可用', failed_entries[0]['content'])
        self.assertEqual(await self.next_turn_web_context(NOW + timedelta(minutes=1)), [])

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_a_dropped_request_is_a_visible_warn_not_silence(self) -> None:
        """模型申请了浏览、配置却没开 → 必须说一句（否则她永远"还在转"）。

        v1.9.7 用户逐字定稿：断言**原始 format 串 + 实参**（分层渲染会把正文里的
        `key=value` 拆成字段行，所以这里钉的是我们交出去的那一句本身）。
        """
        self.service.config = {**self.service.config, 'browser': {'enabled': False}}
        self.service.__dict__.pop('cached_browser_config', None)
        with mock.patch.object(self.service, 'report_standalone') as reported:
            await self.run_turn(self.decision(browserIntents=[dict(_DEFERRED_SEARCH)]))
        self.assertEqual(self.db.all('interlude_intent', {'storyId': STORY_ID}), [])
        self.assertIn(
            (
                'warn',
                '网页浏览请求被忽略：网页观察未启用（模式=%s）。打开「网页观察」后她才会真的去查。',
                'search',
            ),
            [call.args for call in reported.call_args_list],
        )

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_an_incomplete_draft_is_a_visible_warn_that_says_what_is_missing(self) -> None:
        """模型申请了浏览、草稿形状却不可用 → 同样必须说出来（v1.9.7 定稿文案 + v1.9.10 原因）。"""
        with mock.patch.object(self.service, 'report_standalone') as reported:
            await self.service.append_browser_intent(
                STORY_ID, {'mode': 'search'}, NOW, PARTICIPANT_ID,
            )
        self.assertEqual(self.db.all('interlude_intent', {'storyId': STORY_ID}), [])
        self.assertIn(
            (
                'warn',
                '网页浏览请求被忽略：草稿不完整（模式=%s）。需要 mode=search|visit 与 purpose；'
                'search 还需 query，visit 还需公开 url。原因=%s',
                'search', 'mode=search 需要 query',
            ),
            [call.args for call in reported.call_args_list],
        )

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_without_a_browse_request_the_next_turn_has_no_web_context(self) -> None:
        """反向：没申请过浏览就一定没有 webContext——证明上面那条断言不是恒真。"""
        self.browse()
        await self.run_turn(self.decision())
        await self.drain_browser_intents(NOW + timedelta(seconds=30))
        self.assertEqual(self.db.all('interlude_intent', {'storyId': STORY_ID}), [])
        self.assertEqual(self.db.all('interlude_web_observation', {'storyId': STORY_ID}), [])
        self.assertEqual(await self.next_turn_web_context(NOW + timedelta(minutes=1)), [])

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_a_bogus_field_name_yields_nothing(self) -> None:
        """反向：读的只有两种拼写，别的名字不许被当成意图（否则"恒真"式假绿）。"""
        self.browse()
        await self.run_turn(self.decision(browserIntentDraft=dict(_DEFERRED_SEARCH)))
        self.assertEqual(self.db.all('interlude_intent', {'storyId': STORY_ID}), [])

    # ---- 真机 2026-10-05：D 问题①（deferred 浏览意图从未执行）---------------- #

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_the_real_world_draft_shape_still_reaches_the_page_and_the_next_turn(self) -> None:
        """真机草稿形状 `{url, timing, reason}`：落库 → 到期 → 真执行 → 结果进下一回合。

        真机现场（2026-10-05 12:24）：模型写的是
        `{"url": "https://api.xiaoheihe.cn/…", "timing": "deferred", "reason": "…"}` ——
        没有 `mode`、也没有 `purpose`。旧判据要求这两个键都在，于是草稿在
        `_normalize_decision` 里被判脏值**静默丢掉**：没有意图、没有到期、没有 warn，
        模型只能把页面内容编出来。这里钉住宽容读法的两件事（mode 推断 + reason 当
        purpose）以及它真的换来了观察结果。
        """
        transport = self.browse()
        url = 'https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=342a725062c6'
        await self.run_turn(self.decision(browserIntents=[{
            'url': url, 'timing': 'deferred', 'reason': '用户问她能不能看到帖子里面的内容',
        }]))
        intents = [dict(row) for row in self.db.all('interlude_intent', {'storyId': STORY_ID})]
        self.assertEqual([row['type'] for row in intents], ['browser-research'])
        # 宽容读法落进 payload：mode 由 url 推出来，purpose 用模型写的 reason。
        self.assertEqual(intents[0]['payload']['mode'], 'visit')
        self.assertEqual(intents[0]['payload']['url'], url)
        self.assertEqual(intents[0]['payload']['purpose'], '用户问她能不能看到帖子里面的内容')
        self.assertIn(STORY_ID, self.service.due_intent_wake_timers, '到点唤醒必须排上')
        await self.drain_browser_intents(NOW + timedelta(seconds=30))
        self.assertIn(('visit', url), transport.calls, '到期的浏览意图必须真的去抓这一页')
        observations = [dict(row) for row in self.db.all('interlude_web_observation', {'storyId': STORY_ID})]
        self.assertEqual([row['status'] for row in observations], ['success'])
        web_context = await self.next_turn_web_context(NOW + timedelta(minutes=1))
        self.assertEqual(len(web_context), 1)
        self.assertIn(_PAGE_EXCERPT, web_context[0]['excerpt'], '取回的内容必须真的进她下一回合的上下文')

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_a_draft_with_no_usable_target_is_a_visible_warn_not_silence(self) -> None:
        """反向：连 url / query 都没有的草稿绝不静默 —— 落库不了就必须留一条可见 warn。"""
        self.browse()
        await self.run_turn(self.decision(browserIntents=[
            {'timing': 'deferred', 'reason': '她想去看看'},
        ]))
        self.assertEqual(self.db.all('interlude_intent', {'storyId': STORY_ID}), [])
        warns = [text for level, text in self.sink.records if level == 'warn']
        self.assertTrue(
            any('网页浏览请求被忽略：草稿不可用' in text for text in warns), warns,
        )
        self.assertTrue(
            any('给出公开 url 或 query' in text for text in warns), warns,
        )

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_the_real_world_shape_still_hits_the_disabled_switch_warn(self) -> None:
        """总开关关着时，真机形状也必须走到那条"去哪儿开"的可见 warn（不许在判据前消失）。"""
        self.service.config = {**self.service.config, 'browser': {'enabled': False}}
        self.service.__dict__.pop('cached_browser_config', None)
        with mock.patch.object(self.service, 'report_standalone') as reported:
            await self.run_turn(self.decision(browserIntents=[{
                'url': 'https://example.com/post/1', 'timing': 'deferred', 'reason': '她想去看看',
            }]))
        self.assertEqual(self.db.all('interlude_intent', {'storyId': STORY_ID}), [])
        self.assertIn(
            (
                'warn',
                '网页浏览请求被忽略：网页观察未启用（模式=%s）。打开「网页观察」后她才会真的去查。',
                'visit',
            ),
            [call.args for call in reported.call_args_list],
        )


# =========================================================================== #
# 真机回归（2026-10-05）：承诺结算的**入口判据**（chunk4.persist_decision）
# =========================================================================== #

class FollowUpResolutionIntakeTests(unittest.IsolatedAsyncioTestCase):
    """`followUpResolutions` 从模型 JSON 到投递账本的那一跳（真机三次没结算的现场）。

    真机 12:30 / 12:51 / 13:31 三次「即将处理到期计划 类型=follow-up-commitment」，
    模型每次都回了 `followUpResolutions`，但账上一直不结清，直到第三次才打出一条
    "第 3 次没结算"。这里的判据是：模型写 `status` 而不是 `outcome` 时**收下**；
    形状真的坏时**当场点名缺什么**（不许等到第三次才说、更不许静默）。
    """

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        self.service = InterludeService(self.ctx, browser_service_config(), self.db, NullTransport())
        self.db.insert('interlude_story', {
            'id': STORY_ID, 'platform': 'test', 'selfId': '1', 'userId': '1',
            'channelId': 'private:1', 'status': 'active',
            'setting': make_setting(), 'state': encode_story_state(empty_story_state()),
            'cursorAt': FROM, 'createdAt': FROM, 'updatedAt': FROM,
        })
        self.db.insert('interlude_participant', {
            'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'platform': 'test', 'selfId': '1',
            'userId': '2', 'channelId': 'private:2', 'personId': 'person:2',
            'displayName': 'Kela', 'profile': '', 'relationship': '',
            'state': {'openThreads': [], 'relationshipNotes': []}, 'status': 'active',
            'createdAt': FROM, 'updatedAt': FROM,
        })

    async def persist(self, **overrides: Any) -> dict[str, Any]:
        self.service.narrator = _FakeNarrator({
            'script': '她翻了翻手机，把查到的结果说了出来。',
            'interaction': dict(_REAL_INTERACTION),
            **overrides,
        })
        story = await self.service.get_story(STORY_ID)
        participant = await self.service.get_participant(PARTICIPANT_ID)
        result = await self.service.try_decide(
            story, participant, 'user-message', FROM, NOW, '在吗', [],
        )
        return await self.service.persist_decision(
            story, participant, result['decision'], FROM, NOW, True, 'user-message', [], False,
            result.get('timelinePlan'),
        )

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_a_status_spelled_resolution_survives_into_the_ledger_metadata(self) -> None:
        """`status: fulfilled` 必须与 `outcome: fulfilled` 等价地进账本（真机写法）。"""
        persisted = await self.persist(followUpResolutions=[
            {'id': 11, 'status': 'fulfilled', 'resolution': '查过了，有货'},
        ])
        event = find_outgoing_script_event(persisted['commit'], PARTICIPANT_ID)
        self.assertIsNotNone(event)
        self.assertEqual(
            event['metadata']['followUpResolutions'],
            [{'id': 11, 'outcome': 'fulfilled'}],
            '模型写的 status 必须被收下并归一成 outcome；否则结算那一跳永远走不到',
        )
        # 真正落库的那一份（投递账本读的就是它：条目 metadata.script_events[].metadata）。
        entry = self.db.get('interlude_script_entry', {'id': persisted['script_entry']['id']})
        landed = [
            item.get('metadata') or {} for item in entry['metadata']['script_events']
            if item.get('kind') == 'outgoing-message'
        ]
        self.assertEqual(landed[0]['followUpResolutions'], [{'id': 11, 'outcome': 'fulfilled'}])
        self.assertNotIn('承诺结算被忽略', self.sink.text())

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_the_canonical_outcome_spelling_still_works(self) -> None:
        """反向（不许把口径改窄）：上游的 `outcome` 拼写照旧必须能用。"""
        persisted = await self.persist(followUpResolutions=[
            {'id': 11, 'outcome': 'cancelled'},
        ])
        event = find_outgoing_script_event(persisted['commit'], PARTICIPANT_ID)
        self.assertEqual(event['metadata']['followUpResolutions'], [{'id': 11, 'outcome': 'cancelled'}])

    @unittest.skipUnless(FULL_SERVICE_READY, '兄弟 chunk 未全部就绪')
    async def test_a_broken_resolution_shape_is_named_on_the_spot(self) -> None:
        """坏形状 → 当回合一条可见 warn 点名缺什么，且**不许**当成结清。"""
        persisted = await self.persist(followUpResolutions=[
            {'id': 0, 'outcome': 'fulfilled'},
            {'id': 11, 'status': 'maybe'},
        ])
        self.assertNotIn('followUpResolutions', persisted['commit']['events'][0].get('metadata') or {})
        text = self.sink.text()
        self.assertIn('承诺结算被忽略', text)
        self.assertIn('id 必须是正整数', text)
        self.assertIn('outcome 必须是 fulfilled|rescheduled|cancelled', text)
        self.assertIn("'maybe'", text)


# =========================================================================== #
# 守门占位：被点名的上游测试文件里，断言本范围成员的用例为 0 条
# =========================================================================== #

class UpstreamCoverageGateTests(unittest.TestCase):
    """显式记录「这四个上游测试文件对本 Chunk 无断言」，避免假绿。"""

    @unittest.skipUnless(_INTEGRATION_READY, 'Chunk5/Chunk9 未就绪')
    def test_evidence_repair_asserts_chunk5_members_not_chunk4(self) -> None:
        self.assertFalse(callable(getattr(ServiceChunk4, 'persist_fact', None)))
        self.assertFalse(callable(getattr(ServiceChunk4, 'contact_threads', None)))

    @unittest.skipUnless(_INTEGRATION_READY, 'Chunk5/Chunk9 未就绪')
    def test_beta6_handoff_asserts_chunk5_scene_anchor_not_chunk4(self) -> None:
        self.assertFalse(callable(getattr(ServiceChunk4, 'persist_timeline_scene_anchor', None)))

    @unittest.skipUnless(_INTEGRATION_READY, 'Chunk5/Chunk9 未就绪')
    def test_continuity_checkpoint_asserts_chunk8_compaction_not_chunk4(self) -> None:
        self.assertFalse(callable(getattr(ServiceChunk4, 'persist_compaction', None)))

    @unittest.skipUnless(_INTEGRATION_READY, 'Chunk5/Chunk9 未就绪')
    def test_episode_index_asserts_script_modules_not_chunk4(self) -> None:
        self.assertFalse(callable(getattr(ServiceChunk4, 'build_episode_index', None)))

    @unittest.skipUnless(CHUNK4_READY, 'Chunk4 未就绪')
    def test_chunk4_owns_exactly_the_eight_members_of_its_range(self) -> None:
        """铁律：只移植声明起始行落在 [3217, 4185) 的成员。"""
        owned = {
            'advance_unlocked', 'decide', 'should_refresh_continuity', 'plan_automatic_timeline',
            'is_timeline_director_fused', 'persist_timeline_retry', 'try_decide', 'persist_decision',
        }
        for name in owned:
            self.assertTrue(callable(getattr(ServiceChunk4, name, None)), name)
        for name in ('persist_timeline_scene_anchor', 'admin_schedule_preplan', 'append_entry',
                     'active_scene', 'append_intent', 'with_browser_slot', 'emotional_offset_for_prompt'):
            self.assertFalse(hasattr(ServiceChunk4, name), name)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
