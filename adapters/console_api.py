"""插件控制台（WebUI 插件页面）的后端。

**为什么单独一个模块**：`main.py` 只负责注册路由与转发，真正的取数逻辑放这里，
这样命令表那边（32 条）与页面这边互不干扰，测试也能直接对着数据形状写。

放在 `adapters/` 而不是 `core/`：它要读 AstrBot 的 Provider、要拿 `Bridge` 的数据
目录，属于宿主侧代码；`core/` 依旧一行 astrbot 都不 import。

设计约束：

* 只读。除了配置导入导出（复用 `Bridge.import_config`），这里不改任何状态。
* 每个接口都要能在**服务没起来 / 数据库没建表 / 没配模型**时正常返回一个空壳，
  而不是抛异常——控制台要能打开，并在界面上说明"哪里还没配好"。
* 不返回任何密钥。连接行的 `api_key` 只以「有没有填」的形式出现。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ..core.database import TABLES
from ..core.meta import HDS_INTERLUDE_VERSION
from .astrbot_bridge import (
    CONSOLE_LOG_BUFFER as CONSOLE_LOG_MAX,
    CONSOLE_USAGE_BUFFER as CONSOLE_USAGE_MAX,
    PLUGIN_NAME,
    _plugin_version,
)

__all__ = ['ConsoleApi', 'ConsoleError', 'CONSOLE_TASKS', 'mask_endpoint']

#: 控制台「模型」页展示的任务顺序与中文名（与 `model_routing` 的任务键一致）。
CONSOLE_TASKS: tuple[tuple[str, str], ...] = (
    ('main', '主叙事'),
    ('compaction', '压缩与总结'),
    ('timeline', '时间导演'),
    ('alter', 'Alter 分析'),
    ('embedding', 'Embedding'),
    ('stickers', '表情包描述'),
    ('vision', '侧端识图'),
)

#: 任务键 → 配置里对应的「指名模型」项（见 `AstrbotBridge.TASK_MODEL_PATHS`）。
TASK_LABELS = dict(CONSOLE_TASKS)


def mask_endpoint(value: Any) -> str:
    """endpoint 只显示 `主机/路径`，不显示查询串。

    有些网关把 key 或签名放在 query 里（`?api_key=...`），控制台是给人看的页面，
    没必要把它送进浏览器。
    """
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(text)
        if not parts.netloc:
            return text
        return f'{parts.scheme}://{parts.netloc}{parts.path}'
    except Exception:  # noqa: BLE001 - 解析失败就原样返回（它本来也不是合法 URL）
        return text


class ConsoleError(Exception):
    """控制台能直接展示给用户的错误（比如"地址格式不对"），映射成 400。"""


def _float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ('' if value is None else str(value))


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _safe_count(database: Any, table: str, where: Optional[dict[str, Any]] = None) -> int:
    """计数失败返回 0（表可能还没建、或旧库缺列）。"""
    try:
        return int(database.count(table, where) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _safe_all(
    database: Any,
    table: str,
    where: Optional[dict[str, Any]] = None,
    order: Any = None,
    limit: Optional[int] = None,
) -> list[Any]:
    try:
        return list(database.all(table, where, order, limit) or [])
    except Exception:  # noqa: BLE001
        return []


class ConsoleApi:
    """控制台的数据来源。所有方法都是协程或纯同步读，返回可 JSON 序列化的 dict。"""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    # ------------------------------------------------------------------ #
    # 总览
    # ------------------------------------------------------------------ #

    async def overview(self, story_id: str = '') -> dict[str, Any]:
        database = self.bridge.db
        stories = _safe_all(database, 'interlude_story', order='updatedAt DESC', limit=50)
        current = self._pick_story(stories, story_id)

        counts = {name: _safe_count(database, name) for name in TABLES}
        participants = 0
        entries = 0
        if current:
            participants = _safe_count(database, 'interlude_participant', {'storyId': current.get('id')})
            entries = _safe_count(database, 'interlude_script_entry', {'storyId': current.get('id')})

        return {
            'plugin': {
                'name': PLUGIN_NAME,
                'display_name': 'HDS Interlude',
                'version': _plugin_version(),
                'upstream_version': HDS_INTERLUDE_VERSION,
                'data_dir': _text(getattr(self.bridge, 'data_dir', '')),
                'config_path': _text(self.bridge.config_file_path()),
            },
            'service': {
                'started': bool(getattr(self.bridge, '_started', False)),
                'blind_mode': bool(getattr(self.bridge, 'blind_mode_enabled', False)),
                'story_count': len(stories),
                'participant_count': participants,
                'entry_count': entries,
            },
            'story': self._story_brief(current) if current else None,
            'stories': [self._story_brief(item) for item in stories],
            'flags': self._flags(),
            'routing': self._routing_rows(),
            'capability': {
                'image': self._note('image'),
                'audio': self._note('audio'),
            },
            'counts': counts,
        }

    # ------------------------------------------------------------------ #
    # 模型
    # ------------------------------------------------------------------ #

    async def models(self, story_id: str = '') -> dict[str, Any]:
        del story_id  # 模型配置与具体故事无关，保留参数是为了接口形状统一
        section = self.bridge.section('model')
        connections = []
        for raw in section.get('providers') or []:
            if not isinstance(raw, dict):
                continue
            connections.append({
                'label': _text(raw.get('label')),
                'enabled': raw.get('enabled') is not False,
                'mode': _text(raw.get('mode')) or 'openai-compatible',
                'endpoint': mask_endpoint(raw.get('endpoint')),
                'has_endpoint': bool(_text(raw.get('endpoint')).strip()),
                'model': _text(raw.get('model')),
                'has_key': bool(_text(raw.get('api_key')).strip()),
                'tasks': [TASK_LABELS[key] for key, _ in CONSOLE_TASKS if raw.get(f'use_for_{key}') is True],
                'prices': {
                    'input': raw.get('price_input') or 0,
                    'output': raw.get('price_output') or 0,
                    'cached': raw.get('price_cached_input') or 0,
                },
                'response_format': _text(raw.get('response_format')),
                'temperature': raw.get('temperature'),
                'top_p': raw.get('top_p'),
                'max_tokens': raw.get('max_tokens'),
                'timeout': raw.get('timeout'),
            })

        failover = section.get('failover') or {}
        embedding = section.get('embedding') or {}
        vision = section.get('vision') or {}
        audio = section.get('audio') or {}

        return {
            'tasks': self._routing_rows(),
            # `audio` 不是聊天任务（它是"语音→文字"的转写模型，不参与叙事路由），
            # 所以不进 `tasks`，但页面要显示它，仍然放进 task_models。
            'task_models': {
                key: {
                    'label': label,
                    'astrbot_provider': self.bridge.task_model_id(key),
                    'modalities': sorted(self.bridge.task_provider_modalities(key)),
                }
                for key, label in (*CONSOLE_TASKS, ('audio', '语音转写'))
            },
            'connections': connections,
            'astrbot_providers': self._astrbot_providers(),
            'embedding': {
                'enabled': bool(embedding.get('enabled')),
                'semantic_history': bool(embedding.get('semantic_history')),
                'live_query': bool(embedding.get('live_query')),
                'endpoint': mask_endpoint(embedding.get('endpoint')),
                'dimensions': _int(embedding.get('dimensions')),
                'provider_id': self.bridge.task_model_id('embedding'),
            },
            'vision': {
                'enabled': bool(vision.get('enabled')),
                'mode': _text(vision.get('mode')) or 'native',
                'detail': _text(vision.get('detail')) or 'auto',
                'max_image_dimension': _int(vision.get('max_image_dimension')),
                'provider_id': self.bridge.task_model_id('vision'),
            },
            'audio': {
                'enabled': bool(audio.get('enabled')),
                'out_format': _text(audio.get('out_format')) or 'mp3',
                'max_file_size_mb': _int(audio.get('max_file_size_mb')),
                'provider_id': self.bridge.task_model_id('audio'),
            },
            'failover': {
                'enabled': failover.get('enabled') is not False,
                'strategy': _text(failover.get('strategy')) or 'priority',
                'max_attempts': _int(failover.get('max_attempts_per_provider'), 1),
                'cooldown_minutes': _int(failover.get('cooldown_minutes')),
            },
            'main': {
                'provider_label': self.bridge.main_provider_label(),
                'temperature': section.get('main_temperature'),
                'top_p': section.get('main_top_p'),
                'max_tokens': section.get('main_max_tokens'),
                'timeout': section.get('main_timeout'),
                'response_format': _text(section.get('main_response_format')),
                'streaming_mode': _text(section.get('main_streaming_mode')) or 'off',
            },
            'capability': {'image': self._note('image'), 'audio': self._note('audio')},
            'usage': self._usage(),
        }

    # ------------------------------------------------------------------ #
    # 剧本 / 记忆 / 数据库 / 日志
    # ------------------------------------------------------------------ #

    async def script(self, story_id: str = '', limit: int = 60, offset: int = 0) -> dict[str, Any]:
        database = self.bridge.db
        stories = _safe_all(database, 'interlude_story', order='updatedAt DESC', limit=50)
        current = self._pick_story(stories, story_id)
        if not current:
            return {'story': None, 'entries': [], 'total': 0, 'scenes': [], 'arcs': []}
        sid = _text(current.get('id'))
        size = max(1, min(200, _int(limit, 60)))
        skip = max(0, _int(offset, 0))
        total = _safe_count(database, 'interlude_script_entry', {'storyId': sid})
        rows = _safe_all(
            database, 'interlude_script_entry', {'storyId': sid}, 'occurredAt DESC', skip + size,
        )
        rows = rows[skip:]
        return {
            'story': self._story_brief(current),
            'total': total,
            'offset': skip,
            'limit': size,
            'entries': [self._entry_brief(row) for row in rows],
            'scenes': [
                {
                    'id': item.get('id'),
                    'status': _text(item.get('status')),
                    'hook': _text(item.get('hook')),
                    'summary': _text(item.get('summary')),
                    'started_at': _text(item.get('startedAt')),
                    'ended_at': _text(item.get('endedAt')),
                    'entry_count': _int(item.get('entryCount')),
                }
                for item in _safe_all(database, 'interlude_scene', {'storyId': sid}, 'startedAt DESC', 20)
            ],
            'arcs': [
                {
                    'id': item.get('id'),
                    'status': _text(item.get('status')),
                    'title': _text(item.get('title')),
                    'summary': _text(item.get('summary')),
                    'scene_count': _int(item.get('sceneCount')),
                }
                for item in _safe_all(database, 'interlude_arc', {'storyId': sid}, 'updatedAt DESC', 20)
            ],
        }

    async def memory(self, story_id: str = '') -> dict[str, Any]:
        database = self.bridge.db
        stories = _safe_all(database, 'interlude_story', order='updatedAt DESC', limit=50)
        current = self._pick_story(stories, story_id)
        if not current:
            return {
                'story': None, 'facts': [], 'memories': [], 'intents': [],
                'patches': [], 'overlays': [], 'participants': [],
            }
        sid = _text(current.get('id'))
        return {
            'story': self._story_brief(current),
            'facts': [self._fact_brief(row) for row in _safe_all(
                database, 'interlude_fact', {'storyId': sid}, 'importance DESC', 80,
            )],
            'memories': [self._memory_brief(row) for row in _safe_all(
                database, 'interlude_memory', {'storyId': sid}, 'importance DESC', 60,
            )],
            'intents': [self._intent_brief(row) for row in _safe_all(
                database, 'interlude_intent', {'storyId': sid}, 'notBefore DESC', 40,
            )],
            'patches': [self._patch_brief(row) for row in _safe_all(
                database, 'interlude_state_patch', {'storyId': sid}, 'createdAt DESC', 40,
            )],
            'overlays': [self._overlay_brief(row) for row in _safe_all(
                database, 'interlude_overlay_snapshot', {'storyId': sid}, 'periodEnd DESC', 20,
            )],
            'participants': [self._participant_brief(row) for row in _safe_all(
                database, 'interlude_participant', {'storyId': sid}, 'updatedAt DESC', 40,
            )],
        }

    async def database(self) -> dict[str, Any]:
        database = self.bridge.db
        tables = []
        for name, spec in TABLES.items():
            tables.append({
                'name': name,
                'columns': len(spec.fields),
                'rows': _safe_count(database, name),
                'added_later': bool(getattr(spec, 'added_later', False)),
            })
        path = ''
        try:
            path = _text(getattr(database, 'path', ''))
        except Exception:  # noqa: BLE001
            path = ''
        size = 0
        if path and os.path.isfile(path):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        return {
            'tables': sorted(tables, key=lambda item: item['rows'], reverse=True),
            'total_rows': sum(item['rows'] for item in tables),
            'path': path,
            'size_bytes': size,
        }

    async def logs(self, limit: int = 200, level: str = '') -> dict[str, Any]:
        size = max(1, min(CONSOLE_LOG_MAX, _int(limit, 200)))
        wanted = _text(level).strip().lower()
        records = list(getattr(self.bridge, 'log_buffer', []) or [])
        if wanted:
            records = [item for item in records if _text(item.get('level')) == wanted]
        # 最新的在前，方便页面直接渲染
        return {
            'records': list(reversed(records))[:size],
            'total': len(records),
            'capacity': CONSOLE_LOG_MAX,
        }

    # ------------------------------------------------------------------ #
    # 写操作（控制台里唯一会改状态的两处）
    # ------------------------------------------------------------------ #

    #: 控制台允许切换的开关：`键` → `(分组, 配置键, 中文名)`。
    #: **白名单**，不接受任意路径——控制台不是配置编辑器，别把整个 schema 暴露成写接口。
    FLAG_KEYS: dict[str, tuple[str, str, str]] = {
        'allow_proactive_messages': ('runtime', 'allow_proactive_messages', '主动可见消息'),
        'agency': ('agency', 'enabled', 'Agency 行动窗口'),
        'urge': ('urge', 'enabled', 'Urge 弹性推进'),
        'schedule_preplan': ('schedule_preplan', 'enabled', '日程预排'),
        'timeline_director': ('timeline_director', 'enabled', '时间导演'),
        'compaction': ('compaction', 'enabled', '后台压缩'),
        'embedding': ('embedding', 'enabled', '语义检索'),
        'vision': ('vision', 'enabled', '图片理解'),
        'audio': ('audio', 'enabled', '语音理解'),
        'browser': ('browser', 'enabled', '网页观察'),
    }

    async def set_flag(self, name: str, value: Any) -> dict[str, Any]:
        """切换一个运行开关。返回落盘后的完整开关表，前端直接替换本地状态。"""
        spec = self.FLAG_KEYS.get(_text(name))
        if not spec:
            raise ConsoleError('不认识的开关：%s' % name)
        section, key, label = spec
        target = dict(self.bridge.raw_config())
        group = target.get(section)
        group = dict(group) if isinstance(group, dict) else {}
        group[key] = bool(value)
        target[section] = group
        # `compaction` / `embedding` / `vision` / `audio` 在 schema 里嵌在 `model_center` 下，
        # 而 core 读的是内部名 `model`；两处都要写，否则配置页看到的是默认值。
        if section in ('compaction', 'embedding', 'vision', 'audio'):
            model = target.get('model_center')
            model = dict(model) if isinstance(model, dict) else {}
            nested = dict(model.get(section)) if isinstance(model.get(section), dict) else {}
            nested[key] = bool(value)
            model[section] = nested
            target['model_center'] = model
        saved_via = await self.bridge.save_raw_config(target)
        self._reload()
        return {
            'flags': self._flags(),
            'saved_via': saved_via,
            'changed': f'{label} → {"开启" if value else "关闭"}',
        }

    #: 连接行允许前端写的字段与类型（其余字段保留原值，杜绝越权写）。
    CONNECTION_FIELDS: dict[str, str] = {
        'label': 'str', 'enabled': 'bool', 'mode': 'str', 'endpoint': 'str',
        'model': 'str', 'response_format': 'str',
        'temperature': 'float', 'top_p': 'float', 'max_tokens': 'int', 'timeout': 'int',
        'extra_headers': 'str', 'extra_body': 'str',
        'reasoning_effort': 'str', 'deepseek_thinking': 'str', 'deepseek_reasoning_effort': 'str',
        'dashscope_region': 'str',
        'price_input': 'float', 'price_output': 'float', 'price_cached_input': 'float',
    }

    #: 「用途」勾选（`use_for_*`）单独处理：它们决定这条连接参与哪些任务的路由。
    TASK_FLAGS: tuple[str, ...] = (
        'use_for_main', 'use_for_compaction', 'use_for_alter',
        'use_for_embedding', 'use_for_stickers', 'use_for_vision',
    )

    async def save_connection(self, payload: Any) -> dict[str, Any]:
        """新增 / 修改一条模型连接。

        `index` 为空表示新增。`api_key` 特殊约定：
        - 字段**不传**或传空串 → **保留原值**（前端拿不到现有密钥，也不该拿到）；
        - 传 `null` → 显式清空；
        - 传非空字符串 → 覆盖。
        """
        if not isinstance(payload, dict):
            raise ConsoleError('请求体必须是对象')
        target = dict(self.bridge.raw_config())
        model = target.get('model_center')
        model = dict(model) if isinstance(model, dict) else {}
        rows = model.get('providers')
        rows = [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []

        raw_index = payload.get('index', '')
        editing = str(raw_index).strip() != ''
        index = _int(raw_index, -1) if editing else -1
        if editing and not (0 <= index < len(rows)):
            raise ConsoleError('要修改的连接不存在：序号 %s' % raw_index)
        row: dict[str, Any] = dict(rows[index]) if editing else {}
        if not editing:
            row.setdefault('enabled', True)
            row.setdefault('mode', 'openai-compatible')
            row.setdefault('response_format', 'json-object')

        for field, kind in self.CONNECTION_FIELDS.items():
            if field not in payload:
                continue
            value = payload[field]
            if kind == 'bool':
                row[field] = bool(value)
            elif kind == 'int':
                row[field] = _int(value, _int(row.get(field), 0))
            elif kind == 'float':
                row[field] = _float(value, _float(row.get(field), 0.0))
            else:
                row[field] = _text(value)

        for flag in self.TASK_FLAGS:
            if flag in payload:
                row[flag] = bool(payload[flag])

        if 'api_key' in payload:
            key = payload['api_key']
            if key is None:
                row['api_key'] = ''
            elif _text(key).strip():
                row['api_key'] = _text(key).strip()

        endpoint = _text(row.get('endpoint')).strip()
        if endpoint and not endpoint.lower().startswith(('http://', 'https://')):
            raise ConsoleError('地址要以 http:// 或 https:// 开头（填完整的 Chat Completions 地址）')
        row['endpoint'] = endpoint

        label = _text(row.get('label')).strip()
        if not label:
            raise ConsoleError('连接名称不能为空')

        if editing:
            rows[index] = row
        else:
            rows.append(row)
        model['providers'] = rows
        target['model_center'] = model

        saved_via = await self.bridge.save_raw_config(target)
        self._reload()
        return {
            'saved_via': saved_via,
            'index': index if editing else len(rows) - 1,
            'connections': (await self.models())['connections'],
            'changed': ('已更新连接 %s' % label) if editing else ('已新增连接 %s' % label),
        }

    async def delete_connection(self, index: Any) -> dict[str, Any]:
        """删掉一条连接。数组会被重排，所以返回新列表让前端整体替换。"""
        target = dict(self.bridge.raw_config())
        model = target.get('model_center')
        model = dict(model) if isinstance(model, dict) else {}
        rows = model.get('providers')
        rows = [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
        position = _int(index, -1)
        if not (0 <= position < len(rows)):
            raise ConsoleError('要删除的连接不存在：序号 %s' % index)
        label = _text(rows[position].get('label'))
        rows.pop(position)
        model['providers'] = rows
        target['model_center'] = model
        saved_via = await self.bridge.save_raw_config(target)
        self._reload()
        return {
            'saved_via': saved_via,
            'connections': (await self.models())['connections'],
            'changed': '已删除连接 %s' % (label or ('#%d' % position)),
        }

    def _reload(self) -> None:
        """写盘之后让内存里的服务立刻用上新配置（与 `import_config` 同一条路径）。"""
        self.bridge.reload_config()

    # ------------------------------------------------------------------ #
    # Alter / Agency / 投递账本
    # ------------------------------------------------------------------ #

    async def alter(self, story_id: str = '') -> dict[str, Any]:
        """Alter 情绪：当前氛围位移、按来源分的桶、历史曲线与冷却状态。

        数据全在 `interlude_story.state.alter_system` 里（上游 `AlterSystemState`），
        不额外建表；控制台只是把它读出来画出来。
        """
        story = self._current_story(story_id)
        if not story:
            return {'story': None, 'state': None, 'history': [], 'pending': [], 'config': self._alter_config()}
        state = story.get('state') if isinstance(story.get('state'), dict) else {}
        raw_alter = state.get('alter_system', state.get('alterSystem'))
        alter_state = raw_alter if isinstance(raw_alter, dict) else {}
        raw_offset = alter_state.get('emotional_offset', alter_state.get('emotionalOffset'))
        offset = raw_offset if isinstance(raw_offset, dict) else None
        history = []
        for item in alter_state.get('history') or []:
            if not isinstance(item, dict):
                continue
            history.append({
                'turn': _int(item.get('turn')),
                'phase': _text(item.get('phase')),
                'alter': float(item.get('alter') or 0),
                'alter_value': float(item.get('alterValue', item.get('alter_value')) or 0),
                'timestamp': _text(item.get('timestamp')),
                'participant_id': _text(item.get('participantId', item.get('participant_id'))),
            })
        pending = []
        raw_pending = alter_state.get('pending_scopes', alter_state.get('pendingScopes'))
        for item in raw_pending or []:
            if not isinstance(item, dict):
                continue
            pending.append({
                'participant_id': _text(item.get('participantId', item.get('participant_id'))),
                'value': float(item.get('alterValue', item.get('alter_value')) or 0),
                'last_attempt_at': _text(item.get('lastAnalysisAttemptAt', item.get('last_analysis_attempt_at'))),
            })
        return {
            'story': self._story_brief(story),
            'state': {
                'value': float(alter_state.get('alter_value', alter_state.get('alterValue')) or 0),
                'weight': float(alter_state.get('alter_weight', alter_state.get('alterWeight')) or 0),
                'direction': _int(alter_state.get('last_trigger_direction', alter_state.get('lastTriggerDirection'))),
                'updated_at': _text(alter_state.get('last_updated_at', alter_state.get('lastUpdatedAt'))),
                'last_attempt_at': _text(alter_state.get('last_analysis_attempt_at', alter_state.get('lastAnalysisAttemptAt'))),
                'offset': {
                    'direction': _text(offset.get('direction')),
                    'description': _text(offset.get('description')),
                    'intensity': float(offset.get('intensity') or 0),
                    'generated_at': _text(offset.get('generated_at', offset.get('generatedAt'))),
                } if offset else None,
            },
            'history': history,
            'pending': pending,
            'config': self._alter_config(),
        }

    async def agency(self, story_id: str = '') -> dict[str, Any]:
        """Agency 行动窗口 + 日程预排摘要。"""
        story = self._current_story(story_id)
        if not story:
            return {'story': None, 'window': None, 'plan': None, 'config': self._agency_config()}
        state = story.get('state') if isinstance(story.get('state'), dict) else {}
        raw_window = state.get('agency_window', state.get('agencyWindow'))
        window = raw_window if isinstance(raw_window, dict) else None
        plan = self._schedule_plan(_text(story.get('id')))
        return {
            'story': self._story_brief(story),
            'window': {
                'activity_load': _text(window.get('activity_load', window.get('activityLoad'))),
                'privacy': _text(window.get('privacy')),
                'device_access': _text(window.get('device_access', window.get('deviceAccess'))),
                'next_opportunity_at': _text(window.get('next_opportunity_at', window.get('nextOpportunityAt'))),
                'valid_until': _text(window.get('valid_until', window.get('validUntil'))),
                'basis': _text(window.get('basis')),
                'source_entry_ids': window.get('source_entry_ids', window.get('sourceEntryIds')) or [],
                'updated_at': _text(window.get('updated_at', window.get('updatedAt'))),
            } if window else None,
            'plan': plan,
            'config': self._agency_config(),
        }

    async def delivery(self, story_id: str = '', limit: int = 300, status: str = '') -> dict[str, Any]:
        """投递账本：按剧本条目回放每一次行动的投递结果。

        账本存在 `script_entry.metadata.delivery_actions`（上游 M6.1），所以这里扫最近
        若干条带账本的条目，把 commit / event / segment 三层拍平成表格——页面要能回答
        "这条消息到底发出去了没有、哪个分段失败了"。
        """
        story = self._current_story(story_id)
        if not story:
            return {'story': None, 'actions': [], 'totals': {}, 'scanned': 0}
        database = self.bridge.db
        size = max(1, min(1000, _int(limit, 300)))
        rows = _safe_all(
            database, 'interlude_script_entry', {'storyId': _text(story.get('id'))},
            'occurredAt DESC', size,
        )
        wanted = _text(status).strip().lower()
        actions: list[dict[str, Any]] = []
        totals: dict[str, int] = {}
        for row in rows:
            metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
            entries = metadata.get('delivery_actions')
            if not isinstance(entries, list):
                continue
            for action in entries:
                if not isinstance(action, dict):
                    continue
                segments = []
                for segment in action.get('segments') or []:
                    if not isinstance(segment, dict):
                        continue
                    segments.append({
                        'index': _int(segment.get('index')),
                        'kind': _text(segment.get('kind')),
                        'status': _text(segment.get('status')),
                        'attempts': _int(segment.get('attempts')),
                        'error': _text(segment.get('error'))[:200],
                    })
                state = _text(action.get('status'))
                totals[state] = totals.get(state, 0) + 1
                if wanted and state.lower() != wanted:
                    continue
                actions.append({
                    'entry_id': row.get('id'),
                    'occurred_at': _text(row.get('occurredAt')),
                    'commit_id': _text(action.get('commitId', action.get('commit_id'))),
                    'event_id': _text(action.get('eventId', action.get('event_id'))),
                    'status': state,
                    'target': _text(action.get('target') or action.get('channel')),
                    'attempts': _int(action.get('attempts')),
                    'updated_at': _text(action.get('updatedAt', action.get('updated_at'))),
                    'segments': segments,
                    'done': len([item for item in segments if item['status'] == 'delivered']),
                })
        return {
            'story': self._story_brief(story),
            'actions': actions,
            'totals': totals,
            'scanned': len(rows),
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _current_story(self, story_id: str = '') -> Optional[dict[str, Any]]:
        stories = _safe_all(self.bridge.db, 'interlude_story', order='updatedAt DESC', limit=50)
        return self._pick_story(stories, story_id)

    def _alter_config(self) -> dict[str, Any]:
        section = self.bridge.section('alter_system')
        return {
            'enabled': section.get('enabled') is not False,
            'threshold': section.get('threshold'),
            'max_intensity': section.get('max_intensity', section.get('maxIntensity')),
            'decay': section.get('decay'),
            'cooldown_minutes': section.get('cooldown_minutes', section.get('cooldownMinutes')),
            'weight_step': section.get('weight_step', section.get('weightStep')),
        }

    def _agency_config(self) -> dict[str, Any]:
        section = self.bridge.section('agency')
        return {
            'enabled': section.get('enabled') is not False,
            'activity_load': section.get('activity_load', section.get('activityLoad')),
            'privacy': section.get('privacy'),
            'device_access': section.get('device_access', section.get('deviceAccess')),
            'window_hours': section.get('window_hours', section.get('windowHours')),
        }

    def _schedule_plan(self, story_id: str) -> Optional[dict[str, Any]]:
        rows = _safe_all(self.bridge.db, 'interlude_schedule_preplan', {'storyId': story_id}, None, 1)
        if not rows:
            return None
        row = rows[0]
        regimes = row.get('regimes') if isinstance(row.get('regimes'), list) else []
        exceptions = row.get('exceptions') if isinstance(row.get('exceptions'), list) else []
        days = row.get('materializedDays', row.get('materialized_days'))
        days = days if isinstance(days, list) else []
        return {
            'revision': _int(row.get('revision')),
            'timezone': _text(row.get('timezone')),
            'valid_from': _text(row.get('validFrom')),
            'valid_through': _text(row.get('validThrough')),
            'last_reviewed': _text(row.get('lastReviewedLocalDate')),
            'review_reason': _text(row.get('reviewReason'))[:300],
            'regimes': regimes[:12],
            'exceptions': exceptions[:12],
            'materialized_days': days[:14],
            'updated_at': _text(row.get('updatedAt')),
        }

    @staticmethod
    def _pick_story(stories: list[Any], story_id: str) -> Optional[dict[str, Any]]:
        cleaned = _text(story_id).strip()
        if cleaned:
            for item in stories:
                if _text(item.get('id')) == cleaned:
                    return item
        return stories[0] if stories else None

    def _story_brief(self, story: dict[str, Any]) -> dict[str, Any]:
        setting = story.get('setting') if isinstance(story.get('setting'), dict) else {}
        character = setting.get('character') if isinstance(setting.get('character'), dict) else {}
        state = story.get('state') if isinstance(story.get('state'), dict) else {}
        return {
            'id': _text(story.get('id')),
            'status': _text(story.get('status')),
            'platform': _text(story.get('platform')),
            'character': _text(character.get('name')),
            'user_name': _text((setting.get('user') or {}).get('name')) if isinstance(setting.get('user'), dict) else '',
            'timezone': _text(setting.get('timezone')),
            'cursor_at': _text(story.get('cursorAt')),
            'created_at': _text(story.get('createdAt')),
            'updated_at': _text(story.get('updatedAt')),
            'scene': _text((state.get('currentScene') or {}).get('hook')) if isinstance(state.get('currentScene'), dict) else '',
        }

    def _flags(self) -> dict[str, Any]:
        bridge = self.bridge
        return {
            'allow_proactive_messages': bridge.config_flag(
                'runtime', 'allow_proactive_messages', 'allowProactiveMessages', default=False,
            ),
            'agency': bridge.config_flag('agency', 'enabled', default=True) is not False,
            'urge': bridge.config_flag('urge', 'enabled', default=False),
            'schedule_preplan': bridge.config_flag('schedule_preplan', 'enabled', default=False),
            'timeline_director': bridge.config_flag('timeline_director', 'enabled', default=False),
            'compaction': bridge.config_flag('compaction', 'enabled', default=True) is not False,
            'embedding': bridge.config_flag('embedding', 'enabled', default=False),
            'vision': bridge.config_flag('vision', 'enabled', default=False),
            'audio': bridge.config_flag('audio', 'enabled', default=False),
            'browser': bridge.config_flag('browser', 'enabled', default=False),
            'blind_mode': bool(getattr(bridge, 'blind_mode_enabled', False)),
        }

    def _routing_rows(self) -> list[dict[str, Any]]:
        """每个任务当前实际指向哪个模型。"""
        table = getattr(getattr(self.bridge, 'service', None), 'model_routing', None) or {}
        rows: list[dict[str, Any]] = []
        for key, label in CONSOLE_TASKS:
            route = table.get(key) if isinstance(table, dict) else None
            route = route if isinstance(route, dict) else {}
            providers = route.get('providers') if isinstance(route.get('providers'), list) else []
            first = providers[0] if providers and isinstance(providers[0], dict) else {}
            target = route.get('target') if isinstance(route.get('target'), dict) else {}
            bound = self.bridge.task_model_id(key)
            source = 'astrbot' if bound else ('connection' if first else 'none')
            rows.append({
                'task': key,
                'label': label,
                'source': source,
                'provider_label': _text(first.get('label')) or (f'AstrBot · {bound}' if bound else ''),
                'model': _text(target.get('model')) or _text(first.get('model')) or bound,
                'assigned': bool(route.get('assigned')),
                'available': bool(route.get('available')),
                'reason': _text(route.get('reason')),
                'candidates': len(providers),
            })
        return rows

    def _astrbot_providers(self) -> list[dict[str, Any]]:
        getter = getattr(self.bridge.context, 'get_all_providers', None)
        providers: list[Any] = []
        if callable(getter):
            try:
                providers = list(getter() or [])
            except Exception:  # noqa: BLE001
                providers = []
        rows = []
        for provider in providers:
            meta = getattr(provider, 'meta', None)
            identifier = ''
            if callable(meta):
                try:
                    identifier = _text(getattr(meta(), 'id', ''))
                except Exception:  # noqa: BLE001
                    identifier = ''
            config = getattr(provider, 'provider_config', None) or {}
            rows.append({
                'id': identifier,
                'model': _text(config.get('model')) if isinstance(config, dict) else '',
                'type': _text(config.get('type')) if isinstance(config, dict) else '',
                'provider_type': _text(config.get('provider_type')) if isinstance(config, dict) else '',
                'modalities': sorted(self.bridge.provider_modalities(provider)),
                'used_by': [
                    label for key, label in CONSOLE_TASKS if self.bridge.task_model_id(key) == identifier
                ],
            })
        return rows

    def _note(self, kind: str) -> str:
        try:
            return self.bridge.image_capability_note() if kind == 'image' else self.bridge.audio_capability_note()
        except Exception:  # noqa: BLE001
            return ''

    def _usage(self) -> dict[str, Any]:
        records = list(getattr(self.bridge, 'usage_records', []) or [])
        totals: dict[str, dict[str, Any]] = {}
        for item in records:
            task = _text(item.get('task')) or '未标注'
            bucket = totals.setdefault(task, {'task': task, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'calls': 0})
            bucket['prompt_tokens'] += _int(item.get('prompt_tokens'))
            bucket['completion_tokens'] += _int(item.get('completion_tokens'))
            bucket['total_tokens'] += _int(item.get('total_tokens'))
            bucket['calls'] += 1
        ordered = sorted(totals.values(), key=lambda item: item['total_tokens'], reverse=True)
        return {
            'recent': list(reversed(records))[:40],
            'totals': ordered,
            'sum': {
                'calls': len(records),
                'prompt_tokens': sum(item['prompt_tokens'] for item in ordered),
                'completion_tokens': sum(item['completion_tokens'] for item in ordered),
                'total_tokens': sum(item['total_tokens'] for item in ordered),
            },
            'capacity': CONSOLE_USAGE_MAX,
        }

    def _entry_brief(self, entry: dict[str, Any]) -> dict[str, Any]:
        metadata = entry.get('metadata') if isinstance(entry.get('metadata'), dict) else {}
        content = _text(entry.get('content'))
        return {
            'id': entry.get('id'),
            'kind': _text(entry.get('kind')),
            'actor': _text(entry.get('actor')),
            'content': content[:600],
            'truncated': len(content) > 600,
            'occurred_at': _text(entry.get('occurredAt')),
            'commit_id': _text(metadata.get('commit_id')),
        }

    def _fact_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        knowledge = row.get('knowledge') if isinstance(row.get('knowledge'), dict) else {}
        return {
            'id': row.get('id'),
            'scope': _text(row.get('scope')),
            'content': _text(row.get('content')),
            'importance': float(row.get('importance') or 0),
            'confidence': float(row.get('confidence') or 0),
            'status': _text(row.get('status')),
            'unresolved': bool(row.get('unresolved')),
            'knowledge_kind': _text(knowledge.get('kind')),
            'quote': _text(knowledge.get('quote'))[:200],
            'last_seen_at': _text(row.get('lastSeenAt')),
        }

    def _memory_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            'id': row.get('id'),
            'category': _text(row.get('category')),
            'content': _text(row.get('content')),
            'importance': float(row.get('importance') or 0),
            'status': _text(row.get('status')),
            'updated_at': _text(row.get('updatedAt')),
        }

    def _intent_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            'id': row.get('id'),
            'type': _text(row.get('type')),
            'summary': _text(row.get('summary')),
            'status': _text(row.get('status')),
            'not_before': _text(row.get('notBefore')),
        }

    def _patch_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            'id': row.get('id'),
            'target': _text(row.get('target')),
            'path': _text(row.get('path')),
            'proposed_value': _text(row.get('proposedValue'))[:300],
            'confidence': float(row.get('confidence') or 0),
            'impact': _text(row.get('impact')),
            'status': _text(row.get('status')),
            'created_at': _text(row.get('createdAt')),
        }

    def _overlay_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            'id': row.get('id'),
            'target': _text(row.get('target')),
            'tier': _text(row.get('tier')),
            'summary': _text(row.get('summary')),
            'period_end': _text(row.get('periodEnd')),
            'status': _text(row.get('status')),
        }

    def _participant_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        state = row.get('state') if isinstance(row.get('state'), dict) else {}
        return {
            'id': _text(row.get('id')),
            'display_name': _text(row.get('displayName')),
            'platform': _text(row.get('platform')),
            'status': _text(row.get('status')),
            'relationship': _text(row.get('relationship'))[:200],
            'updated_at': _text(row.get('updatedAt')),
            'has_state': bool(state),
        }


