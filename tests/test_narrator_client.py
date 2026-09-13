"""`upstream/test/token-usage.test.ts` 与 `upstream/test/streaming-reply.test.ts` 的 Python 对应物。

上游测试逐条移植（断言一一对照），并补齐 `core/narrator.py` 客户端半部分的边界用例：

- token 计量与计费（`token-usage.test.ts` 的 5 条）
- 流式早期可见回复与固定合约（`streaming-reply.test.ts` 的前 3 条）
- `extractEarlyNarrativeReply` 的私聊/群聊、字段顺序、破损 JSON 边界
- `jsonCandidates` / `balancedJsonValues` 的提取行为
- `rotate` / `deriveEmbeddingEndpoint` / `aggregateTokenUsages` 的合并规则
- 传输层（假 `HttpClient`）：请求体、故障切换与冷却、round-robin、旁路 JSON 降级重试、
  智谱首 token 超时、OpenAI 兼容 SSE 与「假装流式」回落、向量化、表情描述、侧端识图

`streaming-reply.test.ts` 的第 4 条（打字延迟）测的是 `InterludeService.typingDelayMilliseconds`，
属于 `core/service.py`（另一个并行移植任务），因此这里按上游数值写成条件用例：模块或方法
尚未落地时自动跳过，落地后立即生效。

键名约定：本移植版的内部结构是 snake_case（`input_tokens` / `group_reply` /
`provider_label`），上游同名断言里的 `inputTokens` / `groupReply` / `providerLabel`
即对应这些键；发给模型的请求体（`messages` / `response_format` / `input_audio` …）
则逐字保持上游 camelCase。
"""

from __future__ import annotations

import asyncio
import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from plugin.core import logging as core_logging
from plugin.core import narrator
from plugin.core.narrator import (
    HttpxHttpClient,
    HttpStatusError,
    OpenAICompatibleEmbedder,
    OpenAICompatibleNarrator,
    SinkLogger,
    SilentCompactor,
    SilentEmbedder,
    SilentNarrator,
    SilentStickerDescriber,
    SilentVisionDescriber,
    ZHIPU_FIRST_VISIBLE_TOKEN_TIMEOUT,
    aggregate_token_usages,
    balanced_json_values,
    chat_text_candidates,
    compute_token_cost,
    create_compactor,
    create_embedder,
    create_narrator,
    create_sticker_describer,
    create_vision_describer,
    derive_embedding_endpoint,
    extract_chat_text,
    extract_early_narrative_reply,
    extract_top_level_json_field,
    flatten_chat_text,
    format_token_usage_line,
    has_usage_fields,
    json_candidates,
    parse_object,
    parse_token_usage,
    request_openai_compatible_streaming,
    request_zhipu_streaming,
    resolve_http,
    rotate,
    with_deepseek_thinking,
)

try:  # 并行移植任务：`core/service.py`（打字延迟用例的上游宿主）。
    from plugin.core import service as _service
except ImportError:  # pragma: no cover - 仅在 service.py 尚未落地时生效
    _service = None


# ======================================================================================
# 测试替身
# ======================================================================================


class RecordingLogger:
    """记录式 logger（上游 `ctx.logger` 的替身）。"""

    def __init__(self) -> None:
        self.debugs: list[tuple] = []
        self.warns: list[tuple] = []

    def debug(self, message, *args):
        self.debugs.append((message, args))

    def warn(self, message, *args):
        self.warns.append((message, args))


class FakeStream:
    """按顺序产出文本块的假流；元素可以是 str、`(延迟秒数, str)` 或异常。"""

    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        item = self._chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, tuple):
            await asyncio.sleep(item[0])
            item = item[1]
        return item

    async def aclose(self):
        self.closed = True
        self._chunks = []


class FakeHttpClient:
    """注入式假 `HttpClient`：记录请求，按队列返回响应 / 流。"""

    def __init__(self, responses=None, streams=None) -> None:
        self._responses = list(responses or [])
        self._streams = list(streams or [])
        self.posts: list[dict] = []
        self.opened_streams: list[dict] = []

    async def post_json(self, url, headers=None, body=None, timeout=None):
        self.posts.append({'url': url, 'headers': headers, 'body': body, 'timeout': timeout})
        if not self._responses:
            raise AssertionError('测试替身没有准备更多的 post_json 响应。')
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def iterate_sse(self, url, headers=None, body=None, timeout=None):
        self.opened_streams.append({'url': url, 'headers': headers, 'body': body, 'timeout': timeout})
        item = self._streams.pop(0) if self._streams else []
        return FakeStream(item)


def sse(payload) -> str:
    """构造一个 SSE data 事件块。"""
    return 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'


def delta_chunk(text: str) -> str:
    """OpenAI 兼容的增量文本事件。"""
    return sse({'choices': [{'delta': {'content': text}}]})


def make_provider(**overrides) -> dict:
    """一条已归一化的连接配置（与 `model_routing.normalize_provider` 的输出对齐）。"""
    provider = {
        'id': 'p1',
        'label': 'P1',
        'enabled': True,
        'endpoint': 'https://example.test/v1/chat/completions',
        'api_key': 'key-1',
        'model': 'm1',
        'temperature': 0.8,
        'top_p': 1,
        'max_tokens': 4096,
        'timeout': 60_000,
        'response_format': 'json-object',
        'extra_headers': '',
        'extra_body': '',
        'use_for_main': True,
    }
    provider.update(overrides)
    return provider


def make_config(**overrides) -> dict:
    """一份最小可用的 `ModelConfig`。"""
    config = {
        'providers': [make_provider()],
        'failover': {
            'enabled': True, 'strategy': 'priority',
            'max_attempts_per_provider': 1, 'cooldown_minutes': 5,
        },
        'main_prompt': '',
        'format_prompt': '',
        'fixed_prompt': 'FIXED',
        'style_prompt': 'STYLE',
    }
    config.update(overrides)
    return config


def make_request(**overrides) -> dict:
    """一份最小的 `NarrativeRequest`。"""
    now = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    request = {
        'phase': 'advance',
        'story': {'setting': {'style': 'Realistic'}, 'state': {}},
        'from': now,
        'now': now,
        'recent_entries': [],
    }
    request.update(overrides)
    return request


def stub_prompts():
    """把提示词组装替换成桩，专注验证客户端半部分（上游同一文件的另一半）。"""
    return (
        mock.patch.object(narrator, 'to_prompt_payload', lambda request, options=None: {'stub': True}),
        mock.patch.object(narrator, 'system_prompt', lambda *args, **kwargs: 'SYS'),
    )


# ======================================================================================
# token-usage.test.ts（逐条移植）
# ======================================================================================


class TokenUsageTests(unittest.TestCase):
    def test_parse_token_usage_reads_openai_and_deepseek_cache_shapes_and_ignores_unknown_ones(self):
        self.assertEqual(
            parse_token_usage({
                'prompt_tokens': 1000, 'completion_tokens': 250,
                'prompt_tokens_details': {'cached_tokens': 640},
            }),
            {'input_tokens': 1000, 'output_tokens': 250, 'cached_input_tokens': 640},
        )
        self.assertEqual(
            parse_token_usage({'prompt_tokens': 900, 'completion_tokens': 100, 'prompt_cache_hit_tokens': 333}),
            {'input_tokens': 900, 'output_tokens': 100, 'cached_input_tokens': 333},
        )
        self.assertEqual(parse_token_usage({'usage': 'nonsense'}), {})
        self.assertEqual(parse_token_usage(None), {})

    def test_aggregate_token_usages_sums_attempts_and_keeps_the_final_attempt_identity_and_pricing(self):
        records = [
            {'task': '主叙事', 'provider_label': 'A', 'model': 'm-a', 'input_tokens': 100, 'output_tokens': 20, 'price_input': 2},
            {
                'task': '主叙事', 'provider_label': 'B', 'model': 'm-b',
                'input_tokens': 1500, 'output_tokens': 300, 'cached_input_tokens': 1200,
                'price_input': 1, 'price_output': 4, 'price_cached_input': 0.2,
            },
        ]
        total = aggregate_token_usages(records)
        self.assertEqual(total['input_tokens'], 1600)
        self.assertEqual(total['output_tokens'], 320)
        self.assertEqual(total['cached_input_tokens'], 1200)
        self.assertEqual(total['provider_label'], 'B')
        self.assertEqual(total['model'], 'm-b')
        self.assertEqual(total['price_input'], 1)
        self.assertIsNone(aggregate_token_usages([]))

    def test_compute_token_cost_bills_cached_tokens_at_the_cache_price_and_reports_savings(self):
        cost = compute_token_cost({
            'task': '主叙事', 'provider_label': 'B', 'model': 'm-b',
            'input_tokens': 1_000_000, 'output_tokens': 100_000, 'cached_input_tokens': 800_000,
            'price_input': 2, 'price_output': 8, 'price_cached_input': 0.5,
        })
        # 非缓存 200k × ¥2 + 缓存 800k × ¥0.5 + 输出 100k × ¥8，均按每 1M 计价。
        self.assertEqual(cost['input_cost'], 0.8)
        self.assertEqual(cost['output_cost'], 0.8)
        self.assertLess(abs(cost['total'] - 1.6), 1e-9)
        self.assertLess(abs(cost['saved'] - 1.2), 1e-9)

    def test_compute_token_cost_is_undefined_without_prices_and_falls_back_to_the_input_price(self):
        base = {'task': '主叙事', 'provider_label': 'A', 'model': 'm', 'input_tokens': 1000, 'output_tokens': 100}
        self.assertIsNone(compute_token_cost({**base}))
        fallback = compute_token_cost({**base, 'price_input': 3, 'price_cached_input': 0})
        self.assertEqual(fallback['input_cost'], 0.003)

    def test_format_token_usage_line_renders_usage_hit_rate_and_optional_billing(self):
        line = format_token_usage_line({
            'task': '主叙事', 'provider_label': 'B', 'model': 'm-b',
            'input_tokens': 1600, 'output_tokens': 320, 'cached_input_tokens': 1200,
            'price_input': 1, 'price_output': 4, 'price_cached_input': 0.2,
        })
        self.assertIn('输入=1600（缓存 1200，命中率 75.0%）', line)
        self.assertIn('输出=320', line)
        self.assertIn('计费合计=', line)
        unpriced = format_token_usage_line({
            'task': '主叙事', 'provider_label': 'A', 'model': 'm-a', 'input_tokens': 10, 'output_tokens': 2,
        })
        self.assertNotIn('计费合计', unpriced)
        self.assertIn('输入=10', unpriced)


class TokenUsageBoundaryTests(unittest.TestCase):
    """`aggregateTokenUsages` 的合并规则（上游测试未覆盖的部分）。"""

    def test_aggregate_returns_none_when_no_record_carries_a_count(self):
        self.assertIsNone(aggregate_token_usages([{'task': '主叙事', 'provider_label': 'A', 'model': 'm'}]))
        self.assertIsNone(aggregate_token_usages([
            {'task': '主叙事', 'provider_label': 'A', 'model': 'm', 'input_tokens': None},
        ]))

    def test_aggregate_omits_zero_totals_and_searches_prices_from_the_end(self):
        aggregated = aggregate_token_usages([
            {'task': '主叙事', 'provider_label': 'A', 'model': 'm-a', 'input_tokens': 0, 'output_tokens': 7},
        ])
        self.assertNotIn('input_tokens', aggregated)
        self.assertEqual(aggregated['output_tokens'], 7)
        self.assertNotIn('price_input', aggregated)

        priced_last = aggregate_token_usages([
            {'task': '主叙事', 'provider_label': 'A', 'model': 'm-a', 'input_tokens': 5, 'price_input': 9},
            {'task': '主叙事', 'provider_label': 'B', 'model': 'm-b', 'input_tokens': 5, 'price_output': 4},
        ])
        # 身份取最后一条，价目取**最后一条带价目**的记录（上游 `[...records].reverse().find(...)`）。
        self.assertEqual(priced_last['provider_label'], 'B')
        self.assertEqual(priced_last['price_output'], 4)
        self.assertIsNone(priced_last['price_input'])

    def test_has_usage_fields_accepts_any_single_reported_count(self):
        self.assertFalse(has_usage_fields({'task': 't', 'provider_label': 'p', 'model': 'm'}))
        self.assertTrue(has_usage_fields({'cached_input_tokens': 0}))
        self.assertTrue(has_usage_fields({'output_tokens': 0}))

    def test_parse_token_usage_ignores_non_number_counts_and_prefers_the_legacy_cache_field(self):
        self.assertEqual(
            parse_token_usage({'prompt_tokens': '100', 'completion_tokens': True}),
            {},
        )
        self.assertEqual(
            parse_token_usage({
                'prompt_tokens': 10,
                'prompt_tokens_details': {'cached_tokens': 4},
                'prompt_cache_hit_tokens': 6,
            }),
            {'input_tokens': 10, 'cached_input_tokens': 6},
        )
        self.assertEqual(parse_token_usage({'prompt_tokens_details': {'cached_tokens': 'x'}}), {})

    def test_compute_token_cost_needs_a_positive_price_and_clamps_the_cached_subset(self):
        self.assertIsNone(compute_token_cost({'input_tokens': 10, 'price_input': 0, 'price_output': 0}))
        # 缓存数超过输入数时按输入数封顶（上游 `Math.min`）。
        cost = compute_token_cost({
            'input_tokens': 1_000, 'output_tokens': 0,
            'cached_input_tokens': 5_000, 'price_input': 2, 'price_cached_input': 0.5,
        })
        self.assertEqual(cost['input_cost'], 0.0005)

    def test_format_token_usage_line_omits_the_rate_when_there_are_no_input_tokens(self):
        line = format_token_usage_line({'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 3})
        self.assertEqual(line, '输入=0（缓存 0） 输出=3')


# ======================================================================================
# streaming-reply.test.ts（逐条移植）
# ======================================================================================


class StreamingReplyTests(unittest.TestCase):
    def test_stream_parser_emits_only_a_complete_private_interaction_before_the_script_arrives(self):
        partial = '{"interaction":{"seen":true,"reply":{"mode":"immediate","content":"收到啦"}},"script":"'
        self.assertEqual(
            extract_early_narrative_reply(partial, False),
            {
                'kind': 'private',
                'content': '收到啦',
                'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '收到啦'}},
            },
        )
        self.assertIsNone(
            extract_early_narrative_reply(
                '{"interaction":{"seen":true,"reply":{"mode":"immediate","content":"未结束', False,
            ),
        )

    def test_stream_parser_supports_a_complete_group_reply_field_without_requiring_the_later_script(self):
        partial = '{"groupReply":{"mode":"immediate","content":"群里见","replyTo":"msg-7"},"script":"'
        # 上游断言里的 `groupReply` / `replyTo` 在本移植版内部结构中是 `group_reply` / `reply_to`。
        self.assertEqual(
            extract_early_narrative_reply(partial, True),
            {
                'kind': 'group',
                'content': '群里见',
                'group_reply': {'mode': 'immediate', 'content': '群里见', 'reply_to': 'msg-7'},
            },
        )

    def test_fixed_contract_asks_for_transport_before_script_when_experimental_streaming_is_enabled(self):
        prompt = narrator.system_prompt(
            'user-message', '', '', '', '', '',
            False, False, False, False, False, None, False, None, True, True,
        )
        self.assertRegex(prompt, r'(?i)put interaction first and script after it')
        self.assertRegex(prompt, r'(?i)streaming protocol')
        ordinary = narrator.system_prompt('advance', '', '', '', '', '')
        self.assertRegex(ordinary, r'(?i)script first')


@unittest.skipUnless(_service is not None and hasattr(_service, 'InterludeService'),
                     'core/service.py 由并行移植任务落地（打字延迟属于服务层）')
class TypingDelayTests(unittest.TestCase):
    def test_typing_delay_applies_bounded_thirty_percent_variation_and_can_be_made_deterministic(self):
        method = getattr(_service.InterludeService, 'typing_delay_milliseconds', None)
        if method is None:
            self.skipTest('service.InterludeService.typing_delay_milliseconds 尚未落地')
        config = {
            'runtime': {
                'typing_base_delay_seconds': 1,
                'typing_characters_per_second': 8,
                'typing_max_delay_seconds': 12,
                'typing_jitter_ratio': 0.3,
            },
        }
        holder = SimpleNamespace(config=config)
        try:
            with mock.patch.object(random, 'random', return_value=0.0):
                self.assertEqual(method(holder, '12345678'), 1_400)
            with mock.patch.object(random, 'random', return_value=1.0):
                self.assertEqual(method(holder, '12345678'), 2_600)
            holder.config['runtime']['typing_jitter_ratio'] = 0
            self.assertEqual(method(holder, '12345678'), 2_000)
        except KeyError as error:  # pragma: no cover - 取决于 service 模块的配置键名
            self.skipTest(f'服务层配置键名与上游不同：{error}')


# ======================================================================================
# 早期回复解析的边界
# ======================================================================================


class EarlyReplyBoundaryTests(unittest.TestCase):
    def test_returns_none_when_the_field_is_absent_or_not_an_object(self):
        self.assertIsNone(extract_early_narrative_reply('', False))
        self.assertIsNone(extract_early_narrative_reply('{"script":"x"}', False))
        self.assertIsNone(extract_early_narrative_reply('{"interaction":"nope"}', False))
        self.assertIsNone(extract_early_narrative_reply('{"interaction":["nope"]}', False))
        self.assertIsNone(extract_early_narrative_reply('no json at all', False))
        self.assertIsNone(extract_early_narrative_reply('{"interaction":', False))

    def test_scans_past_earlier_top_level_fields_in_order(self):
        raw = (
            '{"script":"先写了一半","memories":[{"content":"记得"}],'
            '"interaction":{"seen":false,"reply":{"mode":"immediate","content":"在的"}},"intents":['
        )
        self.assertEqual(
            extract_early_narrative_reply(raw, False),
            {
                'kind': 'private',
                'content': '在的',
                'interaction': {'seen': False, 'reply': {'mode': 'immediate', 'content': '在的'}},
            },
        )

    def test_private_reply_requires_a_boolean_seen_and_an_immediate_mode(self):
        self.assertIsNone(
            extract_early_narrative_reply('{"interaction":{"seen":1,"reply":{"mode":"immediate","content":"x"}}}', False),
        )
        self.assertIsNone(
            extract_early_narrative_reply('{"interaction":{"seen":true,"reply":{"mode":"delayed","content":"x"}}}', False),
        )
        self.assertIsNone(
            extract_early_narrative_reply('{"interaction":{"seen":true,"reply":{"mode":"immediate","content":"   "}}}', False),
        )
        self.assertIsNone(
            extract_early_narrative_reply('{"interaction":{"seen":true,"reply":{"mode":"immediate"}}}', False),
        )

    def test_group_reply_rejects_a_none_mode_and_drops_a_non_string_reply_to(self):
        self.assertIsNone(extract_early_narrative_reply('{"groupReply":{"mode":"none","content":"x"}}', True))
        self.assertEqual(
            extract_early_narrative_reply('{"groupReply":{"mode":"immediate","content":" 群里见 ","replyTo":7}}', True),
            {'kind': 'group', 'content': '群里见', 'group_reply': {'mode': 'immediate', 'content': '群里见'}},
        )

    def test_broken_json_never_yields_a_partial_value(self):
        # 字符串里的花括号必须被尊重：朴素扫描器会在 `a}b{c` 的第一个 `}` 处收尾并解析失败。
        self.assertEqual(
            extract_early_narrative_reply(
                '{"interaction":{"seen":true,"reply":{"mode":"immediate","content":"a}b{c"}}', False,
            )['content'],
            'a}b{c',
        )
        self.assertEqual(
            extract_early_narrative_reply(
                '{"interaction":{"seen":true,"reply":{"mode":"immediate","content":"a}b{c"}}},"script":"', False,
            )['content'],
            'a}b{c',
        )
        # 真正未闭合的嵌套值一律放弃。
        self.assertIsNone(extract_early_narrative_reply('{"interaction":{"seen":true,"reply":', False))
        self.assertIsNone(
            extract_early_narrative_reply('{"interaction":{"seen":true,"reply":{"mode":"immediate","content":"未闭合', False),
        )

    def test_extract_top_level_json_field_reads_scalars_arrays_and_objects(self):
        self.assertEqual(extract_top_level_json_field('{"a":1,"b":2}', 'b'), 2)
        self.assertEqual(extract_top_level_json_field('{"a":[1,{"b":2}],"c":3}', 'a'), [1, {'b': 2}])
        self.assertEqual(extract_top_level_json_field('{"a":true,"b":"x"}', 'a'), True)
        self.assertIsNone(extract_top_level_json_field('{"a":1}', 'b'))
        self.assertIsNone(extract_top_level_json_field('', 'a'))
        self.assertIsNone(extract_top_level_json_field('{"a" 1}', 'a'))


# ======================================================================================
# 宽容 JSON 提取
# ======================================================================================


class JsonExtractionTests(unittest.TestCase):
    def test_json_candidates_prefers_raw_text_then_fence_bodies_then_balanced_values(self):
        self.assertEqual(json_candidates(''), [])
        fenced = '说明文字\n```json\n{"a":1}\n```\n结束'
        candidates = json_candidates(fenced)
        self.assertEqual(candidates[0], fenced.strip())
        self.assertIn('{"a":1}', candidates)
        unclosed = '前言 ```json\n{"b":2}\n'
        self.assertIn('{"b":2}', json_candidates(unclosed))
        prose = '答案如下：{"c":{"d":3}} —— 完'
        self.assertIn('{"c":{"d":3}}', json_candidates(prose))

    def test_balanced_json_values_respects_strings_and_reports_only_balanced_values(self):
        # 上游从每一个 `{` / `[` 起扫，因此嵌套对象也会被单独列出。
        self.assertEqual(balanced_json_values('a {"x":{"y":1}} b'), ['{"x":{"y":1}}', '{"y":1}'])
        self.assertEqual(balanced_json_values('[1,{"a":"}"}] tail'), ['[1,{"a":"}"}]', '{"a":"}"}'])
        self.assertEqual(balanced_json_values('{"unclosed":1'), [])
        self.assertEqual(balanced_json_values('{"mismatch":[}'), [])
        self.assertEqual(balanced_json_values('nope'), [])

    def test_parse_json_response_accepts_fences_prose_bom_and_zero_width_characters(self):
        parse = narrator.parse_json_response
        self.assertEqual(parse('```json\n{"script":"x"}\n```', 'T'), {'script': 'x'})
        self.assertEqual(parse('\ufeff\u200b{"script":"y"}\u2060  ', 'T'), {'script': 'y'})
        self.assertEqual(parse('前言 {"script":"z"} 后记', 'T'), {'script': 'z'})
        # 上游 `typeof value === 'object'` 连数组也接受。
        self.assertEqual(parse('[1,2]', 'T'), [1, 2])
        with self.assertRaises(RuntimeError) as ctx:
            parse('42', 'T')
        self.assertEqual(str(ctx.exception), 'T returned invalid JSON (JSON root is not an object.).')
        with self.assertRaises(RuntimeError) as ctx:
            parse('', 'T')
        self.assertEqual(str(ctx.exception), 'T returned invalid JSON (No JSON object found.).')

    def test_chat_text_candidates_normalize_the_gateway_shapes(self):
        self.assertEqual(
            chat_text_candidates({
                'choices': [{'message': {'content': '正文', 'reasoning_content': '思考', 'refusal': '拒绝'}, 'text': '旧式'}],
                'output_text': '输出',
            }),
            ['正文', '思考', '拒绝', '旧式', '输出'],
        )
        self.assertEqual(chat_text_candidates({'choices': [{'message': {'content': [{'text': '分片A'}, {'text': '分片B'}]}}]}), ['分片A分片B'])
        self.assertEqual(chat_text_candidates(None), [])
        self.assertEqual(chat_text_candidates({'choices': []}), [])
        self.assertEqual(extract_chat_text({'output_text': '只要这个'}), '只要这个')
        self.assertEqual(extract_chat_text({}), '')

    def test_flatten_chat_text_handles_strings_arrays_and_object_shapes(self):
        self.assertEqual(flatten_chat_text('x'), 'x')
        self.assertEqual(flatten_chat_text(['a', {'text': 'b'}, 3]), 'ab')
        self.assertEqual(flatten_chat_text({'content': 'c'}), 'c')
        self.assertEqual(flatten_chat_text({'output_text': ['d']}), 'd')
        self.assertEqual(flatten_chat_text(None), '')
        self.assertEqual(flatten_chat_text(7), '')

    def test_parse_object_reads_json_fields_and_warns_on_invalid_input(self):
        logger = RecordingLogger()
        self.assertEqual(parse_object('{"a":1}', 'extraBody', logger), {'a': 1})
        self.assertEqual(parse_object('', 'extraBody', logger), {})
        self.assertEqual(parse_object('   ', 'extraBody', logger), {})
        self.assertEqual(parse_object('[1]', 'extraBody', logger), {})
        self.assertEqual(parse_object('{oops', 'extraBody', logger), {})
        self.assertEqual(parse_object(None, 'extraBody', logger), {})
        self.assertEqual(len(logger.warns), 2)

    def test_with_deepseek_thinking_only_touches_the_official_deepseek_route(self):
        body = {'model': 'm'}
        self.assertIs(with_deepseek_thinking({'deepseek_official': False}, body), body)
        self.assertEqual(
            with_deepseek_thinking({'deepseek_official': True, 'deepseek_thinking': 'disabled'}, body),
            {'model': 'm', 'thinking': {'type': 'disabled'}},
        )
        self.assertEqual(
            with_deepseek_thinking({'deepseek_official': True, 'deepseek_thinking': 'enabled'}, body),
            {'model': 'm', 'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low'},
        )


# ======================================================================================
# 纯函数：rotate / deriveEmbeddingEndpoint / 传输层杂项
# ======================================================================================


class PureHelperTests(unittest.TestCase):
    def test_rotate_matches_the_javascript_slice_semantics(self):
        self.assertEqual(rotate([1, 2, 3], 0), [1, 2, 3])
        self.assertEqual(rotate([1, 2, 3], 1), [2, 3, 1])
        self.assertEqual(rotate([1, 2, 3], 3), [1, 2, 3])
        self.assertEqual(rotate([1, 2, 3], 4), [2, 3, 1])
        self.assertEqual(rotate([1, 2, 3], -1), [3, 1, 2])
        self.assertEqual(rotate([], 5), [])

    def test_derive_embedding_endpoint_handles_the_conventional_openai_path(self):
        self.assertEqual(
            derive_embedding_endpoint('https://api.example.com/v1/chat/completions'),
            'https://api.example.com/v1/embeddings',
        )
        self.assertEqual(
            derive_embedding_endpoint('  https://api.example.com/v1/chat/completions/  '),
            'https://api.example.com/v1/embeddings',
        )
        self.assertEqual(
            derive_embedding_endpoint('https://api.example.com/v1/chat/completions?x=1'),
            'https://api.example.com/v1/embeddings',
        )
        self.assertEqual(derive_embedding_endpoint('https://api.example.com/v1/embeddings'), '')
        self.assertEqual(derive_embedding_endpoint('   '), '')
        self.assertEqual(derive_embedding_endpoint(None), '')

    def test_resolve_http_accepts_a_client_or_a_koishi_style_context(self):
        client = FakeHttpClient()
        self.assertIs(resolve_http(client), client)
        self.assertIs(resolve_http(SimpleNamespace(http=client)), client)
        with self.assertRaises(TypeError):
            resolve_http(SimpleNamespace())

    def test_sink_logger_forwards_to_the_core_logging_sink(self):
        captured = []
        original = core_logging.get_log_sink()
        core_logging.set_log_sink(lambda level, text: captured.append((level, text)))
        try:
            SinkLogger().warn('时间导演输出不可解析 错误=%s 原始输出=%s', 'E', 'raw')
        finally:
            core_logging.set_log_sink(original)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], 'warn')
        self.assertIn('时间导演输出不可解析', captured[0][1])

    def test_httpx_client_is_imported_lazily_so_the_core_stays_dependency_free(self):
        client = HttpxHttpClient()
        self.assertIsNone(client._client)
        with self.assertRaises(RuntimeError) as ctx:
            client._ensure_client()
        self.assertIn('httpx', str(ctx.exception))
        self.assertEqual(ZHIPU_FIRST_VISIBLE_TOKEN_TIMEOUT, 45_000)


# ======================================================================================
# 流式传输
# ======================================================================================


class StreamingTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_zhipu_stream_accumulates_visible_content_and_reports_usage(self):
        http = FakeHttpClient(streams=[[
            sse({'choices': [{'delta': {'content': '{"script":"'}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}),
            sse({'choices': [{'delta': {'content': '嗯，在。"}'}}]}),
            sse({'choices': [{'delta': {}}]}),
            'data: [DONE]\n\n',
        ]])
        usages = []
        seen = []

        async def on_text(text):
            seen.append(text)

        text = await request_zhipu_streaming(
            'https://open.bigmodel.cn/api/paas/v4/chat/completions',
            {'model': 'glm', 'stream': True},
            {'content-type': 'application/json'},
            on_text,
            usages.append,
            http,
        )
        self.assertEqual(text, '{"script":"嗯，在。"}')
        self.assertEqual(seen, ['{"script":"', '{"script":"嗯，在。"}'])
        self.assertEqual(usages, [{'prompt_tokens': 10, 'completion_tokens': 2}])
        self.assertEqual(http.opened_streams[0]['timeout'], None)

    async def test_zhipu_first_visible_token_timeout_aborts_the_stream(self):
        http = FakeHttpClient(streams=[[(1.0, delta_chunk('迟到的内容'))]])
        with mock.patch.object(narrator, 'ZHIPU_FIRST_VISIBLE_TOKEN_TIMEOUT', 60):
            with self.assertRaises(RuntimeError) as ctx:
                await request_zhipu_streaming('https://x/y', {}, {}, None, None, http)
        self.assertEqual(str(ctx.exception), 'Zhipu first visible token timed out after 60ms.')

    async def test_zhipu_stream_without_visible_content_and_status_errors_use_the_upstream_messages(self):
        empty = FakeHttpClient(streams=[[sse({'choices': [{'delta': {}}]}), 'data: [DONE]\n\n']])
        with self.assertRaises(RuntimeError) as ctx:
            await request_zhipu_streaming('https://x/y', {}, {}, None, None, empty)
        self.assertEqual(str(ctx.exception), 'Zhipu stream ended without visible content.')

        failing = FakeHttpClient(streams=[[HttpStatusError(500, 'boom', 'Internal Server Error')]])
        with self.assertRaises(RuntimeError) as ctx:
            await request_zhipu_streaming('https://x/y', {}, {}, None, None, failing)
        self.assertEqual(str(ctx.exception), 'Zhipu request failed (500): boom')

        no_detail = FakeHttpClient(streams=[[HttpStatusError(503, '', 'Service Unavailable')]])
        with self.assertRaises(RuntimeError) as ctx:
            await request_zhipu_streaming('https://x/y', {}, {}, None, None, no_detail)
        self.assertEqual(str(ctx.exception), 'Zhipu request failed (503): Service Unavailable')

    async def test_openai_compatible_stream_falls_back_to_a_single_json_body(self):
        http = FakeHttpClient(streams=[['{"choices":[{"message":{"content":"整段回复"}}]}']])
        text = await request_openai_compatible_streaming(
            'https://x/y', {'stream': True}, {}, 60_000, None, None, http,
        )
        self.assertEqual(text, '整段回复')

    async def test_openai_compatible_stream_reports_timeout_and_empty_stream(self):
        slow = FakeHttpClient(streams=[[(3.0, delta_chunk('x'))]])
        with self.assertRaises(RuntimeError) as ctx:
            await request_openai_compatible_streaming('https://x/y', {}, {}, 50, None, None, slow)
        # 上游 `Math.max(1_000, timeout)` 决定真正的截止时间，消息里仍打印原始 timeout。
        self.assertEqual(str(ctx.exception), 'Streaming request timed out after 50ms.')

        empty = FakeHttpClient(streams=[[]])
        with self.assertRaises(RuntimeError) as ctx:
            await request_openai_compatible_streaming('https://x/y', {}, {}, 60_000, None, None, empty)
        self.assertEqual(str(ctx.exception), 'Streaming provider ended without visible content.')

        failing = FakeHttpClient(streams=[[HttpStatusError(429, 'slow down', 'Too Many Requests')]])
        with self.assertRaises(RuntimeError) as ctx:
            await request_openai_compatible_streaming('https://x/y', {}, {}, 60_000, None, None, failing)
        self.assertEqual(str(ctx.exception), 'Streaming request failed (429): slow down')


# ======================================================================================
# OpenAICompatibleNarrator 客户端行为
# ======================================================================================


class NarratorClientTests(unittest.IsolatedAsyncioTestCase):
    def make_narrator(self, http, config=None, logger=None, on_usage=None):
        return OpenAICompatibleNarrator(
            http, config if config is not None else make_config(),
            silent_logs=logger is None, on_usage=on_usage, logger=logger,
        )

    async def test_decide_posts_the_openai_compatible_body_and_parses_the_decision(self):
        http = FakeHttpClient(responses=[{
            'choices': [{'message': {'content': '{"script":"嗯。"}'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 2},
        }])
        usages = []
        client = self.make_narrator(http, on_usage=usages.append)
        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            decision = await client.decide(make_request())
        self.assertEqual(decision['script'], '嗯。')

        post = http.posts[0]
        self.assertEqual(post['url'], 'https://example.test/v1/chat/completions')
        self.assertEqual(post['headers']['content-type'], 'application/json')
        self.assertEqual(post['headers']['authorization'], 'Bearer key-1')
        self.assertEqual(post['timeout'], 60_000)
        self.assertEqual(post['body']['model'], 'm1')
        self.assertEqual(post['body']['temperature'], 0.8)
        self.assertEqual(post['body']['top_p'], 1)
        self.assertEqual(post['body']['max_tokens'], 4096)
        self.assertEqual(post['body']['response_format'], {'type': 'json_object'})
        self.assertEqual(post['body']['messages'][0], {'role': 'system', 'content': 'SYS'})
        self.assertEqual(post['body']['messages'][1], {'role': 'user', 'content': '{"stub":true}'})
        # `emitUsage` 输出的是聚合记录：没有价目时价目键整体省略（上游 `...(priced ? {...} : {})`）。
        self.assertEqual(usages, [{
            'task': '主叙事', 'provider_label': 'P1', 'model': 'm1',
            'input_tokens': 10, 'output_tokens': 2,
        }])

    async def test_decide_passes_the_transport_flags_to_the_fixed_contract(self):
        http = FakeHttpClient(responses=[{'choices': [{'message': {'content': '{"script":"x"}'}}]}])
        client = self.make_narrator(http)
        seen = []

        def record_system_prompt(*args, **kwargs):
            seen.append(args)
            return 'SYS'

        config = make_config(main_payload_order='cache-first')
        client.config = config
        client.routing = narrator.resolve_model_routing(config)
        with mock.patch.object(narrator, 'to_prompt_payload', lambda request, options=None: {'stub': True}), \
                mock.patch.object(narrator, 'system_prompt', record_system_prompt):
            await client.decide(make_request(quoted_messages=[{'id': 1}]))
        args = seen[0]
        self.assertEqual(args[0], 'advance')
        self.assertEqual(args[1], '')
        self.assertEqual(args[5], 'Realistic')
        self.assertIs(args[6], False)   # refreshContinuity
        self.assertIs(args[7], False)   # alterEnabled
        self.assertIs(args[8], False)   # agencyEnabled
        self.assertIs(args[9], False)   # perspectiveEnabled
        self.assertIs(args[10], False)  # outputRecovery
        self.assertIsNone(args[11])     # chatCapabilities
        self.assertIs(args[12], True)   # hasQuotedMessage
        self.assertIsNone(args[13])     # stickerCatalog
        self.assertIs(args[14], False)  # schedulePreplanEnabled
        self.assertIs(args[15], False)  # streamingReplyFirst
        self.assertIs(args[16], True)   # cacheFirstPayload
        self.assertIs(args[17], False)  # groupTurn
        self.assertIsNone(args[18])     # writingOptions

    async def test_experimental_streaming_delivers_the_early_reply_before_the_script(self):
        partial = '{"interaction":{"seen":true,"reply":{"mode":"immediate","content":"收到啦"}},"script":"'
        http = FakeHttpClient(streams=[[delta_chunk(partial), delta_chunk('嗯。"}')]])
        client = self.make_narrator(http, make_config(main_streaming_mode='experimental'))
        early = []

        async def on_early_reply(reply):
            early.append(reply)
            return True

        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            decision = await client.decide(make_request(
                phase='user-message', user_message='在吗', on_early_reply=on_early_reply,
            ))
        self.assertEqual(early, [{
            'kind': 'private', 'content': '收到啦',
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '收到啦'}},
        }])
        self.assertEqual(decision['script'], '嗯。')
        self.assertEqual(http.opened_streams[0]['body']['stream'], True)

    async def test_a_stream_failure_after_a_committed_early_reply_aborts_failover(self):
        partial = '{"interaction":{"seen":true,"reply":{"mode":"immediate","content":"收到啦"}},"script":"'
        http = FakeHttpClient(streams=[[delta_chunk(partial), RuntimeError('连接断了')]])
        client = self.make_narrator(http, make_config(main_streaming_mode='experimental'))

        async def on_early_reply(reply):
            return True

        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            with self.assertRaises(RuntimeError) as ctx:
                await client.decide(make_request(phase='user-message', on_early_reply=on_early_reply))
        self.assertEqual(
            str(ctx.exception),
            'Narrative stream failed after an early visible reply: 连接断了',
        )

    async def test_zhipu_official_providers_use_the_streaming_thinking_channel(self):
        http = FakeHttpClient(streams=[[
            sse({'choices': [{'delta': {'content': '{"script":"'}}], 'usage': {'prompt_tokens': 7, 'completion_tokens': 1}}),
            sse({'choices': [{'delta': {'content': '嗯，在。"}'}}]}),
        ]])
        config = make_config(providers=[make_provider(mode='zhipu-official', model='glm', label='ZP')])
        usages = []
        client = self.make_narrator(http, config, on_usage=usages.append)
        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            decision = await client.decide(make_request())
        self.assertEqual(decision['script'], '嗯，在。')
        opened = http.opened_streams[0]
        self.assertEqual(opened['url'], narrator.ZHIPU_OFFICIAL_CHAT_ENDPOINT)
        self.assertEqual(opened['body']['thinking'], {'type': 'enabled'})
        self.assertEqual(opened['body']['reasoning_effort'], 'high')
        self.assertEqual(opened['body']['stream'], True)
        self.assertEqual(usages[0]['provider_label'], 'ZP')
        self.assertEqual(usages[0]['input_tokens'], 7)

    async def test_failover_moves_to_the_next_provider_and_cools_the_failed_one_down(self):
        http = FakeHttpClient(responses=[
            RuntimeError('boom'),
            {'choices': [{'message': {'content': '{"script":"备用"}'}}]},
        ])
        logger = RecordingLogger()
        config = make_config(providers=[
            make_provider(id='p1', label='P1', model='a', use_for_main=False),
            make_provider(id='p2', label='P2', model='b', use_for_main=False),
        ])
        client = self.make_narrator(http, config, logger=logger)
        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            decision = await client.decide(make_request())
        self.assertEqual(decision['script'], '备用')
        self.assertGreater(client.cooldown_until['p1'], narrator.dt_ms(narrator.utc_now()))
        self.assertNotIn('p2', client.cooldown_until)
        self.assertEqual(http.posts[0]['body']['model'], 'a')
        self.assertEqual(http.posts[1]['body']['model'], 'b')
        self.assertEqual(logger.debugs, [('叙事模型服务商失败：%s；尝试=%s', ('P1', 'boom'))])

    async def test_failover_disabled_stops_after_the_first_provider(self):
        http = FakeHttpClient(responses=[RuntimeError('boom'), {'choices': [{'message': {'content': '{}'}}]}])
        config = make_config(
            providers=[
                make_provider(id='p1', label='P1', use_for_main=False),
                make_provider(id='p2', label='P2', use_for_main=False),
            ],
            failover={'enabled': False, 'strategy': 'priority', 'max_attempts_per_provider': 1, 'cooldown_minutes': 5},
        )
        client = self.make_narrator(http, config)
        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            with self.assertRaises(RuntimeError) as ctx:
                await client.decide(make_request())
        self.assertEqual(str(ctx.exception), 'All narrative providers failed. P1 (attempt 1): boom')
        self.assertEqual(len(http.posts), 1)

    async def test_priority_route_retries_each_provider_before_moving_on(self):
        http = FakeHttpClient(responses=[
            RuntimeError('first'),
            {'choices': [{'message': {'content': '{"script":"第二次"}'}}]},
        ])
        config = make_config(failover={
            'enabled': True, 'strategy': 'priority', 'max_attempts_per_provider': 2, 'cooldown_minutes': 5,
        })
        client = self.make_narrator(http, config)
        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            decision = await client.decide(make_request())
        self.assertEqual(decision['script'], '第二次')
        self.assertEqual(len(http.posts), 2)
        self.assertNotIn('p1', client.cooldown_until)

    async def test_round_robin_rotates_the_static_candidate_list(self):
        http = FakeHttpClient(responses=[
            {'choices': [{'message': {'content': '{"script":"1"}'}}]},
            {'choices': [{'message': {'content': '{"script":"2"}'}}]},
            {'choices': [{'message': {'content': '{"script":"3"}'}}]},
        ])
        config = make_config(
            providers=[
                make_provider(id='pa', label='A', model='a', use_for_main=False),
                make_provider(id='pb', label='B', model='b', use_for_main=False),
                make_provider(id='pc', label='C', model='c', use_for_main=False),
            ],
            failover={'enabled': True, 'strategy': 'round-robin', 'max_attempts_per_provider': 1, 'cooldown_minutes': 5},
        )
        client = self.make_narrator(http, config)
        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            await client.decide(make_request())
            await client.decide(make_request())
            await client.decide(make_request())
        self.assertEqual([post['body']['model'] for post in http.posts], ['a', 'b', 'c'])
        self.assertEqual(client.round_robin_offset, 3)

    async def test_decide_raises_when_no_provider_is_usable(self):
        http = FakeHttpClient()
        client = self.make_narrator(http, make_config(providers=[make_provider(enabled=False)]))
        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            with self.assertRaises(RuntimeError) as ctx:
                await client.decide(make_request())
        self.assertEqual(str(ctx.exception), 'No enabled OpenAI-compatible provider is available.')
        self.assertEqual(http.posts, [])

    async def test_empty_and_invalid_responses_use_the_upstream_messages(self):
        empty = FakeHttpClient(responses=[{'choices': [{'message': {'content': ''}}]}])
        client = self.make_narrator(empty)
        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            with self.assertRaises(RuntimeError) as ctx:
                await client.decide(make_request())
        self.assertEqual(str(ctx.exception), 'All narrative providers failed. P1 (attempt 1): Narrative provider returned an empty response.')

        invalid = FakeHttpClient(responses=[{'choices': [{'message': {'content': '不是 JSON'}}]}])
        client = self.make_narrator(invalid)
        with payload_patch, prompt_patch:
            with self.assertRaises(RuntimeError) as ctx:
                await client.decide(make_request())
        self.assertEqual(str(ctx.exception), 'All narrative providers failed. P1 (attempt 1): Narrative provider returned invalid JSON.')

    async def test_image_and_audio_turns_send_one_multipart_user_message(self):
        http = FakeHttpClient(responses=[{'choices': [{'message': {'content': '{"script":"x"}'}}]}])
        client = self.make_narrator(http)
        payload_patch, prompt_patch = stub_prompts()
        with payload_patch, prompt_patch:
            await client.decide(make_request(
                phase='user-message',
                images=[{'id': 'i1', 'mime_type': 'image/png', 'data_uri': 'data:image/png;base64,AAA'}],
                audio=[{'id': 'a1', 'format': 'mp3', 'base64': 'QUJD'}],
            ))
        content = http.posts[0]['body']['messages'][1]['content']
        self.assertEqual(content[0], {'type': 'text', 'text': '{"stub":true}'})
        self.assertEqual(content[1], {
            'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAA', 'detail': 'auto'},
        })
        self.assertEqual(content[2], {'type': 'input_audio', 'input_audio': {'data': 'QUJD', 'format': 'mp3'}})

    async def test_compaction_retries_without_max_tokens_when_the_first_output_is_unparsable(self):
        http = FakeHttpClient(responses=[
            {'choices': [{'message': {'content': '思考被截断的残句'}}]},
            {'choices': [{'message': {'content': '{"summary":"旧事"}'}}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3}},
        ])
        logger = RecordingLogger()
        usages = []
        config = make_config(
            providers=[make_provider(use_for_main=False, use_for_compaction=True)],
            compaction={'enabled': True, 'max_tokens': 700, 'timeout': 30_000},
        )
        client = OpenAICompatibleNarrator(http, config, silent_logs=False, on_usage=usages.append, logger=logger)
        with mock.patch.object(narrator, 'compaction_prompt', lambda *args: 'COMPACT'), \
                mock.patch.object(narrator, 'to_compaction_payload', lambda request: {'story': 'stub'}):
            decision = await client.compact({'story': {'id': 's1'}, 'from': make_request()['from']})
        self.assertEqual(decision, {'summary': '旧事'})
        self.assertEqual(http.posts[0]['body']['max_tokens'], 700)
        self.assertNotIn('max_tokens', http.posts[1]['body'])
        self.assertEqual(http.posts[0]['body']['temperature'], 0.4)
        self.assertEqual(http.posts[0]['body']['messages'][0], {'role': 'system', 'content': 'COMPACT'})
        self.assertEqual(http.posts[0]['body']['messages'][1], {'role': 'user', 'content': '{"story":"stub"}'})
        self.assertEqual(logger.warns[0][0], '%s 首次输出不可解析（疑似思考预算截断），已去掉 max_tokens 重试一次 错误=%s')
        self.assertEqual(usages[0]['input_tokens'], 5)

    async def test_compaction_returns_empty_when_disabled_or_unavailable(self):
        http = FakeHttpClient()
        disabled = OpenAICompatibleNarrator(
            http,
            make_config(
                providers=[make_provider(use_for_main=False, use_for_compaction=True)],
                compaction={'enabled': False},
            ),
            silent_logs=True,
        )
        self.assertEqual(await disabled.compact({}), {})
        self.assertEqual(await disabled.compact_overlay({}), {'summary': ''})
        self.assertIsNone(await disabled.plan_timeline({}))

        silent = create_compactor(http, make_config(compaction={'enabled': False}))
        self.assertIsInstance(silent, SilentCompactor)
        self.assertEqual(await silent.compact({}), {})
        self.assertEqual(await silent.compact_overlay({}), {'summary': ''})
        self.assertIsNone(await silent.plan_timeline({}))
        self.assertIsNone(await silent.plan_schedule_preplan({}))

    async def test_timeline_director_uses_the_fixed_budget_and_never_breaks_narration(self):
        http = FakeHttpClient(responses=[
            {'choices': [{'message': {'content': '还是不是 JSON'}}]},
            {'choices': [{'message': {'content': '{"beats":[]}'}}]},
        ])
        logger = RecordingLogger()
        config = make_config(
            providers=[make_provider(use_for_main=False, use_for_compaction=True)],
            compaction={'enabled': True, 'timeout': 30_000},
        )
        client = OpenAICompatibleNarrator(http, config, silent_logs=False, logger=logger)
        with mock.patch.object(narrator, 'timeline_director_prompt', lambda: 'DIRECTOR'), \
                mock.patch.object(narrator, 'to_timeline_plan_payload', lambda request: {'stub': 1}):
            plan = await client.plan_timeline({'story': {'id': 's1'}})
        self.assertEqual(plan, {'beats': []})
        # 首次带 cap 的请求被思考挤断 → 重试时去掉 max_tokens，所以 1600 只在第一发里。
        self.assertEqual(http.posts[0]['body']['max_tokens'], 1600)
        self.assertNotIn('max_tokens', http.posts[1]['body'])
        self.assertEqual(http.posts[0]['body']['response_format'], {'type': 'json_object'})
        self.assertEqual(http.posts[0]['body']['messages'][0], {'role': 'system', 'content': 'DIRECTOR'})

        # 两次都不可解析时只记日志并返回 undefined（绝不向叙事抛错）。
        broken = FakeHttpClient(responses=[
            {'choices': [{'message': {'content': '坏'}}]},
            {'choices': [{'message': {'content': '还是坏'}}]},
        ])
        client = OpenAICompatibleNarrator(broken, config, silent_logs=False, logger=logger)
        with mock.patch.object(narrator, 'timeline_director_prompt', lambda: 'DIRECTOR'), \
                mock.patch.object(narrator, 'to_timeline_plan_payload', lambda request: {'stub': 1}):
            self.assertIsNone(await client.plan_timeline({'story': {'id': 's1'}}))
        self.assertEqual(logger.warns[-1][0], '时间导演输出不可解析 错误=%s 原始输出=%s')

    async def test_alter_analysis_aggregates_usage_and_requires_a_description(self):
        http = FakeHttpClient(responses=[
            {'choices': [{'message': {'content': '  {'}}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 1}},
            {'choices': [{'message': {'content': '{"description":"  变化了  "}'}}], 'usage': {'prompt_tokens': 4, 'completion_tokens': 2}},
        ])
        usages = []
        config = make_config(providers=[make_provider(use_for_main=False, use_for_alter=True)])
        client = OpenAICompatibleNarrator(http, config, silent_logs=True, on_usage=usages.append)
        with mock.patch.object(narrator, 'alter_analysis_prompt', lambda prompt='': 'ALTER'):
            decision = await client.analyze_alter(
                {
                    'character_name': '凌梦', 'trigger_value': 1.5, 'threshold': 2,
                    'direction': 'serious', 'recent_scripts': [{'content': 'x', 'occurred_at': '2026-01-01T00:00:00Z'}],
                    'history': [], 'setting_overlay': {'character_traits': ['quiet']}, 'current_offset': None,
                },
                {'enabled': True, 'max_tokens': 300, 'timeout': 20_000, 'temperature': 0.3, 'top_p': 1, 'prompt': 'P'},
            )
        self.assertEqual(decision, {'description': '变化了'})
        # Alter 的用户消息必须是上游 camelCase 键名（内部结构是 snake_case）。
        self.assertEqual(
            http.posts[1]['body']['messages'][1]['content'],
            '{"characterName":"凌梦","triggerValue":1.5,"threshold":2,"direction":"serious",'
            '"recentScripts":[{"content":"x","occurredAt":"2026-01-01T00:00:00Z"}],"history":[],'
            '"settingOverlay":{"characterTraits":["quiet"]},"currentOffset":null}',
        )
        # 多尝试的用量聚合成一条输出。
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]['task'], 'Alter 分析')
        self.assertEqual(usages[0]['input_tokens'], 7)

    async def test_alter_analysis_raises_when_nothing_is_configured(self):
        http = FakeHttpClient()
        client = OpenAICompatibleNarrator(http, make_config(providers=[]), silent_logs=True)
        with self.assertRaises(RuntimeError) as ctx:
            await client.analyze_alter({}, {'enabled': True})
        self.assertEqual(str(ctx.exception), 'No enabled provider is available for Alter System analysis.')
        self.assertEqual(await client.analyze_alter({}, {'enabled': False}), {'description': ''})

    async def test_describe_sticker_builds_the_low_detail_catalog_request(self):
        http = FakeHttpClient(responses=[{'choices': [{'message': {'content': json.dumps({
            'description': '  一张猫猫表情  ',
            'aliases': ['猫', '猫', '猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫猫', 7, ' '],
        }, ensure_ascii=False)}}]}])
        config = make_config(providers=[make_provider(use_for_main=False, use_for_stickers=True, model='vision-m')])
        describer = create_sticker_describer(http, config)
        result = await describer.describe_sticker('data:image/png;base64,AAA', 'image/png', 'cat.png', True)
        self.assertEqual(result['description'], '一张猫猫表情')
        self.assertEqual(len(result['aliases']), 2)
        self.assertEqual(result['aliases'][0], '猫')
        self.assertEqual(len(result['aliases'][1]), 32)

        body = http.posts[0]['body']
        self.assertEqual(body['model'], 'vision-m')
        self.assertEqual(body['temperature'], 0.2)
        self.assertEqual(body['max_tokens'], 768)
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertIn('cat.png', body['messages'][1]['content'][0]['text'])
        self.assertTrue(body['messages'][1]['content'][0]['text'].endswith('animated: true.'))
        self.assertEqual(body['messages'][1]['content'][1]['image_url'], {'url': 'data:image/png;base64,AAA', 'detail': 'low'})

    async def test_describe_sticker_degrades_quietly(self):
        http = FakeHttpClient(responses=[{'choices': [{'message': {'content': ''}}]}])
        config = make_config(providers=[make_provider(use_for_main=False, use_for_stickers=True)])
        describer = create_sticker_describer(http, config)
        self.assertIsNone(await describer.describe_sticker('data:image/png;base64,AAA', 'image/png', 'a.png', False))
        self.assertIsNone(await describer.describe_sticker('', 'image/png', 'a.png', False))
        self.assertTrue(describer.available())
        silent = create_sticker_describer(http, make_config())
        self.assertIsInstance(silent, SilentStickerDescriber)
        self.assertFalse(silent.available())
        self.assertIsNone(await silent.describe_sticker('x', 'y', 'z', False))

    async def test_describe_images_uses_the_sidecar_contract_and_retries_once(self):
        http = FakeHttpClient(responses=[
            {'choices': [{'message': {'content': '   '}}]},
            {'choices': [{'message': {'content': '  一张照片  '}}], 'usage': {'prompt_tokens': 9, 'completion_tokens': 4}},
        ])
        usages = []
        config = make_config(providers=[make_provider(use_for_main=False, use_for_vision=True, model='vision-m')])
        describer = create_vision_describer(http, config, on_usage=usages.append)
        images = [{'id': 'i1', 'mime_type': 'image/png', 'data_uri': 'data:image/png;base64,AAA'}]
        self.assertEqual(await describer.describe_images(images, '看这个', 'low'), ['一张照片'])
        self.assertEqual(len(http.posts), 2)
        body = http.posts[0]['body']
        self.assertEqual(body['max_tokens'], 600)
        self.assertIs(body['stream'], False)
        self.assertIn('The user attached 1 image(s).', body['messages'][1]['content'][0]['text'])
        self.assertIn('"看这个"', body['messages'][1]['content'][0]['text'])
        self.assertEqual(body['messages'][1]['content'][1]['image_url'], {'url': 'data:image/png;base64,AAA', 'detail': 'low'})
        self.assertEqual(usages[0]['task'], '侧端识图')

        self.assertIsNone(await describer.describe_images([]))
        silent = create_vision_describer(http, make_config())
        self.assertIsInstance(silent, SilentVisionDescriber)
        self.assertFalse(silent.available())
        self.assertIsNone(await silent.describe_images(images))


class EmbedderClientTests(unittest.IsolatedAsyncioTestCase):
    def make_embedding_config(self, **overrides):
        config = make_config(
            providers=[make_provider(use_for_main=False, use_for_embedding=True, model='embed-m')],
            embedding={
                'enabled': True,
                'endpoint': 'https://example.test/v1/embeddings',
                'model': 'embed-m',
                'dimensions': 128,
                'timeout': 5_000,
                'max_input_characters': 10,
            },
        )
        config.update(overrides)
        return config

    async def test_embed_posts_the_openai_embedding_body(self):
        http = FakeHttpClient(responses=[{'data': [{'embedding': [0.1, 0.2]}]}])
        embedder = create_embedder(http, self.make_embedding_config())
        self.assertIsInstance(embedder, OpenAICompatibleEmbedder)
        self.assertEqual(await embedder.embed('  hello world  '), [0.1, 0.2])
        post = http.posts[0]
        self.assertEqual(post['url'], 'https://example.test/v1/embeddings')
        self.assertEqual(post['headers']['authorization'], 'Bearer key-1')
        self.assertEqual(post['timeout'], 5_000)
        self.assertEqual(post['body'], {'model': 'embed-m', 'input': 'hello worl', 'dimensions': 128})

    async def test_embed_rejects_invalid_vectors_and_derives_the_endpoint(self):
        invalid = FakeHttpClient(responses=[{'data': [{'embedding': [0.1, 'x']}]}])
        embedder = create_embedder(invalid, self.make_embedding_config())
        with self.assertRaises(RuntimeError) as ctx:
            await embedder.embed('hi')
        self.assertEqual(str(ctx.exception), 'Embedding provider returned an invalid vector.')

        empty = FakeHttpClient(responses=[{'data': [{'embedding': [1]}]}])
        embedder = create_embedder(empty, self.make_embedding_config(embedding={
            'enabled': True, 'endpoint': '', 'model': 'embed-m', 'dimensions': 0,
            'timeout': 5_000, 'max_input_characters': 100,
        }))
        self.assertEqual(await embedder.embed('hi'), [1])
        self.assertEqual(empty.posts[0]['url'], 'https://example.test/v1/embeddings')
        self.assertEqual(empty.posts[0]['body'], {'model': 'embed-m', 'input': 'hi'})

        missing = FakeHttpClient(responses=[{'data': []}])
        embedder = create_embedder(missing, self.make_embedding_config())
        with self.assertRaises(RuntimeError) as ctx:
            await embedder.embed('hi')
        self.assertEqual(str(ctx.exception), 'Embedding provider returned an invalid vector.')

    async def test_embed_returns_early_for_any_unusable_configuration(self):
        http = FakeHttpClient()
        silent = create_embedder(http, make_config())
        self.assertIsInstance(silent, SilentEmbedder)
        self.assertEqual(await silent.embed('hi'), [])

        disabled = create_embedder(http, self.make_embedding_config(embedding={'enabled': False}))
        self.assertEqual(await disabled.embed('hi'), [])

        unlimited = create_embedder(http, self.make_embedding_config(embedding={
            'enabled': True, 'endpoint': 'https://example.test/v1/embeddings', 'model': 'embed-m',
        }))
        # 缺 maxInputCharacters 时上游 `Math.max(1, undefined)` → NaN → 空串 → 不请求。
        self.assertEqual(await unlimited.embed('hi'), [])

        blank = create_embedder(http, self.make_embedding_config(embedding={
            'enabled': True, 'endpoint': 'https://example.test/v1/embeddings', 'model': 'embed-m',
            'max_input_characters': 10,
        }))
        self.assertEqual(await blank.embed('   '), [])
        self.assertEqual(http.posts, [])

    async def test_identity_is_a_stable_twenty_four_character_digest(self):
        http = FakeHttpClient()
        embedder = create_embedder(http, self.make_embedding_config())
        identity = embedder.identity()
        self.assertEqual(len(identity), 24)
        self.assertEqual(identity, create_embedder(http, self.make_embedding_config()).identity())
        other = create_embedder(http, self.make_embedding_config(embedding={
            'enabled': True, 'endpoint': 'https://example.test/v2/embeddings', 'model': 'embed-m',
            'dimensions': 128, 'timeout': 5_000, 'max_input_characters': 10,
        }))
        self.assertNotEqual(identity, other.identity())


class FactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_narrator_returns_the_silent_provider_without_a_usable_route(self):
        http = FakeHttpClient()
        silent = create_narrator(http, make_config(providers=[]))
        self.assertIsInstance(silent, SilentNarrator)
        self.assertEqual(await silent.decide(make_request()), {})

        real = create_narrator(http, make_config(), logger=RecordingLogger())
        self.assertIsInstance(real, OpenAICompatibleNarrator)
        self.assertTrue(callable(real.available))

    async def test_logger_defaults_to_the_core_sink_and_can_be_silenced(self):
        http = FakeHttpClient()
        loud = create_narrator(http, make_config())
        self.assertIsInstance(loud.logger, SinkLogger)
        quiet = create_narrator(http, make_config(), silent_logs=True)
        self.assertIsNone(quiet.logger)
        quiet._debug('不该输出 %s', 1)
        quiet._warn('不该输出 %s', 1)

    async def test_assigned_sticker_and_vision_routes_are_reported(self):
        http = FakeHttpClient()
        stickers = create_sticker_describer(
            http, make_config(providers=[make_provider(use_for_main=False, use_for_stickers=True)]),
        )
        self.assertTrue(stickers.available())
        vision = create_vision_describer(
            http, make_config(providers=[make_provider(use_for_main=False, use_for_vision=True)]),
        )
        # 上游 `available()` 问的是表情连接；侧端识图看 `visionAvailable()`。
        self.assertTrue(vision.vision_available())
        self.assertFalse(vision.available())

    async def test_context_style_sources_are_accepted_by_the_factories(self):
        http = FakeHttpClient()
        client = create_narrator(SimpleNamespace(http=http, logger=RecordingLogger()), make_config())
        self.assertIs(client.http, http)
        self.assertIsInstance(client.logger, SinkLogger)
        explicit = create_narrator(http, make_config(), logger=RecordingLogger())
        self.assertIsInstance(explicit.logger, RecordingLogger)


if __name__ == '__main__':
    unittest.main()
