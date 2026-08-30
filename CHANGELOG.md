# 更新日志

## v0.1.0

首个 AstrBot 移植版。把上游 Koishi 的持续叙事框架移植为 AstrBot 插件，聚焦核心叙事子集。

- 主叙事闭环：用户私聊 → 读取活跃场景与当前状态 → 一次持续写作 → 决定看见/回复/沉默/延迟。
- 时间感知：UTC + 故事本地时间、时段/日照预期，作为主叙事权威时间锚点。
- 长期事实记忆：从剧本条目提炼长期事实，供主叙事召回。
- 记忆压缩：剧本条目超阈值时自动压缩旧场景为连续性快照，并增量并入长期事实（默认 18 条触发）。
- OneBot/NapCat 适配：`@filter.platform_adapter_type(AIOCQHTTP)` 拦截私聊文本；支持可选用户白名单。
- 结构化输出：可见回复、剧本补写、意图（延迟回复/提醒/主动联系/后续计划）。
- 一键导入人格：`/hdsi.personas` 列出 AstrBot 人格，`/hdsi.import_persona <id>` 把某个人格 prompt 解析并覆盖填充进插件角色配置；配置页可选 `persona_id`(`_special: select_persona`) 启动时自动导入。
- 叙事工具感知：`runtime.narrative_tools` 开启后，主叙事可感知 AstrBot 会话工具 / MCP（默认关闭，避免影响 JSON 输出）。

未移植（见 README「本移植版 vs 上游」）：群聊、Agency Window、Alter System、图片视觉、QQ 语音转写、网页观察、完整记忆分层、后台自动生活推进。

## 实测修复

- 叙事 JSON 请求兼容低配模型：`response_format=json_object` 在部分 Ollama 模型上导致 500，失败时回落为普通请求 + 宽松提取。
- `_data_dir` 用固定插件名替代 `self.name`（Star 基类无 name 属性），修复插件加载 AttributeError。

> 已用本地 AstrBot + Ollama `huihui_ai/qwen2.5-vl-abliterated` 实测：主叙事成功返回结构化决策（凌梦人格回复「吃过了，主人。」）。

## 待发布

- 后台自动生活推进（角色在用户不说话时也持续生活）。
