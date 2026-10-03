#!/usr/bin/env python3
"""重新生成「生产金样」：`fixtures/sticker-inline-response.json`。

前端拿不到真宿主，所以取图的形状必须**对真实后端**取一次并落成固定夹具，
否则前端就只是在"对着自己手写的桩"验证自己（踩过的坑：桩比生产更宽/更窄都不算验证）。

这个脚本跑的是**真实路由** `main.page_console_sticker_file`（经宿主响应桩），
底层是 `ConsoleApi.sticker_file_inline()`，用的是仓库自己的测试夹具（真内存库 + 真文件）。

一条命令（仓库根目录）：

    python3 plugin/frontend/scripts/fixtures/regenerate-sticker-inline-fixture.py

跑完 `git diff` 里那份 json 有任何变化 = 后端形状变了，前端 `src/sticker-images.ts`
与 `pnpm test:unit` 里的金样断言必须一起看（形状冻在 `docs/PORTING_NOTES.md` §45.8）。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import unittest

#: 一张**真** PNG（96×96 猫爪图）。必须真能被浏览器解码：
#: 真机 harness 会回放这份金样，并断言 <img> 的 naturalWidth === 96。
PNG_BASE64 = (
    'iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAIAAABt+uBvAAABYklEQVR42u3dQQ7DIAxEUe5/wO57jW6mJ2jSCGzG5o/YF722EgHjjM/rzbgYAwKAAAIIIIAuh37nRCDNpS2QVqcJkOJTFUi5qQSkfXEHkkdMgeQULyC5xgJI3tkMpArZBqQ62QCkakkFUs0kAalywoFUP4FA6pIQIPUKQLlA6phlQOobgOKB1D0ARQLpjBQAMvz0eyCfxcjGmWwDcntINgLy3MN8DGS7i548t1Qg8yOmzUBFqyFG6UqMhHkCZABUqLwGID+gWrVrAK0AKvT/SpjwqP7ziZ4zQAABBBBAAAF00jqIlTRAPM0D1AqIHcXzgDjV4FzMEqjVySpn81R3eAM9Yto4EyrMbr4nahTngKhyBWgaiEp7gLjtM7HI4L4YQGlA3Fnl1jP35hPuzdN5gd4ddH+hf9DCPUw6UNHDjC54QTT0UaQTJ71cQ2noBkw/aTqS09P+yJ72vBUBIIAYAP07vqyYVPuxkHCXAAAAAElFTkSuQmCC'
)
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sticker-inline-response.json')
ASSET_ID = 'sticker-fixture-1a2b3c4d'


def main() -> int:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(FIXTURE), '..', '..', '..', '..'))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from plugin.tests.test_console_api import ConsoleStickerLibraryTests  # noqa: E402  导入即装桩

    class Golden(ConsoleStickerLibraryTests):
        def runTest(self):  # noqa: N802 - unittest 约定
            pass

    case = Golden()
    case.setUp()
    try:
        body = base64.b64decode(PNG_BASE64)
        case._insert(ASSET_ID, body=body)
        response = case._page(assetId=ASSET_ID, inline='1')
        assert response.status_code == 200, response
        payload = response.payload

        # 金样的自检：形状/字节都要对得上，否则写出去的就是一份错夹具。
        #: 键名是 `base64` **不是** `data`（改回去 = 真机整屏缩略图又挂，见 §45.8）。
        assert set(payload) == {'assetId', 'mimeType', 'size', 'base64'}, sorted(payload)
        assert payload['assetId'] == ASSET_ID, payload['assetId']
        assert payload['mimeType'] == 'image/png', payload['mimeType']
        assert payload['size'] == len(body), (payload['size'], len(body))
        decoded = base64.b64decode(payload['base64'])
        assert decoded == body, 'base64 必须逐字节等于原文件'
        assert decoded[:8] == b'\x89PNG\r\n\x1a\n', '得是一张真 PNG'
    finally:
        case.tearDown()

    with open(FIXTURE, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write('\n')
    print('已写入 %s（%d 字节 base64，原图 %d 字节）' % (FIXTURE, len(payload['base64']), payload['size']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
