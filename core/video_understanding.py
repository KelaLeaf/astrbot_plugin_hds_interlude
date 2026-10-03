# -*- coding: utf-8 -*-
"""视频理解（v1.9.0）：抽帧识别 / 原生识别 / 外挂识别。

上游（Koishi `1.0.1-rc28`）没有这一层：入站的 `<video>` 只是一段文本标记，
没有任何"把视频变成模型看得懂的东西"的实现。本移植版按用户要求在**模型中心**新增
`model_center.video` 组，三种模式：

| 模式 | 干什么 | 前提 |
| --- | --- | --- |
| `frames`（默认） | ffmpeg 间隔抽帧 + 单独抽音轨；帧走**现有图像理解通道**、音轨走**现有语音理解通道** | 系统里要有 `ffmpeg`（外部二进制，缺了显式降级） |
| `native` | 把视频原样交给模型 | **本宿主没有这条路**（源码依据见下）→ 显式降级，绝不假装成功 |
| `external` | 把视频交给 `model_id` 指名的模型 | 指名一个能吃视频的 Provider；指名却找不到**失败不回落** |

## 抽帧识别的预算（全部是有界常量）

* **间隔抽帧**：每 `VIDEO_FRAME_INTERVAL_SECONDS` 秒 1 帧，最多 `VIDEO_MAX_FRAMES` 帧。
  帧数上限**照直发视觉预算定**——`chunk3.load_native_images` 里就是 `sources[:3]`，
  视频帧与直发图片**共用**那一个 3 张的预算，这里刻意不另立第二套。
* **音轨**：`-vn -ac 1 -ar 16000` 转 16k 单声道 wav，最多取前
  `VIDEO_AUDIO_CLIP_SECONDS` 秒；再按现有音频预算（`audio.max_file_size_mb`）交给
  `load_native_audio`（`data:audio/wav;base64,…` 正是它认的输入形态之一）。
* **时长**：超过 `VIDEO_MAX_DURATION_SECONDS` 只处理前 N 秒，并在正文留可数线索。
* **体积**：本地文件超过 `VIDEO_MAX_FILE_SIZE_MB` 直接降级（直链没有本地体积，
  由时长上限与命令超时兜底——见 `extract_video` 的说明）。
* **超时 / 并发**：单条 ffmpeg 命令 `VIDEO_FFMPEG_TIMEOUT_SECONDS` 秒；同时最多
  `VIDEO_MAX_CONCURRENCY` 个视频在处理，超出的那条**明确 warn 后跳过**，
  不让一个视频卡住回合。

## 降级一律不静默（坑 25）

ffmpeg 不存在 / 抽帧失败 / 命令超时 / 无音轨 / 视频超时长或体积 —— 每一条都：
① 正文里留一句**可行动**的说明（`video_fact_note`，含可数线索）；
② 打一条按会话节流的 `warn`（走 `service.note_access_skip`，同一原因 10 分钟一条）；
③ **绝不因此丢消息**（回合照常，视频退化成"收到了一段视频"这条事实）。

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
from typing import Any
from urllib.parse import unquote

__all__ = [
    'VIDEO_FRAME_INTERVAL_SECONDS', 'VIDEO_MAX_FRAMES', 'VIDEO_MAX_DURATION_SECONDS',
    'VIDEO_MAX_FILE_SIZE_MB', 'VIDEO_AUDIO_CLIP_SECONDS', 'VIDEO_AUDIO_SAMPLE_RATE',
    'VIDEO_FFMPEG_TIMEOUT_SECONDS', 'VIDEO_MAX_CONCURRENCY', 'VIDEO_WARN_INTERVAL_MS',
    'VIDEO_MODES', 'VIDEO_DEFAULT_MODE', 'VIDEO_CONFIG_DEFAULTS', 'VIDEO_FACT_PREFIX',
    'VIDEO_TASK', 'VIDEO_MODE_HINT', 'FFMPEG_MISSING_REASON', 'NATIVE_UNSUPPORTED_REASON',
    'EXTERNAL_NO_MODEL_REASON', 'EXTERNAL_MISSING_MODEL_REASON', 'EXTERNAL_NO_URL_REASON',
    'VIDEO_TRUNCATED_REASON', 'VIDEO_TOO_LARGE_REASON', 'VIDEO_BUSY_REASON', 'clip',
    'VideoExtraction', 'VideoMedia', 'resolve_video_config', 'video_config',
    'ffmpeg_path', 'ffmpeg_available', 'reset_ffmpeg_probe', 'ffmpeg_status_label',
    'apply_ffmpeg_status_hint', 'extract_session_video_sources', 'video_input_target',
    'extract_video', 'video_fact_note', 'degradation_message', 'collect_video_sources',
    'reset_video_runtime_state',
]

# =========================================================================== #
# 预算常量（有界；改这里就够了，别在调用点再写一遍数字）
# =========================================================================== #

#: 间隔抽帧：每 N 秒取 1 帧。
VIDEO_FRAME_INTERVAL_SECONDS = 4
#: 一条视频最多抽几帧。**等于直发视觉预算**（`chunk3.load_native_images` 的
#: `sources[:3]`）——两者同量级是刻意的：视频帧和直发图片抢同一个 3 张预算。
VIDEO_MAX_FRAMES = 3
#: 一条视频最多处理多长（秒）；超出部分只留可数线索，不再抽帧。
VIDEO_MAX_DURATION_SECONDS = 180
#: 本地视频文件的上限（MB）。直链没有本地体积可言，由时长上限 + 命令超时兜底。
VIDEO_MAX_FILE_SIZE_MB = 50
#: 音轨最多取多长（秒）。
VIDEO_AUDIO_CLIP_SECONDS = 60
#: 音轨采样率（单声道）：语音模型 / STT 都认的窄带形态，往上传也小。
VIDEO_AUDIO_SAMPLE_RATE = 16_000
#: 单条 ffmpeg 命令的超时（秒）。
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
#: `model_center.video` 的默认值（与 schema 默认逐字一致）。
VIDEO_CONFIG_DEFAULTS: dict[str, Any] = {'enabled': False, 'mode': VIDEO_DEFAULT_MODE, 'model_id': ''}

#: 正文里那句视频事实的**固定前缀**（用例按它断言；也是"这一回合有视频"的机器可读标记）。
VIDEO_FACT_PREFIX = '[视频'
#: 侧任务的中文任务名（用量与日志按它记账；传输层任务键见 `narrator.SIDE_TASK_ROUTES`）。
VIDEO_TASK = '视频理解'


# =========================================================================== #
# 配置
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


def resolve_video_config(raw: Any) -> dict[str, Any]:
    """`model_center.video` 段 → 归一化配置（缺键按默认，口径与 schema 一致）。

    缺键一律按**省成本那侧**：`enabled=False`（关）、`mode='frames'`、`model_id=''`。
    `mode` 认不出来时回到 `frames`（用户手改坏了配置也不该悄悄变成"原生识别"）。
    """
    section = raw if isinstance(raw, dict) else {}
    mode = _text(_pick(section, 'mode')).strip().lower()
    if mode not in VIDEO_MODES:
        mode = VIDEO_DEFAULT_MODE
    return {
        'enabled': _pick(section, 'enabled') is True,
        'mode': mode,
        'model_id': _text(_pick(section, 'modelId', 'model_id')).strip(),
    }


def video_config(service: Any) -> dict[str, Any]:
    """从服务实例的配置里读 `model_center.video`（core 侧的段名是上游的 `model`）。

    ⚠️ 段位必须是 `model` 下的 `video`：schema 分组名是 `model_center`，而上游 /
    core 一律读 `config.model.*`（与 `_vision_config` / `_audio_config` 同一约定）。
    AstrBot 的 `AstrbotConfig` 是 dict 子类，键就是 schema 里的分组名，所以两处都认。
    """
    config = getattr(service, 'config', None)
    section = _pick(config, 'model', 'model_center')
    return resolve_video_config(_pick(section, 'video', 'video_understanding'))


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


def ffmpeg_status_label() -> str:
    """配置项旁边那句状态（用户原话的两个取值）。"""
    return 'FFmpeg 已识别' if ffmpeg_available() else '未检查到 FFmpeg'


#: 配置项的**静态** hint：无论探测结果如何都要说清"抽帧识别需要 FFmpeg"。
#: 动态那句由 `apply_ffmpeg_status_hint` 顶在这个前缀上（见那边的说明）。
VIDEO_MODE_HINT = (
    '抽帧识别需要系统装有 FFmpeg（能执行 ffmpeg -version）：'
    '按每 %d 秒 1 帧、最多 %d 帧抽画面，并单独抽前 %d 秒音轨，'
    '分别走图片理解与语音理解模型。'
) % (VIDEO_FRAME_INTERVAL_SECONDS, VIDEO_MAX_FRAMES, VIDEO_AUDIO_CLIP_SECONDS)


def apply_ffmpeg_status_hint(schema: Any) -> str:
    """把「FFmpeg 已识别 / 未检查到 FFmpeg」写进**内存里的** schema（返回状态文本）。

    ## 为什么能动态（宿主源码依据，AstrBot 4.28）

    * schema 是宿主在插件加载时从 `_conf_schema.json` 读一次得到的 **Python 对象**，
      挂在 `AstrbotConfig.schema` 上（`astrbot/core/star/star_manager.py:603-616`
      读取、`:1157-1165` 构造、`:1208` `metadata.config = plugin_config`、
      `:1223-1226` 把**同一个对象**传给插件实例的 `config=`）；
    * 配置页每次打开都现取：`astrbot/dashboard/services/config_service.py:853-872`
      的 `get_plugin_config()` 里是 `"items": plugin_md.config.schema`（**活对象**），
      端点 `astrbot/dashboard/api/plugins.py:753` / `:1020`；
    * 前端就是把这个字符串塞进 DOM：`astrbot/dashboard/dist/assets/ProviderSelectMenu-*.js`
      的 `G.hint ? ... U(A.__template_key, ie, "hint", G.hint)`（并认 `invisible`）。
    * 而 `AstrbotConfig.save_config()` 只写 `dict(self)`（配置值），
      **schema 从不落盘**（`astrbot/core/config/astrbot_config.py:262-272 / :308 / :339`）。

    所以改内存里的 schema 既能动态、又**不碰仓库里的 `_conf_schema.json`**（用户明确
    要求不许改写那个文件）。改的是 `video.mode.hint` —— 用户原话就是"这个配置项旁边"。

    拿不到那个节点（宿主换了形状 / 手搓 schema）时**原样返回**，只把状态文本交给调用方
    去写日志：绝不为了让提示好看而抛异常。
    """
    label = ffmpeg_status_label()
    if not isinstance(schema, dict):
        return label
    hint = '%s。%s' % (
        label,
        VIDEO_MODE_HINT if ffmpeg_available() else
        (VIDEO_MODE_HINT + '当前没探测到 FFmpeg：抽帧识别会降级成一句「收到了一段视频」，'
                           '装上 FFmpeg 后重载插件即可生效。'),
    )
    try:
        node = schema['model_center']['items']['video']['items']['mode']
        if isinstance(node, dict):
            node['hint'] = hint
    except (KeyError, TypeError):  # pragma: no cover - schema 形状由仓库自己保证
        pass
    return label


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


def _element_video_source(element: Any) -> str:
    """一个适配器直给的 `<video>` 元素 → 可信坐标；认不出回空串。"""
    if not isinstance(element, dict):
        return ''
    if _text(element.get('type')).lower() not in ('video', 'video_url'):
        return ''
    attrs = element.get('attrs') if isinstance(element.get('attrs'), dict) else {}
    data = element.get('data') if isinstance(element.get('data'), dict) else {}
    for key in ('src', 'url', 'file'):
        value = _text(attrs.get(key) or data.get(key)).strip()
        if not value:
            continue
        if re.match(r'^https?://', value, re.IGNORECASE):
            return 'onebot-url:%s' % value
        local = _local_path(value)
        if local:
            return 'onebot-file:%s' % local
    return ''


def extract_session_video_sources(session: Any) -> list[str]:
    """入站视频坐标（唯一入口）。与图片来源抽取**同一条纪律**：

    * 第一遍只认**适配器直给的元素**（`session.elements`）——适配器从观测到的
      原始段写下来的，是可信坐标（`onebot-url:` / `onebot-file:`）；
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
      只能由适配器直给的元素产生）；
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

    #: 抽出来的帧文件绝对路径（按时间顺序；≤ `VIDEO_MAX_FRAMES`）。
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
    #: 本次用的临时目录（调用方读完帧字节后要删掉它，见 `collect_video_sources`）。
    workdir: str = ''

    @property
    def frame_count(self) -> int:
        return len(self.frames)


def probe_video(binary: str, target: str, timeout: float) -> tuple[float, bool, str]:
    """探时长与有没有音轨：`(时长秒, 有音轨, 错误)`。

    用 `ffmpeg -i <target>` 本身（只读文件头，退出码是 1）：**不引入 ffprobe**
    这第二个二进制——用户装了 ffmpeg 不等于装了 ffprobe（静态构建里常常只有一个）。
    时长探不到时回 `0.0`，调用方据此**不声称截断**（"不知道"不等于"很短"）。
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


def extract_video(
    target: str,
    *,
    binary: str = '',
    workdir: str = '',
    interval_seconds: int = VIDEO_FRAME_INTERVAL_SECONDS,
    max_frames: int = VIDEO_MAX_FRAMES,
    max_duration_seconds: int = VIDEO_MAX_DURATION_SECONDS,
    audio_seconds: int = VIDEO_AUDIO_CLIP_SECONDS,
    with_audio: bool = True,
    timeout: float = VIDEO_FFMPEG_TIMEOUT_SECONDS,
) -> VideoExtraction:
    """间隔抽帧 + 单独抽音轨（**同步**、阻塞；异步调用方走 `asyncio.to_thread`）。

    两条命令：

    ```
    ffmpeg -hide_banner -loglevel error -nostdin -y -i <target> -t <max_duration>
           -vf fps=1/<interval> -frames:v <max_frames> -q:v 3 <dir>/frame-%02d.jpg
    ffmpeg -hide_banner -loglevel error -nostdin -y -i <target> -t <audio_seconds>
           -vn -ac 1 -ar 16000 -f wav <dir>/audio.wav
    ```

    单条命令各自的失败**互不牵连**：抽帧失败不影响音轨，音轨没有也不影响帧
    （"无音轨 → 只走图像不失败"）。命令超时同样只影响那一条。

    `with_audio=False` 时**连音轨那条命令都不发**（调用方已经知道语音理解那条通道
    关着，抽出来也会被丢掉 —— 省一次 ffmpeg 与 60 秒音轨的字节）。
    """
    binary = binary or ffmpeg_path()
    if not binary:
        return VideoExtraction(frame_error='没检查到 FFmpeg')
    directory = workdir or tempfile.mkdtemp(prefix='hdsi-video-')
    os.makedirs(directory, exist_ok=True)
    duration, has_audio, _probe_error = probe_video(binary, target, timeout)
    truncated = bool(duration and duration > max_duration_seconds)

    frames: list[str] = []
    frame_error = ''
    pattern = os.path.join(directory, 'frame-%02d.jpg')
    try:
        code, _, stderr = _run_ffmpeg(binary, [
            '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', target, '-t', str(max_duration_seconds),
            '-vf', 'fps=1/%d' % max(1, int(interval_seconds)),
            '-frames:v', str(max(1, int(max_frames))),
            '-q:v', '3', pattern,
        ], timeout)
        if code != 0:
            frame_error = _last_error_line(stderr) or '抽帧命令退出码 %d' % code
    except subprocess.TimeoutExpired:
        frame_error = '抽帧超时（超过 %d 秒）' % timeout
    except OSError as error:
        frame_error = '抽帧失败：%s' % error
    if not frame_error:
        frames = sorted(
            os.path.join(directory, name) for name in os.listdir(directory)
            if name.startswith('frame-') and name.endswith('.jpg')
        )[:max(1, int(max_frames))]
        if not frames:
            frame_error = '抽帧没有得到任何一帧'

    audio_path = ''
    audio_error = ''
    if has_audio and with_audio:
        candidate = os.path.join(directory, 'audio.wav')
        try:
            code, _, stderr = _run_ffmpeg(binary, [
                '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-i', target, '-t', str(max(1, int(audio_seconds))),
                '-vn', '-ac', '1', '-ar', str(VIDEO_AUDIO_SAMPLE_RATE),
                '-f', 'wav', candidate,
            ], timeout)
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
        has_audio=has_audio, workdir=directory,
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
) -> str:
    """正文里那句视频事实（含**可数线索**：时长 / 抽了几帧 / 音频取了几秒 / 有没有截断）。

    措辞与 `describe_user_event` 给图片 / 语音写的那几句同一条尺子：只报形式与边界，
    不报画面（"没看到就别编"）。`degrade_reason` 非空时那句降级说明顶在最前面——
    视频没有进入任何模型时，必须让模型知道"这一段你确实没看见"。
    """
    parts: list[str] = []
    if degrade_reason:
        parts.append(degrade_reason)
    if duration_seconds:
        parts.append('约 %s' % _seconds_label(duration_seconds))
    if truncated:
        parts.append('超过 %d 秒上限，只看了前 %d 秒' % (
            VIDEO_MAX_DURATION_SECONDS, VIDEO_MAX_DURATION_SECONDS,
        ))
    if frame_count:
        parts.append('已按每 %d 秒 1 帧抽了 %d 帧画面' % (VIDEO_FRAME_INTERVAL_SECONDS, frame_count))
    elif frame_error and not degrade_reason:
        # `degrade_reason` 已经说过抽帧失败时不再重复（同一件事说两遍是噪音）。
        parts.append('抽帧失败')
    if has_audio:
        parts.append('已单独抽出前 %d 秒音轨' % VIDEO_AUDIO_CLIP_SECONDS)
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


#: 降级原因 → 一条**可行动**的 warn（`service.note_access_skip` 的 message）。
#: 只放"能力 / 配置"层面的原因：单条视频的偶然失败（比如这段没有音轨）不该刷 warn。
_ACTIONABLE_REASONS: dict[str, str] = {
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
}


def degradation_message(reason: str) -> str:
    """降级原因 → 可行动的 warn 文案（没有对应文案时回空串 = 不刷 warn）。

    先精确匹配，再做一次包含匹配：外挂识别失败时原因里会再挂上底层报错
    （`指名的…（Connection error）`），那一条也该拿到同一句可行动的提示。
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
      （`load_native_images`，与直发图片共用那一个 3 张预算）；
    * `audio_sources`：抽出来的音轨，作为**现有语音理解通道**的来源
      （`load_native_audio` 认的 `data:audio/wav;base64,…`）；
    * `note`：进当前事件的正文事实（空串 = 一个字都不加）；
    * `workdir`：临时目录，**调用方读完帧字节后**必须删（`cleanup()`）。
    """

    mode: str = VIDEO_DEFAULT_MODE
    image_sources: list[str] = field(default_factory=list)
    audio_sources: list[str] = field(default_factory=list)
    note: str = ''
    reason: str = ''
    workdir: str = ''

    def cleanup(self) -> None:
        """删掉本次的临时目录（幂等；失败不抛）。"""
        directory = self.workdir
        self.workdir = ''
        if not directory:
            return
        try:
            shutil.rmtree(directory, ignore_errors=True)
        except Exception:  # noqa: BLE001 - 临时文件删不掉不该影响回合
            pass


def _wav_data_uri(path: str, max_bytes: float) -> str:
    """音轨文件 → `data:audio/wav;base64,…`（超预算回空串）。"""
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
    return 'data:audio/wav;base64,%s' % base64.b64encode(data).decode('ascii')


def _local_size_bytes(target: str) -> int:
    try:
        return os.path.getsize(target)
    except OSError:
        return -1


def _warn(service: Any, story: Any, reason: str) -> None:
    """按会话节流地打一条降级 warn（`note_access_skip`：同一原因 10 分钟一条）。"""
    message = degradation_message(reason)
    if not message:
        return
    note = getattr(service, 'note_access_skip', None)
    story_id = _text(_pick(story, 'id')) or '-'
    if callable(note):
        try:
            note('video|%s|%s' % (story_id, reason), VIDEO_WARN_INTERVAL_MS, message)
            return
        except Exception:  # noqa: BLE001 - 报告失败不该带崩回合
            pass
    # 兜底：宿主 / 替身没提供节流口时**照样要说话** —— 只是少了节流，
    # 绝不能因为"节流口不在"就把能力缺失吞成静默（坑 25）。
    report = getattr(service, 'report_standalone', None)
    if not callable(report):
        return
    try:
        report('warn', message)
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
        if not capped:
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
    service: Any, story: Any, session: Any,
) -> VideoMedia:
    """抽帧识别（主路）：拿到帧与音轨的**来源坐标**，交给现有那两条通道去取。

    返回的 `image_sources` / `audio_sources` 由调用方并进它本来就有的
    `image_sources` / `audio_sources` —— 帧走 `load_native_images`、音轨走
    `load_native_audio`，**一条判据、一套实现**，这里不另造通道。

    `enabled=False` 时**一个 ffmpeg 都不调、一个模型都不调**，直接回空。
    """
    result = VideoMedia(mode=VIDEO_DEFAULT_MODE)
    config = video_config(service)
    result.mode = config['mode']
    if not config['enabled']:
        return result
    sources = extract_session_video_sources(session)
    if not sources:
        return result

    if config['mode'] == 'native':
        result.reason = NATIVE_UNSUPPORTED_REASON
        result.note = video_fact_note(degrade_reason=result.reason)
        _warn(service, story, result.reason)
        return result

    kind, target = video_input_target(sources[0])
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
    try:
        extraction = await asyncio.to_thread(extract_video, target, with_audio=audio_enabled)
    except Exception as error:  # noqa: BLE001 - 抽帧出任何岔子都不许带崩回合
        extraction = VideoExtraction(frame_error='抽帧异常：%s' % error)
    finally:
        _release_slot()

    result.workdir = extraction.workdir
    result.image_sources = ['onebot-file:%s' % path for path in extraction.frames]
    audio_budget = _audio_budget_bytes(service)
    notes = {'audio_over_budget': False}
    if extraction.audio_path and audio_enabled:
        uri = _wav_data_uri(extraction.audio_path, audio_budget)
        if uri:
            result.audio_sources = [uri]
        else:
            # 音轨与语音走**同一条**通道，就得服从那条通道自己的预算：超了会被
            # `fetch_native_audio` 丢掉（那就成了"静默没声音"），所以这里提前判、
            # 并且**明说**（`VIDEO_AUDIO_OVER_BUDGET_REASON` 那条 warn 是可行动的）。
            notes['audio_over_budget'] = True
            result.reason = VIDEO_AUDIO_OVER_BUDGET_REASON
    if result.image_sources or result.audio_sources:
        if extraction.frame_error and not result.image_sources and not result.reason:
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
    )
    _warn(service, story, result.reason)
    return result


def _audio_channel_enabled(service: Any) -> bool:
    """`model.audio.enabled`（语音理解总开关）：与 `load_native_audio` 读的是**同一个键**。

    关着时那条通道本来就会丢下音轨，所以抽帧识别**连抽都不抽**（省一次 ffmpeg 与
    60 秒音轨的字节）；正文里也就不会声称"抽出了音轨"。
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
