"""core 独立单元测试（不依赖 AstrBot SDK）。

运行方式（从任意插件目录的父目录）：
    python -m <插件目录名>.tests.test_core

这些测试只验证叙事核心逻辑本身，可在任何 Python 环境（含 cnb 构建机）运行。
包名与目录名解耦：这里按插件目录的实际名字动态 import，不硬编码。
"""

import importlib
import os
import sys
import tempfile
import unittest

# 把插件根目录的父目录（repo 根）加入 sys.path，便于以插件包结构 import
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

# 自动解析插件目录名（即 Python 包名）
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_core_mod = importlib.import_module(f"{_PLUGIN_DIR}.core")
narrative = _core_mod.narrative
htime = _core_mod.time
_types = importlib.import_module(f"{_PLUGIN_DIR}.core.types")
(
    ContinuitySnapshot,
    NarrativeDecision,
    NarrativeDecisionKind,
    ScriptEntryKind,
    SettingOverlay,
    StorySetting,
) = (
    _types.ContinuitySnapshot,
    _types.NarrativeDecision,
    _types.NarrativeDecisionKind,
    _types.ScriptEntryKind,
    _types.SettingOverlay,
    _types.StorySetting,
)


class TestTime(unittest.TestCase):
    def test_timezone_resolve_valid(self):
        self.assertEqual(htime.resolve_timezone("Asia/Shanghai"), "Asia/Shanghai")

    def test_timezone_resolve_invalid_fallback_utc(self):
        self.assertEqual(htime.resolve_timezone("Not/AZone"), "UTC")

    def test_period_morning(self):
        from datetime import datetime

        ctx = htime.story_local_time_context(datetime(2026, 1, 1, 9, 0, 0), "Asia/Shanghai")
        self.assertEqual(ctx["period"], "morning")
        self.assertEqual(ctx["periodZh"], "上午")

    def test_period_night(self):
        from datetime import datetime

        ctx = htime.story_local_time_context(datetime(2026, 1, 1, 2, 0, 0), "Asia/Shanghai")
        self.assertEqual(ctx["period"], "night")

    def test_utc_present(self):
        from datetime import datetime

        ctx = htime.story_local_time_context(datetime(2026, 1, 1, 12, 0, 0), "UTC")
        self.assertIn("T", ctx["utc"])


class TestSettingOverlay(unittest.TestCase):
    def test_overlay_merge(self):
        base = StorySetting(character_name="Kela", character_profile="cat")
        overlay = SettingOverlay(character_profile="cat-girl")
        merged = overlay.merge(base)
        self.assertEqual(merged.character_name, "Kela")
        self.assertEqual(merged.character_profile, "cat-girl")


class TestNarrativeDecision(unittest.TestCase):
    def test_default_see(self):
        d = NarrativeDecision()
        self.assertEqual(d.decided, NarrativeDecisionKind.see)

    def test_parse_delay(self):
        data = {"decided": "delay", "delayed_reply_seconds": 30}
        parsed = narrative._parse_decision(data, None, 1440)  # type: ignore[arg-type]
        self.assertEqual(parsed.decided, NarrativeDecisionKind.delay)
        self.assertEqual(parsed.delayed_reply_seconds, 30)
        self.assertIsNotNone(parsed.delayed_reply_at)

    def test_parse_unknown_decided_falls_back_see(self):
        parsed = narrative._parse_decision({"decided": "weird"}, None, 1440)  # type: ignore[arg-type]
        self.assertEqual(parsed.decided, NarrativeDecisionKind.see)

    def test_reply_content(self):
        parsed = narrative._parse_decision(
            {"decided": "reply", "visible_reply": "你好"}, None, 1440  # type: ignore[arg-type]
        )
        self.assertEqual(parsed.visible_reply, "你好")


class TestContinuitySnapshot(unittest.TestCase):
    def test_defaults(self):
        snap = ContinuitySnapshot()
        self.assertEqual(snap.current, "")
        self.assertIsInstance(snap.next, list)


class TestBridgeStore(unittest.TestCase):
    def test_json_store_persistence(self):
        _bridge_mod = importlib.import_module(f"{_PLUGIN_DIR}.adapters.astrbot_bridge")
        AstrbotBridge = _bridge_mod.AstrbotBridge

        d = tempfile.mkdtemp()
        conf = {
            "story": {"character_name": "Kela", "timezone": "Asia/Shanghai"},
            "runtime": {"auto_create": True},
        }
        bridge = AstrbotBridge(d, conf)
        story = bridge.ensure_story("qq", "self", "user1", "chan1")
        self.assertIsNotNone(story)
        self.assertEqual(story.setting.character_name, "Kela")

        bridge.append_entry(story, ScriptEntryKind.user_event, "hi")
        bridge.add_facts(["Kela is a cat"])

        # 重新加载验证持久化
        bridge2 = AstrbotBridge(d, {})
        self.assertIsNotNone(bridge2._story)
        self.assertEqual(len(bridge2._entries), 1)
        self.assertEqual(len(bridge2._facts), 1)
        self.assertEqual(bridge2._entries[0].content, "hi")


class TestCompact(unittest.TestCase):
    def test_below_threshold_skips_llm(self):
        """条目数未达阈值时直接返回，不触发 LLM（用会抛异常的 call 验证）。"""
        _bridge_mod = importlib.import_module(f"{_PLUGIN_DIR}.adapters.astrbot_bridge")
        _model = importlib.import_module(f"{_PLUGIN_DIR}.core.model")
        AstrbotBridge = _bridge_mod.AstrbotBridge
        ModelConfig = _model.ModelConfig

        d = tempfile.mkdtemp()
        bridge = AstrbotBridge(d, {"runtime": {"compact_threshold": 18}})
        story = bridge.ensure_story("qq", "self", "u", "c")
        # auto_create 未开则手动注入一个 story 到 bridge
        if story is None:
            _types = importlib.import_module(f"{_PLUGIN_DIR}.core.types")
            story = _types.InterludeStory(id="hds-main", platform="qq", user_id="u")
            bridge._story = story
        # 只加 5 条，低于阈值
        for i in range(5):
            bridge.append_entry(story, ScriptEntryKind.user_event, f"msg {i}")

        async def _bomb_call(*_a, **_k):
            raise AssertionError("不应触发 LLM 调用")

        import asyncio
        asyncio.run(
            bridge.maybe_compact(story, ModelConfig(), threshold_entries=18, astrobot_call=_bomb_call)
        )
        # 未压缩：快照保持 None
        self.assertIsNone(story.state.continuity_snapshot)
        self.assertEqual(len(bridge._facts), 0)


class TestImportPersona(unittest.TestCase):
    def test_import_persona_sets_story(self):
        """从人格 prompt 提取角色名并填充 story。"""
        _bridge_mod = importlib.import_module(f"{_PLUGIN_DIR}.adapters.astrbot_bridge")
        AstrbotBridge = _bridge_mod.AstrbotBridge

        d = tempfile.mkdtemp()
        bridge = AstrbotBridge(d, {})
        info = bridge.import_persona(
            persona_id="cat",
            system_prompt="你是凌梦，一只温柔的白猫娘，傲娇又粘人。生活在现代都市。",
        )
        self.assertEqual(info["character_name"], "凌梦")
        self.assertIsNotNone(bridge._story)
        self.assertEqual(bridge._story.setting.character_name, "凌梦")
        self.assertIn("凌梦", bridge._story.setting.character_profile)

    def test_import_persona_without_name_uses_id(self):
        _bridge_mod = importlib.import_module(f"{_PLUGIN_DIR}.adapters.astrbot_bridge")
        AstrbotBridge = _bridge_mod.AstrbotBridge

        d = tempfile.mkdtemp()
        bridge = AstrbotBridge(d, {})
        info = bridge.import_persona(persona_id="default", system_prompt="你是一个助手。")
        self.assertEqual(info["character_name"], "default")  # 未匹配"你是XX"则回退 persona_id
        self.assertEqual(bridge._story.setting.character_name, "default")


if __name__ == "__main__":
    unittest.main()
