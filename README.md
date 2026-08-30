# astrbot_plugin_hds_interlude

时间感知的持续叙事聊天框架，AstrBot 移植版。

> 聊天在幕前发生，生活在幕间继续。

本插件是 [koishi-plugin-hds-interlude](https://gitee.com/MomoiCore/hds-interlude)（Koishi 叙事框架 v0.1.4-beta3-enhanced）的 AstrBot 二次开发版，
聚焦其核心叙事子集：让用户消息、角色的沉默、延迟回复、主动联系和自动推进，都成为同一段生活剧本中自然可见的部分，并由一次主叙事写作连贯地决定。

## 功能（v0.1.0）

- **主叙事闭环**：用户私聊 → 读取活跃场景与当前状态 → 一次持续写作 → 决定看见/回复/沉默/延迟
- **时间感知**：UTC + 故事本地时间、时段/日照预期，作为主叙事权威时间锚点
- **长期事实记忆**：从剧本条目提取长期事实，供主叙事召回
- **结构化决策**：主模型输出可见回复、剧本补写、意图（待办/承诺/延迟动作）

## 安装

1. 将插件文件夹放入 AstrBot 的插件目录。
2. 在 AstrBot 后台安装依赖（`requirements.txt` 含 `httpx`、`pyyaml`）。
3. 重启 AstrBot，进入插件配置页填写模型与剧本起点。

## 配置

见 `astrbot_plugin_hds_interlude/_conf_schema.json`。核心项：

- **模型**：勾选 `enabled` 后使用独立 OpenAI 兼容连接；否则走 AstrBot 默认 LLM Provider。
- **剧本起点**：主角姓名、角色设定、世界设定、叙事风格、时区。
- **运行时**：私聊拦截开关、自动创建开关、上下文条目数、长期事实数。

## 隐私与安全

- 插件**不收集、不上传任何 API Key 或个人信息**。模型密钥通过 AstrBot 插件配置注入，或复用 AstrBot 默认 Provider，**绝不硬编码在源码**。
- 数据仅在本地读写：`data/plugin_data/astrbot_plugin_hds_interlude/`。
- 除你所配置的模型端点外，不调用任何外部服务。

## 目录

```
astrbot_plugin_hds_interlude/
├── main.py                 # 插件入口（Star 子类）
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── LICENSE                 # AGPL-3.0
├── core/                   # 叙事核心（与 AstrBot 解耦）
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
