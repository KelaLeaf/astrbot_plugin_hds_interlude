# -*- coding: utf-8 -*-
"""视频理解（v1.9.0）的用例：三模式、降级、预算上限、通道接线与**反向变异**。

覆盖用户点名的八件事：

1. 三模式各自的行为；
2. `enabled=false` → **零 ffmpeg 调用、零模型调用**；
3. ffmpeg 缺失 → 降级 + **可行动**的 warn；
4. 帧数 / 间隔上限真的生效（超长视频被截断且**留可数线索**）；
5. 音轨缺失 → 只走图像、不失败；
6. `external` 指名找不到 Provider → **明确失败不回落**；
7. 抽帧出来的帧确实进了**现有图像理解通道**、音轨确实进了**现有语音理解通道**
   （两条通道都是 `ServiceChunk3` 的真实实现，断言的是"同一份来源表"）；
8. 变异：去掉上限 → 红；ffmpeg 缺失时假装成功 → 红；把帧绕过既有图像通道自造一套 → 红。

独立运行（仓库根目录；发布仓布局去掉 `plugin.` 前缀）：
    python3 -m unittest plugin.tests.test_video_understanding -v
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest import mock

from plugin.core import video_understanding as video
from plugin.core.service.base import ServiceChunk0
from plugin.core.service.chunk3 import ServiceChunk3
from plugin.core.service.session import SessionView
from plugin.core.service.transport import NullTransport

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(PLUGIN_ROOT)
SCHEMA_PATH = os.path.join(PLUGIN_ROOT, "_conf_schema.json")
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

#: 一段"长视频"的时长行（10 分钟，超过 180 秒上限）。
LONG_DURATION = '  Duration: 00:10:00.00, start: 0.000000, bitrate: 1200 kb/s'
SHORT_DURATION = '  Duration: 00:00:12.00, start: 0.000000, bitrate: 1200 kb/s'
_AUDIO_STREAM = '  Stream #0:1(und): Audio: aac (LC), 44100 Hz, stereo, fltp, 128 kb/s'
_VIDEO_STREAM = '  Stream #0:0: Video: h264 (High), yuv420p, 640x360, 25 fps'


def _png_bytes() -> bytes:
    """1x1 透明 PNG（不依赖 Pillow）。"""
    return base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=='
    )


def _wav_bytes(payload: bytes = b'\x00\x00' * 64) -> bytes:
    """一个**能被 `_wav_data_uri` 接受**的最小 wav（> 44 字节，RIFF/WAVE 魔数）。"""
    header = b'RIFF' + (36 + len(payload)).to_bytes(4, 'little') + b'WAVEfmt ' + (16).to_bytes(4, 'little')
    header += (1).to_bytes(2, 'little') + (1).to_bytes(2, 'little') + (16000).to_bytes(4, 'little')
    header += (32000).to_bytes(4, 'little') + (2).to_bytes(2, 'little') + (16).to_bytes(2, 'little')
    header += b'data' + len(payload).to_bytes(4, 'little')
    return header + payload


class _Ctx:
    """`InterludeContext` 的最小替身：`ServiceChunk0.__init__` 会用到这三个钩子。

    不提供它们的话，`ServiceChunk0` 的后台调度会在测试输出里刷一屏 `AttributeError`
    回溯（用例照样过，但把"看起来像挂了"的回溯留给下一个人才是真坑）。
    """

    base_dir = os.path.join(tempfile.gettempdir(), 'hdsi-video-test')

    def __init__(self) -> None:
        self.timers: list[tuple[Any, float, bool]] = []

    def set_timeout(self, callback: Any, delay_ms: float) -> Any:
        self.timers.append((callback, delay_ms, False))
        return lambda: None

    def set_interval(self, callback: Any, delay_ms: float) -> Any:
        self.timers.append((callback, delay_ms, True))
        return lambda: None

    def ensure_http(self) -> Any:
        return None


class FakeFfmpeg:
    """`_run_ffmpeg` 的替身：按命令形状模拟 ffmpeg，并记下每一条 argv。

    刻意**不**假装成功：只有真的被调到时才写文件；探测、抽帧、抽音轨三条命令分别
    可单独设成失败 / 超时，好让"抽帧失败但音轨还在"这类组合能被真实地演出来。
    """

    def __init__(
        self,
        *,
        duration: str = SHORT_DURATION,
        has_audio: bool = True,
        frames_written: int = 3,
        fail_frames: bool = False,
        fail_audio: bool = False,
        timeout_frames: bool = False,
        frames_before_timeout: int = 0,
        timeout_audio: bool = False,
        audio_bytes: int = 0,
    ) -> None:
        self.duration = duration
        self.has_audio = has_audio
        self.frames_written = frames_written
        self.fail_frames = fail_frames
        self.fail_audio = fail_audio
        self.timeout_frames = timeout_frames
        #: 抽帧超时**之前**先写出几帧（真机上 ffmpeg 被杀时磁盘上就是这样的半成品）。
        self.frames_before_timeout = frames_before_timeout
        self.timeout_audio = timeout_audio
        self.audio_bytes = audio_bytes
        self.calls: list[list[str]] = []

    # ---- 命令分类 ----
    def kind(self, args: list[str]) -> str:
        if '-vf' in args:
            return 'frames'
        if '-vn' in args:
            return 'audio'
        return 'probe'

    def __call__(self, binary: str, args: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append(list(args))
        kind = self.kind(args)
        if kind == 'probe':
            streams = [_VIDEO_STREAM] + ([_AUDIO_STREAM] if self.has_audio else [])
            return 1, '', self.duration + '\n' + '\n'.join(streams) + '\n'
        if kind == 'frames':
            if self.timeout_frames:
                import subprocess  # noqa: PLC0415 - 与真实实现同一类异常
                pattern = args[-1]
                for index in range(1, self.frames_before_timeout + 1):
                    with open(pattern % index, 'wb') as handle:
                        handle.write(_png_bytes())
                raise subprocess.TimeoutExpired(binary, timeout)
            if self.fail_frames:
                return 1, '', 'Invalid data found when processing input\n'
            pattern = args[-1]
            for index in range(1, self.frames_written + 1):
                with open(pattern % index, 'wb') as handle:
                    handle.write(_png_bytes())
            return 0, '', ''
        if self.timeout_audio:
            import subprocess  # noqa: PLC0415
            raise subprocess.TimeoutExpired(binary, timeout)
        if self.fail_audio:
            return 1, '', 'Output file does not contain any stream\n'
        with open(args[-1], 'wb') as handle:
            handle.write(_wav_bytes(b'\x00' * self.audio_bytes) if self.audio_bytes else _wav_bytes())
        return 0, '', ''

    # ---- 断言辅助 ----
    def argvs(self, kind: str) -> list[list[str]]:
        return [args for args in self.calls if self.kind(args) == kind]


class Host(ServiceChunk0, ServiceChunk3):
    """`InterludeService` 生产 MRO 的忠实缩小版（`ServiceChunk0` + `ServiceChunk3`）。

    `ServiceChunk0` 必须带上：`note_access_skip`（降级 warn 的节流口）在它那里，
    只有 `ServiceChunk3` 的话那些 warn 会退回无节流的兜底口——用例就测不到节流。

    刻意继承真实的 chunk3：`load_native_images` / `load_native_audio` /
    `fetch_native_*` 都是**真货**，本文件断言的"帧进了图像通道、音轨进了语音通道"
    才有意义。
    """

    def __init__(self, config: Any = None, **fields: Any) -> None:
        # 日志容器必须在 `super().__init__` **之前**就位：`ServiceChunk0.__init__`
        # 末尾自己会走一次 `report_standalone_operation`。
        self.logs = []
        self.notes = []
        self.clock = 1_000_000
        super().__init__(_Ctx(), config or {}, None, NullTransport())
        self.__dict__.update(fields)

    def now(self) -> datetime:
        return NOW

    def now_ms(self) -> int:
        return self.clock

    async def serial(self, key: str, task: Any) -> Any:
        return await task()

    def report(self, *args: Any, **kwargs: Any) -> None:
        self.logs.append(('report',) + args)

    def report_operation(self, *args: Any, **kwargs: Any) -> None:
        self.logs.append(('operation',) + args)

    def report_standalone(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        self.logs.append(('standalone', level, message % args if args else message))

    def report_standalone_operation(self, *args: Any, **kwargs: Any) -> None:
        self.logs.append(('standalone-operation',) + args)

    # ---- 断言辅助 ----
    def warns(self) -> list[str]:
        return [entry[2] for entry in self.logs if entry[0] == 'standalone' and entry[1] == 'warn']


def video_config(enabled: bool = True, mode: str = 'frames', model_id: str = '') -> dict[str, Any]:
    """一份"模型中心"配置：视频那一组 + 两条既有通道各自的开关。

    视频那组按 **schema 的默认值** 起手（`VIDEO_CONFIG_DEFAULTS`），要改哪一项就在
    用例里直接改——这样"新增配置键忘了进夹具"不会变成假绿。
    """
    section = dict(video.VIDEO_CONFIG_DEFAULTS)
    section.update({'enabled': enabled, 'mode': mode, 'model_id': model_id})
    return {
        'model': {
            'vision': {'enabled': True, 'mode': 'native', 'detail': 'auto', 'max_image_dimension': 0},
            'audio': {
                'enabled': True, 'out_format': 'mp3', 'max_file_size_mb': 10, 'max_per_message': 1,
            },
            'video': section,
        },
    }


def video_session(target: str = 'https://cdn.example.com/v.mp4', *, is_direct: bool = True) -> SessionView:
    """一条带视频的**私聊**入站事件（适配器直给元素 = 可信坐标）。

    `is_direct=True` 是生产值：适配层 `session_view()` 一定会按事件填它
    （`is_direct=not resolved.is_group`）。群聊用例显式传 `False`。
    """
    return SessionView(
        platform='onebot', self_id='1', user_id='2', is_direct=is_direct,
        content='看这个<video src="%s"/>' % target,
        elements=[{'type': 'video', 'attrs': {'src': target}, 'children': []}],
        media=[],
    )


class FakeNarrator:
    """`narrator._side_task_json` 的替身：记下每次调用，可设成失败。

    两条 `build_body` 都探一遍（`capped=True` 首轮 / `capped=False` 降级重试）：
    "首轮带 cap、重试放开"是旁路任务家族的共同形状，只探一条就看不见反过来的实现
    （v1.9.7 现场：外挂视频那支写成 `if not capped`，探针只调 `build_body(True)`
    时它看起来完全正常）。替身比生产更严，不是更宽。
    """

    def __init__(self, answer: str = '画面里是一只猫', error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def _side_task_json(
        self, provider: Any, model: str, task: str, timeout: Any,
        build_body: Any, parse: Any, *rest: Any, **kwargs: Any,
    ) -> Any:
        body = build_body(True)
        self.calls.append({
            'task': task, 'model': model, 'provider': provider, 'body': body,
            'body_without_cap': build_body(False),
        })
        if self.error is not None:
            raise self.error
        return parse(self.answer)


#: 一段本地视频文件的内容（字节数只用于体积预算）。
_FAKE_VIDEO_BYTES = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 4096


class VideoTestCase(unittest.IsolatedAsyncioTestCase):
    """视频用例的公共底座：模块状态归零 + **临时目录清扫**。

    抽帧会在 `tempfile.gettempdir()` 下建 `hdsi-video-*`；生产里由调用方在读完帧字节后
    删掉（`VideoMedia.cleanup()`）。用例里**只认本进程自己建的那几个**（记录式），
    不去"扫整个 `/tmp` 再取差集"——`/tmp` 是共享的，别的测试进程 / 上一次崩掉的运行
    留下的 `hdsi-video-case-*` 会被算到这一条用例头上，于是"清理干净"随环境偶发变红
    （见 `docs/PORTING_NOTES.md` §84；根因取证的用例是 `TempDirIsolationTests`）。
    """

    def setUp(self) -> None:
        video.reset_ffmpeg_probe()
        video.reset_video_runtime_state()
        self._created_temp_dirs: list[str] = []
        self._mkdtemp = tempfile.mkdtemp
        self._mkdtemp_patch = mock.patch.object(tempfile, 'mkdtemp', self._recording_mkdtemp)
        self._mkdtemp_patch.start()

    def _recording_mkdtemp(self, *args: Any, **kwargs: Any) -> str:
        path = self._mkdtemp(*args, **kwargs)
        self._created_temp_dirs.append(path)
        return path

    def tearDown(self) -> None:
        self._mkdtemp_patch.stop()
        video.reset_ffmpeg_probe()
        video.reset_video_runtime_state()
        for path in self._created_temp_dirs:
            shutil.rmtree(path, ignore_errors=True)

    def new_temp_dirs(self) -> list[str]:
        """本用例期间真的建过、**现在还留在盘上**的临时目录（= 没被清理的那些）。"""
        return sorted(path for path in self._created_temp_dirs if os.path.exists(path))

    @contextlib.contextmanager
    def video_slot_guard(self) -> Any:
        """本回合的**每一次** `_acquire_slot()` 都必须成功。

        视频并发上限（`VIDEO_MAX_CONCURRENCY=1`）是**进程级**共享状态：同一个事件循环里
        只要还有另一条视频收集在跑，本回合就会静默少读几段——那正是"配了 3 段只读 2 段"
        唯一的来源。顺序 await 的单回合绝不可能自己撞自己，所以这条不变量必须成立；
        它一旦破了，要在**这里**以清楚的文案红，而不是让下面的条数断言漂移。
        """
        refused: list[int] = []
        original = video._acquire_slot

        def tracked() -> bool:
            granted = original()
            if not granted:
                refused.append(len(refused) + 1)
            return granted

        with mock.patch.object(video, '_acquire_slot', tracked):
            yield refused
        self.assertEqual(
            refused, [],
            '本回合有视频被并发闸拒了 %d 次：说明同一个事件循环里另有一条视频收集在跑'
            '（进程级共享状态），条数断言会因此漂移' % len(refused),
        )


class _TempVideo:
    """临时本地视频文件（用例结束后删）。"""

    def __init__(self, payload: bytes = _FAKE_VIDEO_BYTES) -> None:
        self.directory = tempfile.mkdtemp(prefix='hdsi-video-case-')
        self.path = os.path.join(self.directory, 'clip.mp4')
        with open(self.path, 'wb') as handle:
            handle.write(payload)

    def session(self) -> SessionView:
        return SessionView(
            platform='onebot', self_id='1', user_id='2', is_direct=True,
            content='<video src="file://%s"/>' % self.path,
            elements=[{'type': 'video', 'attrs': {'src': 'file://%s' % self.path}, 'children': []}],
            media=[],
        )

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


# =========================================================================== #
# 1. 配置：三处同改 + 默认值取省成本那侧
# =========================================================================== #

class VideoConfigTests(unittest.TestCase):

    def setUp(self) -> None:
        with open(SCHEMA_PATH, encoding='utf-8-sig') as handle:
            self.schema = json.load(handle)
        self.video = self.schema['model_center']['items']['video']

    def test_the_group_lives_in_the_model_center_next_to_vision_and_audio(self) -> None:
        keys = list(self.schema['model_center']['items'])
        self.assertIn('video', keys)
        # 位置（v1.9.1 新契约，用户口径）：感知类设置排在**连接池前面** ——
        # 视频理解在语音 / 音频理解下面、模型连接上面。
        self.assertEqual(keys[:4], ['vision', 'audio', 'video', 'providers'])
        self.assertEqual(self.video['description'], '视频理解设置')

    def test_defaults_are_the_cost_saving_side(self) -> None:
        items = self.video['items']
        self.assertIs(items['enabled']['default'], False)
        self.assertEqual(items['mode']['default'], 'frames')
        self.assertEqual(items['model_id']['default'], '')
        self.assertEqual(items['mode']['options'], ['frames', 'native', 'external'])
        self.assertEqual(items['mode']['default'], video.VIDEO_DEFAULT_MODE)
        # v1.9.1：预算全部可配，默认值一律取省成本那侧。
        self.assertEqual(items['frame_mode']['options'], ['sequence', 'average'])
        self.assertEqual(items['frame_mode']['default'], 'sequence')
        self.assertEqual(items['frame_interval_seconds']['default'],
                         video.VIDEO_FRAME_INTERVAL_SECONDS)
        self.assertEqual(items['frame_average_count']['default'], video.VIDEO_AVERAGE_FRAMES)
        self.assertEqual(items['out_format']['default'], video.VIDEO_DEFAULT_AUDIO_FORMAT)
        # 音轨格式与「语音 / 音频理解」那组**同名同候选**（别另造一套语义）。
        self.assertEqual(items['out_format']['options'],
                         self.schema['model_center']['items']['audio']['items']['out_format']['options'])
        self.assertEqual(items['out_format']['default'],
                         self.schema['model_center']['items']['audio']['items']['out_format']['default'])
        self.assertEqual(items['audio_duration']['options'], ['custom', 'unlimited'])
        self.assertEqual(items['audio_duration']['default'], 'custom')
        self.assertEqual(items['audio_duration_seconds']['default'],
                         video.VIDEO_AUDIO_CLIP_SECONDS)
        self.assertEqual(items['timeout_seconds']['default'],
                         video.VIDEO_FFMPEG_TIMEOUT_SECONDS)
        self.assertIs(items['group_enabled']['default'], False)
        # 群聊默认关 = 省成本那侧（私聊不受它管，见 `GroupChatSwitchTests`）。
        self.assertIs(items['group_enabled']['default'], video.VIDEO_DEFAULT_GROUP_ENABLED)

    def test_core_defaults_match_the_schema(self) -> None:
        from plugin.core.service.config import CONFIG_DEFAULTS  # noqa: PLC0415

        # 三方逐字一致：schema 的 `default`、core 的 `CONFIG_DEFAULTS`、
        # `video_understanding.VIDEO_CONFIG_DEFAULTS`（默认值只有一处真相）。
        self.assertEqual(
            CONFIG_DEFAULTS['model']['video'],
            {key: value['default'] for key, value in self.video['items'].items()},
        )
        self.assertEqual(CONFIG_DEFAULTS['model']['video'], video.VIDEO_CONFIG_DEFAULTS)
        self.assertEqual(
            video.VIDEO_CONFIG_DEFAULTS,
            {key: value['default'] for key, value in self.video['items'].items()},
        )

    def test_the_master_switch_hint_is_verbatim(self) -> None:
        """用户逐字口径：总开关那句只留两截，多一个字都不加。"""
        self.assertEqual(
            self.video['items']['enabled']['hint'],
            '视频理解的总开关（默认关）。关闭时视频只留一条「收到了一段视频」的事实。',
        )

    def test_the_model_key_is_a_named_provider_picker(self) -> None:
        node = self.video['items']['model_id']
        self.assertEqual(node['_special'], 'select_provider')
        self.assertIn('外挂', node['description'])
        # 用户点名删掉了解释句（v1.9.2）：这一项只留标题，行为不变（找不到不回落仍在代码里）。
        self.assertNotIn('hint', node)

    def test_the_mode_hint_is_the_users_verbatim_sentence(self) -> None:
        """识别模式的描述：状态提示顶在最前面，后面接用户逐字那一句（v1.9.1）。

        静态 hint 里**不许**再写死任何数字：抽帧间隔 / 帧数 / 音轨秒数现在都是配置项，
        写进文案就是第二个真相（过时即误导）。
        """
        hint = self.video['items']['mode']['hint']
        self.assertEqual(hint, '需要启用语音原生理解与启用图片理解后抽帧模式才会生效。')
        self.assertNotIn(str(video.VIDEO_MAX_FRAMES), hint)
        self.assertNotIn(str(video.VIDEO_AUDIO_CLIP_SECONDS), hint)
        # description 不放 URL、不写长句（宿主配置页标题的规则，与其它组同一条尺子）。
        for key, node in self.video['items'].items():
            with self.subTest(key=key):
                self.assertTrue(node['description'])
                self.assertLessEqual(len(node['description']), 60)
                self.assertNotIn('http', node['description'].lower())

    def test_the_three_keys_are_in_the_reconciliation_tables(self) -> None:
        from plugin.tests import test_configuration as conf  # noqa: PLC0415

        self.assertIn('video', conf.ConfigurationSchemaTest.LOCAL_ONLY_FIELDS['model_center'])
        paths = {(path, key) for path, key, _value in conf.DEEP_DEFAULTS}
        for key in self.video['items']:
            self.assertIn((('model_center', 'video'), key), paths)
        # 三处同改的第三处是文档（`test_config_map_documents_the_group_and_every_key`）。
        self.assertTrue(hasattr(conf, 'DEEP_DEFAULTS'))

    def test_config_map_documents_the_group_and_every_key(self) -> None:
        # 发布仓布局（仓库根＝插件根）里没有 `docs/`：工作区一致性检查在那里**整体跳过**，
        # 与 `CommandTableTests` / `ReleaseConsistencyTest` 同一口径；缺文件时报错会变成假红。
        path = os.path.join(REPO_ROOT, 'docs', 'CONFIG_MAP.md')
        if not os.path.exists(path):
            self.skipTest('发布仓布局没有 docs/CONFIG_MAP.md')
        with open(path, encoding='utf-8') as handle:
            config_map = handle.read()
        documented = ['video'] + ['video.%s' % key for key in self.video['items']]
        for key in documented:
            self.assertIn('`model_center.%s`' % key, config_map)


class VideoConfigIoTests(unittest.TestCase):
    """导出 / 导入是**跨版本用户资产**：新键必须能原样带走、缺键补默认、双拼写都认。"""

    def test_export_import_carries_the_video_group(self) -> None:
        """v1.9.1：**每一个**视频配置键都要能原样带走（含新增的七项）。

        导出文件是跨版本用户资产：新键漏出往返，用户的预算设置就会在导入后回到默认。
        """
        from plugin.core.service.config import normalize_config, to_schema_shape  # noqa: PLC0415

        section = {
            'enabled': True, 'mode': 'external', 'model_id': 'vlm',
            'frame_mode': 'average', 'frame_interval_seconds': 6, 'frame_average_count': 2,
            'out_format': 'flac', 'audio_duration': 'unlimited', 'audio_duration_seconds': 90,
            'timeout_seconds': 45, 'group_enabled': True,
        }
        config = normalize_config({'model': {'video': dict(section)}})
        self.assertEqual(config['model']['video'], section)
        # 落盘要转 **schema 分组名**（`model_center`），否则宿主配置页显示"全是默认值"。
        self.assertEqual(to_schema_shape(config)['model_center']['video'], section)
        self.assertEqual(set(section), set(video.VIDEO_CONFIG_DEFAULTS))

    def test_missing_keys_are_filled_with_the_cost_saving_defaults(self) -> None:
        from plugin.core.service.config import normalize_config  # noqa: PLC0415

        self.assertEqual(normalize_config({})['model']['video'], video.VIDEO_CONFIG_DEFAULTS)

    def test_both_spellings_are_read(self) -> None:
        from plugin.core.service.config import normalize_config  # noqa: PLC0415

        video = normalize_config({'model': {'video': {'modelId': 'abc'}}})['model']['video']
        self.assertEqual(video['model_id'], 'abc')

    def test_an_unknown_mode_falls_back_to_frames_not_native(self) -> None:
        # 用户手改坏了配置也不该悄悄变成"原生识别"（那条在宿主上走不通）。
        for raw in ('weird', '', None, 7, 'NATIVE '):
            with self.subTest(raw=raw):
                resolved = video.resolve_video_config({'mode': raw})
                self.assertIn(resolved['mode'], ('frames', 'native'))
        self.assertEqual(video.resolve_video_config({'mode': 'weird'})['mode'], 'frames')
        self.assertEqual(video.resolve_video_config({'mode': 'NATIVE '})['mode'], 'native')


# =========================================================================== #
# 2. FFmpeg 状态提示（宿主源码依据：写内存 schema，不动仓库文件）
# =========================================================================== #

class FfmpegStatusTests(unittest.TestCase):

    def test_the_label_has_the_two_values_the_user_asked_for(self) -> None:
        """两个取值 + **文本标记**：宿主配置页不支持着色（依据见模块注释），
        所以用 ✅ / ⚠️ 代替绿 / 黄。"""
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'):
            self.assertEqual(video.ffmpeg_status_label(), '✅ FFmpeg 已识别')
            self.assertEqual(video.ffmpeg_status_label(), video.FFMPEG_FOUND_LABEL)
            self.assertTrue(video.ffmpeg_available())
        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            self.assertEqual(video.ffmpeg_status_label(), '⚠️ 未发现 FFmpeg')
            self.assertEqual(video.ffmpeg_status_label(), video.FFMPEG_MISSING_LABEL)
            self.assertFalse(video.ffmpeg_available())

    def test_the_probe_reflects_which_on_a_fresh_process(self) -> None:
        video.reset_ffmpeg_probe()
        calls: list[str] = []

        def which(name: str) -> str:
            calls.append(name)
            return '/opt/ffmpeg'

        with mock.patch.object(video.shutil, 'which', which):
            self.assertEqual(video.ffmpeg_path(), '/opt/ffmpeg')
            self.assertEqual(video.ffmpeg_path(), '/opt/ffmpeg')
        # 只探一次：这条判据每条视频消息都会读。
        self.assertEqual(calls, ['ffmpeg'])
        video.reset_ffmpeg_probe()

    def test_the_hint_patch_writes_the_runtime_state_into_the_in_memory_schema(self) -> None:
        with open(SCHEMA_PATH, encoding='utf-8-sig') as handle:
            schema = json.load(handle)
        static_hint = schema['model_center']['items']['video']['items']['mode']['hint']
        self.assertNotIn('已识别', static_hint, '静态 hint 里不许写死状态')
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'):
            label = video.apply_ffmpeg_status_hint(schema)
        self.assertEqual(label, '✅ FFmpeg 已识别')
        dynamic = schema['model_center']['items']['video']['items']['mode']['hint']
        self.assertEqual(dynamic, '✅ FFmpeg 已识别。' + video.VIDEO_MODE_HINT)
        self.assertNotIn('<span', dynamic, '宿主把 hint 当纯文本，HTML 只会显示成源码')
        # 只改了**内存里的**这一份；仓库里的文件一字未动（用户明确要求）。
        with open(SCHEMA_PATH, encoding='utf-8-sig') as handle:
            self.assertEqual(json.load(handle)['model_center']['items']['video']['items']['mode']['hint'],
                             static_hint)

    def test_the_hint_patch_is_honest_when_ffmpeg_is_missing(self) -> None:
        schema = json.loads(json.dumps(_schema_video()))
        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            video.apply_ffmpeg_status_hint(schema)
        hint = schema['model_center']['items']['video']['items']['mode']['hint']
        self.assertEqual(hint, '⚠️ 未发现 FFmpeg。' + video.VIDEO_MODE_HINT)

    def test_a_missing_schema_node_never_raises(self) -> None:
        for schema in (None, {}, {'model_center': {}}, {'model_center': {'items': {'video': None}}}):
            with self.subTest(schema=schema):
                with mock.patch.object(video, '_FFMPEG_PATH', ''):
                    self.assertEqual(video.apply_ffmpeg_status_hint(schema), '⚠️ 未发现 FFmpeg')

    # ------------------------------------------------------------------ #
    # v1.9.9：状态词**只许出现在「识别模式」一处**（用户真机验收原话：
    # "怎么到处都是 `✅ FFmpeg 已识别` 的文字，只需要「视频识别模式」那里显示就可以了"）
    # ------------------------------------------------------------------ #

    def test_the_status_lands_on_the_mode_field_and_nowhere_else(self) -> None:
        """**同一份状态文本、同一处判据**，但只贴一个节点：识别模式。

        反向：把 `video.enabled` 加回 `FFMPEG_HINT_TARGETS`（或给它的 hint 顶上状态词）
        → 本条的 `enabled` 相等断言红，`FfmpegHintUniquenessTests` 也红。
        """
        with open(SCHEMA_PATH, encoding='utf-8-sig') as handle:
            schema = json.load(handle)
        enabled_static = schema['model_center']['items']['video']['items']['enabled']['hint']
        self.assertNotIn('FFmpeg', enabled_static, '总开关的静态文案本来就没有状态词')
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'):
            self.assertEqual(video.apply_ffmpeg_status_hint(schema), '✅ FFmpeg 已识别')
        items = schema['model_center']['items']['video']['items']
        self.assertEqual(items['mode']['hint'], '✅ FFmpeg 已识别。' + video.VIDEO_MODE_HINT)
        self.assertEqual(items['enabled']['hint'], enabled_static, '总开关一字不动')

    def test_the_write_target_table_has_exactly_one_entry(self) -> None:
        """写入表只有一项 —— 放宽它就是"到处都有状态词"（用户真机验收的原话）。"""
        self.assertEqual(video.FFMPEG_HINT_TARGETS, (
            (('model_center', 'items', 'video', 'items', 'mode'), video.VIDEO_MODE_HINT),
        ))
        self.assertEqual(video.ffmpeg_status_hint_targets(), ('model_center.video.mode',))

    def test_the_two_labels_are_the_only_text_that_follows_the_state(self) -> None:
        """有 / 无 ffmpeg 两种状态各自渲染出的**字面量**（只落在识别模式那一项上）。"""
        with open(SCHEMA_PATH, encoding='utf-8-sig') as handle:
            schema = json.load(handle)
        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            video.apply_ffmpeg_status_hint(schema)
        items = schema['model_center']['items']['video']['items']
        self.assertTrue(items['mode']['hint'].startswith('⚠️ 未发现 FFmpeg。'))
        # 判据一处：这句的前缀就是 `ffmpeg_status_label()` 当时返回的那个串。
        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            self.assertEqual(video.ffmpeg_status_label(), '⚠️ 未发现 FFmpeg')
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'):
            video.apply_ffmpeg_status_hint(schema)
            self.assertEqual(video.ffmpeg_status_label(), '✅ FFmpeg 已识别')
        self.assertTrue(items['mode']['hint'].startswith('✅ FFmpeg 已识别。'))
        self.assertNotIn('⚠️', items['mode']['hint'], '旧状态必须被剥掉，不许两句并存')
        self.assertNotIn('FFmpeg', items['enabled']['hint'], '总开关那处不许跟着翻状态')

    def test_writing_twice_does_not_stack_the_status(self) -> None:
        """每个刷新点都会重写；重写必须幂等（否则配置页会「⚠️。⚠️。⚠️。」）。"""
        with open(SCHEMA_PATH, encoding='utf-8-sig') as handle:
            schema = json.load(handle)
        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            for _ in range(3):
                video.apply_ffmpeg_status_hint(schema)
        items = schema['model_center']['items']['video']['items']
        self.assertEqual(items['mode']['hint'], '⚠️ 未发现 FFmpeg。' + video.VIDEO_MODE_HINT)
        self.assertEqual(
            items['mode']['hint'].count('⚠️ 未发现 FFmpeg'), 1,
            '状态词只许出现一次',
        )

    def test_a_broken_schema_returns_the_label_plus_a_visible_reason(self) -> None:
        """缺节点不许再**静默**（v1.9.0 那版是 `except (KeyError, TypeError): pass`）。

        反向：把原因吞掉（回空串）→ 本条红。
        """
        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            label, problem = video.apply_ffmpeg_status_hint_or_problem(None)
        self.assertEqual(label, '⚠️ 未发现 FFmpeg', '写不进去也要把状态串交出去')
        self.assertTrue(problem, '写不进去必须给出一句可读的原因（不许静默）')
        self.assertIn('schema', problem)

        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            label, problem = video.apply_ffmpeg_status_hint_or_problem({'model_center': {}})
        self.assertEqual(label, '⚠️ 未发现 FFmpeg')
        # 原因要点名**缺了哪个节点**（用户/维护者据此知道是宿主形状变了）。
        self.assertIn('model_center.video.mode', problem)
        self.assertNotIn('model_center.video.enabled', problem, '总开关不再是写入目标')

        with open(SCHEMA_PATH, encoding='utf-8-sig') as handle:
            ok_schema = json.load(handle)
        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            label, problem = video.apply_ffmpeg_status_hint_or_problem(ok_schema)
        self.assertEqual(problem, '', '正常 schema 上不该报问题')
        self.assertEqual(label, '⚠️ 未发现 FFmpeg')

    def test_the_silent_swallow_is_gone(self) -> None:
        """源码级守卫：那个 `except (KeyError, TypeError): pass` 不许回来。"""
        code = _code_only(_read('core/video_understanding.py'))
        self.assertNotIn('except (KeyError, TypeError)', code)
        self.assertIn('apply_ffmpeg_status_hint_or_problem', code)

    def test_the_plugin_refreshes_the_hint_at_every_chance_it_gets(self) -> None:
        """不许退回"初始化时写一次就完"：能拿到 schema 的时机都要补写。

        反向：删掉 `page_console_config` 里那一句 → 本条红。
        """
        source = _read('main.py')
        self.assertIn('def _refresh_ffmpeg_status_hint(', source)
        self.assertIn('self.video_ffmpeg_status = self._refresh_ffmpeg_status_hint()', source)
        page = source.split('async def page_console_config(self):', 1)
        self.assertEqual(len(page), 2, 'page_console_config 必须还在')
        head = page[1].split('async def ', 1)[0]
        self.assertIn('_refresh_ffmpeg_status_hint(refresh=True)', head,
                      '打开控制台配置页时要补写一次（并重探 ffmpeg）')

    def test_the_plugin_warns_when_the_hint_cannot_be_written(self) -> None:
        """写不进去要有一条**用户可见**的 warn（坑 25），且同一条原因只打一次。"""
        source = _read('main.py')
        self.assertIn('未能把 FFmpeg 状态写进宿主配置页提示', source)
        self.assertIn('logger.warning(', source)
        body = source.split('def _refresh_ffmpeg_status_hint(', 1)[1].split('async def initialize', 1)[0]
        self.assertIn('problem != self._ffmpeg_hint_problem', body, '同一条原因要节流')

    def test_the_plugin_applies_the_patch_at_load_and_logs_one_line(self) -> None:
        source = _read('main.py')
        self.assertIn('apply_ffmpeg_status_hint_or_problem(', source)
        self.assertIn('getattr(self.config, \'schema\', None)', source)
        self.assertIn('视频抽帧识别', source)

    def test_the_host_basis_for_the_dynamic_hint_is_written_down(self) -> None:
        """动态提示能不能做，取决于宿主行为；**依据必须写在注释里**（别只在聊天里说）。"""
        source = _read('core/video_understanding.py')
        for needle in (
            'astrbot/core/star/star_manager.py:603-616',
            'astrbot/dashboard/services/config_service.py:853-872',
            'astrbot/core/config/astrbot_config.py:262-272',
            'astrbot_bridge.py:3845-3866',
        ):
            self.assertIn(needle, source, '宿主依据要写在注释里，别只在聊天里说')


#: 状态词的**文字**判据（不含 emoji）：全量扫描用它，免得漏掉手滑写进来的变体
#: （`✅ FFmpeg 已识别` / `⚠️ 未发现 FFmpeg` 两个取值共用这两个词根）。
FFMPEG_STATUS_NEEDLES = ('FFmpeg 已识别', '未发现 FFmpeg')


def _status_bearing_paths(node: Any, path: tuple[str, ...] = ()) -> list[str]:
    """全量扫描：整份 schema 里**每一处**含状态词的 `hint` / `description` 的点分路径。

    递归走所有 dict / list（分组 → `items` → 键 → 行 → 候选项），`hint` 与 `description`
    都查。`items` 只是结构层，不入路径（与 `ffmpeg_hint_field_path` 同一口径，便于人来读）。
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ('hint', 'description') and isinstance(value, str):
                if any(needle in value for needle in FFMPEG_STATUS_NEEDLES):
                    found.append('.'.join(path + (key,)))
            else:
                found.extend(_status_bearing_paths(value, path if key == 'items' else path + (key,)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_status_bearing_paths(value, path + (str(index),)))
    return found


class FfmpegHintUniquenessTests(unittest.TestCase):
    """**全量唯一性守卫**：状态词只许出现在 `model_center.video.mode.hint`。

    用户真机验收："怎么到处都是 `✅ FFmpeg 已识别` 的文字，只需要「视频识别模式」那里
    显示就可以了。"——所以判据不能是"我检查了那两处"，而是**遍历整份 schema 的每一个
    hint / description**：将来谁把状态词贴到第三个地方（新字段、行、候选项、分组描述…），
    都会在这里被抓住。
    """

    def _repo_schema(self) -> dict[str, Any]:
        with open(SCHEMA_PATH, encoding='utf-8-sig') as handle:
            return json.load(handle)

    def test_the_repo_schema_carries_no_status_word_at_all(self) -> None:
        """静态 schema（`_conf_schema.json`，一个字都不改）里状态词必须是**零处**。"""
        self.assertEqual(_status_bearing_paths(self._repo_schema()), [])

    def test_after_the_write_exactly_one_place_carries_the_status(self) -> None:
        for probe, expected in (('/usr/bin/ffmpeg', '✅ FFmpeg 已识别'),
                                ('', '⚠️ 未发现 FFmpeg')):
            with self.subTest(expected=expected):
                schema = self._repo_schema()
                with mock.patch.object(video, '_FFMPEG_PATH', probe):
                    video.apply_ffmpeg_status_hint(schema)
                self.assertEqual(_status_bearing_paths(schema), ['model_center.video.mode.hint'])
                self.assertTrue(
                    schema['model_center']['items']['video']['items']['mode']['hint']
                    .startswith(expected + '。')
                )

    def test_the_guard_itself_catches_a_second_place(self) -> None:
        """守卫的**反向用例**：把状态词贴回总开关 / 分组描述 → 全量扫描必须报出来。

        没有这一条，"唯一性"就只是个恒真的口号（守卫自己也要能被证明会红）。
        """
        schema = self._repo_schema()
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'):
            video.apply_ffmpeg_status_hint(schema)
        items = schema['model_center']['items']['video']['items']
        # 反向 ①：v1.9.9 那处「启用视频理解」总开关。
        items['enabled']['hint'] = '✅ FFmpeg 已识别。' + items['enabled']['hint']
        self.assertEqual(
            sorted(_status_bearing_paths(schema)),
            ['model_center.video.enabled.hint', 'model_center.video.mode.hint'],
        )
        # 反向 ②：分组描述（控制台分组徽章那条路回到文案上）。
        schema['model_center']['description'] = '✅ FFmpeg 已识别'
        self.assertIn('model_center.description', _status_bearing_paths(schema))
        # 反向 ③：深层 —— 抽帧设置里某一行 / 某个候选项也算"别处"。
        schema['model_center']['items']['video']['items']['frame_interval'] = {
            'hint': '⚠️ 未发现 FFmpeg', 'options': [{'description': '未发现 FFmpeg 时不可用'}],
        }
        self.assertIn('model_center.video.frame_interval.hint', _status_bearing_paths(schema))
        self.assertIn('model_center.video.frame_interval.options.0.description',
                      _status_bearing_paths(schema))


def _schema_video() -> dict[str, Any]:
    return {'model_center': {'items': {'video': {'items': {'mode': {'hint': '静态'}}}}}}  # type: ignore[dict-item]


def _code_only(source: str) -> str:
    """去掉模块/函数 docstring，只留代码（注释里的说法不该被当成实现）。"""
    import re as _re  # noqa: PLC0415

    return _re.sub(r'""".*?"""', '', source, flags=_re.DOTALL)


def _read(relative: str) -> str:
    """读插件根下的一个源文件（`core/...` / `adapters/...` / `main.py`）。"""
    with open(os.path.join(PLUGIN_ROOT, relative), encoding='utf-8') as handle:
        return handle.read()


# =========================================================================== #
# 3. 入站视频坐标（与图片同一条纪律）
# =========================================================================== #

class VideoSourceTests(unittest.TestCase):

    def test_adapter_elements_are_trusted_and_body_text_is_inert(self) -> None:
        url = 'https://multimedia.nt.qq.com.cn/download?fileid=x'
        session = SessionView(
            platform='onebot', self_id='1', user_id='2',
            content='<video src="%s"/>' % url,
            elements=[{'type': 'video', 'attrs': {'src': url}, 'children': []}],
        )
        self.assertEqual(
            video.extract_session_video_sources(session),
            ['onebot-url:%s' % url, 'text:%s' % url],
        )

    def test_local_paths_in_the_body_are_never_readable(self) -> None:
        session = SessionView(
            platform='onebot', self_id='1', user_id='2',
            content='<video src="/etc/passwd"/>[CQ:video,file=/etc/shadow]',
        )
        self.assertEqual(
            video.extract_session_video_sources(session),
            ['text:/etc/passwd', 'text:/etc/shadow'],
        )
        for source in video.extract_session_video_sources(session):
            self.assertEqual(video.video_input_target(source), ('', ''))

    def test_adapter_local_file_becomes_the_one_trusted_file_coordinate(self) -> None:
        session = SessionView(
            platform='onebot', self_id='1', user_id='2', content='',
            elements=[{'type': 'video', 'attrs': {'file': '/data/temp/a.mp4'}, 'children': []}],
        )
        sources = video.extract_session_video_sources(session)
        self.assertEqual(sources, ['onebot-file:/data/temp/a.mp4'])
        self.assertEqual(video.video_input_target(sources[0]), ('file', '/data/temp/a.mp4'))

    def test_url_file_and_unknown_coordinates(self) -> None:
        self.assertEqual(video.video_input_target('onebot-url:https://a/b.mp4'), ('url', 'https://a/b.mp4'))
        self.assertEqual(video.video_input_target('file:///a/b.mp4'), ('file', '/a/b.mp4'))
        self.assertEqual(video.video_input_target('/a/b.mp4'), ('file', '/a/b.mp4'))
        self.assertEqual(video.video_input_target('ftp://a/b.mp4'), ('', ''))

    def test_describe_vision_event_keeps_the_fact_and_drops_the_short_lived_url(self) -> None:
        host = Host(config=video_config())
        url = 'https://cdn.example.com/v.mp4?rkey=SECRET'
        visual = host.describe_vision_event(video_session(url))
        self.assertEqual(visual['content'], '看这个[视频]')
        self.assertNotIn('SECRET', visual['content'], '短效直链不许漏进提示词')
        self.assertNotIn('video', visual['content'])


# =========================================================================== #
# 4. 抽帧识别：预算、降级、以及"真的进了既有两条通道"
# =========================================================================== #

class FramesModeTests(VideoTestCase):

    async def _collect(
        self, host: Host, session: SessionView, ffmpeg: FakeFfmpeg,
    ) -> video.VideoMedia:
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            return await video.collect_video_sources(host, {'id': 's'}, session)

    # ---- 4.1 预算常量真的落在命令行上 ----
    async def test_interval_frame_cap_and_clip_budgets_reach_ffmpeg(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        frames = ffmpeg.argvs('frames')
        audio = ffmpeg.argvs('audio')
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(audio), 1)
        argv = frames[0]
        self.assertIn('fps=1/%d' % video.VIDEO_FRAME_INTERVAL_SECONDS, argv)
        self.assertIn('-frames:v', argv)
        self.assertEqual(argv[argv.index('-frames:v') + 1], str(video.VIDEO_MAX_FRAMES))
        self.assertEqual(argv[argv.index('-t') + 1], str(video.VIDEO_MAX_DURATION_SECONDS))
        # 音轨：单独一条命令、16k 单声道、按配置的格式与时长。
        self.assertEqual(audio[0][audio[0].index('-t') + 1], str(video.VIDEO_AUDIO_CLIP_SECONDS))
        self.assertEqual(audio[0][audio[0].index('-ar') + 1], str(video.VIDEO_AUDIO_SAMPLE_RATE))
        self.assertEqual(audio[0][audio[0].index('-ac') + 1], '1')
        self.assertEqual(audio[0][-1].split('.')[-1], video.VIDEO_DEFAULT_AUDIO_FORMAT)
        self.assertEqual(len(result.image_sources), video.VIDEO_MAX_FRAMES)

    async def test_more_frames_than_the_cap_are_truncated(self) -> None:
        """变异保护：ffmpeg 写了 50 帧，回来只能有 `VIDEO_MAX_FRAMES` 帧。"""
        ffmpeg = FakeFfmpeg(frames_written=50)
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(len(result.image_sources), video.VIDEO_MAX_FRAMES)
        self.assertLess(len(result.image_sources), 50)

    async def test_a_long_video_is_truncated_with_a_countable_clue(self) -> None:
        ffmpeg = FakeFfmpeg(duration=LONG_DURATION, frames_written=50)
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertIn('超过 %d 秒上限' % video.VIDEO_MAX_DURATION_SECONDS, result.note)
        self.assertIn('只看了前 %d 秒' % video.VIDEO_MAX_DURATION_SECONDS, result.note)
        self.assertIn('%d 帧画面' % video.VIDEO_MAX_FRAMES, result.note)
        self.assertTrue(any('秒上限' in message for message in host.warns()))

    async def test_an_unprobeable_duration_is_not_reported_as_truncation(self) -> None:
        """"不知道"不等于"很短"，也不等于"截断了"——探不到就不声称。"""
        ffmpeg = FakeFfmpeg(duration='  Duration: N/A, bitrate: N/A')
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertNotIn('上限', result.note)
        self.assertEqual(result.reason, '')

    # ---- 4.2 帧进图像通道、音轨进语音通道（真实实现，不是第二套） ----
    async def test_frames_go_through_the_existing_image_channel(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertTrue(result.image_sources)
        for source in result.image_sources:
            self.assertTrue(source.startswith('onebot-file:'), source)
        # **同一条** `load_native_images`：能取回 data URI 说明帧真的走了视觉通道。
        images = await ServiceChunk3.load_native_images(
            host, {'id': 's'}, result.image_sources, None,
        )
        self.assertEqual(len(images), video.VIDEO_MAX_FRAMES)
        for image in images:
            self.assertEqual(image['mime_type'], 'image/png')
            self.assertTrue(image['data_uri'].startswith('data:image/png;base64,'))

    async def test_the_audio_track_goes_through_the_existing_voice_channel(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(len(result.audio_sources), 1)
        self.assertTrue(result.audio_sources[0].startswith(
            'data:audio/%s;base64,' % video.VIDEO_DEFAULT_AUDIO_FORMAT))
        audio = await ServiceChunk3.load_native_audio(
            host, {'id': 's'}, result.audio_sources, None,
        )
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]['format'], video.VIDEO_DEFAULT_AUDIO_FORMAT)
        self.assertEqual(audio[0]['id'], 'turn-audio-1')

    async def test_the_vision_and_audio_switches_of_the_existing_channels_still_apply(self) -> None:
        """帧与音轨走的是既有通道 ⇒ 那两条通道自己的闸照旧管着它们。"""
        ffmpeg = FakeFfmpeg()
        config = video_config()
        config['model']['vision']['enabled'] = False
        host = Host(config=config)
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(
            await ServiceChunk3.load_native_images(host, {'id': 's'}, result.image_sources, None), [],
        )

    async def test_the_voice_master_switch_skips_the_audio_command_entirely(self) -> None:
        ffmpeg = FakeFfmpeg()
        config = video_config()
        config['model']['audio']['enabled'] = False
        host = Host(config=config)
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(ffmpeg.argvs('audio'), [])
        self.assertEqual(result.audio_sources, [])
        self.assertNotIn('音轨', result.note)
        self.assertTrue(result.image_sources)

    # ---- 4.3 降级一律不静默、绝不假装成功 ----
    async def test_missing_audio_track_only_skips_the_sound(self) -> None:
        ffmpeg = FakeFfmpeg(has_audio=False)
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(len(result.image_sources), video.VIDEO_MAX_FRAMES)
        self.assertEqual(result.audio_sources, [])
        self.assertEqual(result.reason, '', '没有音轨不是失败')
        self.assertIn('没有音轨', result.note)
        self.assertEqual(ffmpeg.argvs('audio'), [], '没有音轨就不发那条命令')

    async def test_frame_failure_still_keeps_the_audio(self) -> None:
        ffmpeg = FakeFfmpeg(fail_frames=True)
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(result.image_sources, [])
        self.assertEqual(len(result.audio_sources), 1)
        self.assertIn('抽帧失败', result.note)
        self.assertIn('音轨', result.note)

    async def test_an_over_budget_track_says_so_instead_of_claiming_there_is_no_sound(self) -> None:
        """音轨抽到了、被那条通道的体积预算挡下来：**明说**，不许说成"没有音轨"。"""
        ffmpeg = FakeFfmpeg(audio_bytes=2 * 1024 * 1024)
        config = video_config()
        config['model']['audio']['max_file_size_mb'] = 1
        host = Host(config=config)
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(len(result.image_sources), video.VIDEO_MAX_FRAMES, '画面照旧')
        self.assertEqual(result.audio_sources, [])
        self.assertNotIn('没有音轨', result.note)
        self.assertIn('体积预算', result.note)
        self.assertEqual(result.reason, video.VIDEO_AUDIO_OVER_BUDGET_REASON)
        self.assertTrue(any('体积预算' in message for message in host.warns()))

    async def test_nothing_extracted_degrades_with_an_actionable_warn(self) -> None:
        ffmpeg = FakeFfmpeg(fail_frames=True, fail_audio=True)
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual((result.image_sources, result.audio_sources), ([], []))
        self.assertEqual(result.reason, video.VIDEO_EMPTY_REASON)
        self.assertIn('没有抽出任何画面或音轨', result.note)
        self.assertTrue(any('完整可播' in message for message in host.warns()))

    async def test_a_local_file_over_the_size_budget_degrades_before_ffmpeg(self) -> None:
        clip = _TempVideo(b'\x00' * (video.VIDEO_MAX_FILE_SIZE_MB * 1024 * 1024 + 16))
        try:
            ffmpeg = FakeFfmpeg()
            host = Host(config=video_config())
            result = await self._collect(host, clip.session(), ffmpeg)
            self.assertEqual(ffmpeg.calls, [], '超预算的视频一个 ffmpeg 命令都不该发')
            self.assertEqual(result.reason, video.VIDEO_TOO_LARGE_REASON)
            self.assertIn('%d MB' % video.VIDEO_MAX_FILE_SIZE_MB, result.note)
            self.assertTrue(host.warns())
        finally:
            clip.close()

    async def test_a_timeout_degrades_instead_of_hanging_the_turn(self) -> None:
        ffmpeg = FakeFfmpeg(timeout_frames=True)
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(result.image_sources, [])
        self.assertIn('抽帧失败', result.note)
        self.assertIn('音轨', result.note)
        self.assertEqual(result.reason, video.VIDEO_FRAME_FAILED_REASON)
        self.assertTrue(any('抽帧失败' in message for message in host.warns()))

    async def test_the_concurrency_cap_skips_the_second_video_with_a_warn(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())
        video._acquire_slot()  # 模拟"上一个视频还在抽帧"
        try:
            result = await self._collect(host, video_session(), ffmpeg)
        finally:
            video._release_slot()
        self.assertEqual(ffmpeg.calls, [])
        self.assertEqual(result.reason, video.VIDEO_BUSY_REASON)
        self.assertTrue(any('并发上限' in message for message in host.warns()))
        self.assertLessEqual(video.VIDEO_MAX_CONCURRENCY, 1)

    async def test_the_temp_directory_is_cleaned_up_by_the_caller(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        workdir = result.workdir
        self.assertTrue(os.path.isdir(workdir))
        result.cleanup()
        self.assertFalse(os.path.exists(workdir))
        result.cleanup()  # 幂等
        # 音轨是 `data:` URI：临时目录删掉之后仍能交给语音通道。
        audio = await ServiceChunk3.load_native_audio(host, {'id': 's'}, result.audio_sources)
        self.assertEqual(len(audio), 1)

    async def test_the_same_reason_is_warned_at_most_once_per_window(self) -> None:
        host = Host(config=video_config())
        for _ in range(3):
            await self._collect(host, video_session(), FakeFfmpeg(fail_frames=True, fail_audio=True))
        self.assertEqual(len(host.warns()), 1, host.warns())
        host.clock += video.VIDEO_WARN_INTERVAL_MS + 1
        await self._collect(host, video_session(), FakeFfmpeg(fail_frames=True, fail_audio=True))
        self.assertEqual(len(host.warns()), 2)


# =========================================================================== #
# 4.4 端到端：真实 `flush_buffered_narrative` 里的调用序列
# =========================================================================== #

class TurnLevelWiringTests(VideoTestCase):
    """在**真实回合主链**上断言"帧进了图像通道、音轨进了语音通道"。

    复用 `test_service_chunk3._FlushHost`（house style：跨测试模块复用宿主桩，
    见 `test_console_api` / `test_qzone_napcat_channel`）：这里只把 `model.video`
    打开、把会话换成一条带视频的事件，再记下那两条通道**实际收到的来源表**。

    这一条是"把帧绕过既有图像通道自造一套"的**行为**守卫：只改注释或换个变量名
    骗不过它——`load_native_images` 收到的来源表里必须真的有那三帧。
    """

    def _host(self) -> Any:
        from plugin.tests.test_service_chunk3 import _FlushHost  # noqa: PLC0415

        class _VideoFlushHost(_FlushHost):
            def __init__(self) -> None:
                super().__init__()
                self.config = video_config()
                self.seen: dict[str, Any] = {'images': [], 'audio': []}

            async def load_native_images(self, story, sources, session=None, media=None):
                self.seen['images'] = list(sources)
                return await ServiceChunk3.load_native_images(self, story, sources, session, media)

            async def load_native_audio(self, story, sources, session=None, max_count=None):
                self.seen['audio'] = list(sources)
                return await ServiceChunk3.load_native_audio(self, story, sources, session, max_count)

        return _VideoFlushHost()

    async def test_the_real_turn_feeds_frames_into_vision_and_the_track_into_voice(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = self._host()
        turn = {
            'storyId': 's', 'participantId': 'p',
            'messages': [{
                'content': '看这个[视频]', 'occurredAt': NOW,
                'imageSources': [], 'audioSources': [],
            }],
            'latestSession': video_session(), 'timer': None, 'nextRevision': 3,
            'inFlightRequestId': None, 'obsoleteRequestIds': set(),
        }
        host.buffered_narrative_turns = {'k': turn}
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)

        # ① 调用序列：那两条通道收到的来源表里确实有帧与音轨。
        self.assertEqual(len(host.seen['images']), video.VIDEO_MAX_FRAMES, host.seen['images'])
        for source in host.seen['images']:
            self.assertTrue(source.startswith('onebot-file:'), source)
        self.assertEqual(len(host.seen['audio']), 1)
        self.assertTrue(host.seen['audio'][0].startswith(
            'data:audio/%s;base64,' % video.VIDEO_DEFAULT_AUDIO_FORMAT))

        # ② 进叙事的形态：帧是原生视觉图片、音轨是原生音频附件。
        images = host.calls['try_decide'][9]
        audio = host.calls['try_decide'][10]
        self.assertEqual(len(images), video.VIDEO_MAX_FRAMES)
        for image in images:
            self.assertEqual(image['mime_type'], 'image/png')
            self.assertNotIn('path', image, '交给模型的是字节，不是本地路径')
        self.assertEqual([item['format'] for item in audio], [video.VIDEO_DEFAULT_AUDIO_FORMAT])

        # ③ 正文里那句事实（含可数线索）并进了本回合的用户消息。
        user_message = host.calls['try_decide'][5]
        self.assertIn(video.VIDEO_FACT_PREFIX, user_message)
        self.assertIn('%d 帧画面' % video.VIDEO_MAX_FRAMES, user_message)

        # ④ 这一次的临时目录在图像通道读完字节后就被删掉（不留垃圾）。
        self.assertEqual(self.new_temp_dirs(), [], self.new_temp_dirs())

    async def test_three_forwarded_videos_really_take_three_ffmpeg_runs(self) -> None:
        """v1.9.4 端到端：媒体表里 3 段转发视频 + `max_videos=3` → **三次抽帧**。

        顺带把两件"共用通道"的事钉在真参数上（它们不是视频自己的闸，但决定了端到端
        有效值）：

        * 帧与直发 / 转发的图共用**每回合图片预算**：本用例把「单条转发最多读取的图片数」
          调到 9（每回合上限没动 → 跟随成 9），所以 3×3 帧**全都**进得了模型；
        * 音轨走的是**语音通道**，要服从那条通道自己的 `max_per_message`（默认 1；
          可见键在「模型中心 → 语音 / 音频理解设置」）——它今天是一处**静默**截断，
          本用例只如实断言现状，不在这一轮改它。
        """
        ffmpeg = FakeFfmpeg()
        host = self._host()
        host.config['forward_message'] = {'max_videos': 3, 'max_images': 9}
        sources = ['https://cdn.example.com/v%d.mp4' % index for index in (1, 2, 3)]
        turn = {
            'storyId': 's', 'participantId': 'p',
            'messages': [{'content': '看这几个视频', 'occurredAt': NOW,
                          'imageSources': [], 'audioSources': []}],
            'latestSession': SessionView(
                platform='onebot', self_id='1', user_id='2', is_direct=True,
                content='[视频]', elements=[],
                media=[{'kind': 'video', 'source': source, 'summary': ''} for source in sources],
            ),
            'timer': None, 'nextRevision': 3,
            'inFlightRequestId': None, 'obsoleteRequestIds': set(),
        }
        host.buffered_narrative_turns = {'k': turn}
        self.assertEqual(video._VIDEO_IN_FLIGHT, 0, '进这一条时不许有别的收集占着闸')
        with self.video_slot_guard(), \
                mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)

        self.assertEqual(len(ffmpeg.argvs('frames')), 3, '三段视频 = 三次抽帧（旧实现只有 1 次）')
        self.assertEqual(len(host.seen['images']), 3 * video.VIDEO_MAX_FRAMES)
        self.assertEqual(len(host.calls['try_decide'][9]), 3 * video.VIDEO_MAX_FRAMES,
                         '每回合上限跟随到 9 → 九帧全都进模型')
        self.assertEqual(len(host.calls['try_decide'][10]), 1,
                         '音轨由语音通道的 max_per_message 决定（默认 1，可见键）')
        self.assertEqual(host.calls['try_decide'][5].count(video.VIDEO_FACT_PREFIX), 3,
                         '正文里一段视频一行事实')
        self.assertEqual(self.new_temp_dirs(), [], '三个临时目录都要删干净')

    async def test_a_video_bug_never_breaks_the_turn_or_loses_the_message(self) -> None:
        """反向：视频理解炸了也必须**照常出正文**（回合主链不为附加能力回滚）。"""
        from plugin.core.service import chunk3 as chunk3_module  # noqa: PLC0415

        host = self._host()

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError('视频理解炸了')

        turn = {
            'storyId': 's', 'participantId': 'p',
            'messages': [{'content': '看这个[视频]', 'occurredAt': NOW,
                          'imageSources': [], 'audioSources': []}],
            'latestSession': video_session(), 'timer': None, 'nextRevision': 3,
            'inFlightRequestId': None, 'obsoleteRequestIds': set(),
        }
        host.buffered_narrative_turns = {'k': turn}
        with mock.patch.object(chunk3_module, 'collect_video_sources', boom):
            await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)

        self.assertEqual(host.calls['try_decide'][5], '看这个[视频]', '正文一个字都不能丢')
        self.assertEqual(host.calls['try_decide'][9], [])
        self.assertEqual(host.calls['send_outgoing_messages'][0], [{'content': '在的。'}])
        self.assertEqual(host.buffered_narrative_turns, {}, '回合照常收尾')
        self.assertTrue(
            any('视频理解失败' in str(entry) and '炸了' in str(entry) for entry in host.logs),
            host.logs,
        )

    async def test_a_turn_without_video_is_completely_untouched(self) -> None:
        """反向：没有视频的回合照旧——一条 ffmpeg 都不发、图片/音频照旧为空。"""
        ffmpeg = FakeFfmpeg()
        host = self._host()
        turn = {
            'storyId': 's', 'participantId': 'p',
            'messages': [{'content': '在吗', 'occurredAt': NOW, 'imageSources': [], 'audioSources': []}],
            'latestSession': SessionView(platform='onebot', self_id='1', user_id='2', content='在吗'),
            'timer': None, 'nextRevision': 3,
            'inFlightRequestId': None, 'obsoleteRequestIds': set(),
        }
        host.buffered_narrative_turns = {'k': turn}
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)
        self.assertEqual(ffmpeg.calls, [])
        self.assertEqual(host.calls['try_decide'][9], [])
        self.assertEqual(host.calls['try_decide'][10], [])
        self.assertEqual(host.calls['try_decide'][5], '在吗')


# =========================================================================== #
# 5. 模式：关闭 / 原生 / 外挂
# =========================================================================== #

class DisabledModeTests(VideoTestCase):

    async def test_disabled_means_zero_ffmpeg_and_zero_model_calls(self) -> None:
        ffmpeg = FakeFfmpeg()
        narrator = FakeNarrator()
        host = Host(config=video_config(enabled=False, mode='frames'), narrator=narrator)
        baseline = list(host.logs)
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            result = await video.collect_video_sources(host, {'id': 's'}, video_session())
        self.assertEqual(ffmpeg.calls, [], '关着时一个 ffmpeg 命令都不许发')
        self.assertEqual(narrator.calls, [], '关着时一次模型调用都不许发')
        self.assertEqual((result.image_sources, result.audio_sources), ([], []))
        self.assertEqual(result.note, '')
        self.assertEqual(result.reason, '')
        self.assertEqual(host.logs, baseline, '关着时一条日志都不该多')
        self.assertEqual(host.warns(), [])

    async def test_disabled_with_external_mode_also_stays_silent(self) -> None:
        ffmpeg = FakeFfmpeg()
        narrator = FakeNarrator()
        host = Host(config=video_config(enabled=False, mode='external', model_id='vlm'), narrator=narrator)
        with mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            result = await video.collect_video_sources(host, {'id': 's'}, video_session())
        self.assertEqual((ffmpeg.calls, narrator.calls, result.note), ([], [], ''))

    async def test_a_session_without_any_video_does_nothing(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())
        session = SessionView(platform='onebot', self_id='1', user_id='2', content='纯文字', elements=[])
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            result = await video.collect_video_sources(host, {'id': 's'}, session)
        self.assertEqual(ffmpeg.calls, [])
        self.assertEqual(result.note, '')


class NativeModeTests(VideoTestCase):

    async def test_native_degrades_explicitly_without_any_model_call(self) -> None:
        ffmpeg = FakeFfmpeg()
        narrator = FakeNarrator()
        host = Host(config=video_config(mode='native'), narrator=narrator)
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            result = await video.collect_video_sources(host, {'id': 's'}, video_session())
        self.assertEqual(result.reason, video.NATIVE_UNSUPPORTED_REASON)
        self.assertEqual((result.image_sources, result.audio_sources), ([], []))
        self.assertEqual(ffmpeg.calls, [], 'native 不是抽帧识别，不许偷偷抽帧')
        self.assertEqual(narrator.calls, [], '宿主没有通路，不许假装交给了模型')
        self.assertIn('通路', result.note)
        self.assertIn('没有交给任何模型', result.note)
        self.assertIn('抽帧识别', result.note, '降级文案必须可行动')
        self.assertTrue(any('没有通路' in message for message in host.warns()))

    def test_the_host_limitation_claim_matches_our_own_outbound_chain(self) -> None:
        """`native` 之所以只能降级：出站只认图片与音频（本插件侧的源码依据）。"""
        bridge = _read('adapters/astrbot_bridge.py') if os.path.exists(
            os.path.join(PLUGIN_ROOT, 'adapters', 'astrbot_bridge.py')) else ''
        self.assertIn("kind == 'image_url'", bridge)
        self.assertIn("kind == 'input_audio'", bridge)
        self.assertIn("parts.append(_text(part.get('text')))", bridge)
        self.assertIn("params['image_urls'] = image_urls", bridge)
        self.assertIn("params['audio_urls'] = audio_urls", bridge)
        self.assertNotIn("'video_url'", bridge, '宿主链路上没有视频部件，别在适配层假装有')

    def test_the_degradation_names_the_two_ways_out(self) -> None:
        message = video.degradation_message(video.NATIVE_UNSUPPORTED_REASON)
        self.assertIn('抽帧识别', message)
        self.assertIn('外挂识别', message)


class ExternalModeTests(VideoTestCase):

    def _config(self, model_id: str) -> dict[str, Any]:
        config = video_config(mode='external', model_id=model_id)
        config['model']['providers'] = [{
            'id': 'astrbot-video', 'label': 'AstrBot · vlm', 'enabled': True,
            'mode': 'openai-compatible', 'endpoint': '',
            'transport_target': 'astrbot:vlm', 'model': 'qwen-vl-max',
            'use_for_video': True,
        }]
        return config

    async def test_a_missing_named_model_fails_without_falling_back(self) -> None:
        ffmpeg = FakeFfmpeg()
        narrator = FakeNarrator()
        host = Host(config=video_config(mode='external', model_id=''), narrator=narrator)
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            result = await video.collect_video_sources(host, {'id': 's'}, video_session())
        self.assertEqual(result.reason, video.EXTERNAL_NO_MODEL_REASON)
        self.assertEqual((result.image_sources, result.audio_sources), ([], []))
        self.assertEqual(ffmpeg.calls, [], '不许悄悄退回抽帧')
        self.assertEqual(narrator.calls, [])
        self.assertTrue(host.warns())

    async def test_an_unknown_named_model_fails_without_falling_back(self) -> None:
        ffmpeg = FakeFfmpeg()
        narrator = FakeNarrator()
        host = Host(config=video_config(mode='external', model_id='typo'), narrator=narrator)
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            result = await video.collect_video_sources(host, {'id': 's'}, video_session())
        self.assertEqual(result.reason, video.EXTERNAL_MISSING_MODEL_REASON)
        self.assertEqual(ffmpeg.calls, [], '不回落 = 不许退回抽帧')
        self.assertEqual(narrator.calls, [], '不回落 = 不许改走别的模型')
        self.assertTrue(any('不回落' in message for message in host.warns()))

    async def test_the_named_provider_gets_the_url_and_its_answer_enters_the_turn(self) -> None:
        narrator = FakeNarrator(answer='画面里是一只橘猫在窗台上')
        host = Host(config=self._config('vlm'), narrator=narrator)
        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            result = await video.collect_video_sources(host, {'id': 's'}, video_session())
        self.assertEqual(result.reason, '')
        self.assertEqual(len(narrator.calls), 1)
        call = narrator.calls[0]
        self.assertEqual(call['task'], video.VIDEO_TASK)
        self.assertEqual(call['model'], 'qwen-vl-max')
        prompt = call['body']['messages'][-1]['content']
        self.assertIn('https://cdn.example.com/v.mp4', prompt, '外挂拿到的是**直链文本**')
        # 首轮带输出预算、降级重试放开（与同族所有 `build_body` 同一形状）。
        # 反向：把这一支写成 `if not capped`（v1.9.7 的现场）→ 本条立刻红。
        self.assertEqual(call['body']['max_tokens'], 1_200, '首轮要带输出预算')
        self.assertNotIn('max_tokens', call['body_without_cap'], '重试那一趟要放开预算')
        # 不许自造宿主链路上不存在的视频部件（那会被静默丢掉）。
        self.assertNotIn('video_url', json.dumps(call['body'], ensure_ascii=False))
        self.assertIn('橘猫', result.note)
        self.assertEqual((result.image_sources, result.audio_sources), ([], []))

    async def test_a_local_file_cannot_be_uploaded_through_the_host_path(self) -> None:
        clip = _TempVideo()
        try:
            narrator = FakeNarrator()
            host = Host(config=self._config('vlm'), narrator=narrator)
            result = await video.collect_video_sources(host, {'id': 's'}, clip.session())
            self.assertEqual(result.reason, video.EXTERNAL_NO_URL_REASON)
            self.assertEqual(narrator.calls, [], '发不出字节就别假装发了')
            self.assertTrue(any('直链' in message for message in host.warns()))
        finally:
            clip.close()

    async def test_a_provider_error_is_a_failure_not_a_silent_fallback(self) -> None:
        narrator = FakeNarrator(error=RuntimeError('Connection error'))
        host = Host(config=self._config('vlm'), narrator=narrator)
        result = await video.collect_video_sources(host, {'id': 's'}, video_session())
        self.assertTrue(result.reason.startswith(video.EXTERNAL_MISSING_MODEL_REASON))
        self.assertIn('Connection error', result.note)
        self.assertNotIn('橘猫', result.note)


# =========================================================================== #
# 6. 变异（反向）：三条"如果实现被改坏就必须红"的守卫
# =========================================================================== #

class MutationTests(unittest.TestCase):

    def test_removing_the_frame_cap_would_break_the_argv_assertion(self) -> None:
        """上限不是装饰：`-frames:v` / `fps=…` / `-t` 都由配置算出并写进命令行（v1.9.1）。"""
        source = _read('core/video_understanding.py')
        self.assertIn("'-frames:v', str(frames_cap)", source)
        self.assertIn("'fps=1/%d' % max(1, int(interval_seconds))", source)
        self.assertIn("'-t', str(max_duration_seconds)", source)
        self.assertIn('frames = _collect_frames(directory, frames_cap)', source)
        self.assertIn('return frames[:max(1, int(cap))]', source)
        # 真正的行为守卫在 `FramesModeTests.test_more_frames_than_the_cap_are_truncated`
        # 与 `test_a_long_video_is_truncated_with_a_countable_clue`：那两条会真的喂 50 帧。

    def test_pretending_success_when_ffmpeg_is_missing_would_be_caught(self) -> None:
        source = _read('core/video_understanding.py')
        self.assertIn('if not ffmpeg_available():', source)
        self.assertIn('result.reason = FFMPEG_MISSING_REASON', source)
        self.assertIn('_warn(service, story, result.reason)', source)
        self.assertIn('装上 FFmpeg', video.degradation_message(video.FFMPEG_MISSING_REASON))
        # 行为守卫：`MissingFfmpegTests`。

    def test_a_second_vision_path_would_be_caught(self) -> None:
        """帧只以**来源坐标**交出去；本模块自己不许去取字节、也不许调模型。"""
        source = _read('core/video_understanding.py')
        self.assertNotIn('load_native_images(', source, '帧要走既有通道，不许在这里取回')
        self.assertNotIn('load_native_audio(', source, '音轨要走既有通道')
        chunk3 = _read('core/service/chunk3.py')
        body = chunk3.split('async def flush_buffered_narrative', 1)[1].split(
            '\n    async def advance_story', 1,
        )[0]
        self.assertIn('collect_video_sources(self,', body)
        # v1.9.4：帧并进来源表那一行现在叫 `available_images`（候选 = 直发 / 转发来源 +
        # 帧，之后按**每回合图片预算**取前 N 张）。行名变了，但这条守卫守的两件事没变：
        # ① 帧必须并进**既有的**来源表（不是另造通道）；② 并进去之后才交给
        # `load_native_images`，且附件（"消息自带了什么"）在合并之前就算完。
        merge_image = 'available_images = _unique(image_candidates + list(video_media.image_sources))'
        budget_cut = 'image_sources = available_images[:image_budget]'
        merge_audio = '] + list(video_media.audio_sources))'
        self.assertIn(merge_image, body)
        self.assertIn(budget_cut, body, '取交集必须用同一个每回合图片预算（判据一处）')
        self.assertIn(merge_audio, body)
        # 顺序：先并进来源表、再按预算取前 N 张、最后交给那两条通道（并反了就白搭）。
        self.assertLess(body.index(merge_image), body.index(budget_cut))
        self.assertLess(body.index(merge_image), body.index('loaded_images = await self.load_native_images('))
        self.assertLess(body.index(merge_audio), body.index('audio = await self.load_native_audio('))
        # 附件（"消息自带了什么"）在合并之前就算完了：派生帧不该被标成三张图片。
        self.assertLess(body.index('attachments = ['), body.index(merge_image))


class MissingFfmpegTests(VideoTestCase):
    """ffmpeg 缺失：降级 + 可行动 warn，且**绝不假装成功**（变异 2 的行为守卫）。"""

    async def test_no_ffmpeg_means_a_degradation_and_an_actionable_warning(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())
        with mock.patch.object(video, '_FFMPEG_PATH', ''), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            result = await video.collect_video_sources(host, {'id': 's'}, video_session())
        self.assertEqual(ffmpeg.calls, [], '没有 ffmpeg 就没有命令可发')
        self.assertEqual((result.image_sources, result.audio_sources), ([], []))
        self.assertEqual(result.reason, video.FFMPEG_MISSING_REASON)
        self.assertIn('没检查到 FFmpeg', result.note)
        self.assertIn('ffmpeg -version', result.note)
        self.assertEqual(len(host.warns()), 1)
        self.assertIn('装上 FFmpeg', host.warns()[0])

    async def test_the_message_tells_the_user_what_to_install(self) -> None:
        message = video.degradation_message(video.FFMPEG_MISSING_REASON)
        self.assertIn('FFmpeg', message)
        self.assertIn('ffmpeg -version', message)


# =========================================================================== #
# 4.5 可配置预算（v1.9.1）：抽帧模式 / 帧数 / 音轨格式 / 音轨时长 / 超时 / 群聊开关
# =========================================================================== #

class BudgetConfigTests(VideoTestCase):
    """**读配置只在一处**（`resolve_video_config`），行为必须真的跟着配置走。"""

    async def _collect(
        self, host: Host, session: SessionView, ffmpeg: FakeFfmpeg,
    ) -> video.VideoMedia:
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            return await video.collect_video_sources(host, {'id': 's'}, session)

    def _config(self, **video_keys: Any) -> dict[str, Any]:
        config = video_config()
        config['model']['video'].update(video_keys)
        return config

    # ---- 4.5.1 抽帧模式 ----
    async def test_sequence_mode_uses_the_configured_interval(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(frame_interval_seconds=10))
        await self._collect(host, video_session(), ffmpeg)
        argv = ffmpeg.argvs('frames')[0]
        self.assertIn('fps=1/10', argv)
        self.assertEqual(argv[argv.index('-frames:v') + 1], str(video.VIDEO_MAX_FRAMES))

    async def test_average_mode_spreads_exactly_the_configured_frame_count(self) -> None:
        """变异保护：平均抽帧**必须**按「平均抽几帧」算 fps 与帧数上限。

        改成忽略它（照旧 `fps=1/4`、或照旧封顶 3 帧）这一条就红。
        """
        ffmpeg = FakeFfmpeg(duration='  Duration: 00:00:12.00, start: 0.000000')
        host = Host(config=self._config(frame_mode='average', frame_average_count=1))
        result = await self._collect(host, video_session(), ffmpeg)
        argv = ffmpeg.argvs('frames')[0]
        self.assertIn('fps=%.6f' % (1 / 12), argv)
        self.assertEqual(argv[argv.index('-frames:v') + 1], '1')
        self.assertEqual(len(result.image_sources), 1, '配成 1 帧就只能交 1 帧')
        self.assertIn('已整段平均抽了 1 帧画面', result.note)

    async def test_average_mode_without_a_probeable_duration_falls_back_to_the_interval(self) -> None:
        """探不到时长就退回连续抽帧那条 fps（**不猜时长**），帧数上限照旧是配置值。"""
        ffmpeg = FakeFfmpeg(duration='  Duration: N/A, bitrate: N/A')
        host = Host(config=self._config(frame_mode='average', frame_average_count=2))
        await self._collect(host, video_session(), ffmpeg)
        argv = ffmpeg.argvs('frames')[0]
        self.assertIn('fps=1/%d' % video.VIDEO_FRAME_INTERVAL_SECONDS, argv)
        self.assertEqual(argv[argv.index('-frames:v') + 1], '2')

    # ---- 4.5.2 音轨：格式与时长 ----
    async def test_the_configured_audio_format_reaches_ffmpeg_and_the_voice_channel(self) -> None:
        """转码格式照「语音 / 音频理解」那组**同名同义**：复用器与 data URI 的格式段都要
        对上，否则 `fetch_native_audio` 的白名单会把音轨整条丢掉（静默没声音）。"""
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(out_format='ogg'))
        result = await self._collect(host, video_session(), ffmpeg)
        argv = ffmpeg.argvs('audio')[0]
        self.assertEqual(argv[argv.index('-f') + 1], 'ogg')
        self.assertTrue(argv[-1].endswith('.ogg'), argv[-1])
        self.assertTrue(result.audio_sources[0].startswith('data:audio/ogg;base64,'))
        audio = await ServiceChunk3.load_native_audio(host, {'id': 's'}, result.audio_sources, None)
        self.assertEqual([item['format'] for item in audio], ['ogg'])

    async def test_the_default_audio_format_is_the_voice_groups_default(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(ffmpeg.argvs('audio')[0][-1].split('.')[-1],
                         video.VIDEO_DEFAULT_AUDIO_FORMAT)
        audio = await ServiceChunk3.load_native_audio(host, {'id': 's'}, result.audio_sources, None)
        self.assertEqual([item['format'] for item in audio], [video.VIDEO_DEFAULT_AUDIO_FORMAT])

    async def test_custom_audio_duration_is_a_configuration_value(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(audio_duration='custom', audio_duration_seconds=15))
        result = await self._collect(host, video_session(), ffmpeg)
        argv = ffmpeg.argvs('audio')[0]
        self.assertEqual(argv[argv.index('-t') + 1], '15')
        self.assertIn('已单独抽出前 15 秒音轨', result.note)

    async def test_unlimited_audio_duration_does_not_truncate_the_track(self) -> None:
        """变异保护：`unlimited` 还按 60 秒截 → 红。

        `-t` 就是截断本身；不限制时音轨那条命令里一个字都不该出现（帧那条命令的 `-t`
        是"最长处理多长"，与音轨时长无关，所以只看 audio 那条 argv）。
        """
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(audio_duration='unlimited'))
        result = await self._collect(host, video_session(), ffmpeg)
        argv = ffmpeg.argvs('audio')[0]
        self.assertNotIn('-t', argv, '不限制 = 不给 ffmpeg 传 -t')
        self.assertIn('已单独抽出整段音轨', result.note)

    # ---- 4.5.3 超时：抽到几帧交几帧（用户口径） ----
    async def test_a_timeout_keeps_the_frames_already_written(self) -> None:
        """变异保护：超时把已抽到的帧丢掉 → 红（旧行为就是"整段丢弃"）。"""
        ffmpeg = FakeFfmpeg(timeout_frames=True, frames_before_timeout=2)
        host = Host(config=self._config(timeout_seconds=7))
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(len(result.image_sources), 2, '超时前抽到的帧必须照常交出去')
        for source in result.image_sources:
            self.assertTrue(source.startswith('onebot-file:'), source)
        # 可数线索：超时上限 + 已交付帧数。
        self.assertIn('抽帧在 7 秒超时，已抽到的 2 帧照常提交', result.note)
        self.assertTrue(result.reason.startswith(video.VIDEO_FRAME_TIMEOUT_PREFIX))
        self.assertIn('2 帧', result.reason)
        self.assertTrue(any('照常交给模型' in message for message in host.warns()),
                        '超时是"配置可调"的降级，必须打一条可行动的 warn')
        # 帧真的能走既有图像通道（不是只留下一串路径）。
        images = await ServiceChunk3.load_native_images(
            host, {'id': 's'}, result.image_sources, None,
        )
        self.assertEqual(len(images), 2)

    async def test_a_timeout_without_any_frame_is_still_a_frame_failure(self) -> None:
        """一帧都没抽到就不该假装"交了几帧"：走既有的抽帧失败降级。"""
        ffmpeg = FakeFfmpeg(timeout_frames=True)
        host = Host(config=self._config(timeout_seconds=7))
        result = await self._collect(host, video_session(), ffmpeg)
        self.assertEqual(result.image_sources, [])
        self.assertEqual(result.reason, video.VIDEO_FRAME_FAILED_REASON)
        self.assertIn('抽帧失败', result.note)

    # ---- 4.5.4 群聊独立开关 ----
    async def test_the_group_switch_off_means_zero_ffmpeg_in_groups(self) -> None:
        """变异保护：群聊开关关着仍抽帧 → 红。"""
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(group_enabled=False))
        result = await self._collect(host, video_session(is_direct=False), ffmpeg)
        self.assertEqual(ffmpeg.calls, [], '群聊开关关着时一个 ffmpeg 命令都不许发')
        self.assertEqual((result.image_sources, result.audio_sources, result.note), ([], [], ''))

    async def test_the_group_switch_on_is_the_only_difference_in_groups(self) -> None:
        """反向：同一个群聊会话，开关打开就照常抽帧（闸门真的在管，不是恒假）。"""
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(group_enabled=True))
        result = await self._collect(host, video_session(is_direct=False), ffmpeg)
        self.assertEqual(len(ffmpeg.argvs('frames')), 1)
        self.assertTrue(result.image_sources)

    async def test_private_chats_are_not_gated_by_the_group_switch(self) -> None:
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(group_enabled=False))
        result = await self._collect(host, video_session(is_direct=True), ffmpeg)
        self.assertTrue(result.image_sources, '私聊只受总开关管')

    # ---- 4.5.5 配置归一化：手改坏了也不许悄悄变成"更贵的那个" ----
    def test_resolve_video_config_clamps_and_falls_back(self) -> None:
        resolved = video.resolve_video_config({
            'frame_mode': 'AVERAGE', 'frameIntervalSeconds': '3',
            'frame_average_count': 99, 'outFormat': 'MP3',
            'audio_duration': 'UNLIMITED', 'audioDurationSeconds': '0',
            'timeoutSeconds': 'abc', 'groupEnabled': True,
        })
        self.assertEqual(resolved['frame_mode'], 'average')
        self.assertEqual(resolved['frame_interval_seconds'], 3)
        self.assertEqual(resolved['frame_average_count'], 99,
                         'v1.9.7 起没有上界：多配的帧由每回合图片预算那道可见的闸去裁')
        self.assertEqual(resolved['out_format'], 'mp3')
        self.assertEqual(resolved['audio_duration'], 'unlimited')
        self.assertEqual(resolved['audio_duration_seconds'], 1, '脏值回默认再夹到下限')
        self.assertEqual(resolved['timeout_seconds'], video.VIDEO_FFMPEG_TIMEOUT_SECONDS)
        self.assertIs(resolved['group_enabled'], True)
        for raw in ('weird', None, 7):
            with self.subTest(raw=raw):
                self.assertEqual(
                    video.resolve_video_config({'frame_mode': raw})['frame_mode'], 'sequence',
                )
                self.assertEqual(
                    video.resolve_video_config({'audio_duration': raw})['audio_duration'], 'custom',
                )
                self.assertEqual(
                    video.resolve_video_config({'out_format': raw})['out_format'], 'mp3',
                )

    def test_unlimited_is_the_only_way_to_get_no_clip(self) -> None:
        self.assertIsNone(video.audio_clip_seconds({'audio_duration': 'unlimited'}))
        self.assertEqual(video.audio_clip_seconds({'audio_duration': 'custom',
                                                   'audio_duration_seconds': 30}), 30)
        self.assertEqual(video.audio_clip_seconds({}), video.VIDEO_AUDIO_CLIP_SECONDS)

    def test_the_config_is_read_in_exactly_one_place(self) -> None:
        """判据一处：模块里只有 `video_config()` 读 `model.video`，其余函数只收参数。"""
        source = _read('core/video_understanding.py')
        self.assertEqual(source.count("'video', 'video_understanding'"), 1)
        # `collect_video_sources` 把**归一化后的**配置往下传（不再各自读一遍）。
        for needle in (
            "frame_mode=config['frame_mode']",
            "interval_seconds=config['frame_interval_seconds']",
            "average_frames=config['frame_average_count']",
            "audio_format=config['out_format']",
            "timeout=float(config['timeout_seconds'])",
        ):
            self.assertIn(needle, source)
        # 常量退化成默认值：命令行里的数字全部由参数（= 配置）算出来。
        self.assertIn("'fps=1/%d' % max(1, int(interval_seconds))", source)


# =========================================================================== #
# 4.5.5b 一回合读几段视频（v1.9.4）：`forward_message.max_videos` 是**同一处**判据
# =========================================================================== #

class MultiVideoBudgetTests(VideoTestCase):
    """配了几段就真的读几段 —— 旧实现写死 `sources[0]`：配 3 段也只读第 1 段。

    那是用户点名的"不合逻辑的分支"：配置项说"最多读取 N 段"，正文线索也写
    「仅取前 N 段」，实际却只读第一段 —— 配置与线索同时说谎。这里的用例是那条的
    反向守卫：`max_videos=3` 就必须有三次抽帧；写死回 1 → 红。
    """

    def _config(self, max_videos: Any, **video_keys: Any) -> dict[str, Any]:
        config = video_config()
        config['model']['video'].update(video_keys)
        config['forward_message'] = {'max_videos': max_videos}
        return config

    @staticmethod
    def _session(*targets: str, is_direct: bool = True) -> SessionView:
        """一条带 N 段视频的会话（元素 = 可信坐标；正文那份 `text:` 坐标永不取回）。"""
        return SessionView(
            platform='onebot', self_id='1', user_id='2', is_direct=is_direct,
            content=' '.join('<video src="%s"/>' % target for target in targets),
            elements=[
                {'type': 'video', 'attrs': {'src': target}, 'children': []}
                for target in targets
            ],
            media=[],
        )

    async def _collect(
        self, host: Host, session: SessionView, ffmpeg: FakeFfmpeg,
    ) -> video.VideoMedia:
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            return await video.collect_video_sources(host, {'id': 's'}, session)

    async def test_every_video_in_the_turn_is_really_read(self) -> None:
        """① 配 3 段 + 会话里 3 段 → **三次抽帧、三段音轨**（不是只读第一段）。

        `video_slot_guard()` 把"单回合不会被并发闸自己撞到"这条不变量钉在这一条上：
        历史 flake「2 != 3」唯一可能的来源就是它被别的收集占住（§84）。
        """
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(3))
        session = self._session(*['https://cdn.example.com/v%d.mp4' % index for index in (1, 2, 3)])
        self.assertEqual(video._VIDEO_IN_FLIGHT, 0, '进这一条时不许有别的收集占着闸')
        with self.video_slot_guard():
            result = await self._collect(host, session, ffmpeg)

        self.assertEqual(len(ffmpeg.argvs('frames')), 3, '三段视频 = 三次抽帧命令')
        self.assertEqual(len(ffmpeg.argvs('audio')), 3, '每段各抽一条音轨')
        self.assertEqual(
            len(result.image_sources), 3 * video.VIDEO_MAX_FRAMES, '每段各交自己的帧',
        )
        self.assertEqual(len(result.audio_sources), 3)
        self.assertNotIn('仅取前', result.note, '没截断就不许说"仅取前 N 段"')

    async def test_the_default_budget_reads_exactly_one_and_says_so(self) -> None:
        """**反向**：默认（没有 `forward_message` 段 = 1）只读一段，并留下可数线索。

        变异保护：把"写死 1"还原成"写死第 0 段但线索还写 N" → 这里断言的线索与条数对不上。
        """
        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())  # 刻意不给 forward_message 段 = 全默认
        session = self._session(
            'https://cdn.example.com/v1.mp4',
            'https://cdn.example.com/v2.mp4',
            'https://cdn.example.com/v3.mp4',
        )
        result = await self._collect(host, session, ffmpeg)
        self.assertEqual(len(ffmpeg.argvs('frames')), 1, '默认只读一段（省成本那侧）')
        self.assertEqual(len(result.image_sources), video.VIDEO_MAX_FRAMES)
        self.assertIn('[视频×3，本回合仅取前 1 段]', result.note, '截断必须可见')
        self.assertTrue(
            any('单条转发最多读取的视频数' in message for message in host.warns()),
            '截断是丢内容：日志里必须有一条可行动的 warn',
        )

    async def test_zero_does_not_switch_off_direct_videos(self) -> None:
        """`max_videos=0` 管的是"**转发里**的视频一段都不读"（那一侧连坐标都不收）。

        私聊直发的视频不该被一个转发键关掉 —— 所以预算的下限是 1（同一把闸不许两处判）。
        """
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(0))
        result = await self._collect(host, self._session('https://cdn.example.com/v1.mp4'), ffmpeg)
        self.assertEqual(len(ffmpeg.argvs('frames')), 1)
        self.assertTrue(result.image_sources)

    def test_the_budget_is_the_forward_key_and_nothing_else(self) -> None:
        """判据一处：一回合读几段就是 `forward_read_limits(...).max_videos`（含夹取）。"""
        def budget(forward: dict[str, Any]) -> int:
            config = video_config()
            config['forward_message'] = forward
            return video.video_read_budget(Host(config=config))

        self.assertEqual(budget({'max_videos': 3}), 3)
        self.assertEqual(budget({'maxVideos': 4}), 4, '两种拼写都认')
        self.assertEqual(budget({}), 1, '缺键 = 默认 1')
        self.assertEqual(budget({'max_videos': 99}), 99, 'v1.9.7 起没有上界（夹取只住在 forward_message）')
        self.assertEqual(budget({'max_videos': -5}), 1, '夹到下限 0 之后按 1 算')
        self.assertEqual(budget({'max_videos': 'abc'}), 1, '脏值回默认')
        self.assertEqual(budget({'max_videos': 0}), 1, '0 = 转发里不读；直发仍按 1')

    def test_the_section_names_match_the_adapter_and_the_service(self) -> None:
        """段名兜底顺序三处同一套（否则"配置页写 A、行为读 B"，坑 41 / 65 同类）。"""
        from plugin.core.service import chunk3 as chunk3_module  # noqa: PLC0415

        expected = video._FORWARD_SECTION_NAMES
        self.assertEqual(chunk3_module._FORWARD_SECTION_NAMES, expected)
        bridge = _read('adapters/astrbot_bridge.py')
        self.assertIn('FORWARD_SECTION_NAMES: tuple[str, ...] = %r' % (expected,), bridge)


# =========================================================================== #
# 4.5.5d flaky 收口（v1.9.7）：/tmp 隔离 + 并发闸不变量（§84）
# =========================================================================== #

class TempDirIsolationTests(VideoTestCase):
    """两条历史 flaky 的**根因取证**与守卫。

    `test_every_video_in_the_turn_is_really_read` 的「2 != 3」与
    `test_three_forwarded_videos_really_take_three_ffmpeg_runs` 的「残留
    `hdsi-video-case-*`」单独跑都绿、全量偶红。查完的结论是**环境耦合**，不是产品缺陷：
    两条都只是把"这一回合没留垃圾 / 三段都读了"钉在**进程外共享状态**上——前者钉在
    `/tmp` 的全局差集、后者钉在进程级并发闸 `_VIDEO_IN_FLIGHT`。这里的用例把两件事
    各自隔离成**只跟本用例有关**的判据，并留下反向证据。
    """

    async def _collect(self, host: Any, session: Any, ffmpeg: FakeFfmpeg) -> video.VideoMedia:
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            return await video.collect_video_sources(host, {'id': 's'}, session)

    async def test_a_foreign_temp_dir_does_not_pollute_the_leftover_check(self) -> None:
        """**反向（旧写法）**：`/tmp` 是共享的，别的进程留下的 `hdsi-video-*` 不许算到我们头上。

        旧写法是"setUp 拍一张 `/tmp` 快照、测试后再取差集"，于是任何外来目录（并发的
        另一次测试运行、上一次崩掉/被杀掉的运行留下的 `hdsi-video-case-*`）都会让
        「这一回合没留垃圾」这条断言红 —— 那正是"残留临时目录"那条偶发的真身。
        改回旧写法 → 这条立刻红。
        """
        foreign = os.path.join(tempfile.gettempdir(), 'hdsi-video-foreign-0')
        os.makedirs(foreign, exist_ok=True)
        self.addCleanup(shutil.rmtree, foreign, True)

        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())
        result = await self._collect(host, video_session(), ffmpeg)
        result.cleanup()

        self.assertEqual(len(ffmpeg.argvs('frames')), 1)
        self.assertEqual(self.new_temp_dirs(), [], '外来目录不是我们的垃圾')

    async def test_a_single_refused_slot_costs_a_whole_video(self) -> None:
        """根因取证：并发闸被占一次 → 这一回合**少读一整段视频**（"配 3 段只读 2 段"）。

        `_VIDEO_IN_FLIGHT` 是**进程级**的（`VIDEO_MAX_CONCURRENCY=1`）。顺序 await 的
        单回合不会自己撞自己，所以历史 flake 的「2 != 3」只可能来自"同一个事件循环里
        另有一条收集在跑"。这不是产品缺陷（那道闸是故意的、也有可见 warn），但必须
        让它在测试里**可见**：`video_slot_guard()` 就是那条不变量。
        """
        ffmpeg = FakeFfmpeg()
        config = video_config()
        config['forward_message'] = {'max_videos': 3}
        host = Host(config=config)
        session = SessionView(
            platform='onebot', self_id='1', user_id='2', is_direct=True,
            content=' '.join('<video src="https://cdn.example.com/v%d.mp4"/>' % index for index in (1, 2, 3)),
            elements=[
                {'type': 'video', 'attrs': {'src': 'https://cdn.example.com/v%d.mp4' % index}, 'children': []}
                for index in (1, 2, 3)
            ],
            media=[],
        )
        calls = {'n': 0}
        original = video._acquire_slot

        def refuse_the_third() -> bool:
            calls['n'] += 1
            if calls['n'] == 3:
                return False
            return original()

        with mock.patch.object(video, '_acquire_slot', refuse_the_third):
            result = await self._collect(host, session, ffmpeg)

        self.assertEqual(len(ffmpeg.argvs('frames')), 2, '第三段被闸掉 = 只读了两段（就是那个 2 != 3）')
        self.assertIn(video.VIDEO_BUSY_REASON, result.note, '丢了一段必须说出来')

    async def test_the_slot_guard_flags_contention_instead_of_letting_counts_drift(self) -> None:
        """守卫有牙：本回合真被闸拒一次时，`video_slot_guard()` 必须红（不是静默少读）。"""
        ffmpeg = FakeFfmpeg()
        host = Host(config=video_config())
        leaked = video._acquire_slot()  # 模拟另一条收集占着闸
        try:
            with self.assertRaises(AssertionError) as ctx:
                with self.video_slot_guard():
                    await self._collect(host, video_session(), ffmpeg)
            self.assertIn('并发闸拒了', str(ctx.exception))
        finally:
            if leaked:
                video._release_slot()

    def test_the_leftover_check_no_longer_scans_the_shared_temp_root(self) -> None:
        """判据一处：清扫/判"留没留垃圾"只认本进程建过的目录，不再 `listdir` 整个 `/tmp`。"""
        self.assertEqual(self.new_temp_dirs(), [])
        created = tempfile.mkdtemp(prefix='hdsi-video-case-')
        self.assertEqual([created], self.new_temp_dirs(), '自己建的必须算进"留在盘上的"')
        shutil.rmtree(created, ignore_errors=True)
        self.assertEqual(self.new_temp_dirs(), [])


# =========================================================================== #
# 4.5.5c 私聊里的转发视频不受**群聊开关**管（v1.9.4）
# =========================================================================== #

class PrivateForwardVideoTests(VideoTestCase):
    """合并转发与群聊**没有关系**：私聊里转发来的视频只看总开关。

    现场：`group_enabled`（群聊视频理解）默认关，而私聊转发来的视频走的是**同一个**
    `collect_video_sources` —— 只要那一道闸按会话判（`is_direct`）而不是按"视频从哪来"，
    私聊就不该被它挡住。这里的用例把两个方向都钉住。
    """

    def _config(self, **video_keys: Any) -> dict[str, Any]:
        config = video_config()
        config['model']['video'].update(video_keys)
        return config

    def _media_session(self, source: str, *, is_direct: bool) -> SessionView:
        """一段**转发来的**视频：坐标在媒体表里（`kind='video'`），不在元素里。"""
        return SessionView(
            platform='onebot', self_id='1', user_id='2', is_direct=is_direct,
            content='[视频]',
            elements=[],
            media=[{'kind': 'video', 'source': source, 'summary': ''}],
        )

    async def _collect(self, host: Host, session: SessionView, ffmpeg: FakeFfmpeg) -> Any:
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            return await video.collect_video_sources(host, {'id': 's'}, session)

    async def test_a_private_forward_is_not_gated_by_the_group_switch(self) -> None:
        """**反向**：私聊 + 转发视频 + 群聊开关关着（默认）→ 照常抽帧。

        变异保护：把群闸写成"看视频从哪来"而不是"看会话是不是群"（或者干脆恒真），
        这一条立刻红。
        """
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(group_enabled=False))
        result = await self._collect(
            host, self._media_session('https://cdn.example.com/f.mp4', is_direct=True),
            ffmpeg,
        )
        self.assertEqual(len(ffmpeg.argvs('frames')), 1, '私聊只受总开关管')
        self.assertTrue(result.image_sources)
        self.assertTrue(result.audio_sources)

    async def test_a_group_forward_is_gated_by_the_group_switch(self) -> None:
        """同一段视频换到群会话：群开关关着 → 一个 ffmpeg 都不发（闸门真的在管）。"""
        ffmpeg = FakeFfmpeg()
        host = Host(config=self._config(group_enabled=False))
        result = await self._collect(
            host, self._media_session('https://cdn.example.com/f.mp4', is_direct=False),
            ffmpeg,
        )
        self.assertEqual(ffmpeg.calls, [])
        self.assertEqual((result.image_sources, result.audio_sources, result.note), ([], [], ''))

    def test_the_group_judgement_reads_only_the_session(self) -> None:
        """判据只认会话的 `is_direct`；拿不到这个字段时按**私聊**处理（保守那侧）。"""
        self.assertFalse(video._is_group_session(
            SessionView(platform='onebot', self_id='1', user_id='2', is_direct=True),
        ))
        self.assertTrue(video._is_group_session(
            SessionView(platform='onebot', self_id='1', user_id='2', is_direct=False),
        ))
        self.assertFalse(video._is_group_session({'platform': 'onebot', 'user_id': '2'}),
                         '"判不出来"不当成群聊（不该静默什么都不做）')


# =========================================================================== #
# 4.5.6 群聊 = 同一套设置（v1.9.2）：命令行逐项同款，画面进不去只说一次
# =========================================================================== #

class _TimeoutRecordingFfmpeg(FakeFfmpeg):
    """`FakeFfmpeg` + 记下每条命令拿到的 `timeout`。

    超时也是配置项（`timeout_seconds`），只在 `_run_ffmpeg(binary, args, timeout)` 这个
    **调用参数**里，argv 上看不见——所以"群里有没有沿用超时设置"必须有地方能断言。
    """

    def __init__(self, **fields: Any) -> None:
        super().__init__(**fields)
        self.timeouts: list[float] = []

    def __call__(self, binary: str, args: list[str], timeout: float) -> tuple[int, str, str]:
        self.timeouts.append(timeout)
        return super().__call__(binary, args, timeout)


class GroupVideoParityTests(VideoTestCase):
    """群回合沿用**同一份设置**：抽帧模式 / 帧数 / 音轨格式 / 音轨时长 / 超时逐项同款。

    用户口径（`group_enabled` 的 hint）："群里沿用上面同一套设置"。所以这里断言的
    **不是"差不多"**，而是两条 ffmpeg 命令行逐字相同（只有临时目录不同），
    外加"没人识别"那条说明同一原因只说一次。

    v1.9.9 起群与私聊的差别只剩两处：帧的**去处**（群回合由
    `chunk1.flush_group_turn` 喂进群聊图片通道）与"这条视频没有人识别要说出来"
    （`explain_skips=True`，私聊默认一个字的说明都不加）。

    变异保护：在群路径上给任何一项写死（例如群里恒用 `sequence` / 恒抽默认 3 帧 /
    恒按 60 秒截音轨 / 恒用 20 秒超时），`test_every_item_reaches_the_group_unchanged`
    必红——它与私聊跑同一份配置，逐字对比 argv 与 timeout。
    """

    async def _run(
        self, host: Host, session: SessionView, ffmpeg: FakeFfmpeg, *, group: bool,
    ) -> video.VideoMedia:
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'), \
                mock.patch.object(video, '_run_ffmpeg', side_effect=ffmpeg):
            if group:
                return await video.collect_group_video_media(host, {'id': 's'}, session)
            return await video.collect_video_sources(host, {'id': 's'}, session)

    @staticmethod
    def _argvs(ffmpeg: FakeFfmpeg, media: video.VideoMedia) -> dict[str, list[list[str]]]:
        """三条命令各自的 argv，临时目录路径换成 `<dir>`（两次运行必然不同）。"""
        def fix(argv: list[str]) -> list[str]:
            return [arg.replace(media.workdir, '<dir>') if media.workdir else arg for arg in argv]
        return {
            kind: [fix(argv) for argv in ffmpeg.argvs(kind)]
            for kind in ('probe', 'frames', 'audio')
        }

    def _config(self, **video_keys: Any) -> dict[str, Any]:
        config = video_config()
        config['model']['video'].update(video_keys)
        return config

    async def test_every_item_reaches_the_group_unchanged(self) -> None:
        """群开关开 + 平均抽帧 2 帧 + `audio_duration=unlimited`：与私聊**逐项同款**。

        一项一项钉：`frame_mode` / `frame_average_count` / `frame_interval_seconds` /
        `out_format` / `audio_duration`（`unlimited` = 没有 `-t`）/ `timeout_seconds`。
        """
        config = self._config(
            group_enabled=True, frame_mode='average', frame_average_count=2,
            frame_interval_seconds=10, out_format='ogg',
            audio_duration='unlimited', timeout_seconds=7,
        )
        private_ffmpeg = _TimeoutRecordingFfmpeg()
        private = await self._run(
            Host(config=config), video_session(is_direct=True), private_ffmpeg, group=False,
        )
        group_ffmpeg = _TimeoutRecordingFfmpeg()
        group_host = Host(config=config)
        group = await self._run(
            group_host, video_session(is_direct=False), group_ffmpeg, group=True,
        )

        # ① 命令行逐字相同：探测 / 抽帧 / 音轨三条，一条都不许走另一套。
        self.assertEqual(
            self._argvs(group_ffmpeg, group), self._argvs(private_ffmpeg, private),
            '群里的 ffmpeg 命令行必须与私聊逐项同款（只有临时目录不同）',
        )
        self.assertEqual(group_ffmpeg.timeouts, private_ffmpeg.timeouts, '超时秒数也是同一份配置')
        self.assertEqual(group_ffmpeg.timeouts, [7.0, 7.0, 7.0])

        # ② 每一项**真的**落在群那条命令行上（写死哪一项，这里就指得出是它）。
        frames = group_ffmpeg.argvs('frames')[0]
        self.assertIn('fps=%.6f' % (2 / 12), frames, '平均抽帧 2 帧 / 12 秒')
        self.assertEqual(frames[frames.index('-frames:v') + 1], '2')
        self.assertEqual(frames[frames.index('-t') + 1], str(video.VIDEO_MAX_DURATION_SECONDS))
        audio = group_ffmpeg.argvs('audio')[0]
        self.assertNotIn('-t', audio, 'unlimited = 不给 ffmpeg 传 -t')
        self.assertEqual(audio[audio.index('-f') + 1], 'ogg')
        self.assertTrue(audio[-1].endswith('audio.ogg'), audio[-1])

        # ③ 帧与音轨**两边都交出去**（v1.9.9：群回合的帧由调用方喂进群聊图片通道，
        #    所以这里与私聊同形；"帧真的进了模型"由 chunk1 那侧端到端钉着）。
        self.assertTrue(private.image_sources, '私聊照常交帧')
        self.assertEqual(
            [source.rsplit('/', 1)[-1] for source in group.image_sources],
            [source.rsplit('/', 1)[-1] for source in private.image_sources],
            '群回合交的帧与私聊同款（同一条判据抽出来的）',
        )
        self.assertTrue(group.audio_sources, '音轨走既有语音通道')

        # ④ 正文事实写实：帧交出去了，就不许再写"帧没进去"。
        self.assertIn('帧画面', group.note)
        self.assertIn('已单独抽出整段音轨', group.note)
        self.assertNotIn(video.GROUP_FRAMES_NO_CHANNEL_REASON, group.note)
        self.assertEqual(group_host.warns(), [], '抽到帧、交出去了 → 没有降级 warn')

    async def test_the_group_skip_is_explained_exactly_once_per_reason(self) -> None:
        """群里"这条视频没有人识别"**说明一次**（同故事同原因 10 分钟一条 warn）。

        正文事实照旧每回合都写（她每回合都该知道"这里有一条视频，我没看"），
        但日志里同一原因不许刷屏。
        """
        config = self._config(group_enabled=False)
        host = Host(config=config)
        first = await self._run(
            host, video_session(is_direct=False), _TimeoutRecordingFfmpeg(), group=True,
        )
        self.assertIn(video.GROUP_DISABLED_REASON, first.note, '只有视频未识别')
        self.assertEqual(len(host.warns()), 1)
        self.assertIn('群聊视频理解', host.warns()[0])
        # 同一分钟内的第二条视频：正文事实照旧，但不再刷同一条说明。
        second = await self._run(
            host, video_session(is_direct=False), _TimeoutRecordingFfmpeg(), group=True,
        )
        self.assertIn(video.GROUP_DISABLED_REASON, second.note)
        self.assertEqual(len(host.warns()), 1, '同原因只明说一次')

    async def test_the_group_skip_speaks_only_when_there_is_a_video(self) -> None:
        """**反向**：群里**没有**视频时一个字的说明都不加（零影响）。"""
        host = Host(config=self._config(group_enabled=False))
        plain = SessionView(
            platform='onebot', self_id='1', user_id='2', is_direct=False,
            content='大家好', elements=[], media=[],
        )
        result = await self._run(host, plain, _TimeoutRecordingFfmpeg(), group=True)
        self.assertEqual((result.image_sources, result.audio_sources, result.note), ([], [], ''))
        self.assertEqual(result.reason, '')
        self.assertEqual(host.warns(), [], '没有视频 = 零影响')

    async def test_the_master_switch_off_in_a_group_also_says_the_video_was_not_recognized(
        self,
    ) -> None:
        """总开关关着（群开关开着）：同一个"只有视频未识别"，但出路是**总开关**。"""
        host = Host(config=self._config(enabled=False, group_enabled=True))
        result = await self._run(
            host, video_session(is_direct=False), _TimeoutRecordingFfmpeg(), group=True,
        )
        self.assertEqual((result.image_sources, result.audio_sources), ([], []))
        self.assertIn(video.VIDEO_DISABLED_REASON, result.note)
        self.assertEqual(len(host.warns()), 1)
        self.assertIn('打开总开关', host.warns()[0])

    async def test_private_chats_stay_silent_when_the_chain_is_off(self) -> None:
        """**反向（私聊逐字不变）**：总开关关着时，私聊一个字的说明都不加、也不 warn。

        变异保护：把 `explain_skips` 的默认值写成 `True`，这一条立刻红——那是**用户可见**
        的行为变化（每回合多一句"只有视频未识别"）。
        """
        host = Host(config=self._config(enabled=False))
        result = await self._run(
            host, video_session(is_direct=True), _TimeoutRecordingFfmpeg(), group=False,
        )
        self.assertEqual(
            (result.image_sources, result.audio_sources, result.note, result.reason),
            ([], [], '', ''),
            '私聊关着时不许留任何说明',
        )
        self.assertEqual(host.warns(), [], '私聊关着时也不许 warn')

    async def test_the_group_switch_never_touches_a_private_chat(self) -> None:
        """**反向**：群聊开关关着（默认）对私聊**一个字节**都不影响——不挡、也不写说明。"""
        host = Host(config=self._config(group_enabled=False))
        result = await self._run(
            host, video_session(is_direct=True), _TimeoutRecordingFfmpeg(), group=False,
        )
        self.assertTrue(result.image_sources, '私聊只受总开关管')
        self.assertNotIn(video.GROUP_DISABLED_REASON, result.note)
        self.assertNotIn(video.VIDEO_DISABLED_REASON, result.note)
        self.assertEqual(host.warns(), [])

    def test_the_private_flush_never_goes_through_the_group_wrapper(self) -> None:
        """私聊那条路自己调 `collect_video_sources`，一个字的群语义都不碰（判据一处）。"""
        source = _read('core/service/chunk3.py')
        self.assertIn('collect_video_sources(self, snapshot[\'story\'], latest_session)', source)
        self.assertNotIn('collect_group_video_media', source)
        self.assertNotIn('explain_skips', source)

    def test_the_group_judgement_lives_in_exactly_one_place(self) -> None:
        """判据一处：模块里只有 `group_enabled` 那一道闸读会话是不是群聊。

        群里**不许**有第二处特判（"群里就不抽帧了" / "群里固定抽 3 帧"那类）：出现第二处
        `_is_group_session(...)` 调用，或者它跑到抽帧路径上去，这一条就红。
        """
        source = _read('core/video_understanding.py')
        code = _code_only(source)
        self.assertEqual(code.count('_is_group_session('), 2, '一处定义 + 一处调用')
        gate = "if _is_group_session(session) and not config['group_enabled']:"
        self.assertIn(gate, source)
        # 抽帧那一段（`extract_video` 调用点）里不许再出现群判据。
        extract = source.split('extraction = await asyncio.to_thread(', 1)[1].split(
            'result.workdir = extraction.workdir', 1,
        )[0]
        self.assertNotIn('_is_group_session', extract)
        self.assertIn("frame_mode=config['frame_mode']", extract)
        self.assertIn("average_frames=config['frame_average_count']", extract)


# =========================================================================== #
# 7. 适配层接线：指名 Provider 那一套（任务键、合成行、能力自检）
# =========================================================================== #

class AdapterWiringTests(unittest.TestCase):

    def setUp(self) -> None:
        self.bridge = _read('adapters/astrbot_bridge.py')

    def test_the_video_task_is_a_named_provider_task(self) -> None:
        # 任务键（配置页渲染成模型选择器，所以路径是 `model.video.model_id`）。
        self.assertIn("'video': ('model', 'video', 'model_id'),", self.bridge)
        # 合成行只挂 `use_for_video`：core 的 `is_assigned_to(..., 'video')` 认它。
        self.assertIn("'use_for_video': task == 'video',", self.bridge)
        # `routing_config()` 的任务表里必须有它，否则请求走不到那个 Provider。
        self.assertIn("'works', 'video')", self.bridge)

    def test_the_routing_copy_synthesises_the_binding_row_only_when_it_resolves(self) -> None:
        self.assertIn("if task == 'video' and not self.video_named_provider():", self.bridge)
        self.assertIn('def video_named_provider(self) -> str:', self.bridge)
        self.assertIn("return self.named_provider_value('video', allow_connection_row=False)", self.bridge)

    def test_the_capability_note_speaks_only_when_something_is_wrong(self) -> None:
        self.assertIn('def video_capability_note(self) -> str:', self.bridge)
        self.assertIn('self.video_capability_note(),', self.bridge)

    def test_the_side_task_route_maps_to_the_video_task(self) -> None:
        narrator = _read('core/narrator.py')
        self.assertIn("'视频理解': 'video',", narrator)

    def test_the_routing_flag_is_video_specific_not_the_vision_fallthrough(self) -> None:
        routing = _read('core/model_routing.py')
        self.assertIn("if task == 'video':", routing)
        self.assertIn("return provider.get('use_for_video') is True", routing)
        self.assertIn("'use_for_video': provider.get('use_for_video') is True,", routing)


# =========================================================================== #
# 8. 范围铁律与陈旧说法
# =========================================================================== #

class DocAndScopeTests(unittest.TestCase):

    def test_the_forward_media_comment_no_longer_claims_there_is_no_frame_extraction(self) -> None:
        """抽帧能力落地后，转发媒体那句"没有抽帧能力"必须改成准确的边界说法。"""
        source = _read('core/forward_message.py')
        self.assertNotIn('**不产生条目**（没有抽帧能力，别假装有）', source)
        self.assertIn('抽帧识别', source)

    def test_the_video_module_does_not_import_astrbot(self) -> None:
        code = _code_only(_read('core/video_understanding.py'))
        self.assertNotIn('import astrbot', code)
        self.assertNotIn('from astrbot', code)

    def test_no_new_python_dependency_is_introduced(self) -> None:
        source = _read('core/video_understanding.py')
        for module in ('import shutil', 'import subprocess', 'import tempfile'):
            self.assertIn(module, source)
        self.assertNotIn('ffmpeg-python', source)
        self.assertNotIn('import ffmpeg', source)

    def test_the_module_documents_the_three_modes_and_the_budget(self) -> None:
        source = _read('core/video_understanding.py')
        for needle in ('抽帧识别', '原生识别', '外挂识别', 'VIDEO_FRAME_INTERVAL_SECONDS',
                       'VIDEO_MAX_FRAMES', 'VIDEO_MAX_DURATION_SECONDS', 'VIDEO_MAX_CONCURRENCY'):
            self.assertIn(needle, source)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
