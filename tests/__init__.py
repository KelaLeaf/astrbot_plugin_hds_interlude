"""测试包引导：让测试在**两种仓库布局**下都能直接跑。

1. **开发工作区**：`<repo>/plugin/tests/` → 包名 `plugin.tests.*`，被测代码是 `plugin.core...`。
2. **发布仓**（GitHub 上仓库根**就是**插件根）：`<repo>/tests/` → 包名 `tests.*`，
   此时 **`plugin` 这个包并不存在**，那些写死 `from plugin.core.x import y` 的测试会
   `ModuleNotFoundError: No module named 'plugin'`。

第 2 种情况在这里把仓库根注册成一个**合成 `plugin` 包**（`__path__ = [插件根]`），
两种布局下测试代码一字不用改。
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import sys

#: 插件根目录。开发工作区里是 `<repo>/plugin`；发布仓里就是仓库根。
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 仓库根目录。开发工作区里是 `PLUGIN_ROOT` 的上一级；发布仓里与 `PLUGIN_ROOT` 相同。
REPO_ROOT = os.path.dirname(PLUGIN_ROOT)

#: 从 `REPO_ROOT` 出发能 import 到本插件的**包名**。
#: 本地工作区是 `plugin`；发布仓里是仓库目录名（PEP 420 命名空间包）。
PACKAGE_NAME = os.path.basename(PLUGIN_ROOT)


def _ensure_plugin_package() -> None:
    """发布仓布局下，把插件根伪装成可导入的 `plugin` 包。"""
    if 'plugin' in sys.modules:
        return
    try:  # 开发工作区：`plugin` 本来就能导入
        importlib.import_module('plugin')
        return
    except ImportError:
        pass
    spec = importlib.machinery.ModuleSpec('plugin', None, is_package=True)
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [PLUGIN_ROOT]  # type: ignore[attr-defined]
    sys.modules['plugin'] = module


_ensure_plugin_package()
