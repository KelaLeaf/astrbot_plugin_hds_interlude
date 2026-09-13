"""Agency Window 单元测试（不依赖 AstrBot SDK）。

移植自上游 `test/agency.test.ts`（node:test + node:assert/strict），逐条对照断言。
上游该文件的最后一条用例断言的是 `src/service.ts` 的 `groupDueIntents`，
**不属于本模块**，按 docs/PORT_PLAN.md §3 保留为 `SkipTest`，交由
`core/service.py` 的移植任务接管（不删除断言）。

运行方式（在仓库根目录）：

    python3 -m unittest plugin.tests.test_agency -v

包名与目录名解耦：按插件目录的实际名字动态 import，不硬编码。
"""

import importlib
import os
import sys
import unittest

# 把插件根目录的父目录（repo 根）加入 sys.path，便于以插件包结构 import
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 自动解析插件目录名（即 Python 包名）
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_agency = importlib.import_module(f"{_PLUGIN_DIR}.core.agency")
_time = importlib.import_module(f"{_PLUGIN_DIR}.core.time")

DEFAULT_AGENCY_CONFIG = _agency.DEFAULT_AGENCY_CONFIG
active_agency_window = _agency.active_agency_window
evaluate_agency_capacity = _agency.evaluate_agency_capacity
normalize_agency_window_draft = _agency.normalize_agency_window_draft
normalize_proactive_contact = _agency.normalize_proactive_contact
proactive_candidate_fingerprint = _agency.proactive_candidate_fingerprint
proactive_origin_bypasses_ordinary_interval = _agency.proactive_origin_bypasses_ordinary_interval
proactive_recheck_at = _agency.proactive_recheck_at
resolve_agency_config = _agency.resolve_agency_config

parse_dt = _time.parse_dt

CONFIG = resolve_agency_config({
    'enabled': True,
    'max_window_minutes': 240,
    'minimum_proactive_interval_minutes': 60,
    'max_candidate_hours': 24,
})
NOW = parse_dt('2026-08-24T08:00:00.000Z')


def candidate(**overrides):
    """上游 `candidate(overrides)`：默认候选，再叠加覆盖项。"""
    value = {
        'participant_id': 'friend', 'origin': 'life-event', 'motive': '她遇到一件想分享的事。',
        'disclosure': 'ordinary', 'source_entry_ids': [10], 'willingness': 0.8,
        'outcome': 'send-now', 'expires_at': '2026-08-25T08:00:00.000Z',
    }
    value.update(overrides)
    return value


def window(**overrides):
    """上游 `window(overrides)`：默认窗口，再叠加覆盖项。"""
    value = {
        'activity_load': 'free', 'privacy': 'private', 'device_access': 'available',
        'valid_until': '2026-08-24T12:00:00.000Z', 'basis': '她已经回到自己的房间。',
        'source_entry_ids': [10], 'updated_at': _time.iso(NOW),
    }
    value.update(overrides)
    return value


class AgencyTest(unittest.TestCase):
    def test_agency_window_accepts_only_grounded_bounded_practical_state(self):
        normalized = normalize_agency_window_draft({
            'activityLoad': 'occupied', 'privacy': 'public', 'deviceAccess': 'limited',
            'nextOpportunityAt': '2026-08-24T10:00:00.000Z', 'validUntil': '2026-08-25T10:00:00.000Z',
            'basis': '她还在教室里，周围有人。', 'sourceEntryIds': [10, 999],
        }, NOW, CONFIG, {10})
        self.assertEqual(normalized.get('activity_load'), 'occupied')
        self.assertEqual(normalized.get('source_entry_ids'), [10])
        self.assertEqual(normalized.get('valid_until'), '2026-08-24T12:00:00.000Z')
        self.assertEqual(active_agency_window(normalized, NOW).get('privacy'), 'public')

    def test_capacity_rules_separate_schedule_privacy_and_device_access_from_emotion(self):
        self.assertEqual(evaluate_agency_capacity(window(), candidate(), NOW, CONFIG)['allowed'], True)
        self.assertEqual(
            evaluate_agency_capacity(window(activity_load='overloaded'), candidate(), NOW, CONFIG)['reason'],
            'schedule-overloaded',
        )
        self.assertEqual(
            evaluate_agency_capacity(
                window(privacy='public'), candidate(disclosure='personal'), NOW, CONFIG
            )['reason'],
            'privacy-insufficient',
        )
        self.assertEqual(
            evaluate_agency_capacity(window(device_access='unavailable'), candidate(), NOW, CONFIG)['reason'],
            'device-unavailable',
        )
        self.assertEqual(
            evaluate_agency_capacity(
                window(activity_load='occupied'), candidate(origin='promise'), NOW, CONFIG
            )['allowed'],
            True,
        )

    def test_ordinary_proactive_contact_respects_minimum_interval_while_promises_bypass_it(self):
        recent = '2026-08-24T07:30:00.000Z'
        self.assertEqual(
            evaluate_agency_capacity(window(), candidate(), NOW, CONFIG, recent)['reason'],
            'minimum-proactive-interval',
        )
        self.assertEqual(
            evaluate_agency_capacity(
                window(), candidate(origin='promise'), NOW, CONFIG, recent
            )['allowed'],
            True,
        )

    def test_contact_candidates_require_permitted_target_and_real_source_evidence(self):
        normalized = normalize_proactive_contact({
            'participantId': 'friend', 'origin': 'life-event', 'motive': '她想告诉对方今天发生的事。',
            'disclosure': 'ordinary', 'willingness': 0.8, 'outcome': 'recheck-later',
        }, NOW, CONFIG, {'friend'}, set(), 42)
        self.assertEqual(normalized.get('source_entry_ids'), [42])
        self.assertIsNone(normalize_proactive_contact(
            {**normalized, 'participantId': 'blocked'}, NOW, CONFIG, {'friend'}, {42}
        ))

    def test_candidate_identity_ignores_wording_changes_and_recheck_time_stays_bounded(self):
        first = candidate(motive='第一种措辞')
        second = candidate(motive='完全不同的措辞')
        self.assertEqual(
            proactive_candidate_fingerprint(first), proactive_candidate_fingerprint(second)
        )
        capacity = evaluate_agency_capacity(
            window(activity_load='overloaded', next_opportunity_at='2026-08-24T09:00:00.000Z'),
            first, NOW, CONFIG,
        )
        self.assertEqual(
            _time.iso(proactive_recheck_at(
                first, capacity, window(next_opportunity_at='2026-08-24T09:00:00.000Z'), NOW
            )),
            '2026-08-24T09:00:00.000Z',
        )

    def test_proactive_checks_are_isolated_from_ordinary_due_messages_for_same_participant(self):
        # 上游此处断言的是 `src/service.ts` 的 `groupDueIntents`（不属于 agency.ts）。
        # 断言原样保留给 core/service.py 的移植任务，本文件先跳过（不删除断言）。
        self.skipTest("归属 core/service.py 移植任务")
        group_due_intents = importlib.import_module(f"{_PLUGIN_DIR}.core.service").group_due_intents

        def intent(intent_id, intent_type):
            return {
                'id': intent_id, 'story_id': 'story', 'participant_id': 'friend',
                'type': intent_type, 'summary': intent_type, 'not_before': NOW,
                'status': 'pending', 'payload': {}, 'created_at': NOW, 'updated_at': NOW,
            }

        batches = group_due_intents([intent(1, 'delayed-reply'), intent(2, 'proactive-check')])
        self.assertEqual(len(batches), 2)
        self.assertEqual(
            sorted(batch[0]['type'] for batch in batches), ['delayed-reply', 'proactive-check']
        )


if __name__ == '__main__':
    unittest.main()
