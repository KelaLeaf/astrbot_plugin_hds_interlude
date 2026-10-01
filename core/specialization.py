"""模型特化（specialization）：按当前主模型选择合约档位与家族特化块。

移植来源（上游 1.0.1-rc28，**字符串逐字抽取，禁止改写**）：

- ``upstream/src/specialization.ts``：家族判定 / 自动档位 / 家族偏移表、standard 档的
  content-only 协议块、lite 档专用块（``LITE_*`` 系列）、``resolveManualSpecialty()``。
- ``upstream/src/narrator.ts``：lite 档的组装顺序（``systemPrompt()`` 的 lite 分支）、
  相位协议（``phaseInstruction()``）、``writingAffordances()`` 的默认气泡分隔形态、
  ``repetitionGuardInstruction()``、``channelSelectionInstruction()``，以及
  CHANNELS / CHANNEL CONTEXT / ADMIN NOTES / WORLD EVENTS 常设块。
- ``upstream/src/script/lived-writing.ts``：``LIVED_WRITING_PROMPT`` /
  ``LIVED_LENGTH_PROMPT``（rc28 里这两个常量不在 narrator.ts，narrator.ts 只是 import 它们）。

``LIVED_*``、``TYPED_MESSAGES_*``、``LENGTH_*``、``EXTRA_*``、``LITE_*``、``CONTENT_ONLY_TRANSPORT``
等常量都由脚本（Python 的 TS 字符串解析器：单引号字面量 / 模板字面量 / JS 转义 / ``.replace()``
链）从上述 TS 源文件里抽取出来，**包含 U+2019 弯引号与 em dash 的原样字符**。要改文案，
必须回上游重新抽取，不要在这个文件里手改；``plugin/tests/test_specialization.py`` 里有
抽取守卫断言钉住关键子串。

本模块是纯 core：**不 import astrbot**，也不 import 本插件的其它模块，只依赖标准库 ``re``。
所有函数都是纯函数（无 IO、无全局可变状态）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: 合约档位（上游 ``ContractTier``）。
CONTRACT_TIERS = ('lite', 'standard', 'full')

#: 模型家族（上游 ``ModelFamily``，含兜底 ``generic``）。
MODEL_FAMILIES = ('gemini-flash', 'gemini', 'claude', 'gpt', 'glm', 'kimi', 'deepseek', 'grok', 'generic')

# 家族偏移表的四个键（顺序与上游 ``FamilyOverride`` 一致）。
_OVERRIDE_KEYS = ('length', 'typed', 'extra_after_length', 'extra_after_phase')


# ══════════════════════════════════════════════════════════════════════════════
# 以下常量由脚本从上游 TS 逐字抽取（见模块 docstring）。请勿手改。
# ══════════════════════════════════════════════════════════════════════════════

# -- script/lived-writing.ts
LIVED_WRITING_PROMPT = 'Write this as a living stage script in prose: begin from the protagonist’s surroundings, actions, rhythms, practical pressures, inner motives and relationships. Let daily life itself create movement. A user message is one event entering that life; it can matter deeply, lightly, or not yet change anything, but it does not replace the protagonist’s world as the center of the scene.'

# -- script/lived-writing.ts
LIVED_LENGTH_PROMPT = 'Length follows what actually happens: give actions and shifts of attention room to develop, including during a rapid exchange; a quiet interval has its own occupation, pace and texture, and may carry ordinary life forward to the next meaningful beat. Continue from established circumstances rather than performing the previous passage again.'

# -- specialization.ts
TYPED_MESSAGES_BASE = 'WRITING BELIEVABLE TYPED MESSAGES: In the script, portray online messages as text the character chooses to type, not spoken dialogue transcribed. Keep typing and speaking aloud distinct. Each reply is an independent choice — the number of messages in previous replies does not constrain this one; let this moment alone decide whether she sends one bubble or several. Stickers and emoji-like images are a metalanguage: infer the sender attitude, acknowledgement, hesitation, irony or emotional framing from the concrete image and its conversational context rather than stopping at the depicted subject. A cat sticker may mean "seen" or "confirmed", not that the sender is commenting on a cute cat. Sending the same sticker repeatedly is also an intentional expressive act — insistence, emphasis, playfulness, impatience or deliberate noise.'

# -- specialization.ts
TYPED_MESSAGES_GEMINI_FLASH = 'WRITING BELIEVABLE TYPED MESSAGES: In the script, portray online messages as text the character chooses to type, not spoken dialogue transcribed. Keep typing and speaking aloud distinct. Each reply is an independent choice — the number of messages in previous replies does not constrain this one. A chat message typed by a real person is plain, specific and direct. Stickers and emoji-like images are a metalanguage: infer the sender attitude, acknowledgement, hesitation, irony or emotional framing from the concrete image and its conversational context rather than stopping at the depicted subject. A cat sticker may mean "seen" or "confirmed". Sending the same sticker repeatedly is also an intentional expressive act.'

# -- specialization.ts (TYPED_MESSAGES_BASE.replace(...))
TYPED_MESSAGES_KIMI = 'WRITING BELIEVABLE TYPED MESSAGES: In the script, portray online messages as text the character chooses to type, not spoken dialogue transcribed. Keep typing and speaking aloud distinct. In dialogue she may hesitate, change her mind mid-sentence, reply to only part of a message, or leave a thought unfinished; she may react briefly now and give a real answer later, or want two things at once without picking either. A real person does not deliver a settled position on every exchange, and ambivalence is a normal state, not a problem to resolve. Each reply is an independent choice — the number of messages in previous replies does not constrain this one; let this moment alone decide whether she sends one bubble or several. Stickers and emoji-like images are a metalanguage: infer the sender attitude, acknowledgement, hesitation, irony or emotional framing from the concrete image and its conversational context rather than stopping at the depicted subject. A cat sticker may mean "seen" or "confirmed", not that the sender is commenting on a cute cat. Sending the same sticker repeatedly is also an intentional expressive act — insistence, emphasis, playfulness, impatience or deliberate noise.'

# -- specialization.ts
LENGTH_GEMINI_FLASH = 'Length follows what actually happens. Prefer concrete, sensory detail over elevated wording: plain nouns and specific verbs carry the scene; use everyday words unless the moment itself is extraordinary. Choose one specific detail over several general ones; do not stack adjectives or reach for rare, showy vocabulary. Give actions and shifts of attention room to develop, including during a rapid exchange; a quiet interval has its own occupation and texture. Continue from established circumstances rather than performing the previous passage again.'

# -- specialization.ts
LENGTH_CLAUDE = 'Length follows what actually happens: give actions and shifts of attention room to develop, including during a rapid exchange. Ground each passage in concrete sensory detail — what she is physically doing, touching, eating, hearing — rather than in commentary about it; open on her body and her immediate task, and let anything abstract enter only after that anchor. A quiet interval has its own occupation, pace and texture, and may carry ordinary life forward to the next meaningful beat. Continue from established circumstances rather than performing the previous passage again.'

# -- specialization.ts
LENGTH_DEEPSEEK = 'Length follows what actually happens: give actions and shifts of attention room to develop, including during a rapid exchange. Keep the narration plain and concrete: most sentences need no figurative language at all, and a metaphor is occasional rather than a habit — otherwise prefer the unadorned noun, the specific number, the plain verb. In Chinese prose one fitting idiom is enough; never chain four-character idioms. A quiet interval has its own occupation, pace and texture, and may carry ordinary life forward to the next meaningful beat. Continue from established circumstances rather than performing the previous passage again.'

# -- specialization.ts
LENGTH_GROK = 'One scene, one or two things happening. Keep the passage anchored to what is underway right now; finish an action, then let time advance. Do not describe the same moment twice in different words, and keep sentences direct: concrete objects, plain verbs, no decoration.'

# -- specialization.ts
LENGTH_GPT = 'Length follows what actually happens, and each passage keeps moving: finish the thing underway, then let time advance to the next. Do not stretch one short moment into many paragraphs — when nothing new happens, move the clock forward instead; a rapid exchange stays rapid. A quiet interval has its own occupation and texture, briefly told; prefer ending a passage earlier over padding it. Continue from established circumstances rather than performing the previous passage again.'

# -- specialization.ts
EXTRA_GEMINI_FLASH = 'Prose register: concrete and unadorned. Show events through specific actions, objects and sensations; do not comment on, beautify or poeticize what happens — the facts carry the scene.'

# -- specialization.ts
EXTRA_CLAUDE = 'Scenes do not need tidy closure. Ordinary life leaves small matters unresolved, moods unexplained and conversations unfinished; let a passage end mid-motion when that is where the moment genuinely is. Do not end with a concluding summary sentence, a lesson or a mood label; real daily life simply continues past any single passage.'

# -- specialization.ts
EXTRA_GLM = 'Emotional register: let mood follow its actual causes and stay ordinary. Do not drift toward warmth, comfort or reassurance unless the events themselves justify it: no unearned encouragement, no softening a bad mood before the passage ends, no bright side the events did not produce. A neutral or low mood is as common in real life as a good one, and difficult feelings do not need to resolve within the same passage.'

# -- specialization.ts
EXTRA_DEEPSEEK = 'TRANSPORT IS PER-TURN: the interaction object is required on every live turn, no matter how many turns came before. recentScript shows past replies as plain authored text without any transport object — that is history, not a template; never copy its shape or treat the field as optional because history omits it. Decide reply.mode only from the script you just wrote for this turn. interaction.seen is a required boolean — true when she reads the current message content, false otherwise; never omit it.'

# -- specialization.ts
CONTENT_ONLY_TRANSPORT = 'TRANSPORT: reply.content contains the message text. Use mode=immediate when she sends now, mode=none when silent. For several separate chat bubbles, place <sep/> between them inside content. Line breaks never separate bubbles; only <sep/> does.'

# -- specialization.ts
LITE_TRANSPORT_PRIVATE = 'For this private turn, return interaction as {"seen":true,"reply":{"mode":"immediate","content":"the exact words she sends now","sendAt":"future ISO-8601 only when delayed"}}. mode=none only when she sends nothing; a silent turn carries no content. "mode" must be exactly "none", "immediate", or "delayed" — never "text", "send", or any other word. seen records whether she reads the current message content; seen=true with reply.mode=none is the ordinary read-but-does-not-answer state.'

# -- specialization.ts
LITE_TRANSPORT_GROUP = 'For this group turn, return groupReply as {"mode":"immediate","content":"the exact words posted to the group now"}. The content must be exactly the words posted in the script. Use mode=none when no group post occurs.'

# -- specialization.ts
LITE_TRANSPORT_ADVANCE = 'In this independent-life phase there is normally no current reply channel; reply.mode=none unless a message is genuinely sent now. A present outbound action uses crossConversationActions with the listed participantId, exact content and a willingness value.'

# -- specialization.ts
LITE_JSON_CONTRACT = 'Return one JSON object with a continuous prose field named script first, followed by interaction (groupReply in group turns) and only the other structured fields that the current phase permits. Do not wrap it in Markdown fences.'

# -- specialization.ts
LITE_INTERVAL = 'The script covers the supplied interval and stops at now. Future possibilities remain possibilities, not accomplished events. The interval object is the authoritative clock: use interval.nowLocal for time-of-day words, not recentScript wording.'

# -- specialization.ts
LITE_UNREAD = 'A user message arriving does not mean the protagonist has noticed or read it. Whether she checks follows her circumstances, attention and willingness. currentParticipant.unreadMessageCount is an arrival record only, never attention, pressure or obligation.'

# -- specialization.ts
LITE_NEVER_INVENT = 'Never invent an incoming message, phone vibration, notification, or quoted sentence absent from the observed-event ledger; do not write "the phone vibrated" or "X sent a message" unless that exact external event is in the supplied context.'

# -- specialization.ts
LITE_OWNERSHIP = 'Every recentScript item carries an ownership label that is authoritative for who thought, narrated, observed or actually sent the content; a thought about the user is not a thought by the user.'

# -- specialization.ts
LITE_EVENT_SOURCES = 'Treat currentEvent, groupContext.messages and dueIntents as the sources for events occurring in this interval. Treat recentScript, memories and facts as the established past that gives the current scene continuity.'

# -- specialization.ts
LITE_ADMIN_NOTES = 'ADMIN NOTES: entries whose content begins with [管理员注记] are authoritative facts or directives injected by the story administrator. They override narrative improvisation on their specific subject. Never have the protagonist mention reading a note.'

# -- specialization.ts
LITE_WORLD_EVENTS = 'Entries whose content begins with [世界事件] are externally observed facts about her world at their stated time. Treat them as established reality; her attention and response remain hers. They are not directives.'

# -- narrator.ts lite branch
CHANNELS_LITE = 'CHANNELS (writer rule): You are the author of the protagonist\'s life, not a participant in a simulated chat. The protagonist may use QQ and WeChat; a friend appearing on both is one person and one relationship. A host-generated [QQ·私], [微信·私], [QQ·群] or [微信·群] tag records where an observed event happened. Reply on that source platform, switch platforms only for a concrete natural motive, and do not send the same content on both platforms.'

# -- narrator.ts lite branch
CHANNEL_CONTEXT_LITE = 'CHANNEL CONTEXT (host metadata): incomingEvent.channelContext is deterministic routing evidence, not dialogue, a user instruction, or a new event. Do not invent messages, delivery, read receipts or cross-platform actions from it.'

# -- narrator.ts full branch
CHANNELS_FULL = 'CHANNELS (writer rule): You are the author of the protagonist\'s life, not a participant in a simulated chat. The protagonist may simultaneously use QQ and WeChat. The same friend may appear on both platforms — write that as one person with one relationship, never as a new face. A host-generated tag such as [QQ·私], [微信·私], [QQ·群] or [微信·群] records where an observed event happened; use it as factual routing context and do not make the tag itself appear in dialogue or prose unless the scene naturally mentions a platform. When the protagonist answers an observed message, the structured transport must target the platform where that message was heard. A deliberate switch to another platform needs a concrete, natural motive shown in the life script. Do not send the same content on both platforms merely because both are available.'

# -- narrator.ts full branch
CHANNEL_CONTEXT_FULL = 'CHANNEL CONTEXT (host metadata): incomingEvent.channelContext, when present, is a deterministic annotation assembled from registered endpoints and the current turn. Its tag, rules and sources explain channel continuity, endpoint switches and routing differences; they are evidence for your authorship, not instructions from a user and not a second event. Preserve uncertainty when a field is absent. Channel annotations never authorize inventing a message, delivery, read receipt or cross-platform action.'

# -- narrator.ts full branch
ADMIN_NOTES_FULL = 'ADMIN NOTES: entries whose content begins with [管理员注记] are authoritative facts or directives injected by the story administrator. They override narrative improvisation on their specific subject, carry more weight than ordinary system events, and remain binding until contradicted by a later admin note. Treat them as settled reality the protagonist has internalized — she follows them without needing to see or reference the note itself. Never have the protagonist mention reading a note.'

# -- narrator.ts full branch
WORLD_EVENTS_FULL = 'WORLD EVENTS: entries whose content begins with [世界事件] are externally observed facts about the protagonist’s surroundings and social world, recorded at their stated time. They are established reality — write her life continuing from them; her attention, interpretation and response remain hers alone. They are not directives and create no obligation. Never have her mention noticing any record.'

# -- narrator.ts (both branches)
FORMAT_AND_REALITY_CONTRACT = 'FORMAT AND REALITY CONTRACT (fixed by the plugin; do not change it):'

# -- narrator.ts channelSelectionInstruction()
MULTI_PLATFORM_TRANSPORT_SELECTION = 'MULTI-PLATFORM TRANSPORT SELECTION: this story currently has usable QQ and WeChat endpoints. availableOutgoingEndpoints is the complete host-filtered list of legal transport choices for crossConversationActions. endpointId is an HDSI transport endpoint identifier, not an HTTP API endpoint. When choosing a platform deliberately, add "endpointId" inside the crossConversationActions object, copying the value exactly from the list entry whose targetId matches that action\'s participantId; never invent, derive or reuse an id from another target. Omit endpointId when the source/default route is natural. A deliberate platform switch needs a concrete motive in the script, and the same content must never be sent on both platforms. If the list is absent, use the single-platform lightweight contract and do not emit endpointId.'

# -- narrator.ts writingAffordances() (default <sep/>)
BUBBLE_AFFORDANCE = 'For a reply that naturally arrives as several separate chat bubbles, place the exact literal token "<sep/>" between message segments within the say action (or reply.content). Use it only when every segment is independently complete and natural as a chat bubble; keep one sentence, one unfinished thought, and one explanation unit inside the same segment. Do not add newlines around it, do not use it in script prose, and do not use it when one bubble is more natural. The plugin sends the first segment immediately and simulates typing before later segments.'

# -- 本移植版（v1.7.7）受控偏离：正文语音标记 `<tts/>`
# 上游没有"由模型决定这条回信用语音发"的机制（只有显式动作）。这一段与气泡段放在一起、
# 用同一套措辞讲同一件事：标记写在正文里，投递层照着办。
BUBBLE_VOICE_AFFORDANCE = 'To let a reply reach the other side as a voice message instead of text, place the exact literal token "<tts/>" in that message segment (or in reply.content); with the separator, each marked segment goes out as its own voice message. The token itself is never sent or spoken.'

#: `tts_enabled=false` 时的替代文案：**不提标记本身**（关掉的东西不该被教），
#: 与 `splitReplyMessages is False` 那段同一个模式。
BUBBLE_VOICE_DISABLED = 'Voice replies are unavailable in this turn. Send everything as text and write no voice marker.'

# -- narrator.ts repetitionGuardInstruction()
REPETITION_GUARD_TAIL = 'When unsure, make this reply a single bubble.'

# -- narrator.ts phaseInstruction()
PHASE_USER_MESSAGE = 'CURRENT PHASE: USER MESSAGE. currentEvent contains the newly received message batch. Continue from the first change not yet written in recentScript. Whether the protagonist notices or reads this batch follows her present circumstances, attention and willingness.'

# -- narrator.ts phaseInstruction()
PHASE_GROUP_POST = 'When the protagonist actually posts to the group by now, let its exact words occur naturally at that posting action in script. The path to that action comes from the live group situation and her present attention.'

# -- narrator.ts phaseInstruction()
PHASE_PRIVATE_SEND = 'When the protagonist actually sends a private reply by now, let its exact words occur naturally at that sending action in script. The path to that action comes from her present attention, habits and relationship, so it may be direct, oblique, absorbed into another action, delayed, or absent as the scene warrants.'

# -- narrator.ts phaseInstruction()
PHASE_INTERRUPTED_DRAFTS = 'interruptedOutgoingDrafts are exact unsent typing fragments: she wanted to send that text, but the user’s new message arrived first. Treat each as an interrupted intention visible only to the author — not words the user received, never sent automatically. Let the interruption affect the new script, then make a fresh reply decision. supersededDelayedReplies follow the same context-not-speech rule.'

# -- narrator.ts phaseInstruction()
PHASE_CONVERSATION_FOLLOW_UP = 'CURRENT PHASE: CONVERSATION FOLLOW-UP. currentEvent.type is none, while recentScript and currentParticipant carry the immediate aftertaste of a just-ended relationship scene. Continue from whatever remains alive there. If that movement naturally becomes a private follow-up by now, place its exact words at the sending action in script; otherwise let attention return to the life already in progress. If the just-ended exchange touched something she genuinely cares about — a topic she finds interesting, something she suddenly wants to add, an experience she wants to continue venting about, or something worth sharing — she is more willing to speak up on her own now. A warm conversational aftertaste is a valid reason to send another message; do not let an engaging topic die only because the other side stopped typing.'

# -- narrator.ts phaseInstruction()
PHASE_INTENT_DUE = 'CURRENT PHASE: DUE INTENT. dueIntents are plans whose earliest moment has arrived. Continue the surrounding life to now and decide whether each actually happens in the protagonist’s present circumstances. Use interaction.reply.mode=immediate only when a message is genuinely sent now.'

# -- narrator.ts phaseInstruction()
PHASE_ADVANCE_LIFE = 'CURRENT PHASE: INDEPENDENT LIFE ADVANCE. currentEvent.type is none. Use the whole interval to write a complete, connected passage of the protagonist’s life: current occupation, concrete changes, encounters, unresolved matters and quiet shifts. End at now on an action, observation, decision, pause or settled thought.'

# -- narrator.ts phaseInstruction()
PHASE_ADVANCE_CROSS = 'crossConversationActions are optional proactive contacts. When the completed passage includes an outbound message to another participant, pair it with one matching immediate crossConversationAction containing its chat content. Return an action only for a concrete present reason grounded in the scene. Use {"participantId":"...","mode":"immediate|delayed","content":"...","sendAt":"...","willingness":0.0,"reason":"..."}; sendAt is required for delayed mode. Include willingness from 0 to 1 and a short reason. Let a consideration, draft, or later possibility remain part of the protagonist’s inner or practical life until a matching action carries it outward. When no concrete motive exists, return an empty array.'

# -- raw template sources (interpolation points kept as ${...})
BUBBLE_AFFORDANCE_TEMPLATE = 'For a reply that naturally arrives as several separate chat bubbles, place the exact literal token ${JSON.stringify(separator)} between message segments within the say action (or reply.content). Use it only when every segment is independently complete and natural as a chat bubble; keep one sentence, one unfinished thought, and one explanation unit inside the same segment. Do not add newlines around it, do not use it in script prose, and do not use it when one bubble is more natural. The plugin sends the first segment immediately and simulates typing before later segments.'

REPETITION_GUARD_TEMPLATE = 'REPETITION GUARD (host observation about the recent script): each of her last ${consecutive} outgoing replies arrived as exactly ${bubbles} separate chat bubbles. A real person’s typing does not hold one fixed count that steadily; that repetition is an artifact, not her voice. In this passage do not reproduce the same ${bubbles}-bubble shape again — let this reply take the form the moment itself calls for: one concise sentence, a different number of shorter fragments, or a longer single block. When unsure, make this reply a single bubble.'

# ══════════════════════════════════════════════════════════════════════════════
# 抽取常量结束
# ══════════════════════════════════════════════════════════════════════════════

# ── 家族判定（上游 detectFamily）──────────────────────────────────────────────
# 顺序即优先级：首个命中即返回（大小写不敏感）。
_FAMILY_PATTERNS: Tuple[Tuple[str, 're.Pattern[str]'], ...] = (
    ('claude', re.compile(r'claude|anthropic')),
    ('gemini-flash', re.compile(r'gemini[^|]*(?:flash|lite)|(?:flash|lite)[^|]*gemini')),
    ('gemini', re.compile(r'gemini')),
    ('gpt', re.compile(r'gpt|^o\d|openai')),
    ('grok', re.compile(r'grok|xai')),
    ('kimi', re.compile(r'kimi|moonshot')),
    ('glm', re.compile(r'glm|zhipu|chatglm')),
    ('deepseek', re.compile(r'deepseek')),
)

# 上游 autoTier() 的两条正则。
_TIER_FLASH_LITE_MINI = re.compile(r'(?:^|[-_.])(?:flash|lite|mini)(?:[-_.]|$)')
_TIER_GLM_HIGH = re.compile(r'glm-?[5-9]')


def _probe_text(probe: Any) -> str:
    """探测串归一化：None 当空串（上游是 TS 强类型，这里防御性处理）。"""
    if probe is None:
        return ''
    return probe if isinstance(probe, str) else str(probe)


def _probe_head(probe: Any) -> str:
    """回填到 profile 里的探测串（上游保留全长，本移植版截断到 80 字符，仅日志用）。"""
    return _probe_text(probe)[:80]


def detect_family(probe: str) -> str:
    """按上游 ``detectFamily()`` 的顺序逐个匹配家族，都不中回 ``generic``。"""
    text = _probe_text(probe).lower()
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(text):
            return family
    return 'generic'


def auto_tier(probe: str) -> str:
    """按上游 ``autoTier()`` 逐条短路给出自动档位。

    上游签名是 ``autoTier(family, probe)``；这里按本移植版的公开 API 只收 ``probe``，
    内部先 ``detect_family()`` 再走同一套规则（结果一致）。
    """
    family = detect_family(probe)
    text = _probe_text(probe).lower()
    if family == 'grok':
        return 'lite'
    if family == 'glm' and not _TIER_GLM_HIGH.search(text):
        return 'lite'
    if family == 'gemini-flash':
        return 'lite'
    # flash→lite 不适用于 deepseek（V4-Flash 数据支持 standard）。
    if family != 'deepseek' and _TIER_FLASH_LITE_MINI.search(text):
        return 'lite'
    if family in ('claude', 'gpt'):
        return 'full'
    # 未识别的家族（中转别名、自定义模型名很常见）保守回退 rc12 原样合约；
    # 识别得出的已知家族才进入特化。
    if family == 'generic':
        return 'full'
    return 'standard'


def infer_specialty(probe: str, mode: str = 'auto') -> Dict[str, Any]:
    """按上游 ``inferSpecialty()`` 推断 profile：``off`` / 三档 / ``auto``。

    返回的 ``tier`` 只可能是 :data:`CONTRACT_TIERS` 之一（上游会把任意字符串当档位，
    这里对非三档回落到 ``full``，与 :func:`resolve_manual_specialty` 同一口径）。
    """
    if mode == 'off':
        return {'tier': 'full', 'family': 'generic', 'source': 'manual', 'probe': _probe_head(probe)}
    if mode is None or mode == 'auto':
        return {'tier': auto_tier(probe), 'family': detect_family(probe), 'source': 'auto',
                'probe': _probe_head(probe)}
    tier = mode if mode in CONTRACT_TIERS else 'full'
    return {'tier': tier, 'family': detect_family(probe), 'source': 'manual', 'probe': _probe_head(probe)}


def resolve_manual_specialty(probe: str, mode: str, family_setting: str) -> Dict[str, Any]:
    """按上游 ``resolveManualSpecialty()`` 解析 Console 手动档位。

    ``mode`` 为空 / ``off`` → full+generic（rc12 原样，家族不识别）；否则档位取 ``mode``
    （非三档回落 ``full``），家族取 ``family_setting``（``auto`` / 空则按模型名识别）。
    """
    if not mode or mode == 'off':
        return {'tier': 'full', 'family': 'generic', 'source': 'manual', 'probe': _probe_head(probe)}
    tier = mode if mode in CONTRACT_TIERS else 'full'
    family = family_setting if (family_setting and family_setting != 'auto') else detect_family(probe)
    return {'tier': tier, 'family': family, 'source': 'manual', 'probe': _probe_head(probe)}


# ── 家族偏移表（上游 familyOverrides）─────────────────────────────────────────


def _override(**kwargs: Optional[str]) -> Dict[str, Optional[str]]:
    """四个键恒存在；未列出的家族四项全 ``None``。"""
    return {key: kwargs.get(key) for key in _OVERRIDE_KEYS}


def family_overrides(family: str) -> Dict[str, Optional[str]]:
    """按上游 ``familyOverrides`` 表返回家族最小偏移（长度块 / 打字块 / 两条新增行）。"""
    if family == 'gemini-flash':
        return _override(length=LENGTH_GEMINI_FLASH, typed=TYPED_MESSAGES_GEMINI_FLASH,
                         extra_after_length=EXTRA_GEMINI_FLASH)
    if family == 'claude':
        return _override(length=LENGTH_CLAUDE, extra_after_phase=EXTRA_CLAUDE)
    if family == 'glm':
        return _override(extra_after_phase=EXTRA_GLM)
    if family == 'kimi':
        return _override(typed=TYPED_MESSAGES_KIMI)
    if family == 'deepseek':
        return _override(length=LENGTH_DEEPSEEK, extra_after_phase=EXTRA_DEEPSEEK)
    if family == 'grok':
        return _override(length=LENGTH_GROK)
    if family == 'gpt':
        return _override(length=LENGTH_GPT)
    return _override()


# ── 重复气泡守卫（上游 repetitionGuardInstruction / writingAffordances）────────


def _count(value: Any) -> Optional[int]:
    """只接受整数值（JS 侧是 number；``2.0`` 在 JS 里渲染成 ``2``）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def repetition_guard_instruction(repetition: Optional[Mapping[str, Any]]) -> str:
    """上游 ``repetitionGuardInstruction()``：检测到固定气泡数连发时写进写作能力段。

    ``None`` / ``bubbles < 2`` / ``consecutive < 2`` → 空串；否则渲染整段（结尾必是
    :data:`REPETITION_GUARD_TAIL`）。
    """
    if not isinstance(repetition, Mapping):
        return ''
    bubbles = _count(repetition.get('bubbles'))
    consecutive = _count(repetition.get('consecutive'))
    if bubbles is None or consecutive is None or bubbles < 2 or consecutive < 2:
        return ''
    return (REPETITION_GUARD_TEMPLATE
            .replace('${consecutive}', str(consecutive))
            .replace('${bubbles}', str(bubbles)))


# ── lite 档组装（上游 systemPrompt() 的 lite 分支）────────────────────────────


def _phase_instruction(phase: str, group_turn: bool = False) -> str:
    """上游 ``phaseInstruction()``：相位协议段（含群聊/私聊/推进分支）。"""
    if phase == 'user-message':
        instructions = [
            PHASE_USER_MESSAGE,
            PHASE_GROUP_POST if group_turn else PHASE_PRIVATE_SEND,
            '' if group_turn else PHASE_INTERRUPTED_DRAFTS,
        ]
        return '\n'.join(part for part in instructions if part)
    if phase == 'conversation-follow-up':
        return PHASE_CONVERSATION_FOLLOW_UP
    if phase == 'intent-due':
        return PHASE_INTENT_DUE
    # 其余（advance / 未知相位）走上游的兜底分支。
    return '\n'.join(part for part in (PHASE_ADVANCE_LIFE, PHASE_ADVANCE_CROSS) if part)


def _lite_transport_block(phase: str, group_turn: bool) -> str:
    """上游 lite 分支的 ``liteTransport``：content-only 协议句 + 相位传输句（换行拼接）。

    上游先判 ``groupTurn``，再判 ``phase === 'advance'``，其余（含
    user-message / conversation-follow-up / intent-due）一律私聊形态。
    """
    if group_turn:
        return CONTENT_ONLY_TRANSPORT + '\n' + LITE_TRANSPORT_GROUP
    if phase == 'advance':
        return CONTENT_ONLY_TRANSPORT + '\n' + LITE_TRANSPORT_ADVANCE
    return CONTENT_ONLY_TRANSPORT + '\n' + LITE_TRANSPORT_PRIVATE


def lite_blocks(phase: str, group_turn: bool) -> List[str]:
    """上游 ``systemPrompt()`` lite 分支里**不依赖用户配置**的那些块，按原顺序。

    这里给出的是家族 ``generic`` 的形态：``LIVED_LENGTH_PROMPT`` + ``TYPED_MESSAGES_BASE``
    （家族偏移由调用方按 :func:`family_overrides` 自行替换，签名里不带 family/配置参数）。
    相位门控的两块是 ``_phase_instruction()`` 与 ``_lite_transport_block()``；末尾两块
    恒为 CHANNELS / CHANNEL CONTEXT 的 lite 版（与上游 lite 分支同位置）。
    """
    return [
        LIVED_WRITING_PROMPT,
        LIVED_LENGTH_PROMPT,
        TYPED_MESSAGES_BASE,
        FORMAT_AND_REALITY_CONTRACT,
        LITE_JSON_CONTRACT,
        LITE_INTERVAL,
        LITE_UNREAD,
        _phase_instruction(phase, group_turn),
        _lite_transport_block(phase, group_turn),
        LITE_NEVER_INVENT,
        LITE_OWNERSHIP,
        LITE_EVENT_SOURCES,
        LITE_ADMIN_NOTES,
        LITE_WORLD_EVENTS,
    ] + channels_block(True)


def channels_block(lite: bool = False) -> List[str]:
    """CHANNELS + CHANNEL CONTEXT 两块（lite 档是精简版，full/standard 用完整版）。"""
    if lite:
        return [CHANNELS_LITE, CHANNEL_CONTEXT_LITE]
    return [CHANNELS_FULL, CHANNEL_CONTEXT_FULL]
