"""插件配置的导出 / 导入：带版本的信封、迁移链与**向后兼容**。

## 为什么单独一层

配置是**跨版本的用户资产**：用户今天导出一份，半年后升级插件再导入，中间可能经历过
分组改名、键改名、键拆分或合并。所以导入路径必须做到：

1. **认识所有历史格式**——带信封的新导出、早期版本的"裸配置 dict"、手写的片段；
2. **把旧键迁移到当前键**——迁移函数**只增不改**（见 `_MIGRATIONS`），
   永远不要删掉或改写已有的迁移步骤，否则老用户的文件就废了；
3. **只补默认值，不丢未知键**——用户可能装了别的版本、手写了扩展项；
4. **永不因为多一个键 / 少一个键就拒绝整份配置**。

真正的键/分组归一交给 `core.service.config` 的
`apply_section_aliases` + `normalize_config`（那边是唯一实现源），本模块只管
**信封、版本与迁移链**，两边职责不重叠。

## 文件格式

```json
{
  "format": "astrbot_plugin_hds_interlude.config",
  "formatVersion": 1,
  "pluginVersion": "v1.1.0",
  "upstreamVersion": "1.0.1-beta6-rebuild",
  "exportedAt": "2026-09-13T18:40:00.000Z",
  "sections": ["story_defaults", "model_center", "..."],
  "config": { "story_defaults": {...}, "model_center": {...} }
}
```

`config` 就是 `data/config/astrbot_plugin_hds_interlude_config.json` 的原样内容
（未归一化，保留 AstrBot schema 的分组名与用户填的一切）。

## 兼容契约（改动本文件前必读）

* `formatVersion` **只增不改**：新增字段永远是可选的。
* 导入时 `formatVersion` 比当前**新**（用户用了更新版插件导出的文件）→ **仍然尝试导入**，
  只发一条 warning。拒绝导入比"少认识几个键"糟糕得多。
* 导入时 `formatVersion` 比当前**旧** → 依次跑 `_MIGRATIONS` 里每一步。
* 没有任何信封的裸 dict → 视为 `formatVersion = 0`。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Callable

__all__ = [
    'CONFIG_EXPORT_FORMAT',
    'CONFIG_EXPORT_VERSION',
    'build_export',
    'export_filename',
    'parse_import',
    'migrate_config',
    'diff_config',
    'looks_like_export',
    'KNOWN_SECTIONS',
]

#: 信封标识。改它等于换一种文件格式，**不要改**。
CONFIG_EXPORT_FORMAT = 'astrbot_plugin_hds_interlude.config'

#: 当前信封版本。新增/改名配置项时**不需要**动它；只有信封结构本身变了才 +1。
CONFIG_EXPORT_VERSION = 1

#: 信封里可能承载配置正文的键，按优先级排列（兼容未来/历史写法）。
_PAYLOAD_KEYS = ('config', 'settings', 'conf', 'data')

#: 认得出是"我们的信封"的键。
_ENVELOPE_KEYS = frozenset({
    'format', 'formatVersion', 'pluginVersion', 'upstreamVersion', 'exportedAt', 'sections', 'note',
})

#: 已知顶层分组（含 AstrBot schema 名与上游名）。用于把"裸 dict"与误传的文件区分开。
KNOWN_SECTIONS = frozenset({
    'story_defaults', 'model_center', 'qq_access', 'shared_story', 'runtime', 'urge',
    'schedule_preplan', 'timeline_director', 'agency', 'chat_actions', 'stickers',
    'memory', 'alter_system', 'browser', 'blind_mode', 'logging', 'prompts',
    'chat_rhythm', 'black_box', 'shared_story_compat', 'runtime_compat', 'model_compat',
    # 上游 Koishi Console 的分组名（老导出文件里会出现）
    'model', 'onebot', 'storyDefaults', 'sharedStory', 'alterSystem',
    'schedulePreplan', 'timelineDirector', 'chatActions', 'blindMode', 'chatRhythm', 'blackBox',
})


class ConfigImportError(ValueError):
    """导入载荷无法识别时抛出（调用方应把 message 直接展示给用户）。"""


# --------------------------------------------------------------------------- #
# 迁移链
# --------------------------------------------------------------------------- #

def _migrate_envelope_0_to_1(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """v0（裸配置，没有信封）→ v1。

    早期版本/手写文件直接就是一份 `{分组: {...}}`。键与分组名的归一交给
    `normalize_config`（它会补 `model_center ↔ model` 的别名），这里只记录一条说明，
    让用户知道这份文件是"无信封"的老格式，导入是刻意支持的而不是猜出来的。
    """
    return config, ['识别为无信封的早期配置（formatVersion 0），按当前格式导入。']


#: `formatVersion` N → N+1 的迁移步骤。**只增不改**：老用户的文件永远要能导入。
_MIGRATIONS: dict[int, Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]]] = {
    0: _migrate_envelope_0_to_1,
}


def migrate_config(config: dict[str, Any], from_version: int) -> tuple[dict[str, Any], list[str]]:
    """把 `from_version` 的配置正文逐级迁移到当前版本。

    返回 `(迁移后的配置, 说明列表)`。缺失的迁移步骤按"无需迁移"处理（不会报错）——
    因为迁移链只要求**结构性变化**才登记一步。
    """
    notes: list[str] = []
    current = config
    version = max(0, int(from_version))
    while version < CONFIG_EXPORT_VERSION:
        step = _MIGRATIONS.get(version)
        if step is not None:
            current, step_notes = step(current)
            notes.extend(step_notes)
        version += 1
    return current, notes


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #

def _now_iso(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def build_export(
    config: Any,
    *,
    plugin_version: str,
    upstream_version: str | None = None,
    now: datetime | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """把一份配置包成可长期保存的导出信封。

    `config` 原样放进 `config` 键（**不做归一化**）——导出要保留用户实际存下来的样子，
    包括我们不认识的键；归一化只发生在导入那一步。
    """
    body = copy.deepcopy(config) if isinstance(config, dict) else {}
    envelope: dict[str, Any] = {
        'format': CONFIG_EXPORT_FORMAT,
        'formatVersion': CONFIG_EXPORT_VERSION,
        'pluginVersion': str(plugin_version or ''),
        'exportedAt': _now_iso(now),
        'sections': sorted(body.keys()),
        'config': body,
    }
    if upstream_version:
        envelope['upstreamVersion'] = str(upstream_version)
    if note:
        envelope['note'] = str(note)
    return envelope


def export_filename(plugin_version: str, now: datetime | None = None) -> str:
    """`hdsi-config-v1.1.0-20260913-184000.json`。"""
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    stamp = value.astimezone(timezone.utc).strftime('%Y%m%d-%H%M%S')
    safe = ''.join(ch for ch in str(plugin_version or 'unknown') if ch.isalnum() or ch in '.-_') or 'unknown'
    return f'hdsi-config-{safe}-{stamp}.json'


def looks_like_export(payload: Any) -> bool:
    """粗判一份载荷是不是我们的导出文件（用于给用户更准确的报错）。"""
    return isinstance(payload, dict) and (
        payload.get('format') == CONFIG_EXPORT_FORMAT or bool(_ENVELOPE_KEYS & set(payload))
    )


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #

def _coerce_payload(payload: Any) -> Any:
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode('utf-8-sig')
        except UnicodeDecodeError as error:  # pragma: no cover - 罕见
            raise ConfigImportError(f'文件不是 UTF-8 文本：{error}') from error
    if isinstance(payload, str):
        # 文件可能带 BOM（AstrBot 自己写配置文件就带），粘贴时也可能带进来。
        text = payload.lstrip('\ufeff').strip()
        if not text:
            raise ConfigImportError('配置内容为空。')
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ConfigImportError(
                f'不是合法的 JSON（第 {error.lineno} 行第 {error.colno} 列）：{error.msg}'
            ) from error
    return payload


def parse_import(payload: Any) -> dict[str, Any]:
    """解析一份导入载荷，返回：

    ```
    {
      'config':         归一化**之前**的配置正文（dict），
      'format_version': 识别出来的信封版本（裸配置为 0），
      'source':         'envelope' | 'bare' | 'sections',
      'notes':          [迁移说明…],
      'warnings':       [需要提醒用户的地方…],
      'envelope':       原始信封（裸配置时为 {}），
    }
    ```

    接受：信封 dict / JSON 字符串 / 裸配置 dict / 只有几个分组的片段 dict。
    只有"完全不是对象"或"JSON 语法错"才抛 `ConfigImportError`。
    """
    value = _coerce_payload(payload)
    if not isinstance(value, dict):
        raise ConfigImportError(
            f'配置必须是 JSON 对象，实际是 {type(value).__name__}。'
        )

    notes: list[str] = []
    warnings: list[str] = []
    envelope: dict[str, Any] = {}

    # 1) 先找信封里的配置正文
    body: Any = None
    for key in _PAYLOAD_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, dict):
            body = candidate
            envelope = value
            break

    if body is not None:
        source = 'envelope'
        raw_version = value.get('formatVersion')
        if raw_version is None:
            # 有信封壳但没有版本号：按 v1 处理（我们自己写的信封一定有版本号）
            format_version = CONFIG_EXPORT_VERSION
            warnings.append('导出文件缺少 formatVersion，按当前版本处理。')
        else:
            try:
                format_version = int(raw_version)
            except (TypeError, ValueError):
                format_version = CONFIG_EXPORT_VERSION
                warnings.append(f'formatVersion 不是整数（{raw_version!r}），按当前版本处理。')
        fmt = value.get('format')
        if fmt and fmt != CONFIG_EXPORT_FORMAT:
            warnings.append(f'导出文件的 format 是 {fmt!r}，不是本插件的格式，仍按本插件格式尝试导入。')
        if format_version > CONFIG_EXPORT_VERSION:
            warnings.append(
                f'这份配置由更新版本的插件导出（formatVersion {format_version} > '
                f'{CONFIG_EXPORT_VERSION}）：本插件只认识自己已知的键，'
                '未知键会原样保留，升级插件后再导入即可完整使用。'
            )
        version_label = value.get('pluginVersion')
        if version_label:
            notes.append(f'导出文件来自插件 {version_label}。')
    elif value and set(value.keys()) <= KNOWN_SECTIONS:
        # 2) 裸配置：顶层全是已知分组
        body = value
        source = 'bare'
        format_version = 0
    else:
        # 3) 片段：只有一两个分组，或者是用户手写的
        body = value
        source = 'sections'
        format_version = 0
        unknown = sorted(k for k in value if k not in KNOWN_SECTIONS)
        if unknown:
            warnings.append(
                '以下顶层键不是本插件已知的配置分组，会按原样保留：' + '、'.join(unknown[:8])
                + ('…' if len(unknown) > 8 else '')
            )

    migrated, migration_notes = migrate_config(body, format_version)
    notes.extend(migration_notes)
    if not isinstance(migrated, dict):  # pragma: no cover - migrate 保证是 dict
        raise ConfigImportError('迁移后的配置不是对象。')
    if not migrated:
        warnings.append('这份配置没有任何分组内容，导入后等同于「全部用默认值」。')

    return {
        'config': migrated,
        'format_version': format_version,
        'source': source,
        'notes': notes,
        'warnings': warnings,
        'envelope': envelope,
    }


# --------------------------------------------------------------------------- #
# 差异预览
# --------------------------------------------------------------------------- #

def _flatten(value: Any, prefix: str = '') -> dict[str, Any]:
    """把嵌套配置压成 `'a.b.c' -> 标量`，列表整体当成一个值。"""
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            flat.update(_flatten(item, f'{prefix}.{key}' if prefix else str(key)))
    else:
        flat[prefix] = value
    return flat


def diff_config(current: Any, incoming: Any) -> dict[str, Any]:
    """比较"当前配置"与"待导入配置"，给出人话级别的变更概览。

    返回 `{'added': [键…], 'removed': [键…], 'changed': [键…], 'same': int}`，
    键是点分路径。用于导入前的确认提示——用户要能一眼看出这次导入会改什么。
    """
    left = _flatten(current if isinstance(current, dict) else {})
    right = _flatten(incoming if isinstance(incoming, dict) else {})
    added = sorted(k for k in right if k not in left)
    removed = sorted(k for k in left if k not in right)
    changed = sorted(k for k in right if k in left and left[k] != right[k])
    same = sum(1 for k in right if k in left and left[k] == right[k])
    return {'added': added, 'removed': removed, 'changed': changed, 'same': same}
