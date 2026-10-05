"""上游 `test/world-seeder.test.ts` 的逐条移植（stdlib `unittest`，零依赖）。

上游参照：`src/world-seeder.ts` 与 `test/world-seeder.test.ts`，版本 **1.0.1-rc28**。

运行（仓库根目录；发布仓布局去掉 `plugin.` 前缀）：
    python3 -m unittest plugin.tests.test_world_seeder -v

对应关系
--------
上游 8 条用例 → `WorldSeederUpstreamTests`（夹具、期望值、断言逐条保留；只做
语言转换，不加不减）：

* `runtime resolves from the assignment checkbox: no assigned provider means disabled, clamps applied`
* `parseWorldSeedEvents trims defensively and drops malformed entries`
* `validation gates reject each documented failure mode`
* `high importance is allowed during waking hours and duplicates below threshold pass`
* `summaryJaccard separates near-duplicates from unrelated text`
* `seeder prompt teaches the critical rules`
* `世界切面轮换：同槽稳定、跨槽前进、一个周期覆盖全部切面`
* `播种提示词按切面提问且每轮至多一事件`

上游没有覆盖、但本次移植必须钉住的纯函数边界 → `WorldSeederBoundaryTests`：
夜半 high 的小时边界（含一处**受控偏离**）、切面轮换不变量、畸形 payload
一律空、运行配置的三态与夹取（含 `temperature` 的 `||` 怪癖）。

语言映射
--------
* 时间：上游 `new Date('...')` → aware `datetime`（`datetime.fromisoformat`）；
  事件草稿里的时间是 `datetime` 对象，不是 ISO 字符串。
* 键名：草稿（`occursAt` / `expiresAt` …）逐字保留上游 camelCase；运行快照与
  校验上下文 `input_` 用本移植版的 snake_case（读取侧两种拼写都认）。
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone

from plugin.core.world_seeder import (
    DEFAULT_WORLD_SEEDER_RUNTIME,
    SEED_REJECTIONS,
    STATUS_DROPPED,
    STATUS_EXPIRED,
    STATUS_INJECTED,
    STATUS_INJECTING,
    STATUS_SCHEDULED,
    WORLD_SEED_DOMAINS,
    _utf16_code_unit,  # 私有：只为钉住「JS charCodeAt(0) = 高代理」这条语义
    parse_world_seed_events,
    resolve_world_seeder_runtime,
    season_for_month,
    seed_domain_for_run,
    summary_jaccard,
    validate_seed_event,
    world_seeder_system_prompt,
)

#: 上游 `const NOW = new Date('2026-09-26T10:00:00+08:00')`。
NOW = datetime.fromisoformat('2026-09-26T10:00:00+08:00')

#: 上游 `const BASE: SeedValidationInput`（键名换成 snake_case）。
BASE = {
    'now': NOW,
    'timezone': 'Asia/Shanghai',
    'max_horizon_hours': 72,
    'blocked_names': ['小桃'],
    'recent_summaries': [],
}


def draft(**overrides):
    """上游 `function draft(overrides = {})`：合法基准草稿 + 覆盖。"""
    value = {
        'summary': '楼下五金店开始装修，电钻声断断续续。',
        'importance': 'low',
        'occursAt': datetime.fromisoformat('2026-09-26T11:30:00+08:00'),
        'subjects': [],
        'rationale': '上午在家的质感事件',
    }
    value.update(overrides)
    return value


def at(text: str) -> datetime:
    """ISO-8601 字面量 → aware `datetime`（等价上游 `new Date(text)`）。"""
    return datetime.fromisoformat(text)


class WorldSeederUpstreamTests(unittest.TestCase):
    """上游 8 条用例的逐条移植。"""

    def test_runtime_resolves_from_the_assignment_checkbox(self) -> None:
        off = resolve_world_seeder_runtime(None)
        self.assertIs(off['enabled'], False)
        # 总开关开了但没有勾选“用于世界播种”的连接 → 关闭。
        unassigned = resolve_world_seeder_runtime({'enabled': True})
        self.assertIs(unassigned['enabled'], False)
        disabled_provider = resolve_world_seeder_runtime(
            {'enabled': True}, {'enabled': False, 'model': 'm'})
        self.assertIs(disabled_provider['enabled'], False)
        on = resolve_world_seeder_runtime(
            {'enabled': True, 'cadenceMinutes': 999_999, 'dailyCap': 99, 'temperature': 9},
            {'enabled': True, 'model': 'seeder-model'})
        self.assertIs(on['enabled'], True)
        self.assertEqual(on['provider']['model'], 'seeder-model')
        self.assertEqual(on['cadence_minutes'], 1_440)
        self.assertEqual(on['daily_cap'], 20)
        self.assertEqual(on['temperature'], 2)

    def test_parse_world_seed_events_trims_defensively_and_drops_malformed(self) -> None:
        parsed = parse_world_seed_events({
            'events': [
                {'summary': '  快递到了，放在驿站。 ', 'importance': 'medium',
                 'occursAt': '2026-09-26T12:00:00+08:00', 'subjects': ['快递员', 42],
                 'rationale': 'x' * 500},
                {'summary': '', 'importance': 'low', 'occursAt': '2026-09-26T12:00:00+08:00'},
                {'summary': '坏时间', 'importance': 'low', 'occursAt': 'not-a-date'},
                {'summary': '坏档', 'importance': 'huge', 'occursAt': '2026-09-26T12:00:00+08:00'},
                'junk',
            ],
        })
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['summary'], '快递到了，放在驿站。')
        self.assertEqual(parsed[0]['subjects'], ['快递员'])
        self.assertEqual(len(parsed[0]['rationale']), 200)
        self.assertEqual(parse_world_seed_events({}), [])
        self.assertEqual(parse_world_seed_events('junk'), [])

    def test_validation_gates_reject_each_documented_failure_mode(self) -> None:
        self.assertIsNone(validate_seed_event(draft(), BASE), 'clean draft passes')
        self.assertEqual(validate_seed_event(draft(summary=''), BASE), 'empty-summary')
        self.assertEqual(validate_seed_event(draft(importance='huge'), BASE), 'invalid-importance')
        self.assertEqual(
            validate_seed_event(draft(occursAt=at('2026-09-26T09:00:00+08:00')), BASE),
            'invalid-time', 'past')
        self.assertEqual(
            validate_seed_event(draft(occursAt=at('2026-10-20T09:00:00+08:00')), BASE),
            'invalid-time', 'beyond horizon')
        self.assertEqual(
            validate_seed_event(draft(summary='小桃在楼下和她打招呼。'), BASE),
            'blocked-name', 'summary mention')
        self.assertEqual(
            validate_seed_event(draft(subjects=['小桃']), BASE),
            'blocked-name', 'subject mention')
        self.assertEqual(
            validate_seed_event(
                draft(importance='high', occursAt=at('2026-09-27T02:00:00+08:00')), BASE),
            'night-high')
        self.assertEqual(
            validate_seed_event(
                draft(), {**BASE, 'recent_summaries': ['楼下五金店开始装修，电钻声断断续续响着。']}),
            'duplicate')

    def test_high_importance_waking_hours_and_sub_threshold_duplicates(self) -> None:
        self.assertIsNone(validate_seed_event(
            draft(importance='high', occursAt=at('2026-09-26T14:00:00+08:00')), BASE))
        self.assertIsNone(validate_seed_event(
            draft(), {**BASE, 'recent_summaries': ['完全无关的另一件事。']}))

    def test_summary_jaccard_separates_near_duplicates_from_unrelated_text(self) -> None:
        self.assertGreater(
            summary_jaccard('楼下五金店开始装修，电钻声断断续续。', '楼下五金店开始装修，电钻声阵阵。'),
            0.6)
        self.assertLess(
            summary_jaccard('楼下五金店开始装修。', '她在集市买了两斤苹果。'), 0.3)

    def test_seeder_prompt_teaches_the_critical_rules(self) -> None:
        prompt = world_seeder_system_prompt()
        self.assertRegex(prompt, re.compile(r'NEVER generate events about BLOCKED NAMES'))
        self.assertRegex(prompt, re.compile(r'Offline channels only'))
        self.assertRegex(prompt, re.compile(r'External facts only'))
        self.assertRegex(prompt, re.compile(r'MOST RUNS MUST RETURN an empty events array'))
        self.assertRegex(prompt, re.compile(r'high is rare'))

    def test_domain_rotation_is_stable_across_slots_and_covers_all_slices(self) -> None:
        story_id = 'fixture-story'
        slot = at('2026-09-29T01:50:00+08:00')
        first = seed_domain_for_run(story_id, slot, 45)
        self.assertEqual(seed_domain_for_run(story_id, slot, 45)['key'], first['key'],
                         '同参数确定性（同一轮 sweep 的多次调用一致）')
        following = seed_domain_for_run(story_id, slot + timedelta(minutes=45), 45)
        self.assertNotEqual(following['key'], first['key'], '相邻槽前进一格')
        sequence = [
            seed_domain_for_run(story_id, slot + timedelta(minutes=index * 45), 45)['key']
            for index in range(6)
        ]
        self.assertEqual(len(set(sequence)), 6, '一个完整周期覆盖六个切面')
        self.assertEqual(
            seed_domain_for_run(story_id, slot + timedelta(minutes=6 * 45), 45)['key'],
            first['key'], '周期回到起点')
        # 不同剧本相位不同：全部故事不会同步走同一切面
        phase = {
            seed_domain_for_run('s%d' % index, slot, 45)['key'] for index in range(1, 7)
        }
        self.assertGreater(len(phase), 1, '跨剧本相位错开')

    def test_seeder_prompt_asks_per_slice_and_at_most_one_event(self) -> None:
        plain = world_seeder_system_prompt()
        self.assertRegex(plain, re.compile(r'Output at most 1 event'))
        self.assertNotRegex(plain, re.compile(r'SLICE OF THE WORLD'),
                            '无切面时保持通用形态（兼容旧调用）')
        sliced = world_seeder_system_prompt(WORLD_SEED_DOMAINS[2])
        self.assertRegex(sliced, re.compile(r"THIS RUN'S SLICE OF THE WORLD: 生计与日常事务"))
        self.assertRegex(sliced, re.compile(r"Originate this run's event from this slice only"))
        self.assertRegex(sliced, re.compile(r'If nothing genuine fits the slice right now, return an empty array'))
        self.assertRegex(sliced, re.compile(
            r'never import real-world institutions into a world that does not have them'),
            '异世界兼容：切面按世界语汇本地化')


class WorldSeederBoundaryTests(unittest.TestCase):
    """上游没覆盖、但移植必须钉住的纯函数边界。"""

    def test_night_high_covers_the_half_past_midnight_boundary(self) -> None:
        """00:30 的 high 事件必须被拒——**受控偏离**。

        上游用 `Intl.DateTimeFormat('en-US', {hour12:false}).format()` 取小时，
        某些引擎在午夜会给出 `"24"`，于是 `24 >= 0 && 24 < 6` 为假，夜半的 high
        事件反而漏放。本移植版用 aware `datetime` 在故事时区取 `.hour`（恒在
        0–23），因此 00:30 必然命中 `night-high`——我们刻意取了更严格的一侧。
        区间与上游一致，是半开的 `[0, 6)`：05:59 拒、06:00 放。
        """
        self.assertEqual(
            validate_seed_event(
                draft(importance='high', occursAt=at('2026-09-27T00:30:00+08:00')), BASE),
            'night-high', 'Python 的 datetime.hour 在 00:30 给 0，必拒')
        self.assertEqual(
            validate_seed_event(
                draft(importance='high', occursAt=at('2026-09-27T05:59:00+08:00')), BASE),
            'night-high')
        self.assertIsNone(validate_seed_event(
            draft(importance='high', occursAt=at('2026-09-27T06:00:00+08:00')), BASE))
        # 小时的判定基准是**故事时区**，不是 UTC：UTC 02:00 == 北京 10:00 → 放行。
        self.assertIsNone(validate_seed_event(
            draft(importance='high', occursAt=at('2026-09-27T02:00:00+00:00')), BASE))
        # 时区名认不出来时上游 catch 回落 UTC（同样的绝对时刻，此时 UTC 是 02:00 → 拒）。
        self.assertEqual(
            validate_seed_event(
                draft(importance='high', occursAt=at('2026-09-27T02:00:00+00:00')),
                {**BASE, 'timezone': 'Not/AZone'}),
            'night-high')

    def test_rotation_invariants_across_slots_and_stories(self) -> None:
        slot = at('2026-09-29T01:50:00+08:00')
        first = seed_domain_for_run('fixture-story', slot, 45)
        # 同 (story_id, slot) 幂等：返回的就是切面表里的那一项（上游返回数组元素）。
        self.assertIs(seed_domain_for_run('fixture-story', slot, 45), first)
        self.assertNotEqual(
            seed_domain_for_run('fixture-story', slot + timedelta(minutes=45), 45)['key'],
            first['key'])
        cycle = {
            seed_domain_for_run('fixture-story', slot + timedelta(minutes=index * 45), 45)['key']
            for index in range(6)
        }
        self.assertEqual(len(cycle), 6, '连续 6 槽恰好覆盖 6 个切面')
        self.assertEqual(
            seed_domain_for_run('fixture-story', slot + timedelta(minutes=6 * 45), 45)['key'],
            first['key'], '第 7 槽回到起点')
        self.assertGreater(
            len({seed_domain_for_run('story-%d' % index, slot, 45)['key'] for index in range(6)}),
            1, '6 个不同 story_id 在同一槽上不会全部同一切面')
        # cadence 下限 5 分钟（上游 `Math.max(5, cadence)`）。
        self.assertEqual(
            seed_domain_for_run('fixture-story', slot, 1)['key'],
            seed_domain_for_run('fixture-story', slot, 5)['key'])

    def test_domain_hash_uses_the_utf16_code_unit(self) -> None:
        """逐字等价：JS `[...s]` 按码点切分，`ch.charCodeAt(0)` 取**第一个 UTF-16 码元**。

        星平面字符按**高代理**参与哈希（`𠮷` U+20BB7 → 0xD842），BMP 字符就是本身。
        """
        self.assertEqual(_utf16_code_unit('楼'), 0x697C)
        self.assertEqual(_utf16_code_unit('𠮷'), 0xD842)
        slot = at('2026-09-29T01:50:00+08:00')
        self.assertIn(seed_domain_for_run('𠮷-story', slot, 45), WORLD_SEED_DOMAINS)
        # 1970 年之前的槽位：Python `%` 是欧几里得取模，恒给出合法切面；上游此处
        # 会算出负下标 → `WORLD_SEED_DOMAINS[-1]` → `undefined`（受控偏离）。
        self.assertIn(
            seed_domain_for_run('fixture-story', datetime(1960, 1, 1, tzinfo=timezone.utc), 45),
            WORLD_SEED_DOMAINS)

    def test_parse_world_seed_events_rejects_junk_payloads(self) -> None:
        for value in ({}, 'junk', {'events': 'x'}, [], None, 42, {'events': None},
                      {'events': [1, 'x', None, []]}):
            self.assertEqual(parse_world_seed_events(value), [], repr(value))

    def test_parse_world_seed_events_keeps_expires_only_when_after_occurs_at(self) -> None:
        base = {
            'summary': '楼下五金店开始装修。',
            'importance': 'low',
            'occursAt': '2026-09-26T12:00:00+08:00',
        }
        kept = parse_world_seed_events(
            {'events': [{**base, 'expiresAt': '2026-09-26T18:00:00+08:00'}]})[0]
        self.assertIsInstance(kept['occursAt'], datetime, '时间值是 aware datetime，不是字符串')
        self.assertEqual(kept['expiresAt'], at('2026-09-26T18:00:00+08:00'))
        for bad in ('2026-09-26T11:00:00+08:00',  # 早于 occursAt
                    '2026-09-26T12:00:00+08:00',  # 等于 occursAt（上游是严格大于）
                    'not-a-date'):
            dropped = parse_world_seed_events({'events': [{**base, 'expiresAt': bad}]})[0]
            self.assertNotIn('expiresAt', dropped, bad)
        # 上游 `.slice(0, limit)`：默认最多 2 条。
        many = {'events': [dict(base, summary='事件%d' % index) for index in range(5)]}
        self.assertEqual(len(parse_world_seed_events(many)), 2)
        self.assertEqual(len(parse_world_seed_events(many, 4)), 4)

    def test_runtime_is_disabled_for_all_three_unassigned_shapes(self) -> None:
        provider = {'enabled': True, 'model': 'seeder-model'}
        # ① 无 provider ② 总开关开了但没有 provider ③ provider 被显式关掉
        self.assertIs(resolve_world_seeder_runtime(None)['enabled'], False)
        self.assertIs(resolve_world_seeder_runtime({})['enabled'], False)
        self.assertIs(resolve_world_seeder_runtime({'enabled': True})['enabled'], False)
        self.assertIs(resolve_world_seeder_runtime(
            {'enabled': True}, {'enabled': False, 'model': 'm'})['enabled'], False)
        # 总开关本身没开（`record.enabled === true` 严格相等）
        self.assertIs(resolve_world_seeder_runtime({'enabled': False}, provider)['enabled'], False)
        # 勾了连接但没填模型 → 不算勾选
        self.assertIs(resolve_world_seeder_runtime(
            {'enabled': True}, {'enabled': True, 'model': '   '})['enabled'], False)
        self.assertIs(resolve_world_seeder_runtime({'enabled': True}, provider)['enabled'], True)

    def test_runtime_clamps_and_reads_both_spellings(self) -> None:
        provider = {'enabled': True, 'model': 'seeder-model'}
        on = resolve_world_seeder_runtime({
            'enabled': True, 'cadenceMinutes': 999_999, 'dailyCap': 99, 'maxPending': 99,
            'maxHorizonHours': 99_999, 'maxTokens': 1, 'timeout': 10, 'temperature': 9,
        }, provider)
        self.assertEqual(on['cadence_minutes'], 1_440)
        self.assertEqual((on['max_pending'], on['daily_cap']), (20, 20))
        self.assertEqual(on['max_horizon_hours'], 336)
        self.assertEqual((on['max_tokens'], on['timeout']), (256, 5_000))
        self.assertEqual(on['temperature'], 2)
        low = resolve_world_seeder_runtime({
            'enabled': True, 'cadenceMinutes': 1, 'maxPending': 0, 'dailyCap': -5,
            'maxHorizonHours': 0, 'maxTokens': 0, 'timeout': 0,
        }, provider)
        self.assertEqual(low['cadence_minutes'], 5)
        self.assertEqual((low['max_pending'], low['daily_cap']), (1, 1))
        self.assertEqual((low['max_horizon_hours'], low['max_tokens'], low['timeout']),
                         (1, 256, 5_000))
        # snake_case 也认；两种拼写同时出现时优先 camelCase
        snake = resolve_world_seeder_runtime(
            {'enabled': True, 'cadence_minutes': 30, 'daily_cap': 7}, provider)
        self.assertEqual((snake['cadence_minutes'], snake['daily_cap']), (30, 7))
        both = resolve_world_seeder_runtime(
            {'enabled': True, 'cadenceMinutes': 60, 'cadence_minutes': 30}, provider)
        self.assertEqual(both['cadence_minutes'], 60)
        # 非数字回落默认；显式 null 是 `Number(null) === 0` → 夹到下限（不是回落默认）
        self.assertEqual(
            resolve_world_seeder_runtime({'enabled': True, 'cadenceMinutes': 'abc'}, provider)
            ['cadence_minutes'], 45)
        self.assertEqual(
            resolve_world_seeder_runtime({'enabled': True, 'cadenceMinutes': None}, provider)
            ['cadence_minutes'], 5)
        # 缺键（undefined）走 `Number(undefined) = NaN` → 回落默认
        self.assertEqual(
            resolve_world_seeder_runtime({'enabled': True}, provider)['max_pending'], 4)
        # 默认快照不被污染
        self.assertEqual(DEFAULT_WORLD_SEEDER_RUNTIME['cadence_minutes'], 45)
        self.assertIsNone(DEFAULT_WORLD_SEEDER_RUNTIME['provider'])

    def test_temperature_keeps_the_upstream_or_quirk(self) -> None:
        """上游 `Number(record.temperature) || 0.9`：显式 0 / NaN / null 全部回落 0.9。

        这是逐字对等、不是 bug；真要让「零温度」生效得改上游。本测试把它钉死，
        免得日后有人"顺手修好"而静默改变采样行为。
        """
        provider = {'enabled': True, 'model': 'm'}
        for value in (0, 0.0, None, 'abc', float('nan')):
            self.assertEqual(
                resolve_world_seeder_runtime(
                    {'enabled': True, 'temperature': value}, provider)['temperature'],
                0.9, repr(value))
        self.assertEqual(resolve_world_seeder_runtime(
            {'enabled': True, 'temperature': -1}, provider)['temperature'], 0.0)
        self.assertEqual(resolve_world_seeder_runtime(
            {'enabled': True, 'temperature': '0.5'}, provider)['temperature'], 0.5)
        self.assertEqual(resolve_world_seeder_runtime(
            {'enabled': True, 'temperature': float('inf')}, provider)['temperature'], 2.0)

    def test_length_gate_measures_the_untrimmed_summary(self) -> None:
        """上游量的是**未 trim** 的 `summary.length`：201 个字符里带空白也算超长。"""
        self.assertEqual(
            validate_seed_event(draft(summary='楼' * 200 + ' '), BASE), 'empty-summary')
        # 200 字符（含首尾空白）刚好通过长度闸
        self.assertIsNone(validate_seed_event(draft(summary=' 楼' + '下' * 197 + ' '), BASE))

    def test_constants_and_season_for_month(self) -> None:
        self.assertEqual([domain['key'] for domain in WORLD_SEED_DOMAINS],
                         ['nature', 'dwelling', 'livelihood', 'close-people', 'paths', 'chance'])
        self.assertEqual([domain['label'] for domain in WORLD_SEED_DOMAINS],
                         ['天象与环境', '居所与近邻', '生计与日常事务', '亲近之人', '途中与陌生人', '小意外与际遇'])
        self.assertEqual(SEED_REJECTIONS, frozenset({
            'invalid-shape', 'empty-summary', 'invalid-importance', 'invalid-time',
            'blocked-name', 'night-high', 'duplicate'}))
        self.assertEqual(
            (STATUS_SCHEDULED, STATUS_INJECTING, STATUS_INJECTED, STATUS_EXPIRED, STATUS_DROPPED),
            ('scheduled', 'injecting', 'injected', 'expired', 'dropped'))
        seasons = {1: 'winter', 2: 'winter', 3: 'spring', 4: 'spring', 5: 'spring',
                   6: 'summer', 7: 'summer', 8: 'summer', 9: 'autumn', 10: 'autumn',
                   11: 'autumn', 12: 'winter'}
        for month, expected in seasons.items():
            self.assertEqual(season_for_month(month), expected, '月份 %d' % month)
        # 越界月份落 winter（上游用 `< 3` / `< 6` 的连续区间判定，其余即 winter）
        self.assertEqual(season_for_month(0), 'winter')
        self.assertEqual(season_for_month(13), 'winter')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
