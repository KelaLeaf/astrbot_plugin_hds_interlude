"""彩色分层日志。

从上游 `src/logging.ts` 逐句移植：动作识别（`detect_log_action`）、字段提取
（`_extract_fields`）、分层排版（`format_layered_log`）、配色主题
（dark/light 两套 256 色盘）。

与上游的唯一差异是**输出通道**：上游直接调 `ctx.logger`，本移植版不依赖
Koishi 运行时，改为模块级可注入 sink（`set_log_sink`），默认写 `sys.stderr`。
AstrBot 侧在适配层把 sink 接到自己的 logger 上即可；日志文本的生成逻辑
与上游完全一致。
"""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Sequence

if TYPE_CHECKING:  # 仅类型标注用；运行期不依赖 core/types.py
    from .types import NarrativePhase

InterludeLogLevel = Literal["error", "warn", "info", "debug"]
InterludeLogFormat = Literal["compact", "detailed", "layered"]
InterludeLogColorTheme = Literal["dark", "light"]
InterludeLogAction = Literal[
    "receive", "send", "processing", "complete", "trigger", "emotion",
    "memory", "advance", "agency", "group", "error", "retry", "warning",
    "waiting", "system",
]

_KAOMOJI: Dict[str, str] = {
    "receive": "(*^▽^*)",
    "send": "(・ω・)ノ",
    "processing": "(•̀ᴗ•́)و",
    "complete": "(ﾉ´ヮ`)ﾉ*: ･ﾟ",
    "trigger": "(๑•̀ㅂ•́)و✧",
    "emotion": "(*>ω<*)",
    "memory": "₍ᐢ- ˕ -ᐢ₎zzZ",
    "advance": "(⊙ω⊙)",
    "agency": "ᕙ( •̀ ᗜ •́ )ᕗ",
    "group": "(´▽｀)ノ",
    "error": "(˶ˊᜊˋ˶)",
    "retry": "(ง •̀_•́)ง",
    "warning": "(´･_･`)",
    "waiting": "(っ˘ω˘ς )",
    "system": "(^_^)/",
}

_SYMBOLS: Dict[str, str] = {
    "receive": "←", "send": "→", "processing": "⋯", "complete": "✓",
    "trigger": "⚡", "emotion": "★", "memory": "◈", "advance": "⟳",
    "agency": "◇", "group": "◎", "error": "✗", "retry": "↻",
    "warning": "!", "waiting": "…", "system": "•",
}

_FIELD_LABELS: Dict[str, str] = {
    "任务": "任务", "模型": "模型", "参与者": "参与者", "时间段": "时间段", "到期计划": "到期计划",
    "耗时": "耗时", "剧本文字": "剧本文字", "回复模式": "回复模式", "成功": "成功", "可见消息": "可见消息",
    "合并消息": "合并消息", "数量": "数量", "数值": "数值", "累计": "累计", "阈值": "阈值", "方向": "方向",
    "强度": "强度", "描述": "描述", "权重": "权重", "错误": "错误", "群": "群聊", "发送者": "发送者",
    "模式": "模式", "条目": "条目", "字符": "字符", "长期事实": "长期事实", "状态变更": "状态变更",
    "时间": "时间", "间隔": "间隔", "等待": "等待", "已投递": "已投递", "原因": "原因", "请求": "请求",
}

# The logger runs on the server and cannot inspect the Console's CSS theme.
# Keep two manually selectable 256-color palettes instead of guessing from a
# terminal's color capability. Dark uses luminous pastels; light uses deeper
# ink colors with enough contrast against white backgrounds.
_COLOR_PALETTES: Dict[str, Dict[str, int]] = {
    "dark": {
        "protagonist": 159,
        "detail": 250,
        "body": 255,
        "user": 81,
        "success": 114,
        "alter": 219,
        "memory": 111,
        "warning": 222,
        "error": 210,
    },
    "light": {
        "protagonist": 24,
        "detail": 240,
        "body": 236,
        "user": 25,
        "success": 28,
        "alter": 90,
        "memory": 25,
        "warning": 130,
        "error": 160,
    },
}

# ---------------------------------------------------------------------------
# 输出通道（上游为 ctx.logger，本移植版改为可注入 sink）
# ---------------------------------------------------------------------------

#: sink 签名：sink(level, text) -> None
LogSink = Callable[[str, str], None]

_sink: Optional[LogSink] = None


def _default_sink(level: str, text: str) -> None:
    """默认 sink：写到 sys.stderr，与上游 logger 的终端行为一致。"""
    print(text, file=sys.stderr)


def set_log_sink(sink: Optional[LogSink]) -> None:
    """注入日志 sink；传 None 恢复默认（sys.stderr）。"""
    global _sink
    _sink = sink


def get_log_sink() -> LogSink:
    """返回当前生效的 sink。"""
    return _sink if _sink is not None else _default_sink


# ---------------------------------------------------------------------------
# 消息插值（Node util.format 的等价物）
# ---------------------------------------------------------------------------

_FORMAT_SPECIFIERS = "sdifjoOc"


def _stringify(value: Any) -> str:
    """近似 Node 的 String(value)（`%s` 用）。"""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _inspect(value: Any) -> str:
    """近似 Node 的 util.inspect（`%o` / `%O` / 多余参数用）。

    Python 的 `%o` 是八进制，语义完全不同，因此这里用 `%r` 承接。
    """
    if isinstance(value, str):
        return repr(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return repr(value)


def _to_number(value: Any) -> Optional[float]:
    """近似 JS 的 Number(value)；无法转换时返回 None（对应 NaN）。"""
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


def _number_text(value: Any) -> str:
    number = _to_number(value)
    if number is None:
        return "NaN"
    if number != number:
        return "NaN"
    if number == float("inf"):
        return "Infinity"
    if number == float("-inf"):
        return "-Infinity"
    if number.is_integer() and abs(number) < 1e21:
        return str(int(number))
    return repr(number)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "[Circular]"


def _extra_text(value: Any) -> str:
    """参数多于占位符时追加的文本（Node 用 util.inspect，字符串不加引号）。"""
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, bool):
        return _stringify(value)
    if isinstance(value, (int, float)):
        return _number_text(value)
    return _inspect(value)


def _stringify_spec(spec: str, value: Any) -> str:
    """按 Node util.format 的语义渲染单个占位符。"""
    if spec == "s":
        return "%s" % (_stringify(value),)
    if spec == "d":
        return _number_text(value)
    if spec == "i":
        number = _to_number(value)
        return "NaN" if number is None else str(int(number))
    if spec == "f":
        number = _to_number(value)
        return "NaN" if number is None else repr(number)
    if spec == "j":
        return _json_text(value)
    if spec in ("o", "O"):
        return _inspect(value)
    if spec == "c":
        return ""
    return "%" + spec


def _format_message(message: str, args: Sequence[Any]) -> str:
    """`node:util` 的 `format()`：`%s/%d/%i/%f/%j/%o/%O/%c/%%` 逐个替换。

    占位符多于参数时，多余的占位符原样保留；参数多于占位符时，剩余参数
    以空格拼接追加到末尾（与 Node 一致）。
    """
    out: List[str] = []
    index = 0
    used = 0
    length = len(message)
    while index < length:
        char = message[index]
        if char != "%" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        spec = message[index + 1]
        if spec == "%":
            out.append("%")
            index += 2
            continue
        if spec in _FORMAT_SPECIFIERS and used < len(args):
            out.append(_stringify_spec(spec, args[used]))
            used += 1
            index += 2
            continue
        out.append(char)
        index += 1
    if used < len(args):
        remainder = " ".join(_extra_text(value) for value in args[used:])
        if out:
            out.append(" ")
        out.append(remainder)
    return "".join(out)


def render_log_message(message: str, args: Optional[Sequence[Any]] = None) -> str:
    """插值日志模板。Error 取 `message`（与上游 `value instanceof Error` 对齐）。"""
    values = [] if args is None else list(args)
    normalized = [str(value) if isinstance(value, BaseException) else value for value in values]
    return _format_message(message, normalized)


# ---------------------------------------------------------------------------
# 动作识别 / 字段布局
# ---------------------------------------------------------------------------

_RE_RETRY = re.compile(r"重试|再次尝试")
_RE_HARD_FAIL = re.compile(r"模型调用失败|主叙事失败|消息投递失败")
_RE_WARN_WORDS = re.compile(r"警告|拦截|不可用|失败")
_RE_TRIGGER = re.compile(r"Alter.*(?:触发|超过阈值)|累积触发")
_RE_COMPLETE_JOB = re.compile(r"(?:模型调用|情绪偏移生成|记忆整理|后台扫描|剧本推进).*完成")
_RE_EMOTION = re.compile(r"情绪偏移|Alter")
_RE_AGENCY = re.compile(r"Agency|主动联系判断|主动联系重查")
_RE_MEMORY = re.compile(r"记忆|压缩|Overlay")
_RE_GROUP = re.compile(r"群消息|群聊|群发言")
_RE_SEND = re.compile(r"投递|发送")
_RE_RECEIVE = re.compile(r"收到|接收|入队")
_RE_PROCESSING = re.compile(r"模型调用开始|分析开始|读取开始|整理开始")
_RE_DONE_WORDS = re.compile(r"完成|成功|已就绪|已启动")
_RE_ADVANCE = re.compile(r"推进|后台扫描")
_RE_WAITING = re.compile(r"等待|计时器|排队")

_RE_FIELD = re.compile(r"(?:^|\s)([\w-]+)=([^=]*?)(?=\s+[\w-]+=|$)")
_RE_TRAILING_PUNCT = re.compile(r"[：:，,]+$")

_RE_CATEGORY_ALTER = re.compile(r"Alter|情绪偏移")
_RE_CATEGORY_AGENCY = re.compile(r"Agency")
_RE_CATEGORY_MEMORY = re.compile(r"记忆|压缩|Overlay")
_RE_CATEGORY_GROUP = re.compile(r"群聊|群消息")

_RE_ROOT_MEMORY_START = re.compile(r"开始")
_RE_ROOT_ADVANCE = re.compile(r"(?:开始|即将执行)")
_RE_ROOT_RECEIVE = re.compile(r"(?:收到|接收)")
_RE_ROOT_GROUP = re.compile(r"收到")
_RE_FINAL_BRANCH = re.compile(r"写作回合完成|扫描完成|整理完成|已注入")
_RE_NOT_FINAL_COMPLETE = re.compile(r"模型调用完成")

_PHASE_LABELS: Dict[str, str] = {
    "user-message": "用户消息",
    "conversation-follow-up": "对话后续",
    "advance": "自动推进",
    "intent-due": "到期意图",
}


def detect_log_action(message: str, level: InterludeLogLevel) -> InterludeLogAction:
    """从日志文本 + 级别推断语义动作（顺序即优先级，与上游一致）。"""
    if level == "error":
        return "error"
    if _RE_RETRY.search(message):
        return "retry"
    if _RE_HARD_FAIL.search(message):
        return "error"
    if level == "warn" or _RE_WARN_WORDS.search(message):
        return "warning"
    if _RE_TRIGGER.search(message):
        return "trigger"
    if _RE_COMPLETE_JOB.search(message):
        return "complete"
    if _RE_EMOTION.search(message):
        return "emotion"
    if _RE_AGENCY.search(message):
        return "agency"
    if _RE_MEMORY.search(message):
        return "memory"
    if _RE_GROUP.search(message):
        return "group"
    if _RE_SEND.search(message):
        return "send"
    if _RE_RECEIVE.search(message):
        return "receive"
    if _RE_PROCESSING.search(message):
        return "processing"
    if _RE_DONE_WORDS.search(message):
        return "complete"
    if _RE_ADVANCE.search(message):
        return "advance"
    if _RE_WAITING.search(message):
        return "waiting"
    return "system"


def phase_label(phase: Optional["NarrativePhase"] = None) -> Optional[str]:
    """叙事阶段的展示名；未知/缺省时返回 '系统'。"""
    if not phase:
        return "系统"
    return _PHASE_LABELS.get(phase)


def _extract_fields(text: str) -> Dict[str, Any]:
    """把 `key=value` 轨迹切成摘要 + 字段表；含换行的文本视为整段摘要。"""
    if "\n" in text:
        return {"summary": text, "fields": []}
    fields: List[Dict[str, str]] = []
    first = -1
    for match in _RE_FIELD.finditer(text):
        if first < 0:
            first = match.start()
        raw = match.group(1)
        value = match.group(2).strip()
        if not value:
            continue
        fields.append({"label": _FIELD_LABELS.get(raw, raw), "value": value})
    if first >= 0:
        summary = _RE_TRAILING_PUNCT.sub("", text[:first].strip())
    else:
        summary = text
    return {"summary": summary, "fields": fields}


def _log_category(
    action: InterludeLogAction,
    phase: Optional["NarrativePhase"] = None,
    standalone: bool = False,
    message: str = "",
) -> str:
    if action == "trigger" or action == "emotion" or _RE_CATEGORY_ALTER.search(message):
        return "[情绪追踪]"
    if action == "agency" or _RE_CATEGORY_AGENCY.search(message):
        return "[主体节奏]"
    if action == "memory" or _RE_CATEGORY_MEMORY.search(message):
        return "[记忆整理]"
    if action == "group" or _RE_CATEGORY_GROUP.search(message):
        return "[群聊]"
    if action == "retry":
        return "[自动重试]"
    if standalone:
        return "[系统]"
    return "[%s]" % (phase_label(phase),)


def _is_root_log(
    summary: str,
    action: InterludeLogAction,
    level: InterludeLogLevel,
    standalone: bool,
) -> bool:
    if standalone or level == "error" or action == "error":
        return True
    if action == "trigger" or (action == "memory" and _RE_ROOT_MEMORY_START.search(summary)):
        return True
    if action == "advance" and _RE_ROOT_ADVANCE.search(summary):
        return True
    if action == "receive" and _RE_ROOT_RECEIVE.search(summary):
        return True
    if action == "group" and _RE_ROOT_GROUP.search(summary):
        return True
    return False


def _is_final_branch(summary: str, action: InterludeLogAction) -> bool:
    if action == "send":
        return True
    if action == "complete" and not _RE_NOT_FINAL_COMPLETE.search(summary):
        return True
    return bool(_RE_FINAL_BRANCH.search(summary))


def _category_color(
    action: InterludeLogAction,
    phase: Optional["NarrativePhase"],
    message: str,
    palette: Dict[str, int],
) -> int:
    if action == "error":
        return palette["error"]
    if action == "warning" or action == "retry":
        return palette["warning"]
    if action == "trigger" or action == "emotion" or _RE_CATEGORY_ALTER.search(message):
        return palette["alter"]
    if action == "agency" or _RE_CATEGORY_AGENCY.search(message):
        return palette["user"]
    if action == "memory" or _RE_CATEGORY_MEMORY.search(message):
        return palette["memory"]
    if action == "complete":
        return palette["success"]
    if phase == "advance":
        return palette["memory"]
    return palette["user"]


def _action_color(action: InterludeLogAction, palette: Dict[str, int]) -> int:
    if action == "error":
        return palette["error"]
    if action == "warning" or action == "retry":
        return palette["warning"]
    if action == "complete" or action == "send":
        return palette["success"]
    if action == "trigger" or action == "emotion":
        return palette["alter"]
    if action == "memory" or action == "advance":
        return palette["memory"]
    if action == "agency":
        return palette["user"]
    return palette["user"]


def _summary_color(
    action: InterludeLogAction,
    level: InterludeLogLevel,
    palette: Dict[str, int],
) -> int:
    if level == "error":
        return palette["error"]
    if level == "warn":
        return palette["warning"]
    if action == "complete":
        return palette["success"]
    return palette["body"]


def _paint(value: str, code: int, enabled: bool = True) -> str:
    if not enabled:
        return value
    basic_ansi = 30 <= code <= 37 or 90 <= code <= 97
    sequence = str(code) if basic_ansi else "38;5;%d" % (code,)
    return "\x1b[%sm%s\x1b[0m" % (sequence, value)


def format_layered_log(data: Dict[str, Any]) -> str:
    """渲染一条分层日志（摘要行 + 缩进字段行）。

    `data` 键与上游 `LayeredLogInput` 一一对应：
    `level` / `phase` / `protagonist` / `message` / `args` / `colors` /
    `color_theme` / `kaomoji` / `standalone`。
    """
    text = render_log_message(data.get("message", ""), data.get("args"))
    action = detect_log_action(text, data.get("level"))
    details = _extract_fields(text)
    summary = details["summary"] or text
    standalone = data.get("standalone") is True
    root = _is_root_log(summary, action, data.get("level"), standalone)
    branch = "" if root else ("└─" if _is_final_branch(summary, action) else "├─")
    category = _log_category(action, data.get("phase"), standalone, text)
    face = _SYMBOLS[action] if data.get("kaomoji") is False else _KAOMOJI[action]
    theme = data.get("color_theme")
    palette = _COLOR_PALETTES["dark" if theme is None else theme]
    colors = data.get("colors")
    colors = True if colors is None else bool(colors)
    if root:
        header = "%s %s" % (
            _paint(category, _category_color(action, data.get("phase"), text, palette), colors),
            _paint(data.get("protagonist") or "HDSI", palette["protagonist"], colors),
        )
    else:
        header = branch
    main = "%s%s%s %s" % (
        header,
        " " if header else "",
        _paint(face, _action_color(action, palette), colors),
        _paint(summary, _summary_color(action, data.get("level"), palette), colors),
    )
    main = main.rstrip()
    fields = details["fields"]
    if not fields:
        return main
    lines = []
    for index, field in enumerate(fields):
        connector = "└─" if index == len(fields) - 1 else "├─"
        lines.append(
            "%s %s %s" % (
                connector if root else "   " + connector,
                _paint(field["label"] + ":", palette["detail"], colors),
                field["value"],
            )
        )
    return "\n".join([main, *lines])


def log_layered(data: Dict[str, Any]) -> str:
    """`format_layered_log` + 投递到 sink，返回渲染出的文本。

    这是"上游 `ctx.logger`"的落点：AstrBot 适配层通过 `set_log_sink`
    把 sink 接到自己的 logger。
    """
    text = format_layered_log(data)
    get_log_sink()(data.get("level", "info"), text)
    return text
