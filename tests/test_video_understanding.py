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

独立运行：
    cd <仓库根目录>
    python3 -m unittest plugin.tests.test_video_understanding -v
"""

from __future__ import annotations

import base64
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
        timeout_audio: bool = False,
        audio_bytes: int = 0,
    ) -> None:
        self.duration = duration
        self.has_audio = has_audio
        self.frames_written = frames_written
        self.fail_frames = fail_frames
        self.fail_audio = fail_audio
        self.timeout_frames = timeout_frames
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
    """一份"模型中心"配置：视频那三个键 + 两条既有通道各自的开关。"""
    return {
        'model': {
            'vision': {'enabled': True, 'mode': 'native', 'detail': 'auto', 'max_image_dimension': 0},
            'audio': {
                'enabled': True, 'out_format': 'mp3', 'max_file_size_mb': 10, 'max_per_message': 1,
            },
            'video': {'enabled': enabled, 'mode': mode, 'model_id': model_id},
        },
    }


def video_session(target: str = 'https://cdn.example.com/v.mp4') -> SessionView:
    """一条带视频的入站事件（适配器直给元素 = 可信坐标）。"""
    return SessionView(
        platform='onebot', self_id='1', user_id='2',
        content='看这个<video src="%s"/>' % target,
        elements=[{'type': 'video', 'attrs': {'src': target}, 'children': []}],
        media=[],
    )


class FakeNarrator:
    """`narrator._side_task_json` 的替身：记下每次调用，可设成失败。"""

    def __init__(self, answer: str = '画面里是一只猫', error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def _side_task_json(
        self, provider: Any, model: str, task: str, timeout: Any,
        build_body: Any, parse: Any, *rest: Any, **kwargs: Any,
    ) -> Any:
        body = build_body(True)
        self.calls.append({'task': task, 'model': model, 'provider': provider, 'body': body})
        if self.error is not None:
            raise self.error
        return parse(self.answer)


#: 一段本地视频文件的内容（字节数只用于体积预算）。
_FAKE_VIDEO_BYTES = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 4096


class VideoTestCase(unittest.IsolatedAsyncioTestCase):
    """视频用例的公共底座：模块状态归零 + **临时目录清扫**。

    抽帧会在 `tempfile.gettempdir()` 下建 `hdsi-video-*`；生产里由调用方在读完帧字节后
    删掉（`VideoMedia.cleanup()`），用例里则统一在这里扫尾——顺带让"这一回合有没有留垃圾"
    这条断言（`TurnLevelWiringTests`）有干净的基准。
    """

    def setUp(self) -> None:
        video.reset_ffmpeg_probe()
        video.reset_video_runtime_state()
        self._tmp_before = set(os.listdir(tempfile.gettempdir()))

    def tearDown(self) -> None:
        video.reset_ffmpeg_probe()
        video.reset_video_runtime_state()
        for name in set(os.listdir(tempfile.gettempdir())) - self._tmp_before:
            if name.startswith('hdsi-video-'):
                shutil.rmtree(os.path.join(tempfile.gettempdir(), name), ignore_errors=True)

    def new_temp_dirs(self) -> list[str]:
        return sorted(
            name for name in set(os.listdir(tempfile.gettempdir())) - self._tmp_before
            if name.startswith('hdsi-video-')
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
            platform='onebot', self_id='1', user_id='2',
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
        # 位置：`vision` / `audio` / `providers` 必须先出现（宿主配置页的既有顺序断言
        # 钉着前三个），视频组紧随其后。
        self.assertEqual(keys[:4], ['vision', 'audio', 'providers', 'video'])
        self.assertEqual(self.video['description'], '视频理解设置')

    def test_defaults_are_the_cost_saving_side(self) -> None:
        items = self.video['items']
        self.assertIs(items['enabled']['default'], False)
        self.assertEqual(items['mode']['default'], 'frames')
        self.assertEqual(items['model_id']['default'], '')
        self.assertEqual(items['mode']['options'], ['frames', 'native', 'external'])
        self.assertEqual(items['mode']['default'], video.VIDEO_DEFAULT_MODE)

    def test_core_defaults_match_the_schema(self) -> None:
        from plugin.core.service.config import CONFIG_DEFAULTS  # noqa: PLC0415

        self.assertEqual(CONFIG_DEFAULTS['model']['video'], {
            'enabled': False, 'mode': 'frames', 'model_id': '',
        })
        self.assertEqual(
            CONFIG_DEFAULTS['model']['video'],
            {key: value['default'] for key, value in self.video['items'].items()},
        )

    def test_the_model_key_is_a_named_provider_picker(self) -> None:
        node = self.video['items']['model_id']
        self.assertEqual(node['_special'], 'select_provider')
        self.assertIn('外挂', node['description'])
        # 只在 external 时生效 + 找不到不回落，两句都要写清楚。
        self.assertIn('外挂识别', node['hint'])
        self.assertIn('不回落', node['hint'])

    def test_the_mode_hint_names_ffmpeg_and_the_budgets(self) -> None:
        hint = self.video['items']['mode']['hint']
        self.assertIn('FFmpeg', hint)
        self.assertIn('ffmpeg -version', hint)
        self.assertIn(str(video.VIDEO_MAX_FRAMES), hint)
        self.assertIn(str(video.VIDEO_AUDIO_CLIP_SECONDS), hint)
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
        for key in ('enabled', 'mode', 'model_id'):
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
        for key in ('video', 'video.enabled', 'video.mode', 'video.model_id'):
            self.assertIn('`model_center.%s`' % key, config_map)


class VideoConfigIoTests(unittest.TestCase):
    """导出 / 导入是**跨版本用户资产**：新键必须能原样带走、缺键补默认、双拼写都认。"""

    def test_export_import_carries_the_video_group(self) -> None:
        from plugin.core.service.config import normalize_config, to_schema_shape  # noqa: PLC0415

        config = normalize_config({'model': {'video': {
            'enabled': True, 'mode': 'external', 'model_id': 'vlm',
        }}})
        self.assertEqual(config['model']['video'],
                         {'enabled': True, 'mode': 'external', 'model_id': 'vlm'})
        # 落盘要转 **schema 分组名**（`model_center`），否则宿主配置页显示"全是默认值"。
        self.assertEqual(to_schema_shape(config)['model_center']['video'],
                         {'enabled': True, 'mode': 'external', 'model_id': 'vlm'})

    def test_missing_keys_are_filled_with_the_cost_saving_defaults(self) -> None:
        from plugin.core.service.config import normalize_config  # noqa: PLC0415

        self.assertEqual(normalize_config({})['model']['video'],
                         {'enabled': False, 'mode': 'frames', 'model_id': ''})

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
        with mock.patch.object(video, '_FFMPEG_PATH', '/usr/bin/ffmpeg'):
            self.assertEqual(video.ffmpeg_status_label(), 'FFmpeg 已识别')
            self.assertTrue(video.ffmpeg_available())
        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            self.assertEqual(video.ffmpeg_status_label(), '未检查到 FFmpeg')
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
        self.assertEqual(label, 'FFmpeg 已识别')
        dynamic = schema['model_center']['items']['video']['items']['mode']['hint']
        self.assertTrue(dynamic.startswith('FFmpeg 已识别'))
        self.assertIn('ffmpeg -version', dynamic)
        # 只改了**内存里的**这一份；仓库里的文件一字未动（用户明确要求）。
        with open(SCHEMA_PATH, encoding='utf-8-sig') as handle:
            self.assertEqual(json.load(handle)['model_center']['items']['video']['items']['mode']['hint'],
                             static_hint)

    def test_the_hint_patch_is_honest_when_ffmpeg_is_missing(self) -> None:
        schema = json.loads(json.dumps(_schema_video()))
        with mock.patch.object(video, '_FFMPEG_PATH', ''):
            video.apply_ffmpeg_status_hint(schema)
        hint = schema['model_center']['items']['video']['items']['mode']['hint']
        self.assertTrue(hint.startswith('未检查到 FFmpeg'))
        self.assertIn('降级', hint)
        self.assertIn('装上 FFmpeg', hint)

    def test_a_missing_schema_node_never_raises(self) -> None:
        for schema in (None, {}, {'model_center': {}}, {'model_center': {'items': {'video': None}}}):
            with self.subTest(schema=schema):
                with mock.patch.object(video, '_FFMPEG_PATH', ''):
                    self.assertEqual(video.apply_ffmpeg_status_hint(schema), '未检查到 FFmpeg')

    def test_the_plugin_applies_the_patch_at_load_and_logs_one_line(self) -> None:
        source = _read('main.py')
        self.assertIn('apply_ffmpeg_status_hint(', source)
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
        # 音轨：单独一条命令、16k 单声道 wav、只取前 N 秒。
        self.assertEqual(audio[0][audio[0].index('-t') + 1], str(video.VIDEO_AUDIO_CLIP_SECONDS))
        self.assertEqual(audio[0][audio[0].index('-ar') + 1], str(video.VIDEO_AUDIO_SAMPLE_RATE))
        self.assertEqual(audio[0][audio[0].index('-ac') + 1], '1')
        self.assertEqual(audio[0][-1].split('.')[-1], 'wav')
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
        self.assertTrue(result.audio_sources[0].startswith('data:audio/wav;base64,'))
        audio = await ServiceChunk3.load_native_audio(
            host, {'id': 's'}, result.audio_sources, None,
        )
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]['format'], 'wav')
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
        self.assertTrue(host.seen['audio'][0].startswith('data:audio/wav;base64,'))

        # ② 进叙事的形态：帧是原生视觉图片、音轨是原生音频附件。
        images = host.calls['try_decide'][9]
        audio = host.calls['try_decide'][10]
        self.assertEqual(len(images), video.VIDEO_MAX_FRAMES)
        for image in images:
            self.assertEqual(image['mime_type'], 'image/png')
            self.assertNotIn('path', image, '交给模型的是字节，不是本地路径')
        self.assertEqual([item['format'] for item in audio], ['wav'])

        # ③ 正文里那句事实（含可数线索）并进了本回合的用户消息。
        user_message = host.calls['try_decide'][5]
        self.assertIn(video.VIDEO_FACT_PREFIX, user_message)
        self.assertIn('%d 帧画面' % video.VIDEO_MAX_FRAMES, user_message)

        # ④ 这一次的临时目录在图像通道读完字节后就被删掉（不留垃圾）。
        self.assertEqual(self.new_temp_dirs(), [], self.new_temp_dirs())

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
        """上限不是装饰：`-frames:v` / `fps=1/4` / `-t` 都由常量算出并写进命令行。"""
        source = _read('core/video_understanding.py')
        self.assertIn("'-frames:v', str(max(1, int(max_frames)))", source)
        self.assertIn("'fps=1/%d' % max(1, int(interval_seconds))", source)
        self.assertIn("'-t', str(max_duration_seconds)", source)
        self.assertIn('frames = sorted(', source)
        self.assertIn(')[:max(1, int(max_frames))]', source)
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
        merge_image = 'image_sources = _unique(list(image_sources) + list(video_media.image_sources))'
        merge_audio = '] + list(video_media.audio_sources))'
        self.assertIn(merge_image, body)
        self.assertIn(merge_audio, body)
        # 顺序：先并进来源表，再交给那两条通道（并反了就白搭）。
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
