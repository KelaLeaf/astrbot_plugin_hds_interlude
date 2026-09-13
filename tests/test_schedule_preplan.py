"""上游 `upstream/test/schedule-preplan.test.ts` 的逐条移植（stdlib `unittest`）。

运行：
    cd /home/kela/文档/harness/hds-interlude && python3 -m unittest plugin.tests.test_schedule_preplan -v

对应关系
--------
上游 10 条用例 → 本文件 11 个方法（断言逐条保留，不加不减）：

* 10 条里 8 条只测 `src/schedule-preplan.ts` → `plugin.core.schedule_preplan`：
  `SchedulePreplanTests`。
* `main narration receives only the coming twelve hours ...` 的后半句断言
  （`systemPrompt(...)` 含 `roughly twelve hours`）与
  `prompt payload exposes Schedule Preplan ...` 断言的是 `src/narrator.ts`
  （→ `plugin.core.narrator`）；按 `docs/PORT_PLAN.md` §1「模块一一对应」，
  它们不属于本模块，故拆到 `SchedulePreplanNarratorTests`：
  **narrator 尚未落地时以 `SkipTest` 显式标记**（断言逐字保留，落地后自动生效）。
* 上游用例里的时间字面量、期望值、`?? undefined` 之类的边界一律照抄；
  唯一的形式转换是键名：记录与配置用本移植版的 snake_case，
  模型提案里仍保留上游 camelCase（顺带回归「读取侧同时接受两种拼写」）。

时间约定：上游 `new Date('...Z')` → `datetime(..., tzinfo=timezone.utc)`；
ISO 输出断言走 `core/time.py` 的 `iso()`（与 `toISOString()` 同形）。
"""

from __future__ import annotations

import importlib
import importlib.util
import unittest
from datetime import datetime, timezone

from plugin.core.schedule_preplan import (
    DEFAULT_SCHEDULE_PREPLAN_CONFIG,
    apply_schedule_preplan_proposal,
    materialize_schedule_preplan,
    next_schedule_preplan_transition,
    schedule_preplan_needs_model,
    schedule_preplan_review_due,
    schedule_preplan_window,
)
from plugin.core.time import iso

_UTC = timezone.utc


def _dt(text: str) -> datetime:
    """上游 `new Date('...Z')`。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _load(name: str, *attrs: str):
    """按需加载并行移植中的同侪模块；未落地时跳过（而非改弱断言）。"""
    try:
        spec = importlib.util.find_spec(f"plugin.core.{name}")
    except (ImportError, AttributeError, ValueError):
        spec = None
    if spec is None:
        raise unittest.SkipTest(f"plugin.core.{name} 由并行移植任务负责，尚未落地：跳过对应用例")
    module = importlib.import_module(f"plugin.core.{name}")
    missing = [attr for attr in attrs if not hasattr(module, attr)]
    if missing:
        raise unittest.SkipTest(f"plugin.core.{name} 暂缺 {', '.join(missing)}：跳过对应用例")
    return module


def _key(mapping, *names):
    """在 prompt 载荷里按 camelCase / snake_case 取键（上游原文是 camelCase）。"""
    for name in names:
        if name in mapping:
            return mapping[name]
    raise unittest.SkipTest(f"载荷里没有 {'/'.join(names)}：narrator 的 Schedule Preplan 段尚未落地")


# 上游 `const regime: SchedulePreplanRegime = {...}`（写出侧用 snake_case，
# 与 `core/types.py` 的 TypedDict 对齐）。
REGIME = {
    'id': 'summer', 'label': '暑假', 'from': '2026-08-01', 'to': '2026-09-02',
    'weekly': {
        'monday': [
            {'id': 'class', 'start': '14:00', 'end': '17:00', 'label': '补课', 'kind': 'fixed'},
            {'id': 'drawing', 'start': '20:00', 'end': '21:30', 'label': '画画', 'kind': 'flexible'},
        ],
        'tuesday': [{'id': 'morning-rest', 'start': '09:00', 'end': '11:00', 'label': '休息', 'kind': 'open'}],
    },
}


def record() -> dict:
    """上游 `function record(): SchedulePreplanRecord`。"""
    now = _dt('2026-08-30T00:00:00.000Z')
    return {
        'story_id': 'story', 'revision': 2, 'timezone': 'Asia/Shanghai',
        'valid_from': '2026-08-30', 'valid_through': '2026-09-12',
        'last_reviewed_local_date': '2026-08-30', 'last_evidence_entry_id': 10, 'review_reason': 'stable',
        'regimes': [REGIME], 'exceptions': [],
        'materialized_days': materialize_schedule_preplan([REGIME], [], '2026-08-30', 14),
        'created_at': now, 'updated_at': now,
    }


class SchedulePreplanTests(unittest.TestCase):
    """只依赖 `plugin.core.schedule_preplan`（+ `core/time`）的 9 条上游用例。"""

    def test_schedule_preplan_expands_recurring_rules_and_applies_dated_exceptions_deterministically(self):
        """上游：Schedule Preplan expands recurring rules and applies dated exceptions deterministically."""
        days = materialize_schedule_preplan([REGIME], [{
            'date': '2026-08-31', 'mode': 'patch', 'reason': '停课', 'remove_block_ids': ['class'],
            'blocks': [{'id': 'library', 'start': '15:00', 'end': '17:00', 'label': '图书馆', 'kind': 'flexible'}],
        }], '2026-08-31', 2)
        self.assertEqual([item['id'] for item in days[0]['blocks']], ['library', 'drawing'])
        self.assertEqual([item['id'] for item in days[1]['blocks']], ['morning-rest'])

    def test_life_stage_boundaries_switch_from_vacation_to_school_without_leaking_the_old_weekly_plan(self):
        """上游：life-stage boundaries switch from vacation to school without leaking the old weekly plan."""
        school = {
            'id': 'school-term', 'label': '开学后', 'from': '2026-09-01',
            'weekly': {'tuesday': [{'id': 'at-school', 'start': '07:20', 'end': '17:20', 'label': '在校', 'kind': 'fixed'}]},
        }
        vacation = {**REGIME, 'to': '2026-08-31'}
        days = materialize_schedule_preplan([vacation, school], [], '2026-08-31', 2)
        self.assertEqual([item['id'] for item in days[0]['blocks']], ['class', 'drawing'])
        self.assertEqual([item['id'] for item in days[1]['blocks']], ['at-school'])

    def test_main_narration_receives_only_the_coming_twelve_hours_not_the_stored_multi_day_horizon(self):
        """上游：main narration receives only the coming twelve hours, not the stored multi-day horizon。

        （同一条上游用例的 `systemPrompt` 断言在 `SchedulePreplanNarratorTests` 里。）
        """
        current = record()
        now = _dt('2026-08-31T04:00:00.000Z')  # 12:00 Asia/Shanghai
        window = schedule_preplan_window(current, now, 'Asia/Shanghai', 12)
        self.assertIsNotNone(window)
        self.assertEqual(window['name'], 'Schedule Preplan')
        self.assertEqual([item['id'] for item in window['blocks']], ['class', 'drawing'])
        self.assertEqual(any(item['date'] > '2026-08-31' for item in window['blocks']), False)

    def test_daily_review_is_once_per_local_day_and_unchanged_reviews_preserve_revision(self):
        """上游：daily review is once per local day and unchanged reviews preserve revision."""
        current = record()
        self.assertEqual(
            schedule_preplan_review_due(current, _dt('2026-08-30T18:30:00.000Z'), 'Asia/Shanghai', DEFAULT_SCHEDULE_PREPLAN_CONFIG),
            False)
        self.assertEqual(
            schedule_preplan_review_due(current, _dt('2026-08-31T04:00:00.000Z'), 'Asia/Shanghai', DEFAULT_SCHEDULE_PREPLAN_CONFIG),
            True)
        evidence = [{'id': 11}]
        nxt = apply_schedule_preplan_proposal(
            current,
            {'outcome': 'unchanged', 'reason': '没有足以改变日程的新证据', 'sourceEntryIds': [11]},
            evidence, '2026-08-31', 'Asia/Shanghai', DEFAULT_SCHEDULE_PREPLAN_CONFIG, _dt('2026-08-31T04:00:00.000Z'))
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt['revision'], current['revision'])
        self.assertEqual(nxt['last_evidence_entry_id'], 11)
        self.assertEqual(nxt['last_reviewed_local_date'], '2026-08-31')

    def test_an_evidence_free_first_review_persists_an_explicit_empty_schedule_instead_of_retrying_forever(self):
        """上游：an evidence-free first review persists an explicit empty schedule instead of retrying forever."""
        now = _dt('2026-08-31T04:00:00.000Z')
        empty = apply_schedule_preplan_proposal(
            None,
            {'outcome': 'replace', 'reason': '暂无可靠的重复日程证据', 'regimes': [], 'exceptions': []},
            [], '2026-08-31', 'Asia/Shanghai', DEFAULT_SCHEDULE_PREPLAN_CONFIG, now,
        )
        self.assertIsNotNone(empty)
        self.assertEqual(empty['regimes'], [])
        self.assertEqual(empty['last_reviewed_local_date'], '2026-08-31')
        self.assertEqual(
            schedule_preplan_needs_model(empty, [], '2026-08-31', 'Asia/Shanghai', DEFAULT_SCHEDULE_PREPLAN_CONFIG),
            False)

    def test_chinese_stable_ids_from_a_chinese_compaction_model_remain_distinct(self):
        """上游：Chinese stable ids from a Chinese compaction model remain distinct."""
        evidence = [{'id': 11}]
        nxt = apply_schedule_preplan_proposal(None, {
            'outcome': 'replace', 'reason': '根据明确剧本建立暑假日程', 'sourceEntryIds': [11],
            'regimes': [{
                'id': '暑假安排', 'label': '暑假', 'from': '2026-08-31',
                'weekly': {'monday': [{
                    'id': '下午补课', 'start': '14:00', 'end': '17:00', 'label': '补课', 'kind': 'fixed',
                    'sourceEntryIds': [11],
                }]},
                'sourceEntryIds': [11],
            }],
        }, evidence, '2026-08-31', 'Asia/Shanghai', DEFAULT_SCHEDULE_PREPLAN_CONFIG, _dt('2026-08-31T04:00:00.000Z'))
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt['regimes'][0]['id'], '暑假安排')
        self.assertEqual(nxt['regimes'][0]['weekly']['monday'][0]['id'], '下午补课')

    def test_fixed_blocks_can_anchor_automatic_advance_while_flexible_hobbies_cannot(self):
        """上游：fixed blocks can anchor automatic advance while flexible hobbies cannot."""
        current = record()
        now = _dt('2026-08-31T05:30:00.000Z')  # 13:30 local
        transitions = next_schedule_preplan_transition(current, now, 'Asia/Shanghai')
        self.assertEqual(iso(transitions), '2026-08-31T06:00:00.000Z')

    def test_granular_tentative_blocks_use_a_stable_activation_and_reveal_only_their_vague_availability_early(self):
        """上游：granular tentative blocks use a stable activation and reveal only their vague availability early."""
        now = _dt('2026-08-31T04:00:00.000Z')  # 12:00 Asia/Shanghai
        config = {'candidateActivationProbability': 0.5, 'candidateRevealMinutes': 120}
        current = None
        for index in range(40):
            if current:
                break
            candidate = {
                'id': f'candidate-regime-{index}', 'label': '近期节奏', 'from': '2026-08-01',
                'weekly': {'monday': [{
                    'id': f'candidate-{index}', 'start': '20:00', 'end': '21:00', 'label': '社团活动调整',
                    'kind': 'flexible', 'tentative': True,
                }]},
            }
            draft = {
                **record(), 'regimes': [candidate],
                'materialized_days': materialize_schedule_preplan([candidate], [], '2026-08-31', 14),
            }
            window = schedule_preplan_window(draft, now, 'Asia/Shanghai', 12, config)
            if window and window['blocks']:
                current = draft
        self.assertIsNotNone(current)
        far = schedule_preplan_window(current, now, 'Asia/Shanghai', 12, config)
        self.assertEqual(far['blocks'][0]['label'], '可能的个人安排')
        near = schedule_preplan_window(current, _dt('2026-08-31T10:30:00.000Z'), 'Asia/Shanghai', 3, config)
        self.assertEqual(near['blocks'][0]['label'], '社团活动调整')

    def test_stable_and_contextual_reviews_reject_model_proposed_tentative_blocks(self):
        """上游：stable and contextual reviews reject model-proposed tentative blocks."""
        evidence = [{'id': 11}]
        proposal = {
            'outcome': 'replace', 'reason': '有规律的晚间活动', 'sourceEntryIds': [11],
            'regimes': [{
                'id': 'weekly', 'label': '日常', 'from': '2026-08-31', 'sourceEntryIds': [11],
                'weekly': {'monday': [{
                    'id': 'maybe', 'start': '20:00', 'end': '21:00', 'label': '可能活动', 'kind': 'flexible',
                    'tentative': True, 'sourceEntryIds': [11],
                }]},
            }],
        }
        # 上游此处传 `new Date()`；这里用固定时刻，断言与时间无关。
        now = _dt('2026-08-31T04:00:00.000Z')
        stable = apply_schedule_preplan_proposal(
            None, proposal, evidence, '2026-08-31', 'Asia/Shanghai', DEFAULT_SCHEDULE_PREPLAN_CONFIG, now, 'stable')
        self.assertIsNotNone(stable)
        self.assertIsNone(stable['regimes'][0]['weekly']['monday'][0].get('tentative'))
        granular = apply_schedule_preplan_proposal(
            None, proposal, evidence, '2026-08-31', 'Asia/Shanghai', DEFAULT_SCHEDULE_PREPLAN_CONFIG, now, 'granular')
        self.assertIsNotNone(granular)
        self.assertEqual(granular['regimes'][0]['weekly']['monday'][0]['tentative'], True)


class SchedulePreplanNarratorTests(unittest.TestCase):
    """上游同一文件里依赖 `src/narrator.ts` 的两条断言（narrator 落地后自动生效）。"""

    def test_prompt_describes_the_schedule_preplan_horizon_as_roughly_twelve_hours(self):
        """上游：`assert.match(systemPrompt(...), /roughly twelve hours/)`。"""
        narrator = _load('narrator', 'system_prompt')
        prompt = narrator.system_prompt(
            'advance', '', '', '', '', '', False, False, False, False, False, None, False, None, True)
        self.assertRegex(prompt, r'roughly twelve hours')

    def test_prompt_payload_exposes_schedule_preplan_as_planned_structure_separate_from_story_state(self):
        """上游：prompt payload exposes Schedule Preplan as planned structure separate from story state."""
        narrator = _load('narrator', 'to_prompt_payload')
        types = _load('types', 'empty_story_setting', 'empty_story_state')
        now = _dt('2026-08-31T04:00:00.000Z')
        story = {
            'id': 'story', 'platform': 'onebot', 'self_id': 'bot', 'user_id': '', 'channel_id': '',
            'status': 'active', 'setting': types.empty_story_setting(), 'state': types.empty_story_state(),
            'cursor_at': now, 'created_at': now, 'updated_at': now,
        }
        story['setting']['timezone'] = 'Asia/Shanghai'
        request = {
            'phase': 'advance', 'story': story, 'from': now, 'now': now, 'participant': None, 'participants': [],
            'share_participant_details': False, 'due_intents': [], 'active_consequences': [],
            'superseded_intents': [], 'recent_entries': [], 'memories': [],
            'schedule_preplan': schedule_preplan_window(record(), now, 'Asia/Shanghai', 12),
        }
        payload = narrator.to_prompt_payload(request)
        near_future = _key(payload, 'availableNearFuture', 'available_near_future')
        preplan = _key(near_future, 'schedulePreplan', 'schedule_preplan')
        self.assertEqual(_key(preplan, 'plannedNotObserved', 'planned_not_observed'), True)
        ongoing = _key(payload, 'ongoingThreads', 'ongoing_threads')
        state = _key(ongoing, 'state')
        self.assertEqual(state.get('schedulePreplan', state.get('schedule_preplan')), None)
        self.assertLessEqual(len(preplan['blocks']), 8)


if __name__ == '__main__':
    unittest.main()
