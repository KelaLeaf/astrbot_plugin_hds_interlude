# HDS Interlude — AstrBot 移植版（开发仓）

> 聊天在幕前发生，生活在幕间继续。

把 [HDS Interlude](https://gitee.com/MomoiCore/hds-interlude)（一个 Koishi / TypeScript 的「时间感知持续叙事聊天框架」）全量移植成 **AstrBot 插件**（Python）。

- **上游（只读参考）**：https://gitee.com/MomoiCore/hds-interlude — `1.0.1-beta6-rebuild`
- **GitHub（public，发布）**：https://github.com/KelaLeaf/astrbot_plugin_hds_interlude
- **cnb（private，工作/迭代）**：https://cnb.cool/cerasusdream/ai/hds-interlude
- 当前版本：插件 `v1.0.0`，对应上游 `1.0.1-beta6-rebuild`

## 两个仓库怎么分

| | GitHub | cnb |
| --- | --- | --- |
| 角色 | 公开**发布**仓，给用户装插件 | 私有**工作**仓，日常迭代 |
| 内容 | `plugin/` 的内容（仓库根 = 插件根） | 整个工作区（含 `upstream/`、`docs/`、脚本） |
| 本地副本 | `.gh/`（已 gitignore） | `.cnb_repo/`（已 gitignore） |

公开仓里**不能有**任何密钥、令牌或私有路径。推送用一次性凭据，推完检查 `.git/config` 和 `.git/` 没有残留。

## 目录结构

```
hds-interlude/
├── plugin/                 # 【主战场】AstrBot 插件本体
│   ├── main.py             #   插件入口（Star 子类）：事件、38 条命令、生命周期
│   ├── metadata.yaml       #   插件元数据
│   ├── _conf_schema.json   #   配置 schema（AstrBot 格式，37 组）
│   ├── core/               #   叙事核心，70 个模块、约 51.4k 行
│   │   ├── service/        #     InterludeService：base + chunk0..14 + config/helpers/session/transport/desktop
│   │   ├── script/         #     剧本中枢：提交、投递账本、场景帧、证据、上下文编译
│   │   └── *.py            #     narrator / types / database / alter / agency / urge / memory…
│   ├── adapters/           #   AstrBot ↔ core 桥接与 Transport 实现
│   ├── tests/              #   1900+ 项单元测试（stdlib unittest）
│   └── CHANGELOG.md / README.md / requirements.txt / LICENSE
├── docs/                   # 移植契约、对照表、架构、同步指南（给维护者/AI 看）
├── upstream/               # 【只读】上游 Koishi 快照，不参与开发
├── scripts/                # sync_upstream.sh、sandbox_stub_model.py
├── AGENTS.md               # 项目地图：结构、完成度、坑、协作注意事项
└── .cnb.yml                # cnb 流水线
```

## 跑测试

```bash
cd hds-interlude
python3 -m unittest discover -s plugin/tests -t .        # 全量：1173 项
python3 -m unittest plugin.tests.test_agency -v          # 单模块
```

当前结果：**1173 项，全部通过，18 项按条件跳过**（未装 Pillow、依赖并行任务、需显式开关的基准用例）。

`plugin/core/` 不 import `astrbot`，所以整套测试不需要 AstrBot 环境，`python:3.11-slim` 一个核就能跑完。

## 想真正跑起来

- 插件怎么装、怎么配、有哪些命令 → [`plugin/README.md`](plugin/README.md)
- 逐字段配置对照上游 → [`docs/CONFIG_MAP.md`](docs/CONFIG_MAP.md)
- 命令全表（38 条）→ [`docs/COMMANDS.md`](docs/COMMANDS.md)
- 移植决策与刻意的行为差异 → [`docs/PORTING_NOTES.md`](docs/PORTING_NOTES.md)

## 许可

AGPL-3.0，与上游一致。
