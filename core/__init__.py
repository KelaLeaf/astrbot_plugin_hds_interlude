"""HDS Interlude 核心逻辑包。

与 AstrBot 解耦，只表达叙事本身，便于移植与跟进上游。上层通过
`adapters/astrbot_bridge.py` 把 AstrBot 事件翻译进这里的 NarrativeContext，
并从这里拿到 NarrativeDecision 再回投给 AstrBot。
"""

from . import model, narrative, time, types  # noqa: F401
