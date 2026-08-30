"""主叙事：一次持续写作，连贯决定看见/回复/沉默/延迟。

从上游 `src/narrator.ts` + `src/service.ts` 中主叙事主循环移植概念子集。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import time as htime
from .model import (
    ChatMessage,
    ModelConfig,
    build_system_prompt,
    chat_completion_json,
)
from .types import (
    NarrativeContext,
    NarrativeDecision,
    NarrativeDecisionKind,
    NarrativeIntentKind,
    ScriptEntry,
    StorySetting,
)

_UTC = timezone.utc


def kind_value(kind) -> str:
    """返回枚举/字符串 kind 的字符串值。兼容从 JSON 反序列化后 kind 为字符串的情况。"""
    if isinstance(kind, str):
        return kind
    if hasattr(kind, "value"):
        return str(kind.value)
    return str(kind)


# AstrBot 平台 ID → 人类可读名
_PLATFORM_NAMES = {
    "aiocqhttp": "QQ（OneBot/NapCat）",
    "qqofficial": "QQ（官方）",
    "qq_official": "QQ（官方）",
    "telegram": "Telegram",
    "wecom": "企业微信",
    "lark": "飞书",
    "dingtalk": "钉钉",
    "discord": "Discord",
    "slack": "Slack",
    "kook": "KOOK",
    "voicechat": "语音聊天",
    "weixin_official_account": "微信公众号",
    "wechat": "微信",
    "satori": "Satori",
    "misskey": "Misskey",
}


def platform_name_human(platform_id: str) -> str:
    """把平台 ID 映射为人类可读名；未知则原样返回。"""
    if not platform_id:
        return "未知"
    return _PLATFORM_NAMES.get(platform_id, platform_id)


# 主叙事使用的结构输出协议（JSON）。
_NARRATIVE_SCHEMA_INSTRUCTION = (
    "You are the continuity engine of a persistent, character-centered life script. "
    "Write one continuous turn that: (1) catches up the in-story time that has passed, "
    "(2) decides how the character sees/replies to the current event, and (3) keeps "
    "future content as intentions/plans rather than forcing drama.\n"
    'Respond with a single JSON object with keys: "visible_reply" (string, the message '
    'the character actually sends, or ""), "script_append" (string, grounded life '
    'continuation in third person), "decided" (one of "see"|"reply"|"remain_silent"|"delay"), '
    '"reason" (short rationale), "delayed_reply_seconds" (integer or null), '
    '"intentions" (array of {type, about, due_at}).'
)


def _build_messages(ctx: NarrativeContext, prompts: dict, locale_style: str) -> list[ChatMessage]:
    setting: StorySetting = ctx.setting
    system = build_system_prompt(
        base_instruction=prompts.get("main_prompt", ""),
        style=prompts.get("style_prompt", locale_style),
        fixed=prompts.get("fixed_prompt", ""),
    )
    system += "\n" + _NARRATIVE_SCHEMA_INSTRUCTION

    # 剧本起点（canon）
    lines: list[str] = []
    lines.append(f"Character: {setting.character_name}")
    if setting.character_profile:
        lines.append(f"Profile: {setting.character_profile}")
    if setting.world:
        lines.append(f"World: {setting.world}")
    if setting.relationship:
        lines.append(f"Relationship: {setting.relationship}")
    if setting.supporting_cast:
        lines.append(f"Supporting cast: {setting.supporting_cast}")
    if setting.location:
        lines.append(f"Location: {setting.location}")
    lines.append(f"Story style: {setting.style}")

    if ctx.continuity:
        lines.append(
            "Continuity snapshot: "
            + json.dumps(
                {
                    "current": ctx.continuity.current,
                    "next": ctx.continuity.next,
                    "recent": ctx.continuity.recent,
                    "salient": ctx.continuity.salient,
                },
                ensure_ascii=False,
            )
        )

    if ctx.local_time:
        lt = ctx.local_time
        lines.append(
            f"Local time ({lt['timezone']}): {lt['local']} {lt['weekday']} "
            f"({lt['periodZh']}), {lt['daylightExpectation']}."
        )

    if ctx.facts:
        lines.append("Known long-term facts:")
        for fact in ctx.facts[:20]:
            lines.append(f"- {fact.content}")

    # 当前对话对端身份：自动用发送者名片告诉模型"在跟谁说话"
    if ctx.participant is not None:
        p = ctx.participant
        who = p.display_name or p.user_id or "对方"
        lines.append(f"当前对端: {who}")
        if p.relationship:
            lines.append(f"与主角的关系: {p.relationship}")
        if p.profile:
            lines.append(f"对端资料: {p.profile}")

    # 消息来源平台：告诉模型这是 QQ/微信/Telegram/Kook 等哪个平台
    platform = getattr(ctx.story, "platform", "") or ""
    lines.append(f"消息平台: {platform_name_human(platform)}")

    if ctx.participant and ctx.participant.state.open_threads:
        lines.append("Open threads: " + "; ".join(ctx.participant.state.open_threads))

    if ctx.recent_entries:
        lines.append("Recent script entries:")
        for entry in ctx.recent_entries[-30:]:
            sender = entry.participant_id or kind_value(entry.kind)
            lines.append(f"[{entry.created_at or ''}] {sender}: {entry.content}")

    # 当前事件
    if ctx.current_intent:
        intent = ctx.current_intent
        lines.append(f"Current event ({kind_value(intent.kind)}): {intent.content}")

    user_text = "\n".join(lines)
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user_text),
    ]


def _delay_to(delayed_seconds: Optional[int], now: Optional[datetime], maximum_minutes: int) -> Optional[str]:
    if not delayed_seconds:
        return None
    if now is None:
        now = datetime.now(_UTC)
    delayed_seconds = max(0, int(delayed_seconds))
    cap = int(maximum_minutes) * 60
    if delayed_seconds > cap:
        delayed_seconds = cap
    return (now + timedelta(seconds=delayed_seconds)).isoformat()


def _parse_decision(data: dict, now: Optional[datetime], maximum_minutes: int) -> NarrativeDecision:
    decided_raw = str(data.get("decided", "see")).strip().lower()
    decided_map = {
        NarrativeDecisionKind.see.value: NarrativeDecisionKind.see,
        NarrativeDecisionKind.reply.value: NarrativeDecisionKind.reply,
        NarrativeDecisionKind.remain_silent.value: NarrativeDecisionKind.remain_silent,
        NarrativeDecisionKind.delay.value: NarrativeDecisionKind.delay,
    }
    decided = decided_map.get(decided_raw, NarrativeDecisionKind.see)

    delay_sec: Optional[int] = None
    if decided == NarrativeDecisionKind.delay:
        raw = data.get("delayed_reply_seconds")
        if isinstance(raw, (int, float)) and raw > 0:
            delay_sec = int(raw)

    return NarrativeDecision(
        visible_reply=str(data.get("visible_reply") or "").strip(),
        script_append=str(data.get("script_append") or "").strip(),
        decided=decided,
        reason=str(data.get("reason") or "").strip(),
        delayed_reply_seconds=delay_sec,
        delayed_reply_at=_delay_to(delay_sec, now, maximum_minutes),
        intentions=list(data.get("intentions") or []),
    )


async def run_narrative(
    ctx: NarrativeContext,
    cfg: ModelConfig,
    *,
    astrobot_call=None,
    prompts: Optional[dict] = None,
    main_response_format: str = "json-object",
    max_tokens: Optional[int] = None,
    lower_max_tokens_limit: int = 4096,
    style_prompt: str = "",
) -> NarrativeDecision:
    """执行一次主叙事写作，返回结构化决策。"""
    prompts = prompts or {}
    messages = _build_messages(ctx, {**prompts, "style_prompt": style_prompt or prompts.get("style_prompt", "")}, style_prompt)

    now = datetime.now(_UTC)
    if max_tokens:
        cfg.max_tokens = max_tokens

    data, err = await chat_completion_json(messages, cfg, astrobot_call=astrobot_call)
    if err is not None or data is None:
        return NarrativeDecision(
            decided=NarrativeDecisionKind.remain_silent,
            reason=f"narrative model failed: {err}",
        )

    # 兜底：即使没有 reply 决策，也能保底产出可见回复
    decision = _parse_decision(data, now, 1440)
    return decision
