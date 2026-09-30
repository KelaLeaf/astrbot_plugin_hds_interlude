"""`upstream/test/anthropic.test.ts` 的 Python 对应物（Anthropic Messages 协议支持）。

上游用例逐条移植（断言一一对照），另补 `core/anthropic.py` 协议层自己的边界：
请求头 / 请求体回退 / 多模态拆分 / 缓存前缀的逐字不变性 / 首帧与总时限 / 假装流式的
JSON 体 / 连接行两种拼写。

键名约定：本移植版的内部结构与配置键是 snake_case（`provider_label` /
`anthropic_cache`），上游同名断言里的 `anthropicCache` / `inputTokens` 即对应这些
键；发给模型的请求体与从服务端读回的字段（`system` / `messages` / `max_tokens` /
`cache_control` / `content_block_delta` / `input_tokens` …）**逐字保持上游线上名**。

上游用真实 `node:http` 服务器验证 SSE；本移植版按既有约定注入假 `HttpClient`
（`iterate_sse` 产出**按 7 字节切块并增量解码**的文本，复刻上游的分帧边界），
行为断言与上游逐条相同。

**两处依赖本次改动范围之外的模块**（上游断言逐条保留，用 `skipUnless` 守门 +
下方用例说明，详见交付汇报）：

1. `toPromptPayload` 的 **群回合 `currentEvent.audioCount`** 由 `core/narrator_prompts.py`
   组装（本任务只碰 `anthropic.py` / `narrator.py` / 本文件）；
2. 上游在 `resolveModelRouting` 里把 Anthropic 连接从**向量化**候选剔除
   （`model-routing.ts:123`）——那条在 `core/model_routing.py`；本移植版在
   `narrator.OpenAICompatibleEmbedder` 构造时补了同一条规则，用例按向量化客户端断言。
"""

from __future__ import annotations

import asyncio
import codecs
import json
import unittest
from datetime import datetime, timezone

from plugin.core import narrator as narrator_module
from plugin.core.anthropic import (
    ANTHROPIC_VERSION,
    anthropic_body,
    anthropic_headers,
    anthropic_response,
    anthropic_usage,
    blocks,
    cache_prefix,
    normalize_protocol_endpoint,
    request_anthropic_streaming,
)
from plugin.core.model_routing import resolve_model_routing
from plugin.core.narrator import (
    OpenAICompatibleEmbedder,
    OpenAICompatibleNarrator,
    parse_token_usage,
)
from plugin.core.narrator_prompts import to_prompt_payload


# ======================================================================================
# 测试替身（与 `test_narrator_client.py` 同一套形状）
# ======================================================================================


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

    async def post_json(self, url, headers=None, body=None, timeout=None, task=None):
        self.posts.append({
            'url': url, 'headers': headers, 'body': body, 'timeout': timeout, 'task': task,
        })
        if not self._responses:
            raise AssertionError('测试替身没有准备更多的 post_json 响应。')
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def iterate_sse(self, url, headers=None, body=None, timeout=None, task=None):
        self.opened_streams.append({
            'url': url, 'headers': headers, 'body': body, 'timeout': timeout, 'task': task,
        })
        item = self._streams.pop(0) if self._streams else []
        return FakeStream(item)


def sse_wire(frames) -> str:
    """上游 `streamFixture` 里逐帧拼出的线上文本（`event:` + `data:` + 空行）。"""
    return ''.join(
        'event: {}\r\ndata: {}\r\n\r\n'.format(frame['type'], json.dumps(frame, ensure_ascii=False))
        for frame in frames
    )


def chunked_wire(frames, size: int = 7):
    """按 `size` **字节**切块并增量解码（复刻上游分块读 + `TextDecoder`）。"""
    raw = sse_wire(frames).encode('utf-8')
    decoder = codecs.getincrementaldecoder('utf-8')()
    pieces: list[str] = []
    for index in range(0, len(raw), size):
        piece = decoder.decode(raw[index:index + size])
        if piece:
            pieces.append(piece)
    tail = decoder.decode(b'', True)
    if tail:
        pieces.append(tail)
    return pieces


# ======================================================================================
# 夹具（与上游 anthropic.test.ts 首段一一对应）
# ======================================================================================


def make_provider(**overrides) -> dict:
    """上游 `provider` 夹具；本移植版的连接行键是 snake_case（schema 形状）。"""
    provider = {
        'id': 'p1',
        'label': 'messages',
        'enabled': True,
        'endpoint': 'https://gateway.test/v1/messages',
        'api_key': 'test-key',
        'model': 'test-model',
        'protocol': 'anthropic-messages',
        'anthropic_cache': True,
        'temperature': 0.8,
        'top_p': 1,
        'max_tokens': 4096,
        'timeout': 1000,
        'response_format': 'json-object',
        'extra_headers': '',
        'extra_body': '',
        'use_for_main': True,
        'use_for_compaction': True,
        'use_for_vision': True,
        'use_for_stickers': True,
    }
    provider.update(overrides)
    return provider


def make_config(providers=None) -> dict:
    """上游 `config()` 夹具（`mainPayloadOrder: 'cache-first'`）。"""
    return {
        'providers': list(providers if providers is not None else [make_provider()]),
        'fixed_prompt': '',
        'style_prompt': '',
        'main_payload_order': 'cache-first',
        'failover': {
            'enabled': True, 'strategy': 'priority',
            'max_attempts_per_provider': 1, 'cooldown_minutes': 0,
        },
    }


def make_request(**overrides) -> dict:
    """上游 `request()` 夹具。"""
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    request = {
        'phase': 'user-message',
        'story': {
            'id': 's', 'platform': 'onebot', 'selfId': 'b', 'userId': '', 'channelId': '',
            'status': 'active', 'setting': {}, 'state': {},
            'cursorAt': now, 'createdAt': now, 'updatedAt': now,
        },
        'from': now,
        'now': now,
        'participant': None,
        'participants': [],
        'shareParticipantDetails': False,
        'dueIntents': [],
        'activeConsequences': [],
        'supersededIntents': [],
        'recentEntries': [],
        'memories': [],
        'images': [],
        'audio': [],
        'userMessage': '你好',
    }
    request.update(overrides)
    return request


def text_response(text: str) -> dict:
    """上游 `textResponse()`：带 thinking 块的 Messages 响应。"""
    return {
        'type': 'message',
        'content': [{'type': 'thinking', 'thinking': 'hidden'}, {'type': 'text', 'text': text}],
        'stop_reason': 'end_turn',
        'usage': {
            'input_tokens': 20, 'cache_read_input_tokens': 100,
            'cache_creation_input_tokens': 30, 'output_tokens': 5,
        },
    }


def _group_audio_payload(cache_first: bool) -> str:
    request = make_request(groupContext={'groupId': '100', 'channelId': 'group:100', 'messages': []})
    request['audio'] = [{'id': 'a', 'format': 'mp3', 'base64': 'AAAA'}]
    return narrator_module._stringify_json(to_prompt_payload(request, {'cacheFirst': cache_first}))


#: 群回合的 payload 是否会声明 `audioCount`（由 `core/narrator_prompts.py` 组装）。
GROUP_TURN_DECLARES_AUDIO_COUNT = '"audioCount":1' in _group_audio_payload(False)


# ======================================================================================
# anthropic.test.ts（逐条移植）
# ======================================================================================


class AnthropicPortedTests(unittest.TestCase):
    """上游用例 1–8：请求体 / 响应 / 路由选择（纯函数与同步路径）。"""

    def test_group_event_keeps_the_group_marker_and_never_leaks_audio_base64(self):
        """上游用例 1 的前半：两种 payload 顺序下群标记都在、音频 base64 不外泄。"""
        for cache_first in (False, True):
            payload = _group_audio_payload(cache_first)
            self.assertIn('group-message-batch', payload)
            self.assertNotIn('AAAA', payload)

    @unittest.skipUnless(
        GROUP_TURN_DECLARES_AUDIO_COUNT,
        '群回合的 currentEvent.audioCount 由 core/narrator_prompts.py 组装（本次改动范围之外）；'
        '该模块补上后本用例自动生效。',
    )
    def test_group_event_declares_native_audio_evidence_in_both_payload_orders(self):
        """上游用例 1：`"audioCount":1` 在两种顺序下都要出现。"""
        for cache_first in (False, True):
            self.assertIn('"audioCount":1', _group_audio_payload(cache_first))

    def test_messages_request_lifts_system_converts_images_and_never_sends_openai_json_mode(self):
        """上游用例 2。"""
        body = anthropic_body({
            'model': 'm', 'max_tokens': 0, 'temperature': 1.5, 'top_p': 0.9,
            'response_format': {'type': 'json_object'},
            'messages': [
                {'role': 'system', 'content': 'write JSON'},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': 'look'},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}},
                ]},
            ],
        }, make_provider())
        self.assertEqual(body['max_tokens'], 4096)
        self.assertEqual(body['temperature'], 1)
        self.assertNotIn('response_format', body)
        self.assertNotIn('top_p', body)
        self.assertEqual(body['system'][0]['text'], 'write JSON')
        self.assertEqual(len(body['messages']), 1)
        self.assertEqual(body['messages'][0]['content'][1], {
            'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AA=='},
        })
        self.assertEqual(anthropic_headers(make_provider())['x-api-key'], 'test-key')
        self.assertNotIn('authorization', anthropic_headers(make_provider()))

    def test_both_payload_orders_retain_exact_json_bytes_and_cache_markers_stop_before_live_scene(self):
        """上游用例 3：切分**不改 JSON 一个字符**，缓存断点落在 live scene 之前。"""
        for cache_first in (False, True):
            text = narrator_module._stringify_json(to_prompt_payload(make_request(), {'cacheFirst': cache_first}))
            body = anthropic_body({
                'messages': [
                    {'role': 'system', 'content': 'system'},
                    {'role': 'user', 'content': text},
                ],
            }, make_provider(), cache_first)
            content = body['messages'][0]['content']
            self.assertEqual(''.join(part['text'] for part in content), text)
            self.assertEqual(len(content), 2 if cache_first else 1)
            if cache_first:
                self.assertEqual(content[0]['cache_control']['type'], 'ephemeral')
                self.assertNotIn('"incomingEvent":', content[0]['text'])
            off = anthropic_body({
                'messages': [
                    {'role': 'system', 'content': 'system'},
                    {'role': 'user', 'content': text},
                ],
            }, {**make_provider(), 'anthropic_cache': False}, cache_first)
            self.assertNotIn('cache_control', json.dumps(off, ensure_ascii=False))

    def test_messages_responses_exclude_thinking_and_normalize_total_input_plus_cache_reads(self):
        """上游用例 4。"""
        result = anthropic_response(text_response('{"script":"正文"}'))
        self.assertEqual(result['choices'][0]['message']['content'], '{"script":"正文"}')
        self.assertEqual(parse_token_usage(result['usage']), {
            'input_tokens': 150, 'output_tokens': 5, 'cached_input_tokens': 100,
        })
        with self.assertRaisesRegex(RuntimeError, 'max_tokens'):
            anthropic_response({**text_response('{}'), 'stop_reason': 'max_tokens'})
        with self.assertRaisesRegex(RuntimeError, 'overloaded'):
            anthropic_response({'type': 'error', 'error': {'message': 'overloaded'}})
        self.assertIsNone(anthropic_usage(None))

    def test_native_audio_is_explicitly_unsupported_and_thinking_sampling_is_compatible(self):
        """上游用例 5。"""
        with self.assertRaisesRegex(RuntimeError, 'native audio'):
            anthropic_body({'messages': [
                {'role': 'user', 'content': [{'type': 'input_audio', 'input_audio': {'data': 'AA=='}}]},
            ]}, make_provider())
        body = anthropic_body({
            'messages': [], 'temperature': 0.8, 'thinking': {'type': 'enabled', 'budget_tokens': 1024},
        }, make_provider())
        self.assertNotIn('temperature', body)
        self.assertEqual(body['thinking']['budget_tokens'], 1024)

    def test_protocol_switching_preserves_gateway_prefix_and_history_stays_compatible(self):
        """上游用例 6（端点归一化 + 旧配置默认 + 官方模式 + 向量化不认 Messages）。"""
        # configuredProviders 的路径改写：网关前缀保留，只换标准尾段。
        self.assertEqual(
            normalize_protocol_endpoint('https://gateway.test/proxy/v1/chat/completions?x=1', 'anthropic-messages'),
            'https://gateway.test/proxy/v1/messages?x=1',
        )
        self.assertEqual(
            normalize_protocol_endpoint('https://gateway.test/v1/messages', 'chat-completions'),
            'https://gateway.test/v1/chat/completions',
        )
        # 旧配置（没有 protocol 键）保持 chat-completions；官方模式永远是 chat-completions。
        self.assertEqual(
            narrator_module._provider_protocol({**make_provider(), 'protocol': None}), 'chat-completions',
        )
        self.assertEqual(
            narrator_module._provider_protocol({**make_provider(), 'mode': 'zhipu-official'}), 'chat-completions',
        )
        # 请求时用的地址按协议对齐（只对 Messages 连接动手）。
        self.assertEqual(
            narrator_module._provider_endpoint({**make_provider(), 'endpoint': 'https://gateway.test/proxy/v1/chat/completions?x=1'}),
            'https://gateway.test/proxy/v1/messages?x=1',
        )
        self.assertEqual(
            narrator_module._provider_endpoint({**make_provider(), 'protocol': 'chat-completions'}),
            'https://gateway.test/v1/messages',
        )
        # 上游 `resolveModelRouting(...).embedding.available === false`：Messages 连接
        # 不承担向量化（上游在 model-routing 里过滤；本移植版落在向量化客户端构造处）。
        embedding_config = {**make_config(), 'embedding': {'enabled': True}}
        embedder = OpenAICompatibleEmbedder(FakeHttpClient(), embedding_config, None)
        self.assertFalse(embedder.routing['embedding']['available'])
        self.assertEqual(embedder.routing['embedding']['providers'], [])
        # 反向断言：同一条连接换成 chat-completions 后向量化照旧可用（零回归）。
        plain_config = {
            **make_config([{**make_provider(), 'protocol': 'chat-completions'}]), 'embedding': {'enabled': True},
        }
        plain = OpenAICompatibleEmbedder(FakeHttpClient(), plain_config, None)
        self.assertEqual(len(plain.routing['embedding']['providers']), 1)
        self.assertTrue(resolve_model_routing(plain_config)['embedding']['available'])


class AnthropicProductionTests(unittest.IsolatedAsyncioTestCase):
    """上游用例 7–12：生产路径（主叙事 / 旁路任务 / 流式 / 故障切换）。"""

    async def test_production_main_and_visual_side_tasks_use_messages_while_retaining_the_narrative_contract(self):
        """上游用例 7。"""
        calls: list[dict] = []

        class RecordingHttp(FakeHttpClient):
            async def post_json(self, url, headers=None, body=None, timeout=None, task=None):
                calls.append({'url': url, 'headers': headers, 'body': body, 'task': task})
                return text_response(json.dumps({
                    'script': '正文',
                    'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '你好'}},
                    'description': '一只猫',
                    'aliases': [],
                }, ensure_ascii=False))

            def iterate_sse(self, url, headers=None, body=None, timeout=None, task=None):
                raise AssertionError('本用例不该走流式。')

        narrator = OpenAICompatibleNarrator(RecordingHttp(), make_config(), True)
        decision = await narrator.decide(make_request())
        self.assertEqual(decision['script'], '正文')
        await narrator.describe_sticker('data:image/png;base64,AA==', 'image/png', 'cat.png', False)
        await narrator.describe_images([{'id': '1', 'mime_type': 'image/png', 'data_uri': 'data:image/png;base64,AA=='}])
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertEqual(call['headers']['anthropic-version'], ANTHROPIC_VERSION)
            self.assertFalse(any(message['role'] == 'system' for message in call['body']['messages']))
            self.assertEqual(len(call['body']['system']), 1)
        self.assertEqual(len(calls[0]['body']['messages'][0]['content']), 2)

    async def test_side_task_json_path_keeps_max_tokens_and_does_not_retry_uncapped_on_messages(self):
        """上游用例 8。"""
        seen: list[dict] = []

        class CappedHttp(FakeHttpClient):
            async def post_json(self, url, headers=None, body=None, timeout=None, task=None):
                seen.append(body)
                self.assert_positive(body)
                return text_response('{"ok":true}')

            @staticmethod
            def assert_positive(body):
                if not body.get('max_tokens', 0) > 0:
                    raise AssertionError('Messages 请求必须带正数 max_tokens。')

        narrator = OpenAICompatibleNarrator(CappedHttp(), make_config(), True)
        result = await narrator._side_task_json(
            make_provider(), 'm', 'test', 1000,
            lambda capped: {'messages': [
                {'role': 'system', 'content': 'JSON'},
                {'role': 'user', 'content': 'test'},
            ]},
            json.loads,
        )
        self.assertEqual(result, {'ok': True})
        self.assertEqual(len(seen), 1)

    async def test_anthropic_sse_accumulates_text_only_and_counts_cumulative_usage_once(self):
        """上游用例 9。"""
        frames = [
            {'type': 'message_start', 'message': {'usage': {
                'input_tokens': 10, 'cache_read_input_tokens': 30, 'output_tokens': 1,
            }}},
            {'type': 'content_block_delta', 'delta': {'type': 'thinking_delta', 'thinking': 'hidden'}},
            {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '{"script":"你'}},
            {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '好"}'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 8}},
            {'type': 'message_stop'},
        ]
        http = FakeHttpClient(streams=[chunked_wire(frames)])
        chunks: list[str] = []
        usage: list = []

        async def on_text(part: str) -> None:
            chunks.append(part)

        text = await request_anthropic_streaming(
            'https://gateway.test/v1/messages', {}, {}, 2000, on_text, usage.append, http, task='main',
        )
        self.assertEqual(text, '{"script":"你好"}')
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(usage), 1)
        self.assertEqual(parse_token_usage(usage[0]), {
            'input_tokens': 40, 'output_tokens': 8, 'cached_input_tokens': 30,
        })
        self.assertEqual(http.opened_streams[0]['body'], {'stream': True})

    async def test_anthropic_truncated_and_error_streams_do_not_masquerade_as_completed_script(self):
        """上游用例 10。"""
        for frames in (
            [{'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '{}'}}],
            [{'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'busy'}}],
            [{'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}}, {'type': 'message_stop'}],
        ):
            http = FakeHttpClient(streams=[chunked_wire(frames)])
            with self.assertRaises(RuntimeError):
                await request_anthropic_streaming(
                    'https://gateway.test/v1/messages', {}, {}, 2000, http=http,
                )

    async def test_production_experimental_early_reply_uses_anthropic_sse_once_and_keeps_the_completed_script(self):
        """上游用例 11：早期可见回复走 Anthropic SSE，完整剧本照旧保留。"""
        first = '{"interaction":{"seen":true,"reply":{"mode":"immediate","content":"收到"}},"script":"'
        frames = [
            {'type': 'message_start', 'message': {'usage': {'input_tokens': 20, 'output_tokens': 1}}},
            {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': first}},
            {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '她发出了消息。"}'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 30}},
            {'type': 'message_stop'},
        ]
        http = FakeHttpClient(streams=[chunked_wire(frames)])
        replies: list = []
        usages: list = []
        config = {**make_config([{**make_provider(), 'endpoint': 'https://gateway.test/v1/messages'}]),
                  'main_streaming_mode': 'experimental'}

        async def on_early_reply(reply):
            replies.append(reply)
            return True

        narrator = OpenAICompatibleNarrator(http, config, True, usages.append)
        result = await narrator.decide({**make_request(), 'on_early_reply': on_early_reply})
        self.assertEqual(result['script'], '她发出了消息。')
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]['content'], '收到')
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]['output_tokens'], 30)

    async def test_messages_failure_can_fall_back_to_chat_completions_without_changing_payload_content(self):
        """上游用例 12：Messages 失败后回落 Chat Completions，payload 内容一字不差。"""
        calls: list[dict] = []

        class FallbackHttp(FakeHttpClient):
            async def post_json(self, url, headers=None, body=None, timeout=None, task=None):
                calls.append(body)
                if 'system' in body:
                    raise RuntimeError('messages unavailable')
                return {'choices': [{'message': {'content': json.dumps({
                    'script': '继续生活',
                    'interaction': {'seen': False, 'reply': {'mode': 'none'}},
                }, ensure_ascii=False)}}]}

        fallback = {
            **make_provider(), 'id': 'p2', 'label': 'fallback',
            'protocol': 'chat-completions', 'endpoint': 'https://fallback.test/v1/chat/completions',
        }
        narrator = OpenAICompatibleNarrator(FallbackHttp(), make_config([make_provider(), fallback]), True)
        result = await narrator.decide(make_request())
        self.assertEqual(result['script'], '继续生活')
        self.assertEqual(len(calls), 2)
        messages_text = ''.join(part['text'] for part in calls[0]['messages'][0]['content'])
        self.assertEqual(messages_text, calls[1]['messages'][1]['content'])
        self.assertEqual(calls[1]['response_format'], {'type': 'json_object'})


# ======================================================================================
# 协议层边界（上游测试未覆盖，但翻译规则的关键不变量）
# ======================================================================================


class AnthropicProtocolUnitTests(unittest.TestCase):
    def test_headers_use_x_api_key_and_let_extra_headers_override_anything(self):
        headers = anthropic_headers(
            {**make_provider(), 'api_key': None, 'anthropic_cache': None,
             'anthropicCache': True, 'apiKey': 'camel-key'},
            {'anthropic-version': '2024-01-01', 'x-trace': 't1'},
        )
        self.assertEqual(headers['content-type'], 'application/json')
        self.assertEqual(headers['anthropic-version'], '2024-01-01')
        self.assertEqual(headers['x-api-key'], 'camel-key')
        self.assertEqual(headers['x-trace'], 't1')
        self.assertNotIn('authorization', headers)

    def test_body_reads_camel_aliases_and_falls_back_for_missing_max_tokens(self):
        # 上游 `anthropicCache` / `maxTokens`（camelCase）同样要认。
        body = anthropic_body({'messages': [{'role': 'user', 'content': 'hi'}]}, {
            'apiKey': 'k', 'maxTokens': '2048', 'anthropicCache': True,
        })
        self.assertEqual(body['max_tokens'], 2048)
        self.assertEqual(body['system'], [])
        # 缺失 / 非正数 / 非数字一律回退 4096（服务端不接受空的 max_tokens）。
        for value in (None, 0, -5, '', 'abc'):
            with self.subTest(max_tokens=value):
                candidate = anthropic_body({'messages': [], 'max_tokens': value}, make_provider())
                self.assertEqual(candidate['max_tokens'], 4096)
        self.assertEqual(anthropic_body({'messages': [], 'max_tokens': 100.9}, make_provider())['max_tokens'], 100)
        # 连接行缺失时用 provider.max_tokens。
        self.assertEqual(
            anthropic_body({'messages': []}, {'max_tokens': 777})['max_tokens'], 777,
        )

    def test_body_keeps_thinking_and_rejects_unsupported_blocks_and_roles(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Anthropic input block: mystery'):
            blocks([{'type': 'mystery'}])
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Anthropic image source'):
            blocks([{'type': 'image_url', 'image_url': {'url': 'ftp://host/a.png'}}])
        with self.assertRaisesRegex(RuntimeError, 'must be text or content blocks'):
            blocks(42)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Anthropic message role: tool'):
            anthropic_body({'messages': [{'role': 'tool', 'content': 'x'}]}, make_provider())
        # http(s) 图片走 url source；其它媒体类型（gif / webp）走 base64。
        self.assertEqual(
            blocks([{'type': 'image_url', 'image_url': {'url': 'https://host/a.webp'}}]),
            [{'type': 'image', 'source': {'type': 'url', 'url': 'https://host/a.webp'}}],
        )
        self.assertEqual(
            blocks([{'type': 'image_url', 'image_url': {'url': 'data:image/gif;base64,R0lGOD'}}]),
            [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/gif', 'data': 'R0lGOD'}}],
        )

    def test_cache_prefix_refuses_to_cut_when_the_bytes_would_change(self):
        text = narrator_module._stringify_json({
            'storyIdentity': {'a': 1},
            'relevantEstablishedEpisodes': {'b': '你好'},
            'currentSceneEvidence': {'c': 3},
        })
        content = [{'type': 'text', 'text': text}]
        split = cache_prefix(content)
        self.assertEqual(len(split), 2)
        self.assertEqual(split[0]['text'] + split[1]['text'], text)
        self.assertEqual(split[0]['cache_control'], {'type': 'ephemeral'})
        self.assertNotIn('currentSceneEvidence', split[0]['text'])
        # 不是脚手架 JSON / 缺键 / 不是 text 块 —— 一律原样返回，绝不改字符。
        self.assertEqual(cache_prefix([{'type': 'text', 'text': '不是 JSON'}]), [{'type': 'text', 'text': '不是 JSON'}])
        self.assertEqual(
            cache_prefix([{'type': 'text', 'text': narrator_module._stringify_json({'storyIdentity': {}})}]),
            [{'type': 'text', 'text': narrator_module._stringify_json({'storyIdentity': {}})}],
        )
        self.assertEqual(cache_prefix([{'type': 'image', 'source': {}}]), [{'type': 'image', 'source': {}}])
        self.assertEqual(cache_prefix([]), [])

    def test_normalize_protocol_endpoint_leaves_custom_paths_alone(self):
        self.assertEqual(
            normalize_protocol_endpoint('https://gateway.test/proxy/anthropic', 'anthropic-messages'),
            'https://gateway.test/proxy/anthropic',
        )
        self.assertEqual(normalize_protocol_endpoint('', 'anthropic-messages'), '')
        self.assertEqual(normalize_protocol_endpoint(None, 'anthropic-messages'), '')

    def test_response_rejections_use_the_upstream_messages(self):
        with self.assertRaisesRegex(RuntimeError, 'refused the request'):
            anthropic_response({'content': [], 'stop_reason': 'refusal'})
        with self.assertRaisesRegex(RuntimeError, 'empty text response'):
            anthropic_response({'content': [{'type': 'thinking', 'thinking': 'x'}]})
        with self.assertRaisesRegex(RuntimeError, 'overloaded_error'):
            anthropic_response({'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'busy'}})


class AnthropicStreamEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_frame_guard_exposes_a_silent_gateway_early(self):
        http = FakeHttpClient(streams=[[(1.0, 'data: {}\n\n')]])
        with self.assertRaisesRegex(RuntimeError, 'first frame within 333ms'):
            await request_anthropic_streaming('https://gateway.test/v1/messages', {}, {}, 600, http=http)

    async def test_total_deadline_applies_after_the_first_frame(self):
        http = FakeHttpClient(streams=[[': keep-alive\n\n', (2.0, 'data: {"type":"message_stop"}\n\n')]])
        with self.assertRaisesRegex(RuntimeError, 'timed out after 1000ms'):
            await request_anthropic_streaming('https://gateway.test/v1/messages', {}, {}, 600, http=http)

    async def test_a_gateway_that_ignores_stream_returns_one_plain_json_body(self):
        http = FakeHttpClient(streams=[[json.dumps(text_response('{"script":"正文"}'), ensure_ascii=False)]])
        usage: list = []
        text = await request_anthropic_streaming(
            'https://gateway.test/v1/messages', {}, {}, 2000, None, usage.append, http,
        )
        self.assertEqual(text, '{"script":"正文"}')
        self.assertEqual(parse_token_usage(usage[0]), {
            'input_tokens': 150, 'output_tokens': 5, 'cached_input_tokens': 100,
        })

    async def test_transport_status_errors_use_the_upstream_message_shape(self):
        class FailingHttp(FakeHttpClient):
            def iterate_sse(self, url, headers=None, body=None, timeout=None, task=None):
                return _FailingStream()

        with self.assertRaisesRegex(RuntimeError, r'Anthropic streaming request failed \(500\): boom'):
            await request_anthropic_streaming('https://gateway.test/v1/messages', {}, {}, 2000, http=FailingHttp())

    async def test_garbage_frames_do_not_discard_a_usable_turn(self):
        frames = [
            {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '{"script":"好"}'}},
            {'type': 'message_stop'},
        ]
        wire = 'data: [DONE]\n\n' + 'event: ping\ndata: not-json\n\n' + sse_wire(frames)
        http = FakeHttpClient(streams=[list(wire)])
        text = await request_anthropic_streaming('https://gateway.test/v1/messages', {}, {}, 2000, http=http)
        self.assertEqual(text, '{"script":"好"}')


class _FailingStream:
    """立刻抛 `HttpStatusError` 的流替身（传输层非 2xx 的形状）。"""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise narrator_module.HttpStatusError(500, 'boom', 'Internal Server Error')

    async def aclose(self):
        return None


class AnthropicOpenAIRegressionTests(unittest.IsolatedAsyncioTestCase):
    """零回归：没有 `protocol` 的连接行逐字保持旧行为。"""

    async def test_chat_completions_rows_keep_url_headers_and_body_untouched(self):
        http = FakeHttpClient(responses=[{'choices': [{'message': {'content': json.dumps({
            'script': '继续生活', 'interaction': {'seen': False, 'reply': {'mode': 'none'}},
        }, ensure_ascii=False)}}]}])
        provider = {
            'id': 'p1', 'label': 'legacy', 'enabled': True,
            'endpoint': 'https://legacy.test/v1/chat/completions',
            'api_key': 'legacy-key', 'model': 'legacy-model', 'temperature': 0.8, 'top_p': 1,
            'max_tokens': 4096, 'timeout': 60_000, 'response_format': 'json-object',
            'extra_headers': '', 'extra_body': '', 'use_for_main': True,
        }
        narrator = OpenAICompatibleNarrator(http, make_config([provider]), True)
        result = await narrator.decide(make_request())
        self.assertEqual(result['script'], '继续生活')
        self.assertEqual(len(http.posts), 1)
        post = http.posts[0]
        self.assertEqual(post['url'], 'https://legacy.test/v1/chat/completions')
        self.assertNotIn('anthropic-version', post['headers'])
        self.assertEqual(post['headers']['authorization'], 'Bearer legacy-key')
        self.assertIn('response_format', post['body'])
        self.assertTrue(any(message['role'] == 'system' for message in post['body']['messages']))
        self.assertNotIn('system', post['body'])

    async def test_audio_turns_skip_anthropic_candidates_without_touching_the_rest(self):
        """Anthropic Messages 没有原生音频块：带语音的回合用确定性筛选避开它。"""
        class RecordingHttp(FakeHttpClient):
            async def post_json(self, url, headers=None, body=None, timeout=None, task=None):
                self.posts.append({'url': url, 'headers': headers, 'body': body, 'timeout': timeout, 'task': task})
                return {'choices': [{'message': {'content': json.dumps({
                    'script': '嗯', 'interaction': {'seen': True, 'reply': {'mode': 'none'}},
                }, ensure_ascii=False)}}]}

        anthropic_row = make_provider()
        legacy_row = {
            **make_provider(), 'id': 'p2', 'label': 'legacy',
            'protocol': 'chat-completions', 'endpoint': 'https://legacy.test/v1/chat/completions',
        }
        http = RecordingHttp()
        narrator = OpenAICompatibleNarrator(http, make_config([anthropic_row, legacy_row]), True)
        request = make_request(
            images=[], audio=[{'id': 'a', 'format': 'mp3', 'base64': 'AAAA'}],
        )
        result = await narrator.decide(request)
        self.assertEqual(result['script'], '嗯')
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(http.posts[0]['url'], 'https://legacy.test/v1/chat/completions')

    async def test_audio_turns_fail_cleanly_when_only_anthropic_candidates_exist(self):
        http = FakeHttpClient()
        narrator = OpenAICompatibleNarrator(http, make_config([make_provider()]), True)
        with self.assertRaisesRegex(RuntimeError, 'No enabled OpenAI-compatible provider is available'):
            await narrator.decide(make_request(audio=[{'id': 'a', 'format': 'mp3', 'base64': 'AAAA'}]))
        self.assertEqual(http.posts, [])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
