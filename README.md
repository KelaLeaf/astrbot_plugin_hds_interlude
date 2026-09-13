# HDS Interlude / 幕间系统

> 聊天在幕前发生，生活在幕间继续。

HDS Interlude 是一个给 **AstrBot** 用的持续叙事聊天插件（上游 Koishi 版的完整移植）。装上以后，你的角色不再"收到一条消息就回一条"，而是**一直在过自己的日子**——你发过去的消息只是这段时间里发生的一件事，她可能马上看见，也可能没看见、不想回、正忙着、晚点再回。

当前版本 `v1.0.0`，对应上游 `1.0.1-beta6-rebuild`。

| | |
| --- | --- |
| 插件名 | `astrbot_plugin_hds_interlude` |
| 版本 | `v1.0.0` |
| AstrBot | `>=4.16,<5` |
| 依赖 | `httpx`、`pyyaml` |
| 上游 | [HDS Interlude（Koishi）](https://gitee.com/MomoiCore/hds-interlude) |
| 发布 | [KelaLeaf/astrbot_plugin_hds_interlude](https://github.com/KelaLeaf/astrbot_plugin_hds_interlude) |
| 许可 | AGPL-3.0 |

## 它和普通角色扮演插件有什么不同

普通角色扮演插件的模型是「输入一条，输出一条」。HDSI 的模型是**一段连续的生活**：

- **时间是真的**。故事有自己的时区、本地时间、星期和日照；每次写作都知道"现在是周四下午四点，天还亮着"。你昨晚说的事、三天前答应的回信，都在正确的时间位置上。
- **你的消息只是一件事**。它进入角色的当下，参与判断，但不主宰一切。她可以看了不回，可以只回一个"嗯"，可以说"在忙，晚点说"，也可以隔半小时才想起来。
- **沉默也是剧情**。没回不是出错，是一个被记录下来的决定；剧本里写着她那会儿在干什么。
- **投递有账本**。每条真的发出去的话都带 `commitId` / `eventId`，按气泡、贴图、原生表情逐段回写 `pending / delivered / partial / failed / cancelled`。"她说过"和"她想过但没说"是两件事，不会混。
- **记忆分层**。原始剧本 → 场景/弧摘要 → 长期事实 → Overlay 设定演化 → Perspective 人格外壳 → 剧情余波。事实还带证据：她是**亲眼看到**、听人转述，还是自己猜的。
- **日程是活的**。Schedule Preplan 在后台维护近期计划，主叙事只读未来约半天，而且不会把计划当成已经发生的事实。
- **Alter 情绪天气**。每轮记录一个 -5..+5 的氛围偏移，累积过阈值就让侧端模型写一句氛围描述，注入后续提示词；同向强化、反向消退、太淡就自动清掉。角色不会聊久了就只剩一种腔调。
- **Agency 行动窗口**。她主动找你之前，先看日程负荷、有没有隐私、设备顺不顺手。忙或者不方便的时候，不是"不发"，而是过一阵子带着新的生活重新判断。
- **群聊意愿层**。群里普通消息先过一个纯算法的意愿分（衰减、边际递减、阈值概率），觉得值得才叫主模型；@ 她永远直接通过。

一句话：**它演的是一个人的生活，不是一次问答。**

## 安装

### 插件市场

AstrBot 后台 → 插件市场 → 搜 `hds_interlude` → 安装 → 重启。

### 从 GitHub 装

后台 → 插件管理 → 从链接安装，填仓库根地址：

```text
https://github.com/KelaLeaf/astrbot_plugin_hds_interlude
```

**不要**填 `.../tree/main/xxx` 之类带子目录的地址——这个仓库的根目录**就是**插件目录，`metadata.yaml` 就在根上。

手动装也一样：把 `plugin/` 里的东西整个放进 AstrBot 的 `data/plugins/astrbot_plugin_hds_interlude/`，再装依赖：

```bash
pip install -r requirements.txt
```

依赖只有 `httpx` 和 `pyyaml`，都是 AstrBot 环境里本来就有的东西。图片缩放/动图抽帧需要 `Pillow`，**可选**：没装就透传原图，不会报错。

## 快速开始

1. **配模型**：进插件配置页 → **模型中心** → 加一条 OpenAI 兼容连接，填 `endpoint`（要完整的 Chat Completions 地址，比如 `https://api.deepseek.com/v1/chat/completions`）、`api_key`、`model`，然后勾上"用于主叙事"。想让不同任务用不同模型（压缩、Alter、Embedding、识图、表情描述），再加几条各勾各的用途。
2. **配故事**：**故事档案** → 填主角名、角色设定、世界设定、时区。偷懒的办法是 `persona_id` 那里选一个 AstrBot 人格直接导入，角色名和设定自动带过来；还想补世界观就用"其它设定补充"。
3. **说第一句话**。私聊机器人即可。默认不会自动建故事，去 **QQ 接入** 里把白名单用户加好，或者干脆打开 `runtime` 的 `auto_create` 让它第一次私聊时自己建。
4. **看状态**：`/hdsi_doctor` 体检，`/hdsi_status` 看运行状态，`/hdsi_context` 看当前场景和关系态势。想盯得更细就把 `logging` 的 `level` 调成 `debug`。

故事跑起来之后什么都不用管。后台每 5 分钟扫一遍：补写生活、兑现到期意图、压缩旧场景、审查日程、整理 Overlay。

## 配置概览

配置页一共 22 个分组。**前三组配完就能跑**，剩下的按需要开。

### 先配这三组

| 分组 | 干什么 |
| --- | --- |
| **故事档案** `story_defaults` | 主角、视角、世界、配角、默认关系、地点、文风、时区；也可以直接选一个 AstrBot 人格导入。 |
| **模型中心** `model_center` | 模型连接池 + 任务级路由（主叙事/压缩/时间线/Alter/Embedding/表情/识图），以及主叙事参数、failover、Embedding、后台压缩。 |
| **QQ 接入** `qq_access` | 机器人账号、私聊用户白名单（含每人背景与初始关系）、群聊白名单（含群用途、角色定位、触发方式）、语音转写。 |

### 结构与节奏

| 分组 | 干什么 |
| --- | --- |
| **共享主剧本** `shared_story` | 多个用户进同一份主剧本，各自的资料、关系与近期状态独立；跨账号联系与管理员。 |
| **运行时** `runtime` | 消息合并、打字节奏、失败重试、上下文条数、延迟回复、自动生活推进与休息时段。 |
| **Urge 弹性推进** `urge` | 用真实消息热度决定下次推进什么时候发生，取代固定间隔（默认关闭）。 |
| **日程预排** `schedule_preplan` | 后台维护近期日程结构，以及 stable / contextual / granular 三种变化颗粒度。 |
| **时间导演** `timeline_director` | 自动回合里的相对时间账本与降级保护。 |
| **Agency 行动窗口** `agency` | 主动联系前的日程负荷、隐私、设备三类容量条件。 |

### 表达

| 分组 | 干什么 |
| --- | --- |
| **聊天动作** `chat_actions` | 引用回复、贴反应、QQ 原生小表情，以及最低表达意愿。 |
| **本地表情包** `stickers` | 扫描本地素材目录，用视觉模型生成描述，主模型按语境挑图。 |

### 内在

| 分组 | 干什么 |
| --- | --- |
| **记忆与连续性** `memory` | 压缩触发、事实召回权重、剧情余波、以及设定演化（Overlay）的置信度与冷却门槛。 |
| **Alter 情绪** `alter_system` | 氛围偏移的阈值、权重增减、强度上限与侧端分析参数。 |

### 提示词、扩展与维护

| 分组 | 干什么 |
| --- | --- |
| **提示词** `prompts` | 主叙事写作指令、结构化输出补充规则、长期固定约束、全局文风。 |
| **网页观察** `browser` | 只读浏览：时机、搜索模板、域名黑白名单、并发与超时、正文长度上限。 |
| **盲区模式** `blind_mode` | 沉浸运行：屏蔽全部管理命令，日志只留周期心跳。 |
| **日志** `logging` | 级别、信息密度、布局配色、颜文字，以及是否输出剧本/消息内容预览。 |

剩下五个分组（`chat_rhythm`、`black_box`、`shared_story_compat`、`runtime_compat`、`model_compat`）是**旧配置兼容位**，默认折叠，正常用不到。

每个字段的类型、默认值、含义与上游出处都在 `docs/CONFIG_MAP.md` 里逐条列出。

## 命令

AstrBot 的命令名不能带点，所以上游的 `interlude.memory.facts` 在这里叫 `hdsi_memory_facts`，调用时加前缀：`/hdsi_memory_facts`。上游那套 `interlude.*` 写法也照样能被识别（不会被当成聊天内容），从 Koishi 迁过来的用户不会踩坑。

**完整 32 条对照表在 `docs/COMMANDS.md`。** 常用的这些：

**看看状态**

| 命令 | 作用 |
| --- | --- |
| `/hdsi_doctor` | 体检：档案、白名单、时区、主模型是否就绪。 |
| `/hdsi_status` | 故事状态、运行游标、主动消息、行动窗口。 |
| `/hdsi_context` | 活动场景、关系态势、剧本引子与长期连续性。 |
| `/hdsi_timeline [条数]` | 最近原始剧本条目，默认 10、最多 30。 |
| `/hdsi_schedule` | 日程覆盖范围与未来约半天的计划。 |

**管理故事**

| 命令 | 作用 |
| --- | --- |
| `/hdsi_story_start` | 从配置档案启动第一份故事（会问 y/n）。 |
| `/hdsi_pause` / `/hdsi_resume` | 暂停 / 恢复自动推进、延迟处理与主动处理。 |
| `/hdsi_advance` | 立刻把剧本补写到当前真实时间。 |
| `/hdsi_setup <JSON>` | 改当前故事的基础设定。 |
| `/hdsi_schedule_refresh` | 重新审查日程，旧计划留作稳定参考。 |

**记忆与设定演化**

| 命令 | 作用 |
| --- | --- |
| `/hdsi_memory [条数]` | 当前账号相关的记忆摘要。 |
| `/hdsi_memory_facts` / `/hdsi_memory_add` / `/hdsi_memory_forget` | 列长期事实、手工加一条、把某条标记失效（不物理删除）。 |
| `/hdsi_memory_intents` / `/hdsi_memory_cancel` | 看等待中的延迟回复、提醒、承诺与剧情余波，或取消一条。 |
| `/hdsi_memory_patches` / `/hdsi_memory_reject` | 看 / 拒绝尚未应用的设定演化提案。 |
| `/hdsi_overlay_status` / `/hdsi_overlay_compact` / `/hdsi_overlay_clear <部分>` | 看、压缩、清除指定部分的设定 overlay。 |
| `/hdsi_compact` | 整理场景、事实、状态提案，并顺带做 overlay 维护。 |
| `/hdsi_script [条数]` / `/hdsi_script_note <内容>` | 看跨参与者的原始剧本，或写一条带来源标记的人工注记。 |

**危险操作**（都会先问 y/n，60 秒不回视为取消）

| 命令 | 作用 |
| --- | --- |
| `/hdsi_timeline_rebase` | 从当前真实时间重建推进时间线，保留历史剧本。 |
| `/hdsi_database_clear` | 清空 HDSI 自有 SQLite 表。 |
| `/hdsi_purge_range <开始> <结束>` | 删除时间范围内的剧本与关联记忆，ISO-8601。 |
| `/hdsi_purge_platform <平台>` | 清空并归档某个平台的全部故事。 |
| `/hdsi_purge_all` | 彻底重置所有平台的剧本、记忆与 Canon。 |

权限分两档：`member` 只要在白名单里就能用，`admin` 需要 `shared_story.manager_accounts`；这个列表留空时，白名单里的用户都算管理员。

**配置备份**

配置的导出 / 导入不在聊天里，在 **WebUI 的插件页面「配置备份」**里（插件详情 → 页面 → 配置备份）。换机器、备份配置、给别人抄一份配置都用它。

- **下载配置备份**：把当前配置导出成一个 JSON 文件下载下来，同时落一份到插件数据目录的 `exports/`。
- **导入配置**：选文件 → 先看会改哪些项（改 / 增 / 删分别列出）→ 点「确认导入」才写盘。

导出的文件带着格式版本号，**以后升级插件仍然能导回来**：老格式的文件、没有格式头的裸配置、手写的片段、甚至更新版本导出的文件都认；缺的键自动补默认值。导入是**合并**——文件里没写的设置保持原值，不会被清掉，导入后立即生效、不用重启。

一个边界：AstrBot 的插件配置由 `_conf_schema.json` 定义，插件不认识的键写进去之后会在下次加载时被 AstrBot 清掉。这不影响你认识的任何设置，只是别把配置文件当成插件之外的通用存储。

## 与上游的差异

上游是 Koishi 的 TypeScript 插件，这里是 AstrBot 的 Python 插件。能逐字搬的全搬了（包括命令返回的中文文案），搬不了的都是**显式降级分支**，不留占位。

| 项目 | 上游（Koishi） | 本移植版 |
| --- | --- | --- |
| 宿主 | Koishi + OneBot / NapCat | AstrBot（主流平台都行，见 `metadata.yaml`） |
| 安装 | npm 包 / tgz | AstrBot 插件市场，或从仓库根安装 |
| 配置 | Koishi Console Schema | 插件配置页 `_conf_schema.json`，22 组，键名 snake_case |
| 命令 | `interlude.story.start` | `hdsi_story_start`（32 条，语义与文案不变） |
| 数据 | Koishi ORM | 自带 SQLite，13 张表，**列名与上游逐字一致**（camelCase） |
| 出站投递 | 直接调 `session.bot` | 统一收敛到 `Transport` 协议，做不到的走 `transport-unavailable` 降级 |
| 网页观察 | Puppeteer 服务 | 走 `Transport` 的 `search_web` / `visit_web`，由适配层提供 |
| 图像处理 | Puppeteer + sharp | `PIL`；没装就透传原图（可选依赖） |
| 桌面桥 | 与 Koishi 进程 `process.send` | 可选 HTTP 桥（`HDSI_DESKTOP_BRIDGE=1` 才启用） |
| 人格 | 上游 Console 手填 | 多一个 `story_defaults.persona_id`，可直接导入 AstrBot 人格 |
| 群表态 / 原生表情 | Satori 原生接口 | 平台不支持时返回失败，由调用方走既有的投递失败分支 |

几个已知的、刻意的行为差异（原因写在 `docs/PORTING_NOTES.md`）：网页观察落库前不传 `id`（自增主键不会被 `0` 占住）；范围查询退化成"取足够行 + Python 侧过滤"，遇到不支持的算子**显式抛错**而不是静默返回偏窄结果；`interlude_schedule_preplan` 换主键是 no-op（只能删旧行插新行）；群规则 `enabled` 缺省视为启用。

> 上游 README 里提到的 npm 安装、Koishi Console、Puppeteer、`interlude.*` 命令名都不适用于本移植版；上面这张表就是换算关系。

## 常见问题

**模型返回的不是合法 JSON**

先看 `logging.level=debug` 下的原始响应。两个方向：把该连接（或 `model_center.main_response_format`）改成 `prompt-only`，让插件用宽松提取去捞 JSON；或者换个模型——结构化输出能力直接决定这个插件能不能用，小模型经常输出一段中文分析而不是协议要求的对象。沙箱里想验管线本身，可以用 `scripts/sandbox_stub_model.py` 起一个"永远回合法决策"的桩服务，把插件的问题和模型的问题分开。

**`response_format=json_object` 在部分 Ollama 上会让请求崩**

有些 Ollama 版本/模型组合一收到 `json_object` 就 500，从日志上看是 runner 崩掉而不是模型答错。把该连接的 `response_format` 改成 `prompt-only` 即可。插件本身也有回落：带 `json_object` 失败后会用普通请求重发一次，再宽松提取 JSON，但源头改掉更干净。

**插件完全不回复**

按这个顺序排：

1. `logging.level=debug`，看有没有收到事件、有没有调模型。
2. `qq_access.enabled` 是不是没开，或者白名单是空的——**空白名单 = 私聊入口关闭**，这是刻意的默认值。
3. 故事建了没有：`/hdsi_status`。没有就先 `/hdsi_story_start`，或者打开 `runtime.auto_create`。
4. 模型连接：`endpoint` 必须是**完整的 Chat Completions 地址**（`.../v1/chat/completions`），只填 base URL 会 404。
5. 是不是 `blind_mode.enabled=true` 了——那会屏蔽全部管理命令，日志也收束成心跳。回配置页关掉再重载。

**为什么不回答我，明明看到了**

这是功能，不是 bug。看 `/hdsi_script` 就知道她那轮在干什么——剧本里那条"没回"的记录和她真发出去的话是同一份账本里的两件事。真想让她开口，给个具体由头比连发几条更管用。

**她回得太快 / 太慢**

`runtime` 的 `typing_base_delay_seconds`、`typing_characters_per_second`、`typing_max_delay_seconds` 控制拆分气泡之间的模拟打字时间，`typing_jitter_ratio`（默认 `0.3`）给它加随机浮动，填 `0` 就是固定延迟。自动推进的节奏在 `auto_advance_interval_minutes`、`conversation_follow_up_minutes` 和 `rest_windows` 里。

**想让她安静一点**

`runtime.allow_proactive_messages` 默认就是关的；真要开，`proactive_willingness_threshold` 决定多愿意才主动找你，`agency` 管的是"现在方不方便"。群聊那边用 `response_mode` 和每群的 `willingness` 控制。

## 隐私

- 密钥只存在 AstrBot 配置里，插件不写死、不外传。
- 数据落在本地 SQLite，除了你配的模型接口和 `browser` 里允许的域名，不发任何其它请求。
- `logging` 的剧本/消息内容预览默认关闭。

## 文档

- 逐字段配置对照：`docs/CONFIG_MAP.md`
- 命令全表：`docs/COMMANDS.md`
- 移植决策、刻意的行为差异与实测修复：`docs/PORTING_NOTES.md`
- 架构总览：`docs/ARCHITECTURE.md`
- 跟进上游更新：`docs/UPSTREAM_SYNC.md`
- 版本记录：`CHANGELOG.md`

## 许可

AGPL-3.0，与上游一致。
