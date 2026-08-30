"""OpenAI 兼容 LLM 调用层。

提供两条路径：
1. 独立连接：配置了 endpoint/api_key/model 时直连 OpenAI 兼容 /v1/chat/completions。
2. AstrBot Provider：未独立配置时，通过 AstrBot 的 LLM 管理接口（在 adapters 层实现）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


def _httpx():
    """延迟导入 httpx；仅在直连 LLM 时才需要。"""
    try:
        import httpx

        return httpx
    except ImportError as err:  # pragma: no cover
        raise ModelError("直连模型需要安装 httpx：pip install httpx") from err


@dataclass
class ModelConfig:
    enabled: bool = False
    endpoint: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.8
    max_tokens: int = 4096
    timeout_seconds: int = 60

    def __bool__(self) -> bool:
        return self.enabled and bool(self.endpoint and self.model)


@dataclass
class ChatMessage:
    role: str   # "system" | "user" | "assistant"
    content: str


@dataclass
class CompletionResult:
    content: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class ModelError(Exception):
    """模型调用失败。"""


# AstrBot Provider 回调：由 adapters 层注入。
# signature: async (messages: list[ChatMessage], config: ModelConfig) -> str
AstrBotCallable = Callable[
    [list[ChatMessage], ModelConfig], Awaitable[str]
]


def build_system_prompt(base_instruction: str, style: str, fixed: str = "") -> str:
    parts = [base_instruction]
    if style:
        parts.append(f"Style: {style}")
    if fixed:
        parts.append(f"Rules: {fixed}")
    return "\n".join(parts)


async def chat_completion(
    messages: list[ChatMessage],
    config: ModelConfig,
    *,
    response_format: Optional[dict] = None,
    astrobot_call: Optional[AstrBotCallable] = None,
) -> CompletionResult:
    """调用模型。优先走独立连接；否则回退 AstrBot Provider。"""
    if not config.enabled or not config.endpoint:
        # 走 AstrBot Provider
        if astrobot_call is None:
            return CompletionResult(error="未配置模型连接，也未提供 AstrBot Provider 回调。")
        try:
            text = await astrobot_call(messages, config)
            return CompletionResult(content=text or "", model=config.model)
        except Exception as err:  # noqa: BLE001
            return CompletionResult(error=f"AstrBot Provider 调用失败: {err}")

    # 直连 OpenAI 兼容
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format

    try:
        httpx = _httpx()
        async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
            resp = await client.post(config.endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as err:
        return CompletionResult(error=f"模型返回 HTTP {err.response.status_code}: {err.response.text[:300]}")
    except httpx.HTTPError as err:
        return CompletionResult(error=f"模型请求失败: {err}")

    try:
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        model = data.get("model", config.model)
        return CompletionResult(content=content, model=model, usage=usage)
    except (KeyError, IndexError, TypeError) as err:
        return CompletionResult(error=f"模型响应格式异常: {err}: {data}")


def extract_json_object(text: str) -> Optional[dict]:
    """尽力从模型输出中提取 JSON object。"""
    if not text:
        return None
    text = text.strip()
    # 去掉可能的 ```json ... ``` 围栏
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _lenient_extract(text)


def _lenient_extract(text: str) -> Optional[dict]:
    """找不到严格 JSON 时，定位第一个 { 到最后一个 } 再尝试。"""
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


async def chat_completion_json(
    messages: list[ChatMessage],
    config: ModelConfig,
    *,
    astrobot_call: Optional[AstrBotCallable] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """请求 JSON object 响应并解析。返回 (data, error)。

    先尝试带 response_format=json_object；若模型不支持（如低配 Ollama 模型会导致 500），
    回落为不带，靠 prompt 指示 + 宽松提取。避免低配模型因 json mode 崩溃。
    """
    result = await chat_completion(
        messages, config,
        response_format={"type": "json_object"},
        astrobot_call=astrobot_call,
    )
    if not result.ok:
        # 回落：不带 response_format，让模型自然输出 JSON 文本
        result = await chat_completion(
            messages, config,
            astrobot_call=astrobot_call,
        )
    if not result.ok:
        return None, result.error
    data = extract_json_object(result.content)
    if data is None:
        return None, f"无法从模型输出解析 JSON: {result.content[:300]}"
    return data, None
