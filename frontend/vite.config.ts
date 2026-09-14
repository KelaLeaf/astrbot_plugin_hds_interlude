import { defineConfig } from 'vite'
import preact from '@preact/preset-vite'
import tailwindcss from '@tailwindcss/vite'

/**
 * 产物直接落到 `plugin/pages/console/`——AstrBot 的插件页面就是「宿主托管
 * `pages/<页名>/` 这个目录」，所以这里不产出到默认的 `dist/`。
 *
 * `base: './'` 是硬要求：宿主只重写**相对**资源地址（`src` / `href` / CSS 的
 * `url()` / JS 的 `import()`），绝对路径 `/assets/...` 会被当成外部链接跳过，
 * 结果整页 404。
 */
export default defineConfig({
  base: './',
  plugins: [preact(), tailwindcss()],
  // `public/` 里的东西会被原样拷进产物目录——`_page.json`（AstrBot 的页面元数据）
  // 就放在那儿。**别手工往 `pages/console/` 里放文件**：`emptyOutDir` 每次构建都会清空它。
  publicDir: 'public',
  build: {
    outDir: '../pages/console',
    emptyOutDir: true,
    assetsDir: 'assets',
    // 页面在离线内网里跑：不要任何 data URI 之外的内联大资源，也不要 sourcemap。
    sourcemap: false,
    reportCompressedSize: true,
    chunkSizeWarningLimit: 300,
  },
})
