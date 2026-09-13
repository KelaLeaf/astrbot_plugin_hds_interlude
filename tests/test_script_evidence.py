"""`plugin/core/script/` 证据类模块的单元测试（stdlib `unittest`）。

主要来源（逐条对照上游断言，见 `docs/PORT_PLAN.md` §3「测试即契约」）：
- `upstream/test/evidence-repair.test.ts` → 知识证据 / 授权发言部分
- `upstream/test/m10-cooperation.test.ts` → `developmentContextQuery` 部分
- `upstream/test/m7-m8-continuity.test.ts` → `developmentScenes` / `developmentDimension` 部分

补充来源（上游这三个文件之外，本移植版为避免整块模块零覆盖而一并移植）：
- `upstream/test/beta6-handoff.test.ts` → `resolveAuthoredActions` / `normalizeLifeHandoff` /
  `interactionEvidence` / `reviewedDevelopmentSupport`
- `upstream/test/p1-memory-navigation.test.ts` → `promptReadyDevelopment`

上游同一用例里属于 **其它模块**（`core/service.py`、`core/narrator.py`、`core/story_state.py`、
`core/script/commit_builder.py` …）的断言不允许删除：它们被原样移植成独立用例并
`skipTest`，等对应模块落地后由该模块的负责人接管。

数据形状说明（`docs/PORT_PLAN.md` §2「⚠️ 键名法」）：
- `ScriptEntry` / `NarrativeFact` 等**领域对象**及其 `metadata` 运行期数据袋一律
  `snake_case`（`life_handoff` / `scene_checkpoint` / `frame_id` / `related_fact_ids` /
  `source_entry_id` / `resolved_details`），见 `plugin/core/types.py`。
- 本文件里凡是被断言到的**模型可见投影**一律上游 camelCase：
  `factEvidenceForPrompt`（`participantId` / `sourceEntryIds` / `knowledge.relatedFactIds` /
  `knowledge.clauses[].sourceEntryId`）、`contactEvidenceThreads`
  （`originalEntryIds` / `missingSourceEntryIds`）、`interactionEvidence`
  （`participantId` / `feedbackEntryId` / `priorCommunicationEntryId` /
  `interpretationEntryIds` / `responseEntryIds`）、`narrativeEvidence`
  （`narrativeAuthority` / `lifeHandoff` / `proposedTimeline` / `timelineEvidence` /
  `communicationOutcome`，其中 `lifeHandoff.resolvedDetails` 也是 wire 名）。
  这些结构由 `narrator_prompts` 直接发给模型，上游断言的就是这些名字。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from plugin.core.script import authored_actions as authored
from plugin.core.script.authored_actions import (
    complete_legacy_bubble_block,
    read_authored_actions,
    resolve_authored_actions,
)
from plugin.core.script.development import (
    development_context_query,
    development_dimension,
    development_scenes,
    interaction_evidence,
    prompt_ready_development,
    reviewed_development_support,
)
from plugin.core.script.knowledge_evidence import (
    contact_evidence_threads,
    fact_evidence_for_prompt,
    knowledge_clauses,
    knowledge_related_ids,
    legacy_condition_cue,
    normalize_knowledge_evidence,
    supports_recorded_outcome,
)
from plugin.core.script.life_handoff import entry_life_handoff, narrative_evidence, normalize_life_handoff

NOW = datetime(2026, 9, 6, 9, 38, tzinfo=timezone.utc)


def entry(entry_id: int, kind: str, content: str, participant_id: str = 'alice') -> dict:
    """上游 `entry()` 助手（`occurred_at` 为 timezone-aware UTC datetime）。"""
    return {
        'id': entry_id, 'story_id': 'story', 'kind': kind, 'content': content,
        'participant_id': participant_id,
        'actor': 'user' if kind == 'user-message' else 'character',
        'occurred_at': NOW, 'created_at': NOW, 'metadata': {},
    }


def fact(fact_id: int, content: str, source_entry_ids: list[int], participant_id: str = 'alice') -> dict:
    """上游 `fact()` 助手。"""
    return {
        'id': fact_id, 'story_id': 'story', 'participant_id': participant_id, 'scope': 'promise',
        'content': content, 'source_entry_ids': source_entry_ids, 'importance': 0.5,
        'confidence': 0.6, 'unresolved': True, 'status': 'active',
        'last_seen_at': NOW, 'created_at': NOW, 'updated_at': NOW,
    }


def script_source(entry_id: int, frame: str = 'one', participant_id: str = '') -> dict:
    """`m7-m8-continuity.test.ts` 的 `source()` 助手。"""
    return {
        'id': entry_id, 'story_id': 's', 'participant_id': participant_id, 'kind': 'script',
        'actor': 'narrator', 'content': '她答应明天在书店归还书本。',
        'occurred_at': NOW, 'created_at': NOW, 'metadata': {'frame_id': frame},
    }


class KnowledgeEvidenceTests(unittest.TestCase):
    """`src/script/knowledge-evidence.ts`（上游 evidence-repair.test.ts）。"""

    def test_fact_evidence_for_prompt_projects_internal_storage_onto_the_upstream_wire_names(self):
        """库里的证据是内部 snake_case，发给模型的那一份必须是上游 camelCase。"""
        row = entry(1, 'user-message', '如果电影好看，我想再跟你看一次')
        stored = fact(7, '电影条件', [1])
        stored['knowledge'] = {
            'mode': 'belief', 'holder': 'protagonist', 'topic': '电影',
            'clauses': [{'role': 'interpretation', 'source_entry_id': 1, 'quote': row['content']}],
            'related_fact_ids': [9],
        }
        wire = fact_evidence_for_prompt(stored)
        self.assertEqual(wire['id'], 7)
        self.assertEqual(wire['participantId'], 'alice')
        self.assertEqual(wire['sourceEntryIds'], [1])
        # 主角自己相信的事只能以「归属信念」出现。
        self.assertEqual(wire['authority'], 'attributed-belief')
        self.assertEqual(wire['knowledge'], {
            'mode': 'belief', 'holder': 'protagonist', 'topic': '电影',
            'clauses': [{'role': 'interpretation', 'sourceEntryId': 1, 'quote': row['content']}],
            'relatedFactIds': [9],
        })
        self.assertNotIn('related_fact_ids', wire['knowledge'])
        self.assertNotIn('source_entry_id', wire['knowledge']['clauses'][0])
        # 库里那一份没有被就地改写。
        self.assertEqual(stored['knowledge']['related_fact_ids'], [9])

    def test_a_narrative_imagining_acceptance_remains_interpretation_not_mutual_confirmation(self):
        rows = [entry(1, 'user-message', '我想再跟你看一次电影'), entry(2, 'script', '她觉得自己已经默认接受了')]
        knowledge = normalize_knowledge_evidence({'mode': 'confirmed', 'clauses': [
            {'role': 'proposal', 'source_entry_id': 1, 'quote': rows[0]['content']},
            {'role': 'confirmation', 'source_entry_id': 2, 'quote': rows[1]['content']},
        ]}, rows, [1, 2])
        self.assertEqual(knowledge['mode'], 'unclassified')
        self.assertEqual(knowledge['clauses'][1]['role'], 'interpretation')
        self.assertFalse(supports_recorded_outcome(knowledge))

    def test_actual_acceptance_can_be_recorded_while_retaining_the_original_condition_and_relationship(self):
        rows = [entry(1, 'user-message', '如果电影好看，我想再跟你看一次'), entry(2, 'character-message', '行吧')]
        knowledge = normalize_knowledge_evidence({'mode': 'confirmed', 'topic': '电影', 'clauses': [
            {'role': 'proposal', 'source_entry_id': 1, 'quote': rows[0]['content']},
            {'role': 'condition', 'source_entry_id': 1, 'quote': '如果电影好看'},
            {'role': 'confirmation', 'source_entry_id': 2, 'quote': '行吧'},
        ]}, rows, [1, 2], [99])
        self.assertEqual(knowledge['mode'], 'confirmed')
        self.assertTrue(supports_recorded_outcome(knowledge))
        self.assertEqual(knowledge['clauses'][1]['quote'], '如果电影好看')
        self.assertEqual(knowledge['related_fact_ids'], [99])
        # 确认一旦换成另一个人（participantId 不同），confirmed 立刻降级为 conditional。
        self.assertEqual(normalize_knowledge_evidence(
            knowledge, [rows[0], {**rows[1], 'participant_id': 'bob'}], [1, 2])['mode'], 'conditional')

    def test_missing_or_forged_quotations_never_acquire_stronger_authority_and_old_facts_are_not_rewritten(self):
        rows = [entry(1, 'user-message', '想睡觉')]
        knowledge = normalize_knowledge_evidence(
            {'mode': 'observed', 'clauses': [{'role': 'observation', 'source_entry_id': 1, 'quote': '睡了五小时'}]},
            rows, [1])
        self.assertEqual(knowledge['mode'], 'unclassified')
        old = fact(1, '旧的承诺摘要', [1])
        self.assertEqual(fact_evidence_for_prompt(old)['knowledge']['mode'], 'unclassified')
        self.assertIsNone(old.get('knowledge'))

    def test_condition_chains_keep_missing_source_markers_without_leaking_branches(self):
        """上游同一用例中属于本模块的断言（service.contactThreads 部分见下方 skip 用例）。"""
        rows = [
            entry(100, 'character-message', '至少连续一周再说'), entry(101, 'user-message', '好吧'),
            entry(200, 'user-message', '如果好看，我想再跟你看一次'), entry(201, 'character-message', '別急'),
            entry(202, 'user-message', '私密', 'bob'),
        ]
        self.assertEqual(contact_evidence_threads([fact(1, 'missing', [999])], rows)[0]['missingSourceEntryIds'], [999])

    def test_condition_chains_retain_whole_originals_and_do_not_leak_branches(self):
        self.skipTest("归属 core/service.py 移植任务")
        import asyncio  # noqa: F401  # 以下为上游原样断言，只等 service.py 落地
        import json
        from plugin.core.service import InterludeService
        condition = fact(10, '早睡一周才看电影', [100])
        proposal = fact(11, '如果好看，想再看一次', [200])
        proposal['knowledge'] = {'mode': 'conditional', 'clauses': [], 'related_fact_ids': [10]}
        rows = [entry(100, 'character-message', '至少连续一周再说'), entry(101, 'user-message', '好吧'),
                entry(200, 'user-message', '如果好看，我想再跟你看一次'), entry(201, 'character-message', '別急'),
                entry(202, 'user-message', '私密', 'bob')]

        def db_get(table, query=None):
            return [proposal, condition, fact(12, '另一个人的电影', [202], 'bob')] if table == 'interlude_fact' else rows

        chain = asyncio.run(InterludeService.contact_threads({'db_get': db_get}, 'story', [proposal], 'alice'))
        self.assertTrue(any(any(item['content'] == '至少连续一周再说' for item in link['originals']) for link in chain))
        self.assertNotIn('私密', json.dumps(chain, ensure_ascii=False))
        public_chain = asyncio.run(
            InterludeService.contact_threads({'db_get': db_get}, 'story', [proposal], None))
        self.assertEqual(public_chain, [])

    def test_main_user_and_automatic_windows_keep_contact_conditions_in_both_payload_orders(self):
        self.skipTest("归属 core/narrator.py 移植任务")
        from plugin.core.narrator import to_prompt_payload
        source = entry(1, 'character-message', '至少连续一周再说')
        chain = contact_evidence_threads([fact(1, '电影条件', [1])], [source])
        story = {'id': 'story', 'setting': {}, 'state': {}, 'cursor_at': NOW, 'created_at': NOW, 'updated_at': NOW}
        for phase in ('user-message', 'conversation-follow-up', 'advance'):
            for cache_first in (True, False):
                payload = to_prompt_payload({
                    'story': story, 'phase': phase, 'from': NOW, 'now': NOW, 'participant': None,
                    'participants': [], 'share_participant_details': False, 'recent_entries': [source],
                    'memories': [], 'due_intents': [], 'active_consequences': [], 'superseded_intents': [],
                    'contact_threads': chain,
                }, {'cache_first': cache_first})
                self.assertEqual(payload['ongoingThreads']['contactThreads'], chain)

    def test_legacy_knowledge_rows_persisted_as_empty_objects_never_crash_evidence_reads(self):
        # 证据字段出现之前落库的行会存成 knowledge = {}，既没有 clauses 也没有 relatedFactIds，
        # 所有读取路径都必须防御性强制转换。
        legacy = fact(9, '她答应周末一起去万松园', [1])
        legacy['knowledge'] = {}
        modern = fact(10, '另一条事实', [2])
        modern['knowledge'] = {'mode': 'proposal', 'topic': '万松园'}
        self.assertEqual(fact_evidence_for_prompt(legacy)['knowledge'],
                         {'mode': 'unclassified', 'clauses': [], 'relatedFactIds': []})
        # 半截 legacy 形状（有 mode 没有 clauses）同样视为没有证据。
        self.assertEqual(fact_evidence_for_prompt(modern)['knowledge'],
                         {'mode': 'unclassified', 'clauses': [], 'relatedFactIds': []})
        self.assertFalse(supports_recorded_outcome({**legacy['knowledge'], 'mode': 'confirmed'}))
        threads = contact_evidence_threads([legacy, modern], [entry(1, 'user-message', '她答应周末一起去万松园')])
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0]['fact']['knowledge']['clauses'], [])

    def test_working_detail_belief_evidence_keeps_its_prose_untouched(self):
        knowledge = normalize_knowledge_evidence({'mode': 'belief', 'holder': 'protagonist', 'clauses': [
            {'role': 'interpretation', 'source_entry_id': 1, 'quote': '她希望他今天来'},
        ]}, [entry(1, 'script', '她希望他今天来')], [1])
        self.assertEqual(knowledge['mode'], 'belief')
        self.assertEqual(knowledge['clauses'], [
            {'role': 'interpretation', 'source_entry_id': 1, 'quote': '她希望他今天来'},
        ])

    def test_working_detail_evidence_survives_state_encoding_without_modifying_its_prose(self):
        self.skipTest("归属 core/story_state.py 移植任务")
        from plugin.core.story_state import decode_story_state, encode_story_state
        knowledge = normalize_knowledge_evidence({'mode': 'belief', 'holder': 'protagonist', 'clauses': [
            {'role': 'interpretation', 'source_entry_id': 1, 'quote': '她希望他今天来'},
        ]}, [entry(1, 'script', '她希望他今天来')], [1])
        state = decode_story_state({'working_details': [
            {'label': '期待', 'value': '她希望他今天来', 'created_at': NOW.isoformat(),
             'source_entry_ids': [1], 'knowledge': knowledge},
        ]})
        self.assertEqual(decode_story_state(encode_story_state(state))['working_details'][0]['knowledge'], knowledge)

    def test_evidence_bounds_and_roles_match_upstream_limits(self):
        """按上游实现逐条断言的边界（上游无对应用例，作为实现契约的补充覆盖）。"""
        long_content = '她今天去了书店。' + 'x' * 900
        rows = [entry(1, 'script', long_content), entry(2, 'user-message', '她今天去了书店。')]
        # quote 上限 800：即使确实被原文包含，超长也直接丢弃。
        oversized = normalize_knowledge_evidence({'mode': 'observed', 'clauses': [
            {'role': 'observation', 'source_entry_id': 1, 'quote': long_content[:801]},
        ]}, rows, [1])
        self.assertEqual(oversized['clauses'], [])
        self.assertEqual(oversized['mode'], 'unclassified')
        # clauses 上限 12。
        many = [{'role': 'observation', 'source_entry_id': 2, 'quote': '她今天去了书店。'} for _ in range(15)]
        self.assertEqual(len(normalize_knowledge_evidence({'mode': 'observed', 'clauses': many}, rows, [1, 2])['clauses']), 12)
        # role 白名单。
        self.assertEqual(normalize_knowledge_evidence({'mode': 'observed', 'clauses': [
            {'role': 'guess', 'source_entry_id': 2, 'quote': '她今天去了书店。'},
        ]}, rows, [1, 2])['clauses'], [])
        # 条目必须来自 sourceEntryIds。
        self.assertEqual(normalize_knowledge_evidence({'mode': 'observed', 'clauses': [
            {'role': 'observation', 'source_entry_id': 1, 'quote': '她今天去了书店。'},
        ]}, rows, [2])['clauses'], [])
        # topic 必须 ≤80 且被某条引用原话包含。
        clause = {'role': 'observation', 'source_entry_id': 2, 'quote': '她今天去了书店。'}
        self.assertEqual(normalize_knowledge_evidence(
            {'mode': 'observed', 'topic': '书店', 'clauses': [clause]}, rows, [1, 2])['topic'], '书店')
        self.assertNotIn('topic', normalize_knowledge_evidence(
            {'mode': 'observed', 'topic': '电影院', 'clauses': [clause]}, rows, [1, 2]))
        long_text = 't' * 81
        rows_long = [entry(3, 'user-message', long_text + '尾巴')]
        self.assertNotIn('topic', normalize_knowledge_evidence({'mode': 'observed', 'topic': long_text, 'clauses': [
            {'role': 'observation', 'source_entry_id': 3, 'quote': long_text + '尾巴'},
        ]}, rows_long, [3]))
        # observed 一旦含 interpretation 就降级为 belief；无 holder 时归主角。
        belief = normalize_knowledge_evidence({'mode': 'observed', 'clauses': [
            {'role': 'observation', 'source_entry_id': 2, 'quote': '她今天去了书店。'},
            {'role': 'interpretation', 'source_entry_id': 2, 'quote': '她今天去了书店。'},
        ]}, rows, [2])
        self.assertEqual(belief['mode'], 'belief')
        self.assertEqual(belief['holder'], 'protagonist')
        # holder 截断到 127。
        self.assertEqual(normalize_knowledge_evidence({'mode': 'belief', 'holder': 'h' * 200, 'clauses': [
            {'role': 'observation', 'source_entry_id': 2, 'quote': '她今天去了书店。'},
        ]}, rows, [2])['holder'], 'h' * 127)
        # relatedFactIds 去重后截断到 12。
        self.assertEqual(normalize_knowledge_evidence({'mode': 'observed', 'clauses': [
            {'role': 'observation', 'source_entry_id': 2, 'quote': '她今天去了书店。'},
        ]}, rows, [2], [3, 3, 1, *range(20, 40)])['related_fact_ids'], [3, 1, *range(20, 30)])
        # 非投递条目上的 confirmation 降级为 interpretation（进而把 observed 拉成 belief）。
        downgraded = normalize_knowledge_evidence({'mode': 'observed', 'clauses': [
            {'role': 'confirmation', 'source_entry_id': 1, 'quote': '她今天去了书店。'},
        ]}, rows, [1])
        self.assertEqual(downgraded['clauses'][0]['role'], 'interpretation')
        self.assertEqual(downgraded['mode'], 'belief')

    def test_evidence_read_helpers_coerce_partial_shapes(self):
        self.assertEqual(knowledge_clauses(None), [])
        self.assertEqual(knowledge_clauses({}), [])
        self.assertEqual(knowledge_clauses({'clauses': 'no'}), [])
        self.assertEqual(knowledge_clauses({'clauses': [{'role': 'observation'}]}), [{'role': 'observation'}])
        self.assertEqual(knowledge_related_ids(None), [])
        self.assertEqual(knowledge_related_ids({'related_fact_ids': 'no'}), [])
        self.assertEqual(knowledge_related_ids({'related_fact_ids': [1, 2]}), [1, 2])
        self.assertTrue(legacy_condition_cue('至少连续一周再说'))
        self.assertTrue(legacy_condition_cue('除非他先道歉'))
        self.assertFalse(legacy_condition_cue('她今天去了书店。'))
        self.assertFalse(legacy_condition_cue(None))

    def test_unclassified_or_imagined_completion_cannot_close_a_promise(self):
        self.skipTest("归属 core/service.py 移植任务")
        import asyncio
        from plugin.core.service import InterludeService
        existing = fact(12, '电影安排', [1], '')
        captured: dict = {}

        async def db_get(*args, **kwargs):
            return [existing]

        async def db_set(_table, _query, value):
            captured.update(value)

        host = {
            'memory_config': {'fact_content_characters': 4000, 'max_facts_per_story': 200},
            'db_get': db_get, 'db_set': db_set, 'embed_text': lambda *args, **kwargs: [],
        }
        source = entry(2, 'script', '她觉得他默认接受了', '')
        asyncio.run(InterludeService.persist_fact(host, 'story', {
            'scope': 'promise', 'content': existing['content'], 'unresolved': False,
            'confidence': 1, 'source_entry_ids': [2],
        }, [source], NOW))
        self.assertTrue(captured['unresolved'])
        self.assertEqual(captured['confidence'], 0.6)
        self.assertEqual(captured['knowledge']['mode'], 'unclassified')


class AuthoredActionsTests(unittest.TestCase):
    """`src/script/authored-actions.ts`（上游 evidence-repair / beta6-handoff）。"""

    def setUp(self):
        # 上游两个模块级 WeakSet 是进程级的；用例之间清空，避免 id 复用带来的串扰。
        authored._resolved_actions.clear()
        authored._delivered_actions.clear()

    def test_17_38_regression_a_two_bubble_terminal_script_block_becomes_one_complete_delivery_event(self):
        """上游同一用例中属于本模块的断言（提交管线部分见下方 skip 用例）。"""
        raw = {'script': '她拿起手机。\n\n牛逼<sep/>你继续勿扰吧',
               'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '牛逼'}}}
        decision = resolve_authored_actions(raw)
        self.assertEqual(decision['interaction']['reply']['content'], '牛逼<sep/>你继续勿扰吧')
        self.assertEqual(decision['script'], raw['script'])
        self.assertEqual(resolve_authored_actions(decision), decision)

    def test_17_38_regression_commit_pipeline_keeps_bubbles_and_delivery_ledger(self):
        self.skipTest("归属 core/script/commit_builder.py、validator.py、core/delivery.py、core/turn_persistence.py 移植任务")
        from plugin.core.delivery import attach_message_event, prepare_outgoing_delivery
        from plugin.core.script.commit_builder import decision_to_script_commit
        from plugin.core.script.validator import validate_script_commit
        from plugin.core.turn_persistence import script_entry_draft_for_commit
        raw = {'script': '她拿起手机。\n\n牛逼<sep/>你继续勿扰吧',
               'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '牛逼'}}}
        decision = resolve_authored_actions(raw)
        commit = decision_to_script_commit({
            'story_id': 'story', 'participant_id': 'alice', 'phase': 'user-message',
            'from': NOW, 'now': NOW, 'decision': decision, 'frame_id': 'frame', 'burst_id': 'burst',
        })
        self.assertTrue(validate_script_commit(commit)['valid'])
        event = next(item for item in commit['events'] if item['kind'] == 'outgoing-message')
        self.assertEqual(event['bubbles'], ['牛逼', '你继续勿扰吧'])
        output = prepare_outgoing_delivery(
            attach_message_event({'participant_id': 'alice', 'content': event['content']}, event), event['bubbles'])
        self.assertEqual(output['content'], '牛逼')
        self.assertEqual(output['later_segments'], ['你继续勿扰吧'])
        self.assertEqual(
            len(script_entry_draft_for_commit(commit, None)['metadata']['delivery_actions'][0]['segments']), 2)

    def test_tail_repair_respects_non_actions_explicit_action_ids_custom_separators_and_early_delivery_idempotency(self):
        raw = {'script': '她拿起手机。\n\n甲||乙',
               'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '甲'}}}
        self.assertEqual(resolve_authored_actions(raw, False, '||')['interaction']['reply']['content'], '甲||乙')
        sent = resolve_authored_actions(raw, True, '||')
        self.assertEqual(resolve_authored_actions(sent, False, '||')['interaction']['reply']['content'], '甲')
        for script in ('她想起“甲||乙”。', '她拿起手机。\n\n甲||乙\n然后放下手机。'):
            self.assertEqual(
                resolve_authored_actions({**raw, 'script': script}, False, '||')['interaction']['reply']['content'], '甲')
        none = {**raw, 'interaction': {'seen': True, 'reply': {'mode': 'none'}}}
        self.assertEqual(resolve_authored_actions(none)['interaction']['reply']['mode'], 'none')
        tagged = {'script': '她发出<say id="r">甲||乙</say>。',
                  'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'action_id': 'r'}}}
        self.assertEqual(resolve_authored_actions(tagged, False, '||')['interaction']['reply']['content'], '甲||乙')
        partial = resolve_authored_actions(
            {**tagged, 'script': '她拿起手机。\n\n<say id="r">甲</say>||乙'}, False, '||')
        self.assertEqual(partial['interaction']['reply']['content'], '甲||乙')
        self.assertEqual(partial['authored_actions'][0]['content'], '甲||乙')
        self.assertEqual(resolve_authored_actions(partial, False, '||'), partial)

    def test_read_authored_actions_unwraps_speech_and_drops_duplicate_ids(self):
        parsed = read_authored_actions('前<say id="a">甲</say>后<say id="b">乙</say>')
        self.assertEqual(parsed['prose'], '前甲后乙')
        self.assertEqual(parsed['actions'], [
            {'id': 'a', 'start': 1, 'end': 2, 'content': '甲'},
            {'id': 'b', 'start': 3, 'end': 4, 'content': '乙'},
        ])
        duplicated = read_authored_actions('<say id="a">甲</say><say id="a">乙</say>')
        self.assertEqual(duplicated['prose'], '甲乙')
        self.assertEqual(duplicated['actions'], [])
        # id 只接受 JS `\w`（ASCII 字母数字下划线）与 `-`，长度 1–64。
        self.assertEqual(read_authored_actions('<say id="汉字">甲</say>')['actions'], [])
        self.assertEqual(read_authored_actions('<say id="' + 'a' * 65 + '">甲</say>')['actions'], [])
        self.assertEqual(len(read_authored_actions('<say id="' + 'a' * 64 + '">甲</say>')['actions']), 1)
        # 跨行内容（[\s\S]*?）同样被解包。
        self.assertEqual(read_authored_actions('<say id="r">第一行\n第二行</say>')['actions'][0]['content'], '第一行\n第二行')

    def test_complete_legacy_bubble_block_only_accepts_an_explicit_terminal_block(self):
        self.assertIsNone(complete_legacy_bubble_block('她拿起手机。\n\n甲||乙', '甲', ''))
        self.assertIsNone(complete_legacy_bubble_block('她想起“甲||乙”。', '甲', '||'))
        self.assertEqual(complete_legacy_bubble_block('她拿起手机。\n\n甲||乙', '甲', '||'), '甲||乙')
        self.assertEqual(complete_legacy_bubble_block('前言\\n\\n甲||乙', '甲', '||'), '甲||乙')
        # 段里含换行 / 尖括号 / 字面量 \n，或出现空气泡，一律不认。
        self.assertIsNone(complete_legacy_bubble_block('前言\n\n甲||乙\n然后放下手机。', '甲', '||'))
        self.assertIsNone(complete_legacy_bubble_block('前言\n\n甲||<b>乙</b>', '甲', '||'))
        self.assertIsNone(complete_legacy_bubble_block('前言\n\n甲||乙\\n丙', '甲', '||'))
        self.assertIsNone(complete_legacy_bubble_block('前言\n\n甲||', '甲', '||'))

    def test_authored_reference_supplies_exact_words_before_normalization_across_repeated_resolver_calls(self):
        raw = {'script': '  她记起上次的“在吗”，又写下<say id="r">在吗</say>。  ',
               'interaction': {'seen': False, 'reply': {'mode': 'immediate', 'action_id': 'r', 'content': '另一份答案'}}}
        resolved = resolve_authored_actions(raw)
        self.assertEqual(resolved['interaction']['reply']['content'], '在吗')
        self.assertEqual(resolved['script'], '她记起上次的“在吗”，又写下在吗。')
        self.assertEqual(resolve_authored_actions(resolved), resolved)
        self.assertIn('<say', raw['script'])

    def test_bad_explicit_ids_and_duplicate_authored_ids_never_create_guessed_outgoing_text(self):
        for script in ('她想问问。', '<say id="r">甲</say><say id="r">乙</say>'):
            result = resolve_authored_actions({'script': script, 'interaction': {
                'seen': False, 'reply': {'mode': 'immediate', 'action_id': 'r', 'content': '代猜'}}})
            self.assertEqual(result['interaction']['reply']['mode'], 'none')
        forged = resolve_authored_actions({
            'script': '只是旧话',
            'authored_actions': [{'id': 'r', 'start': 0, 'end': 4, 'content': '只是旧话'}],
            'interaction': {'seen': False, 'reply': {'mode': 'immediate', 'action_id': 'r'}},
        })
        self.assertEqual(forged['interaction']['reply']['mode'], 'none')

    def test_a_sole_authored_action_rescues_example_copied_or_omitted_action_ids_in_single_recipient_private_turns(self):
        # 模型照抄协议示例的 id 字面量（reply），而剧本写的是自己的 id：
        mismatched = resolve_authored_actions({'script': '她回道：<say id="s1">在呢</say>',
                                              'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'action_id': 'reply'}}})
        self.assertEqual(mismatched['interaction']['reply']['mode'], 'immediate')
        self.assertEqual(mismatched['interaction']['reply']['content'], '在呢')
        # 模型写了 say 行动与 immediate，但完全省略了引用：
        omitted = resolve_authored_actions({'script': '她回道：<say id="s1">在呢</say>',
                                           'interaction': {'seen': True, 'reply': {'mode': 'immediate'}}})
        self.assertEqual(omitted['interaction']['reply']['content'], '在呢')
        # 已有 content 的 legacy 镜像不被兜底覆盖（无引用时 content 原样保留）：
        legacy = resolve_authored_actions({'script': '她回道：<say id="s1">在呢</say>',
                                          'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '旧镜像原话'}}})
        self.assertEqual(legacy['interaction']['reply']['content'], '旧镜像原话')

    def test_the_sole_action_rescue_stays_private_scoped_and_never_grabs_a_multi_channel_action(self):
        # 群回复在场时不是单接收者回合，兜底不启用：
        with_group = resolve_authored_actions({
            'script': '她回道：<say id="s1">在呢</say>',
            'group_reply': {'mode': 'immediate', 'action_id': 's1'},
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'action_id': 'reply'}},
        })
        self.assertEqual(with_group['interaction']['reply']['mode'], 'none')
        self.assertEqual(with_group['group_reply']['content'], '在呢')
        # 跨关系行动在场时同样不启用：
        with_cross = resolve_authored_actions({
            'script': '她回道：<say id="s1">在呢</say>',
            'cross_conversation_actions': [{'participant_id': 'bob', 'mode': 'delayed', 'content': '晚点说',
                                            'send_at': '2026-09-05T13:00:00Z'}],
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'action_id': 'reply'}},
        })
        self.assertEqual(with_cross['interaction']['reply']['mode'], 'none')

    def test_legacy_delayed_and_already_streamed_delivery_keep_existing_content_and_scheduling(self):
        delayed = {'mode': 'delayed', 'content': '晚点回答', 'send_at': '2026-09-05T13:00:00Z'}
        raw = {'script': '她暂时记下这个问题。', 'interaction': {'seen': False, 'reply': delayed}}
        self.assertEqual(resolve_authored_actions(raw)['interaction']['reply'], delayed)
        streamed = resolve_authored_actions({'script': '<say id="r">晚生成的话</say>', 'interaction': {
            'seen': True, 'reply': {'mode': 'immediate', 'action_id': 'r', 'content': '已发出的原话'}}}, True)
        self.assertEqual(resolve_authored_actions(streamed)['interaction']['reply']['content'], '已发出的原话')
        legacy = resolve_authored_actions({'script': '她回答了。', 'interaction': {
            'seen': True, 'reply': {'mode': 'immediate', 'content': '好'}}})
        self.assertEqual(legacy['interaction']['reply']['content'], '好')

    def test_group_and_cross_contact_references_preserve_scope(self):
        resolved = resolve_authored_actions({
            'script': '她写下<say id="g">群里说</say>，又私发<say id="p">想好了||选第一个</say>。',
            'group_reply': {'mode': 'immediate', 'action_id': 'g'},
            'cross_conversation_actions': [{'participant_id': 'bob', 'mode': 'immediate', 'action_id': 'p', 'content': ''}],
        })
        self.assertEqual(resolved['group_reply']['content'], '群里说')
        self.assertEqual(resolved['cross_conversation_actions'][0]['content'], '想好了||选第一个')

    def test_resolve_leaves_non_script_decisions_untouched(self):
        self.assertEqual(resolve_authored_actions(None), None)
        self.assertEqual(resolve_authored_actions({'script': 42}), {'script': 42})


class DevelopmentTests(unittest.TestCase):
    """`src/script/development.ts`（上游 m7-m8 / m10 / beta6 / p1）。"""

    def test_ten_turns_in_one_scene_remain_one_development_observation_unknown_provenance_contributes_none(self):
        self.assertEqual(development_scenes([script_source(index + 1) for index in range(10)]), 1)
        self.assertEqual(development_scenes([script_source(1, 'a'), script_source(2, 'b'), script_source(3, 'c')]), 3)
        self.assertEqual(development_scenes([{**script_source(1), 'metadata': {}}]), 0)
        self.assertIsNone(development_dimension('character', 'invented.secret.path'))
        self.assertEqual(development_dimension('relationship', 'relationship.trust'), 'trust')

    def test_quiet_life_development_selection_uses_a_bounded_visible_scene_not_a_new_training_sample(self):
        entries = [{'kind': 'script', 'content': '她仍在书店查证那个问题。'}, {'kind': 'user-message', 'content': 'incoming'}]
        before = repr(entries)
        self.assertEqual(development_context_query('', [], entries), '她仍在书店查证那个问题。')
        self.assertEqual(development_context_query('当前问题', ['due'], entries), '当前问题')
        self.assertEqual(development_context_query('', [], []), '')
        self.assertEqual(repr(entries), before)

    def test_development_context_query_joins_due_summaries_into_one_bounded_line(self):
        entries = [{'kind': 'script', 'content': '她' * 1000}]
        query = development_context_query(None, ['取餐码是 8914', '她答应还书'], entries)
        self.assertTrue(query.startswith('取餐码是 8914\n她答应还书\n'))
        self.assertLessEqual(len(query), 1200)
        self.assertLessEqual(len(query.split('\n')[-1]), 800)

    def test_19_39_feedback_and_subsequent_response_stay_in_one_evidence_chain_without_endorsing_narrator_interpretation(self):
        rows = [entry(15434, 'character-message', '你活该'), entry(15435, 'user-message', '我不喜欢你这样'),
                entry(15436, 'script', '她把抗议理解成撒娇。'), entry(15437, 'character-message', '那你喜欢哪样'),
                entry(15438, 'user-message', '真的')]
        evidence = interaction_evidence(rows)
        self.assertEqual(evidence[0]['priorCommunicationEntryId'], 15434)
        self.assertEqual(evidence[0]['interpretationEntryIds'], [15436])
        draft = {'source_entry_ids': [15435, 15437],
                 'interaction_review': {'outcome': 'contested', 'feedback_entry_ids': [15435],
                                        'response_entry_ids': [15437]}}
        self.assertFalse(reviewed_development_support(draft, rows, 'alice'))
        draft['interaction_review']['outcome'] = 'supported'
        self.assertTrue(reviewed_development_support(draft, rows, 'alice'))
        self.assertFalse(reviewed_development_support(draft, rows, 'bob'))
        draft['interaction_review']['feedback_entry_ids'] = [15436]
        self.assertFalse(reviewed_development_support(draft, rows, 'alice'))

    def test_provisional_development_reaches_the_compactor_only_after_independent_scenes(self):
        candidate = {'status': 'proposed', 'source_entry_ids': [1, 2, 3]}

        def script(entry_id: int, frame: str) -> dict:
            return {**entry(entry_id, 'user-message', f'原始记录 {entry_id}'), 'kind': 'script',
                    'actor': 'narrator', 'metadata': {'frame_id': frame}}

        self.assertFalse(prompt_ready_development(candidate, [script(1, 'same'), script(2, 'same'), script(3, 'same')]))
        self.assertTrue(prompt_ready_development(candidate, [script(1, 'a'), script(2, 'b'), script(3, 'b')]))
        self.assertTrue(prompt_ready_development({**candidate, 'status': 'applied'}, [script(1, 'same')]))

    def test_development_scenes_uses_scene_checkpoints_and_frame_provenance(self):
        checkpoint = {'scene_id': 7, 'first_entry_id': 1, 'last_entry_id': 5}
        rows = [
            {'id': 1, 'kind': 'script', 'metadata': {'frame_id': 'f1', 'scene_checkpoint': checkpoint}},
            {'id': 2, 'kind': 'script', 'metadata': {'frame_id': 'f1'}},
            {'id': 3, 'kind': 'script', 'metadata': {'frame_id': 'f2'}},
            {'id': 6, 'kind': 'script', 'metadata': {'frame_id': 'f2'}},
        ]
        # 1/2/3 都落在同一个 sceneId=7 检查点内，6 通过 frameScenes 归到同一场景 → 1 个场景。
        self.assertEqual(development_scenes(rows), 1)
        rows[3]['metadata'] = {'frame_id': 'f3'}
        self.assertEqual(development_scenes(rows), 2)
        # sceneId 不是安全整数时该检查点不成立，只剩 frameId 出处。
        broken = {'scene_id': 'seven', 'first_entry_id': 1, 'last_entry_id': 5}
        self.assertEqual(
            development_scenes([{'id': 1, 'kind': 'script', 'metadata': {'frame_id': 'fx', 'scene_checkpoint': broken}}]), 1)

    def test_development_dimension_strips_known_prefixes_and_rejects_unknown_paths(self):
        self.assertEqual(development_dimension('character', 'development.preferences'), 'preferences')
        self.assertEqual(development_dimension('world', 'world.established'), 'established')
        # 前缀被剥掉后，词表仍必须属于该 target：'established' 不在 perspective 的词表里。
        self.assertIsNone(development_dimension('perspective', 'world.established'))
        self.assertIsNone(development_dimension('world', 'traits'))
        self.assertIsNone(development_dimension('relationship', 'invented.secret.path'))

    def test_interaction_evidence_only_walks_the_same_relationship_branch(self):
        rows = [entry(1, 'character-message', '甲', 'alice'), entry(2, 'user-message', '乙', 'bob'),
                entry(3, 'script', '丙', 'bob'), entry(4, 'character-message', '丁', 'bob')]
        evidence = interaction_evidence(rows)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['participantId'], 'bob')
        self.assertIsNone(evidence[0]['priorCommunicationEntryId'])
        self.assertEqual(evidence[0]['interpretationEntryIds'], [3])
        self.assertEqual(evidence[0]['responseEntryIds'], [4])


class LifeHandoffTests(unittest.TestCase):
    """`src/script/life-handoff.ts`（上游 beta6-handoff.test.ts + 上游实现契约）。"""

    def test_handoff_quotes_are_grounded_in_the_committed_prose(self):
        self.assertIsNone(normalize_life_handoff({'activity': {'value': '睡觉', 'quote': '不存在'}}, '她在看书。'))
        handoff = normalize_life_handoff({'activity': {'value': '休息', 'quote': '准备休息'}}, '她合上书本，准备休息。')
        self.assertEqual(handoff['activity']['value'], '休息')
        self.assertEqual(handoff['activity']['quote'], '准备休息')

    def test_normalize_life_handoff_bounds_and_grounding(self):
        prose = '她合上书本，准备休息。同学甲来喊她，同学乙也在。'
        # 非对象 / 空对象 / 全部字段都不合规 → None
        self.assertIsNone(normalize_life_handoff(None, prose))
        self.assertIsNone(normalize_life_handoff('睡觉', prose))
        self.assertIsNone(normalize_life_handoff({}, prose))
        self.assertIsNone(normalize_life_handoff({'activity': {'value': '睡觉', 'quote': '不存在'}}, prose))
        # quote 去空白后必须 ≥2 个字符（'她' 只有 1 个）
        self.assertIsNone(normalize_life_handoff({'activity': {'value': '休息', 'quote': '她'}}, prose))
        # value 去空白后非空、≤160
        self.assertIsNone(normalize_life_handoff({'activity': {'value': '   ', 'quote': '准备休息'}}, prose))
        self.assertIsNone(normalize_life_handoff({'activity': {'value': 'x' * 161, 'quote': '准备休息'}}, prose))
        # value 会去掉首尾空白；place/activity 都保留
        self.assertEqual(
            normalize_life_handoff({'place': {'value': ' 卧室 ', 'quote': '准备休息'},
                                    'activity': {'value': '休息', 'quote': '准备休息'}}, prose),
            {'place': {'value': '卧室', 'quote': '准备休息'}, 'activity': {'value': '休息', 'quote': '准备休息'}})
        # quote 上限 500
        long_prose = '她' + '很' * 600
        self.assertIsNone(normalize_life_handoff({'activity': {'value': '休息', 'quote': '很' * 501}}, long_prose))
        self.assertIsNotNone(normalize_life_handoff({'activity': {'value': '休息', 'quote': '很' * 500}}, long_prose))
        # transition
        self.assertEqual(normalize_life_handoff({'transition': {'quote': '准备休息'}}, prose),
                         {'transition': {'quote': '准备休息'}})
        # presence：每个名字都必须被 quote 包含；空名单是合法的（有证据的空名单）
        self.assertEqual(normalize_life_handoff({'presence': {'names': [], 'quote': '同学甲来喊她'}}, prose),
                         {'presence': {'names': [], 'quote': '同学甲来喊她'}})
        self.assertIsNone(normalize_life_handoff({'presence': {'names': ['同学甲', '老师'], 'quote': '同学甲来喊她'}}, prose))
        self.assertEqual(normalize_life_handoff({'presence': {'names': ['同学甲', '同学甲'], 'quote': '同学甲来喊她'}}, prose),
                         {'presence': {'names': ['同学甲'], 'quote': '同学甲来喊她'}})
        # presence 名单上限 8
        names_prose = '她把一二三四五六七八九记在本子上。'
        self.assertEqual(len(normalize_life_handoff(
            {'presence': {'names': list('一二三四五六七八九'), 'quote': '一二三四五六七八九'}}, names_prose)
            ['presence']['names']), 8)
        # resolvedDetails：label ≤80（不去空白）、quote 合规，过滤后取前 10 条。
        # 入参是**模型刚吐出的草稿**（上游提示词写的是 `resolvedDetails`），
        # 返回的是**内部/metadata 形状**（本仓库约定 `resolved_details`）。
        details = [{'label': f'细节{index}', 'quote': '准备休息'} for index in range(12)]
        self.assertEqual(len(normalize_life_handoff({'resolvedDetails': details}, prose)['resolved_details']), 10)
        self.assertEqual(
            normalize_life_handoff({'resolvedDetails': [{'label': 'x' * 81, 'quote': '准备休息'},
                                                         {'label': 'ok', 'quote': '准备休息'}]}, prose),
            {'resolved_details': [{'label': 'ok', 'quote': '准备休息'}]})
        # 库里已存的那一份（snake_case）重新归一化时同样要认。
        self.assertEqual(
            normalize_life_handoff({'resolved_details': [{'label': 'ok', 'quote': '准备休息'}]}, prose),
            {'resolved_details': [{'label': 'ok', 'quote': '准备休息'}]})

    def test_entry_life_handoff_and_narrative_evidence_projection(self):
        plan = {'beats': [{'at': 1, 'kind': 'state', 'summary': '仍在书店'}]}
        original = {'id': 2, 'kind': 'script', 'content': '她回到自己的卧室，独自整理书包。', 'metadata': {
            'narrative_authority': 'original-v2',
            'life_handoff': {'place': {'value': '卧室', 'quote': '回到自己的卧室'}},
            'timeline_plan': plan,
        }}
        handoff = {'place': {'value': '卧室', 'quote': '回到自己的卧室'}}
        # `entry_life_handoff` 是**内部出口**（`scene_frame.py` 按 snake_case 消费）。
        self.assertEqual(entry_life_handoff(original), handoff)
        # `narrative_evidence` 是**wire 出口**：键名逐字照上游 camelCase。
        self.assertEqual(narrative_evidence(original), {
            'narrativeAuthority': 'original-v2', 'lifeHandoff': handoff, 'proposedTimeline': plan,
        })
        # 只有 script 条目才有生活交接
        self.assertIsNone(entry_life_handoff({**original, 'kind': 'user-message'}))
        self.assertIsNone(entry_life_handoff({'id': 3, 'kind': 'script', 'content': 'p', 'metadata': {}}))
        # original-v2 但没有可用 handoff → 键存在、值为 None
        bare = {'id': 4, 'kind': 'script', 'content': 'p', 'metadata': {'narrative_authority': 'original-v2'}}
        self.assertEqual(narrative_evidence(bare), {'narrativeAuthority': 'original-v2', 'lifeHandoff': None})
        # 旧账本只有 timelineEvidence，不会被就地重新解释成 proposedTimeline
        legacy = {'id': 5, 'kind': 'script', 'content': 'p', 'metadata': {'timeline_plan': plan}}
        self.assertEqual(narrative_evidence(legacy), {'timelineEvidence': plan})
        # 有 commitId 却零投递动作 → 明确记为没有出站动作
        silent = {'id': 6, 'kind': 'script', 'content': 'p', 'metadata': {'commit_id': 'c1', 'delivery_actions': []}}
        self.assertEqual(narrative_evidence(silent), {'communicationOutcome': 'no-outgoing-action-recorded'})
        delivered = {'id': 7, 'kind': 'script', 'content': 'p',
                     'metadata': {'commit_id': 'c1', 'delivery_actions': [{'status': 'delivered'}]}}
        self.assertEqual(narrative_evidence(delivered), {})

    def test_narrative_evidence_projects_metadata_life_handoff_onto_the_upstream_wire_names(self):
        """metadata 里存 `resolved_details`，wire 上必须是上游 `resolvedDetails`。"""
        original = {'id': 8, 'kind': 'script', 'content': '她合上书本，准备休息。', 'metadata': {
            'narrative_authority': 'original-v2',
            'life_handoff': {
                'place': {'value': '卧室', 'quote': '准备休息'},
                'resolved_details': [{'label': '整理书包', 'quote': '准备休息'}],
            },
        }}
        wire = narrative_evidence(original)
        self.assertEqual(wire['lifeHandoff']['resolvedDetails'],
                         [{'label': '整理书包', 'quote': '准备休息'}])
        self.assertNotIn('resolved_details', wire['lifeHandoff'])
        # 内部出口保持 snake_case 不动。
        self.assertEqual(entry_life_handoff(original)['resolved_details'],
                         [{'label': '整理书包', 'quote': '准备休息'}])


if __name__ == '__main__':
    unittest.main()
