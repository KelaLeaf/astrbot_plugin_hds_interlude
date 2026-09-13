"""AstrBot 适配层：把 AstrBot 事件翻译进 `plugin/core/`。

本包是全项目**唯一**允许 `import astrbot` 的地方（另一个是 `plugin/main.py`）。
`plugin/core/` 必须保持平台无关、可独立测试（`docs/PORT_PLAN.md` §0.3）。

对外入口：

* `session_view(event)` —— `AstrMessageEvent` → `SessionView`
* `AstrbotTransport` —— `Transport` 协议的 AstrBot 实现（私聊/群聊/表态/下载/网页观察）
* `AstrbotInterludeContext` —— Koishi `ctx` 的等价容器（logger / db / http / timers）
* `AstrbotBridge` —— 装配 + 入站分发 + 生命周期，`plugin/main.py` 只跟它打交道
"""

from __future__ import annotations

from .astrbot_bridge import (
    PLUGIN_NAME,
    AstrbotBridge,
    AstrbotEndpoint,
    AstrbotHttpClient,
    AstrbotInterludeContext,
    AstrbotTransport,
    build_bridge,
    database_path,
    endpoint_for_event,
    looks_like_management_command,
    normalize_bridge_config,
    plugin_data_dir,
    resolve_platform_name,
    serialize_message_chain,
    session_view,
)

__all__ = [
    'PLUGIN_NAME',
    'AstrbotBridge',
    'AstrbotEndpoint',
    'AstrbotHttpClient',
    'AstrbotInterludeContext',
    'AstrbotTransport',
    'build_bridge',
    'database_path',
    'endpoint_for_event',
    'looks_like_management_command',
    'normalize_bridge_config',
    'plugin_data_dir',
    'resolve_platform_name',
    'serialize_message_chain',
    'session_view',
]
