"""上游 `test/model-routing.test.ts` 的逐条移植（stdlib `unittest`，零依赖）。

运行：
    cd /home/kela/文档/harness/hds-interlude && python3 -m unittest plugin.tests.test_model_routing -v

对应关系
--------
* 上游 3 条用例 → `UpstreamParityTests`（断言逐条照抄，含 `assert.match` 的正则匹配）。
* `SupplementaryTests` 是**补充用例**：上游测试只覆盖了「显式指派 / 隔离
  embedding / model profile」三条路径，而本模块还有归一化默认值、官方
  endpoint 预设、`disabled` 与 `legacy-fallback`/`task-config` 分支、
  `timeline` 复用 `compaction`、stickers/vision 只认显式指派等行为，
  补上以免回归。
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest

# 把插件根目录的父目录（仓库根）加入 sys.path，便于以插件包结构 import。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 自动解析插件目录名（即 Python 包名），不硬编码。
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

model_routing = importlib.import_module(f"{_PLUGIN_DIR}.core.model_routing")

ZHIPU_OFFICIAL_CHAT_ENDPOINT = model_routing.ZHIPU_OFFICIAL_CHAT_ENDPOINT
resolve_model_routing = model_routing.resolve_model_routing
resolve_model_target = model_routing.resolve_model_target
effective_main_model_id = model_routing.effective_main_model_id
configured_providers = model_routing.configured_providers
uses_remote_providers = model_routing.uses_remote_providers
provider_key = model_routing.provider_key
is_assigned_to = model_routing.is_assigned_to
format_model_routing = model_routing.format_model_routing
preset_endpoint = model_routing.preset_endpoint
normalize_provider = model_routing.normalize_provider
enabled_model_profiles = model_routing.enabled_model_profiles


def provider(provider_id: str, overrides: dict | None = None) -> dict:
    """上游测试的 `provider()` 夹具：一条「看起来正常」的连接。"""
    entry = {
        'id': provider_id, 'label': provider_id, 'enabled': True,
        'endpoint': f'https://{provider_id}.example/v1/chat/completions',
        'api_key': 'secret', 'model': f'{provider_id}-model', 'temperature': 0.8, 'top_p': 1,
        'max_tokens': 4096, 'timeout': 60_000, 'response_format': 'json-object',
        'extra_headers': '', 'extra_body': '',
    }
    entry.update(overrides or {})
    return entry


def config(providers: list[dict]) -> dict:
    """上游测试的 `config()` 夹具。"""
    return {
        'providers': providers,
        'failover': {'enabled': True, 'strategy': 'ordered', 'max_attempts_per_provider': 1, 'cooldown_minutes': 1},
        'fixed_prompt': '', 'style_prompt': '',
    }


class UpstreamParityTests(unittest.TestCase):
    """`upstream/test/model-routing.test.ts` 三条用例的逐条移植。"""

    def test_one_routing_table_resolves_explicit_task_assignments_before_legacy_fallback(self):
        routing = resolve_model_routing(config([
            provider('fallback'),
            provider('writer', {'use_for_main': True}),
            provider('compact', {'use_for_compaction': True}),
        ]))
        self.assertEqual(routing['main']['providers'][0]['id'], 'writer')
        self.assertEqual(routing['main']['reason'], 'assigned-provider')
        self.assertEqual(routing['compaction']['providers'][0]['id'], 'compact')
        self.assertRegex(format_model_routing(routing), r'main=writer/writer-model\[assigned-provider\]')

    def test_embedding_only_providers_never_become_an_accidental_chat_fallback(self):
        routing = resolve_model_routing(config([
            provider('vectors', {'use_for_embedding': True}),
        ]))
        self.assertEqual(routing['main']['available'], False)
        self.assertEqual(routing['main']['reason'], 'unavailable')

    def test_model_profiles_select_provider_and_task_model_through_the_same_resolver(self):
        model_config = config([provider('shared', {'model': ''})])
        model_config['models'] = [{
            'id': 'writer-profile', 'label': 'writer', 'enabled': True, 'provider_id': 'shared', 'model': 'story-model',
            'max_tokens': 8192, 'timeout': 90_000, 'response_format': 'json-object',
        }]
        model_config['main_model_id'] = 'writer-profile'
        routing = resolve_model_routing(model_config)
        self.assertEqual(routing['main']['available'], True)
        self.assertEqual(routing['main']['reason'], 'model-profile')
        self.assertEqual(routing['main']['target']['model'], 'story-model')


class SupplementaryTests(unittest.TestCase):
    """补充覆盖：上游测试没断言、但本模块已移植的其它分支。"""

    # ---------- 运行期不依赖 narrator ----------

    def test_narrator_types_are_type_checking_only(self):
        # `ProviderConfig` 等只在 TYPE_CHECKING 下 import，运行期不留名字，
        # 因此 core/narrator.py 尚未落地也能 import 本模块。
        for name in ('ProviderConfig', 'ModelConfig', 'ModelProfile', 'ProviderMode', 'ProviderResponseFormat'):
            self.assertNotIn(name, vars(model_routing))

    # ---------- 候选顺序 / timeline ----------

    def test_assigned_candidates_keep_config_order(self):
        routing = resolve_model_routing(config([
            provider('first', {'use_for_main': True}),
            provider('second', {'use_for_main': True}),
        ]))
        self.assertEqual([p['id'] for p in routing['main']['providers']], ['first', 'second'])
        self.assertTrue(routing['main']['assigned'])
        self.assertRegex(format_model_routing(routing), r'main=first/first-model\[assigned-provider\]')

    def test_timeline_mirrors_compaction_and_only_renames_the_task(self):
        routing = resolve_model_routing(config([
            provider('compact', {'use_for_compaction': True}),
        ]))
        self.assertEqual(routing['timeline']['task'], 'timeline')
        self.assertEqual(routing['timeline']['providers'], routing['compaction']['providers'])
        self.assertEqual(routing['timeline']['target'], routing['compaction']['target'])
        self.assertEqual(routing['timeline']['reason'], 'assigned-provider')

    def test_compaction_and_alter_disabled_are_propagated_to_timeline(self):
        model_config = config([provider('one')])
        model_config['compaction'] = {'enabled': False}
        alter_config = {'enabled': False}
        routing = resolve_model_routing(model_config, alter_config)
        self.assertEqual(routing['compaction']['reason'], 'disabled')
        self.assertEqual(routing['compaction']['providers'], [])
        self.assertEqual(routing['timeline']['reason'], 'disabled')
        self.assertEqual(routing['timeline']['task'], 'timeline')
        self.assertEqual(routing['alter']['reason'], 'disabled')
        self.assertFalse(routing['alter']['available'])

    # ---------- 兜底与指定连接 ----------

    def test_legacy_fallback_still_routes_an_ordinary_provider(self):
        routing = resolve_model_routing(config([provider('plain')]))
        self.assertEqual(routing['main']['reason'], 'legacy-fallback')
        self.assertEqual(routing['main']['assigned'], False)
        self.assertEqual(routing['main']['available'], True)
        self.assertEqual([p['id'] for p in routing['main']['providers']], ['plain'])
        self.assertRegex(format_model_routing(routing), r'main=plain/plain-model\[legacy-fallback\]')

    def test_task_configured_provider_without_model_reports_task_config(self):
        model_config = config([provider('plain')])
        model_config['compaction'] = {'enabled': True, 'provider_id': 'plain', 'model': ''}
        routing = resolve_model_routing(model_config)
        self.assertEqual(routing['compaction']['reason'], 'task-config')
        self.assertEqual(routing['compaction']['assigned'], False)
        self.assertEqual(routing['compaction']['available'], True)
        self.assertEqual(routing['compaction']['target']['provider_id'], 'plain')
        self.assertEqual(routing['compaction']['target']['model'], '')

    def test_embedding_route_follows_its_own_assignment(self):
        model_config = config([provider('vectors', {'use_for_embedding': True})])
        model_config['embedding'] = {'enabled': True, 'provider_id': '', 'model': ''}
        routing = resolve_model_routing(model_config)
        self.assertEqual(routing['embedding']['available'], True)
        self.assertEqual(routing['embedding']['reason'], 'assigned-provider')
        self.assertEqual([p['id'] for p in routing['embedding']['providers']], ['vectors'])
        # 聊天任务仍然不能拿这条只做向量的连接兜底。
        self.assertFalse(routing['main']['available'])

    def test_embedding_disabled_when_flag_missing_or_false(self):
        missing = resolve_model_routing(config([provider('vectors', {'use_for_embedding': True})]))
        self.assertEqual(missing['embedding']['reason'], 'disabled')
        model_config = config([provider('vectors', {'use_for_embedding': True})])
        model_config['embedding'] = {'enabled': False, 'provider_id': 'vectors', 'model': 'embed-model'}
        explicit = resolve_model_routing(model_config)
        self.assertEqual(explicit['embedding']['reason'], 'disabled')
        self.assertEqual(explicit['embedding']['providers'], [])

    # ---------- stickers / vision ----------

    def test_stickers_and_vision_are_assigned_only(self):
        routing = resolve_model_routing(config([
            provider('painter', {'use_for_vision': True}),
            provider('sticker', {'use_for_stickers': True}),
        ]))
        self.assertEqual([p['id'] for p in routing['vision']['providers']], ['painter'])
        self.assertEqual(routing['vision']['target']['provider_id'], 'painter')
        self.assertEqual(routing['vision']['target']['model'], 'painter-model')
        self.assertEqual(routing['vision']['reason'], 'assigned-provider')
        self.assertEqual([p['id'] for p in routing['stickers']['providers']], ['sticker'])
        # 旁路连接绝不参与聊天兜底。
        self.assertEqual(routing['main']['reason'], 'unavailable')

    def test_stickers_and_vision_unavailable_without_assignment(self):
        routing = resolve_model_routing(config([provider('plain')]))
        for task in ('stickers', 'vision'):
            self.assertEqual(routing[task]['reason'], 'unavailable')
            self.assertEqual(routing[task]['providers'], [])
            self.assertEqual(routing[task]['assigned'], False)
            self.assertEqual(routing[task]['target'], {'provider_id': '', 'model': ''})

    # ---------- isAssignedTo ----------

    def test_is_assigned_to_uses_strict_boolean_and_falls_through_to_vision(self):
        self.assertTrue(is_assigned_to({'use_for_main': True}, 'main'))
        self.assertFalse(is_assigned_to({'use_for_main': 1}, 'main'))
        self.assertFalse(is_assigned_to({}, 'compaction'))
        self.assertTrue(is_assigned_to({'use_for_compaction': True}, 'compaction'))
        self.assertTrue(is_assigned_to({'use_for_alter': True}, 'alter'))
        self.assertTrue(is_assigned_to({'use_for_embedding': True}, 'embedding'))
        self.assertTrue(is_assigned_to({'use_for_stickers': True}, 'stickers'))
        self.assertTrue(is_assigned_to({'use_for_vision': True}, 'vision'))
        # 上游类型里 timeline 被排除；运行期会一路落到最后一项（vision）。
        self.assertTrue(is_assigned_to({'use_for_vision': True}, 'timeline'))
        self.assertFalse(is_assigned_to({'use_for_compaction': True}, 'timeline'))

    # ---------- providerKey ----------

    def test_provider_key_prefers_explicit_id_then_triple(self):
        self.assertEqual(provider_key({'id': 'writer', 'label': 'l', 'model': 'm', 'endpoint': 'e'}), 'writer')
        self.assertEqual(provider_key({'id': '  writer  '}), 'writer')
        self.assertEqual(
            provider_key({'label': ' Local ', 'model': ' m ', 'endpoint': ' https://x/v1 '}),
            'Local:m:https://x/v1',
        )

    def test_configured_providers_normalizes_identity_for_deduplication(self):
        providers = configured_providers(config([
            {'label': 'Local', 'model': 'm', 'endpoint': 'https://x/v1', 'enabled': True},
            {'label': 'Local', 'model': 'm', 'endpoint': 'https://x/v1', 'enabled': True},
        ]))
        # 没有显式 id 时，归一化会先派生出 `label:model`，两条完全相同的连接
        # 因此得到同一个去重键（上游 `providerKey` 走 id 分支）。
        self.assertEqual([p['id'] for p in providers], ['Local:m', 'Local:m'])
        self.assertEqual([provider_key(p) for p in providers], ['Local:m'] * 2)
        self.assertEqual(providers[0]['api_key'], '')
        self.assertEqual(providers[0]['max_tokens'], 4096)
        self.assertEqual(providers[0]['response_format'], 'json-object')
        self.assertEqual(providers[0]['reasoning_effort'], 'high')
        self.assertEqual(providers[0]['deepseek_thinking'], 'disabled')
        self.assertEqual(providers[0]['deepseek_reasoning_effort'], 'low')
        self.assertEqual(providers[0]['use_for_main'], False)

    # ---------- normalizeProvider / presetEndpoint ----------

    def test_normalize_provider_replaces_official_endpoints_and_defaults(self):
        zhipu = normalize_provider({'mode': 'zhipu-official', 'label': 'Z', 'model': 'glm-4'})
        self.assertEqual(zhipu['endpoint'], ZHIPU_OFFICIAL_CHAT_ENDPOINT)
        self.assertEqual(zhipu['id'], 'Z:glm-4')
        self.assertEqual(zhipu['label'], 'Z')
        self.assertEqual(zhipu['temperature'], 1)
        self.assertEqual(zhipu['top_p'], 0.95)
        self.assertEqual(zhipu['timeout'], 45_000)
        self.assertTrue(zhipu['zhipu_official'])
        self.assertFalse(zhipu['deepseek_official'])

        deepseek = normalize_provider({'mode': 'deepseek-official', 'label': '', 'model': 'r1'})
        self.assertEqual(deepseek['endpoint'], 'https://api.deepseek.com/v1/chat/completions')
        self.assertEqual(deepseek['label'], 'DeepSeek Official')
        self.assertEqual(deepseek['temperature'], 0.8)
        self.assertEqual(deepseek['top_p'], 1)
        self.assertEqual(deepseek['timeout'], 60_000)
        self.assertTrue(deepseek['deepseek_official'])
        self.assertEqual(deepseek['deepseek_thinking'], 'enabled' if False else 'disabled')

        fallback_label = normalize_provider({'mode': 'openai-compatible', 'label': '   ', 'model': 'gpt'})
        self.assertEqual(fallback_label['label'], 'Model connection')
        self.assertEqual(fallback_label['id'], 'provider:gpt')
        # 上游此时得到 undefined（`'' || undefined`）——保持同样的「无 endpoint」状态。
        self.assertIsNone(fallback_label['endpoint'])

    def test_normalize_provider_keeps_explicit_zero_and_unknown_extra_fields(self):
        # `?? ` 只在 None 时回落：显式的 0 / '' 必须原样保留。
        normalized = normalize_provider({
            'id': 'writer', 'label': 'W', 'enabled': True, 'endpoint': 'https://x/v1',
            'api_key': '', 'model': 'm', 'temperature': 0, 'top_p': 0, 'max_tokens': 0,
            'timeout': 0, 'response_format': '', 'extra_headers': '', 'extra_body': '',
            'price_input': 1.5, 'use_for_vision': 1,
        })
        self.assertEqual(normalized['temperature'], 0)
        self.assertEqual(normalized['top_p'], 0)
        self.assertEqual(normalized['max_tokens'], 0)
        self.assertEqual(normalized['timeout'], 0)
        self.assertEqual(normalized['response_format'], '')
        self.assertEqual(normalized['price_input'], 1.5)
        # `useForX === true` 是严格判断：数字 1 不算指派。
        self.assertFalse(normalized['use_for_vision'])

    def test_normalize_provider_keeps_user_endpoint_for_unrecognised_mode(self):
        normalized = normalize_provider({'mode': 'openai-compatible', 'endpoint': ' https://mine/v1 '})
        self.assertEqual(normalized['endpoint'], ' https://mine/v1 ')

    def test_preset_endpoint_covers_every_official_mode(self):
        self.assertEqual(preset_endpoint('zhipu-official'), ZHIPU_OFFICIAL_CHAT_ENDPOINT)
        self.assertEqual(preset_endpoint('openai-official'), 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(preset_endpoint('deepseek-official'), 'https://api.deepseek.com/v1/chat/completions')
        self.assertEqual(preset_endpoint('moonshot-official'), 'https://api.moonshot.cn/v1/chat/completions')
        self.assertEqual(preset_endpoint('siliconflow-official'), 'https://api.siliconflow.cn/v1/chat/completions')
        self.assertEqual(preset_endpoint('openrouter'), 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(
            preset_endpoint('gemini-openai'),
            'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
        )
        self.assertEqual(
            preset_endpoint('dashscope-official', 'singapore'),
            'https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions',
        )
        self.assertEqual(
            preset_endpoint('dashscope-official', 'us'),
            'https://dashscope-us.aliyuncs.com/compatible-mode/v1/chat/completions',
        )
        self.assertEqual(
            preset_endpoint('dashscope-official', 'beijing'),
            'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
        )
        self.assertEqual(
            preset_endpoint('dashscope-official'),
            'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
        )
        self.assertEqual(preset_endpoint('openai-compatible'), '')
        self.assertEqual(preset_endpoint(None), '')

    # ---------- resolveModelTarget / effectiveMainModelId ----------

    def test_resolve_model_target_prefers_profile_then_explicit_arguments(self):
        model_config = config([])
        model_config['models'] = [{
            'id': 'p1', 'label': 'p1', 'enabled': True, 'provider_id': ' shared ',
            'model': ' story-model ', 'max_tokens': 8192, 'timeout': 90_000, 'response_format': 'prompt-only',
        }]
        profile = resolve_model_target(model_config, ' p1 ', 'ignored', 'ignored-model')
        self.assertEqual(profile['provider_id'], 'shared')
        self.assertEqual(profile['model'], 'story-model')
        self.assertEqual(profile['max_tokens'], 8192)
        self.assertEqual(profile['timeout'], 90_000)
        self.assertEqual(profile['response_format'], 'prompt-only')

        # 未命中 profile：回落到显式参数，且同样 trim。
        fallback = resolve_model_target(model_config, 'missing', ' conn ', ' m ')
        self.assertEqual(fallback['provider_id'], 'conn')
        self.assertEqual(fallback['model'], 'm')
        self.assertIsNone(fallback['max_tokens'])
        self.assertIsNone(fallback['timeout'])
        self.assertIsNone(fallback['response_format'])

        # 禁用的 profile 不参与选择。
        model_config['models'][0]['enabled'] = False
        disabled = resolve_model_target(model_config, 'p1', 'conn', 'm')
        self.assertEqual(disabled['provider_id'], 'conn')
        self.assertEqual(disabled['model'], 'm')

    def test_effective_main_model_id_prefers_explicit_then_single_usable_profile(self):
        self.assertEqual(effective_main_model_id({}), '')
        explicit = {'main_model_id': ' writer ', 'models': [{'id': 'only'}]}
        self.assertEqual(effective_main_model_id(explicit), 'writer')

        one = {'models': [{'id': 'only', 'provider_id': 'p', 'model': 'm', 'enabled': True}]}
        self.assertEqual(effective_main_model_id(one), 'only')

        two = {'models': [
            {'id': 'a', 'provider_id': 'p', 'model': 'm'},
            {'id': 'b', 'provider_id': 'p', 'model': 'm'},
        ]}
        self.assertEqual(effective_main_model_id(two), '')

        # 不完整或被禁用的条目不算「可用」。
        names = {'models': [
            {'id': 'no-provider', 'provider_id': '  ', 'model': 'm'},
            {'id': 'disabled', 'provider_id': 'p', 'model': 'm', 'enabled': False},
        ]}
        self.assertEqual(effective_main_model_id(names), '')
        self.assertEqual(enabled_model_profiles(names), [])

    # ---------- usesRemoteProviders ----------

    def test_uses_remote_providers_follows_any_available_task(self):
        self.assertFalse(uses_remote_providers(config([])))
        self.assertTrue(uses_remote_providers(config([provider('writer', {'use_for_main': True})])))
        # 只有一条「纯向量」连接、且 embedding 未启用时，不算远端模式。
        self.assertFalse(uses_remote_providers(config([provider('vectors', {'use_for_embedding': True})])))
        model_config = config([provider('vectors', {'use_for_embedding': True})])
        model_config['embedding'] = {'enabled': True}
        self.assertTrue(uses_remote_providers(model_config))
        self.assertTrue(uses_remote_providers(config([provider('painter', {'use_for_vision': True})])))

    # ---------- formatModelRouting ----------

    def test_format_model_routing_reports_every_task_when_nothing_is_configured(self):
        routing = resolve_model_routing(config([]))
        self.assertEqual(
            format_model_routing(routing),
            'main=未配置[unavailable] compaction=未配置[unavailable] timeline=未配置[unavailable] '
            'alter=未配置[unavailable] embedding=未配置[disabled] stickers=未配置[unavailable] '
            'vision=未配置[unavailable]',
        )

    def test_format_model_routing_uses_assigned_provider_model_and_未指定_fallback(self):
        # `未指定` 分支在公开 API 下不可达（assigned 要求 provider.model 为真值），
        # 但上游保留了它；这里在一个完整路由表上替换 main，覆盖该分支。
        # `formatModelRouting` 会输出全部七个任务，故只比较第一段（main）。
        table = resolve_model_routing(config([provider('plain')]))

        def main_part() -> str:
            return format_model_routing(table).split(' ')[0]

        table['main'] = {
            'task': 'main', 'target': {'provider_id': '', 'model': ''}, 'assigned': True, 'available': True,
            'reason': 'assigned-provider',
            'providers': [{'id': 'p1', 'label': 'P1', 'model': ''}],
        }
        self.assertEqual(main_part(), 'main=P1/未指定[assigned-provider]')

        # label 为空时回落到 id。
        table['main']['providers'] = [{'id': 'p1', 'label': '', 'model': 'm'}]
        self.assertEqual(main_part(), 'main=p1/m[assigned-provider]')

        # 非 assigned 时优先 target.model，其次 provider.model。
        table['main']['assigned'] = False
        table['main']['reason'] = 'model-profile'
        table['main']['providers'] = [{'id': 'p1', 'label': 'P1', 'model': 'm'}]
        table['main']['target'] = {'provider_id': 'p1', 'model': 'profile-model'}
        self.assertEqual(main_part(), 'main=P1/profile-model[model-profile]')
        table['main']['target'] = {'provider_id': 'p1', 'model': ''}
        self.assertEqual(main_part(), 'main=P1/m[model-profile]')


if __name__ == '__main__':
    unittest.main()
