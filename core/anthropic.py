"""上游 `src/anthropic.ts` 的 Python 对应物：Anthropic Messages 传输适配层。

上游文件头注释原话：*Messages transport adapter. Narrative JSON and delivery
decisions remain protocol-independent.* —— 本模块**只做协议翻译**：请求体、
请求头、响应、SSE 流。它不知道叙事 JSON 是什么，也不认识投递决策，因此可以
被任何走 Anthropic Messages 的任务复用（主叙事 / 旁路 JSON / 表情描述 /
侧端识图）。

与上游的**必要偏差**（全部因为宿主运行时不同）：

1. 上游用全局 `fetch` + `AbortController`；本移植版一律走 `HttpClient` 协议
   （`iterate_sse`），超时用 `asyncio` 截止时间表达（与
   `narrator.request_openai_compatible_streaming` 同一套写法）。因此函数把
   `http` / `task` 作为末位参数（上游直接调用全局 `fetch`）。
2. 上游按响应头 `content-type: application/json` 分流"网关假装流式、其实回一个
   完整 JSON 体"；`HttpClient` 抽象拿不到响应头，改为**没有 `message_stop`
   时尝试整体解析**（与既有 OpenAI 兼容流式分支的兜底写法一致）。
3. 畸形帧容错：上游对「JSON.parse 失败」与「非对象帧」的边界依赖 JS 的
   `value.type` 语义（`null.type` 会抛）；本移植版与同文件的其它 SSE 解析器
   对齐——**一帧垃圾不该废掉整个用户回合**——跳过解析不出对象的帧。
4. 上游 `Math.min(Math.floor(timeout / 3), 30_000)` 的首帧守卫逐字保留；
   `timeout` 缺失时按既有移植约定回落到 1000ms（上游会当成 0 立即中止）。

键名约定：

- 发给服务端 / 从服务端读回的字段**逐字保持 Anthropic 线上名**（`system` /
  `max_tokens` / `cache_control` / `content_block_delta` / `input_tokens` /
  `x-api-key` …），一个字不改。
- 本移植版内部结构（`AnthropicOptions`）与函数名用 snake_case；从连接行读配置
  时两种拼写都认（`anthropic_cache` / `anthropicCache`），优先 snake_case。
- 上游 `undefined` → Python `None`（`anthropic_usage` 因此回 `None`）。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any, Awaitable, Callable, Optional, TypedDict

__all__ = [
    'ANTHROPIC_VERSION',
    'ANTHROPIC_FALLBACK_MAX_TOKENS',
    'ANTHROPIC_FIRST_FRAME_MAX_MS',
    'AnthropicOptions',
    'anthropic_headers',
    'blocks',
    'cache_prefix',
    'anthropic_body',
    'anthropic_usage',
    'anthropic_response',
    'normalize_protocol_endpoint',
    'request_anthropic_streaming',
]


#: Anthropic Messages 的版本头（上游 `anthropicHeaders` 里的字面量）。
ANTHROPIC_VERSION = '2023-06-01'
#: `max_tokens` 缺失 / 非正数时的回退值（上游字面量 4096）。
ANTHROPIC_FALLBACK_MAX_TOKENS = 4096
#: 首帧守卫的封顶（上游字面量 30_000）。
ANTHROPIC_FIRST_FRAME_MAX_MS = 30_000
#: `timeout` 缺失时的传输时限（与 `request_openai_compatible_streaming` 同口径）。
DEFAULT_STREAM_TIMEOUT_MS = 1_000


class AnthropicOptions(TypedDict, total=False):
    """上游 `AnthropicOptions`：协议层需要的三个连接行字段（内部 snake_case）。

    `api_key` / `max_tokens` / `anthropic_cache` 对应上游
    `apiKey` / `maxTokens` / `anthropicCache`；读连接行时两种拼写都认。
    """

    api_key: str
    max_tokens: int
    anthropic_cache: bool


# ========== 连接行字段读取（两种拼写都认） ==========


def _provider_field(provider: Any, snake: str, camel: str) -> Any:
    """连接行的字段：优先 snake_case，缺失时认上游 camelCase。"""
    if isinstance(provider, dict):
        value = provider.get(snake)
        if value is not None:
            return value
        return provider.get(camel)
    return None


def _anthropic_cache_enabled(provider: Any) -> bool:
    """`provider.anthropicCache === true`（严格相等：`1` / `'true'` 都不算）。"""
    return _provider_field(provider, 'anthropic_cache', 'anthropicCache') is True


def provider_max_tokens(provider: Any) -> Any:
    """`provider.maxTokens`（两种拼写）。"""
    return _provider_field(provider, 'max_tokens', 'maxTokens')


def provider_api_key(provider: Any) -> Any:
    """`provider.apiKey`（两种拼写）。"""
    return _provider_field(provider, 'api_key', 'apiKey')


# ========== 请求头 ==========


def anthropic_headers(provider: Any, overrides: Optional[dict[str, Any]] = None) -> dict[str, str]:
    """上游 `anthropicHeaders()`。

    `x-api-key` 只在有 key 时出现（**不发** `authorization`）；`overrides`
    最后展开，因此连接行的 `extraHeaders` 可以覆盖包括版本头在内的任何一项。
    """
    headers: dict[str, str] = {'content-type': 'application/json', 'anthropic-version': ANTHROPIC_VERSION}
    api_key = provider_api_key(provider)
    if api_key:
        headers['x-api-key'] = api_key if isinstance(api_key, str) else str(api_key)
    if overrides:
        headers.update({key: value if isinstance(value, str) else str(value) for key, value in overrides.items()})
    return headers


# ========== 请求体 ==========

#: 上游 `anthropicBody` 从 `rest` 里显式摘掉的 OpenAI 专有键。
_OPENAI_ONLY_BODY_KEYS = (
    'messages', 'response_format', 'top_p', 'reasoning_effort', 'stream_options', 'temperature',
)

_IMAGE_DATA_RE = re.compile(r'data:(image/(?:jpeg|png|gif|webp));base64,(.+)', re.S)
_HTTP_URL_RE = re.compile(r'^https?://')


def _truthy(value: Any) -> bool:
    """JS 真值语义：None/false/0/''/NaN 为假，**空数组与空对象为真**。"""
    if value is None or value is False:
        return False
    if isinstance(value, (list, tuple, dict, set)):
        return True
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(value)


def _is_number(value: Any) -> bool:
    """JS `typeof x === 'number'`：布尔不算数字。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _js_number(value: Any) -> Optional[float]:
    """`Number(value)`：数字原样、数字字符串解析、其余（含 `'abc'`）为 NaN（None）。

    `Number(null)` 在 JS 是 0、`Number(undefined)` 是 NaN；两者在
    `anthropicBody` 里都会落到 4096 回退，所以这里统一按 NaN 处理。
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _error_field(error: Any, key: str) -> Any:
    """`response.error?.type`：`error` 不是对象时得到 undefined（None）。"""
    return error.get(key) if isinstance(error, dict) else None


def blocks(content: Any) -> list[dict[str, Any]]:
    """上游 `blocks()`：把 OpenAI 形态的 content 翻成 Anthropic content blocks。

    - 字符串 → 单个 text block；
    - `text` → text；`image_url` → base64 / url 图片；`input_audio` **显式不支持**
      （Anthropic Messages 没有原生音频块，调用方应在候选筛选阶段就避开）；
    - 其余类型一律抛错，绝不静默丢内容。
    """
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    if not isinstance(content, (list, tuple)):
        raise RuntimeError('Anthropic message content must be text or content blocks.')
    result: list[dict[str, Any]] = []
    for part in content:
        part_type = part.get('type') if isinstance(part, dict) else None
        if part_type == 'text':
            result.append({'type': 'text', 'text': part.get('text')})
            continue
        if part_type == 'input_audio':
            raise RuntimeError(
                'Anthropic Messages does not support HDSI native audio input; '
                'use an audio-capable Chat Completions connection for this turn.'
            )
        if part_type == 'image_url':
            image_url = part.get('image_url')
            url = image_url.get('url') if isinstance(image_url, dict) else None
            match = _IMAGE_DATA_RE.fullmatch(url) if isinstance(url, str) else None
            if match:
                result.append({
                    'type': 'image',
                    'source': {'type': 'base64', 'media_type': match.group(1), 'data': match.group(2)},
                })
                continue
            if isinstance(url, str) and _HTTP_URL_RE.match(url):
                result.append({'type': 'image', 'source': {'type': 'url', 'url': url}})
                continue
            raise RuntimeError('Unsupported Anthropic image source or media type.')
        raise RuntimeError(f'Unsupported Anthropic input block: {part_type}')
    return result


def cache_prefix(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """上游 `cachePrefix()`：把序列化后的脚手架切成「前缀 + 其余」。

    *Splits the serialized scaffold without changing one character of its JSON.*
    切点必须是**序列化后的顶层键边界**（`currentSceneEvidence` 之前），绝不按
    用户散文里的子串切——否则一个出现在上下文里的同名字符串就能把缓存断点
    挪到随回合变化的位置，缓存永远不命中。

    读不出 `storyIdentity` / `relevantEstablishedEpisodes` /
    `currentSceneEvidence` 三项，或切出来的前缀与原文不逐字相同（键序、重复键、
    转义差异），一律**原样返回**：宁可没有缓存标记，也不改一个字符。
    """
    if not content:
        return content
    first = content[0]
    if not isinstance(first, dict) or first.get('type') != 'text':
        return content
    text = first.get('text')
    if not isinstance(text, str):
        return content
    try:
        payload = json.loads(text)
    except Exception:  # noqa: BLE001 - 不是 JSON 就不是缓存脚手架（上游 catch 原样返回）
        return content
    if not isinstance(payload, dict):
        return content
    if not _truthy(payload.get('storyIdentity')) \
            or not _truthy(payload.get('relevantEstablishedEpisodes')) \
            or not _truthy(payload.get('currentSceneEvidence')):
        return content
    prefix: dict[str, Any] = {}
    for key in payload:
        if key == 'currentSceneEvidence':
            break
        prefix[key] = payload[key]
    # 与 narrator._stringify_json 同一套序列化参数：无空格、非 ASCII 原样。
    # 切掉末尾的 `}` 换成 `,`，正好落在下一个顶层键之前。
    serialized = json.dumps(prefix, ensure_ascii=False, separators=(',', ':'))[:-1] + ','
    if not text.startswith(serialized):
        return content
    return [
        {'type': 'text', 'text': serialized, 'cache_control': {'type': 'ephemeral'}},
        {'type': 'text', 'text': text[len(serialized):]},
        *content[1:],
    ]


def anthropic_body(body: dict[str, Any], provider: Any, cache_first: bool = False) -> dict[str, Any]:
    """上游 `anthropicBody()`：OpenAI 请求体 → Anthropic Messages 请求体。

    - system 消息**提到顶层 `system` 字段**（不是 messages 里的一条）；
    - 去掉 OpenAI 专有键（`response_format` / `top_p` / `reasoning_effort` /
      `stream_options` / `temperature`，temperature 只按下面的规则有条件放回）；
    - `max_tokens` **必填**：缺失 / 非正数回退 4096（服务端会 400 空值）；
    - 开启了 `anthropicCache` 时缓存 system 的最后一块；`cacheFirst` 时同时用
      `cachePrefix` 标记历史前缀（关闭标记仍保留 cache-first 的内容顺序）；
    - Claude 不接受 temperature 与 top_p 同时出现，只保留 temperature；显式
      思考模式（`thinking.type` 为 `enabled` / `adaptive`）下连 temperature 都不发。
    """
    rest = {key: value for key, value in (body or {}).items() if key not in _OPENAI_ONLY_BODY_KEYS}
    messages = (body or {}).get('messages')
    if messages is None:
        messages = []
    system: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    cache_enabled = _anthropic_cache_enabled(provider)
    for message in messages:
        role = message.get('role') if isinstance(message, dict) else None
        content = message.get('content') if isinstance(message, dict) else None
        if role == 'system':
            system.extend(blocks(content))
        elif role in ('user', 'assistant'):
            converted = blocks(content)
            turns.append({
                'role': role,
                'content': cache_prefix(converted) if cache_first and cache_enabled and role == 'user' else converted,
            })
        else:
            raise RuntimeError(f'Unsupported Anthropic message role: {role}')

    if cache_enabled and system:
        system[-1]['cache_control'] = {'type': 'ephemeral'}

    raw_max_tokens = (body or {}).get('max_tokens')
    if raw_max_tokens is None:
        raw_max_tokens = provider_max_tokens(provider)
    max_tokens = _js_number(raw_max_tokens)
    if max_tokens is None or not math.isfinite(max_tokens) or max_tokens <= 0:
        max_tokens = ANTHROPIC_FALLBACK_MAX_TOKENS
    else:
        max_tokens = math.floor(max_tokens)

    result: dict[str, Any] = {**rest, 'stream': False, 'max_tokens': max_tokens}
    temperature = (body or {}).get('temperature')
    thinking = (body or {}).get('thinking')
    thinking_type = thinking.get('type') if isinstance(thinking, dict) else None
    if _is_number(temperature) and thinking_type not in ('enabled', 'adaptive'):
        result['temperature'] = max(0, min(1, temperature))
    result['system'] = system
    result['messages'] = turns
    return result


# ========== 响应 ==========


def anthropic_usage(usage: Any) -> Optional[dict[str, Any]]:
    """上游 `anthropicUsage()`：Anthropic 用量 → OpenAI 兼容用量形状。

    `prompt_tokens` = 未缓存输入 + 缓存读 + 缓存写；`prompt_tokens_details.
    cached_tokens` 单独带出缓存读，好让 `parse_token_usage` 点亮我们 Token
    统计里的 `cached_input_tokens`。
    """
    if not isinstance(usage, dict):
        return None
    input_keys = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
    has_input = any(_is_number(usage.get(key)) for key in input_keys)
    result: dict[str, Any] = {}
    if has_input:
        total = 0
        for key in input_keys:
            value = usage.get(key)
            total += value if _is_number(value) else 0
        result['prompt_tokens'] = total
    if _is_number(usage.get('output_tokens')):
        result['completion_tokens'] = usage['output_tokens']
    if _is_number(usage.get('cache_read_input_tokens')):
        result['prompt_tokens_details'] = {'cached_tokens': usage['cache_read_input_tokens']}
    return result


def anthropic_response(response: Any) -> dict[str, Any]:
    """上游 `anthropicResponse()`：Anthropic 响应 → OpenAI 兼容响应形状。

    thinking 块被丢弃（不外泄推理内容），只保留 text 块；`max_tokens` /
    `refusal` 停止原因与显式错误一律抛错——**绝不让截断的输出冒充完整剧本**。
    """
    if not isinstance(response, dict):
        response = {}
    error = response.get('error')
    if response.get('type') == 'error' or _truthy(error):
        raise RuntimeError(
            'Anthropic error: {} {}'.format(
                _error_field(error, 'type') or 'unknown', _error_field(error, 'message') or '',
            ).rstrip()
        )
    if response.get('stop_reason') == 'max_tokens':
        raise RuntimeError('Anthropic output reached max_tokens before completion.')
    if response.get('stop_reason') == 'refusal':
        raise RuntimeError('Anthropic provider refused the request.')
    content = response.get('content')
    text = ''.join(
        block.get('text') for block in content
        if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)
    ) if isinstance(content, list) else ''
    if not text:
        raise RuntimeError('Anthropic provider returned an empty text response.')
    return {
        'choices': [{'message': {'content': text}}],
        'usage': anthropic_usage(response.get('usage')),
    }


def normalize_protocol_endpoint(endpoint: Any, protocol: Any) -> str:
    """上游 `normalizeProtocolEndpoint()`：按协议匹配两种标准路径。

    切换协议时自动把 `/chat/completions` 与 `/messages` 互相改写（只改**第一个**
    出现在结尾或 `?` 之前的匹配段，网关前缀原样保留）；自定义路径不动。
    """
    value = endpoint.strip() if isinstance(endpoint, str) else ''
    if not value:
        return value
    if protocol != 'anthropic-messages':
        return re.sub(r'/messages(?=\?|$)', '/chat/completions', value, count=1)
    return re.sub(r'/chat/completions(?=\?|$)', '/messages', value, count=1)


# ========== 流式 ==========

_SSE_FRAME_SPLIT = re.compile(r'\r?\n\r?\n')
_SSE_LINE_SPLIT = re.compile(r'\r?\n')


def _resolve_http(source: Any) -> Any:
    """与 `narrator.resolve_http` 同口径的鸭子类型解析（本模块不 import narrator：
    narrator 反向 import 本模块，运行期会成环）。"""
    if hasattr(source, 'iterate_sse'):
        return source
    inner = getattr(source, 'http', None)
    if inner is not None and hasattr(inner, 'iterate_sse'):
        return inner
    raise TypeError('需要一个 HttpClient（iterate_sse），或带 .http 的上下文对象。')


async def _close_stream(stream: Any) -> None:
    """关闭流（上游 `reader.cancel()` 的资源释放部分）；关闭失败不影响结果。"""
    close = getattr(stream, 'aclose', None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001
        pass


async def request_anthropic_streaming(
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout: Optional[int],
    on_text: Optional[Callable[[str], Awaitable[None]]] = None,
    collect: Optional[Callable[[Any], None]] = None,
    http: Any = None,
    task: Optional[str] = None,
) -> str:
    """上游 `requestAnthropicStreaming()`：SSE 通道，支持早期可见回复。

    超时有两道（上游原样）：

    - **首帧守卫**：连接建立后长时间没有任何帧（部分网关的静默失败）在
      `timeout/3`（至多 30s）内暴露，不耗满全程；
    - **总时限**：`timeout` 兜底。

    用量累计（`message_start` / `message_delta` 里的 usage）在 `finally` 里上报
    **一次**——即使中途失败，已经产生的 token 也要记账（上游同）。
    """
    client = _resolve_http(http)
    loop = asyncio.get_running_loop()
    limit_ms = max(DEFAULT_STREAM_TIMEOUT_MS, int(timeout)) if _is_number(timeout) else DEFAULT_STREAM_TIMEOUT_MS
    first_frame_ms = min(limit_ms // 3, ANTHROPIC_FIRST_FRAME_MAX_MS)
    deadline = loop.time() + limit_ms / 1000
    first_frame_deadline = loop.time() + first_frame_ms / 1000
    timed_out = False
    received_any_frame = False
    stream: Any = None
    pending = ''
    raw = ''
    text = ''
    stopped = False
    stop_reason = ''
    usage: dict[str, Any] = {}
    has_usage = False

    async def event(frame: str) -> None:
        nonlocal usage, has_usage, stop_reason, text, stopped
        data = '\n'.join(
            line[5:].strip() for line in _SSE_LINE_SPLIT.split(frame) if line.startswith('data:')
        )
        if not data:
            return
        # 网关注入的 keep-alive/[DONE]/畸形帧不是 Anthropic 协议事件：一帧垃圾
        # 不该废掉整个用户回合（同文件其它流解析器同口径）。
        try:
            value = json.loads(data)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(value, dict):
            return
        event_type = value.get('type')
        if event_type == 'error':
            error = value.get('error')
            raise RuntimeError(
                'Anthropic stream error: {} {}'.format(
                    _error_field(error, 'type') or 'unknown', _error_field(error, 'message') or '',
                ).rstrip()
            )
        if event_type == 'message_start':
            message = value.get('message')
            start_usage = message.get('usage') if isinstance(message, dict) else None
            if isinstance(start_usage, dict):
                usage = {**usage, **start_usage}
                has_usage = True
        if event_type == 'message_delta':
            delta_usage = value.get('usage')
            if isinstance(delta_usage, dict):
                usage = {**usage, **delta_usage}
                has_usage = True
            delta_value = value.get('delta')
            reason = delta_value.get('stop_reason') if isinstance(delta_value, dict) else None
            if reason:
                stop_reason = reason
        delta = ''
        if event_type == 'content_block_start':
            content_block = value.get('content_block')
            if isinstance(content_block, dict) and content_block.get('type') == 'text':
                delta = content_block.get('text') or ''
        if event_type == 'content_block_delta':
            delta_value = value.get('delta')
            if isinstance(delta_value, dict) and delta_value.get('type') == 'text_delta':
                delta = delta_value.get('text') or ''
        if isinstance(delta, str) and delta:
            text += delta
            if on_text is not None:
                await on_text(text)
        if event_type == 'message_stop':
            stopped = True

    try:
        stream = client.iterate_sse(endpoint, headers, {**body, 'stream': True}, timeout, task=task).__aiter__()
        while not stopped:
            try:
                wait_limit = deadline if received_any_frame else first_frame_deadline
                remaining = wait_limit - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                chunk = await asyncio.wait_for(stream.__anext__(), remaining)
            except StopAsyncIteration:
                # 上游 `chunk.done`：把最后一个没有空行结尾的帧也处理掉再收工。
                if pending.strip():
                    await event(pending)
                break
            except asyncio.TimeoutError:
                timed_out = True
                break
            if chunk:
                received_any_frame = True
            raw += chunk
            pending += chunk
            frames = _SSE_FRAME_SPLIT.split(pending)
            pending = frames.pop()
            for frame in frames:
                await event(frame)
        if timed_out:
            # 上游 `controller.abort()`：中止即抛超时，不再走「流不完整」那条分支。
            waited = f'{limit_ms}ms' if received_any_frame else f'first frame within {first_frame_ms}ms'
            raise RuntimeError(f'Anthropic streaming request timed out after {waited}.')
        if not stopped:
            # 网关可能忽略 `stream: true`，直接回一个完整 JSON 体。上游按响应头
            # 分流；这里拿不到响应头，改为「没有 message_stop 时尝试整体解析」。
            parsed = _try_json(raw)
            if isinstance(parsed, dict) and (
                parsed.get('type') in ('message', 'error') or isinstance(parsed.get('content'), list)
            ):
                result = anthropic_response(parsed)
                if collect is not None:
                    collect(result.get('usage'))
                return result['choices'][0]['message']['content']
            raise RuntimeError('Anthropic stream ended before message_stop; response is incomplete.')
        if stop_reason in ('max_tokens', 'refusal'):
            raise RuntimeError(f'Anthropic stream stopped with {stop_reason}.')
        if not text:
            raise RuntimeError('Anthropic stream ended without visible text.')
        return text
    except Exception as error:  # noqa: BLE001 - 按上游的 catch 分流重写错误文案
        if timed_out:
            # 循环外已经抛出超时错误，原样上抛（不再包一层）。
            raise
        status = getattr(error, 'status', None)
        if isinstance(status, int):
            detail = getattr(error, 'detail', '') or getattr(error, 'status_text', '') or ''
            raise RuntimeError(
                'Anthropic streaming request failed ({}): {}'.format(status, str(detail)[:500])
            ) from error
        raise
    finally:
        if stream is not None:
            await _close_stream(stream)
        if has_usage and collect is not None:
            collect(anthropic_usage(usage))


def _try_json(raw: str) -> Any:
    """整段解析（`JSON.parse` 的等价物）；失败返回 `None`。"""
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
