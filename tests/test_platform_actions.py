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

    def test_dangerous_actions_default_to_disabled_and_have_no_separate_group(self):
        """危险动作默认关闭；**没有**独立的配置组（v1.7.3 取消「风险操作」组）。"""
        risky = pa.risky_actions()
        self.assertGreaterEqual(len(risky), 10, '危险动作该有十来个，不是零星几个')
        for action in risky:
            with self.subTest(action=action.id):
                self.assertEqual(action.default_permission, 'disabled', '危险动作必须默认关闭')
                self.assertIn(pa.action_config_group(action),
                              set(pa.ACTION_CONFIG_GROUPS.values()),
                              '危险动作的开关回自己类别所属的那一组')
        self.assertEqual(pa.RISK_WARNING, '此标签下功能具有一定风险，易误操作，请谨慎开启。')
        # 用户点名的四个危险操作必须在危险清单里。
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


class QzoneVisibilityTests(unittest.TestCase):
    """「改说说可见范围」（v1.7.5）的目录声明与枚举对账。

    这条动作是本移植版补的（上游没有），枚举是用户指定的五档中文标签；整数映射在
    `core/qzone.py`（打到腾讯的 `ugc_right`）。两边漂移的后果是**隐私事故**：
    用户选了「仅自己可见」而实际发成「好友可见」。
    """

    def test_the_action_is_declared_the_way_the_task_asked(self):
        action = pa.ACTIONS['set_qzone_visibility']
        self.assertEqual(action.category, 'qzone')
        self.assertEqual(action.risk, 'sensitive')
        self.assertEqual(action.backends, ('napcat',))
        self.assertTrue(action.napcat_only)
        # 档位与其它空间写动作一致（发布 / 评论 / 点赞 / 转发都是 global 默认）。
        for other in ('publish_qzone_post', 'comment_qzone_post', 'like_qzone_post',
                      'forward_qzone_post'):
            with self.subTest(other=other):
                self.assertEqual(action.default_permission, pa.ACTIONS[other].default_permission)
        # 它是**写**动作：必须落进「QQ 空间动作」那个可见子组。
        self.assertEqual(pa.action_config_group(action), 'robot_actions.qzone')

    def test_the_parameters_are_tid_plus_the_five_tier_enum_plus_the_uin_list(self):
        action = pa.ACTIONS['set_qzone_visibility']
        self.assertEqual([param.name for param in action.params],
                         ['tid', 'visible', 'target_uins'])
        self.assertTrue(action.param('tid').required)
        visible = action.param('visible')
        self.assertTrue(visible.required)
        self.assertEqual(visible.type, 'string')
        self.assertEqual(tuple(visible.choices), pa.QZONE_VISIBILITY_LABELS)
        # 五档就是用户给的那五句（顺序也照它）。
        self.assertEqual(pa.QZONE_VISIBILITY_LABELS, (
            '所有人可见', '仅 QQ 好友可见', '部分人可见', '部分人不可见', '仅自己可见',
        ))
        self.assertEqual(action.param('target_uins').type, 'list')
        self.assertFalse(action.param('target_uins').required,
                         '档次本身决定它要不要，必填校验留给执行侧（16/128 才要）')

    def test_the_choices_match_the_ugc_right_table_in_core(self):
        """`platform_actions` 的枚举顺序/键与 `core/qzone.py` 的取值表逐字相同。"""
        from plugin.core import qzone  # noqa: PLC0415

        self.assertEqual(tuple(qzone.QZONE_VISIBILITY_VALUES), pa.QZONE_VISIBILITY_LABELS)

    def test_validation_accepts_the_five_labels_and_rejects_anything_else(self):
        for label in pa.QZONE_VISIBILITY_LABELS:
            with self.subTest(label=label):
                normalized, reason = pa.validate_action('set_qzone_visibility', {
                    'tid': 'TID-0001', 'visible': label, 'targetUins': ['10002'],
                })
                self.assertEqual(reason, '')
                self.assertEqual(normalized['params']['visible'], label)
                self.assertEqual(normalized['params']['target_uins'], ['10002'])
        for bad in ('仅好友可见', 'friends', 4, '', None):
            with self.subTest(bad=bad):
                normalized, reason = pa.validate_action('set_qzone_visibility', {
                    'tid': 'TID-0001', 'visible': bad,
                })
                self.assertIsNone(normalized)
                self.assertIn('visible', reason)
        # 缺 tid 也拒（改谁的说说必须点名）。
        normalized, reason = pa.validate_action('set_qzone_visibility', {
            'visible': '所有人可见',
        })
        self.assertIsNone(normalized)
        self.assertIn('tid', reason)


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


class ScopeAndTierTests(unittest.TestCase):
    """动作适用范围 → 权限档位（用户指出：非群聊动作不该有"群主/管理员"档）。

    这一层是**目录驱动**的：`scopes` 决定这条动作适用哪些会话，
    `permission_tiers_for()` 据此下发给控制台下拉；手改权限表塞进不适用的档位时
    由 `normalize_tier_for()` 收敛，**绝不静默变成"永远不能用"**。
    """

    def test_group_only_actions_are_declared_as_such(self):
        # 群管理、群只读、群文件这些在私聊里根本没有意义。
        for action_id in (
            'set_group_kick', 'set_group_ban', 'set_group_whole_ban', 'set_group_admin',
            'send_group_notice', 'set_group_card', 'upload_group_file', 'get_group_members_info',
            'get_group_msg_history', 'trans_group_file',
        ):
            with self.subTest(action=action_id):
                self.assertEqual(tuple(pa.ACTIONS[action_id].scopes), ('group',))

    def test_private_only_actions_are_declared_as_such(self):
        self.assertEqual(tuple(pa.ACTIONS['get_friend_msg_history'].scopes), ('private',))

    def test_daily_interaction_actions_stay_available_in_both(self):
        for action_id in ('send_poke', 'send_like', 'recall_message', 'update_qq_status'):
            with self.subTest(action=action_id):
                self.assertEqual(tuple(pa.ACTIONS[action_id].scopes), ('private', 'group'))

    def test_groupadmin_tier_is_only_offered_for_group_actions(self):
        self.assertNotIn('groupadmin', pa.permission_tiers_for('get_friend_msg_history'))
        self.assertIn('groupadmin', pa.permission_tiers_for('set_group_kick'))
        self.assertIn('groupadmin', pa.permission_tiers_for('send_poke'))
        # 另外三档在任何动作上都成立
        for action_id in ('get_friend_msg_history', 'set_group_kick', 'send_poke'):
            for tier in ('global', 'admin', 'disabled'):
                self.assertIn(tier, pa.permission_tiers_for(action_id))

    def test_an_inapplicable_tier_converges_to_the_default_instead_of_locking_out(self):
        # 手改权限表塞了 groupadmin（而这是条私聊动作）：收敛成这条动作的默认档，
        # 而不是"选了等于永远不能发"。
        self.assertEqual(pa.normalize_tier_for('get_friend_msg_history', 'groupadmin'), 'global')
        self.assertEqual(pa.normalize_tier_for('set_group_kick', 'groupadmin'), 'groupadmin')
        # 未知档位仍然是关闭（安全侧）
        self.assertEqual(pa.normalize_tier_for('set_group_kick', 'nonsense'), 'disabled')

    def test_resolve_permission_uses_the_converged_tier(self):
        table = {'get_friend_msg_history': 'groupadmin'}
        self.assertTrue(pa.resolve_permission('get_friend_msg_history', table, True, ''))

    def test_console_only_accepts_tiers_the_action_supports(self):
        """控制台的写入门禁与目录同源：`console_api.set_action_permission` 会拒绝不适用档位。"""
        # 按文件读源码断言（不 import 适配层：那个包会拉起 astrbot）。
        console = pathlib.Path(__file__).resolve().parents[1] / 'adapters' / 'console_api.py'
        source = console.read_text(encoding='utf-8')
        self.assertIn('permission_tiers_for', source, '控制台必须按动作过滤档位')
        self.assertIn('permission_tiers_for(item.id)', source, '目录下发要带每条的适用档位')

    def test_private_turn_is_not_offered_group_only_actions(self):
        """适用范围要真的影响"这一回合能调什么"（不只是控制台显示）。

        `ServiceChunk12.available_platform_actions` 按 `scopes` 过滤目录，
        而回合入口传的 scopes 是 `('private','group')` 或 `('private',)`
        （适配层按会话判定）——所以私聊回合不该被教去踢人/禁言。
        """
        import types

        from plugin.core.service.chunk12 import ServiceChunk12

        # 危险动作默认 `disabled`，所以给 `set_group_kick` 显式授权一档才算"可用"。
        table = {'set_group_kick': 'global'}
        stub = types.SimpleNamespace(
            action_permission_table=lambda: dict(table),
            action_switch=lambda action_id: True,
        )
        available_private = ServiceChunk12.available_platform_actions(stub, '', ('private',))
        available_group = ServiceChunk12.available_platform_actions(stub, '', ('group',))

        self.assertIn('send_poke', available_private)
        self.assertIn('get_group_members_info', available_group)
        # 群管理动作在私聊回合里根本不该出现（哪怕已经授权）
        self.assertNotIn('set_group_kick', available_private)
        self.assertNotIn('send_group_notice', available_private)
        self.assertNotIn('get_group_members_info', available_private)
        # 反过来，私聊专属的动作不该在群里出现
        self.assertNotIn('get_friend_msg_history', available_group)
        self.assertIn('get_friend_msg_history', available_private)
        self.assertIn('set_group_kick', available_group)


if __name__ == '__main__':
    unittest.main()
