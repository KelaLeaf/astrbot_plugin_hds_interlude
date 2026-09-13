"""`continuation-repair` 单元测试（不依赖 AstrBot SDK）。

移植自上游 `test/continuation-repair.test.ts`（node:test + node:assert/strict）。
运行方式（在仓库根目录）：

    python3 -m unittest plugin.tests.test_continuation_repair -v

归属说明
--------
本文件里**只有前两个用例属于 `src/script/continuation.ts`**：
- `bookmark resolves original text, includes already delivered reply, excludes current incoming event`
- `literal reuse observation never suppresses a new contact or modifies prose`（其中
  `proseReuseObservation` 的两条断言）

上游该文件其余用例打的是 `src/script/commit-builder.ts` / `validator.ts` /
`src/narrator.ts` / `src/story-state.ts` / `src/index.ts` 的符号。按移植约定，
这些用例的断言**一条都不删**：原样移植后用 `unittest.SkipTest` 标出归属，
等对应模块落地后取消 skip 即可直接跑。

注意：skip 用例里的**函数名/请求字段**按 PORT_PLAN 的 snake_case 约定书写；
但凡是被断言到的**模型可见 payload 键名**（`authoringWindow.continuation.lastScript.entryId`
这类）一律保留上游 camelCase 原文——上游断言的就是这些名字（§2「键名法」）。
若对应模块最终采用别的形状，以该模块为准再对齐即可（断言本身不删）。
"""

import importlib
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

# 把仓库根加入 sys.path，便于以插件包结构 import
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 自动解析插件目录名（即 Python 包名），不硬编码
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_continuation = importlib.import_module(f'{_PLUGIN_DIR}.core.script.continuation')
continuation_bookmark = _continuation.continuation_bookmark
prose_reuse_observation = _continuation.prose_reuse_observation

#: 上游 `const from = new Date('2026-09-05T06:30:00Z')` / `now = ...06:40:00Z`。
FROM = datetime(2026, 9, 5, 6, 30, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 5, 6, 40, tzinfo=timezone.utc)

_COMMIT_SKIP = '归属 core/script/commit_builder.py 或 core/script/validator.py 移植任务'
_NARRATOR_SKIP = '归属 core/service.py 或 core/narrator.py 移植任务'
_STORY_STATE_SKIP = '归属 core/story_state.py 或 plugin/main.py 移植任务'

#: 上游 `for (const [content, separator, split, expected] of [...] as const)` 的 6 组夹具。
BUBBLE_CASES = [
    ('在吗<sep/>', '<sep/>', True, ['在吗']),
    ('<sep/>在吗', '<sep/>', True, ['在吗']),
    (' <sep/> 在吗 <sep/> ', '<sep/>', True, ['在吗']),
    ('在吗||想好了吗||', '||', True, ['在吗', '想好了吗']),
    ('在吗<sep/>', '<sep/>', False, ['在吗<sep/>']),
    (' 在吗 ', '<sep/>', True, [' 在吗 ']),
]


def row(entry_id, kind, content, at=None):
    """上游 `row(id, kind, content, at = from)`。"""
    at = FROM if at is None else at
    actor = 'user' if kind == 'user-message' else 'narrator' if kind == 'script' else 'character'
    return {
        'id': entry_id, 'kind': kind, 'content': content, 'actor': actor,
        'story_id': 's', 'participant_id': 'alice', 'occurred_at': at, 'created_at': at,
        'metadata': {},
    }


def empty_story_setting():
    """上游 `emptyStorySetting()` 的最小等价物（仅 skip 用例使用）。"""
    return {
        'character': {'name': 'Unnamed character', 'profile': ''},
        'user': {'display_name': '', 'profile': ''},
        'relationship': '', 'world': '', 'perspective': '', 'supporting_cast': '',
        'location': '', 'style': 'Realistic, restrained, and centered on ordinary life.',
        'timezone': 'Asia/Shanghai',
    }


def empty_story_state():
    """上游 `emptyStoryState()` 的最小等价物（仅 skip 用例使用）。"""
    return {'schema_version': 1, 'setting_overlay': {'character_traits': []},
            'automation': {}, 'narrative_update_count': 0}


def make_request(recent_entries, phase='conversation-follow-up'):
    """上游 `request(recentEntries, phase = 'conversation-follow-up')`。"""
    return {
        'phase': phase, 'from': FROM, 'now': NOW, 'participant': None, 'participants': [],
        'story': {'setting': empty_story_setting(), 'state': empty_story_state()},
        'recent_entries': recent_entries,
        'due_intents': [], 'active_consequences': [], 'superseded_intents': [], 'memories': [],
    }


class ContinuationRepairTest(unittest.TestCase):
    # ---------- 以下两个用例属于 src/script/continuation.ts ----------

    def test_bookmark_resolves_original_text_includes_already_delivered_reply_excludes_current_incoming_event(self):
        entries = [
            row(1, 'user-message', '我想想'),
            row(2, 'script', '她决定等他想好。'),
            row(3, 'character-message', '想好叫我', FROM + timedelta(seconds=1)),
            row(4, 'user-message', '想好了', NOW),
        ]
        bookmark = continuation_bookmark(entries, FROM, NOW)
        # 书签是**模型可见的 wire 结构**，键名逐字照上游 camelCase。
        self.assertEqual(bookmark['establishedThrough'], '2026-09-05T06:30:00.000Z')
        self.assertEqual(bookmark['writingStart'], 'after-last-completed-passage')
        self.assertEqual(bookmark['lastScript']['entryId'], 2)
        self.assertEqual(bookmark['lastScript']['participantId'], 'alice')
        self.assertEqual(bookmark['originalEndpoint'],
                         {'entryId': 2, 'characterOffset': len('她决定等他想好。')})
        self.assertEqual(bookmark['newEventEntryIds'], [4])
        self.assertEqual([item['entryId'] for item in bookmark['recentCommunications']], [1, 3])
        self.assertNotRegex(json.dumps(bookmark, ensure_ascii=False), '我想想|想好叫我|unanswered')
        self.assertIsNone(continuation_bookmark([], FROM, NOW).get('lastScript'))

    def test_literal_reuse_observation_never_suppresses_a_new_contact_or_modifies_prose(self):
        # 这一段属于 continuation.ts：诊断用观察值，绝不改动散文、绝不压制新联系。
        self.assertEqual(prose_reuse_observation('在吗', '在吗'), 0)
        prose = '她放下书，看向手机，又记起那件尚未决定的小事。' * 8
        self.assertEqual(prose_reuse_observation(prose, prose), 1)
        # ↑ 之后的三条断言打的是 commit-builder / validator，见下面同名的 skip 用例。

    def test_literal_reuse_commit_stays_valid_and_keeps_prose_untouched(self):
        # 上游同一用例的后半段，测的是 `decisionToScriptCommit` / `validateScriptCommit` /
        # `findOutgoingScriptEvent`（commit-builder.ts / validator.ts）。
        raise unittest.SkipTest(_COMMIT_SKIP)

        # ---- 以下为上游用例译文（断言逐条保留）----
        commit_builder = importlib.import_module(f'{_PLUGIN_DIR}.core.script.commit_builder')
        validator = importlib.import_module(f'{_PLUGIN_DIR}.core.script.validator')
        prose = '她放下书，看向手机，又记起那件尚未决定的小事。' * 8
        commit = commit_builder.decision_to_script_commit({
            'story_id': 's', 'participant_id': 'alice', 'phase': 'conversation-follow-up',
            'from': FROM, 'now': NOW, 'frame_id': 'f', 'burst_id': 'b',
            'decision': {'script': prose,
                         'interaction': {'seen': False, 'reply': {'mode': 'immediate', 'content': '在吗'}}},
        })
        self.assertEqual(validator.validate_script_commit(commit)['valid'], True)
        self.assertEqual(commit['prose'], prose)
        self.assertTrue(commit_builder.find_outgoing_script_event(commit, 'alice', 'immediate', '在吗'))

    # ---------- 以下用例归属其它模块：断言原样保留、整体 skip ----------

    def test_bubble_reconstruction_keeps_transport_binding(self):
        # 上游是 `for (...) { test(...) }` 生成的 6 个用例；这里用 subTest 保留全部 6 组断言。
        raise unittest.SkipTest(_COMMIT_SKIP)

        # ---- 以下为上游用例译文（断言逐条保留）----
        commit_builder = importlib.import_module(f'{_PLUGIN_DIR}.core.script.commit_builder')
        validator = importlib.import_module(f'{_PLUGIN_DIR}.core.script.validator')
        for content, separator, split, expected in BUBBLE_CASES:
            with self.subTest(content=content, split=split):
                commit = commit_builder.decision_to_script_commit({
                    'story_id': 's', 'participant_id': 'alice', 'phase': 'conversation-follow-up',
                    'from': FROM, 'now': NOW, 'frame_id': 'f', 'burst_id': 'b',
                    'message_separator': separator, 'split_reply_messages': split,
                    'decision': {
                        'script': f'她又惦记起那件事，发出“{content}”。',
                        'interaction': {'seen': False, 'reply': {'mode': 'immediate', 'content': content}},
                    },
                })
                self.assertEqual(validator.validate_script_commit(commit, separator),
                                 {'valid': True, 'errors': []})
                event = commit_builder.find_outgoing_script_event(
                    commit, 'alice', 'immediate', content, separator)
                self.assertEqual(event['bubbles'], expected)
                self.assertEqual(commit['prose'], f'她又惦记起那件事，发出“{content}”。')

    def test_both_payload_orders_retain_clock_evidence_open_contact_source_text_and_current_event_separately(self):
        # 上游用例测的是 `src/narrator.ts` 的 `toPromptPayload`。
        raise unittest.SkipTest(_NARRATOR_SKIP)

        # ---- 以下为上游用例译文（断言逐条保留）----
        narrator = importlib.import_module(f'{_PLUGIN_DIR}.core.narrator')
        entries = [row(1, 'script', '她还等着对方决定，但先把书合上。'), row(2, 'character-message', '想好了吗')]
        request = make_request(entries)
        request['timeline_plan'] = {'beats': [{'at': 1, 'kind': 'state', 'summary': '她仍在书店等候'}],
                                    'carry': ['等对方决定']}
        request['timeline_carry'] = ['约定仍未解决']
        request['active_consequences'] = [{'id': 7, 'participant_id': 'alice', 'summary': '惦记未决的约定',
                                           'not_before': FROM, 'payload': {'effect': '仍关心结果', 'strength': 0.5}}]
        request['upcoming_intents'] = [{'id': 8, 'type': 'follow-up-commitment', 'participant_id': 'alice',
                                        'summary': '稍后告诉对方结果', 'not_before': NOW}]
        for cache_first in (False, True):
            payload = narrator.to_prompt_payload(request, {'cache_first': cache_first})
            # `to_prompt_payload` 的返回值就是发给模型的 payload → 全部上游 camelCase。
            self.assertEqual(payload['authoringWindow']['continuation']['lastScript']['entryId'], 1)
            self.assertEqual(payload['incomingEvent']['event'], {'type': 'none'})
            self.assertEqual(payload['availableNearFuture']['timelinePlan'], request['timeline_plan'])
            self.assertEqual(payload['availableNearFuture']['timelineCarry'], request['timeline_carry'])
            self.assertEqual(payload['ongoingThreads']['activeConsequences'][0]['id'], 7)
            self.assertEqual(payload['availableNearFuture']['upcomingPlans'][0]['id'], 8)
            self.assertEqual([item['content'] for item in payload['relevantEstablishedEpisodes']['recentScript']],
                             [entry['content'] for entry in entries])

    def test_forty_mixed_fixture_turns_move_the_bookmark_without_turning_silence_into_user_input(self):
        # 上游用例同样经由 `toPromptPayload`（src/narrator.ts）。
        raise unittest.SkipTest(_NARRATOR_SKIP)

        # ---- 以下为上游用例译文（断言逐条保留）----
        narrator = importlib.import_module(f'{_PLUGIN_DIR}.core.narrator')
        entries = [row(1, 'script', '她在书店等他决定。')]
        clock = FROM
        for i in range(40):
            following = clock + timedelta(minutes=1)
            live = i % 3 == 0
            if live:
                entries.append(row(len(entries) + 1, 'user-message', '想好了' if i == 39 else '我再想想', following))
            before = json.dumps(entries, ensure_ascii=False)
            request = make_request(entries, 'user-message' if live else 'conversation-follow-up')
            request['from'] = clock
            request['now'] = following
            if live:
                request['user_message'] = entries[-1]['content']
            expected = [entry for entry in entries if entry['kind'] == 'script'][-1]
            payload = narrator.to_prompt_payload(request, {'cache_first': i % 2 == 0})
            self.assertEqual(payload['authoringWindow']['continuation']['lastScript']['entryId'], expected['id'])
            self.assertEqual(payload['incomingEvent']['event']['type'],
                             'private-message-batch' if live else 'none')
            self.assertEqual(json.dumps(entries, ensure_ascii=False), before)
            prose = '收到他的决定，她把约定落实下来。' if i == 39 else f'她翻过第{i + 1}页，仍然惦记那件事。'
            entries.append(row(len(entries) + 1, 'script', prose, following))
            clock = following

    def test_positive_continuation_contract_explicitly_preserves_renewed_contact(self):
        # 上游用例测的是 `src/narrator.ts` 的 `systemPrompt`。
        raise unittest.SkipTest(_NARRATOR_SKIP)

        # ---- 以下为上游用例译文（断言逐条保留）----
        narrator = importlib.import_module(f'{_PLUGIN_DIR}.core.narrator')
        prompt = narrator.system_prompt('conversation-follow-up', '', '', '', '', '')
        self.assertRegex(prompt, 'CONTINUATION BOOKMARK')
        self.assertRegex(prompt, 'renewed question is a new action')
        self.assertRegex(prompt, 'not a requirement to stay silent')

    def test_retired_rhythm_state_round_trips_for_rollback_but_never_enters_narrative_context(self):
        # 上游用例测的是 `src/story-state.ts` 的编解码 + `src/narrator.ts` 的
        # `storyStateForPrompt` + `src/index.ts` 的 `Config`。
        raise unittest.SkipTest(_STORY_STATE_SKIP)

        # ---- 以下为上游用例译文（断言逐条保留）----
        story_state = importlib.import_module(f'{_PLUGIN_DIR}.core.story_state')
        narrator = importlib.import_module(f'{_PLUGIN_DIR}.core.narrator')
        state = story_state.decode_story_state(
            {'chat_rhythm': {'recent': [], 'updated_at': NOW.isoformat(), 'exhausted': True}})
        self.assertEqual(story_state.decode_story_state(story_state.encode_story_state(state))['chat_rhythm'],
                         state['chat_rhythm'])
        self.assertIsNone(narrator.story_state_for_prompt(state).get('chat_rhythm'))
        config = importlib.import_module(f'{_PLUGIN_DIR}.main').Config
        self.assertEqual(config['dict']['chat_rhythm']['meta']['hidden'], True)


if __name__ == '__main__':
    unittest.main()
