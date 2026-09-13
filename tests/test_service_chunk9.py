"""`plugin/core/service/chunk9.py`（`upstream/src/service.ts:6714-7084`）的单元测试。

来源分三类：

1. **`upstream/test/logging.test.ts` 里属于 service 日志通道的断言**：上游那些用例
   直接调 `formatLayeredLog`；本移植版的等价物是**service 的日志通道**
   （`write_report` / `write_standalone` → `emit_log`），因此这里用同样的输入调
   `ServiceChunk9.write_report(...)`，断言渲染出来的正文与上游逐字一致。
2. **`upstream/test/configuration.test.ts` 的盲区模式断言**：Schema 默认值与
   `resolveBlindModeConfig` 由 `test_configuration.py` 覆盖；本文件补的是
   **service 层**可观测的那一半 —— 盲区模式的先手闸门与唯一一条健康心跳。
3. **可独立验证的纯逻辑用例**：`developmentForPrompt` 的筛选/打分/截断、
   `resolveCompactionFacts` 的白名单结算、`markContinuityDirty` 的幂等、
   `getStory` 的迁移提示与"只报告一次"、`repairCanonicalOneBotStoryTransport`
   的在线账号判定、`retryDbWrite` 的有界重试。

运行：`python3 -m unittest plugin.tests.test_service_chunk9 -v`
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import unittest
from typing import Any
from unittest import mock

from plugin.core import logging as interlude_logging
from plugin.core.database import Database
from plugin.core.service import InterludeContext
from plugin.core.service import chunk9 as chunk9_module
from plugin.core.service.base import Config as _Config  # noqa: F401  (导入即证明 base 可用)
from plugin.core.service.base import ServiceBase
from plugin.core.service.chunk9 import ServiceChunk9
from plugin.core.story_state import decode_story_state
from plugin.core.time import parse_dt, utc_now

#: `reportTokenUsage` 依赖 `narrator.format_token_usage_line`（惰性导入）。
_NARRATOR_AVAILABLE = importlib.util.find_spec('plugin.core.narrator') is not None


# =========================================================================== #
# 夹具
# =========================================================================== #

def make_config(**overrides: Any) -> dict[str, Any]:
    """最小可用配置：分层日志、debug 级别、diagnostic 详细度、无色。"""
    config: dict[str, Any] = {
        'logging': {'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered', 'colors': False},
        'storyDefaults': {},
    }
    config.update(overrides)
    return config


class _Sink:
    """把分层日志收进内存，避免测试输出噪音。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.records.append((level, text))

    def clear(self) -> None:
        self.records.clear()

    def texts(self) -> list[str]:
        return [text for _level, text in self.records]

    def joined(self) -> str:
        return '\n'.join(self.texts())


class _RecordingLogger:
    """`service_logger` 的等价物：证明 `emit_log` 优先走宿主 logger。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def _record(self, level: str, message: str) -> None:
        if self.fail:
            raise RuntimeError('logger 挂了')
        self.calls.append((level, message))

    def error(self, message: str) -> None:
        self._record('error', message)

    def warning(self, message: str) -> None:
        self._record('warn', message)

    def info(self, message: str) -> None:
        self._record('info', message)

    def debug(self, message: str) -> None:
        self._record('debug', message)


class ServiceChunk9Fixture:
    """共享夹具：内存数据库 + 内存日志 sink + 一个 `ServiceChunk9`。

    刻意不继承 `unittest.TestCase`，好同时混入同步与异步基类
    （`IsolatedAsyncioTestCase` 必须排在 MRO 靠前）。
    """

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()

    def make_service(
        self,
        config: dict[str, Any] | None = None,
        *,
        bots: Any = None,
        logger: Any = None,
    ) -> ServiceChunk9:
        ctx = InterludeContext(logger=logger, database=self.db, bots=bots)
        return ServiceChunk9(ctx, config or make_config(), self.db)

    # ---- 数据行 ----

    @staticmethod
    def story_row(story_id: str = 'story-1', **overrides: Any) -> dict[str, Any]:
        now = utc_now()
        row: dict[str, Any] = {
            'id': story_id,
            'platform': 'onebot',
            'selfId': '10001',
            'userId': '20002',
            'channelId': 'private:20002',
            'status': 'active',
            'setting': {
                'character': {'name': '水濑', 'profile': 'p'},
                'user': {'name': '主人'},
                'timezone': 'Asia/Shanghai',
                'perspective': '',
                'world': 'w',
            },
            'state': {},
            'cursorAt': now,
            'createdAt': now,
            'updatedAt': now,
        }
        row.update(overrides)
        return row

    def insert_story(self, **overrides: Any) -> dict[str, Any]:
        row = self.story_row(**overrides)
        self.db.insert('interlude_story', row)
        return row

    def insert_fact(self, fact_id: int, **overrides: Any) -> dict[str, Any]:
        now = utc_now()
        row: dict[str, Any] = {
            'id': fact_id,
            'storyId': 'story-1',
            'participantId': '',
            'scope': 'global',
            'content': 'fact %d' % fact_id,
            'importance': 0.5,
            'confidence': 0.5,
            'unresolved': True,
            'embedding': [],
            'status': 'active',
            'sourceEntryIds': [],
            'lastSeenAt': now,
            'createdAt': now,
            'updatedAt': now,
        }
        row.update(overrides)
        self.db.insert('interlude_fact', row)
        return row

    def insert_patch(self, patch_id: int, **overrides: Any) -> dict[str, Any]:
        now = utc_now()
        row: dict[str, Any] = {
            'id': patch_id,
            'storyId': 'story-1',
            'participantId': '',
            'target': 'character',
            'path': 'development.traits',
            'proposedValue': '她喜欢在深夜煮咖啡',
            'evidence': 'e',
            'confidence': 0.6,
            'impact': 'minor',
            'status': 'applied',
            'sourceEntryIds': [11],
            'createdAt': now,
            'appliedAt': now,
        }
        row.update(overrides)
        self.db.insert('interlude_state_patch', row)
        return row

    def fact_row(self, fact_id: int) -> dict[str, Any] | None:
        return self.db.get('interlude_fact', {'id': fact_id})


class ServiceChunk9TestCase(ServiceChunk9Fixture, unittest.TestCase):
    """同步测试基类。"""


class AsyncServiceChunk9TestCase(unittest.IsolatedAsyncioTestCase, ServiceChunk9Fixture):
    """异步测试基类（`IsolatedAsyncioTestCase` 必须排在 MRO 前面）。"""

    def setUp(self) -> None:
        ServiceChunk9Fixture.setUp(self)


# =========================================================================== #
# 1. 上游 logging.test.ts：service 日志通道
# =========================================================================== #

class ServiceLogChannelTests(ServiceChunk9TestCase):
    """`upstream/test/logging.test.ts` 的断言逐条落在 service 日志通道上。

    上游直接调 `formatLayeredLog`；本移植版的等价路径是
    `writeReport` / `writeStandalone` → `emitLog` → sink，故这里断言 sink 收到的正文。
    """

    def setUp(self) -> None:
        super().setUp()
        self.service = self.make_service()
        self.story = self.story_row()

    def report(self, level: str, message: str, *args: Any) -> str:
        self.service.write_report(level, self.story, 'user-message', message, args)
        self.assertTrue(self.sink.records, '日志通道没有输出')
        return self.sink.records[-1][1]

    def standalone(self, level: str, message: str, *args: Any) -> str:
        self.service.write_standalone(level, message, args)
        self.assertTrue(self.sink.records, '日志通道没有输出')
        return self.sink.records[-1][1]

    def test_user_message_logs_read_as_a_compact_task_timeline(self) -> None:
        received = self.report('info', '收到参与者私聊消息 参与者=%s', '1319973221')
        self.assertEqual(received, '\n'.join([
            '[用户消息] 水濑 (*^▽^*) 收到参与者私聊消息',
            '└─ 参与者: 1319973221',
        ]))

        started = self.report('info', '模型调用开始 任务=主叙事 模型=%s', 'Narrative')
        self.assertEqual(started, '\n'.join([
            '├─ (•̀ᴗ•́)و 模型调用开始',
            '   ├─ 任务: 主叙事',
            '   └─ 模型: Narrative',
        ]))

    def test_alter_and_error_records_use_stable_semantic_markers(self) -> None:
        alter = self.report('info', 'Alter 累积触发 数值=%s 阈值=%s', '+12.5', '10.3')
        self.assertRegex(alter, re.compile(r'^\[情绪追踪\] 水濑 \(๑•̀ㅂ•́\)و✧ Alter 累积触发', re.MULTILINE))
        self.assertRegex(alter, re.compile(r'数值: \+12\.5'))
        self.assertRegex(alter, re.compile(r'阈值: 10\.3'))

        failure = self.report('warn', '模型调用失败 任务=主叙事 错误=%s', '返回无效 JSON')
        self.assertRegex(failure, re.compile(r'^\[用户消息\] 水濑 \(˶ˊᜊˋ˶\) 模型调用失败', re.MULTILINE))
        self.assertRegex(failure, re.compile(r'错误: 返回无效 JSON'))

    def test_colors_can_be_enabled_and_kaomoji_replaced_by_simple_symbols(self) -> None:
        colored_service = self.make_service(make_config(
            logging={'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered', 'colors': True},
        ))
        self.sink.clear()
        colored_service.write_report('info', self.story, 'intent-due', '消息投递开始 参与者=1319973221', ())
        colored = self.sink.records[-1][1]
        self.assertRegex(colored, re.compile(r'\u001b\[[0-9]+m'))
        self.assertIn('(・ω・)ノ', colored)

        self.sink.clear()
        symbols_service = self.make_service(make_config(
            logging={
                'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered',
                'colors': False, 'kaomoji': False,
            },
        ))
        symbols_service.write_standalone('warn', '叙事模型请求失败，已安排自动重试 第2/6次', ())
        symbols = self.sink.records[-1][1]
        self.assertRegex(symbols, re.compile(r'^\[自动重试\] HDSI ↻'))

    def test_dark_and_light_themes_use_separate_high_contrast_palettes(self) -> None:
        def render(theme: str) -> str:
            service = self.make_service(make_config(
                logging={
                    'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered',
                    'colors': True, 'colorTheme': theme,
                },
            ))
            self.sink.clear()
            service.write_report('info', self.story, 'user-message', '收到参与者私聊消息 参与者=1319973221', ())
            return self.sink.records[-1][1]

        dark = render('dark')
        light = render('light')
        self.assertRegex(dark, re.compile(r'\u001b\[38;5;81m\[用户消息\]'))
        self.assertRegex(light, re.compile(r'\u001b\[38;5;25m\[用户消息\]'))
        self.assertNotEqual(dark, light)

    def test_agency_decisions_have_a_distinct_subjectivity_marker(self) -> None:
        output = self.report(
            'info',
            'Agency 主动联系判断 参与者=friend 结果=稍后重查 原因=schedule-occupied 意愿=0.80',
        )
        # 用 logging 表里的权威字形断言，避免测试文件里的 Unicode 抄写错误。
        self.assertIn(interlude_logging._KAOMOJI['agency'], output)

    def test_log_channel_delivers_exactly_one_record(self) -> None:
        """本移植版的关键不变量：渲染与投递只有一个出口（上游同样只有一条）。

        `base.py` 的早期实现走 `log_layered()`（既渲染又投递 sink）再走
        `emit_log`，会输出两条；`chunk9` 用纯渲染 `format_layered_log` 覆盖了它。
        """
        self.report('info', '收到参与者私聊消息 参与者=%s', '1319973221')
        self.assertEqual(len(self.sink.records), 1)

    def test_standalone_records_use_the_hdsi_protagonist(self) -> None:
        output = self.standalone('info', '服务已就绪')
        self.assertIn('HDSI', output)
        self.assertIn('服务已就绪', output)


# =========================================================================== #
# 2. 上游 configuration.test.ts：盲区模式在 service 层的可观测行为
# =========================================================================== #

class BlindModeChannelTests(ServiceChunk9TestCase):
    """盲区模式先手闸门 + 唯一一条健康心跳（上游 `service.ts:6737/6779/6818`）。"""

    def blind_config(self, **blind: Any) -> dict[str, Any]:
        return make_config(blindMode=blind)

    def test_default_config_is_not_blind(self) -> None:
        service = self.make_service()
        self.assertFalse(service.blind_mode_config.get('enabled'))
        service.write_standalone('error', 'boom')
        self.assertEqual(len(self.sink.records), 1)

    def test_blind_mode_suppresses_report_and_standalone_records(self) -> None:
        service = self.make_service(self.blind_config(enabled=True))
        story = self.story_row()

        service.write_report('error', story, 'advance', '模型调用失败 任务=主叙事 错误=%s', ('x',))
        service.write_report('warn', story, 'advance', '警告')
        service.write_standalone('error', 'standalone 错误')
        service.write_standalone('info', '不该出现的信息')
        self.assertEqual(self.sink.records, [], '盲区模式下不得输出任何正文')
        self.assertTrue(service.blind_mode_health_issue, 'error/warn 必须置健康标记')

    def test_info_and_debug_do_not_raise_the_health_flag(self) -> None:
        service = self.make_service(self.blind_config(enabled=True))
        service.write_standalone('info', '静默')
        service.write_standalone('debug', '静默')
        self.assertFalse(service.blind_mode_health_issue)

    def test_health_heartbeat_is_the_only_record_and_carries_no_details(self) -> None:
        service = self.make_service(self.blind_config(enabled=True))
        service.write_standalone('error', '不该出现的错误细节')
        service.report_blind_mode_health()

        self.assertEqual(self.sink.records, [('info', '[失明模式] 运行状态=需关注 后台任务=未就绪')])
        self.assertFalse(service.blind_mode_health_issue, '心跳后必须清标记')

    def test_health_heartbeat_reports_running_scheduler(self) -> None:
        service = self.make_service(self.blind_config(enabled=True))
        service.background_started = True
        service.report_blind_mode_health()
        self.assertEqual(self.sink.records[-1][1], '[失明模式] 运行状态=正常 后台任务=运行中')

    def test_health_heartbeat_reports_database_reset(self) -> None:
        service = self.make_service(self.blind_config(enabled=True))
        service.database_resetting = True
        service.report_blind_mode_health()
        self.assertEqual(self.sink.records[-1][1], '[失明模式] 运行状态=需关注 后台任务=未就绪')


# =========================================================================== #
# 3. 详细度闸门与格式
# =========================================================================== #

class VerbosityAndFormatTests(ServiceChunk9TestCase):
    """`allowsVerbosity`（`:6827`）与三种 `logging.format`。"""

    def test_allows_verbosity_ranks(self) -> None:
        standard = self.make_service(make_config(logging={'verbosity': 'standard'}))
        self.assertTrue(standard.allows_verbosity('summary'))
        self.assertTrue(standard.allows_verbosity('standard'))
        self.assertFalse(standard.allows_verbosity('diagnostic'))

        summary = self.make_service(make_config(logging={'verbosity': 'summary'}))
        self.assertTrue(summary.allows_verbosity('summary'))
        self.assertFalse(summary.allows_verbosity('standard'))

        diagnostic = self.make_service(make_config(logging={'verbosity': 'diagnostic'}))
        self.assertTrue(diagnostic.allows_verbosity('diagnostic'))

    def test_missing_verbosity_defaults_to_standard(self) -> None:
        service = self.make_service(make_config(logging={'level': 'debug'}))
        self.assertTrue(service.allows_verbosity('standard'))
        self.assertFalse(service.allows_verbosity('diagnostic'))

    def test_report_operation_gate_hides_diagnostic_records(self) -> None:
        service = self.make_service(make_config(logging={'level': 'debug', 'verbosity': 'standard'}))
        story = self.story_row()
        service.report_operation('diagnostic', 'debug', story, 'advance', '内部计数 数量=%d', 3)
        self.assertEqual(self.sink.records, [])
        service.report_operation('standard', 'info', story, 'advance', '调度活动')
        self.assertEqual(len(self.sink.records), 1)

    def test_report_operation_diagnostic_appends_story_id(self) -> None:
        service = self.make_service(make_config(
            logging={'level': 'debug', 'verbosity': 'diagnostic', 'format': 'compact', 'colors': False},
        ))
        service.report('info', self.story_row(), 'advance', '后台推进开始')
        self.assertEqual(self.sink.records[-1][1], '[自动推进] 水濑 后台推进开始 故事=story-1')

    def test_detailed_format_uses_the_event_block(self) -> None:
        service = self.make_service(make_config(
            logging={'level': 'debug', 'verbosity': 'standard', 'format': 'detailed', 'colors': False},
        ))
        service.report('info', self.story_row(), 'advance', '后台推进开始')
        self.assertEqual(self.sink.records[-1][1], '[自动推进] 水濑\n事件：后台推进开始')

    def test_standalone_non_layered_format_prefixed_with_system(self) -> None:
        service = self.make_service(make_config(
            logging={'level': 'debug', 'verbosity': 'standard', 'format': 'compact', 'colors': False},
        ))
        service.report_standalone('info', '服务已就绪 主叙事路由=%s', '已配置')
        self.assertEqual(self.sink.records[-1][1], '[系统] 服务已就绪 主叙事路由=已配置')

    def test_level_filter_silences_lower_priority_records(self) -> None:
        service = self.make_service(make_config(logging={'level': 'warn', 'verbosity': 'diagnostic'}))
        service.report_standalone('info', '不该出现')
        service.report_standalone('debug', '不该出现')
        self.assertEqual(self.sink.records, [])
        service.report_standalone('warn', '警告')
        self.assertEqual(len(self.sink.records), 1)


class EmitLogTests(ServiceChunk9TestCase):
    """`emitLog`（`:6811`）：宿主 logger 优先，sink 兜底，异常绝不冒泡。"""

    def test_service_logger_takes_priority_over_sink(self) -> None:
        logger = _RecordingLogger()
        service = self.make_service(logger=logger)
        service.report_standalone('warn', '警告')
        self.assertEqual(len(logger.calls), 1)
        self.assertEqual(logger.calls[0][0], 'warn')
        self.assertIn('警告', logger.calls[0][1])
        self.assertEqual(self.sink.records, [], '有 logger 时不再重复投 sink')

    def test_logger_failure_falls_back_to_sink(self) -> None:
        logger = _RecordingLogger(fail=True)
        service = self.make_service(logger=logger)
        service.report_standalone('error', '出事了 错误=%s', 'boom')
        self.assertEqual(len(self.sink.records), 1)
        self.assertIn('出事了', self.sink.records[-1][1])
        self.assertIn('错误: boom', self.sink.records[-1][1])

    def test_sink_failure_never_raises(self) -> None:
        def exploding_sink(_level: str, _text: str) -> None:
            raise RuntimeError('sink 挂了')

        interlude_logging.set_log_sink(exploding_sink)
        service = self.make_service()
        service.report_standalone('info', '无关紧要')
        # 没有抛出即通过。

    def test_level_maps_to_logger_method(self) -> None:
        logger = _RecordingLogger()
        service = self.make_service(logger=logger)
        service.emit_log('error', 'e')
        service.emit_log('warn', 'w')
        service.emit_log('info', 'i')
        service.emit_log('debug', 'd')
        self.assertEqual([level for level, _text in logger.calls], ['error', 'warn', 'info', 'debug'])


# =========================================================================== #
# 4. developmentForPrompt（`:6714`）
# =========================================================================== #

class DevelopmentForPromptTests(AsyncServiceChunk9TestCase):
    """上游 `developmentForPrompt`：路径/参与者/证据三重筛选 + 字面打分取前 2。"""

    async def test_blank_query_returns_empty(self) -> None:
        service = self.make_service()
        self.insert_patch(1)
        self.assertEqual(await service.development_for_prompt('story-1', None, '   '), [])
        self.assertEqual(await service.development_for_prompt('story-1', None, ''), [])

    async def test_filters_path_participant_and_evidence(self) -> None:
        service = self.make_service()
        self.insert_patch(1, path='development.traits', proposedValue='她喜欢在深夜煮咖啡')
        self.insert_patch(2, path='relationship.trust', proposedValue='她喜欢在深夜煮咖啡')
        self.insert_patch(3, path='development.traits', proposedValue='她喜欢在深夜煮咖啡', participantId='other')
        self.insert_patch(4, path='development.traits', proposedValue='她喜欢在深夜煮咖啡', sourceEntryIds=[])
        self.insert_patch(5, path='development.traits', proposedValue='她喜欢在深夜煮咖啡', status='proposed')

        rows = await service.development_for_prompt('story-1', 'me', '深夜煮咖啡')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['sourceEntryIds'], [11])

    async def test_global_patch_matches_any_participant(self) -> None:
        service = self.make_service()
        self.insert_patch(1, participantId='', proposedValue='她喜欢在深夜煮咖啡')
        rows = await service.development_for_prompt('story-1', 'someone', '深夜煮咖啡')
        self.assertEqual(len(rows), 1)

    async def test_unrelated_values_are_dropped_by_the_012_threshold(self) -> None:
        service = self.make_service()
        self.insert_patch(1, proposedValue='completely unrelated english words here')
        self.assertEqual(await service.development_for_prompt('story-1', None, '深夜煮咖啡'), [])

    async def test_sorts_by_score_and_keeps_the_top_two(self) -> None:
        service = self.make_service()
        # 打分：'深夜煮咖啡' → 1.0；'煮咖啡' → 0.5；'咖啡' → 0.25。
        self.insert_patch(1, proposedValue='深夜煮咖啡')
        self.insert_patch(2, proposedValue='咖啡')
        self.insert_patch(3, proposedValue='煮咖啡')
        rows = await service.development_for_prompt('story-1', None, '深夜煮咖啡')
        self.assertEqual([row['tendency'] for row in rows], ['深夜煮咖啡', '煮咖啡'])

    async def test_output_keys_stay_upstream_camel_case(self) -> None:
        """返回值直接进提示词 payload（`developmentTendencies`），键名一字不改。"""
        service = self.make_service()
        self.insert_patch(1, proposedValue='她喜欢在深夜煮咖啡')
        rows = await service.development_for_prompt('story-1', None, '深夜煮咖啡')
        self.assertEqual(set(rows[0]), {'target', 'tendency', 'sourceEntryIds'})

    async def test_tendency_is_clipped_to_300_characters(self) -> None:
        service = self.make_service()
        self.insert_patch(1, proposedValue='深夜煮咖啡' * 100)
        rows = await service.development_for_prompt('story-1', None, '深夜煮咖啡')
        self.assertEqual(len(rows[0]['tendency']), 300)


# =========================================================================== #
# 5. resolveCompactionFacts / markContinuityDirty（`:6792` / `:6804`）
# ===========================================================================

class ResolveCompactionFactsTests(AsyncServiceChunk9TestCase):
    """只有**交付给压缩器**且仍 `unresolved` 的事实才被结算。"""

    async def test_settles_only_whitelisted_unresolved_facts(self) -> None:
        service = self.make_service()
        self.insert_fact(1)
        self.insert_fact(2)
        self.insert_fact(3, unresolved=False)
        now = utc_now()

        changed = await service.resolve_compaction_facts('story-1', [1, 2, 3, 99], {1, 2, 3}, now)
        self.assertTrue(changed)
        self.assertFalse(self.fact_row(1)['unresolved'])
        self.assertFalse(self.fact_row(2)['unresolved'])
        self.assertFalse(self.fact_row(3)['unresolved'])

    async def test_ignores_ids_outside_the_allowed_set(self) -> None:
        service = self.make_service()
        self.insert_fact(1)
        self.insert_fact(2)
        self.assertTrue(await service.resolve_compaction_facts('story-1', [1, 2], {1}, utc_now()))
        self.assertFalse(self.fact_row(1)['unresolved'], '白名单内的 1 应被结算')
        self.assertTrue(self.fact_row(2)['unresolved'], '白名单外的 2 不得被动')

    async def test_is_idempotent(self) -> None:
        service = self.make_service()
        self.insert_fact(1)
        self.assertTrue(await service.resolve_compaction_facts('story-1', [1], {1}, utc_now()))
        self.assertFalse(await service.resolve_compaction_facts('story-1', [1], {1}, utc_now()))

    async def test_non_numeric_and_unsafe_ids_are_ignored(self) -> None:
        service = self.make_service()
        self.insert_fact(1)
        bogus = [None, True, '1', 1.0, 0, -3, 2 ** 53, [1], {'id': 1}]
        self.assertFalse(await service.resolve_compaction_facts('story-1', bogus, {1}, utc_now()))
        self.assertTrue(self.fact_row(1)['unresolved'])

    async def test_duplicate_ids_are_collapsed_and_capped_at_twenty(self) -> None:
        service = self.make_service()
        for fact_id in range(1, 26):
            self.insert_fact(fact_id)
        ids = list(range(1, 26)) + list(range(1, 26))
        allowed = set(range(1, 26))
        self.assertTrue(await service.resolve_compaction_facts('story-1', ids, allowed, utc_now()))
        settled = [fact_id for fact_id in range(1, 26) if not self.fact_row(fact_id)['unresolved']]
        self.assertEqual(settled, list(range(1, 21)), '上游 slice(0, 20) 只结算前 20 个')

    async def test_non_list_value_returns_false(self) -> None:
        service = self.make_service()
        self.insert_fact(1)
        for value in (None, '1', 1, {'id': 1}):
            self.assertFalse(await service.resolve_compaction_facts('story-1', value, {1}, utc_now()))

    async def test_other_stories_are_untouched(self) -> None:
        service = self.make_service()
        self.insert_fact(1)
        self.insert_fact(2, storyId='story-2')
        self.assertFalse(await service.resolve_compaction_facts('story-1', [2], {2}, utc_now()))
        self.assertTrue(self.fact_row(2)['unresolved'])


class MarkContinuityDirtyTests(AsyncServiceChunk9TestCase):
    """`markContinuityDirty`：已经脏了就不再写库。"""

    async def test_sets_the_flag_and_timestamp(self) -> None:
        service = self.make_service()
        self.insert_story(state={})
        now = utc_now()
        await service.mark_continuity_dirty('story-1', now)
        row = self.db.get('interlude_story', {'id': 'story-1'})
        self.assertTrue(decode_story_state(row['state'])['continuity_dirty'])
        self.assertEqual(int(parse_dt(row['updatedAt']).timestamp() * 1000), int(now.timestamp() * 1000))

    async def test_is_idempotent(self) -> None:
        service = self.make_service()
        self.insert_story(state={})
        first = utc_now()
        await service.mark_continuity_dirty('story-1', first)
        await service.mark_continuity_dirty('story-1', utc_now())
        row = self.db.get('interlude_story', {'id': 'story-1'})
        self.assertEqual(
            int(parse_dt(row['updatedAt']).timestamp() * 1000),
            int(first.timestamp() * 1000),
            '第二次调用不得再写库',
        )

    async def test_preserves_existing_state(self) -> None:
        service = self.make_service()
        self.insert_story(state={'narrativeUpdateCount': 7})
        await service.mark_continuity_dirty('story-1', utc_now())
        row = self.db.get('interlude_story', {'id': 'story-1'})
        state = decode_story_state(row['state'])
        self.assertTrue(state['continuity_dirty'])
        self.assertEqual(state['narrative_update_count'], 7)


# =========================================================================== #
# 6. getStory / repairCanonicalOneBotStoryTransport（`:6833` / `:6903`）
# ===========================================================================

class GetStoryTests(AsyncServiceChunk9TestCase):
    """`getStory`：缺失即抛错；迁移诊断每个故事只报一次。"""

    async def test_missing_story_raises(self) -> None:
        service = self.make_service()
        with self.assertRaises(RuntimeError) as caught:
            await service.get_story('nope')
        self.assertIn('Interlude story not found: nope', str(caught.exception))

    async def test_reports_perspective_migration_once(self) -> None:
        config = make_config(
            storyDefaults={'perspective': '第三人称限知'},
            logging={'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered', 'colors': False},
        )
        service = self.make_service(config)
        self.insert_story()
        await service.get_story('story-1')
        notices = [text for text in self.sink.texts() if '状态迁移提示' in text]
        self.assertEqual(len(notices), 1)
        await service.get_story('story-1')
        self.assertEqual(len([text for text in self.sink.texts() if '状态迁移提示' in text]), 1)

    async def test_no_notice_when_perspective_is_persisted(self) -> None:
        service = self.make_service(make_config(storyDefaults={'perspective': '第三人称限知'}))
        row = self.story_row()
        row['setting'] = dict(row['setting'], perspective='第一人称')
        self.db.insert('interlude_story', row)
        await service.get_story('story-1')
        self.assertEqual([text for text in self.sink.texts() if '状态迁移提示' in text], [])

    async def test_unknown_state_fields_survive_normalization(self) -> None:
        """本移植版的等价保证：上游"保留未知扩展字段"由 story_state 编解码器完成。"""
        service = self.make_service()
        self.insert_story(state={'legacyWeirdKey': 1})
        story = await service.get_story('story-1')
        self.assertEqual(story['state']['extensions'].get('legacyWeirdKey'), 1)


class RepairCanonicalOneBotStoryTransportTests(AsyncServiceChunk9TestCase):
    """`repairCanonicalOneBotStoryTransport`：只修"配置账号已离线"的陈旧主剧本。"""

    def session(self, platform: str = 'onebot', self_id: str = '10001') -> dict[str, Any]:
        return {'platform': platform, 'selfId': self_id, 'userId': '20002'}

    async def test_non_onebot_session_is_ignored(self) -> None:
        service = self.make_service(bots=lambda: [])
        story = self.story_row()
        returned = await service.repair_canonical_one_bot_story_transport(story, self.session('telegram'))
        self.assertIs(returned, story)

    async def test_live_story_bot_keeps_the_story_untouched(self) -> None:
        service = self.make_service(bots=lambda: [{'platform': 'onebot', 'selfId': '10001'}])
        story = self.story_row(platform='onebot', selfId='10001')
        returned = await service.repair_canonical_one_bot_story_transport(story, self.session('onebot', '99999'))
        self.assertIs(returned, story)
        self.assertEqual(self.sink.records, [])

    async def test_matching_endpoint_keeps_the_story_untouched(self) -> None:
        service = self.make_service(bots=lambda: [])
        story = self.story_row(platform='onebot', selfId='10001')
        returned = await service.repair_canonical_one_bot_story_transport(story, self.session('onebot', '10001'))
        self.assertIs(returned, story)
        self.assertEqual(self.sink.records, [])

    async def test_stale_story_is_repaired_and_persisted(self) -> None:
        service = self.make_service(bots=lambda: [])
        self.insert_story(platform='onebot', selfId='10001')
        story = await service.get_story('story-1')
        returned = await service.repair_canonical_one_bot_story_transport(story, self.session('onebot', '88888'))
        self.assertEqual(returned['selfId'], '88888')
        row = self.db.get('interlude_story', {'id': 'story-1'})
        self.assertEqual(row['selfId'], '88888')
        self.assertTrue(any('主剧本投递账号已自愈' in text for text in self.sink.texts()))

    async def test_missing_session_self_id_is_ignored(self) -> None:
        service = self.make_service(bots=lambda: [])
        story = self.story_row()
        returned = await service.repair_canonical_one_bot_story_transport(story, {'platform': 'onebot'})
        self.assertIs(returned, story)


# =========================================================================== #
# 7. 数据库管道：serial / dbWrite / retryDbWrite（`:6851-6937`）
# ===========================================================================

class DatabasePipelineTests(AsyncServiceChunk9TestCase):
    """`serial` 委托 + `dbWrite` 的有界重试。"""

    async def test_serial_runs_same_key_tasks_in_order(self) -> None:
        service = self.make_service()
        order: list[int] = []

        async def task(index: int, delay: float) -> None:
            await asyncio.sleep(delay)
            order.append(index)

        first = service.serial('k', lambda: task(1, 0.03))
        second = service.serial('k', lambda: task(2, 0.0))
        await asyncio.gather(first, second)
        self.assertEqual(order, [1, 2])

    async def test_serial_does_not_block_after_a_failure(self) -> None:
        service = self.make_service()

        async def boom() -> None:
            raise RuntimeError('第一次失败')

        first = service.serial('k', boom)
        with self.assertRaises(RuntimeError):
            await first
        ran = []

        async def ok() -> str:
            ran.append(True)
            return 'ok'

        self.assertEqual(await service.serial('k', ok), 'ok')
        self.assertEqual(ran, [True])

    async def test_db_write_retries_transient_errors_then_succeeds(self) -> None:
        service = self.make_service()
        attempts = {'count': 0}

        async def flaky() -> str:
            attempts['count'] += 1
            if attempts['count'] < 3:
                raise RuntimeError('disk I/O error')
            return 'done'

        with mock.patch.object(chunk9_module, '_WRITE_RETRY_DELAYS_MS', (0, 0, 0, 0, 0, 0, 0)):
            self.assertEqual(await service.db_write(flaky), 'done')
        self.assertEqual(attempts['count'], 3)

    async def test_db_write_stops_after_seven_retries(self) -> None:
        service = self.make_service()
        attempts = {'count': 0}

        async def always_transient() -> None:
            attempts['count'] += 1
            raise RuntimeError('database is locked')

        with mock.patch.object(chunk9_module, '_WRITE_RETRY_DELAYS_MS', (0, 0, 0, 0, 0, 0, 0)):
            with self.assertRaises(RuntimeError):
                await service.db_write(always_transient)
        self.assertEqual(attempts['count'], 8, '首次 + 7 次重试')
        self.assertTrue(any('SQLite 写入连续失败' in text for text in self.sink.texts()))

    async def test_db_write_does_not_retry_non_transient_errors(self) -> None:
        service = self.make_service()
        attempts = {'count': 0}

        async def fatal() -> None:
            attempts['count'] += 1
            raise ValueError('列名拼错了')

        with self.assertRaises(ValueError):
            await service.db_write(fatal)
        self.assertEqual(attempts['count'], 1)

    async def test_db_write_serializes_concurrent_writers(self) -> None:
        service = self.make_service()
        active = {'now': 0, 'peak': 0}

        async def writer() -> None:
            active['now'] += 1
            active['peak'] = max(active['peak'], active['now'])
            await asyncio.sleep(0.01)
            active['now'] -= 1

        await asyncio.gather(*(service.db_write(writer) for _ in range(5)))
        self.assertEqual(active['peak'], 1)

    async def test_db_get_rejects_query_operators(self) -> None:
        """上游 `$in` 一类算子在本移植版必须显式报错，不得静默给偏窄结果。"""
        service = self.make_service()
        with self.assertRaises(NotImplementedError):
            await service.db_get('interlude_fact', {'id': {'$in': [1, 2]}})


# =========================================================================== #
# 8. 成员覆盖与顺序
# =========================================================================== #

class Chunk9SurfaceTests(ServiceChunk9TestCase):
    """`src/service.ts:6714-7084` 的 25 个成员必须都在，且顺序与上游一致。"""

    #: 上游声明顺序（`upstream/src/service.ts`，行号见 chunk9 模块 docstring）。
    UPSTREAM_ORDER = (
        'development_for_prompt', 'report', 'report_operation', 'write_report',
        'report_standalone', 'report_token_usage', 'report_standalone_operation',
        'write_standalone', 'resolve_compaction_facts', 'mark_continuity_dirty',
        'emit_log', 'report_blind_mode_health', 'allows_verbosity', 'get_story',
        'serial', 'db_write', 'db_read', 'db_get',
        'repair_canonical_one_bot_story_transport', 'retry_db_write', 'db_create',
        'find_possibly_committed_create', 'db_set', 'db_remove', 'purge_table',
    )

    def test_every_member_is_defined_on_the_mixin(self) -> None:
        missing = [name for name in self.UPSTREAM_ORDER if name not in ServiceChunk9.__dict__]
        self.assertEqual(missing, [])

    def test_definition_order_matches_upstream(self) -> None:
        defined = [
            name for name in vars(ServiceChunk9)
            if name in self.UPSTREAM_ORDER
        ]
        self.assertEqual(defined, list(self.UPSTREAM_ORDER))

    def test_mixin_extends_service_base(self) -> None:
        self.assertTrue(issubclass(ServiceChunk9, ServiceBase))

    def test_no_astrbot_import(self) -> None:
        with open(chunk9_module.__file__, encoding='utf-8') as handle:
            source = handle.read()
        self.assertNotIn('import astrbot', source)

    @unittest.skipUnless(_NARRATOR_AVAILABLE, 'plugin.core.narrator 尚未落地')
    def test_report_token_usage_emits_one_line(self) -> None:
        service = self.make_service()
        desktop_events: list[tuple[str, Any]] = []
        # `set_desktop_event_sink` 是 Chunk0 的入口；这里直接写字段，只验证
        # `report_token_usage` 真的把它当成桌面事件出口。
        service.desktop_event_sink = lambda event, payload: desktop_events.append((event, payload))

        service.report_token_usage({
            'task': 'narrative', 'model': 'test-model', 'input_tokens': 100, 'output_tokens': 20,
        })
        self.assertEqual(desktop_events[0][0], 'token')
        self.assertEqual(len(self.sink.records), 1)
        # 分层格式会把 `key=value` 轨迹拆成字段行（与上游 `formatLayeredLog` 一致）。
        text = self.sink.records[-1][1]
        self.assertIn('Token 用量[narrative]', text)
        self.assertIn('模型: test-model', text)
        self.assertIn('输入: 100', text)
        self.assertIn('输出: 20', text)

    @unittest.skipUnless(_NARRATOR_AVAILABLE, 'plugin.core.narrator 尚未落地')
    def test_report_token_usage_is_silent_without_usage_fields(self) -> None:
        service = self.make_service()
        service.report_token_usage({'task': 'narrative', 'model': 'test-model'})
        self.assertEqual(self.sink.records, [])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
