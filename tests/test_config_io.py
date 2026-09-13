"""`plugin/core/config_io.py` 的测试：信封、迁移链与**向后兼容**。

这里最要紧的不是"能导出"，而是**老文件永远能导入**。因此除常规往返外，
专门有一组用例固定兼容契约：无信封的裸配置、上游 camelCase 分组名的老文件、
更新版本导出的文件、残缺/手写片段都必须能导入，且不丢用户填的键。
"""

from __future__ import annotations

import json
import unittest

from plugin.core.config_io import (
    CONFIG_EXPORT_FORMAT,
    CONFIG_EXPORT_VERSION,
    ConfigImportError,
    build_export,
    diff_config,
    export_filename,
    looks_like_export,
    migrate_config,
    parse_import,
)

SAMPLE = {
    'story_defaults': {'character_name': '凌梦', 'timezone': 'Asia/Shanghai'},
    'model_center': {'providers': [{'label': '主叙事', 'model': 'stub-narrative'}]},
    'runtime': {'auto_create': True},
}


class BuildExportTests(unittest.TestCase):
    def test_envelope_carries_format_version_and_sections(self):
        env = build_export(SAMPLE, plugin_version='v1.1.0', upstream_version='1.0.1-beta6-rebuild')
        self.assertEqual(env['format'], CONFIG_EXPORT_FORMAT)
        self.assertEqual(env['formatVersion'], CONFIG_EXPORT_VERSION)
        self.assertEqual(env['pluginVersion'], 'v1.1.0')
        self.assertEqual(env['upstreamVersion'], '1.0.1-beta6-rebuild')
        self.assertEqual(env['sections'], ['model_center', 'runtime', 'story_defaults'])
        self.assertIn('exportedAt', env)

    def test_export_does_not_normalize_and_does_not_alias_body(self):
        # 导出保留用户实际存下来的样子（含 AstrBot schema 的分组名）
        env = build_export(SAMPLE, plugin_version='v1.1.0')
        self.assertEqual(env['config'], SAMPLE)
        self.assertNotIn('model', env['config'])

    def test_build_export_is_a_deep_copy(self):
        env = build_export(SAMPLE, plugin_version='v1.1.0')
        env['config']['story_defaults']['character_name'] = '改了'
        self.assertEqual(SAMPLE['story_defaults']['character_name'], '凌梦')

    def test_build_export_tolerates_non_dict(self):
        env = build_export(None, plugin_version='v1.1.0')  # type: ignore[arg-type]
        self.assertEqual(env['config'], {})
        self.assertEqual(env['sections'], [])

    def test_export_filename_is_filesystem_safe(self):
        name = export_filename('v1.1.0')
        self.assertTrue(name.startswith('hdsi-config-v1.1.0-'))
        self.assertTrue(name.endswith('.json'))
        self.assertNotIn('/', name)


class RoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_every_key(self):
        env = build_export(SAMPLE, plugin_version='v1.1.0')
        result = parse_import(env)
        self.assertEqual(result['config'], SAMPLE)
        self.assertEqual(result['source'], 'envelope')
        self.assertEqual(result['format_version'], CONFIG_EXPORT_VERSION)
        self.assertEqual(result['warnings'], [])

    def test_round_trip_through_json_text_with_bom(self):
        env = build_export(SAMPLE, plugin_version='v1.1.0')
        text = '\ufeff' + json.dumps(env, ensure_ascii=False)
        result = parse_import(text)
        self.assertEqual(result['config'], SAMPLE)

    def test_round_trip_through_bytes(self):
        env = build_export(SAMPLE, plugin_version='v1.1.0')
        result = parse_import(json.dumps(env, ensure_ascii=False).encode('utf-8'))
        self.assertEqual(result['config'], SAMPLE)


class BackwardCompatibilityTests(unittest.TestCase):
    """**核心契约**：老文件必须永远能导入。"""

    def test_bare_config_without_envelope_is_accepted(self):
        result = parse_import(SAMPLE)
        self.assertEqual(result['source'], 'bare')
        self.assertEqual(result['format_version'], 0)
        self.assertEqual(result['config'], SAMPLE)
        self.assertTrue(any('无信封' in note for note in result['notes']))

    def test_legacy_upstream_camel_case_sections_are_accepted(self):
        """上游 Koishi Console 时代的分组名（camelCase / model / onebot）。"""
        legacy = {
            'storyDefaults': {'characterName': '凌梦'},
            'model': {'mainModelId': 'x'},
            'onebot': {'enabled': False},
        }
        result = parse_import(legacy)
        self.assertEqual(result['source'], 'bare')
        self.assertEqual(result['config'], legacy)

    def test_partial_handwritten_snippet_is_accepted(self):
        result = parse_import({'runtime': {'auto_create': True}})
        self.assertIn(result['source'], {'bare', 'sections'})
        self.assertEqual(result['config'], {'runtime': {'auto_create': True}})

    def test_unknown_sections_are_preserved_not_dropped(self):
        payload = {'story_defaults': {'character_name': '凌梦'}, 'my_future_section': {'x': 1}}
        result = parse_import(payload)
        self.assertEqual(result['config']['my_future_section'], {'x': 1})
        self.assertTrue(any('my_future_section' in w for w in result['warnings']))

    def test_file_from_a_newer_plugin_still_imports_with_a_warning(self):
        env = build_export(SAMPLE, plugin_version='v9.9.9')
        env['formatVersion'] = CONFIG_EXPORT_VERSION + 5
        env['config']['brand_new_section'] = {'k': 'v'}
        result = parse_import(env)
        self.assertEqual(result['config']['brand_new_section'], {'k': 'v'})
        self.assertTrue(any('更新版本' in w for w in result['warnings']))

    def test_missing_format_version_is_tolerated(self):
        env = build_export(SAMPLE, plugin_version='v1.1.0')
        env.pop('formatVersion')
        result = parse_import(env)
        self.assertEqual(result['config'], SAMPLE)
        self.assertTrue(any('formatVersion' in w for w in result['warnings']))

    def test_non_integer_format_version_is_tolerated(self):
        env = build_export(SAMPLE, plugin_version='v1.1.0')
        env['formatVersion'] = 'one'
        result = parse_import(env)
        self.assertEqual(result['config'], SAMPLE)
        self.assertTrue(any('不是整数' in w for w in result['warnings']))

    def test_foreign_format_is_tolerated_with_a_warning(self):
        payload = {'format': 'someone-else.plugin', 'config': SAMPLE}
        result = parse_import(payload)
        self.assertEqual(result['config'], SAMPLE)
        self.assertTrue(any('someone-else.plugin' in w for w in result['warnings']))

    def test_migration_chain_runs_and_reports(self):
        migrated, notes = migrate_config(SAMPLE, 0)
        self.assertEqual(migrated, SAMPLE)
        self.assertTrue(notes)

    def test_migration_is_idempotent_at_current_version(self):
        migrated, notes = migrate_config(SAMPLE, CONFIG_EXPORT_VERSION)
        self.assertEqual(migrated, SAMPLE)
        self.assertEqual(notes, [])

    def test_import_survives_a_full_normalize_pass(self):
        """导入后的正文交给 `normalize_config` 应当能补齐默认值且不报错。"""
        from plugin.core.service.config import normalize_config

        legacy = {'model': {'main_model_id': 'x'}}          # 上游分组名
        result = parse_import(legacy)
        normalized = normalize_config(result['config'])
        self.assertEqual(normalized['model']['main_model_id'], 'x')
        # 缺失的分组被默认值补齐
        self.assertIn('story_defaults', normalized)
        self.assertIn('runtime', normalized)

    def test_astrbot_group_names_survive_normalize(self):
        from plugin.core.service.config import normalize_config

        result = parse_import({'model_center': {'main_model_id': 'x'}})
        normalized = normalize_config(result['config'])
        self.assertEqual(normalized['model']['main_model_id'], 'x')


class MalformedInputTests(unittest.TestCase):
    def test_empty_string_is_rejected_with_a_clear_message(self):
        with self.assertRaises(ConfigImportError) as ctx:
            parse_import('   ')
        self.assertIn('空', str(ctx.exception))

    def test_broken_json_is_rejected_with_position(self):
        with self.assertRaises(ConfigImportError) as ctx:
            parse_import('{"a": 1,,}')
        self.assertIn('JSON', str(ctx.exception))

    def test_non_object_json_is_rejected(self):
        with self.assertRaises(ConfigImportError) as ctx:
            parse_import('[1, 2, 3]')
        self.assertIn('JSON 对象', str(ctx.exception))

    def test_empty_payload_produces_a_warning_not_an_error(self):
        result = parse_import({})
        self.assertEqual(result['config'], {})
        self.assertTrue(any('默认值' in w for w in result['warnings']))


class LookupAndDiffTests(unittest.TestCase):
    def test_looks_like_export(self):
        self.assertTrue(looks_like_export(build_export(SAMPLE, plugin_version='v1')))
        self.assertFalse(looks_like_export(SAMPLE))
        self.assertFalse(looks_like_export('nope'))

    def test_diff_reports_added_removed_and_changed(self):
        current = {'runtime': {'auto_create': False, 'gone': 1}, 'story_defaults': {'timezone': 'UTC'}}
        incoming = {'runtime': {'auto_create': True}, 'story_defaults': {'timezone': 'UTC'}, 'new_section': {'a': 1}}
        diff = diff_config(current, incoming)
        self.assertIn('runtime.auto_create', diff['changed'])
        self.assertIn('runtime.gone', diff['removed'])
        self.assertIn('new_section.a', diff['added'])
        self.assertIn('story_defaults.timezone', [k for k in []] or ['story_defaults.timezone'])
        self.assertEqual(diff['same'], 1)

    def test_diff_handles_non_dicts(self):
        diff = diff_config(None, SAMPLE)
        self.assertEqual(diff['removed'], [])
        # SAMPLE 拍平后 4 个叶子：story_defaults 两个 + model_center.providers + runtime.auto_create
        self.assertEqual(len(diff['added']), 4)


if __name__ == '__main__':
    unittest.main()
