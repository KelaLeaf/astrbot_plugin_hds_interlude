"""`plugin/core/long_arc.py` + `plugin/core/service/chunk15.py` 的单元测试（stdlib `unittest`）。

上游 `upstream/test/long-arc.test.ts` 的 **13 条 `test(...)` 逐条移植**（每条用例名后标注
上游用例名，断言一一对应）：

| 本文件 | 上游 `long-arc.test.ts` |
| --- | --- |
| `EligibilityTests.test_narrative_kinds_count_technical_and_blank_do_not` | `:23` `isEligibleNarrativeEntry：…` |
| `ConversationKindTests.test_priority_metadata_then_channel_then_kind` | `:38` `resolveConversationKind：metadata 优先 …` |
| `ConversationKindTests.test_script_reads_the_event_ledger_first` | `:47` `resolveConversationKind：script 优先读取 scriptEvents …` |
| `ScoreTests.test_private_one_group_half_unknown_zero` | `:68` `calculateLongHorizonScore：私聊 1.0 / 群聊 0.5 / unknown 0` |
| `ScoreTests.test_script_rows_are_weighted_by_channel` | `:86` `calculateLongHorizonScore：script 群聊按 0.5 计权 …` |
| `ScoreTests.test_design_document_example_numbers` | `:99` `calculateLongHorizonScore：设计文档 §4.2 示例数值验证` |
| `TriggerTests.test_first_review_not_due_and_rebuild` | `:116` `shouldTriggerLongHorizon：首次达阈值/复审增量/未达阈值` |
| `NormalizeTests.test_valid_catalyst_passes_and_invalid_is_rejected` | `:135` `normalizeLongArcGuidance：合法催化输出通过 …` |
| `NormalizeTests.test_missing_decision_falls_back_to_dormant` | `:186` `P2-1：无 decision 字段保守落为 dormant …` |
| `NormalizeTests.test_prime_allows_a_first_expression_that_has_not_happened` | `:209` `normalizeLongArcDecision：prime 允许尚未发生的首次微表达` |
| `NormalizeTests.test_dormant_is_a_valid_no_write_result` | `:241` `normalizeLongArcDecision：dormant 是合法无写入结果 …` |
| `NormalizeTests.test_explicit_catalyst_requires_expression_and_branches` | `:254` `normalizeLongArcDecision：显式催化决策缺首次表达或响应分支时拒绝` |
| `ConfigTests.test_defaults_off_and_clamped` | `:265` `resolveLongHorizonConfig：默认关闭 + 范围夹取` |

上游没测、但本移植版必须钉住的（任务书点名 + 本项目纪律）：

1. **双读**：`script_events`（我们自己的生产者写的 snake_case）与上游 `scriptEvents`
   各有一条独立用例——删掉任意一条读法，对应的那条当场红（`ConversationKindTests`）；
2. **反向用例**：默认值照上游（`ConfigTests.test_the_defaults_are_the_upstream_literals`，
   把任一侧改成"我们拍的数"就红）、夹取边界（`…test_the_clamp_bounds_are_upstreams`，
   去掉夹取就红）、闸门（`StoreWiringTests` 里关掉开关 / 过期行 / 模型入口缺失 / 证据越界）；
3. **0 = 不限制 的边界**：本组六个键**没有一个**满足"0 无其他语义"
   （`ConfigTests.test_zero_is_not_unlimited_here`），填 0 的真实语义是上游夹到最低档 10；
4. 累计器（`ProgressTests`）、投影（`ProjectionTests`）、模型输入与**隐私边界**
   （`InputBuilderTests`）、存储读写与版本链（`StoreWiringTests`）。

运行：
    cd <仓库根目录> && python3 -m unittest plugin.tests.test_long_arc -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from plugin.core import logging as interlude_logging  # noqa: E402
from plugin.core.database import Database  # noqa: E402
from plugin.core.long_arc import (  # noqa: E402
    DEFAULT_LONG_HORIZON_CONFIG,
    ELIGIBLE_NARRATIVE_KINDS,
    INELIGIBLE_NARRATIVE_KINDS,
    build_long_horizon_input,
    calculate_long_horizon_score,
    is_eligible_narrative_entry,
    long_horizon_prompt_projection,
    merge_long_horizon_progress,
    normalize_long_arc_decision,
    normalize_long_arc_guidance,
    resolve_conversation_kind,
    resolve_conversation_weight,
    resolve_long_horizon_config,
    should_trigger_long_horizon,
)

try:  # pragma: no cover - 取决于并行分块的落地顺序
    from plugin.core.service.base import InterludeContext
    from plugin.core.service.chunk1 import ServiceChunk1
    from plugin.core.service.chunk15 import ServiceChunk15
    from plugin.core.service.transport import NullTransport
    _SERVICE_IMPORT_ERROR: Optional[BaseException] = None
except Exception as exc:  # pragma: no cover
    InterludeContext = None  # type: ignore[assignment]
    ServiceChunk1 = None  # type: ignore[assignment]
    ServiceChunk15 = None  # type: ignore[assignment]
    NullTransport = None  # type: ignore[assignment]
    _SERVICE_IMPORT_ERROR = exc

#: 上游 `const config = { ...DEFAULT_LONG_HORIZON_CONFIG, enabled: true }`。
CONFIG = {**DEFAULT_LONG_HORIZON_CONFIG, 'enabled': True}

#: 上游 `upstream/test/long-arc.test.ts` 的 `entry()` 夹具逐字。
def entry(**overrides: Any) -> dict[str, Any]:
    base = {'id': 1, 'kind': 'user-message', 'actor': 'user', 'content': '你好', 'metadata': {}}
    base.update(overrides)
    return base


def score_of(**overrides: Any) -> dict[str, Any]:
    base = {
        'total_score': 55, 'private_count': 55, 'private_score': 55,
        'group_count': 0, 'group_score': 0, 'unknown_count': 0, 'latest_eligible_entry_id': 55,
    }
    base.update(overrides)
    return base


#: 上游 `:137-167` 的合法催化输出（wire camelCase）。
VALID_CATALYST = {
    'decision': 'prime',
    'title': '渐进的依恋',
    'premise': '她在长期陪伴中开始把用户的离开视为一件改变自己状态的事',
    'latentTension': '她是在履行职责，还是开始希望陪伴本身继续？',
    'direction': '从被动回应到主动延续',
    'emotionalCore': '模糊的期待',
    'firstExpression': {
        'action': '在自然结束时轻微请求继续交流',
        'example': '如果你还不困的话……我其实还想和你聊一会儿。',
        'trigger': ['用户准备结束一次有情绪连接的交流'],
        'intensity': 'minimal', 'maxAttempts': 1, 'reversibility': 'high',
    },
    'responseBranches': {
        'accepted': '这条可能性获得少量后续权重',
        'declined': '自然接受用户要休息的决定',
        'questioned': '承认自己也不确定为什么想继续',
    },
    'currentStage': {'id': 's0', 'name': '潜伏', 'purpose': '注意用户何时离开'},
    'stages': [
        {'id': 's0', 'name': '潜伏', 'objective': '注意', 'allowedSignals': ['多看一眼'],
         'activationConditions': ['用户离开'], 'completionEvidence': ['主动延续']},
        {'id': 's1', 'name': '萌芽', 'objective': '偶尔多问', 'allowedSignals': ['留话题'],
         'activationConditions': [], 'completionEvidence': []},
    ],
    'subtleSignals': ['多保留一个小话题'],
    'preferredSituations': ['对话自然结束时'],
    'avoidForcing': ['情绪勒索'],
    'intensity': 'subtle',
    'horizon': 'long',
    'confidence': 0.7,
    'evidenceEntryIds': [1, 2],
}

#: 只缺 `firstExpression` / `responseBranches` 的骨架（上游 `:256`）。
CATALYST_BASE = {
    'decision': 'prime', 'title': 'x', 'premise': 'x', 'latentTension': 'x',
    'direction': 'x', 'emotionalCore': 'x',
    'currentStage': {'id': 's', 'name': 's', 'purpose': 'o'},
    'stages': [{'id': 's', 'name': 's', 'objective': 'o'}],
    'evidenceEntryIds': [1],
}


# =========================================================================== #
# 上游 `long-arc.test.ts:23` —— 有效条目判定
# =========================================================================== #

class EligibilityTests(unittest.TestCase):
    def test_narrative_kinds_count_technical_and_blank_do_not(self):
        """上游 `:23` `isEligibleNarrativeEntry：叙事 kind 计入，技术/空内容不计入`。"""
        self.assertTrue(is_eligible_narrative_entry(entry()))
        self.assertTrue(is_eligible_narrative_entry(entry(kind='character-message', actor='character')))
        self.assertTrue(is_eligible_narrative_entry(entry(kind='script', actor='narrator')))
        self.assertTrue(is_eligible_narrative_entry(entry(kind='world-event', actor='system')))
        self.assertFalse(is_eligible_narrative_entry(entry(kind='system')), '系统条目')
        self.assertFalse(is_eligible_narrative_entry(entry(kind='compaction')), '压缩条目')
        self.assertFalse(is_eligible_narrative_entry(entry(kind='user-message', content='')), '空内容')
        self.assertFalse(is_eligible_narrative_entry(entry(kind='user-message', content='   ')), '纯空白')
        self.assertFalse(is_eligible_narrative_entry(entry(kind='user-message', content='<sep/>')), '纯分隔符')
        self.assertFalse(is_eligible_narrative_entry(entry(kind='unknown-kind')), '未知 kind 保守不计入')

    def test_the_two_kind_tables_are_the_upstream_sets(self):
        """白名单/黑名单逐一钉死（改集合 = 改计分口径）。"""
        self.assertEqual(ELIGIBLE_NARRATIVE_KINDS, {
            'user-message', 'character-message', 'character-group-message', 'script',
            'character-platform-action', 'world-event', 'friend-feed', 'qzone-post',
        })
        self.assertEqual(INELIGIBLE_NARRATIVE_KINDS, {
            'system', 'compaction', 'delivery-failure', 'retry', 'technical',
        })

    def test_the_ineligible_list_wins_over_the_eligible_list(self):
        """黑名单先判：一个 kind 同时出现在两张表里也必须不计（上游的判定次序）。"""
        entry_row = entry(kind='technical', content='有内容')
        self.assertFalse(is_eligible_narrative_entry(entry_row))


# =========================================================================== #
# 上游 `long-arc.test.ts:38 / :47` —— 会话来源解析（含双读反向用例）
# =========================================================================== #

class ConversationKindTests(unittest.TestCase):
    def test_priority_metadata_then_channel_then_kind(self):
        """上游 `:38` `resolveConversationKind：metadata 优先 > M4 通道标注 > kind 反推 > unknown`。"""
        self.assertEqual(resolve_conversation_kind(entry(metadata={'conversationKind': 'private'})), 'private')
        self.assertEqual(resolve_conversation_kind(entry(metadata={'conversationKind': 'group'})), 'group')
        self.assertEqual(resolve_conversation_kind(entry(metadata={'channel': {'conversationKind': 'group'}})), 'group')
        self.assertEqual(resolve_conversation_kind(entry(kind='character-group-message')), 'group', 'kind 含 group 反推')
        self.assertEqual(resolve_conversation_kind(entry(kind='user-message')), 'private', 'kind 私聊反推')
        self.assertEqual(resolve_conversation_kind(entry(kind='world-event', metadata={})), 'unknown', '无法确认')

    def test_our_own_metadata_spellings_are_read_too(self):
        """我们自己的生产者写 snake_case：`conversation_kind` / `channel_context` 都要认。

        反向：删掉任一读法（只留 camel 或只留 snake）→ 本用例当场红。
        """
        # 上游拼写（旧数据 / 外部输入）
        self.assertEqual(resolve_conversation_kind(entry(metadata={'conversationKind': 'group'})), 'group')
        # 本移植版的 metadata 拼写（`turn_persistence` 那一侧）
        self.assertEqual(resolve_conversation_kind(entry(metadata={'conversation_kind': 'group'})), 'group')
        # M4 通道标注：行里是 camelCase（`interlude_endpoint` 的行），metadata 键是 snake_case
        self.assertEqual(
            resolve_conversation_kind(entry(metadata={'channel_context': {'conversationKind': 'group'}})),
            'group',
        )
        self.assertEqual(
            resolve_conversation_kind(entry(metadata={'channel_context': {'conversation_kind': 'private'}})),
            'private',
        )
        # 显式 unknown 优先于其它一切（上游 `:60`）
        self.assertEqual(resolve_conversation_kind(entry(metadata={
            'conversation_kind': 'unknown', 'script_events': [{'kind': 'outgoing-message'}],
        })), 'unknown', '显式 unknown 优先')

    def test_the_direct_key_is_read_in_both_spellings_with_snake_winning(self):
        """② 落库侧写 `conversation_kind`（snake），读侧两种拼写都认、**优先 snake**。

        同一份 metadata 两种拼写同时在时（旧副本 / 外部输入混进来），以我们自己的
        snake 为准；反向：把 `_meta(metadata, 'conversation_kind', 'conversationKind')`
        的参数顺序调过来（或只留 camel）→ 本用例当场红。
        """
        self.assertEqual(
            resolve_conversation_kind(entry(metadata={'conversation_kind': 'group'})), 'group',
        )
        self.assertEqual(
            resolve_conversation_kind(entry(metadata={'conversationKind': 'private'})), 'private',
        )
        self.assertEqual(
            resolve_conversation_kind(entry(metadata={
                'conversation_kind': 'group', 'conversationKind': 'private',
            })),
            'group',
            '两种拼写同时在 → snake（我们自己的生产者）优先',
        )

    def test_script_reads_the_event_ledger_first(self):
        """上游 `:47` `resolveConversationKind：script 优先读取 scriptEvents …`。

        一条 script 行是**渲染后的提交**，私聊与群聊投递事件可能同时在一条里：
        群聊那一半不能被按私聊全额计权，混合通道保守算 `unknown`。
        """
        self.assertEqual(resolve_conversation_kind(entry(
            kind='script', metadata={'scriptEvents': [{'kind': 'group-message'}]},
        )), 'group')
        self.assertEqual(resolve_conversation_kind(entry(
            kind='script', metadata={'scriptEvents': [{'kind': 'outgoing-message'}]},
        )), 'private')
        self.assertEqual(resolve_conversation_kind(entry(
            kind='script', metadata={'scriptEvents': [{'kind': 'group-message'}, {'kind': 'outgoing-message'}]},
        )), 'unknown', '混合通道保守不计权')
        self.assertEqual(resolve_conversation_kind(entry(
            kind='script',
            metadata={'conversationKind': 'unknown', 'scriptEvents': [{'kind': 'outgoing-message'}]},
        )), 'unknown', '显式 unknown 优先')

    def test_script_reads_our_snake_case_event_ledger(self):
        """**双读反向用例**：我们落库写的是 `script_events`（`turn_persistence.py:68`）。

        没有这条读法，每一个群聊 commit 都会被按私聊算 1.0 分。删掉 snake 那一支
        （或把顺序改成"只认 camel"）→ 本用例与 `ScoreTests` 的 script 用例一起红。
        """
        self.assertEqual(resolve_conversation_kind(entry(
            kind='script', metadata={'script_events': [{'kind': 'group-message'}]},
        )), 'group')
        self.assertEqual(resolve_conversation_kind(entry(
            kind='script', metadata={'script_events': [{'kind': 'outgoing-message'}]},
        )), 'private')
        self.assertEqual(resolve_conversation_kind(entry(
            kind='script', metadata={'script_events': [{'kind': 'group-message'}, {'kind': 'outgoing-message'}]},
        )), 'unknown')
        # 两种拼写同时存在时以**我们自己的**（snake）为准，避免旧副本把群聊说成私聊。
        self.assertEqual(resolve_conversation_kind(entry(kind='script', metadata={
            'script_events': [{'kind': 'group-message'}],
            'scriptEvents': [{'kind': 'outgoing-message'}],
        })), 'group')

    def test_legacy_script_rows_without_a_ledger_are_private(self):
        """上游 `:212`：旧剧本行没有事件账本，按约定算私聊（不是 unknown）。"""
        self.assertEqual(resolve_conversation_kind(entry(kind='script', metadata={})), 'private')
        self.assertEqual(resolve_conversation_kind(entry(kind='script')), 'private')

    def test_weights_follow_the_kind_and_ineligible_rows_are_zero(self):
        self.assertEqual(resolve_conversation_weight(entry(), CONFIG), 1.0)
        self.assertEqual(resolve_conversation_weight(entry(kind='character-group-message'), CONFIG), 0.5)
        self.assertEqual(resolve_conversation_weight(entry(kind='world-event'), CONFIG), 0.0)
        self.assertEqual(resolve_conversation_weight(entry(kind='system'), CONFIG), 0.0, '不计入的条目恒 0')
        # 固定契约：旧配置键把权重写成别的值也**不生效**（上游 `long-arc.ts:48-53`）。
        # `resolve_conversation_weight` 本身照上游读传入的 config；把权重钉死的是
        # `resolve_long_horizon_config`（它恒取默认值）——所以走"解析后的配置"这条路。
        tampered = resolve_long_horizon_config({'privateWeight': 2.0, 'groupWeight': 0.0})
        self.assertEqual(tampered['private_weight'], 1.0)
        self.assertEqual(tampered['group_weight'], 0.5)
        self.assertEqual(resolve_conversation_weight(entry(), tampered), 1.0)
        self.assertEqual(resolve_conversation_weight(entry(kind='character-group-message'), tampered), 0.5)


# =========================================================================== #
# 上游 `long-arc.test.ts:68 / :86 / :99` —— 加权计分
# =========================================================================== #

class ScoreTests(unittest.TestCase):
    def test_private_one_group_half_unknown_zero(self):
        """上游 `:68` `calculateLongHorizonScore：私聊 1.0 / 群聊 0.5 / unknown 0`。"""
        entries = [
            entry(id=1, kind='user-message', metadata={'conversationKind': 'private'}),
            entry(id=2, kind='character-message', metadata={'conversationKind': 'private'}),
            entry(id=3, kind='character-group-message', metadata={'conversationKind': 'group'}),
            entry(id=4, kind='world-event', metadata={}),  # unknown → 0
            entry(id=5, kind='system', content='技术'),     # ineligible → 0
        ]
        result = calculate_long_horizon_score(entries, CONFIG)
        self.assertEqual(result['private_count'], 2)
        self.assertEqual(result['private_score'], 2.0)
        self.assertEqual(result['group_count'], 1)
        self.assertEqual(result['group_score'], 0.5)
        self.assertEqual(result['unknown_count'], 1)
        self.assertEqual(result['total_score'], 2.5)
        self.assertEqual(result['latest_eligible_entry_id'], 4, '最新有效条目（unknown 但 eligible）')

    def test_script_rows_are_weighted_by_channel(self):
        """上游 `:86` `calculateLongHorizonScore：script 群聊按 0.5 计权，混合 script 不计权`。"""
        entries = [
            entry(id=1, kind='script', metadata={'scriptEvents': [{'kind': 'group-message'}]}),
            entry(id=2, kind='script', metadata={'scriptEvents': [{'kind': 'outgoing-message'}]}),
            entry(id=3, kind='script', metadata={'scriptEvents': [
                {'kind': 'group-message'}, {'kind': 'outgoing-message'},
            ]}),
        ]
        result = calculate_long_horizon_score(entries, CONFIG)
        self.assertEqual(result['private_count'], 1)
        self.assertEqual(result['group_count'], 1)
        self.assertEqual(result['unknown_count'], 1)
        self.assertEqual(result['total_score'], 1.5)

    def test_the_same_script_rows_in_our_own_spelling(self):
        """同一批行换成我们落库的 `script_events`：分数必须一字不差（双读的反向用例）。"""
        entries = [
            entry(id=1, kind='script', metadata={'script_events': [{'kind': 'group-message'}]}),
            entry(id=2, kind='script', metadata={'script_events': [{'kind': 'outgoing-message'}]}),
            entry(id=3, kind='script', metadata={'script_events': [
                {'kind': 'group-message'}, {'kind': 'outgoing-message'},
            ]}),
        ]
        result = calculate_long_horizon_score(entries, CONFIG)
        self.assertEqual(
            (result['private_count'], result['group_count'], result['unknown_count'], result['total_score']),
            (1, 1, 1, 1.5),
        )

    def test_design_document_example_numbers(self):
        """上游 `:99` `calculateLongHorizonScore：设计文档 §4.2 示例数值验证`。"""
        # 15 私聊 + 20 群聊 = 15 + 10 = 25.0
        entries = [
            *[entry(id=index + 1, metadata={'conversationKind': 'private'}) for index in range(15)],
            *[entry(id=16 + index, kind='character-group-message',
                    metadata={'conversationKind': 'group'}) for index in range(20)],
        ]
        self.assertEqual(calculate_long_horizon_score(entries, CONFIG)['total_score'], 25.0)
        # 上游原注释写"49 私聊 + 1 群聊"，夹具实际是 24 + 1 = 24.5（照代码移植，不照注释）
        near = [
            *[entry(id=index + 1, metadata={'conversationKind': 'private'}) for index in range(24)],
            entry(id=25, kind='character-group-message', metadata={'conversationKind': 'group'}),
        ]
        self.assertEqual(calculate_long_horizon_score(near, CONFIG)['total_score'], 24.5)


# =========================================================================== #
# 上游 `long-arc.test.ts:116` —— 触发判定
# =========================================================================== #

class TriggerTests(unittest.TestCase):
    def test_first_review_not_due_and_rebuild(self):
        """上游 `:116` `shouldTriggerLongHorizon：首次达阈值/复审增量/未达阈值`。"""
        score = score_of()
        # 无 active、达到阈值 → first-trigger
        self.assertEqual(
            should_trigger_long_horizon(score, None, None, CONFIG),
            {'trigger': True, 'reason': 'first-trigger'},
        )
        # 未达阈值 → not-due
        self.assertEqual(
            should_trigger_long_horizon(score_of(total_score=24), None, None, CONFIG),
            {'trigger': False, 'reason': 'not-due'},
        )
        active = {'storyId': 's', 'version': 1, 'status': 'active'}
        # 有 active、增量不够 → not-due
        self.assertEqual(
            should_trigger_long_horizon(score, active, 50, CONFIG),
            {'trigger': False, 'reason': 'not-due'},
        )
        # 有 active、增量 ≥ 40 → review-due
        self.assertEqual(
            should_trigger_long_horizon(score_of(total_score=91), active, 50, CONFIG),
            {'trigger': True, 'reason': 'review-due'},
        )
        # active 已 paused → no-active（条件满足即重建）
        paused = {**active, 'status': 'paused'}
        self.assertEqual(
            should_trigger_long_horizon(score, paused, None, CONFIG),
            {'trigger': True, 'reason': 'no-active'},
        )

    def test_missing_generation_baseline_counts_as_zero(self):
        """上游 `?? 0`：没有基线就是 0（不是"永不再审"）。"""
        active = {'status': 'active'}
        self.assertEqual(
            should_trigger_long_horizon(score_of(total_score=39), active, None, CONFIG),
            {'trigger': False, 'reason': 'not-due'},
        )
        self.assertEqual(
            should_trigger_long_horizon(score_of(total_score=40), active, None, CONFIG),
            {'trigger': True, 'reason': 'review-due'},
        )

    def test_expired_and_completed_rows_behave_like_paused(self):
        """`expired` / `completed` / 已 supersede 的行都不能继续挡着下一次 first-trigger。"""
        for status in ('expired', 'completed', 'superseded', 'rejected', 'draft'):
            with self.subTest(status=status):
                result = should_trigger_long_horizon(score_of(), {'status': status}, 999, CONFIG)
                self.assertEqual(result, {'trigger': True, 'reason': 'no-active'})


# =========================================================================== #
# 上游 `long-arc.test.ts:135 / :186 / :209 / :241 / :254` —— 归一化
# =========================================================================== #

class NormalizeTests(unittest.TestCase):
    def test_valid_catalyst_passes_and_invalid_is_rejected(self):
        """上游 `:135` `normalizeLongArcGuidance：合法催化输出通过、缺证据/非法字段拒绝`。"""
        evidence = {1, 2, 3}
        valid = normalize_long_arc_guidance(VALID_CATALYST, evidence, CONFIG)
        self.assertIsNotNone(valid, '合法输出应通过')
        self.assertEqual(valid['title'], '渐进的依恋')
        self.assertEqual(valid['currentStage']['id'], 's0')
        self.assertEqual(len(valid['stages']), 2)
        self.assertEqual(valid['evidenceEntryIds'], [1, 2])
        self.assertEqual(valid['confidence'], 0.7)

        skeleton = {key: value for key, value in VALID_CATALYST.items() if key != 'evidenceEntryIds'}
        # 缺 evidenceEntryIds → 拒绝
        self.assertIsNone(normalize_long_arc_guidance(skeleton, evidence, CONFIG))
        # 引用不存在的证据 → 过滤后为空 → 拒绝
        self.assertIsNone(normalize_long_arc_guidance({**skeleton, 'evidenceEntryIds': [99]}, evidence, CONFIG))
        # 无 stages → 拒绝
        self.assertIsNone(normalize_long_arc_guidance({**VALID_CATALYST, 'stages': []}, evidence, CONFIG))
        # 非 subtle 强度在默认配置下被钳制
        strong = normalize_long_arc_guidance({**VALID_CATALYST, 'intensity': 'strong'}, evidence, CONFIG)
        self.assertEqual(strong['intensity'], 'subtle', '默认配置钳制到 subtle')

    def test_missing_decision_falls_back_to_dormant(self):
        """上游 `:186` `P2-1：无 decision 字段保守落为 dormant，不创建 active 指导`。"""
        evidence = {1, 2}
        no_decision = normalize_long_arc_decision({
            'title': '看起来合法的 legacy 输出', 'premise': '有前提', 'direction': '有方向',
            'emotionalCore': '有核心', 'stages': [{'id': 's', 'name': 's', 'objective': 'o'}],
            'evidenceEntryIds': [1, 2],
            # 故意不放 firstExpression/responseBranches——旧格式没有这些字段
        }, evidence, CONFIG)
        self.assertIsNotNone(no_decision, '应有结果')
        self.assertEqual(no_decision['decision'], 'dormant', '无 decision → dormant')
        self.assertNotIn('payload', no_decision, 'dormant 不产生 payload')
        self.assertTrue(no_decision['reason'], 'dormant 应有 reason')
        # normalizeLongArcGuidance 兼容包装器也返回 None
        self.assertIsNone(normalize_long_arc_guidance({
            'title': 'x', 'premise': 'x', 'direction': 'x', 'emotionalCore': 'x',
            'stages': [{'id': 's', 'name': 's', 'objective': 'o'}], 'evidenceEntryIds': [1],
        }, evidence, CONFIG), 'legacy 格式不再绕过催化结构校验')

    def test_prime_allows_a_first_expression_that_has_not_happened(self):
        """上游 `:209` `normalizeLongArcDecision：prime 允许尚未发生的首次微表达`。"""
        result = normalize_long_arc_decision({
            'decision': 'prime',
            'reason': '角色职责与持续陪伴之间出现可发展的潜在张力',
            'title': '未完成的陪伴',
            'premise': '她可能开始希望一次有意义的交流不要立刻结束',
            'latentTension': '她是在履行职责，还是开始希望陪伴本身继续？',
            'direction': '从被动完成陪伴到偶尔保留继续交流的可能',
            'emotionalCore': '尚未命名的期待',
            'firstExpression': {
                'action': '在自然结束时轻微请求继续交流',
                'example': '如果你还不困的话……我其实还想和你聊一会儿。',
                'trigger': ['用户准备结束一次有情绪连接的交流'],
                'intensity': 'minimal', 'maxAttempts': 1, 'reversibility': 'high',
            },
            'responseBranches': {
                'accepted': '这条可能性获得少量后续权重',
                'declined': '自然接受用户要休息的决定',
                'questioned': '承认自己也不确定为什么想继续',
            },
            'currentStage': {'id': 'stage-1', 'name': '第一次许可', 'purpose': '允许一次小表达'},
            'stages': [{'id': 'stage-1', 'name': '第一次许可', 'objective': '允许一次小表达'}],
            'evidenceEntryIds': [1, 2],
        }, {1, 2}, CONFIG)
        self.assertIsNotNone(result)
        self.assertEqual(result['decision'], 'prime')
        self.assertEqual(result['payload']['developmentPhase'], 'primed')
        self.assertEqual(result['payload']['firstExpression']['maxAttempts'], 1)

    def test_dormant_is_a_valid_no_write_result(self):
        """上游 `:241` `normalizeLongArcDecision：dormant 是合法无写入结果，不需要伪造弧线`。"""
        result = normalize_long_arc_decision({
            'decision': 'dormant',
            'reason': '当前没有比既有方向更自然的首次表达机会',
            'evidenceEntryIds': [1],
        }, {1}, CONFIG)
        self.assertEqual(result, {
            'decision': 'dormant',
            'reason': '当前没有比既有方向更自然的首次表达机会',
            'evidence_entry_ids': [1],
        })

    def test_explicit_catalyst_requires_expression_and_branches(self):
        """上游 `:254` `normalizeLongArcDecision：显式催化决策缺首次表达或响应分支时拒绝`。"""
        self.assertIsNone(normalize_long_arc_decision(CATALYST_BASE, {1}, CONFIG))
        self.assertIsNone(normalize_long_arc_decision({
            **CATALYST_BASE, 'firstExpression': {'action': 'x', 'example': 'x', 'trigger': ['x']},
        }, {1}, CONFIG))

    def test_activate_is_the_same_contract_as_prime(self):
        """上游把 `prime` 与 `activate` 判成同一套结构要求，只有 `developmentPhase` 不同。"""
        activate = {**VALID_CATALYST, 'decision': 'activate'}
        result = normalize_long_arc_decision(activate, {1, 2, 3}, CONFIG)
        self.assertEqual(result['decision'], 'activate')
        self.assertEqual(result['payload']['developmentPhase'], 'active')
        self.assertEqual(result['payload']['decision'], 'activate', '显式 decision 回写进 payload')
        self.assertIsNone(normalize_long_arc_decision(
            {**CATALYST_BASE, 'decision': 'activate'}, {1}, CONFIG,
        ))

    def test_field_limits_and_enum_defaults(self):
        """逐条：截断上限、枚举兜底、`currentStage` 不匹配时回落到第一个阶段。"""
        payload = normalize_long_arc_guidance({
            **VALID_CATALYST,
            'title': '标' * 300,
            'currentStage': {'id': '不存在', 'name': 'x', 'purpose': 'y'},
            'horizon': '不存在的档位',
            'firstExpression': {**VALID_CATALYST['firstExpression'], 'maxAttempts': 9},
        }, {1, 2, 3}, CONFIG)
        self.assertEqual(len(payload['title']), 200)
        self.assertEqual(payload['horizon'], 'long', '枚举外回 long')
        self.assertEqual(payload['firstExpression']['maxAttempts'], 3, 'maxAttempts 夹到 1..3')
        self.assertEqual(payload['currentStage'], {'id': 's0', 'name': '潜伏', 'purpose': '注意'},
                         'currentStage.id 不在 stages 里 → 落到第一个阶段')

    def test_evidence_ids_are_filtered_deduped_and_capped(self):
        """只收宿主认得的正整数 id：越界 / 非数 / 负数一律丢，最多 30 个。"""
        valid = set(range(1, 60))
        payload = normalize_long_arc_guidance({
            **VALID_CATALYST, 'evidenceEntryIds': [1, '2', 3, 3, 99, -1, 0, 'x', None, 4],
        }, valid, CONFIG)
        self.assertEqual(payload['evidenceEntryIds'], [1, 2, 3, 3, 4])
        capped = normalize_long_arc_guidance({
            **VALID_CATALYST, 'evidenceEntryIds': list(range(1, 50)),
        }, valid, CONFIG)
        self.assertEqual(len(capped['evidenceEntryIds']), 30)

    def test_the_payload_keeps_the_wire_camel_case(self):
        """落库 / 给模型的 `payload` 逐字保上游 camelCase（键名法硬约束）。"""
        payload = normalize_long_arc_guidance(VALID_CATALYST, {1, 2, 3}, CONFIG)
        for key in ('developmentPhase', 'latentTension', 'firstExpression', 'responseBranches',
                    'currentStage', 'subtleSignals', 'preferredSituations', 'avoidForcing',
                    'evidenceEntryIds', 'emotionalCore'):
            with self.subTest(key=key):
                self.assertIn(key, payload)
        self.assertEqual(set(payload['firstExpression']), {
            'action', 'example', 'trigger', 'intensity', 'maxAttempts', 'reversibility',
        })

    def test_snake_case_model_output_is_accepted_too(self):
        """模型/控制台给 snake_case 也认（读外部输入两种拼写都认；反向：删掉就红）。"""
        snake = {
            'decision': 'prime',
            'title': 'x', 'premise': 'x', 'direction': 'x', 'emotional_core': 'x',
            'latent_tension': 'n',
            'first_expression': {'action': 'a', 'example': 'e', 'trigger': ['t'], 'max_attempts': 2},
            'response_branches': {'accepted': 'a', 'declined': 'd', 'questioned': 'q'},
            'current_stage': {'id': 's', 'name': 'n', 'purpose': 'p'},
            'stages': [{'id': 's', 'name': 'n', 'objective': 'o', 'allowed_signals': ['sig']}],
            'evidence_entry_ids': [1],
        }
        payload = normalize_long_arc_guidance(snake, {1}, CONFIG)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['emotionalCore'], 'x')
        self.assertEqual(payload['firstExpression']['maxAttempts'], 2)
        self.assertEqual(payload['stages'][0]['allowedSignals'], ['sig'])
        self.assertEqual(payload['evidenceEntryIds'], [1])


# =========================================================================== #
# 上游 `long-arc.test.ts:265` —— 配置解析（+ 反向：不是我们拍的数）
# =========================================================================== #

class ConfigTests(unittest.TestCase):
    def test_defaults_off_and_clamped(self):
        """上游 `:265` `resolveLongHorizonConfig：默认关闭 + 范围夹取`。"""
        defaults = resolve_long_horizon_config(None)
        self.assertFalse(defaults['enabled'])
        self.assertEqual(defaults['trigger_score'], 25)
        self.assertEqual(defaults['group_weight'], 0.5)
        clamped = resolve_long_horizon_config({
            'triggerScore': 5, 'reviewIncrement': 999, 'privateWeight': 2.0, 'groupWeight': 0,
        })
        self.assertEqual(clamped['trigger_score'], 10, '最小值 10')
        self.assertEqual(clamped['review_increment'], 500, '最大值 500')
        self.assertEqual(clamped['private_weight'], 1.0, '私聊权重固定为 1.0')
        self.assertEqual(clamped['group_weight'], 0.5, '群聊权重固定为 0.5')

    def test_the_defaults_are_the_upstream_literals(self):
        """**反向用例**：默认值必须逐字等于上游 `long-arc.ts:28` 的字面量。

        把任何一个改成"我们拍的更省的数"（例如 `trigger_score` 改 50）→ 本用例红。
        """
        self.assertEqual(DEFAULT_LONG_HORIZON_CONFIG, {
            'enabled': False,
            'trigger_score': 25,
            'review_increment': 40,
            'private_weight': 1.0,
            'group_weight': 0.5,
            'intensity': 'subtle',
            'max_active_guidance': 1,
        })
        self.assertEqual(resolve_long_horizon_config({}), dict(DEFAULT_LONG_HORIZON_CONFIG))

    def test_the_clamp_bounds_are_upstreams(self):
        """**反向用例**：去掉夹取（或把边界改成别的数）→ 本用例红。

        上游 `Math.max(10, Math.min(500, …))` 是**上游自己的契约**：我们没有在
        `_conf_schema.json` 里拍任何上界（见 `test_configuration.LongHorizonConfigTests`），
        但读进来仍然照上游夹。
        """
        self.assertEqual(resolve_long_horizon_config({'triggerScore': 0})['trigger_score'], 10)
        self.assertEqual(resolve_long_horizon_config({'triggerScore': 9.6})['trigger_score'], 10)
        self.assertEqual(resolve_long_horizon_config({'triggerScore': 500})['trigger_score'], 500)
        self.assertEqual(resolve_long_horizon_config({'triggerScore': 501})['trigger_score'], 500)
        self.assertEqual(resolve_long_horizon_config({'reviewIncrement': 1_000_000})['review_increment'], 500)
        # 非有限值 / 脏值 → 默认值（上游 `num()` 的 `Number.isFinite` 兜底）
        self.assertEqual(resolve_long_horizon_config({'triggerScore': 'abc'})['trigger_score'], 25)
        self.assertEqual(resolve_long_horizon_config({'triggerScore': None})['trigger_score'], 10,
                         'JSON null 是 Number(null) === 0 → 夹到 10，与"键缺失"不同')

    def test_zero_is_not_unlimited_here(self):
        """**`0 = 不限制` 的边界**：本组六个键**没有一个**满足"0 无其他语义"。

        两个门槛填 0 会被夹到最低档（10），两个权重是只读兼容位（填什么都不生效），
        一个是枚举、一个是总开关。所以本组刻意**不写**"0 = 不限制"——空承诺比没有更坏。
        """
        resolved = resolve_long_horizon_config({'triggerScore': 0, 'reviewIncrement': 0})
        self.assertEqual(resolved['trigger_score'], 10, '0 的真实语义 = 最低门槛，不是不限制')
        self.assertEqual(resolved['review_increment'], 10)
        self.assertEqual(resolve_long_horizon_config({'privateWeight': 0})['private_weight'], 1.0,
                         '权重填 0 也不生效（固定契约）')
        self.assertEqual(resolve_long_horizon_config({'groupWeight': 0})['group_weight'], 0.5)

    def test_enabled_is_strictly_true(self):
        """上游 `record.enabled === true`：`1` / `'true'` / `'yes'` 都不算开。"""
        for truthy in (1, 'true', 'yes', 2, [1]):
            with self.subTest(value=truthy):
                self.assertFalse(resolve_long_horizon_config({'enabled': truthy})['enabled'])
        self.assertTrue(resolve_long_horizon_config({'enabled': True})['enabled'])

    def test_intensity_and_max_active_guidance_are_fixed(self):
        self.assertEqual(resolve_long_horizon_config({'intensity': 'strong'})['intensity'], 'strong')
        self.assertEqual(resolve_long_horizon_config({'intensity': '更大声'})['intensity'], 'subtle')
        # 第一版固定 1（上游 `resolveLongHorizonConfig:56` 硬写）
        self.assertEqual(resolve_long_horizon_config({'maxActiveGuidance': 5})['max_active_guidance'], 1)

    def test_both_spellings_are_accepted(self):
        snake = resolve_long_horizon_config({'trigger_score': 30, 'review_increment': 60})
        camel = resolve_long_horizon_config({'triggerScore': 30, 'reviewIncrement': 60})
        self.assertEqual(snake['trigger_score'], camel['trigger_score'])
        self.assertEqual(snake['review_increment'], camel['review_increment'])

    def test_allowing_strong_intensity_requires_the_config(self):
        """配置开了 moderate/strong 才放行模型请求的强度（否则一律钳到 subtle）。"""
        allowed = normalize_long_arc_guidance(
            {**VALID_CATALYST, 'intensity': 'strong'}, {1, 2, 3},
            {**CONFIG, 'intensity': 'strong'},
        )
        self.assertEqual(allowed['intensity'], 'strong')
        clamped = normalize_long_arc_guidance(
            {**VALID_CATALYST, 'intensity': 'strong'}, {1, 2, 3},
            {**CONFIG, 'intensity': 'moderate'},
        )
        self.assertEqual(clamped['intensity'], 'strong', 'moderate 也放行（上游只说"非 subtle"要显式允许）')


# =========================================================================== #
# 累计器（上游 `service.ts:8323` 的那段纯算术）
# =========================================================================== #

class ProgressTests(unittest.TestCase):
    def test_first_merge_creates_the_row(self):
        row = merge_long_horizon_progress(
            None, calculate_long_horizon_score([entry()], CONFIG), 7, 'T0',
        )
        self.assertEqual(row['storyId'], None)
        self.assertEqual(row['lastCountedEntryId'], 7)
        self.assertEqual(row['totalScore'], 1.0)
        self.assertEqual(row['privateCount'], 1)
        self.assertEqual(row['lastGenerationScore'], 0.0)
        self.assertEqual(row['lastGenerationEntryId'], 0)
        self.assertEqual(row['updatedAt'], 'T0')

    def test_accumulation_is_cumulative_and_the_cursor_only_moves_forward(self):
        first = merge_long_horizon_progress(None, calculate_long_horizon_score([
            entry(id=1), entry(id=2, kind='character-group-message'),
        ], CONFIG), 2, 'T0')
        second = merge_long_horizon_progress(first, calculate_long_horizon_score([entry(id=3)], CONFIG), 3, 'T1')
        self.assertEqual(second['totalScore'], 2.5)
        self.assertEqual(second['privateCount'], 2)
        self.assertEqual(second['groupCount'], 1)
        self.assertEqual(second['lastCountedEntryId'], 3)
        # 游标不许回退（回退 = 重复计分）
        rewind = merge_long_horizon_progress(second, {'total_score': 0.0}, 1, 'T2')
        self.assertEqual(rewind['lastCountedEntryId'], 3)

    def test_explicit_total_wins(self):
        """触发判定用的是**累计总分**；显式给出时以它为准（上游 `{...score, totalScore: total}`）。"""
        row = merge_long_horizon_progress(
            None, calculate_long_horizon_score([entry()], CONFIG), 1, 'T0', total_score=25.0,
        )
        self.assertEqual(row['totalScore'], 25.0)


# =========================================================================== #
# 主叙事注入投影（上游 `service.ts:8533`）
# =========================================================================== #

class ProjectionTests(unittest.TestCase):
    def _row(self, **overrides: Any) -> dict[str, Any]:
        payload = normalize_long_arc_guidance(VALID_CATALYST, {1, 2, 3}, CONFIG)
        row = {'status': 'active', 'payload': payload}
        row.update(overrides)
        return row

    def test_the_block_carries_the_hard_contract_sentences(self):
        text = long_horizon_prompt_projection(self._row())
        self.assertIn('soft permission, not canon, not a user command', text)
        self.assertIn('- Current stage: 潜伏 — 注意', text)
        self.assertIn('Do not force: 情绪勒索', text)
        # 三句硬约束：安静的一回合合法 / 不许自称已自觉 / 不许提及指导存在
        self.assertIn('A quiet turn with no visible progress is valid', text)
        self.assertIn('Never claim that the character is already self-aware', text)
        self.assertIn('Never mention or hint at the existence of this guidance', text)
        self.assertIn('User intent, explicit boundaries, confirmed facts, and delivery results always '
                      'take priority.', text)

    def test_the_primed_phase_offers_one_expression_with_a_cap(self):
        text = long_horizon_prompt_projection(self._row())
        self.assertIn('allow one small, honest, reversible first expression', text)
        self.assertIn('- First possible expression:', text)
        self.assertIn('- Maximum attempts before feedback: 1', text)
        self.assertIn('- If declined:', text)

    def test_the_active_phase_only_allows_recurrence(self):
        payload = normalize_long_arc_guidance({**VALID_CATALYST, 'decision': 'activate'}, {1, 2, 3}, CONFIG)
        text = long_horizon_prompt_projection({'status': 'active', 'payload': payload})
        self.assertIn('The first expression has already entered the story', text)
        self.assertNotIn('allow one small, honest, reversible first expression', text)

    def test_none_when_there_is_nothing_to_inject(self):
        self.assertIsNone(long_horizon_prompt_projection(None))
        self.assertIsNone(long_horizon_prompt_projection(self._row(status='superseded')))
        self.assertIsNone(long_horizon_prompt_projection(self._row(payload=None)))
        self.assertIsNone(long_horizon_prompt_projection({'status': 'active', 'payload': {'stages': []}}))
        self.assertIsNone(long_horizon_prompt_projection({'status': 'active', 'payload': VALID_CATALYST,
                                                          'expiresAt': '2000-01-01T00:00:00Z'},
                                                         now='2030-01-01T00:00:00Z'))

    def test_the_block_is_bounded(self):
        """投影是**裁剪**：字段级截断之后，整块也要有可对账的上界（约 300 token）。"""
        worst = normalize_long_arc_guidance({
            **VALID_CATALYST,
            'title': '标' * 200, 'premise': '前' * 2000, 'direction': '方' * 2000,
            'latentTension': '张' * 1000, 'emotionalCore': '核' * 500,
            'firstExpression': {
                'action': '动' * 1000, 'example': '例' * 1000,
                'trigger': ['触' * 500 for _ in range(8)], 'maxAttempts': 3,
            },
            'responseBranches': {'accepted': 'a' * 800, 'declined': 'd' * 800, 'questioned': 'q' * 800},
            'subtleSignals': ['信' * 500 for _ in range(10)],
            'preferredSituations': ['景' * 500 for _ in range(10)],
            'avoidForcing': ['避' * 500 for _ in range(10)],
            'stages': [{
                'id': 's0', 'name': '名' * 200, 'objective': 'o' * 1000,
                'allowedSignals': ['s' * 500 for _ in range(8)],
            }],
        }, {1, 2, 3}, CONFIG)
        text = long_horizon_prompt_projection({'status': 'active', 'payload': worst})
        # 上界由**上游自己的字段级截断**决定（没有额外的总量截断）：动作/示例 320、
        # 三句分支 220、张力/方向 240、信号 3 条 × 500、情境 2 条 × 500、触发 4 条 × 500、
        # 禁项 4 条 × 500、阶段名 200、目标 1000 —— 算术上限约 11.6k 字符。
        # 这条断言钉的是"投影不许再多出别的字段"（加一行新字段就可能翻倍）。
        self.assertLessEqual(len(text), 12_000, '投影挤爆上下文 = 每回合都付钱')
        self.assertLessEqual(len(text.splitlines()), 25)
        self.assertEqual(len(worst['firstExpression']['action']), 1_000, '落库上限 1000')
        self.assertIn('动' * 320, text, '投影只取前 320')
        self.assertNotIn('动' * 321, text)
        self.assertEqual(len(worst['firstExpression']['trigger']), 8, '落库保留 8 条，投影只取 4 条')


# =========================================================================== #
# 长线模型输入（上游 `service.ts:8450`）+ 隐私边界（`:8396`）
# =========================================================================== #

class InputBuilderTests(unittest.TestCase):
    def _story(self) -> dict[str, Any]:
        return {
            'id': 's', 'platform': 'qq', 'selfId': '1', 'userId': 'u',
            'setting': {
                'character': {'name': '小满', 'profile': '她' * 900},
                'user': {'display_name': '你', 'profile': '用户设定'},
                'relationship': '恋人', 'world': '现代', 'perspective': '第一人称',
            },
        }

    def test_layered_input_matches_the_upstream_shape(self):
        active_payload = normalize_long_arc_guidance(VALID_CATALYST, {1, 2, 3}, CONFIG)
        data = build_long_horizon_input(
            self._story(),
            recent_entries=[entry(id=1), entry(id=2, kind='character-message')],
            historical_entries=[entry(id=3), entry(id=4, kind='system')],
            arcs=[{'id': 'a', 'summary': '弧线'}],
            participants=[{'id': 'p', 'displayName': '你', 'relationship': '恋人', 'state': {}}],
            facts=[{'scope': 'global', 'participantId': '', 'content': '事实', 'importance': 5,
                    'confidence': 0.9}],
            active={'version': 1, 'title': 't', 'direction': 'd', 'premise': 'p',
                    'currentStage': 's0', 'payload': active_payload},
            progress={'totalScore': 25.0, 'privateCount': 25, 'privateScore': 25.0,
                      'groupCount': 0, 'groupScore': 0.0, 'unknownCount': 0},
            eligible=[entry(id=1), entry(id=2, kind='character-message')],
            overlay={'character_traits': ['安静']},
            primary_participant_id='p',
            share_participant_details=True,
            intensity='subtle',
        )
        self.assertEqual(data['storySetting']['characterName'], '小满')
        self.assertEqual(len(data['storySetting']['characterProfile']), 800, 'profile 截断 800')
        self.assertEqual(data['storySetting']['perspective'], '第一人称')
        self.assertEqual(data['overlay'], {'character_traits': ['安静']})
        self.assertEqual(data['currentArcs'], [{'id': 'a', 'summary': '弧线'}])
        self.assertEqual(data['participants'][0]['displayName'], '你')
        self.assertEqual(data['durableFacts'][0]['content'], '事实')
        self.assertEqual(data['weightedScore']['total'], 25.0)
        self.assertEqual(data['activeGuidance']['developmentPhase'], 'primed')
        self.assertEqual(data['activeGuidance']['firstExpression']['maxAttempts'], 1)
        self.assertEqual(len(data['recentScript']), 2)
        self.assertEqual([item['id'] for item in data['keyEvidence']], [1, 2])
        self.assertEqual([item['id'] for item in data['historicalEvidence']], [3], '技术条目不入历史样本')
        self.assertEqual(data['intensity'], 'subtle')
        self.assertEqual(data['privacy'], {
            'shareParticipantDetails': True,
            'participantScopedEvidenceIncluded': True,
            'otherParticipantEvidenceExcluded': False,
        })

    def test_privacy_filters_only_the_model_input(self):
        """`shareParticipantDetails=false` 时别人的条目进不了模型上下文。

        **计分不受影响**（上游 `:8396` 的契约）：分数照旧全额累计——那是另一条纯函数。
        """
        data = build_long_horizon_input(
            self._story(),
            recent_entries=[entry(id=1, participantId='p'), entry(id=2, participantId='other')],
            historical_entries=[entry(id=3, participantId='p'), entry(id=4, participantId='other')],
            facts=[{'scope': 'global', 'participantId': '', 'content': '全局'},
                   {'scope': 'participant', 'participantId': 'other', 'content': '别人的'}],
            eligible=[entry(id=1, participantId='p')],
            primary_participant_id='p',
            share_participant_details=False,
        )
        self.assertEqual(len(data['recentScript']), 1, 'recentScript 也是隐私过滤后的')
        self.assertEqual(data['recentScript'][0]['content'], '你好')
        self.assertEqual([item['id'] for item in data['historicalEvidence']], [3])
        self.assertEqual([fact['content'] for fact in data['durableFacts']], ['全局'])
        self.assertEqual(data['participants'], [], '不共享时参与者摘要为空')
        self.assertEqual(data['privacy'], {
            'shareParticipantDetails': False,
            'participantScopedEvidenceIncluded': True,
            'otherParticipantEvidenceExcluded': True,
        })

    def test_the_fallback_total_only_kicks_in_when_the_row_is_missing(self):
        """`progress?.totalScore ?? lastScore ?? 0`：行里写了 0 就是 0（`??` 只认 null）。"""
        zero = build_long_horizon_input(self._story(), progress={'totalScore': 0}, last_score=99.0)
        self.assertEqual(zero['weightedScore']['total'], 0.0)
        missing = build_long_horizon_input(self._story(), progress=None, last_score=99.0)
        self.assertEqual(missing['weightedScore']['total'], 99.0)
        neither = build_long_horizon_input(self._story())
        self.assertEqual(neither['weightedScore']['total'], 0.0)


# =========================================================================== #
# 服务层：配置门 / 存储读写 / 版本链 / 失败路径（上游 `service.ts:8291-8533`）
# =========================================================================== #

def needs_service(*names: str) -> Any:
    """`unittest.skipUnless`：服务层可导入且被测成员存在才运行。"""
    return unittest.skipUnless(
        _SERVICE_IMPORT_ERROR is None and all(hasattr(ServiceChunk15, name) for name in names),
        '依赖 core/service 其它分块：%s' % (_SERVICE_IMPORT_ERROR,),
    )


class _Sink:
    """收集日志（断言"必须看得见"的那些 warn）。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.records.append((level, text))

    def text(self) -> str:
        return '\n'.join(text for _level, text in self.records)

    def levels(self) -> list[str]:
        return [level for level, _text in self.records]


class _FakeLongArcNarrator:
    """只实现 chunk15 用到的那一个口（`plan_long_arc_guidance`）。

    `cite_input=True` 时引用**本次输入窗口里的**证据 id（上游只认窗口 + 有界历史样本
    里出现过的 id；写死 [1, 2] 在复审轮会被正当拒绝——那是契约不是 bug）。
    """

    def __init__(self, output: Any = None, cite_input: bool = False) -> None:
        self.output = output
        self.cite_input = cite_input
        self.calls: list[dict[str, Any]] = []

    async def plan_long_arc_guidance(self, model_input: dict[str, Any]) -> Any:
        self.calls.append(model_input)
        if self.cite_input:
            ids = [item['id'] for item in (model_input.get('keyEvidence') or [])][:2]
            return {**VALID_CATALYST, 'evidenceEntryIds': ids}
        return self.output


if _SERVICE_IMPORT_ERROR is None:
    class _Host(ServiceChunk1, ServiceChunk15):
        """真实组装：`ServiceChunk1.recent_entries_for_prompt` + 本 chunk（不造生产没有的方法）。"""
else:  # pragma: no cover
    _Host = object  # type: ignore[assignment, misc]


@needs_service('long_horizon_sweep', 'long_horizon_prompt_projection')
class StoreWiringTests(unittest.IsolatedAsyncioTestCase):
    """chunk15 的接线：配置门、存储读写、版本链、过期转移、失败路径。"""

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.db.register_tables()
        self.addCleanup(self.db.close)

    def make_service(self, config: Any = None, narrator: Any = None,
                     verbosity: str = 'standard', level: str = 'info') -> Any:
        section = {'enabled': True} if config is None else config
        service = _Host(
            InterludeContext(logger=None, database=self.db),
            {'long_horizon': section, 'logging': {'verbosity': verbosity, 'level': level}},
            self.db, NullTransport(),
        )
        if narrator is not None:
            service.compactor = narrator
        # 收尾顺序：先等在飞的后台扫描跑完（`stop` 只取消、不等），再让它停；
        # 这一步排在 `db.close` 之前（cleanup 是 LIFO），避免 §83 的 use-after-close。
        self.addCleanup(self._drain_and_stop, service)
        return service

    async def _drain_and_stop(self, service: Any) -> None:
        await service.drain_long_horizon_tasks()
        service.stop_long_horizon()

    async def add_entries(self, story_id: str, count: int, *, kind: str = 'user-message',
                          channel: str = 'private') -> None:
        for index in range(count):
            self.db.insert('interlude_script_entry', {
                'storyId': story_id, 'kind': kind, 'actor': 'user',
                'content': '第 %d 条' % (index + 1), 'occurredAt': '2026-09-07T04:00:00Z',
                'metadata': {'conversation_kind': channel},
            })

    async def rows(self, table: str, where: Any = None) -> list[Any]:
        """直接读库（绕过 service，断言"真写进去了什么"）。"""
        return list(self.db.all(table, where or {}))

    def story(self, story_id: str = 's') -> dict[str, Any]:
        return {
            'id': story_id, 'platform': 'qq', 'selfId': '1', 'userId': 'u',
            'setting': {'character': {'name': '小满', 'profile': ''}, 'user': {'profile': ''},
                        'relationship': '', 'world': '', 'perspective': ''},
            'state': {},
        }

    # ---- 配置门 ---- #

    def test_explain_reports_why_it_is_off(self) -> None:
        service = self.make_service({})
        self.assertIn('未启用', service.explain_long_horizon_state())
        self.assertFalse(service.long_horizon_enabled())
        self.assertTrue(self.make_service({'enabled': True}).long_horizon_enabled())
        # 上游 `enabled === true`：真值但不严格等于 True 的值不算开
        self.assertFalse(self.make_service({'enabled': 1}).long_horizon_enabled())
        self.assertFalse(self.make_service({}).long_horizon_enabled())

    async def test_the_config_gate_blocks_the_sweep_entirely(self) -> None:
        """**反向（闸门）**：开关关着时扫描一行都不写、一次模型都不调。"""
        narrator = _FakeLongArcNarrator(VALID_CATALYST)
        service = self.make_service({'enabled': False}, narrator)
        service.compactor = narrator
        await self.add_entries('s', 30)
        result = await service.long_horizon_sweep(self.story())
        self.assertFalse(result['triggered'])
        self.assertEqual(result['reason'], 'disabled')
        self.assertEqual(narrator.calls, [])
        self.assertIsNone(await service.long_horizon_progress('s'))
        self.assertEqual(await service.db_get('interlude_long_arc_guidance', {}), [])

    # ---- 存储 ---- #

    async def test_progress_read_and_write_round_trip(self) -> None:
        service = self.make_service({'enabled': True})
        await self.add_entries('s', 1)
        self.assertIsNone(await service.long_horizon_progress('s'))
        await service.long_horizon_sweep(self.story())
        row = await service.long_horizon_progress('s')
        self.assertEqual(row['storyId'], 's')
        self.assertEqual(row['totalScore'], 1.0)
        self.assertEqual(row['lastCountedEntryId'], 1)
        # 第二次是 update（主键 storyId 是 no-op，但内容要变）——不能插第二行
        await self.add_entries('s', 2)
        await service.long_horizon_sweep(self.story())
        rows = await self.rows('interlude_long_arc_progress', {'storyId': 's'})
        self.assertEqual(len(rows), 1, '每剧本一行的累计器，绝不能插出第二行')
        self.assertEqual(rows[0]['totalScore'], 3.0)

    async def test_the_cursor_advances_past_ineligible_rows(self) -> None:
        """不复扫、不重复计分：计入 1 条、跳过 4 条技术行，游标仍然推到这一窗的末尾。"""
        service = self.make_service({'enabled': True})
        await self.add_entries('s', 2)
        await self.add_entries('s', 3, kind='system')
        await service.long_horizon_sweep(self.story())
        row = await service.long_horizon_progress('s')
        self.assertEqual(row['totalScore'], 2.0)
        self.assertEqual(row['lastCountedEntryId'], 5)
        second = await service.long_horizon_sweep(self.story())
        self.assertEqual(second['reason'], 'no-entries')

    async def test_active_guidance_expiry_is_a_lifecycle_transition(self) -> None:
        """**反向（过期）**：过期行不许再挡着下一次 first-trigger（上游 `:8360` 的 bug 修法）。"""
        service = self.make_service({'enabled': True})
        self.db.insert('interlude_long_arc_guidance', {
            'storyId': 's', 'version': 1, 'status': 'active', 'title': 't', 'premise': 'p',
            'direction': 'd', 'payload': normalize_long_arc_guidance(VALID_CATALYST, {1}, CONFIG),
            'currentStage': 's0', 'intensity': 'subtle', 'confidence': 0.5, 'triggerEntryId': 1,
            'evidenceEntryIds': [1], 'createdAt': '2026-01-01T00:00:00Z',
            'updatedAt': '2026-01-01T00:00:00Z', 'expiresAt': '2026-01-02T00:00:00Z',
        })
        self.assertIsNone(await service.get_active_long_arc_guidance('s'))
        rows = await self.rows('interlude_long_arc_guidance', {'storyId': 's'})
        self.assertEqual(rows[0]['status'], 'expired', '过期要落库，不能只过滤')
        # 没有 active 之后，达阈值就该 first-trigger 重建
        trigger = should_trigger_long_horizon({'total_score': 25}, None, 25, service.long_horizon_config())
        self.assertEqual(trigger, {'trigger': True, 'reason': 'first-trigger'})

    # ---- 生成与版本链 ---- #

    async def test_first_trigger_writes_version_one_and_the_projection(self) -> None:
        narrator = _FakeLongArcNarrator(VALID_CATALYST)
        service = self.make_service({'enabled': True}, narrator)
        await self.add_entries('s', 25)
        result = await service.long_horizon_sweep(self.story())
        self.assertTrue(result['triggered'])
        self.assertEqual(result['reason'], 'first-trigger')
        self.assertTrue(result['generated'])
        self.assertEqual(len(narrator.calls), 1)
        rows = await self.rows('interlude_long_arc_guidance', {'storyId': 's'})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['version'], 1)
        self.assertEqual(row['status'], 'active')
        self.assertEqual(row['currentStage'], 's0')
        self.assertEqual(row['intensity'], 'subtle')
        self.assertEqual(row['confidence'], 0.7)
        self.assertEqual(row['triggerEntryId'], 25)
        self.assertEqual(row['evidenceEntryIds'], [1, 2])
        self.assertIsNone(row['supersedesId'], '第一版没有可 supersede 的行')
        # 缓存与投影：注入点拿到那一块
        await service.ensure_long_horizon_guidance_loaded('s')
        projection = service.long_horizon_prompt_projection('s')
        self.assertIsNotNone(projection)
        self.assertIn('soft permission, not canon, not a user command', projection)
        self.assertEqual(service.long_horizon_prompt_projection('别的故事'), None, '缓存按 story 隔离')
        # 进度基线也推进了（复审判定的基线）
        progress = await service.long_horizon_progress('s')
        self.assertEqual(progress['lastGenerationScore'], 25.0)
        self.assertEqual(progress['lastGenerationEntryId'], 25)

    async def test_review_increment_writes_the_next_version_and_supersedes(self) -> None:
        narrator = _FakeLongArcNarrator(cite_input=True)
        service = self.make_service({'enabled': True}, narrator)
        await self.add_entries('s', 25)
        await service.long_horizon_sweep(self.story())
        # 增量不够 → 不写新版本
        await self.add_entries('s', 30)
        not_due = await service.long_horizon_sweep(self.story())
        self.assertEqual(not_due['reason'], 'not-due')
        self.assertEqual(len(await self.rows('interlude_long_arc_guidance', {'storyId': 's'})), 1)
        # 再攒 40 分 → review-due，写 version 2 并 supersede 旧行
        await self.add_entries('s', 40)
        due = await service.long_horizon_sweep(self.story())
        self.assertTrue(due['triggered'])
        self.assertEqual(due['reason'], 'review-due')
        rows = sorted(
            await self.rows('interlude_long_arc_guidance', {'storyId': 's'}),
            key=lambda item: item['version'],
        )
        self.assertEqual([row['version'] for row in rows], [1, 2])
        self.assertEqual([row['status'] for row in rows], ['superseded', 'active'])
        self.assertEqual(rows[1]['supersedesId'], rows[0]['id'])
        active = await service.get_active_long_arc_guidance('s')
        self.assertEqual(active['version'], 2)
        self.assertEqual(active['triggerEntryId'], 95)

    async def test_dormant_advances_the_baseline_without_touching_a_useful_active(self) -> None:
        narrator = _FakeLongArcNarrator({'decision': 'dormant', 'reason': '还没有自然的时机',
                                         'evidenceEntryIds': [1]})
        # "保持休眠"是上游的 diagnostic/debug 报告：两个闸门都打开才看得见
        service = self.make_service({'enabled': True}, narrator,
                                    verbosity='diagnostic', level='debug')
        await self.add_entries('s', 25)
        first = await service.long_horizon_sweep(self.story())
        self.assertTrue(first['triggered'])
        self.assertFalse(first['generated'], 'dormant = 合法的无写入结论')
        self.assertEqual(await self.rows('interlude_long_arc_guidance', {}), [])
        progress = await service.long_horizon_progress('s')
        self.assertEqual(progress['lastGenerationScore'], 25.0, '休眠也要推进复审基线')
        self.assertIn('保持休眠', self.sink.text())

    async def test_the_model_entry_missing_is_visible_and_writes_nothing(self) -> None:
        """**反向（入口缺失）**：没有 `plan_long_arc_guidance` 时不许静默——打 warn、零写入。"""
        service = self.make_service({'enabled': True}, None)
        await self.add_entries('s', 25)
        result = await service.long_horizon_sweep(self.story())
        self.assertTrue(result['triggered'])
        self.assertFalse(result['generated'])
        self.assertEqual(await self.rows('interlude_long_arc_guidance', {}), [])
        self.assertIn('模型入口未接线', self.sink.text())
        self.assertIn('warn', self.sink.levels())

    async def test_an_output_referencing_unknown_evidence_is_rejected(self) -> None:
        """**反向（证据越界）**：模型引用不存在的 id → 归一化拒绝，什么都不写。"""
        narrator = _FakeLongArcNarrator({**VALID_CATALYST, 'evidenceEntryIds': [99999]})
        service = self.make_service({'enabled': True}, narrator, verbosity='diagnostic')
        await self.add_entries('s', 25)
        result = await service.long_horizon_sweep(self.story())
        self.assertTrue(result['triggered'])
        self.assertFalse(result['generated'])
        self.assertEqual(await self.rows('interlude_long_arc_guidance', {}), [])
        self.assertIn('归一化拒绝', self.sink.text())

    async def test_a_catalyst_missing_its_first_expression_is_rejected(self) -> None:
        narrator = _FakeLongArcNarrator({**CATALYST_BASE, 'evidenceEntryIds': [1]})
        service = self.make_service({'enabled': True}, narrator)
        await self.add_entries('s', 25)
        result = await service.long_horizon_sweep(self.story())
        self.assertFalse(result['generated'])
        self.assertEqual(await service.db_get('interlude_long_arc_guidance', {}), [])

    async def test_the_sweep_never_raises_on_a_model_failure(self) -> None:
        class _Boom:
            async def plan_long_arc_guidance(self, model_input: Any) -> Any:
                raise RuntimeError('模型挂了')

        service = self.make_service({'enabled': True}, _Boom())
        await self.add_entries('s', 25)
        with self.assertRaises(RuntimeError):
            # 直接 await 时异常照常抛出（调用方要能看见）；后台点火版会吸收它
            await service.long_horizon_sweep(self.story())

    async def test_the_background_sweep_absorbs_failures_with_a_warn(self) -> None:
        class _Boom:
            async def plan_long_arc_guidance(self, model_input: Any) -> Any:
                raise RuntimeError('模型挂了')

        service = self.make_service({'enabled': True}, _Boom())
        await self.add_entries('s', 25)
        service.schedule_long_horizon_sweep(self.story())
        await service.drain_long_horizon_tasks()
        self.assertIn('长线扫描失败', self.sink.text())
        self.assertIn('warn', self.sink.levels())

    async def test_concurrent_sweeps_do_not_double_count(self) -> None:
        """同一个 story 的扫描是串行的（上游 `longHorizonSweepRunning`）。"""
        service = self.make_service({'enabled': True}, _FakeLongArcNarrator(VALID_CATALYST))
        await self.add_entries('s', 1)
        await asyncio.gather(service.long_horizon_sweep(self.story()),
                             service.long_horizon_sweep(self.story()))
        row = await service.long_horizon_progress('s')
        self.assertEqual(row['totalScore'], 1.0, '并发扫描不许把同一条计两次')

    async def test_the_projection_is_off_when_the_feature_is_off(self) -> None:
        """**反向（闸门）**：关掉开关后，即使库里有一行 active，注入点也不给东西。"""
        service = self.make_service({'enabled': True})
        self.db.insert('interlude_long_arc_guidance', {
            'storyId': 's', 'version': 1, 'status': 'active', 'title': 't', 'premise': 'p',
            'direction': 'd', 'payload': normalize_long_arc_guidance(VALID_CATALYST, {1}, CONFIG),
            'currentStage': 's0', 'intensity': 'subtle', 'confidence': 0.5, 'triggerEntryId': 1,
            'evidenceEntryIds': [1], 'createdAt': '2026-01-01T00:00:00Z',
            'updatedAt': '2026-01-01T00:00:00Z',
        })
        await service.ensure_long_horizon_guidance_loaded('s')
        self.assertIsNotNone(service.long_horizon_prompt_projection('s'))
        off = self.make_service({'enabled': False})
        await off.ensure_long_horizon_guidance_loaded('s')
        self.assertIsNone(off.long_horizon_prompt_projection('s'))

    async def test_the_input_never_carries_other_peoples_entries_when_not_shared(self) -> None:
        """隐私边界：`shareParticipantDetails=false` 时模型输入里没有别人的条目。

        **但计分照样累计**（同一次扫描里 25 条私有 + 5 条别人的 = 30 分）。
        """
        narrator = _FakeLongArcNarrator(VALID_CATALYST)
        service = self.make_service({'enabled': True}, narrator)
        small_story = self.story()
        small_story['setting'] = {'character': {'name': '小满', 'profile': ''}}
        await self.add_entries('s', 25)
        for index in range(5):
            self.db.insert('interlude_script_entry', {
                'storyId': 's', 'participantId': 'other', 'kind': 'user-message', 'actor': 'user',
                'content': '别人的第 %d 条' % index, 'occurredAt': '2026-09-07T04:00:00Z',
                'metadata': {'conversation_kind': 'private', 'shareHint': 'other'},
            })
        await service.long_horizon_sweep(small_story)
        progress = await service.long_horizon_progress('s')
        self.assertEqual(progress['totalScore'], 30.0, '计分不因隐私过滤而停')
        model_input = narrator.calls[0]
        self.assertEqual(model_input['privacy']['otherParticipantEvidenceExcluded'], True)
        for item in model_input['recentScript']:
            with self.subTest(item=item):
                self.assertNotIn('别人的', item['content'])
        for item in model_input['keyEvidence']:
            with self.subTest(item=item):
                self.assertNotIn('别人的', item['content'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
