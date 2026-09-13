# 更新日志

## v1.1.0

**新增配置导出 / 导入**（上游 Koishi 没有，本移植版独有）。

- `hdsi_config_export`：把当前插件配置导出成一个 JSON 文件，同时在聊天里发给你一份，
  并落到 `<插件数据目录>/exports/`。导出的是**磁盘上那份原样配置**（含你不认识的键），
  不做任何归一化。
- `hdsi_config_import`：引用（回复）那份文件后发送即可导入，也可以直接把 JSON 贴在命令后面。
  导入前会给出**变更预览**（改哪些、增哪些、删哪些）并要你回 y/n 确认。
- 导出的文件带 `format` / `formatVersion` / 插件版本 / 上游版本，所以**以后升级插件仍然能导回来**。
- 兼容契约（硬要求，见 `docs/PORTING_NOTES.md`）：
  - 带信封的新文件、早期版本没有信封的裸配置、手写片段、甚至更新版本插件导出的文件，**都能导入**；
  - 分组改名（`model_center` ↔ 上游 `model`）由配置别名统一处理，两种名字都认；
  - **未知键原样保留、缺失键补默认值**，绝不因为多一个键 / 少一个键拒绝整份配置；
  - 迁移链只增不改：将来任何键改名都往 `core/config_io.py` 的 `_MIGRATIONS` 里**追加**一步。
- 导入写回时统一转成 **schema 分组名**（`model_center` / `qq_access`），这样 AstrBot 配置页
  能正确显示导入后的值——写上游名会让配置页看起来"全是默认值"。
- 命令总数 32 → **34**（上游 32 条 + 本地新增 2 条），`docs/COMMANDS.md` 同步。

## v1.0.0

**全量移植上游 `1.0.1-beta6-rebuild`（v2 剧本内核）**。v0.1.0 的"核心叙事子集"被整体替换：这一版把上游 Koishi 插件从配置、数据层、提示词到投递账本逐模块搬到了 AstrBot 上。

上游这一代的中心思想是**以持续剧本为唯一叙事主链**：原始剧本保存生活与人物理解，实际通信由同一行动的投递结果确认；用户消息不是"一条要回答的消息"，而是进入主角生活的**真实外部事件**，她可以看见、可以不看、可以沉默、可以晚点回。

### 叙事内核

- **时间感知**：故事时区 / 本地时间 / 时段与日照预期 / 用户陈述的时间引用，作为主叙事的权威时钟。
- **七段式上下文**：`storyIdentity → relevantEstablishedEpisodes → currentSceneEvidence → ongoingThreads → availableNearFuture → incomingEvent → authoringWindow`，附 cache-first 紧凑投影。
- **剧本提交**：一次写作产出 `ScriptCommit`（正文 + 事件 + 场景增量），宿主提交带 `frameId`/`burstId` 的确定性身份。
- **正文内 <say> 行动**：模型在正文里用 `<say id="…">原话</say>` 标记真实台词，宿主解出原话、按投递规范化发送，引用失配时保守兜底，绝不凭空造第二条回复。
- **SceneFrame / DialogueBurst**：当前场景的可重建导航（每一格都带来源条目 id），同一 scene 的消息共享身份，时间间隔不改变它。
- **投递账本**：每个已提交行动在剧本条目 metadata 内建 M6.1 账本，私聊/群聊/拆分气泡/贴图/原生表情/群表态共享 `commitId`/`eventId`，平台结果按 segment 回写 `pending | delivered | partial | failed | cancelled`；已 delivered 是终态，晚到的记账不得降级。
- **lifeHandoff**：用本轮原文引用交接地点、活动、在场与已完成小事，只作导航，不是第二份场景摘要。

### 记忆与内在

- **长期事实与证据**：事实带 `knowledge`（观察/转述/想法/提议/条件/确认 + 原文引用 + 认知主体），区分"她相信的事"与"实际沟通结果"。
- **记忆分层**：原始剧本 → 场景/弧摘要 → 长期事实 → continuity 快照（历史兼容）→ Overlay（稳定演化）→ Perspective（外壳人格层）→ active consequence。
- **场景压缩**：条目超阈值后台增量压缩，顺带更新在场表、working details、人物倾向候选。
- **Alter System**：可消退的内在天气，按来源分桶累计、按阈值触发侧端分析、带冷却与权重衰减。
- **Agency Window**：日程 / 隐私 / 设备构成的外部行动容量，只约束联系行动，不控制文风与情绪。
- **Urge**：以真实消息热度与已提交原文的 urge/slow 交接统一调度下一次推进（默认关闭，四档 + 自定义）。
- **Schedule Preplan**：未来近期的计划结构，程序保存多日，主叙事只读未来约半天，且不得把计划当已发生事实。
- **群聊意愿层**：半衰减分数 + 边际递减 + 阈值概率 + 成功发言成本，决定普通群消息是否值得进主模型；@ 机器人绕过概率门。
- **承诺回访**：用户回合里说出口的"想想再答复"落成带到期时间的 intent，到期必须给出可见履行、延期说明或取消说明。

### 能力

- **模型中心**：任意多条 OpenAI 兼容连接 + 任务级路由（`main` / `compaction` / `timeline` / `alter` / `embedding` / `stickers` / `vision`）、failover、round-robin、智谱官方 / DeepSeek 思考 / 百炼区域等预设模式、token 用量与费用统计。
- **多模态**：原生图片输入、QQ 语音与音频文件、侧端视觉描述模型、本地表情库（含语义检索与描述生成）。
- **网页观察**：模型提出浏览意图 → 宿主在并发闸门内取回文本摘要 → 作为上下文进入后续回合（能力由平台适配层提供）。
- **多平台**：私聊与群聊入站统一走适配层，`metadata.yaml` 的 `support_platforms` 覆盖 AstrBot 主流平台。
- **盲区模式**（上游原"失明模式"）：开启后不注册管理命令、运行日志收束为周期心跳。
- **桌面桥**：桌面端用的时间线 / 游标 / 暂停恢复等命令，本移植版做成可选桥。

### 数据

- **13 张 SQLite 表**，列名与上游逐字一致（`camelCase`），含增量补列与热重载不重建表的升级路径。
- **状态信封 v4**：版本化编解码 + 幂等升级，未知顶层键搬进 `extensions` 保留而不是写回时丢失。

### 命令

上游全部 HDSI 管理命令已移植（`interlude.story.*` / `participant.*` / `admin.*` / `memory.*` / `schedule.*` / `desktop.*` / `blind.*` 等）。
AstrBot 命令名不能带点号层级，因此统一改为下划线形式并保留原语义与**逐字中文返回文案**，
完整对照表见 `docs/COMMANDS.md`。

## 实测修复

移植期间在真机/实测中暴露并修掉的问题（都写进了 `docs/PORTING_NOTES.md`）：

- **AstrBot `_conf_schema.json` 不是标准 JSON Schema**：顶层直接是字段表；`type` 只能用 `string/text/int/float/bool/object/list/template_list/file`；`object` 子字段用 `items` 而不是 `properties`；文件带 BOM。写错会报 `TypeError: string indices must be integers`。
- **`on_private_message` 里事件属性必须用 getter**：`event.self_id` / `session_id` 在真机 `AiocqhttpMessageEvent` 上不存在，要用 `get_self_id()` / `get_session_id()` / `get_sender_id()` / `get_platform_name()`。
- **sqlite 单连接不是线程安全的**：`Database.write` 走 `asyncio.to_thread`，service 层又会 `asyncio.gather` 并发读，无互斥时实测 6 路并发读 × 200 轮约 7.5% 抛 `sqlite3.InterfaceError: bad parameter or other API misuse`。现由每个 `Database` 实例的可重入锁把**每一次**连接操作串行化（1200 次并发读 0 失败）。
- **`WeakSet` 语义不能在 Python 里用裸 `id()` 集合替代**：对象被 GC 后 `id` 立刻复用，会留下失效标记 → 本应沉默的回复被放行、或回复直接丢内容。改用带强引用 pin 的标记注册表。
- **JS 与 Python 的语义差异**（逐条实测对齐）：`Math.round` 是半值向上（Python `round` 是银行家舍入）；`isRecord({})` 在 JS 为真；`matchAll` 里 `[^\d]` 会吃掉一个字符而 Python 前瞻不吃；`??` 与 `||` 对显式 `0` 的语义完全不同（`userMessageDebounceSeconds ?? 2` 写成 `||` 会让"显式 0 = 不防抖"失效）；`Number.isSafeInteger(Number(null))` 为真。
- **测试的跨模块顺序污染**：`'astrbot' not in sys.modules` 这类全局断言会因别的测试先导入 astrbot 而误报；改为在干净子进程里只导入本包再检查。

## 与上游的差异

见 `README.md`「与上游的差异」。原则是：**行为以能跑通为最高优先级，凡是无法在 AstrBot 复现的能力都走显式降级分支并记录**，不留占位。
