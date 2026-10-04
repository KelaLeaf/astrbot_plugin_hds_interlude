# -*- coding: utf-8 -*-
"""视频理解（v1.9.0；v1.9.1 起预算可配）：抽帧识别 / 原生识别 / 外挂识别。

上游（Koishi `1.0.1-rc28`）没有这一层：入站的 `<video>` 只是一段文本标记，
没有任何"把视频变成模型看得懂的东西"的实现。本移植版按用户要求在**模型中心**新增
`model_center.video` 组，三种模式：

| 模式 | 干什么 | 前提 |
| --- | --- | --- |
| `frames`（默认） | ffmpeg 抽帧 + 单独抽音轨；帧走**现有图像理解通道**、音轨走**现有语音理解通道** | 系统里要有 `ffmpeg`（外部二进制，缺了显式降级） |
| `native` | 把视频原样交给模型 | **本宿主没有这条路**（源码依据见下）→ 显式降级，绝不假装成功 |
| `external` | 把视频交给 `model_id` 指名的模型 | 指名一个能吃视频的 Provider；指名却找不到**失败不回落** |

## 预算全部可配（v1.9.1；常量退化成默认值，**读配置只有一处**）

用户口径：抽帧模式（连续 / 平均）+ 各自的那个量、音轨转码格式、音轨时长
（自定义秒数 / 不限制）、ffmpeg 超时、群聊独立开关。落点：

| 配置键（`model_center.video.*`） | 默认值 | 常量来源 | 作用 |
| --- | --- | --- | --- |
| `frame_mode` | `sequence` | `VIDEO_DEFAULT_FRAME_MODE` | `sequence` 连续抽帧（每 N 秒 1 帧）/ `average` 平均抽帧（整段均分 N 帧） |
| `frame_interval_seconds` | `4` | `VIDEO_FRAME_INTERVAL_SECONDS` | 连续抽帧：每几秒抽 1 帧 |
| `frame_average_count` | `3` | `VIDEO_AVERAGE_FRAMES` | 平均抽帧：整段平均抽几帧（无上界；超出每回合图片预算的帧进不了模型，会留可数线索） |
| `out_format` | `mp3` | `VIDEO_DEFAULT_AUDIO_FORMAT` | 音轨转码输出格式（与「语音 / 音频理解」那组的**同名同义键**） |
| `audio_duration` | `custom` | `VIDEO_DEFAULT_AUDIO_DURATION` | `custom` 按下面的秒数截；`unlimited` 整段都要（只受体积预算与超时兜底） |
| `audio_duration_seconds` | `60` | `VIDEO_AUDIO_CLIP_SECONDS` | `custom` 时取前几秒音轨 |
| `timeout_seconds` | `20` | `VIDEO_FFMPEG_TIMEOUT_SECONDS` | 单条 ffmpeg 命令超时（秒） |
| `group_enabled` | `false` | `VIDEO_DEFAULT_GROUP_ENABLED` | **群聊**的视频理解独立开关（打开后群回合只让**音轨**进去，画面帧没有通道；私聊只受总开关管） |

`resolve_video_config()` 是**唯一**读这段配置的地方（schema 默认值、`CONFIG_DEFAULTS`
与 `VIDEO_CONFIG_DEFAULTS` 三份逐字一致，用例钉着）。

## 超时口径：抽到几帧交几帧（v1.9.1）

旧行为是"超时 → 整段丢弃"。新口径照用户原话：**到达超时时间时，已经抽到磁盘上的帧
照常交出去**（`VideoExtraction.timed_out`），并在正文里留可数线索、打一条可行动的 warn
（"想抽完就调大超时秒数 / 把视频裁短"）。一帧都没抽到时仍是"抽帧失败"那条降级路径。

## 为什么"平均抽帧"要先用 ffmpeg 探时长

平均 = 整段均分 N 帧，命令只能是 `-vf fps=N/时长`（ffmpeg 没有"一共抽 N 帧"的开关）。
时长由 `probe_video` 顺手拿到（探测命令本来就要跑，不额外加进程）；探不到时长时
退回连续抽帧那条 fps（**不猜时长**），帧数上限照旧。

## 为什么 `native` 只能是显式降级（源码依据，别推翻）

1. 宿主 AstrBot 4.28 的 Provider 模态表里**没有 `video`**：
   `astrbot/core/provider/modalities.py` 的 `sanitize_contexts_by_modalities()`
   只判 `supports_image` / `supports_audio` / `supports_tool_use` 三个。
2. 本插件自己的出站链路也只认图片与音频：适配层
   `plugin/adapters/astrbot_bridge.py:3845-3866` 的 content 分段只处理
   `image_url` / `input_audio`，**其余分段只贡献 `part['text']`**（视频分段会被
   静默丢掉）；`:3896-3899` 传给 `context.llm_generate` 的参数只有
   `image_urls` / `audio_urls`。
3. 宿主内部对收到的视频同样只是占位文本（`astrbot/core/astr_main_agent.py:792`
   生成 `[Video: name …, ref …]`）。

所以"把视频原样交给模型"在这台宿主上**不存在通路**；`native` 模式只能是一句
明确说明 + warn。把那句"我看不到视频"说清楚，比编一段画面强。

## 外挂识别拿到的输入形态（必须说清楚）

一次侧任务调用（与共同作品写手同一条旁路：`narrator._side_task_json`），交给指名
Provider 的是**视频直链文本（URL）**，不是字节：宿主出站部件里没有"上传视频"这一种
（同上第 2 条），本地文件也发不出去。因此 `external` 的三条口径：

* `model_id` 空 → 明确失败（**不回落**到抽帧或主模型）；
* 指名了但找不到 / 不可用 → 明确失败（照既有"指名 Provider"的纪律）；
* 视频只有本地文件、没有直链 → 明确失败（宿主没有上传通道）。

## 群聊与合并转发

* **群聊**独立开关（`group_enabled`，默认关=省成本）：群聊里视频刷屏最贵，默认不动它；
  关着时连 `ffmpeg` 都不调（与总开关关着同一条路径）。私聊只看总开关。
  **打开后的真实语义（写实，别读成"群里能看见画面"）**：群回合会为这段视频跑一次
  ffmpeg——**音轨**并进群音频批次（`chunk1.flush_group_turn` 的 `group_audio`，那条
  通道是通的），**帧没有视觉通道可去**（群回合给 `try_decide` 的图片位恒为 `[]`）→
  丢弃，并按 `GROUP_NO_VISION_REASON` 打一条**节流**的可见说明。
  接线见 `collect_group_video_media()`。
  **群回合沿用上面同一套设置**：`frame_mode` / `frame_average_count` /
  `frame_interval_seconds` / `out_format` / `audio_duration(_seconds)` /
  `timeout_seconds` 一项都不特判（都从同一个 `video_config()` 来、走同一个
  `extract_video(...)` 调用点），所以群与私聊的 ffmpeg 命令行**逐字相同**；唯一的差别是
  帧的去处。群里另来一套更省的就等于多一个真相（配置页说什么都不再可信），
  用例 `test_video_understanding.GroupVideoParityTests` 钉着这一点。
* **合并转发里的视频**由 `forward_message.max_videos`（单条转发最多读取的视频数，
  默认 **1**；配 0 才是一段都不读）在 `core/forward_message.py` 那一侧截断，读出来的坐标经
  `SessionView.media`（`kind='video'`）流到这里——**判据只有一处**：本模块的
  `extract_session_video_sources()` 顺带收媒体表里的视频坐标。**v1.9.4 起同一个数也是
  "一回合真正读取几段"的上限**（`video_read_budget()`）：配几段就真读几段，来源比预算多
  时在当前事件里留 `[视频×5，本回合仅取前 1 段]` 并打一条节流的可行动 warn。
  ⚠️ **QQ 空间动态那条路是例外**（v1.9.6）：它自己的上限（`qzone.feed_video_cap` ×
  每回合视觉预算）经 `collect_video_sources(..., limit=…)` 交给这里，**不读本键**。
* **群聊开关只认会话**：`group_enabled` 只管群会话（`_is_group_session()` 读事件的
  `is_direct`）——私聊里**转发**来的视频与它无关，只看总开关。它只管"群里的视频要不要花
  一次 ffmpeg 把声音取出来"，抽出的帧与图片共用**同一个**每回合图片预算。

不 import astrbot（`plugin/core/` 的硬约束）。
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote

# 单卡转发上限（`forward_message.max_videos`）是**一回合真正读取几段视频**的同一处判据，
# 见 `video_read_budget()`。夹取与默认值都留在 `forward_message` 那一处，这里只读结果。
from .forward_message import forward_read_limits

__all__ = [
    'VIDEO_FRAME_INTERVAL_SECONDS', 'VIDEO_AVERAGE_FRAMES', 'VIDEO_MAX_FRAMES',
    'VIDEO_MAX_DURATION_SECONDS', 'VIDEO_MAX_FILE_SIZE_MB', 'VIDEO_AUDIO_CLIP_SECONDS',
    'VIDEO_AUDIO_SAMPLE_RATE', 'VIDEO_FFMPEG_TIMEOUT_SECONDS', 'VIDEO_MAX_CONCURRENCY',
    'VIDEO_WARN_INTERVAL_MS', 'VIDEO_MODES', 'VIDEO_DEFAULT_MODE', 'VIDEO_FRAME_MODES',
    'VIDEO_DEFAULT_FRAME_MODE', 'VIDEO_AUDIO_FORMATS', 'VIDEO_DEFAULT_AUDIO_FORMAT',
    'VIDEO_AUDIO_DURATIONS', 'VIDEO_DEFAULT_AUDIO_DURATION', 'VIDEO_DEFAULT_GROUP_ENABLED',
    'VIDEO_CONFIG_DEFAULTS', 'VIDEO_FACT_PREFIX', 'VIDEO_TASK', 'VIDEO_MODE_HINT',
    'FFMPEG_MISSING_REASON', 'NATIVE_UNSUPPORTED_REASON', 'EXTERNAL_NO_MODEL_REASON',
    'EXTERNAL_MISSING_MODEL_REASON', 'EXTERNAL_NO_URL_REASON', 'VIDEO_TRUNCATED_REASON',
    'VIDEO_TOO_LARGE_REASON', 'VIDEO_BUSY_REASON', 'VIDEO_FRAME_TIMEOUT_PREFIX',
    'VIDEO_FRAME_PARTIAL_PREFIX', 'GROUP_NO_VISION_REASON',
    'FFMPEG_FOUND_LABEL', 'FFMPEG_MISSING_LABEL', 'FFMPEG_STATUS_LABELS',
    'FFMPEG_HINT_TARGETS', 'ffmpeg_hint_field_path', 'ffmpeg_status_hint_targets',
    'ffmpeg_status_group',
    'clip', 'VideoExtraction', 'VideoMedia', 'resolve_video_config', 'video_config',
    'audio_clip_seconds', 'frame_timeout_reason', 'frame_partial_reason', 'ffmpeg_path', 'ffmpeg_available', 'reset_ffmpeg_probe',
    'ffmpeg_status_label', 'apply_ffmpeg_status_hint',
    'apply_ffmpeg_status_hint_or_problem', 'extract_session_video_sources',
    'video_input_target', 'extract_video', 'video_fact_note', 'degradation_message',
    'collect_video_sources', 'collect_group_video_media', 'reset_video_runtime_state',
    'video_read_budget', 'video_turn_budget_note', 'video_turn_budget_warning',
    'VIDEO_TURN_BUDGET_PREFIX',
]

# =========================================================================== #
# 预算常量（**默认值**；可配的项见 `VIDEO_CONFIG_DEFAULTS`，改这里就够了）
# =========================================================================== #

#: 连续抽帧：每 N 秒取 1 帧（`frame_interval_seconds` 的默认值）。
VIDEO_FRAME_INTERVAL_SECONDS = 4
#: 平均抽帧：整段平均抽几帧（`frame_average_count` 的默认值）。
VIDEO_AVERAGE_FRAMES = 3
#: 一条视频最多抽几帧。**与每回合图片预算同量级**（`model_center.vision.max_per_turn`，
#: 默认 3；v1.9.4 起可配，见 `core/vision_budget.py`）——两者同量级是刻意的：
#: 视频帧和直发 / 转发的图片抢**同一个**预算，而这一条是帧自己的硬顶。
#: `frame_average_count` 的**上限**也是它：多抽的帧到不了模型，只是白花时间。
VIDEO_MAX_FRAMES = 3
#: 一条视频最多处理多长（秒）；超出部分只留可数线索，不再抽帧。
VIDEO_MAX_DURATION_SECONDS = 180
#: 本地视频文件的上限（MB）。直链没有本地体积可言，由时长上限 + 命令超时兜底。
VIDEO_MAX_FILE_SIZE_MB = 50
#: 音轨最多取多长（秒；`audio_duration_seconds` 的默认值）。
VIDEO_AUDIO_CLIP_SECONDS = 60
#: 音轨采样率（单声道）：语音模型 / STT 都认的窄带形态，往上传也小。
VIDEO_AUDIO_SAMPLE_RATE = 16_000
#: 单条 ffmpeg 命令的超时（秒；`timeout_seconds` 的默认值）。
VIDEO_FFMPEG_TIMEOUT_SECONDS = 20
#: 同时最多处理几个视频（超出的那条明确 warn 后跳过）。
VIDEO_MAX_CONCURRENCY = 1
#: 能力缺失 / 降级告警的节流间隔（毫秒）。与 `MEDIA_OBSERVABILITY_WARN_INTERVAL_MS` 同档：
#: 能力缺失必须让人看见，但同一条原因不能刷屏。
VIDEO_WARN_INTERVAL_MS = 10 * 60 * 1000

#: 三种识别模式（与 `_conf_schema.json` 的 `options` 逐字一致）。
VIDEO_MODES = ('frames', 'native', 'external')
#: 默认模式：抽帧识别（不吃外部 API、不依赖 Provider 选型）。
VIDEO_DEFAULT_MODE = 'frames'
#: 两种抽帧模式（连续 / 平均）。
VIDEO_FRAME_MODES = ('sequence', 'average')
#: 默认抽帧模式：连续抽帧（等间隔，最省 ffmpeg 的一次探测结果依赖）。
VIDEO_DEFAULT_FRAME_MODE = 'sequence'
#: 音轨转码格式。**逐字等于「语音 / 音频理解」那组的 `out_format` 候选**
#: （`chunk3.fetch_native_audio` 的 `data:audio/<fmt>` 白名单也是这六个）。
VIDEO_AUDIO_FORMATS = ('mp3', 'wav', 'ogg', 'm4a', 'flac', 'amr')
#: 默认音轨格式：与「语音 / 音频理解」的默认同值（mp3 兼容性最好、体积最小）。
VIDEO_DEFAULT_AUDIO_FORMAT = 'mp3'
#: 音轨时长口径：`custom`（按 `audio_duration_seconds` 截）/ `unlimited`（不截）。
VIDEO_AUDIO_DURATIONS = ('custom', 'unlimited')
#: 默认口径：自定义秒数（省字节；`unlimited` 由体积预算与超时兜底）。
VIDEO_DEFAULT_AUDIO_DURATION = 'custom'
#: 群聊视频理解默认开关。**省成本那侧 = 关**：群聊里视频刷屏最费 ffmpeg 与模型调用，
#: 而私聊一条视频是"她真的在看"的强信号。要开就明确去开。
#: 打开后**只让音轨进去**（群回合没有视觉通道，见 `GROUP_NO_VISION_REASON`）——
#: 这个开关的真实语义是"群里的视频要不要花一次 ffmpeg 把声音取出来"。
VIDEO_DEFAULT_GROUP_ENABLED = False

#: `model_center.video` 的默认值（与 schema 默认逐字一致；`CONFIG_DEFAULTS` 也照抄它）。
VIDEO_CONFIG_DEFAULTS: dict[str, Any] = {
    'enabled': False,
    'mode': VIDEO_DEFAULT_MODE,
    'model_id': '',
    'frame_mode': VIDEO_DEFAULT_FRAME_MODE,
    'frame_interval_seconds': VIDEO_FRAME_INTERVAL_SECONDS,
    'frame_average_count': VIDEO_AVERAGE_FRAMES,
    'out_format': VIDEO_DEFAULT_AUDIO_FORMAT,
    'audio_duration': VIDEO_DEFAULT_AUDIO_DURATION,
    'audio_duration_seconds': VIDEO_AUDIO_CLIP_SECONDS,
    'timeout_seconds': VIDEO_FFMPEG_TIMEOUT_SECONDS,
    'group_enabled': VIDEO_DEFAULT_GROUP_ENABLED,
}

#: 音轨格式 → (ffmpeg 复用器, 文件扩展名)。`.m4a` 的复用器叫 `mp4`——两者不同名，
#: 所以这张表必须显式写（别拿格式当复用器用）。
_AUDIO_CONTAINERS: dict[str, tuple[str, str]] = {
    'mp3': ('mp3', 'mp3'),
    'wav': ('wav', 'wav'),
    'ogg': ('ogg', 'ogg'),
    'm4a': ('mp4', 'm4a'),
    'flac': ('flac', 'flac'),
    'amr': ('amr', 'amr'),
}

#: 正文里那句视频事实的**固定前缀**（用例按它断言；也是"这一回合有视频"的机器可读标记）。
VIDEO_FACT_PREFIX = '[视频'
#: 侧任务的中文任务名（用量与日志按它记账；传输层任务键见 `narrator.SIDE_TASK_ROUTES`）。
VIDEO_TASK = '视频理解'


# =========================================================================== #
# 配置（**唯一**读取点）
# =========================================================================== #

def _pick(record: Any, *names: str) -> Any:
    """按给定拼写逐个取值（camelCase / snake_case 双读，与 `service.base.pick` 同语义）。

    这里刻意不 import `service.base`：本模块是**顶层 core 模块**，同目录的
    `works.py` / `forward_message.py` 都只 import 标准库与顶层兄弟模块（避免
    与 `service` 包形成环）。双读语义照抄那里，只有六行。
    """
    if isinstance(record, dict):
        for name in names:
            if name in record:
                return record[name]
        return None
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    return None


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return '' if value is None else str(value)
    return value if isinstance(value, str) else str(value)


def _int_at_least(value: Any, fallback: int, low: int) -> int:
    """读一个整数并夹到 `>= low`（**没有上界**）；读不出来 / bool / 非数 → `fallback`。

    v1.9.7：`model_center.video` 是本移植版新增的一组键，早先四个数值项各有一个
    我们自己拍的上界。上界撤掉之后，值就是用户在配置页看到的那个值。

    "手改坏了配置"与"从没写过"都回到默认值，绝不因为一个脏值把超时变成 0 秒
    （那会把每一条视频都判成超时）。
    """
    if isinstance(value, bool) or value is None:
        return fallback
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return max(low, number)


def resolve_video_config(raw: Any) -> dict[str, Any]:
    """`model_center.video` 段 → 归一化配置（缺键按默认，口径与 schema 一致）。

    缺键一律按**省成本那侧**：关着、抽帧识别、连续抽帧、mp3、自定义 60 秒、群聊关。
    认不出来的枚举值回到默认（用户手改坏了配置也不该悄悄变成"原生识别"或
    "不限制音轨时长"）。
    """
    section = raw if isinstance(raw, dict) else {}
    mode = _text(_pick(section, 'mode')).strip().lower()
    if mode not in VIDEO_MODES:
        mode = VIDEO_DEFAULT_MODE
    frame_mode = _text(_pick(section, 'frameMode', 'frame_mode')).strip().lower()
    if frame_mode not in VIDEO_FRAME_MODES:
        frame_mode = VIDEO_DEFAULT_FRAME_MODE
    out_format = _text(_pick(section, 'outFormat', 'out_format')).strip().lower()
    if out_format not in VIDEO_AUDIO_FORMATS:
        out_format = VIDEO_DEFAULT_AUDIO_FORMAT
    audio_duration = _text(_pick(section, 'audioDuration', 'audio_duration')).strip().lower()
    if audio_duration not in VIDEO_AUDIO_DURATIONS:
        audio_duration = VIDEO_DEFAULT_AUDIO_DURATION
    return {
        'enabled': _pick(section, 'enabled') is True,
        'mode': mode,
        'model_id': _text(_pick(section, 'modelId', 'model_id')).strip(),
        'frame_mode': frame_mode,
        # v1.9.7：这四项**只有下限、没有上界**。它们各自的"上界"（60 秒间隔 /
        # 3 帧 / 1 小时音轨 / 600 秒超时）都是我们自己拍的：用户填 10 帧会静默变 3，
        # 界面与日志里一个字都没有。现在读得出多少就是多少——帧多了会在
        # **可见**的那道闸上被削（帧与直发图片共用每回合图片预算，附可数线索）。
        'frame_interval_seconds': _int_at_least(
            _pick(section, 'frameIntervalSeconds', 'frame_interval_seconds'),
            VIDEO_FRAME_INTERVAL_SECONDS, 1,
        ),
        'frame_average_count': _int_at_least(
            _pick(section, 'frameAverageCount', 'frame_average_count'),
            VIDEO_AVERAGE_FRAMES, 1,
        ),
        'out_format': out_format,
        'audio_duration': audio_duration,
        'audio_duration_seconds': _int_at_least(
            _pick(section, 'audioDurationSeconds', 'audio_duration_seconds'),
            VIDEO_AUDIO_CLIP_SECONDS, 1,
        ),
        'timeout_seconds': _int_at_least(
            _pick(section, 'timeoutSeconds', 'timeout_seconds'),
            VIDEO_FFMPEG_TIMEOUT_SECONDS, 1,
        ),
        'group_enabled': _pick(section, 'groupEnabled', 'group_enabled') is True,
    }


def audio_clip_seconds(config: Any) -> Optional[int]:
    """配置 → 音轨时长（秒）；`unlimited` 回 `None`（= 不给 ffmpeg 传 `-t`）。"""
    section = config if isinstance(config, dict) else {}
    if _text(section.get('audio_duration')).strip().lower() == 'unlimited':
        return None
    value = section.get('audio_duration_seconds')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return VIDEO_AUDIO_CLIP_SECONDS
    return max(1, int(value))


def video_config(service: Any) -> dict[str, Any]:
    """从服务实例的配置里读 `model_center.video`（core 侧的段名是上游的 `model`）。

    ⚠️ 段位必须是 `model` 下的 `video`：schema 分组名是 `model_center`，而上游 /
    core 一律读 `config.model.*`（与 `_vision_config` / `_audio_config` 同一约定）。
    AstrBot 的 `AstrbotConfig` 是 dict 子类，键就是 schema 里的分组名，所以两处都认。
    """
    config = getattr(service, 'config', None)
    section = _pick(config, 'model', 'model_center')
    return resolve_video_config(_pick(section, 'video', 'video_understanding'))


#: 合并转发那一段的组名（新名优先、旧隐藏兼容位兜底）。与适配层
#: `AstrbotBridge.FORWARD_SECTION_NAMES` 同一套顺序 —— 两处读的是同一个用户配置，
#: 兜底顺序不一致就等于"配置页写 A、行为读 B"。
_FORWARD_SECTION_NAMES: tuple[str, ...] = (
    'forward_message', 'forwardMessage', 'forward_message_compat',
)


def _forward_section(service: Any) -> dict[str, Any]:
    """`forward_message` 段（读不到回空字典 = 全默认）。"""
    config = getattr(service, 'config', None)
    for name in _FORWARD_SECTION_NAMES:
        section = _pick(config, name)
        if isinstance(section, dict) and section:
            return section
    return {}


def video_read_budget(service: Any) -> int:
    """一回合真正读取几段视频（v1.9.4）= `forward_message.max_videos`，**判据一处**。

    为什么就是它（而不再另立一个"每回合视频预算"）：视频比图贵一个量级 —— 一段就是
    一次 ffmpeg 抽帧，抽出的帧还与直发 / 转发的图抢同一个每回合图片预算。配了
    "单条转发最多读取 N 段"却只读第 1 段，正是"配置项存在但只有第 1 段生效"那类
    不合逻辑的实现（`collect_video_sources` 早先写死 `sources[0]`）；两处各算一份数字
    就会重演图片那两张卡打架。所以**同一个数**既是单卡取坐标的上限，也是这一回合
    真正读取的上限。

    `0` 时按 **1** 算：`max_videos=0` 管的是"**转发里**的视频一段都不读"
    （转发那一侧连坐标都不收，`forward_message.extract_forward_media`），私聊直发的
    视频不该被一个转发键关掉 —— 那是同一把闸的两处判据。

    夹取（0~10，缺键默认 1）只在 `forward_message.forward_read_limits()` 一处，这里
    不重写区间。
    """
    return max(1, forward_read_limits(_forward_section(service)).max_videos)


def _is_group_session(session: Any) -> bool:
    """这条会话是不是群聊（`SessionView.is_direct`，适配层按事件判出来的那一份）。

    只有**显式** `False` 才算群聊：拿不到这个字段（老适配器 / 手搓 dict 会话）时
    按私聊处理 —— 群聊开关是"省成本"的闸，不该把"判不出来"当成"是群聊"而静默
    什么都不做（那就成了能力缺失无声降级）。生产里 `session_view()` 总会填它。
    """
    direct = _pick(session, 'isDirect', 'is_direct')
    return direct is False


# =========================================================================== #
# FFmpeg 探测与状态提示
# =========================================================================== #

#: `shutil.which('ffmpeg')` 的进程级缓存；哨兵对象区分"还没探过"与"探过、没有"。
_UNPROBED = object()
_FFMPEG_PATH: Any = _UNPROBED


def reset_ffmpeg_probe() -> None:
    """清掉探测缓存（用例与"用户刚装上 ffmpeg"的场景用；不改磁盘）。"""
    global _FFMPEG_PATH
    _FFMPEG_PATH = _UNPROBED


def ffmpeg_path(*, refresh: bool = False) -> str:
    """`ffmpeg` 的绝对路径；没有就回空串。

    **只探一次**（进程级缓存）：这条判据在每条视频消息上都会被读到，而
    `shutil.which` 是文件系统扫描。用 `refresh=True` 或 `reset_ffmpeg_probe()`
    可以重探。
    """
    global _FFMPEG_PATH
    if refresh or _FFMPEG_PATH is _UNPROBED:
        try:
            _FFMPEG_PATH = shutil.which('ffmpeg') or ''
        except Exception:  # noqa: BLE001 - which 在畸形 PATH 上抛过异常，不该带崩加载
            _FFMPEG_PATH = ''
    return _FFMPEG_PATH if isinstance(_FFMPEG_PATH, str) else ''


def ffmpeg_available() -> bool:
    """系统里有没有 `ffmpeg`（外部二进制依赖，宿主不保证装了）。"""
    return bool(ffmpeg_path())


#: 状态提示（用户口径的两个取值；宿主配置页**不支持着色**，所以用文本标记，
#: 见 `apply_ffmpeg_status_hint` 的宿主依据）。
#: **判据只有一处**：`ffmpeg_status_label()` —— 配置页、控制台、日志全都读它。
FFMPEG_FOUND_LABEL = '✅ FFmpeg 已识别'
FFMPEG_MISSING_LABEL = '⚠️ 未发现 FFmpeg'

#: 那两个取值本身。只用于写提示前把**上一次**贴上去的状态剥掉（写第二次不叠罗汉）。
FFMPEG_STATUS_LABELS = (FFMPEG_FOUND_LABEL, FFMPEG_MISSING_LABEL)


def ffmpeg_status_label(*, refresh: bool = False) -> str:
    """配置项旁边那句状态（用户原话的两个取值 + 文本标记）。

    `refresh=True` 重探一次外部二进制——"动态"得真的动态：用户装完 ffmpeg 不重启，
    打开配置页也该看见它翻成 `✅`。重探只发生在**显式刷新点**（插件加载、控制台配置页
    打开），不在每条视频消息都会走的那条读路径上。
    """
    return FFMPEG_FOUND_LABEL if ffmpeg_path(refresh=refresh) else FFMPEG_MISSING_LABEL


#: 识别模式的**静态** hint（用户逐字口径）：状态提示由 `apply_ffmpeg_status_hint`
#: 顶在它前面。这里刻意**不写**任何具体数字——抽帧间隔 / 帧数 / 音轨秒数现在都是
#: 配置项，写死在文案里就是第二个真相（过时即误导）。
VIDEO_MODE_HINT = '需要启用语音原生理解与启用图片理解后抽帧模式才会生效。'

#: 状态提示要贴的 schema 节点 —— **同一份状态文本、同一处判据**，贴两处：
#: 「识别模式」说的是这条状态管什么，「启用视频理解」（总开关）是用户找"视频理解到底
#: 开没开"的第一眼位置。只贴一处是用户报"看不见"的直接原因之一。
#:
#: 每组 = `(schema 节点路径, 接续文本)`；接续文本为 `None` 时接着该节点**自己已有的**
#: hint 写——静态文案的真相在 `_conf_schema.json` 里，这里不复制第二份（否则改一处漏一处）。
FFMPEG_HINT_TARGETS: tuple[tuple[tuple[str, ...], Optional[str]], ...] = (
    (('model_center', 'items', 'video', 'items', 'mode'), VIDEO_MODE_HINT),
    (('model_center', 'items', 'video', 'items', 'enabled'), None),
)


def ffmpeg_hint_field_path(path: Any) -> str:
    """schema 节点路径 → 配置页里的点分字段路径（`items` 只是结构层，不出现）。"""
    if not isinstance(path, (list, tuple)):
        return ''
    return '.'.join(part for part in path if part != 'items')


def _status_free_hint(text: Any) -> str:
    """剥掉开头那句**上一次**贴上去的状态（让写入幂等）。

    不剥的话每打开一次配置页就叠一层「⚠️ 未发现 FFmpeg。⚠️ 未发现 FFmpeg。…」。
    """
    value = text if isinstance(text, str) else ''
    for known in FFMPEG_STATUS_LABELS:
        if value.startswith(known):
            rest = value[len(known):]
            return rest[1:] if rest.startswith('。') else rest
    return value


def _schema_node(root: Any, path: Any) -> Optional[dict]:
    """沿路径取 schema 节点；中途不是 dict / 取不到就回 `None`（绝不抛）。"""
    node = root
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, dict) else None


def ffmpeg_status_hint_targets() -> tuple[str, ...]:
    """状态提示落在哪几个字段上（控制台按这份表找那两项）。"""
    return tuple(ffmpeg_hint_field_path(path) for path, _ in FFMPEG_HINT_TARGETS)


def ffmpeg_status_group() -> str:
    """状态提示所在的分组（schema 顶层键）——控制台按它把状态词挂在分组标题上。"""
    return FFMPEG_HINT_TARGETS[0][0][0]


def apply_ffmpeg_status_hint(schema: Any, *, refresh: bool = False) -> str:
    """把「✅ FFmpeg 已识别 / ⚠️ 未发现 FFmpeg」写进**内存里的** schema（返回状态文本）。

    签名与返回值与 v1.9.0 一致（给日志 / 自检读）；要看"写没写进去、为什么没写进去"
    用 `apply_ffmpeg_status_hint_or_problem()`。

    ## 为什么能动态（宿主源码依据，AstrBot 4.28）

    * schema 是宿主在插件加载时从 `_conf_schema.json` 读一次得到的 **Python 对象**，
      挂在 `AstrBotConfig.schema` 上（`astrbot/core/star/star_manager.py:603-616`
      读取、`:1157-1165` 构造、`:1208` `metadata.config = plugin_config`、
      `:1222-1225` 把**同一个对象**传给插件实例的 `config=`）；
    * 配置页每次打开都现取：`astrbot/dashboard/services/config_service.py:853-872`
      的 `get_plugin_config()` 里是 `"items": plugin_md.config.schema`（**活对象**），
      端点 `astrbot/dashboard/api/plugins.py:721` / `:754`（`ConfigDisplayService.get_configs`
      → 同一个 `get_plugin_config`）；
    * 前端就是把这个字符串塞进 DOM：`astrbot/dashboard/dist/assets/ProviderSelectMenu-*.js`
      的 `G.hint ? ... U(A.__template_key, ie, "hint", G.hint)`（并认 `invisible`）。
    * 而 `AstrBotConfig.save_config()` 只写 `dict(self)`（配置值），
      **schema 从不落盘**（`astrbot/core/config/astrbot_config.py:262-272 / :308 / :339`）。

    ## 为什么**不能**着色（宿主源码依据，AstrBot 4.28；别改成 HTML）

    宿主配置页把 hint / description 一律当**纯文本**渲染：

    * `astrbot/dashboard/dist/assets/ProviderSelectMenu-DArc81Nx.js:26`（该文件是
      一行压缩产物，行号即唯一长行）里每一处 hint 都是
      `b(m(U(A.__template_key, ie, "hint", G.hint)), 1)`（如 `property-hint` /
      `config-hint` 那几处）：`m` = Vue `toDisplayString`、`b` = `createTextVNode`
      ——落成**文本节点**，HTML 会被转义；
    * 同一个文件里 `innerHTML` / `v-html` **出现 0 次**（`grep -c` 实测 0）。

    所以「绿色 / 黄色状态提示」只能退化成文本标记（用户给的替代口径：
    `✅ FFmpeg 已识别` / `⚠️ 未发现 FFmpeg`）。

    ## 写到哪几处

    `FFMPEG_HINT_TARGETS` 那张表：**识别模式**（这条状态管什么）与**启用视频理解**
    （总开关，用户找"视频理解开没开"的第一眼位置）。两处贴的是**同一份文本**，
    判据只有 `ffmpeg_status_label()` 一处，不各算一次。写入是**幂等**的（写之前把
    上一次贴的那句剥掉），所以每个刷新点都可以放心重写。

    拿不到节点（宿主换了形状 / 手搓 schema）时**原样返回状态文本**，把原因交给
    `apply_ffmpeg_status_hint_or_problem()` 的第二个返回值——调用方负责喊出来，
    绝不为了让提示好看而抛异常，也**不再静默吞掉**（v1.9.0 那版是 `except … : pass`）。
    """
    label, _problem = apply_ffmpeg_status_hint_or_problem(schema, refresh=refresh)
    return label


def apply_ffmpeg_status_hint_or_problem(
    schema: Any, *, refresh: bool = False,
) -> tuple[str, str]:
    """同 `apply_ffmpeg_status_hint`，但把"**没写进去**"的原因也带出来。

    返回 `(状态文本, 问题)`：写成功时问题为空串；拿不到 schema 对象 / 缺节点时是一句
    给日志读的话（由调用方打一条 **warn** —— v1.9.0 那版是 `except (KeyError, TypeError):
    pass`，**静默**，于是"用户看不见状态"这件事在日志里毫无痕迹，这正是本轮要修的）。
    """
    label = ffmpeg_status_label(refresh=refresh)
    if not isinstance(schema, dict):
        return label, '拿不到配置 schema（拿到的是 %s）' % type(schema).__name__
    missed: list[str] = []
    for path, static in FFMPEG_HINT_TARGETS:
        node = _schema_node(schema, path)
        if node is None:
            missed.append(ffmpeg_hint_field_path(path))
            continue
        #: 接续文本：`mode` 用模块常量（与 `_conf_schema.json` 里那句同源）；
        #: `enabled` 用它**自己**已有的 hint（静态文案不复制第二份）。
        tail = static if isinstance(static, str) else _status_free_hint(node.get('hint'))
        node['hint'] = ('%s。%s' % (label, tail)) if tail else label
    if missed:
        return label, '配置 schema 里没有这些节点：%s' % '、'.join(missed)
    return label, ''


# =========================================================================== #
# 入站视频坐标（与图片同一条纪律）
# =========================================================================== #

#: 正文里读出来的坐标一律带这个前缀（**永不取回**）：正文是用户可写的，
#: 谁都能手打一句 `<video src="http://内网地址"/>`，从正文认坐标就等于给用户
#: 开一个"让 ffmpeg 去抓任意地址"的口子（与 `chunk3.TEXT_SOURCE_PREFIX` 同一条纪律 §46.8）。
TEXT_SOURCE_PREFIX = 'text:'

#: `<video …/>` 标签里的属性（照 `helpers._parse_mini_xml_elements` 的形状）。
_VIDEO_ATTR_RE = re.compile(r'<video\b([^>]*?)/?>', re.IGNORECASE)
_CQ_VIDEO_RE = re.compile(r'\[CQ:video,([^\]]+)\]', re.IGNORECASE)
_ATTR_RE = re.compile(r'([A-Za-z_:][-\w:.]*)\s*=\s*"([^"]*)"')


def _local_path(value: Any) -> str:
    """`file:///x` / `file://host/x` → 文件系统路径（解 `%20` 之类）。"""
    text = _text(value).strip()
    if text.lower().startswith('file://'):
        text = text[len('file://'):]
        if not text.startswith('/'):
            slash = text.find('/')
            text = text[slash:] if slash >= 0 else ''
        try:
            text = unquote(text)
        except Exception:  # pragma: no cover - 解码失败就用原串
            pass
    return text.strip()


def _trusted_video_source(value: Any) -> str:
    """**可信**来源坐标（适配器直给的段 / 媒体表）→ `onebot-url:` / `onebot-file:`。

    判据只有这一处：元素（`session.elements`）与媒体表（`session.media`）都走它。
    认不出形状（ftp:// 之类）回空串 —— 那不属于"可取回"的那几类。
    """
    text = _text(value).strip()
    if not text:
        return ''
    if re.match(r'^https?://', text, re.IGNORECASE):
        return 'onebot-url:%s' % text
    if text.lower().startswith('file://'):
        local = _local_path(text)
        return 'onebot-file:%s' % local if local else ''
    if re.match(r'^(?:[A-Za-z]:[\\/]|/)', text):
        return 'onebot-file:%s' % text
    return ''


def _element_video_source(element: Any) -> str:
    """一个适配器直给的 `<video>` 元素 → 可信坐标；认不出回空串。"""
    if not isinstance(element, dict):
        return ''
    if _text(element.get('type')).lower() not in ('video', 'video_url'):
        return ''
    attrs = element.get('attrs') if isinstance(element.get('attrs'), dict) else {}
    data = element.get('data') if isinstance(element.get('data'), dict) else {}
    for key in ('src', 'url', 'file'):
        source = _trusted_video_source(attrs.get(key) or data.get(key))
        if source:
            return source
    return ''


def extract_session_video_sources(session: Any) -> list[str]:
    """入站视频坐标（唯一入口）。与图片来源抽取**同一条纪律**：

    * 第一遍只认**适配器直给的元素**（`session.elements`）——适配器从观测到的
      原始段写下来的，是可信坐标（`onebot-url:` / `onebot-file:`）；
    * 第二遍认**结构化媒体表**里 `kind == 'video'` 的条目（`session.media`）：
      合并转发节点里的视频坐标就是这么进来的（数量已由
      `forward_message.max_videos` 在那一侧截断；这里只翻译坐标，不再数一遍）；
    * 文本那一遍（正文里的 `<video src=…/>` 与 `[CQ:video,…]`）一律加 `text:` 前缀，
      `video_input_target` 见到它**永不取回**。
    """
    sources: list[str] = []
    trusted = _pick(session, 'elements')
    if isinstance(trusted, list):
        for element in trusted:
            source = _element_video_source(element)
            if source and source not in sources:
                sources.append(source)
    media = _pick(session, 'media')
    if isinstance(media, list):
        for item in media:
            if not isinstance(item, dict):
                continue
            if _text(item.get('kind')).strip().lower() != 'video':
                continue
            source = _trusted_video_source(item.get('source'))
            if source and source not in sources:
                sources.append(source)
    raw = _text(_pick(session, 'content'))
    for match in _VIDEO_ATTR_RE.finditer(raw):
        attrs = {key.lower(): value for key, value in _ATTR_RE.findall(match.group(1))}
        value = _text(attrs.get('src') or attrs.get('url') or attrs.get('file')).strip()
        entry = '%s%s' % (TEXT_SOURCE_PREFIX, value) if value else ''
        if entry and entry not in sources:
            sources.append(entry)
    for match in _CQ_VIDEO_RE.finditer(raw):
        fields: dict[str, str] = {}
        for part in match.group(1).split(','):
            index = part.find('=')
            if index > 0:
                fields[part[:index].strip().lower()] = part[index + 1:].strip()
        value = _text(fields.get('url') or fields.get('file')).strip()
        entry = '%s%s' % (TEXT_SOURCE_PREFIX, value) if value else ''
        if entry and entry not in sources:
            sources.append(entry)
    return sources


def video_input_target(source: Any) -> tuple[str, str]:
    """来源坐标 → `('file'|'url'|'', 目标)`。

    * `onebot-file:` / `file://…` / 裸绝对路径 → 本地文件（**唯一读本地文件的口子**，
      只能由适配器直给的元素 / 媒体表产生）；
    * `onebot-url:` → 直链（适配器给的平台 CDN 地址）；
    * `text:` 前缀 → `('', '')`：**永不处理**（正文坐标，用户可写）；
    * 裸 http(s) 只在**没有** `text:` 时当直链（手搓 `SessionView` / 桌面桥的坐标）。
    """
    value = _text(source).strip()
    if not value or value.startswith(TEXT_SOURCE_PREFIX):
        return '', ''
    if value.startswith('onebot-file:'):
        local = _local_path(value[len('onebot-file:'):])
        return ('file', local) if local else ('', '')
    if value.startswith('onebot-url:'):
        url = value[len('onebot-url:'):].strip()
        return ('url', url) if re.match(r'^https?://', url, re.IGNORECASE) else ('', '')
    if re.match(r'^https?://', value, re.IGNORECASE):
        return 'url', value
    if value.lower().startswith('file://'):
        local = _local_path(value)
        return ('file', local) if local else ('', '')
    if re.match(r'^(?:[A-Za-z]:[\\/]|/)', value):
        return 'file', value
    return '', ''


# =========================================================================== #
# ffmpeg 子进程
# =========================================================================== #

#: `ffmpeg` 输出的时长行（`Duration: 00:01:23.45`）。
_DURATION_RE = re.compile(r'Duration:\s*(\d+):(\d{1,2}):(\d{2}(?:\.\d+)?)')
#: 音轨流（`Stream #0:1(und): Audio: aac …`）。
_AUDIO_STREAM_RE = re.compile(r'Stream #\d+:\d+.*?: Audio:', re.IGNORECASE)


def _run_ffmpeg(binary: str, args: list[str], timeout: float) -> tuple[int, str, str]:
    """跑一条 ffmpeg 命令（**同步**；异步调用方用 `asyncio.to_thread`）。

    `-nostdin` 必须有：ffmpeg 会抢终端输入，在服务进程里表现为随机挂住。
    返回 `(returncode, stdout, stderr)`；`FileNotFoundError` 当"二进制没了"（1）。
    `TimeoutExpired` **不吞**：上抛给调用方判降级（它知道这是超时还是别的原因）。
    """
    completed = subprocess.run(  # noqa: S603 - 参数是列表，不经 shell
        [binary, *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors='replace', timeout=timeout, check=False,
    )
    return completed.returncode, completed.stdout or '', completed.stderr or ''


@dataclass(frozen=True)
class VideoExtraction:
    """一次抽帧 + 抽音轨的结果（**只描述事实**，降级文本由 `video_fact_note` 拼）。"""

    #: 抽出来的帧文件绝对路径（按时间顺序；≤ 帧数上限）。
    frames: tuple[str, ...] = ()
    #: 抽出来的音轨文件绝对路径（没有音轨 / 抽取失败时为空串）。
    audio_path: str = ''
    #: 探测到的时长（秒）；探不到是 `0.0`。
    duration_seconds: float = 0.0
    #: 视频比 `VIDEO_MAX_DURATION_SECONDS` 长、只处理了前一段。
    truncated: bool = False
    #: 抽帧失败的原因（空串 = 没失败）。
    frame_error: str = ''
    #: 抽音轨失败 / 没有音轨的原因（空串 = 成功或没试）。
    audio_error: str = ''
    #: 视频里有没有音轨（探测结论；探不到按"有"处理，别把"不知道"说成"没有"）。
    has_audio: bool = True
    #: 抽帧命令**超时**（`frames` 里就是超时前已经落到磁盘的那些帧）。
    timed_out: bool = False
    #: 本次用的临时目录（调用方读完帧字节后要删掉它，见 `collect_video_sources`）。
    workdir: str = ''

    @property
    def frame_count(self) -> int:
        return len(self.frames)


def probe_video(binary: str, target: str, timeout: float) -> tuple[float, bool, str]:
    """探时长与有没有音轨：`(时长秒, 有音轨, 错误)`。

    用 `ffmpeg -i <target>` 本身（只读文件头，退出码是 1）：**不引入 ffprobe**
    这第二个二进制——用户装了 ffmpeg 不等于装了 ffprobe（静态构建里常常只有一个）。
    时长探不到时回 `0.0`，调用方据此**不声称截断**（"不知道"不等于"很短"），
    也不拿它算平均抽帧的 fps（见 `_frame_filter`）。
    """
    try:
        _, _, stderr = _run_ffmpeg(binary, ['-hide_banner', '-nostdin', '-i', target], timeout)
    except subprocess.TimeoutExpired:
        return 0.0, True, '探测超时'
    except OSError as error:
        return 0.0, True, '探测失败：%s' % error
    match = _DURATION_RE.search(stderr)
    duration = 0.0
    if match:
        duration = (
            int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
        )
    has_audio = bool(_AUDIO_STREAM_RE.search(stderr)) or not re.search(
        r'Stream #\d+:\d+', stderr, re.IGNORECASE,
    )
    return duration, has_audio, ''


def _frame_filter(
    frame_mode: str, duration: float, *, interval_seconds: int, average_frames: int,
    max_duration_seconds: int,
) -> tuple[str, int]:
    """抽帧模式 → `(-vf 的值, 帧数上限)`。

    * `sequence`：`fps=1/每几秒`，上限 = 视觉预算 `VIDEO_MAX_FRAMES`（与直发图共用）；
    * `average`：整段均分 N 帧 → `fps=N/时长`，上限 = N（N 就是 `frame_average_count`，
      v1.9.7 起**没有上界**；时长探不到时退回连续抽帧那条 fps，不猜时长）。

    两个模式都受同一件事实约束：**多抽的帧到不了模型**（帧和图片抢同一个每回合预算，
    见 `core/vision_budget.py`）——所以平均抽帧抽得再多，真正交给模型的仍由那道
    **可见**的每回合图片预算裁（附可数线索），而不是在这里被一个看不见的常量削掉。
    """
    frames_cap = max(1, int(average_frames)) if frame_mode == 'average' else VIDEO_MAX_FRAMES
    if frame_mode == 'average':
        span = min(duration, float(max_duration_seconds)) if duration > 0 else 0.0
        if span > 0:
            return 'fps=%.6f' % (max(1, int(average_frames)) / span), frames_cap
        return 'fps=1/%d' % max(1, int(interval_seconds)), frames_cap
    return 'fps=1/%d' % max(1, int(interval_seconds)), frames_cap


def _collect_frames(directory: str, cap: int) -> list[str]:
    """把目录里已经写出的帧按序号收回来（**超时 / 失败之后也要收**）。

    只认非空文件：超时那一瞬间 ffmpeg 可能刚建好下一个文件、还没写字节，
    空文件交出去只会让视觉通道多一次必然失败的取字节。
    """
    try:
        names = os.listdir(directory)
    except OSError:  # pragma: no cover - 目录被外力删掉
        return []
    frames: list[str] = []
    for name in sorted(names):
        if not (name.startswith('frame-') and name.endswith('.jpg')):
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.getsize(path) <= 0:
                continue
        except OSError:  # pragma: no cover
            continue
        frames.append(path)
    return frames[:max(1, int(cap))]


def extract_video(
    target: str,
    *,
    binary: str = '',
    workdir: str = '',
    frame_mode: str = VIDEO_DEFAULT_FRAME_MODE,
    interval_seconds: int = VIDEO_FRAME_INTERVAL_SECONDS,
    average_frames: int = VIDEO_AVERAGE_FRAMES,
    max_duration_seconds: int = VIDEO_MAX_DURATION_SECONDS,
    audio_seconds: Optional[int] = VIDEO_AUDIO_CLIP_SECONDS,
    audio_format: str = VIDEO_DEFAULT_AUDIO_FORMAT,
    with_audio: bool = True,
    timeout: float = VIDEO_FFMPEG_TIMEOUT_SECONDS,
) -> VideoExtraction:
    """抽帧 + 单独抽音轨（**同步**、阻塞；异步调用方走 `asyncio.to_thread`）。

    两条命令（`fps` 由抽帧模式算，`-t` 只在音轨口径是"自定义秒数"时出现）：

    ```
    ffmpeg -hide_banner -loglevel error -nostdin -y -i <target> -t <max_duration>
           -vf <fps=…> -frames:v <上限> -q:v 3 <dir>/frame-%02d.jpg
    ffmpeg -hide_banner -loglevel error -nostdin -y -i <target> [-t <audio_seconds>]
           -vn -ac 1 -ar 16000 -f <复用器> <dir>/audio.<ext>
    ```

    单条命令各自的失败**互不牵连**：抽帧失败不影响音轨，音轨没有也不影响帧
    （"无音轨 → 只走图像不失败"）。命令超时同样只影响那一条。

    **超时口径（v1.9.1，用户原话）**：抽帧超时时**不丢弃**已经写到磁盘的帧——
    `timed_out=True` 且 `frames` 就是那些帧，调用方照常交出去并在正文里留可数线索。

    `with_audio=False` 时**连音轨那条命令都不发**（调用方已经知道语音理解那条通道
    关着，抽出来也会被丢掉 —— 省一次 ffmpeg 与音轨的字节）。

    `audio_seconds=None` = 音轨时长不限制（不给 `-t`）：整段都要，由体积预算
    （`audio.max_file_size_mb`）与这里的超时兜底。
    """
    binary = binary or ffmpeg_path()
    if not binary:
        return VideoExtraction(frame_error='没检查到 FFmpeg')
    directory = workdir or tempfile.mkdtemp(prefix='hdsi-video-')
    os.makedirs(directory, exist_ok=True)
    duration, has_audio, _probe_error = probe_video(binary, target, timeout)
    truncated = bool(duration and duration > max_duration_seconds)

    filter_value, frames_cap = _frame_filter(
        frame_mode, duration, interval_seconds=interval_seconds,
        average_frames=average_frames, max_duration_seconds=max_duration_seconds,
    )
    frames: list[str] = []
    frame_error = ''
    timed_out = False
    pattern = os.path.join(directory, 'frame-%02d.jpg')
    try:
        code, _, stderr = _run_ffmpeg(binary, [
            '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', target, '-t', str(max_duration_seconds),
            '-vf', filter_value,
            '-frames:v', str(frames_cap),
            '-q:v', '3', pattern,
        ], timeout)
        if code != 0:
            frame_error = _last_error_line(stderr) or '抽帧命令退出码 %d' % code
    except subprocess.TimeoutExpired:
        # **超时不丢帧**：进程已经被 subprocess 杀掉并回收，磁盘上写好的帧还在。
        timed_out = True
        frame_error = '抽帧超时（超过 %d 秒）' % timeout
    except OSError as error:
        frame_error = '抽帧失败：%s' % error
    # 无条件回收磁盘上的帧：成功、非零退出、超时三条路都可能是"抽到了一部分"。
    frames = _collect_frames(directory, frames_cap)
    if not frame_error and not frames:
        frame_error = '抽帧没有得到任何一帧'

    audio_path = ''
    audio_error = ''
    if has_audio and with_audio:
        container, extension = _AUDIO_CONTAINERS.get(
            _text(audio_format).strip().lower(), _AUDIO_CONTAINERS[VIDEO_DEFAULT_AUDIO_FORMAT],
        )
        candidate = os.path.join(directory, 'audio.%s' % extension)
        args = ['-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-i', target]
        if audio_seconds is not None:
            args += ['-t', str(max(1, int(audio_seconds)))]
        args += ['-vn', '-ac', '1', '-ar', str(VIDEO_AUDIO_SAMPLE_RATE), '-f', container, candidate]
        try:
            code, _, stderr = _run_ffmpeg(binary, args, timeout)
            if code != 0:
                audio_error = _last_error_line(stderr) or '音轨命令退出码 %d' % code
            elif not os.path.exists(candidate) or os.path.getsize(candidate) <= 44:
                audio_error = '这条视频没有音轨'
            else:
                audio_path = candidate
        except subprocess.TimeoutExpired:
            audio_error = '抽音轨超时（超过 %d 秒）' % timeout
        except OSError as error:
            audio_error = '抽音轨失败：%s' % error
    elif not with_audio:
        audio_error = ''
    else:
        audio_error = '这条视频没有音轨'

    return VideoExtraction(
        frames=tuple(frames), audio_path=audio_path, duration_seconds=duration,
        truncated=truncated, frame_error=frame_error, audio_error=audio_error,
        has_audio=has_audio, timed_out=timed_out, workdir=directory,
    )


def _last_error_line(stderr: str) -> str:
    """取 ffmpeg stderr 的最后一条非空行（`-loglevel error` 下就是错误本身）。"""
    for line in reversed([item.strip() for item in _text(stderr).splitlines()]):
        if line:
            return line[:300]
    return ''


# =========================================================================== #
# 降级文本（可行动；用例逐字/逐段断言）
# =========================================================================== #

def _seconds_label(value: float) -> str:
    total = int(round(max(0.0, value)))
    if total < 60:
        return '%d 秒' % total
    return '%d 分 %d 秒' % (total // 60, total % 60)


def video_fact_note(
    *,
    duration_seconds: float = 0.0,
    frame_count: int = 0,
    truncated: bool = False,
    has_audio: bool = False,
    audio_attempted: bool = True,
    frame_error: str = '',
    degrade_reason: str = '',
    frame_mode: str = VIDEO_DEFAULT_FRAME_MODE,
    interval_seconds: int = VIDEO_FRAME_INTERVAL_SECONDS,
    average_frames: int = VIDEO_AVERAGE_FRAMES,
    audio_seconds: Optional[int] = VIDEO_AUDIO_CLIP_SECONDS,
    max_duration_seconds: int = VIDEO_MAX_DURATION_SECONDS,
    timed_out: bool = False,
    timeout_seconds: int = VIDEO_FFMPEG_TIMEOUT_SECONDS,
) -> str:
    """正文里那句视频事实（含**可数线索**：时长 / 抽了几帧 / 音频取了几秒 / 有没有截断）。

    措辞与 `describe_user_event` 给图片 / 语音写的那几句同一条尺子：只报形式与边界，
    不报画面（"没看到就别编"）。数字全部来自**这一次生效的配置**（不写死常量）。
    `degrade_reason` 非空时那句降级说明顶在最前面——视频没有完整进入任何模型时，
    必须让模型知道"这一段你确实没看见 / 只看见了多少"。
    """
    parts: list[str] = []
    if degrade_reason:
        parts.append(degrade_reason)
    if duration_seconds:
        parts.append('约 %s' % _seconds_label(duration_seconds))
    if truncated:
        parts.append('超过 %d 秒上限，只看了前 %d 秒' % (max_duration_seconds, max_duration_seconds))
    if frame_count:
        if frame_mode == 'average':
            parts.append('已整段平均抽了 %d 帧画面' % frame_count)
        else:
            parts.append('已按每 %d 秒 1 帧抽了 %d 帧画面' % (interval_seconds, frame_count))
        if timed_out:
            # 超时但交出去了：把"这不是全部"说清楚（可数：超时上限 + 已交付帧数）。
            parts.append('抽帧在 %d 秒超时，已抽到的 %d 帧照常提交' % (timeout_seconds, frame_count))
    elif frame_error and not degrade_reason:
        # `degrade_reason` 已经说过抽帧失败时不再重复（同一件事说两遍是噪音）。
        parts.append('抽帧失败')
    if has_audio:
        if audio_seconds is None:
            parts.append('已单独抽出整段音轨')
        else:
            parts.append('已单独抽出前 %d 秒音轨' % int(audio_seconds))
    elif audio_attempted and not degrade_reason:
        parts.append('没有音轨')
    body = '，'.join(parts) if parts else '没取到内容'
    return '%s：%s；画面与声音以本轮原生输入为准，没提供的内容保持未知。]' % (VIDEO_FACT_PREFIX, body)


#: 抽帧识别缺 FFmpeg 时的**可行动**说明（用户原话：装了 ffmpeg 才会有抽帧识别）。
FFMPEG_MISSING_REASON = (
    '本机没检查到 FFmpeg，抽帧识别没跑起来：装上 FFmpeg（命令行里能执行 ffmpeg -version）'
    '并重载插件后才会生效'
)
#: `native` 模式的显式降级说明（源码依据见模块头）。
NATIVE_UNSUPPORTED_REASON = (
    '当前宿主（AstrBot）没有把视频原样交给模型的通路'
    '（出站内容部件只有图片与音频、模态表里也没有 video），这条视频没有交给任何模型；'
    '想看画面请把「视频识别模式」改成「抽帧识别」'
)
#: `external` 没指名模型。
EXTERNAL_NO_MODEL_REASON = (
    '外挂识别需要先在「外挂视频理解模型」里指名一个模型，这条视频没有交给任何模型'
)
#: `external` 指名了却没找到（**失败不回落**）。
EXTERNAL_MISSING_MODEL_REASON = (
    '指名的外挂视频理解模型不存在或不可用，这条视频没有交给任何模型'
)
#: `external` 只有本地文件、没有直链。
EXTERNAL_NO_URL_REASON = (
    '外挂识别只能把视频直链交给模型（宿主没有上传视频的通道），'
    '这条视频只有本地文件，没有交给任何模型'
)
#: 抽帧失败、但音轨拿到了（部分降级）。
VIDEO_FRAME_FAILED_REASON = '这条视频抽帧失败（只取到了音轨）'
#: 音轨抽到了、但超过语音理解那条通道自己的体积预算。
VIDEO_AUDIO_OVER_BUDGET_REASON = '抽出的音轨超过语音通道的体积预算'
#: 并发上限挡住的那条。
VIDEO_BUSY_REASON = (
    '上一个视频还在抽帧（并发上限 %d），这条视频本次没有抽帧' % VIDEO_MAX_CONCURRENCY
)
#: 视频太大 / 太小 / 抽不出东西。
VIDEO_TOO_LARGE_REASON = '视频文件超过 %d MB 上限' % VIDEO_MAX_FILE_SIZE_MB
VIDEO_MISSING_FILE_REASON = '视频文件取不到'
VIDEO_EMPTY_REASON = '视频没有抽出任何画面或音轨'
#: 时长超过上限（只处理了前一段）。**不进 `video_fact_note`**：那句话本身已经写明
#: "超过 N 秒上限，只看了前 N 秒"，再叠一句降级说明就是同一件事说两遍。
VIDEO_TRUNCATED_REASON = '视频超过 %d 秒上限，只处理了前 %d 秒' % (
    VIDEO_MAX_DURATION_SECONDS, VIDEO_MAX_DURATION_SECONDS,
)

#: 一回合的视频段数被**单卡上限**截断（v1.9.4）这条降级原因的**前缀**：
#: `degradation_message` 用包含匹配认它，带计数的完整句子也能拿到同一句可行动的提示。
VIDEO_TURN_BUDGET_PREFIX = '本回合的视频段数被「单条转发最多读取的视频数」截断'


def video_turn_budget_reason(available: int, granted: int) -> str:
    """超过一回合读取预算时的降级原因（可数：本回合几段 / 只读了前几段）。"""
    return '%s（本回合 %d 段，只读了前 %d 段）' % (
        VIDEO_TURN_BUDGET_PREFIX, int(available), int(granted),
    )


def video_turn_budget_warning(available: Any, granted: Any) -> str:
    """超预算时那条 warn 文案（可数：本回合几段 / 只读了前几段）。"""
    try:
        total = int(available)
        taken = int(granted)
    except (TypeError, ValueError):
        return ''
    return (
        '本回合收到 %d 段视频，按「单条转发最多读取的视频数」只读了前 %d 段'
        '（每多读一段多一次 ffmpeg 抽帧）。'
    ) % (total, max(0, taken))


def video_turn_budget_note(available: Any, granted: Any) -> str:
    """超预算时那句可数线索（短；**只有真的截了才说**）——与图片那句同一族。

    形态 `[视频×5，本回合仅取前 1 段]`：卡的旁注（`[视频×5，仅取前 1 段]`）管的是
    "从**这一张卡**里取几段坐标"，这句管的是"**整个回合**真正读了几段"（多张卡 /
    直发多个视频时两者才会不一样）。
    """
    try:
        total = int(available)
        taken = int(granted)
    except (TypeError, ValueError):
        return ''
    if total <= 0 or taken >= total:
        return ''
    return '[视频×%d，本回合仅取前 %d 段]' % (total, max(0, taken))

#: 抽帧超时的降级原因**前缀**：`degradation_message` 用包含匹配认它，所以带计数的
#: 完整原因（`frame_timeout_reason()`）也能拿到同一句可行动的提示。别把它改成一个
#: 只在完整句子里出现的写法——那样 warn 就断了。
VIDEO_FRAME_TIMEOUT_PREFIX = '抽帧超时'


def frame_timeout_reason(timeout_seconds: int, frame_count: int) -> str:
    """超时但抽到了帧的降级原因（带可数线索：超时上限 + 已交付帧数）。"""
    return '%s（超过 %d 秒）：只抽到 %d 帧，已照常提交，剩下的没有抽' % (
        VIDEO_FRAME_TIMEOUT_PREFIX, int(timeout_seconds), int(frame_count),
    )


#: 抽帧命令**中途报错**（非零退出）但仍写出了若干帧时的降级原因前缀。
#: 不进 `_ACTIONABLE_REASONS`：这是单条视频的偶然失败（撞上坏尾 / 平台直链断了），
#: 不是能力或配置问题——线索写进正文就够了，不刷 warn（与"没有音轨"同一条尺子）。
VIDEO_FRAME_PARTIAL_PREFIX = '抽帧中断'


def frame_partial_reason(frame_count: int) -> str:
    """抽帧中断但拿到了若干帧的降级原因（带可数线索）。"""
    return '%s：只拿到 %d 帧，已照常提交' % (VIDEO_FRAME_PARTIAL_PREFIX, int(frame_count))


#: **群聊回合没有视觉通道**：`chunk1.flush_group_turn` 给 `try_decide` 的图片位恒为 `[]`
#: （群消息也不带 `imageSources`），所以帧抽出来也无处可去。这是**唯一**一句要用户看见的
#: 说明——它既是正文事实的降级前缀，也是 `note_access_skip` 的节流键（同故事同原因 10 分钟
#: 一条）。别把它改成只有内部人才懂的名词：用户就是靠这句话知道"为什么开了开关也看不见画面"。
GROUP_NO_VISION_REASON = '这段视频来自群聊，而群回合没有视觉通道，抽出的画面帧没有提交给模型'


#: 降级原因 → 一条**可行动**的 warn（`service.note_access_skip` 的 message）。
#: 只放"能力 / 配置"层面的原因：单条视频的偶然失败（比如这段没有音轨）不该刷 warn。
_ACTIONABLE_REASONS: dict[str, str] = {
    GROUP_NO_VISION_REASON: (
        '视频理解：这条视频来自群聊，而群回合没有视觉通道（群里发的图片同样进不去），'
        '抽出的画面帧没有提交给模型。音轨走的是「语音 / 音频理解」那条通道，'
        '那条总开关关着时声音也不会进去。想让她看见视频画面，请在私聊里发。'
    ),
    FFMPEG_MISSING_REASON: (
        '视频理解降级：本机没检查到 FFmpeg，抽帧识别不可用；'
        '装上 FFmpeg（命令行里能执行 ffmpeg -version）并重载插件后才会生效。'
    ),
    NATIVE_UNSUPPORTED_REASON: (
        '视频理解降级：「原生识别」在当前宿主上没有通路'
        '（AstrBot 出站内容部件只有图片与音频、模态表里也没有 video）；'
        '把「视频识别模式」改成「抽帧识别」（需要 FFmpeg）或「外挂识别」才有画面。'
    ),
    EXTERNAL_NO_MODEL_REASON: (
        '视频理解失败：「外挂识别」没有指名模型，已按不识别处理（不回落）。'
        '请在「模型中心 → 视频理解」的「外挂视频理解模型」里指名一个模型。'
    ),
    EXTERNAL_MISSING_MODEL_REASON: (
        '视频理解失败：「外挂识别」指名的模型不存在或不可用，已按不识别处理'
        '（**不回落**到抽帧或主模型）。请检查那个模型是否还在 AstrBot 里且已启用。'
    ),
    EXTERNAL_NO_URL_REASON: (
        '视频理解失败：「外挂识别」只认视频直链，而这条视频只有本地文件'
        '（当前宿主没有上传视频的通道），已按不识别处理。'
        '想看这条视频请改用「抽帧识别」。'
    ),
    VIDEO_TOO_LARGE_REASON: (
        '视频理解降级：视频文件超过 %d MB 上限，已跳过抽帧（正文照旧）。'
        '要处理大视频请先自行压缩。' % VIDEO_MAX_FILE_SIZE_MB
    ),
    VIDEO_EMPTY_REASON: (
        '视频理解降级：这条视频既没抽出画面也没抽出音轨，已按"只收到一段视频"处理。'
        '请确认这个文件是完整可播的视频，或检查日志里 ffmpeg 的报错。'
    ),
    VIDEO_AUDIO_OVER_BUDGET_REASON: (
        '视频理解降级：抽出的音轨超过语音理解那条通道的体积预算，这一段声音没有进模型'
        '（画面照常）。想连声音一起给可以调大「模型中心 → 语音 / 音频理解设置」里的'
        '「单条音频上限 MB」。'
    ),
    VIDEO_FRAME_FAILED_REASON: (
        '视频理解降级：这条视频抽帧失败了（只取到了音轨），画面对她不可见。'
        '请检查日志里 ffmpeg 的报错，或确认这个文件是完整可播的视频。'
    ),
    VIDEO_MISSING_FILE_REASON: (
        '视频理解降级：视频文件读不到（可能已被清理或路径失效），已按"只收到一段视频"处理。'
    ),
    VIDEO_BUSY_REASON: (
        '视频理解降级：上一个视频还在抽帧（并发上限 %d），这条视频本次跳过了抽帧，'
        '正文照旧。' % VIDEO_MAX_CONCURRENCY
    ),
    VIDEO_TRUNCATED_REASON: (
        '视频理解降级：视频超过 %d 秒上限，只处理了前 %d 秒（正文里留了这条线索）。'
        '整段都要看的话请先自行裁剪。' % (VIDEO_MAX_DURATION_SECONDS, VIDEO_MAX_DURATION_SECONDS)
    ),
    # 包含匹配（键是前缀）：完整句子里带着"超时上限 + 已交付帧数"，那句话本身就是线索。
    VIDEO_FRAME_TIMEOUT_PREFIX: (
        '视频理解降级：抽帧超时了，**已经抽到的帧照常交给模型**（正文里写了帧数），'
        '剩下的没有抽。想抽完请调大「模型中心 → 视频理解」的「抽帧超时秒数」，'
        '或把视频裁短。'
    ),
}


def degradation_message(reason: str) -> str:
    """降级原因 → 可行动的 warn 文案（没有对应文案时回空串 = 不刷 warn）。

    先精确匹配，再做一次包含匹配：外挂识别失败时原因里会再挂上底层报错
    （`指名的…（Connection error）`），抽帧超时那条带着计数，都该拿到同一句可行动的提示。
    """
    text = _text(reason).strip()
    if not text:
        return ''
    if text in _ACTIONABLE_REASONS:
        return _ACTIONABLE_REASONS[text]
    for key, message in _ACTIONABLE_REASONS.items():
        if key and key in text:
            return message
    return ''


def clip(value: Any, limit: int) -> str:
    """有界截断（与 `service/helpers.clip` 同语义；本模块不 import service）。"""
    text = _text(value)
    return text if len(text) <= limit else text[:limit]


# =========================================================================== #
# 并发闸（不让一个视频卡住回合）
# =========================================================================== #

_VIDEO_IN_FLIGHT = 0


def reset_video_runtime_state() -> None:
    """清空并发计数（用例用；正常路径靠 `try/finally` 归还）。"""
    global _VIDEO_IN_FLIGHT
    _VIDEO_IN_FLIGHT = 0


def _acquire_slot() -> bool:
    global _VIDEO_IN_FLIGHT
    if _VIDEO_IN_FLIGHT >= max(1, int(VIDEO_MAX_CONCURRENCY)):
        return False
    _VIDEO_IN_FLIGHT += 1
    return True


def _release_slot() -> None:
    global _VIDEO_IN_FLIGHT
    _VIDEO_IN_FLIGHT = max(0, _VIDEO_IN_FLIGHT - 1)


# =========================================================================== #
# 回合接线
# =========================================================================== #

@dataclass
class VideoMedia:
    """一次视频理解的产物（调用方只认这三个字段 + `note`）。

    * `image_sources`：抽出来的帧，作为**现有图像理解通道**的来源
      （`load_native_images`，与直发 / 转发的图片共用**同一个**每回合预算，
      见 `core/vision_budget.py`）；
    * `audio_sources`：抽出来的音轨，作为**现有语音理解通道**的来源
      （`load_native_audio` 认的 `data:audio/<fmt>;base64,…`）；
    * `note`：进当前事件的正文事实（空串 = 一个字都不加）；
    * `workdir` / `workdirs`：临时目录，**调用方读完帧字节后**必须删（`cleanup()`）。
      一回合读多段视频就有多个目录（每段一次 ffmpeg）：`workdir` 是第一个（老调用方与
      老用例只认它），其余的都在 `workdirs` 里 —— 少删一个就是每回合漏一个临时目录。
    """

    mode: str = VIDEO_DEFAULT_MODE
    image_sources: list[str] = field(default_factory=list)
    audio_sources: list[str] = field(default_factory=list)
    note: str = ''
    reason: str = ''
    workdir: str = ''
    #: 第二段起的临时目录（见 `cleanup()`）。
    workdirs: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        """删掉本次的全部临时目录（幂等；失败不抛）。"""
        first = self.workdir
        self.workdir = ''
        rest = list(self.workdirs)
        self.workdirs = []
        for directory in [first, *rest]:
            if not directory:
                continue
            try:
                shutil.rmtree(directory, ignore_errors=True)
            except Exception:  # noqa: BLE001 - 临时文件删不掉不该影响回合
                pass

    def _add_workdir(self, directory: str) -> None:
        """记下一个临时目录（第一个进 `workdir`，其余进 `workdirs`）。"""
        if not directory:
            return
        if not self.workdir:
            self.workdir = directory
            return
        if directory != self.workdir and directory not in self.workdirs:
            self.workdirs.append(directory)


def _audio_data_uri(path: str, audio_format: str, max_bytes: float) -> str:
    """音轨文件 → `data:audio/<fmt>;base64,…`（超预算 / 空文件回空串）。

    格式段用**裸格式名**（`mp3` / `wav` / …）而不是真 MIME：`chunk3.fetch_native_audio`
    的白名单就是这六个裸名（`data:audio/([a-z0-9]+)` 之后按 `mp3|wav|ogg|m4a|flac|amr`
    判），写成 `audio/mpeg` 会被那条判据拒掉（"静默没声音"）。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return ''
    if size <= 44 or size > max_bytes:
        return ''
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError:
        return ''
    fmt = _text(audio_format).strip().lower()
    if fmt not in VIDEO_AUDIO_FORMATS:
        fmt = VIDEO_DEFAULT_AUDIO_FORMAT
    return 'data:audio/%s;base64,%s' % (fmt, base64.b64encode(data).decode('ascii'))


def _local_size_bytes(target: str) -> int:
    try:
        return os.path.getsize(target)
    except OSError:
        return -1


def _warn(service: Any, story: Any, reason: str, message: Optional[str] = None) -> None:
    """按会话节流地打一条降级 warn（`note_access_skip`：同一原因 10 分钟一条）。

    `message` 给得出可数文案的调用方（如回合视频预算）直接传那一句；不传就走
    `degradation_message(reason)` 的静态映射。
    """
    text = message or degradation_message(reason)
    if not text:
        return
    note = getattr(service, 'note_access_skip', None)
    story_id = _text(_pick(story, 'id')) or '-'
    if callable(note):
        try:
            note('video|%s|%s' % (story_id, reason), VIDEO_WARN_INTERVAL_MS, text)
            return
        except Exception:  # noqa: BLE001 - 报告失败不该带崩回合
            pass
    # 兜底：宿主 / 替身没提供节流口时**照样要说话** —— 只是少了节流，
    # 绝不能因为"节流口不在"就把能力缺失吞成静默（坑 25）。
    report = getattr(service, 'report_standalone', None)
    if not callable(report):
        return
    try:
        report('warn', text)
    except Exception:  # noqa: BLE001
        pass


async def _collect_external(
    service: Any, config: dict[str, Any], target: str,
) -> tuple[str, str]:
    """`external`：一次侧任务调用，把视频直链交给指名的 Provider（**失败不回落**）。

    返回 `(降级原因, 模型观察)`：原因空串 = 成功交给了模型，此时第二项是它的回答。
    """
    model_id = _text(config.get('model_id')).strip()
    if not model_id:
        return EXTERNAL_NO_MODEL_REASON, ''
    if not target:
        return EXTERNAL_NO_URL_REASON, ''
    narrator = getattr(service, 'narrator', None)
    side_task = getattr(narrator, '_side_task_json', None)
    if not callable(side_task):
        return EXTERNAL_MISSING_MODEL_REASON, ''
    try:
        from .model_routing import is_assigned_to, provider_reachable  # noqa: PLC0415
    except ImportError:  # pragma: no cover - 模块缺失时按失败处理
        return EXTERNAL_MISSING_MODEL_REASON, ''
    section = _pick(getattr(service, 'config', None), 'model', 'model_center')
    rows = [
        row for row in (_pick(section, 'providers') or [])
        if isinstance(row, dict) and row.get('enabled') is not False and provider_reachable(row)
    ]
    # 用途勾选「用于视频理解」（`use_for_video`）或适配层为"指名 AstrBot Provider"
    # 合成的那条连接行——两者都挂这个标志。
    bound = [row for row in rows if is_assigned_to(row, 'video')]
    if not bound:
        # 指名却找不到 → 明确失败，**不回落**（连"退回去抽帧"都不做：那是另一套判据，
        # 会让用户以为配错了也能跑）。
        return EXTERNAL_MISSING_MODEL_REASON, ''
    provider = bound[0]
    model = _text(provider.get('model') or model_id).strip() or model_id
    prompt = (
        '这是一段视频的直链，请观看并给出客观观察：\n%s\n\n'
        '请只描述你在视频里实际看到 / 听到的内容；看不到就说看不到，不要猜测。' % target
    )

    def build_body(capped: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            'model': model,
            'temperature': 0.2,
            'messages': [
                {'role': 'system', 'content': '你负责客观描述视频内容，不做文学加工。'},
                {'role': 'user', 'content': prompt},
            ],
        }
        # 首轮带 1200 的输出预算（省成本那侧：这段观察最终只留 800 字符，见函数末尾），
        # `_side_task_json` 在**首轮输出不可解析**时去掉它重试一次。
        #
        # v1.9.7 修：这一支原来是反的（`if not capped`）——首轮**不带**预算，只有
        # "模型返回空"的重试才带，既与同族所有 `build_body` 相反，又让这个数只在一条
        # 几乎走不到的分支上生效。现在与同族一致：**首轮带 cap、重试放开**；真被截断时
        # `narrator._warn_output_truncated()` 会点名说出来（本轮新加的可见截断）。
        if capped:
            body['max_tokens'] = 1_200
        return body

    def parse(text: str) -> str:
        return _text(text)

    try:
        observation = await side_task(provider, model, VIDEO_TASK, 60_000, build_body, parse)
    except Exception as error:  # noqa: BLE001 - 外挂失败即失败，不回落
        return '%s（%s）' % (EXTERNAL_MISSING_MODEL_REASON, error), ''
    return '', clip(observation, 800)


async def collect_video_sources(
    service: Any, story: Any, session: Any, *, limit: Any = None,
) -> VideoMedia:
    """抽帧识别（主路）：拿到帧与音轨的**来源坐标**，交给现有那两条通道去取。

    返回的 `image_sources` / `audio_sources` 由调用方并进它本来就有的
    `image_sources` / `audio_sources` —— 帧走 `load_native_images`、音轨走
    `load_native_audio`，**一条判据、一套实现**，这里不另造通道。

    四道闸的顺序（都在任何 ffmpeg / 模型调用之前）：

    1. `enabled=False` → 直接回空（一个 ffmpeg 都不调、一个模型都不调）；
    2. 这条会话没有视频坐标 → 回空；
    3. **群聊且 `group_enabled=False`** → 回空（群聊独立开关，默认关=省成本）。
       ⚠️ 这条只看**会话是不是群聊**（`is_direct`）：私聊里转发来的视频与群聊开关无关
       （私聊只看总开关）；
    4. 一回合真正读取的视频段数：缺省 = `forward_message.max_videos`（v1.9.4，
       `video_read_budget()`），第 N 段之后的坐标在这里截断，**线索与 warn 都可见**
       —— 早先写死 `sources[0]`，于是"配了 3 段也只读第 1 段"。

    `limit`（关键字，可省）是**调用方自带的那道闸**：QQ 空间动态那条路的上限由
    `qzone.feed_video_cap` × 每回合视觉预算决定（`chunk13._qzone_feed_video_lines`），
    与合并转发没有任何关系。给了它就用它截断，并且**不在这里留线索 / warn**——
    调用方自己会点名真正生效的那一道闸（这里再报一遍就会指向一个与本条路无关的键）。
    它只夹下限（`max(1, …)`），上限由调用方的配置区间负责。

    配置只在这里读一次（`video_config`），下面全部按它传参。
    """
    result = VideoMedia(mode=VIDEO_DEFAULT_MODE)
    config = video_config(service)
    result.mode = config['mode']
    if not config['enabled']:
        return result
    sources = extract_session_video_sources(session)
    if not sources:
        return result
    if _is_group_session(session) and not config['group_enabled']:
        # 群聊开关关着：与总开关关着同一条路径（连坐标都不再看一眼）。
        return result

    if config['mode'] == 'native':
        result.reason = NATIVE_UNSUPPORTED_REASON
        result.note = video_fact_note(degrade_reason=result.reason)
        _warn(service, story, result.reason)
        return result

    # 一回合真正读取的视频段数（缺省判据一处：`video_read_budget()`；调用方自带 `limit`
    # 时以它为准，见 docstring）。多段按顺序读，帧与音轨都并进同一份结果；截断时正文里
    # 留条数、日志里留一条可行动的 warn。
    #
    # 计数只算**取得到坐标**的来源：正文里那些 `text:` 坐标永不取回（用户可写），
    # 把它们算进"本回合有几段视频"会报出一个假条数（同一个视频既在元素里、又在正文里
    # 出现时尤其明显）。
    readable = [source for source in sources if video_input_target(source)[1]]
    owns_note = limit is None
    if owns_note:
        budget = video_read_budget(service)
    else:
        try:
            budget = max(1, int(limit))
        except (TypeError, ValueError):
            budget = 1
    targets = readable[:budget]
    notes: list[str] = []
    if owns_note:
        turn_note = video_turn_budget_note(len(readable), len(targets))
        if turn_note:
            notes.append(turn_note)
            _warn(
                service, story, video_turn_budget_reason(len(readable), len(targets)),
                video_turn_budget_warning(len(readable), len(targets)),
            )
    for source in targets:
        one = await _collect_one_video(service, story, config, source)
        result.image_sources.extend(one.image_sources)
        result.audio_sources.extend(one.audio_sources)
        result._add_workdir(one.workdir)
        if one.note:
            notes.append(one.note)
        if one.reason and not result.reason:
            # 多条视频各有各的降级原因时，**第一条**就是 `reason`（warn 已经在各自的
            # 分路上打过了，这里不再重复报一遍同一条）。
            result.reason = one.reason
    result.note = '\n'.join(notes)
    return result


async def _collect_one_video(
    service: Any, story: Any, config: dict[str, Any], source: str,
) -> VideoMedia:
    """**一段**视频：`external` 外挂识别 / `frames` 抽帧识别（一行一段，各自降级）。

    抽出来的只有**来源坐标**（帧是本地文件、音轨是 `data:` URI）与正文事实；真正的取回
    由调用方那两条既有通道做。多段视频就是把这个函数按顺序调用多次
    （见 `collect_video_sources()`）。
    """
    result = VideoMedia(mode=config['mode'])
    kind, target = video_input_target(source)
    if config['mode'] == 'external':
        if kind == 'file':
            # 宿主出站部件没有"上传视频"这一种：本地文件发不出去。
            result.reason = EXTERNAL_NO_URL_REASON
            result.note = video_fact_note(degrade_reason=result.reason)
            _warn(service, story, result.reason)
            return result
        reason, observation = await _collect_external(service, config, target)
        result.reason = reason
        if reason:
            result.note = video_fact_note(degrade_reason=reason)
            _warn(service, story, reason)
        else:
            # 外挂模型自己的观察就是这一回合的画面来源：它没有走原生视觉通道，
            # 所以只能以事实的形式进正文（措辞标明来源，免得被当成她自己看到的）。
            result.note = '%s：外挂识别（%s）给出的观察：%s]' % (
                VIDEO_FACT_PREFIX, config['model_id'], observation or '（模型没有返回内容）',
            )
        return result

    # ---- frames（默认） ----
    if not ffmpeg_available():
        result.reason = FFMPEG_MISSING_REASON
        result.note = video_fact_note(degrade_reason=result.reason)
        _warn(service, story, result.reason)
        return result
    if kind == 'file':
        size = _local_size_bytes(target)
        if size < 0:
            result.reason = VIDEO_MISSING_FILE_REASON
            result.note = video_fact_note(degrade_reason=result.reason)
            return result
        if size > VIDEO_MAX_FILE_SIZE_MB * 1024 * 1024:
            result.reason = VIDEO_TOO_LARGE_REASON
            result.note = video_fact_note(degrade_reason=result.reason)
            _warn(service, story, result.reason)
            return result
    if not target:
        # 坐标只在正文里（`text:`）：**永不取回**。这里不报 warn ——
        # "拿不到坐标"不是用户配置问题，与图片那条惰性纪律一致。
        return result
    if not _acquire_slot():
        result.reason = VIDEO_BUSY_REASON
        result.note = video_fact_note(degrade_reason=result.reason)
        _warn(service, story, result.reason)
        return result
    audio_enabled = _audio_channel_enabled(service)
    audio_seconds = audio_clip_seconds(config)
    try:
        extraction = await asyncio.to_thread(
            extract_video, target,
            frame_mode=config['frame_mode'],
            interval_seconds=config['frame_interval_seconds'],
            average_frames=config['frame_average_count'],
            audio_seconds=audio_seconds,
            audio_format=config['out_format'],
            with_audio=audio_enabled,
            timeout=float(config['timeout_seconds']),
        )
    except Exception as error:  # noqa: BLE001 - 抽帧出任何岔子都不许带崩回合
        extraction = VideoExtraction(frame_error='抽帧异常：%s' % error)
    finally:
        _release_slot()

    result.workdir = extraction.workdir
    result.image_sources = ['onebot-file:%s' % path for path in extraction.frames]
    audio_budget = _audio_budget_bytes(service)
    notes = {'audio_over_budget': False}
    if extraction.audio_path and audio_enabled:
        uri = _audio_data_uri(extraction.audio_path, config['out_format'], audio_budget)
        if uri:
            result.audio_sources = [uri]
        else:
            # 音轨与语音走**同一条**通道，就得服从那条通道自己的预算：超了会被
            # `fetch_native_audio` 丢掉（那就成了"静默没声音"），所以这里提前判、
            # 并且**明说**（`VIDEO_AUDIO_OVER_BUDGET_REASON` 那条 warn 是可行动的）。
            notes['audio_over_budget'] = True
            result.reason = VIDEO_AUDIO_OVER_BUDGET_REASON
    if result.image_sources or result.audio_sources:
        if extraction.timed_out and result.image_sources and not result.reason:
            # **超时但抽到了帧**：照常交出去（用户口径），并把"这不是全部"说清楚。
            result.reason = frame_timeout_reason(
                config['timeout_seconds'], len(result.image_sources),
            )
        elif extraction.frame_error and result.image_sources and not result.reason:
            # 非零退出（撞上坏尾 / 直链断了）但写出了若干帧：同样是"只交付了一部分"，
            # 留可数线索、不刷 warn（单条视频的偶然失败）。
            result.reason = frame_partial_reason(len(result.image_sources))
        elif extraction.frame_error and not result.image_sources and not result.reason:
            # 画面一帧都没有、只有声音：**也是降级**，得让人看见（用户点名的
            # "抽帧失败 / 超时 → 明确降级 + 可行动的 warn"）。具体报错只在日志里。
            result.reason = VIDEO_FRAME_FAILED_REASON
        result.note = video_fact_note(
            duration_seconds=extraction.duration_seconds,
            frame_count=extraction.frame_count,
            truncated=extraction.truncated,
            has_audio=bool(result.audio_sources),
            # 音轨是"抽到了、被预算挡下来"时不能说成"这条视频没有音轨"。
            audio_attempted=bool(audio_enabled and not notes['audio_over_budget']),
            frame_error=extraction.frame_error,
            degrade_reason=result.reason,
            frame_mode=config['frame_mode'],
            interval_seconds=config['frame_interval_seconds'],
            average_frames=config['frame_average_count'],
            audio_seconds=audio_seconds,
            timed_out=extraction.timed_out,
            timeout_seconds=config['timeout_seconds'],
        )
        if result.reason:
            _warn(service, story, result.reason)
        if extraction.truncated:
            # 截断也是降级（"她只看了前 180 秒"）：线索进正文，warn 让用户看得见。
            _warn(service, story, VIDEO_TRUNCATED_REASON)
        return result
    result.reason = (
        extraction.frame_error if extraction.frame_error in _ACTIONABLE_REASONS
        else VIDEO_EMPTY_REASON
    )
    result.note = video_fact_note(
        duration_seconds=extraction.duration_seconds,
        frame_error=extraction.frame_error,
        degrade_reason=result.reason,
        audio_seconds=audio_seconds,
        timed_out=extraction.timed_out,
        timeout_seconds=config['timeout_seconds'],
    )
    _warn(service, story, result.reason)
    return result


async def collect_group_video_media(service: Any, story: Any, session: Any) -> VideoMedia:
    """**群聊回合**的视频接线：帧没有通道可去，音轨有（判据仍然只有 `collect_video_sources`）。

    与私聊的差别只有一处：群回合给 `try_decide` 的图片位恒为 `[]`
    （`chunk1.flush_group_turn`；群消息也不带 `imageSources`，§46 的既有设计），
    所以帧抽出来也没处可去——"开着开关却什么都不发生"正是用户最恼的那类误导，
    所以这里**必须**留下一条看得见的说明：

    * **帧**：丢掉，并按原因打一条**节流**的 warn（`GROUP_NO_VISION_REASON`，
      同故事同原因 10 分钟一条）。绝不静默。
    * **音轨**：`audio_sources` 原样交给调用方并进群音频批次——那条通道是**通的**
      （`chunk1` 的 `group_audio` 就是它），所以开关打开时**至少让声音进去**。
    * **正文事实**：`note` 按"帧没进去"**重写**。`collect_video_sources` 那句会声称
      "抽了 N 帧画面"，在群聊那是假的——模型不该以为她看见了画面。

    开关链路与私聊共用：总开关关 / 群开关关 / 没有视频坐标时，这里一个 ffmpeg 都不调
    （提前 return，连 `image_sources` 都是空）。
    """
    media = await collect_video_sources(service, story, session)
    if not media.image_sources:
        return media
    frames = len(media.image_sources)
    media.image_sources = []
    config = video_config(service)
    # 用**同一个** `video_fact_note` 造句，只把"抽了 N 帧"那句换成"帧没进去"。
    #
    # `audio_attempted` 也要**读配置**（`model.audio.enabled`）而不是写死 True：
    # 语音通道关着时 `collect_video_sources` 连音轨那条命令都不发，句子就不该说"试过取音轨"。
    # 群里的配置项**一项都不许特判**（用户口径："群里沿用上面同一套设置"），这条和
    # `frame_mode` / `out_format` / `audio_duration` 一样，只是它落在句子而不是命令行上。
    media.note = video_fact_note(
        degrade_reason='%s（抽到的 %d 帧已丢弃）' % (GROUP_NO_VISION_REASON, frames),
        has_audio=bool(media.audio_sources),
        audio_attempted=bool(_audio_channel_enabled(service)),
        audio_seconds=audio_clip_seconds(config),
        frame_mode=config['frame_mode'],
        interval_seconds=config['frame_interval_seconds'],
        average_frames=config['frame_average_count'],
        timeout_seconds=config['timeout_seconds'],
    )
    _warn(service, story, GROUP_NO_VISION_REASON)
    return media


def _audio_channel_enabled(service: Any) -> bool:
    """`model.audio.enabled`（语音理解总开关）：与 `load_native_audio` 读的是**同一个键**。

    关着时那条通道本来就会丢下音轨，所以抽帧识别**连抽都不抽**（省一次 ffmpeg 与
    音轨的字节）；正文里也就不会声称"抽出了音轨"。
    """
    section = _pick(_pick(getattr(service, 'config', None), 'model', 'model_center'), 'audio')
    return _pick(section, 'enabled') is True


def _audio_budget_bytes(service: Any) -> float:
    """现有音频预算（`audio.max_file_size_mb`，默认 10MB）→ 字节。

    音轨要与语音走**同一条**通道，就得服从那条通道自己的预算：超了就会被
    `fetch_native_audio` 丢掉（那就成了"静默没声音"），所以这里提前判。
    """
    section = _pick(_pick(getattr(service, 'config', None), 'model', 'model_center'), 'audio')
    try:
        megabytes = float(_pick(section, 'maxFileSizeMB', 'max_file_size_mb') or 10)
    except (TypeError, ValueError):
        megabytes = 10.0
    return max(1.0, megabytes) * 1024 * 1024
