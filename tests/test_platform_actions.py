"""平台动作目录与权限层（本移植版新增）。

目录是**单一事实源**：提示词注入、参数校验、权限判定、控制台面板与文档都从它派生。
这里钉住四件事：目录自身的完整性（含"绝不出现主动加好友/加群"这条红线）、
权限表的归一化与档位判定、参数校验、以及提示词渲染只列启用项。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core import platform_actions as pa  # noqa: E402


class CatalogIntegrityTests(unittest.TestCase):
    def test_ids_are_unique_snake_case_and_every_category_is_labelled(self):
        self.assertEqual(len(pa.ACTIONS), len(set(pa.ACTIONS)))
        for action_id, action in pa.ACTIONS.items():
            with self.subTest(action=action_id):
                self.assertEqual(action_id, action.id)
                self.assertRegex(action_id, r'^[a-z][a-z0-9_]*$', '动作 id 用 snake_case')
                self.assertIn(action.category, pa.ACTION_CATEGORIES)
                self.assertTrue(action.label and action.summary, '标题与一句话说明都要有')
                self.assertIn(action.risk, pa.RISK_LEVELS)
                self.assertIn(action.default_permission, pa.PERMISSION_TIERS)

    def test_every_action_has_a_chinese_label_and_the_catalog_covers_all_eleven_areas(self):
        labels = {action.label for action in pa.ACTIONS.values()}
        for expected in ('戳一戳', '点赞', '撤回消息', '定时消息', '定时命令', '拉群历史',
                         '改 QQ 状态', '禁言', '踢人', '发语音', '改资料', '联系人列表'):
            self.assertIn(expected, labels, '目录缺少：%s' % expected)

    def test_dangerous_actions_default_to_disabled_and_are_listed_for_the_warning_group(self):
        risky = pa.risky_actions()
        self.assertGreaterEqual(len(risky), 10, '危险动作应该是单独一组，不是零星几个')
        for action in risky:
            with self.subTest(action=action.id):
                self.assertEqual(action.default_permission, 'disabled', '危险动作必须默认关闭')
        self.assertEqual(pa.RISK_WARNING, '以下功能包含风险操作不建议开启')
        # 用户点名的四个危险操作必须在危险组里。
        for required in ('delete_friend', 'set_group_whole_ban', 'set_group_kick', 'set_group_admin'):
            self.assertIn(required, {item.id for item in risky})

    def test_the_catalog_never_offers_proactive_friending_or_group_joining(self):
        """需求红线：只能被动同意被加好友 / 被拉群，**不许**主动加人加群。"""
        forbidden = ('add_friend', 'add_group', 'join_group', 'create_group', 'set_friend_add_request')
        for name in forbidden:
            self.assertNotIn(name, pa.ACTIONS, '目录里不该有主动社交动作：%s' % name)
        # 被动同意/拒绝这两个必须在。
        self.assertIn('handle_friend_request', pa.ACTIONS)
        self.assertIn('handle_group_request', pa.ACTIONS)

    def test_group_management_reads_and_writes_are_both_present(self):
        reads = [a.id for a in pa.ACTIONS.values() if a.category == 'group_read']
        writes = [a.id for a in pa.ACTIONS.values() if a.category == 'group_write']
        self.assertGreaterEqual(len(reads), 6)
        self.assertGreaterEqual(len(writes), 15)
        for required in ('get_group_members_info', 'get_group_notice_list', 'get_group_shut_list',
                         'send_group_notice', 'set_essence_msg', 'set_group_card', 'send_group_sign',
                         'set_group_ban', 'set_group_kick', 'set_group_whole_ban', 'set_group_admin'):
            self.assertIn(required, pa.ACTIONS)


class PermissionTests(unittest.TestCase):
    def test_normalize_drops_unknown_actions_and_unknown_tiers(self):
        table = pa.normalize_permissions({
            'send_poke': 'GLOBAL', 'set_group_ban': 'admin',
            'totally_made_up': 'global', 'send_like': 'root', 'set_group_kick': 3,
        })
        self.assertEqual(table, {'send_poke': 'global', 'set_group_ban': 'admin'})
        self.assertEqual(pa.normalize_permissions(None), {})
        self.assertEqual(pa.normalize_permissions('junk'), {})

    def test_defaults_come_from_the_catalog_and_the_config_switch_is_the_master_gate(self):
        self.assertEqual(pa.permission_for('send_poke'), 'global')
        self.assertEqual(pa.permission_for('set_group_ban'), 'disabled')
        self.assertEqual(pa.permission_for('unknown_action'), 'disabled')
        # 开关关掉 → 任何档位都变 disabled；开关没配（None）→ 不限制。
        self.assertEqual(pa.effective_permission('send_poke', None, False), 'disabled')
        self.assertEqual(pa.effective_permission('send_poke', None, True), 'global')
        self.assertFalse(pa.is_action_enabled('send_poke', None, False))
        self.assertTrue(pa.is_action_enabled('send_poke', None, None))

    def test_tier_resolution_covers_the_four_levels(self):
        table = {'send_poke': 'global', 'set_group_ban': 'groupadmin',
                 'send_voice': 'admin', 'send_like': 'disabled'}
        self.assertTrue(pa.resolve_permission('send_poke', table, None, ''))
        # groupadmin：群主/管理员放行，私聊与普通成员不放行。
        self.assertTrue(pa.resolve_permission('set_group_ban', table, None, 'owner'))
        self.assertTrue(pa.resolve_permission('set_group_ban', table, None, 'admin'))
        self.assertFalse(pa.resolve_permission('set_group_ban', table, None, 'member'))
        self.assertFalse(pa.resolve_permission('set_group_ban', table, None, ''))
        # admin：只有插件管理员。
        self.assertTrue(pa.resolve_permission('send_voice', table, None, 'admin'))
        self.assertFalse(pa.resolve_permission('send_voice', table, None, 'owner'))
        # disabled：谁都不行。
        self.assertFalse(pa.resolve_permission('send_like', table, None, 'admin'))

    def test_a_switched_off_feature_beats_an_explicit_global_permission(self):
        self.assertFalse(pa.resolve_permission('send_poke', {'send_poke': 'global'}, False, 'admin'))


class ValidationTests(unittest.TestCase):
    def test_required_params_are_enforced(self):
        accepted, reason = pa.validate_action('set_group_ban', {'duration': 60})
        self.assertIsNone(accepted)
        self.assertIn('必填', reason)
        accepted, reason = pa.validate_action('schedule_message', {'content': '早安'})
        self.assertEqual(reason, '')
        self.assertEqual(accepted, {'action': 'schedule_message', 'params': {'content': '早安'}})

    def test_target_params_are_optional_so_the_action_can_default_to_the_current_partner(self):
        # `send_like` / `send_poke` 的 user_id 刻意不是必填：留空 = 本回合的对话对象。
        accepted, reason = pa.validate_action('send_like', {'times': 3})
        self.assertEqual(reason, '')
        self.assertEqual(accepted['params'], {'times': 3})

    def test_types_are_coerced_and_camel_case_spellings_are_accepted(self):
        accepted, reason = pa.validate_action('send_like', {'userId': '10001', 'times': '3'})
        self.assertEqual(reason, '')
        self.assertEqual(accepted['params'], {'user_id': '10001', 'times': 3})
        accepted, reason = pa.validate_action('set_group_whole_ban', {'enable': 'true'})
        self.assertEqual(accepted['params'], {'enable': True})

    def test_ranges_choices_and_bad_types_are_rejected_with_a_readable_reason(self):
        self.assertIn('上限', pa.validate_action('send_like', {'times': 99})[1])
        self.assertIn('下限', pa.validate_action('set_group_ban',
                                                {'user_id': '1', 'duration': -5})[1])
        self.assertIn('只能是', pa.validate_action('recall_message', {'target': 'everything'})[1])
        self.assertIn('类型不对', pa.validate_action('send_like', {'times': '很多'})[1])

    def test_unknown_or_unavailable_actions_are_rejected(self):
        self.assertIn('未知动作', pa.validate_action('does_not_exist', {})[1])
        self.assertIn('未知动作', pa.validate_action('', {})[1])
        self.assertIn('未启用', pa.validate_action('send_poke', {}, available=['send_like'])[1])

    def test_batch_validation_keeps_the_ones_that_pass_and_reports_the_rest(self):
        accepted, rejected = pa.validate_actions([
            {'action': 'send_poke', 'params': {}},
            {'action': 'nope'},
            'junk',
            {'action': 'send_like', 'params': {'times': 1}},
        ])
        self.assertEqual([item['action'] for item in accepted], ['send_poke', 'send_like'])
        self.assertEqual(len(rejected), 2)
        self.assertTrue(any('未知动作' in item for item in rejected))
        self.assertTrue(any('不是对象' in item for item in rejected))

    def test_batch_validation_caps_the_burst_size_instead_of_dropping_silently(self):
        accepted, rejected = pa.validate_actions(
            [{'action': 'send_poke'} for _ in range(12)], limit=3,
        )
        self.assertEqual(len(accepted), 3)
        self.assertTrue(any('超过上限' in item for item in rejected))
        self.assertEqual(pa.validate_actions('junk'), ([], []))


class DescribeTests(unittest.TestCase):
    def test_only_the_listed_actions_are_rendered_and_they_are_grouped_by_category(self):
        text = pa.describe_actions(['send_poke', 'recall_message', 'send_voice'])
        self.assertIn('互动：', text)
        self.assertIn('语音：', text)
        self.assertIn('send_poke', text)
        self.assertIn('send_voice', text)
        self.assertNotIn('set_group_ban', text, '没启用的动作绝不能出现在提示词里')

    def test_parameters_are_rendered_with_ranges_and_required_marks(self):
        text = pa.describe_actions(['send_like', 'recall_message'])
        self.assertIn('times :int [1~20]', text)
        self.assertIn('target (last|entry|message)', text)

    def test_unrestricted_mode_is_for_previews_and_empty_selection_renders_nothing(self):
        self.assertIn('set_group_ban', pa.describe_actions(None))
        self.assertEqual(pa.describe_actions([]), '')

    def test_scope_filter_hides_private_only_or_group_only_actions(self):
        text = pa.describe_actions(None, scopes=['private'])
        self.assertIn('recall_message', text)


if __name__ == '__main__':
    unittest.main()
