# -*- coding: utf-8 -*-
"""长线叙事催化器的**接线**回归面（v1.9.9，见 `docs/PORTING_NOTES.md` §89.3 / §90）。

`test_long_arc.py` 钉的是纯函数层（计分 / 触发 / 归一化 / 投影 / 输入组装）；
这里钉的是**最后一跳**——它在生产上真的被接起来了吗：

    配置总闸（`long_horizon.enabled`）
      → 剧本提交后异步点火（`chunk4.persist_decision` 尾部，上游 `service.ts:5218`）
        → 扫描 + 累计 + 判触发（chunk15）
          → **真的调一次模型**（`narrator.plan_long_arc_guidance`，复用 compaction 路由）
            → 写 `interlude_long_arc_guidance` version 1（active）
              → 下一次主叙事：`decide` 开头预热 + request 带 `longHorizonGuidance`
                （`chunk4`）→ `system_prompt` 把它拼进系统提示词（`narrator.py` + F1/F2/F3）

跑的是**真实实现**：真 `InterludeService`（chunk0..15 全在）、真 `Database`、真
`persist_decision`、真 `OpenAICompatibleNarrator` + 真 `long_arc_guidance_prompt`。
只把 HTTP 那一层换成记录请求、按系统提示词分派的替身；叙事请求用只记录不改行为的
包装（`_Recorder`）拿到底层 request。

⚠️ 库的生命周期照抄 `test_async_completion_report.py`（先取消计时器、等后台任务跑完，
再关库）：少这一步，旁路任务会在 C 层撞上已关闭的 sqlite（坑 83）。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from plugin.core import logging as interlude_logging
from plugin.core.database import Database
from plugin.core.narrator import long_arc_guidance_prompt
from plugin.core.narrator_prompts import system_prompt
from plugin.core.service import InterludeContext, InterludeService
from plugin.core.service.transport import NullTransport
from plugin.core.story_state import encode_story_state
from plugin.core.types import empty_story_state

UTC = timezone.utc
NOW = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
FROM = NOW - timedelta(minutes=5)
STORY_ID = 'character:test:1'
PARTICIPANT_ID = 'test:1:2'

#: 一份合法催化（形状对齐 `core/long_arc.normalize_long_arc_decision` 的必需字段）。
CATALYST = {
    'decision': 'prime',
    'reason': '关系结构允许一次最小的首次表达',
    'title': '窗边的位置',
    'premise': '她总在他常坐的位子边停留',
    'latentTension': '想靠近又怕表态',
    'direction': '慢慢把在意变成一件可以承认的小事',
    'emotionalCore': '想被看见',
    'intensity': 'subtle',
    'horizon': 'long',
    'confidence': 0.6,
    'evidenceEntryIds': [1],
    'firstExpression': {
        'action': '顺手把窗边那杯水推近一点', 'example': '喏，放这儿了。',
        'trigger': ['他坐在窗边时'], 'intensity': 'minimal', 'maxAttempts': 1,
        'reversibility': 'high',
    },
    'responseBranches': {
        'accepted': '她会多靠近一点', 'declined': '她会退回去', 'questioned': '她会含糊过去',
    },
    'currentStage': {'id': 'stage-1', 'name': '萌芽', 'purpose': '让在意有第一次落脚'},
    'stages': [{
        'id': 'stage-1', 'name': '萌芽', 'objective': '一次最小表达',
        'allowedSignals': ['顺手'], 'activationConditions': ['安静时'],
        'completionEvidence': ['他接住了'],
    }],
    'subtleSignals': ['停顿'], 'preferredSituations': ['傍晚'], 'avoidForcing': ['表白'],
}

#: 触发门槛是 25（配置默认），30 条私聊条目 = 30 分。
ENOUGH_ENTRIES = 30
NOT_ENOUGH_ENTRIES = 10

_DECISION = {'script': '她看了一眼窗外，风把窗帘吹起一点。',
             'interaction': {'seen': True, 'reply': {'mode': 'none'}}}


class _FakeHttp:
    """按**系统提示词**分派的 HTTP 替身（记录每次请求体）。"""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.bodies: list[dict[str, Any]] = []

    async def post_json(self, url: str, headers: Any = None, body: Any = None,
                        timeout: Any = None, task: Any = None) -> Any:
        self.posts.append({'url': url, 'task': task, 'timeout': timeout})
        self.bodies.append(body)
        system = ''
        for message in (body or {}).get('messages') or []:
            if message.get('role') == 'system':
                system = message.get('content') or ''
        if 'long-horizon dramaturgical catalyst' in system:
            content = json.dumps(CATALYST, ensure_ascii=False)
        else:
            content = json.dumps(_DECISION, ensure_ascii=False)
        return {
            'choices': [{'message': {'content': content}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1},
        }

    def iterate_sse(self, url: str, headers: Any = None, body: Any = None,
                    timeout: Any = None, task: Any = None) -> Any:
        return iter(())

    def guidance_bodies(self) -> list[dict[str, Any]]:
        return [
            body for body in self.bodies
            if any(
                message.get('role') == 'system'
                and 'long-horizon dramaturgical catalyst' in (message.get('content') or '')
                for message in (body or {}).get('messages') or []
            )
        ]

    def narrative_bodies(self) -> list[dict[str, Any]]:
        return [body for body in self.bodies if body not in self.guidance_bodies()]


class _Recorder:
    """主叙事包装：记录底层 request，转发给真实现（行为一个字不改）。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: list[dict[str, Any]] = []

    async def decide(self, request: Any) -> Any:
        self.requests.append(request)
        return await self.inner.decide(request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _make_provider(**overrides: Any) -> dict[str, Any]:
    provider = {
        'id': 'p1', 'label': 'P1', 'enabled': True,
        'endpoint': 'https://example.test/v1/chat/completions',
        'api_key': 'key-1', 'model': 'm1', 'temperature': 0.8, 'top_p': 1,
        'max_tokens': 4096, 'timeout': 60_000, 'response_format': 'json-object',
        'extra_headers': '', 'extra_body': '', 'use_for_main': True, 'use_for_compaction': True,
    }
    provider.update(overrides)
    return provider


def _config(enabled: bool = True) -> dict[str, Any]:
    return {
        'model': {
            'providers': [_make_provider()],
            'failover': {'enabled': True, 'strategy': 'priority',
                         'max_attempts_per_provider': 1, 'cooldown_minutes': 5},
            'main_prompt': '', 'format_prompt': '', 'fixed_prompt': 'FIXED', 'style_prompt': 'STYLE',
        },
        'compaction': {'enabled': True},
        'long_horizon': {'enabled': enabled},
        'runtime': {
            'maxMessageCharacters': 3000, 'messageSeparator': '<sep/>',
            'allowProactiveMessages': True, 'contextEntryLimit': 20,
            'contextTimeWindowMinutes': 60, 'minimumAdvanceMinutes': 5,
            'sweepIntervalMinutes': 5, 'input_status': {'enabled': False},
        },
        'memory': {'enabled': False},
        'sharedStory': {'participantContextLimit': 4, 'maxCrossConversationActions': 2},
        'agency': {'enabled': False}, 'browser': {'enabled': False},
        'stickers': {'directory': 'stickers'},
        'alterSystem': {'enabled': False}, 'schedulePreplan': {'enabled': False},
        'timelineDirector': {'enabled': False}, 'logging': {'level': 'info'},
    }


class LongArcWiringTests(unittest.IsolatedAsyncioTestCase):
    """① 生成走真模型 ② 提交后异步点火 ③ 下一次请求带上块 ④ 关着零调用零写入。"""

    def setUp(self) -> None:
        self.sink: list[tuple[str, str]] = []
        interlude_logging.set_log_sink(lambda level, text: self.sink.append((level, text)))
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()

    def make_service(self, enabled: bool = True) -> Any:
        self.http = _FakeHttp()
        ctx = InterludeContext(
            logger=None, database=self.db, http=self.http,
            clock=lambda: NOW, base_dir=self.tmp.name,
        )
        service = InterludeService(ctx, _config(enabled), self.db, NullTransport())
        service.report = lambda *args, **kwargs: None
        self.addCleanup(self._stop_service, service)
        story_time = FROM
        self.db.insert('interlude_story', {
            'id': STORY_ID, 'platform': 'test', 'selfId': '1', 'userId': '1',
            'channelId': 'private:1', 'status': 'active',
            'setting': {'character': {'name': '凌梦', 'profile': ''},
                        'user': {'displayName': 'Kela', 'profile': ''},
                        'timezone': 'Asia/Shanghai'},
            'state': encode_story_state(empty_story_state()),
            'cursorAt': story_time, 'createdAt': story_time, 'updatedAt': story_time,
        })
        self.db.insert('interlude_participant', {
            'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'platform': 'test', 'selfId': '1',
            'userId': '2', 'channelId': 'private:2', 'personId': 'person:2',
            'displayName': 'Kela', 'profile': '', 'relationship': '',
            'state': {'openThreads': [], 'relationshipNotes': []}, 'status': 'active',
            'createdAt': story_time, 'updatedAt': story_time,
        })
        return service

    async def _stop_service(self, service: Any) -> None:
        """先取消计时器、等后台任务跑完，再轮到关库（见文件头与坑 83）。"""
        service.stop_long_horizon()
        for name in ('_sweep_timer', '_compaction_timer', '_blind_mode_timer', '_sticker_scan_timer'):
            timer = getattr(service, name, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:  # pragma: no cover - 句柄已失效
                    pass
        for handle in list(service.due_intent_wake_timers.values()):
            cancel = handle.get('cancel') if isinstance(handle, dict) else None
            if callable(cancel):
                cancel()
        await service.drain_long_horizon_tasks()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            pending = [task for task in asyncio.all_tasks()
                       if task is not asyncio.current_task() and not task.done()]
            if not pending:
                return
            await asyncio.sleep(0.01)

    # -- 夹具 -------------------------------------------------------------- #

    def seed_entries(self, count: int) -> None:
        for index in range(count):
            self.db.insert('interlude_script_entry', {
                'storyId': STORY_ID, 'kind': 'user-message', 'actor': 'user',
                'content': '第 %d 条' % (index + 1), 'occurredAt': '2026-09-07T04:00:00Z',
                'metadata': {'conversation_kind': 'private'},
            })

    def rows(self, table: str) -> list[Any]:
        return list(self.db.all(table, {}))

    async def _narrative_request(self, service: Any, message: str,
                                 from_: Any = None, now: Any = None) -> dict[str, Any]:
        """跑一次真实 `decide`，拿到底层 request（包装只记录、不改行为）。"""
        recorder = _Recorder(service.narrator)
        service.narrator = recorder
        story = await service.get_story(STORY_ID)
        participant = await service.get_participant(PARTICIPANT_ID)
        await service.decide(story, participant, 'user-message', from_ or FROM, now or NOW, message, [])
        self.assertTrue(recorder.requests, '主叙事一次都没被调用')
        return recorder.requests[-1]

    # -- ① 生成：真模型 + 真接线 ------------------------------------------- #

    async def test_the_catalyst_is_generated_through_the_real_model_entry(self) -> None:
        service = self.make_service(enabled=True)
        self.seed_entries(ENOUGH_ENTRIES)
        story = await service.get_story(STORY_ID)

        result = await service.long_horizon_sweep(story)
        self.assertTrue(result['triggered'], result)
        self.assertTrue(result['generated'], result)

        rows = self.rows('interlude_long_arc_guidance')
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['version'], 1)
        self.assertEqual(rows[0]['status'], 'active')
        self.assertEqual(rows[0]['title'], CATALYST['title'])

        # 真的调了一次模型，走的就是 compaction 路由（`SIDE_TASK_ROUTES`）。
        self.assertEqual([post['task'] for post in self.http.posts], ['compaction'])
        (body,) = self.http.guidance_bodies()
        self.assertEqual(body['model'], 'm1')
        # 上游 `narrator.ts:606-632`：温度夹到 0.35、top_p 默认 1、JSON 档位同 compaction。
        # `max_tokens` 是**截断重试那一档**（`capped`）：`_side_task_json` 首次带 cap 发，
        # 只有命中"思考预算截断"才去掉它重试一次，所以第一发就是 2200。
        self.assertEqual(body['temperature'], 0.35)
        self.assertEqual(body['top_p'], 1)
        self.assertEqual(body['max_tokens'], 2200)
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        # 系统提示词逐字 = 上游 `longArcGuidancePrompt()`；user 侧是分层输入 JSON。
        system = [item['content'] for item in body['messages'] if item['role'] == 'system'][0]
        self.assertEqual(system, long_arc_guidance_prompt({'tier': 'full'}))
        user = [item['content'] for item in body['messages'] if item['role'] == 'user'][0]
        self.assertIn('keyEvidence', json.loads(user))

    async def test_a_score_below_the_trigger_generates_nothing(self) -> None:
        """**反向（闸门）**：攒不够分 → 一次模型都不调、一行都不写。"""
        service = self.make_service(enabled=True)
        self.seed_entries(NOT_ENOUGH_ENTRIES)
        story = await service.get_story(STORY_ID)
        result = await service.long_horizon_sweep(story)
        self.assertFalse(result['triggered'], result)
        self.assertEqual(self.http.posts, [], '没攒够分就不许调模型')
        self.assertEqual(self.rows('interlude_long_arc_guidance'), [])

    # -- ② 提交后异步点火（补丁 C）----------------------------------------- #

    async def test_the_commit_path_fires_the_sweep_asynchronously(self) -> None:
        """真 `persist_decision` 提交成功 → 后台扫描真的跑起来并生成了指导。"""
        service = self.make_service(enabled=True)
        self.seed_entries(ENOUGH_ENTRIES)
        story = await service.get_story(STORY_ID)
        participant = await service.get_participant(PARTICIPANT_ID)
        await service.persist_decision(
            story, participant,
            {'script': '她合上练习册。', 'interaction': {'seen': True, 'reply': {'mode': 'none'}}},
            FROM, NOW, True, 'user-message', [], False, None,
        )
        # 提交路径上不许同步等待扫描（不阻塞主回合）：这里显式等后台任务收尾。
        await service.drain_long_horizon_tasks()
        rows = self.rows('interlude_long_arc_guidance')
        self.assertEqual(len(rows), 1, '提交后的异步点火没有跑起来（补丁 C 没接上）')
        self.assertEqual(len(self.http.guidance_bodies()), 1)

    async def test_a_disabled_feature_never_fires_from_the_commit_path(self) -> None:
        """**反向（闸门）**：关着时提交路径也一步都不走（零调用、零写入）。"""
        service = self.make_service(enabled=False)
        self.seed_entries(ENOUGH_ENTRIES)
        story = await service.get_story(STORY_ID)
        participant = await service.get_participant(PARTICIPANT_ID)
        await service.persist_decision(
            story, participant,
            {'script': '她合上练习册。', 'interaction': {'seen': True, 'reply': {'mode': 'none'}}},
            FROM, NOW, True, 'user-message', [], False, None,
        )
        await service.drain_long_horizon_tasks()
        self.assertEqual(self.http.posts, [])
        self.assertEqual(self.rows('interlude_long_arc_guidance'), [])

    # -- ③ 下一次主叙事的 request / 系统提示词 ------------------------------ #

    async def test_the_next_turn_carries_the_guidance_block(self) -> None:
        """生成之后：request 带 `longHorizonGuidance`，系统提示词里真的出现那块。"""
        service = self.make_service(enabled=True)
        self.seed_entries(ENOUGH_ENTRIES)
        story = await service.get_story(STORY_ID)
        await service.long_horizon_sweep(story)
        state = service._long_horizon_state()
        # 模拟"重启/进程内缓存被清"：下一次 `decide` 开头的预热必须从库里把它捞回来
        # （补丁 D1）。少了那一跳，这里就是本批要修的"生成了但永远注入不进去"。
        state['cache'].clear()
        state['loaded'].clear()

        request = await self._narrative_request(service, '在吗')
        block = request.get('longHorizonGuidance')
        self.assertTrue(block, '下一次主叙事的 request 里没有长期走向块')
        self.assertIn('Long-horizon dramaturgical catalyst', block)
        self.assertIn(CATALYST['direction'], block)

        # 她**真正看到**的系统提示词（真实 `narrator.decide` 渲染出来的那一份）。
        body = self.http.narrative_bodies()[-1]
        system = [item['content'] for item in body['messages'] if item['role'] == 'system'][0]
        self.assertIn(block, system, '块进了 request，却没进系统提示词（补丁 E/F 没接上）')

    async def test_a_disabled_feature_keeps_the_request_key_absent(self) -> None:
        """**反向（闸门）**：关着时这个键**完全消失**（不是注入一个空块）。"""
        service = self.make_service(enabled=False)
        self.seed_entries(ENOUGH_ENTRIES)
        request = await self._narrative_request(service, '在吗')
        self.assertNotIn('longHorizonGuidance', request)
        self.assertNotIn('long_horizon_guidance', request)
        system = [
            item['content']
            for item in self.http.narrative_bodies()[-1]['messages'] if item['role'] == 'system'
        ][0]
        self.assertNotIn('Long-horizon dramaturgical catalyst', system)
        self.assertEqual(
            [post for post in self.http.posts if post['task'] == 'compaction'], [],
        )


class SystemPromptInjectionTests(unittest.TestCase):
    """补丁 F1/F2/F3：两档系统提示词都能吃下这块（空块 = 逐字不变）。"""

    BLOCK = 'LONG-HORIZON GUIDANCE BLOCK (test)'

    def _prompt(self, specialty: Optional[dict[str, Any]] = None, **overrides: Any) -> str:
        kwargs: dict[str, Any] = {
            'phase': 'user-message', 'main_prompt': 'MAIN', 'format_prompt': '',
            'fixed_prompt': 'FIXED', 'base_style_prompt': 'STYLE', 'story_style_prompt': '',
            'specialty': specialty, 'long_horizon_guidance': self.BLOCK,
        }
        kwargs.update(overrides)
        return system_prompt(**kwargs)

    def test_the_full_arrays_includes_the_block_before_channels(self) -> None:
        prompt = self._prompt({'tier': 'full', 'family': 'generic'})
        self.assertIn(self.BLOCK, prompt)
        self.assertLess(prompt.index(self.BLOCK), prompt.index('CHANNELS (writer rule)'))

    def test_the_lite_array_includes_the_block(self) -> None:
        prompt = self._prompt({'tier': 'lite', 'family': 'generic'})
        self.assertIn(self.BLOCK, prompt)
        self.assertLess(prompt.index(self.BLOCK), prompt.index('MAIN NARRATIVE PROMPT'))

    def test_no_block_means_byte_identical_prompts(self) -> None:
        """**反向**：没给块时两档的输出与给空串逐字相同（不注入空行）。"""
        for specialty in ({'tier': 'full', 'family': 'generic'}, {'tier': 'lite', 'family': 'generic'}):
            with self.subTest(tier=specialty['tier']):
                self.assertEqual(
                    self._prompt(specialty, long_horizon_guidance=None),
                    self._prompt(specialty, long_horizon_guidance=''),
                )
                self.assertNotIn(self.BLOCK, self._prompt(specialty, long_horizon_guidance=None))

    def test_the_guidance_prompt_has_the_upstream_lite_switch(self) -> None:
        full = long_arc_guidance_prompt({'tier': 'full'})
        lite = long_arc_guidance_prompt({'tier': 'lite'})
        self.assertTrue(full.startswith('You are the long-horizon dramaturgical catalyst'))
        self.assertIn('Return exactly one JSON object and no Markdown with this shape:', full)
        self.assertIn('Write with specific, psychologically plausible, non-deterministic guidance.', full)
        self.assertIn('Keep every field concise. Prefer one precise first expression over elaborate arc prose.', lite)
        self.assertNotIn('Write with specific, psychologically plausible', lite)
        # 除末行外两档逐字相同。
        self.assertEqual('\n'.join(full.split('\n')[:-1]), '\n'.join(lite.split('\n')[:-1]))
        self.assertEqual(long_arc_guidance_prompt(None), full)


if __name__ == '__main__':
    unittest.main(verbosity=2)
