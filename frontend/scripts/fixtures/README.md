# 前端侧的「生产金样」（fixtures）

这里放的是**从真实后端取下来的**响应样本，不是手写的近似形状。

## `sticker-inline-response.json`

`console/sticker-file?assetId=…&inline=1` 的真实响应（`ConsoleApi.sticker_file_inline()`
经宿主路由 `main.page_console_sticker_file` 出来的那份 JSON）：
`{assetId, mimeType, size, base64}`，`base64` 是一张**真 96×96 PNG** 的 base64
（真机 harness 会回放它并断言 `<img>.naturalWidth === 96`）。

> ⚠️ 键名是 `base64`，**不是 `data`**：宿主父页面递进 iframe 的是
> `response.data?.data ?? response.data`，叫 `data` 的那个键会被当成"整包"取走，
> iframe 只收到一条裸 base64 字符串 → 整屏「取不到图」。见 `docs/PORTING_NOTES.md` §45.8.1。

> 为什么必须有它：前端曾经"对着自己写的桩后端"验证缩略图能不能出来 —— 桩比生产更宽或更窄
> 都不算验证（`docs/PORTING_NOTES.md` §45.8、施工坑汇编里"夹具不许造生产不存在的东西"）。
> 现在这条链路两侧对账：**真后端产出金样 → 前端断言解析它 → 真机 harness 回放它**。

### 怎么重新生成（仓库根目录，一条命令）

```bash
python3 plugin/frontend/scripts/fixtures/regenerate-sticker-inline-fixture.py
```

它跑的是真实路由（底层 `ConsoleApi.sticker_file_inline`，用仓库自己的真内存库 + 真文件夹具），
写完会自检"四键齐、`base64` 逐字节等于原文件、确实是 PNG"。

### 什么时候该重跑

* 后端改了 `inline` 分支的字段名 / 字段集合 / MIME 取值 / base64 编码方式；
* `pnpm test:unit` 里的金样断言红了（`scripts/check-sticker-images.ts`）——那说明**夹具过期**，
  要么重新生成夹具，要么前端跟着后端改（两边必须同时在同一个 commit 里）。

金样是 `sort_keys` + 缩进 2 的稳定序列化，所以重跑后 `git diff` 为空 = 形状没变。
