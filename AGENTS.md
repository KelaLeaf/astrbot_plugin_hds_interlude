# HDS Interlude — 项目说明（给 Agent 的提示文件）

> 本文件是给未来接手本项目的 Agent 看的。记录项目怎么组织、各部分干什么、**功能完成度、踩过的坑与解决办法、协作注意事项**。开新聊天先读这里，避免从头摸。

## 这个项目是什么

把 **HDS Interlude（幕间系统）**——原本是 Koishi(TypeScript) 的「时间感知持续叙事聊天框架」——**全量移植**成 **AstrBot(Python) 插件**。

- 上游（Koishi）：https://gitee.com/MomoiCore/hds-interlude （`1.0.1-rc28`，快照 `cc3452d`）
- 发布仓 https://github.com/KelaLeaf/astrbot_plugin_hds_interlude （public，仓库根=插件根）
- 工作仓 https://cnb.cool/cerasusdream/ai/hds-interlude （私有）
- 插件版本 `v1.7.9`（沿革见 `plugin/CHANGELOG.md`；**v1.7.7 正文语音标记；v1.7.8 输入状态逐条亮灭**）；**上游版本号**记在 `plugin/core/meta.py` 的 `HDS_INTERLUDE_VERSION`（与插件自身版本是两回事）

> GitHub 是公开**发布**仓（内容 = `plugin/`，仓库根**就是**插件根）；cnb 是私有**工作/迭代**仓。别往公开仓推任何密钥/私有路径。

移植的契约文件是 `docs/PORT_PLAN.md`（模块映射 + 键名法 + DoD），移植过程中暴露的受控偏离记在 `docs/PORTING_NOTES.md`。

## 目录结构（本仓库根）

```
hds-interlude/
├── AGENTS.md            ← 本文件。项目地图 + 完成度 + 高频坑索引 + 注意事项
│                          （坑 1–73 全文在 docs/PORTING_NOTES.md「施工坑汇编」）。
├── README.md            ← 仓库根说明（给开发者/维护者）。
├── upstream/            ← 【只读】上游 Koishi 快照（1.0.1-rc28）。跟进上游用，不改。
├── .gh/                 ← GitHub 公开仓本地工作副本（已 .gitignore）。⚠️ 见「同步坑」
├── .cnb_repo/           ← cnb 私有仓本地工作副本（已 .gitignore）
├── plugin/              ← 【主战场】AstrBot 插件本体。
│   ├── main.py          ← 插件入口（Star 子类）：事件监听、38 条命令、y/n 确认、控制台页 API 注册、生命周期
│   ├── metadata.yaml    ← 元数据（support_platforms 覆盖主流平台）
│   ├── _conf_schema.json← 配置 schema（AstrBot 格式！39 个顶层分组，含 17 个隐藏兼容位）
│   ├── requirements.txt ← httpx、pyyaml（Pillow 可选，降级）
│   ├── core/            ← 叙事核心（**不 import astrbot**，可独立测试）
│   │   ├── service/     ← 组装层（18 文件）：base 底座 / chunk0..14（chunk0..9 按上游旧行段切分，
│   │   │                    chunk10/11 = rc28 服务层，chunk12/13 = 平台动作/空间，chunk14 = works）/
│   │   │                    config / helpers / session / transport / desktop(
│   │   ├── script/      ← 剧本中枢（18 文件，3822 行）：contract / commit_builder / validator /
│   │   │                    delivery_ledger / scene_frame / context_compiler / life_handoff …
│   │   └── *.py         ← 19 个顶层模块（12468 行）：narrator / types / database /
│   │                        schedule_preplan / alter / logging / story_state / urge …
│   ├── adapters/
│   │   └── astrbot_bridge.py ← AstrBot 事件/消息链/出站投递 ↔ core 桥接
│   ├── frontend/        ← 控制台前端源码（Vite + Preact + Tailwind v4）
│   ├── pages/console/   ← WebUI「幕间控制台」= frontend/ 的**构建产物**（别手改）
│   ├── .astrbot-plugin/i18n/ ← 插件文案（zh-CN / en-US），页面走 `pages.console.{title,description,nav}`
│   └── tests/           ← 2200+ 项单元测试（stdlib unittest）
├── docs/                ← PORT_PLAN / PORTING_NOTES / CONFIG_MAP / COMMANDS / ARCHITECTURE …
├── scripts/             ← sync_upstream.sh（拉上游快照）/ sandbox_stub_model.py（桩模型）
├── persona_凌梦.json    ← 凌梦人格示例（AstrBot 人格格式）
└── .cnb.yml             ← cnb 流水线（1 核 python:3.11-slim）
```

规模核对：`plugin/core/` 顶层模块 + `service/`（18 个）+ `script/`（18 个）。

## 已实现功能（全量移植）

上游 `1.0.1-rc28` 的能力（P1+P2+P3，见 `PORTING_NOTES.md` §31/§32/§33）**全部**搬到了 AstrBot 上，不是"核心叙事子集"。
逐项清单在 `docs/ARCHITECTURE.md`，用户向说明在 `plugin/README.md`。

- **叙事**：持续剧本为唯一主链（`ScriptCommit` 带 `frameId`/`burstId`）；时间感知；七段式上下文 +
  `cache-first` 紧凑投影；正文内 `<say>` 行动（失配时保守兜底）；SceneFrame / DialogueBurst；
  投递账本 M6.1（`delivered` 是终态）；lifeHandoff；承诺回访。
- **记忆与内在**：长期事实 + 证据（区分"她相信的"与"实际结果"）；五层记忆；后台场景压缩；
  Alter System；Agency Window；Urge（默认关）；Schedule Preplan；**共享主剧本硬开启（坑 37）**；
  群聊意愿层（@ 机器人绕过概率门）。
- **能力**：模型中心（连接池 + 任务级路由 + failover + 用量）；**模型来源双轨**（连接行 / 指名
  AstrBot Provider，按 `modalities` 校验）；多模态（图片 / 语音 / 侧端视觉 / 本地表情）；网页观察；
  多平台；盲区模式（真把 handler 从 `star_handlers_registry` 移除）；桌面桥；人格导入。
- **控制台**（`pages/console/`，产物进仓库，源码 `plugin/frontend/`）：十五个面板（v1.8.0 起含「表情库」）+ 剧本切换器（标「主剧本 / 旧剧本」）。
  **能改的只有**运行开关（10 项）、模型连接池（密钥永不回显）、配置页全量编辑器（门禁＝整份 schema），
  外加 `console/story-promote` / `console/story-merge`；写操作统一走 `Bridge.save_raw_config()`。
- **数据与配置**：20 张 SQLite 表（列名 camelCase，增量补列）；状态信封 v4；39 个顶层配置分组
  （逐项对账 `docs/CONFIG_MAP.md`；**提示词四件套只在 `prompts` 组**，坑 33）。
- **命令**：上游 `registerCommands()` 的 **38 条**全移植，逐字中文文案保留，只把点号层级换成本地化名；
  管理命令同时认上游 `interlude.*`。表在 `plugin/main.py` 的 `COMMANDS`，人读版 `docs/COMMANDS.md`，
  两侧双向断言。配置导入导出是插件页面、**不是命令**；危险操作走 `_ask_confirmation()`。

## 未实现 / 与上游的差异

**没有"未实现"**：上游能力已全部落地；确实无法在 AstrBot 复现的一律走**显式降级分支**（不留占位、不留 TODO）。刻意的行为差异（逐条依据见 `docs/PORTING_NOTES.md`）：

1. **出站平台调用统一走 `Transport`**：上游直接 `sendPrivateMessage`，找不到账号抛 `bot-not-found`；这里表达为 `transport-unavailable`（`NullTransport`）。
2. **网页观察**走 `Transport` 的 `search_web` / `visit_web`（上游是 Puppeteer）；宿主没有插件级搜索 API → 按 `browser.search_url_template` 抓一页，模板要选服务端渲染的（见 `PORTING_NOTES.md` §23）。
3. **图像降采样 / 动图抽帧用 `PIL`** 代替 Puppeteer + sharp；`PIL` 不可用时**透传原图**并记 debug（不阻断投递）。
4. **`interlude_web_observation` 落库前省略 `id`**（本移植版把显式 `0` 当真实主键）。
5. **范围算子查询退化**为「先取足够行 + Python 侧过滤」：`db_get` 遇到 `where` 里的算子**显式抛 `NotImplementedError`**。
6. **`interlude_schedule_preplan` 换主键是 no-op**（`update` 会剥主键列）。
7. **群规则 `enabled` 缺省即启用**：schema 不为 list 元素补默认值，照抄 `!!group?.enabled` 会把"填了群号没填 enabled"的群整体拒收。
8. **群表态 / 部分原生表情**在平台不支持时返回失败，由调用方走既有投递失败分支。
9. **桌面桥**：上游挂在 Koishi 进程 `process.send` 上，这里改成可选 HTTP 桥（`HDSI_DESKTOP_BRIDGE=1` 且插件配置里启用才生效）。
10. **命令名本地化**：三处"引导用户执行命令"的中文文案里，上游命令名换成了本插件命令名，其余中文逐字未动（`CommandCopyLocalizationTests` 整串相等钉死 + AST 兜底扫描）。

## 踩过的坑 & 解决办法（务必记，都是真机/实测暴露的）

> **坑 1–73 已下沉到 `docs/PORTING_NOTES.md`「## 施工坑汇编（自 AGENTS.md 下沉，编号不变）」（编号不变、措辞逐字保留）。**
> 每条坑的现场 / 根因 / 修法 / 自查清单都在那边（原文按 `**配置与 AstrBot API**` 等分组小标题排布）。
> 引用键照旧是「坑 N」——`AGENTS.md`、`plugin/tests/**` 的注释、`PORTING_NOTES.md` 内部互相引用都用它。
> 这里只留**最常踩的高频项**；每句后面括号里的编号就是去那边查全文的钥匙。

### 最常踩的（动手前先看这些）

- **配置段位 / 键名法**（坑 8、9、33、34、62、72、73）：段位按上游——`audio` 读 `model.audio`、
  `stickers` 挂**顶层**、提示词四件套分组名是 `prompts` 而 core 读 `model.*`（`resolve_prompt_fields`
  读 / `to_schema_shape` 写）。**读的段位必须等于写的段位**，否则界面显示与真实行为相反。
  加配置键**三处同改**：`_conf_schema.json` + `test_configuration.py` 对账表 + `docs/CONFIG_MAP.md`。
  分组改名 / 合并：新组进 schema，旧组原样留 + `invisible: true`，`LEGACY_SECTION_MERGES` 做 N:1
  读取归并，**写方向只写新路径**。
- **schema 重建**（坑 22）：AstrBot 每次加载都按 `_conf_schema.json` 重建配置，schema 里没有的键会被删。
  「未知键原样保留」是我们这条链路的性质，**不是宿主的承诺**——新增配置键必须同时写进 schema，
  否则下次加载就没了。推论：「用户写过」只能定义成 **值 ≠ schema 默认值**，不能拿"新组优先"当规则（坑 72）。
- **两类拼写**（坑 41、46、53、65）：DB 列名 camelCase / `metadata` 键 snake_case / wire payload 与
  「模型原样返回的 JSON」camelCase。跨 chunk 传参与模型入口**一律双读**；解析 / 归一化只写一份拼写时，
  另一份别名会指回**解析前**的旧对象（`setdefault` 保证"存在"，不保证"同一对象"，要 `_resync_dual()`）。
- **`ctx` vs `context`**（坑 44、v1.6.0 段）：适配层手上是 `ctx`，`ctx.base_dir` 才是合法路径来源；
  读生产上不存在的 `self.context` 会让权限表**静默失效**（不报错，只是被忽略）。
- **夹具不许造生产不存在的东西**（坑 39、46、66）：夹具必须用**生产写入方**的写法造；
  改 schema 分组名时夹具也要改，否则「用旧名 71 项照样全绿」；断言"两个来源相等"抓不到"两边都错"，
  关键 id 要断言具体形状（`assertNotIn('None', ...)`）。测试替身要比生产**更严**、不能更宽。
- **投递纪律**（坑 25、44、45、48、56）：出站 UMO 第一段只能是宿主的**平台实例 id**
  （`get_platform_id()`），不是我们归一化的平台名；拿不到坐标要打可见 warn，别退化成"看起来发了"。
  上游 `next()` 在 AstrBot 里 = "让第二个人格替你答"，不是"我不管了"。**需要用户一定看见的
  （能力缺失 / 丢内容 / 降级）必须用 `warn`**；`diagnostic` 频道的报告一律当"没有报告"，
  判定不通过的分支不许裸 `return`。
- **别改测试将就实现**；`chunk3` / `chunk9` 有「成员清单铁律」（`vars(ServiceChunkN)` 精确相等），
  往这两类加方法会直接失败——新逻辑要么**内联**、要么放进新 chunk（坑 61）。

## v1.6.0：平台动作层 + QQ 空间

上游没有"动作层"；本移植版把"她能对 QQ 做什么"做成目录驱动的一层（**62 个动作**）：
`core/platform_actions.py` 是**唯一事实源**（id / 类别 / 参数 / 风险 / 默认档位），提示词注入、
参数校验、权限判定、控制台「动作」页与 `docs/CONFIG_MAP.md` 全由它派生。执行侧在
`service/chunk12.py`（可用集 = 配置开关 ⊗ 权限表 ⊗ 会话身份，结果写回剧本留痕），
QQ 空间在 `service/chunk13.py` + `core/qzone.py`（上游 `qzone.ts` 移植）。
权限表 `action_permissions.json` **独立存放**（塞进 schema 的 object 会"保存即失效"，坑 22）、**只按 `ctx.base_dir` 读**（读生产没有的 `self.context` = 权限被静默忽略，坑 44）；
17 个危险动作默认关，开关落在各自类别组里（v1.7.3 起不再有独立风险组），每个开关的 hint 逐字 = `此标签下功能具有一定风险，易误操作，请谨慎开启。`；
**主动加好友 / 主动加群刻意不做**（只有被动同意/拒绝，红线断言钉着）。
QQ 空间由隐藏兼容位 `qzone_compat` 转正为 `qzone`，旧文件走只读的 `LEGACY_SECTION_ALIASES`。

**v1.7.0 补齐 P3**（详见 `PORTING_NOTES.md` §33）：`core/forward_message.py`（合并转发三重预算，
读不到只留线索不吞消息）、`core/anthropic.py`（Anthropic Messages 协议翻译层，与叙事解耦，
连接行 `protocol` 选协议、旧配置零回归）、`core/works.py` + `service/chunk14.py`（共同作品：
CAS + 坏行硬报错 + 滚动剪枝，**上游自己没接线，入口是本移植版补的**，默认关闭）。配置分组 39 个、
表 20 张，P3 再无隐藏兼容位。

**v1.7.1 换通道 + 标注**（详见 `PORTING_NOTES.md` §34）：空间动作（含只读的看说说/看动态）
**优先 NapCat WebSocket 方案**——`get_cookies`（域 `user.qzone.qq.com`）+ `get_login_info` 取得登录态，
由 `p_skey` 算 `g_tk`（djb2，初始 5381）直接打 QZone CGI，SnowLuma 只作**回退**；只读不占配额，
转发计入评论配额。NapCat 专属由 `PlatformAction.backends`（顺序 = 优先级）+ `napcat_only` +
`napcat_actions()`（现 9 条）标注，控制台「动作」页打徽章 + 「只看 NapCat 专属（N）」筛选。
**v1.7.6 口径**：CGI 拿不到响应 → `unknown` 不重试（`code != 0` 才是 `failed`）；`ugc_right` 与 `visible` 同用五档中文枚举；专属清单只认 `napcat_actions()`（§39）。

**v1.7.2 三处配置/权限收口**：
① **动作适用范围要写实**——原先 61 条动作全填 `scopes=('private','group')`，于是"踢人/禁言/群文件"
在私聊回合也会被教给模型。现在 29 条仅群聊、1 条仅私聊（好友消息历史）、31 条两者皆可；
`permission_tiers_for()` 据此下发档位（**非群聊动作不下发「仅群管」**，选了等于关掉），
写权限时拒绝不适用档位、手改权限表塞进去则收敛成默认档（不是静默锁死）。
② **语音可选宿主 TTS**：`robot_actions.chat.tts_provider_id`（`_special: select_provider_tts`）；
**指名却找不到就失败，不回落**（同"模型侧指名 Provider"的纪律），只有宿主根本没暴露 TTS 列表才回落。
③ **动作开关组 10→4→3→1**（v1.7.4 起是一个 `robot_actions` 父组 + 三个子组），
旧组全部 `invisible: true` 留在 schema 里 + `LEGACY_SECTION_MERGES` 做 N:1 读取合并（见坑 72）。

> 本段原有的编号坑 **72 / 73 / 65 / 66 / 67 / 68 / 69 / 70 / 71** 已下沉到 `docs/PORTING_NOTES.md`「施工坑汇编」（编号不变）。

## rc28 跟进（v1.5.0）要点

上游 `1.0.1-beta6-rebuild` → `1.0.1-rc28`（11 个新模块、5 张新表、33→40 条命令）；新增
`specialization` / `world_seeder` / `endpoints` / `health` + `chunk10/11`，受控偏离见 §31。

> 本段原有的编号坑 **59 / 60 / 61 / 62 / 63 / 64** 已下沉到 `docs/PORTING_NOTES.md`「施工坑汇编」（编号不变）。

## 本地实测环境（复现/调试用）

- 本机 `<本机第二份 AstrBot>`（uv 装的 Python 3.11 venv，WebUI `http://localhost:6185`）与 `<本机 AstrBot 根>` 各一份 AstrBot；Ollama 主模型 `192.168.1.9:9`（qwen2.5-vl-abliterated，智商偏低，够测管线）、向量 `192.168.1.9:9`。
- 本地开发机 Python **3.13.5**；CI 用 `python:3.11-slim`，两边都要能跑。
- 小模型只给中文分析、不给协议 JSON 时，用 `scripts/sandbox_stub_model.py` 起桩做端到端验证，并把最后一次请求体存下来核对 payload 形状。
- **NapCat 接口文档（核对动作名/参数的唯一权威）：https://napcat.apifox.cn/llms.txt**（索引；每条对应 `<id>.md`，OpenAPI YAML，`paths:` 即 action 名）。靠它纠正过：`delete_group_file` **无** `busid`、`move/rename_group_file` 的 `current_parent_directory` **必填**；NapCat 原生只有发/删说说（评论/点赞/看动态走 QZone CGI，见 §34）。

## 协作注意事项（从聊天中总结）

- **用户要的是"对等密友"不是"请示型助手"**：自己拿主意、独立思考执行，别问"需不需要 XX"这种没主见的问题。
- **README 要贴近真实人类、参考上游/社区写法**，别写"AI 味"的合规声明；公开 README 以上游 `upstream/README.md` 为蓝本改。
- **配置项标题要简洁**，description 不放冗长 URL（示例放 `hint`）。
- **CHANGELOG 一条更新一句话**（用户明确要求）：受众是用户，不是提交者——只写"适配 SearXNG"这种结论，细节（实测数字、内部机制）进 `docs/PORTING_NOTES.md` 与 AGENTS，别堆在 CHANGELOG 里。
- **文档分工**：`plugin/README.md`（面向用户，像人写的）／`README.md`（仓库总览，给维护者）／`docs/*` 与 `AGENTS.md`（给 AI 和维护者看的工作笔记，可以很直白）。
- **隐私红线**：不往源码/公开仓库写真实令牌、**用户的内网地址与端口**、私有路径；配置示例一律写占位（`http://<地址>:<端口>/search?q={query}`），测试夹具用通用地址（`192.168.1.9`）——`plugin/tests/test_privacy_redlines.py` 会扫发行文件，出现私网字面量直接失败。`git remote` 不带凭据；推送用 `-c credential.helper=` 一次性 PAT，推完检查 `.git/config` 与 `.git/` 无残留。
- **同步坑**：往 `.gh` / `.cnb_repo` 同步时**必须把这组排除项带全**，`rsync --delete` 才不会误删 `.git` / 灌进垃圾：

  ```bash
  rsync -a --delete \
        --exclude='.git/' --exclude='node_modules/' --exclude='__pycache__/' \
        --exclude='*.py[cod]' --exclude='hdsi_db_*/' --exclude='delivery_endpoints.json' \
        --exclude='data/' --exclude='*.log' --exclude='release/' \
        <源>/ <目标>/
  ```

  - 不带 `--exclude='.git/'` 会把工作副本的 `.git` 一起删掉（已经踩过一次，得重新 clone 才恢复）。
  - 漏 `node_modules/` 会让 `.gh/frontend` 与 `.cnb_repo/astrbot_plugin_hds_interlude/frontend`
    **各多出 86 MB**（实测两处共 172 MB，全是没排除 rsync 的产物）。更要紧的是 `.gh/.gitignore`
    原先没有 `node_modules/`，一次 `git add -A` 就能把 86 MB 推上公开仓——现已补上。
  - 漏 `hdsi_db_*/` 曾把 **100 个测试 sqlite**（每个 80 KiB）提交进公开仓（见下方「体积」一节）。
- **`.cnb.yml` 的测试脚本已修**：改成 `python -m unittest discover -s plugin/tests -t .`（`test_core.py` 已不存在）。
- **测试在两种仓库布局下都能跑**（`plugin/tests/__init__.py` 里有一段引导）：
  - 开发工作区 / CNB 工作仓：`python3 -m unittest discover -s plugin/tests -t .`（CNB 仓里是 `-s astrbot_plugin_hds_interlude/tests -t astrbot_plugin_hds_interlude`）
  - GitHub 发布仓（仓库根 **就是**插件根）：`python3 -m unittest discover -s tests -t .`
  发布仓里没有 `docs/` 与 `upstream/`，所以 `CommandTableTests`（对账 `docs/COMMANDS.md`）与
  `ReleaseConsistencyTest`（对账 `docs/*` 与 `upstream/package.json`）会**整体跳过**——它们是工作区
  一致性检查，不是插件行为。别把 skip 当成失败。
  - ⚠️ **别在 `.gh/` 里直接跑测试**：目录名以点开头，`tests/__init__.py` 推出的包名会变成 `.gh`，
    `importlib.import_module('.gh.core.x')` 抛 `TypeError: the 'package' argument is required …`，
    看起来像 11 个模块集体炸掉（连同 `test_no_astrbot_import_in_core`），其实**只是目录名不合法**。
    要按发布仓布局验证就把 `.gh/` 拷成正常名字的目录再跑（`~/.dsh/cache/hdsi/gh-verify`），
    或从 GitHub 重新 clone。

## 怎么跟进上游更新

1. 读 `docs/UPSTREAM_SYNC.md`（流程 + 版本对应表），跑 `scripts/sync_upstream.sh` 更新 `upstream/` 快照（放仓库**外**的工作目录，快照不留 `.git`）。
2. 先读上游 `docs/CHANGELOG.md` / `docs/ARCHITECTURE.md`（比读 diff 快），再对照 `upstream/src/*.ts` 与 `upstream/test/*.test.ts`（**测试是行为的权威**）。
3. 按 `docs/PORT_PLAN.md §1` 的映射改 `plugin/core/**`，上游测试逐条移植到 `plugin/tests/**`；提交注明上游版本号，并同步 `plugin/core/meta.py` 的 `HDS_INTERLUDE_VERSION` 与 `docs/UPSTREAM_SYNC.md`。

**键名法是硬约束**（`docs/PORT_PLAN.md §2`）：发给模型的 payload 与数据库列名**逐字保上游 camelCase**（改一个字母协议就断），Python 内部结构与标识符用 snake_case，读外部输入时两种拼写都认、优先 camelCase。

## 关键约定

- `plugin/core/` **不得 import astrbot**（53 个模块全部遵守）；只有 `plugin/adapters/` 与 `plugin/main.py` 可以 import。
- `main.py` 不 import `core/` 的私有实现细节，**一切经 bridge**。
- 配置集中在 `_conf_schema.json`（AstrBot 格式），不硬编码密钥；配置段位按上游（例如 `audio` 读 `model.audio`，而 `stickers` 挂在**顶层**——顶层 `audio` 是错误段位，永远读不到；`audio.enabled` 默认关、core 早接了闸（§39.3））。**例外只有提示词四件套**：分组名 `prompts`，core 读的仍是 `model.*`，两边由 `resolve_prompt_fields` / `to_schema_shape` 自动搬运（见坑 33）。
- DB 列名 camelCase / `metadata` 键 snake_case / wire payload camelCase —— 三者别混。
- **插件页面在 WebUI 里，不是聊天命令**：AstrBot 内置配置页由 `_conf_schema.json` 驱动、插不进自定义按钮，官方扩展点是**插件页面**（`pages/<名>/index.html` + `window.AstrBotPluginPage` bridge，支持文件上传下载）。本插件的是 `plugin/pages/console/`（**Vite 构建产物**，源码 `plugin/frontend/`），后端接口在 `main.py` 的 `_register_config_page_apis()` 注册、取数在 `adapters/console_api.py`（路由必须以插件名开头）。别再把配置功能加回命令。
- **配置的向后兼容是硬要求**：导出文件是**跨版本用户资产**。
  - 导入必须认：带信封的新文件、裸配置（`formatVersion = 0`）、手写片段、**更新版本导出的文件**（只警告不拒绝）；
  - **未知键原样保留、缺失键补默认值**，永不因为多一个 / 少一个键拒绝整份配置。
    边界见坑 22：落盘后 AstrBot 会按 schema 重建配置，schema 里没有的键会被删掉——所以
    **新增配置键必须同时写进 `_conf_schema.json`**，否则下次加载就没了。
  - `plugin/core/config_io.py` 的 `_MIGRATIONS` **只增不改**：将来键改名就往里**追加**一步，别删别改；
  - 导入是**合并**（以磁盘现有配置为底），不是替换；**深合并**，且**只叠文件里显式写出的键**——
    直接拿 `normalize_config(文件)` 去覆盖会把磁盘上用户改过的值用默认值顶掉（详见 `docs/PORTING_NOTES.md`「施工坑汇编」坑 18）；
  - 变更预览要拿「写盘后的目标」跟磁盘比，别拿文件原文比（否则全报成 `removed`）；
  - 落盘统一转 **schema 分组名**（`model_center` / `qq_access`）——写上游名会让 AstrBot 配置页显示"全是默认值"；
  - 详见 `docs/PORTING_NOTES.md` 的「### 10. 配置导出 / 导入」。
- **模型来源有两轨，优先级别记错**：① 任务指名了 AstrBot Provider（`main_provider_id` 等）→ 用它；
  ② 连接行填了 http endpoint → 直连；③ 都没有 → AstrBot 默认 Provider。全部留空时行为与历史版本逐字一致。
  适配层为此在 `HttpClient` 协议上加了**可选** `task` 参数（唯一扩展，普通 HTTP 传输忽略它），
  详见 `docs/PORTING_NOTES.md` 的「### 11. 模型来源」。
- **能力校验只信 Provider 自己声明的 `modalities`**：声明里没有该模态才丢内容并 warn；
  没声明（空 / 缺失）一律照常发送——"没声明"不等于"不支持"。详见坑 23–25。
- 命令表就是上游那 **38 条**，断言直接写死 `len(COMMANDS) == 38`；`docs/COMMANDS.md` 要与 `COMMANDS` 双向一致。
  要加本地命令，先想清楚它是不是该做成插件页面 / 配置项——聊天命令是给用户用的，不是给配置管理用的。
- 不引入第三方依赖（只有 `httpx`、`pyyaml`；`PIL` 可选且 try-import 降级）。
  **这条只管 Python 侧**；前端有自己的工具链（`plugin/frontend/`），但只引入**必需**的：
  运行时依赖只有 `preact` 一个（3KB），Tailwind 与 Vite 都是构建期。加前端依赖前先量体积
  （对照：同样面板外壳，HeroUI + React 实测 193KB gzip，本方案 29KB）。
- 测试是契约：上游断言逐条移植，**不允许改测试将就实现**。跑法 `python3 -m unittest discover -s plugin/tests -t .`（2300 项上下；发布仓布局会少几项，见上文）。
