/* 配置备份页。只用 AstrBot 的插件页 bridge，不引任何外部资源。 */

const bridge = window.AstrBotPluginPage;

const el = (id) => document.getElementById(id);
const exportBtn = el('export-btn');
const exportStatus = el('export-status');
const fileInput = el('file-input');
const previewBtn = el('preview-btn');
const previewBox = el('preview');
const previewMeta = el('preview-meta');
const previewStats = el('preview-stats');
const previewLists = el('preview-lists');
const previewNotes = el('preview-notes');
const applyBtn = el('apply-btn');
const cancelBtn = el('cancel-btn');
const applyStatus = el('apply-status');

/** 预览时服务端回给我们的原文，确认导入时原样送回（服务端不留 pending）。 */
let pendingPayload = null;

function setStatus(node, text, kind = '') {
  node.textContent = text || '';
  node.className = 'status' + (kind ? ' ' + kind : '');
}

function stamp() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  return (
    now.getFullYear() +
    pad(now.getMonth() + 1) +
    pad(now.getDate()) +
    '-' +
    pad(now.getHours()) +
    pad(now.getMinutes()) +
    pad(now.getSeconds())
  );
}

function listBlock(title, items, limit = 12) {
  if (!items || !items.length) return '';
  const shown = items.slice(0, limit);
  const more = items.length > shown.length ? ` 等 ${items.length} 项` : '';
  return (
    `<div class="list-block"><span class="list-title">${title}</span>` +
    `<code>${shown.join('</code> <code>')}</code>${more}</div>`
  );
}

exportBtn.addEventListener('click', async () => {
  setStatus(exportStatus, '正在导出…');
  exportBtn.disabled = true;
  try {
    await bridge.download('config-export', {}, `hdsi-config-${stamp()}.json`);
    setStatus(exportStatus, '已开始下载。', 'ok');
  } catch (error) {
    setStatus(exportStatus, `导出失败：${error?.message || error}`, 'err');
  } finally {
    exportBtn.disabled = false;
  }
});

fileInput.addEventListener('change', () => {
  previewBtn.disabled = !fileInput.files?.length;
  previewBox.classList.add('hidden');
  pendingPayload = null;
  setStatus(applyStatus, '');
});

previewBtn.addEventListener('click', async () => {
  const file = fileInput.files?.[0];
  if (!file) return;
  setStatus(exportStatus, '');
  previewBtn.disabled = true;
  setStatus(applyStatus, '正在解析…');
  try {
    const response = await bridge.upload('config-import-preview', file);
    renderPreview(response);
  } catch (error) {
    previewBox.classList.add('hidden');
    setStatus(applyStatus, `读取失败：${error?.message || error}`, 'err');
  } finally {
    previewBtn.disabled = false;
  }
});

function renderPreview(response) {
  const report = response?.report;
  if (!report) {
    setStatus(applyStatus, '服务端没有返回预览结果。', 'err');
    return;
  }
  pendingPayload = response.payload;
  const diff = report.diff || {};
  const changed = diff.changed || [];
  const added = diff.added || [];
  const removed = diff.removed || [];

  previewMeta.textContent = `文件格式 v${report.format_version}（${report.source}）· 覆盖 ${report.section_count} 个分组`;
  previewStats.innerHTML = [
    `<li><b>${changed.length}</b> 项将被覆盖</li>`,
    `<li><b>${added.length}</b> 项新增</li>`,
    `<li><b>${removed.length}</b> 项在文件里没有（保持原值）</li>`,
  ].join('');

  previewLists.innerHTML =
    listBlock('将被覆盖', changed) + listBlock('新增', added);

  const notes = [];
  (report.notes || []).forEach((item) => notes.push(`<div class="note">${item}</div>`));
  (report.warnings || []).forEach((item) => notes.push(`<div class="note warn">${item}</div>`));
  previewNotes.innerHTML = notes.join('');

  previewBox.classList.remove('hidden');
  setStatus(applyStatus, changed.length || added.length ? '确认后立即生效。' : '没有需要改动的项。');
}

cancelBtn.addEventListener('click', () => {
  pendingPayload = null;
  previewBox.classList.add('hidden');
  setStatus(applyStatus, '');
});

applyBtn.addEventListener('click', async () => {
  if (pendingPayload === null) {
    setStatus(applyStatus, '请先预览一次。', 'err');
    return;
  }
  applyBtn.disabled = true;
  setStatus(applyStatus, '正在写入…');
  try {
    const response = await bridge.apiPost('config-import-apply', { payload: pendingPayload });
    const diff = response?.diff || {};
    setStatus(
      applyStatus,
      `已导入并生效（${response?.saved_via}）：修改 ${(diff.changed || []).length} 项、` +
        `新增 ${(diff.added || []).length} 项。`,
      'ok'
    );
    pendingPayload = null;
    previewBox.classList.add('hidden');
    fileInput.value = '';
    previewBtn.disabled = true;
  } catch (error) {
    setStatus(applyStatus, `导入失败：${error?.message || error}`, 'err');
  } finally {
    applyBtn.disabled = false;
  }
});

// bridge 就绪后可以按需拿上下文（这里只用来确认通道可用）
try {
  await bridge.ready();
} catch (error) {
  setStatus(exportStatus, `页面通道未就绪：${error?.message || error}`, 'err');
}
