# HDS Interlude for AstrBot

HDS Interlude（幕间系统）的 AstrBot 移植版。聊天在幕前发生，生活在幕间继续。

这是一个**时间感知、持续叙事**的聊天框架：让用户消息、角色的沉默、延迟回复、主动联系和自动推进，都成为同一段生活剧本中自然可见的部分，并由一次主叙事写作连贯地决定。

## 移植来源

上游为 Koishi 插件 [hds-interlude](https://gitee.com/MomoiCore/hds-interlude)（v0.1.4-beta3-enhanced）。本插件是其核心叙事子集的 AstrBot 二次开发版。

## 已实现（v0.1.0）

- 主叙事闭环：用户私聊 → 读取活跃场景与当前状态 → 一次持续写作 → 决定看见/回复/沉默/延迟
- 时间感知：UTC + 故事本地时间、时段/日照预期，作为主叙事权威时间锚点
- 长期事实记忆：从剧本条目提取长期事实，供主叙事召回
- 结构化决策：主模型输出可见回复、剧本补写、意图（待办/承诺/延迟动作）

## 待实现

- 场景压缩与连续性快照（partial，`core/memory.py` 已提供能力）
- 后台自动生活推进
- 群聊意愿、Alter System、Agency Window、聊天动作、网页观察等上游高级功能

## 安装

1. 把 `astrbot_plugin_hds_interlude` 放入 AstrBot 的插件目录。
2. 在 AstrBot 后台安装依赖：`requirements.txt` 含 `httpx`、`pyyaml`。
3. 重启 AstrBot，进入插件配置页填写模型与剧本起点。

## 配置

见 `_conf_schema.json`。核心项：

- **模型**：`enabled` 勾选后使用独立 OpenAI 兼容连接；否则走 AstrBot 默认 Provider。
- **剧本起点**：主角姓名、角色设定、世界设定、叙事风格、时区。
- **运行时**：私聊拦截开关、自动创建开关、上下文条目数、长期事实数。

## 使用

私聊直接发消息即进入叙事。管理命令：

- `/hdsi.status`：查看当前主剧本状态。

## 隐私说明

插件里没有写死的密钥，模型的 key 都是你在 AstrBot 配置里填的，或者直接用它的默认模型。剧本、参与者、记忆这些数据也都只存在本地 `data/plugin_data/astrbot_plugin_hds_interlude/` 里，除了你填的那个模型接口，它不会主动连别的地方。

## 目录

```
astrbot_plugin_hds_interlude/
├── main.py                 # 插件入口（Star 子类）
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── core/                   # 叙事核心逻辑（与 AstrBot 解耦）
│   ├── model.py            # OpenAI 兼容 LLM 客户端
│   ├── time.py             # 时间感知
│   ├── narrative.py        # 主叙事
│   ├── memory.py           # 记忆压缩 + 长期事实
│   └── types.py            # 领域模型
└── adapters/
    └── astrbot_bridge.py   # AstrBot 事件 ↔ core 桥接
```

## 许可证

AGPL-3.0（与上游一致）。
