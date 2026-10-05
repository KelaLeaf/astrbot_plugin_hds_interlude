"""`core/logging.py` 单元测试（不依赖 AstrBot SDK）。

移植自上游 `test/logging.test.ts`，逐条对照断言。运行方式（仓库根目录；发布仓布局去掉 `plugin.` 前缀）：

    python3 -m unittest plugin.tests.test_logging -v
"""

import contextlib
import importlib
import io
import os
import sys
import unittest

# 把插件根目录的父目录（repo 根）加入 sys.path，便于以插件包结构 import
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 自动解析插件目录名（即 Python 包名），不硬编码
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging_mod = importlib.import_module(f"{_PLUGIN_DIR}.core.logging")

format_layered_log = logging_mod.format_layered_log
render_log_message = logging_mod.render_log_message
detect_log_action = logging_mod.detect_log_action
phase_label = logging_mod.phase_label
log_layered = logging_mod.log_layered
set_log_sink = logging_mod.set_log_sink


class FormatLayeredLogTests(unittest.TestCase):
    """上游 test/logging.test.ts 的 5 个用例，逐条对照。"""

    def test_user_message_logs_read_as_a_compact_task_timeline(self):
        received = format_layered_log({
            "level": "info", "phase": "user-message", "protagonist": "水濑", "colors": False,
            "message": "收到参与者私聊消息 参与者=%s", "args": ["1319973221"],
        })
        self.assertEqual(received, "\n".join([
            "[用户消息] 水濑 (*^▽^*) 收到参与者私聊消息",
            "└─ 参与者: 1319973221",
        ]))

        started = format_layered_log({
            "level": "info", "phase": "user-message", "protagonist": "水濑", "colors": False,
            "message": "模型调用开始 任务=主叙事 模型=%s", "args": ["Narrative"],
        })
        self.assertEqual(started, "\n".join([
            "├─ (•̀ᴗ•́)و 模型调用开始",
            "   ├─ 任务: 主叙事",
            "   └─ 模型: Narrative",
        ]))

    def test_alter_and_error_records_use_stable_semantic_markers(self):
        alter = format_layered_log({
            "level": "info", "phase": "user-message", "protagonist": "水濑", "colors": False,
            "message": "Alter 累积触发 数值=%s 阈值=%s", "args": ["+12.5", "10.3"],
        })
        self.assertRegex(alter, r"(?m)^\[情绪追踪\] 水濑 \(๑•̀ㅂ•́\)و✧ Alter 累积触发")
        self.assertRegex(alter, r"数值: \+12\.5")
        self.assertRegex(alter, r"阈值: 10\.3")

        failure = format_layered_log({
            "level": "warn", "phase": "user-message", "protagonist": "水濑", "colors": False,
            "message": "模型调用失败 任务=主叙事 错误=%s", "args": ["返回无效 JSON"],
        })
        self.assertRegex(failure, r"(?m)^\[用户消息\] 水濑 \(˶ˊᜊˋ˶\) 模型调用失败")
        self.assertRegex(failure, r"错误: 返回无效 JSON")

    def test_colors_can_be_enabled_and_kaomoji_can_be_replaced_by_simple_symbols(self):
        colored = format_layered_log({
            "level": "info", "phase": "intent-due", "protagonist": "水濑", "colors": True,
            "message": "消息投递开始 参与者=1319973221",
        })
        self.assertRegex(colored, r"\x1b\[[0-9]+m")
        self.assertRegex(colored, r"\(・ω・\)ノ")

        symbols = format_layered_log({
            "level": "warn", "protagonist": "HDSI", "standalone": True, "colors": False, "kaomoji": False,
            "message": "叙事模型请求失败，已安排自动重试 第2/6次",
        })
        self.assertRegex(symbols, r"^\[自动重试\] HDSI ↻")

    def test_dark_and_light_themes_use_separate_high_contrast_256_color_palettes(self):
        base = {
            "level": "info",
            "phase": "user-message",
            "protagonist": "水濑",
            "colors": True,
            "message": "收到参与者私聊消息 参与者=1319973221",
        }
        dark = format_layered_log({**base, "color_theme": "dark"})
        light = format_layered_log({**base, "color_theme": "light"})
        self.assertRegex(dark, r"\x1b\[38;5;81m\[用户消息\]")
        self.assertRegex(light, r"\x1b\[38;5;25m\[用户消息\]")
        self.assertNotEqual(dark, light)

    def test_agency_decisions_have_a_distinct_subjectivity_marker(self):
        output = format_layered_log({
            "level": "info", "phase": "advance", "protagonist": "水濑", "colors": False,
            "message": "Agency 主动联系判断 参与者=friend 结果=稍后重查 原因=schedule-occupied 意愿=0.80",
        })
        self.assertRegex(output, r"ᕙ\( •̀ ᗜ •́ \)ᕗ")

    def test_a_field_value_may_contain_an_equals_sign(self):
        """`key=value` 的值里带 `=` 时照旧渲染成字段。

        回归：`_RE_FIELD` 原用 `[^=]*?` 取值，值里一旦出现 `=`（拒绝原因里的
        `（userId=20002）` 就是），**整条字段连值一起静默消失**——"原因"这类最该看见的
        信息反而永远打不出来。
        """
        output = format_layered_log({
            "level": "warn", "protagonist": "HDSI", "standalone": True, "colors": False,
            "message": "管理命令被拒 层=%s 用户ID=%s 原因=%s",
            "args": [
                "onebot", "20002",
                "「仅处理名单内的用户」开着，而发送者（userId=20002）不在 `user_accounts` 里。",
            ],
        })
        self.assertIn("管理命令被拒", output)
        self.assertIn("层: onebot", output)
        self.assertIn("用户ID: 20002", output)
        self.assertIn("原因: 「仅处理名单内的用户」开着", output)
        self.assertIn("userId=20002", output)  # 值没被 `=` 截断


class RenderLogMessageTests(unittest.TestCase):
    """`render_log_message` 对齐 Node `util.format` 的插值语义。"""

    def test_string_and_extra_arguments(self):
        self.assertEqual(render_log_message("a %s b", ["x"]), "a x b")
        self.assertEqual(render_log_message("x", ["a", "b"]), "x a b")

    def test_placeholders_without_arguments_are_left_untouched(self):
        self.assertEqual(render_log_message("%s %s", ["a"]), "a %s")
        self.assertEqual(render_log_message("%s", []), "%s")

    def test_escaped_percent(self):
        self.assertEqual(render_log_message("100%% %s", ["ok"]), "100% ok")
        self.assertEqual(render_log_message("%%"), "%")

    def test_error_arguments_render_their_message(self):
        self.assertEqual(render_log_message("错误=%s", [ValueError("boom")]), "错误=boom")

    def test_object_and_json_specifiers(self):
        self.assertEqual(render_log_message("%o", ["x"]), "'x'")
        self.assertEqual(render_log_message("%O", [{"a": 1}]), "{'a': 1}")
        self.assertEqual(render_log_message("%j", [{"a": 1}]), '{"a":1}')

    def test_default_args_is_empty(self):
        self.assertEqual(render_log_message("原样"), "原样")


class DetectLogActionTests(unittest.TestCase):
    """动作识别顺序即优先级。"""

    def test_level_error_wins(self):
        self.assertEqual(detect_log_action("一切正常", "error"), "error")

    def test_retry_beats_hard_failure_keywords(self):
        self.assertEqual(detect_log_action("消息投递失败，已安排自动重试", "warn"), "retry")

    def test_hard_failure_keywords(self):
        self.assertEqual(detect_log_action("模型调用失败 任务=主叙事", "info"), "error")

    def test_a_negated_retry_is_not_a_retry(self):
        """"请勿自动重试"是**别重试**，不是要重试。

        真机日志现场：写动作的失败口径里带着这句否定式，`/重试/` 一命中就给整条 warn
        打上 `[自动重试]` 标签——**标签与正文说的是反话**，看日志的人只会被带偏。
        """
        for text in (
            "OneBot 动作 comment_qzone 传输异常（结果未知，请勿自动重试）：socket closed",
            "QQ 空间说说发表结果未知（无 tid），已按保守计入配额且不自动重试",
            "结果未知：请求可能已生效，为避免重复不会自动重试。",
            "禁止自动重试：这条动作不是幂等的",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(detect_log_action(text, "warn"), "retry")

    def test_a_real_retry_mention_still_counts(self):
        """反向用例：真的在重试（"已安排自动重试" / "自动重试已停止"）标签照旧。"""
        for text in (
            "叙事模型请求失败，已安排自动重试 次数=1/3",
            "叙事模型自动重试已停止 已尝试=3",
        ):
            with self.subTest(text=text):
                self.assertEqual(detect_log_action(text, "warn"), "retry")

    def test_warn_level_falls_back_to_warning(self):
        self.assertEqual(detect_log_action("叙事模型响应缓慢", "warn"), "warning")

    def test_remaining_actions(self):
        cases = [
            ("Alter 累积触发 数值=+12.5", "info", "trigger"),
            ("记忆整理完成", "info", "complete"),
            ("情绪偏移生成开始", "info", "emotion"),
            ("Agency 主动联系判断", "info", "agency"),
            ("剧本压缩 条目=18", "info", "memory"),
            ("群聊 收到消息", "info", "group"),
            ("消息投递开始", "info", "send"),
            ("收到参与者私聊消息", "info", "receive"),
            ("模型调用开始 任务=主叙事", "info", "processing"),
            ("后台推进", "info", "advance"),
            ("等待计时器 间隔=30", "info", "waiting"),
            ("完全无关的一句话", "info", "system"),
        ]
        for message, level, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(detect_log_action(message, level), expected)


class PhaseLabelTests(unittest.TestCase):
    def test_known_phases(self):
        self.assertEqual(phase_label("user-message"), "用户消息")
        self.assertEqual(phase_label("conversation-follow-up"), "对话后续")
        self.assertEqual(phase_label("advance"), "自动推进")
        self.assertEqual(phase_label("intent-due"), "到期意图")

    def test_missing_phase(self):
        self.assertEqual(phase_label(), "系统")
        self.assertEqual(phase_label(None), "系统")


class LogCategoryTests(unittest.TestCase):
    """标签口径：默认按文案推断；调用方知道**真实会话类型**时以它为准。

    用户 2026-09-28 贴的日志里，私聊的一条输入状态日志被标成了 `[群聊]`——只因为
    平台错误文案里写着"群聊不支持"。`category` 就是给这种场景准备的确定口径。
    """

    def _render(self, **extra):
        record = {
            "level": "warn", "protagonist": "HDSI", "colors": False,
            "message": "输入状态设置失败 错误=%s", "args": ["群聊不支持"],
            "standalone": True,
        }
        record.update(extra)
        return format_layered_log(record)

    def test_prose_alone_still_drives_the_category_by_default(self):
        self.assertIn("[群聊]", self._render())

    def test_the_caller_can_pin_the_real_session_type(self):
        self.assertIn("[系统]", self._render(category="[系统]"))
        self.assertNotIn("[群聊]", self._render(category="[系统]"))
        self.assertIn("[群聊]", self._render(category="[群聊]"))

    def test_an_empty_category_falls_back_to_prose(self):
        self.assertIn("[群聊]", self._render(category=""))


class LogSinkTests(unittest.TestCase):
    """移植版新增：`ctx.logger` → 可注入 sink。"""

    def tearDown(self):
        set_log_sink(None)

    def test_default_sink_writes_to_stderr(self):
        record = {
            "level": "info", "phase": "user-message", "protagonist": "水濑", "colors": False,
            "message": "收到参与者私聊消息 参与者=1319973221",
        }
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            text = log_layered(record)
        self.assertEqual(buffer.getvalue(), text + "\n")
        self.assertEqual(text, "\n".join([
            "[用户消息] 水濑 (*^▽^*) 收到参与者私聊消息",
            "└─ 参与者: 1319973221",
        ]))

    def test_injected_sink_receives_level_and_text(self):
        captured = []
        set_log_sink(lambda level, text: captured.append((level, text)))
        record = {
            "level": "warn", "protagonist": "HDSI", "standalone": True, "colors": False,
            "message": "叙事模型请求失败，已安排自动重试 第2/6次",
        }
        text = log_layered(record)
        self.assertEqual(captured, [("warn", text)])
        self.assertTrue(text.startswith("[自动重试] HDSI"))

    def test_sink_can_be_restored(self):
        set_log_sink(lambda level, text: None)
        self.assertIsNot(logging_mod.get_log_sink(), logging_mod._default_sink)
        set_log_sink(None)
        self.assertIs(logging_mod.get_log_sink(), logging_mod._default_sink)


if __name__ == "__main__":
    unittest.main()
