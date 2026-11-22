/* Browser UI. No network calls, framework, build step, or exchange credentials. */
'use strict';
const $ = id => document.getElementById(id);
const M = GridLab;
const WIDTH = 1200, HEIGHT = 400, STORAGE = 'grid-canvas-v1';
const colors = ['#54dcc0', '#b7a0ff', '#f3c779', '#72b8ff', '#f28db6', '#a2da85'];
const esc = value => String(value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const fmt = (n, digits = 4) => Number(n).toLocaleString('en-US', { maximumFractionDigits: digits });
const signed = n => (n > 0 ? '+' : '') + fmt(n, 4);
const pnlClass = n => n < -1e-9 ? 'negative' : n > 1e-9 ? 'positive' : '';
const date = t => new Date(t).toLocaleString('zh-CN', { hour12: false });
const uid = () => crypto.randomUUID();
const color = c => /^#[\da-f]{6}$/i.test(c) ? c : colors[0];
let datasets = [], strategies = [], selectedId = '', engines = [], processed = 0, playing = false;
let dirty = true, initialized = false, failed = false, chartBars = [], chartScale = null, preview = [], strokes = [];
let activeStroke = null, strokePointer = null, drawQueued = false, generation = 0, toastTimer;
const world = new GridDrawing.InkWorld(() => document.createElement('canvas'));
const view = { x: 0, y: 0 };
let pointerClient = null, panDrag = null, spaceHeld = false, autoPanFrame = 0, autoPanTime = 0;
const pen = $('draw-canvas'), penContext = pen.getContext('2d');
const drawingFields = ['start-price', 'pixel-value', 'pixels-per-candle', 'interval', 'start-time'];

function toast(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4200);
}
function safe(action, target = 'replay-error') {
  try { $(target).textContent = ''; return action(); }
  catch (error) { $(target).textContent = error.message; return null; }
}
function dataset() { return datasets.find(d => d.id === selectedId); }
function defaultStrategy(index = strategies.length) {
  return { ...M.defaultStrategy(), id: uid(), name: `Gate ${index % 2 ? '空头' : '多头'} ${index + 1}`,
    direction: index % 2 ? 'short' : 'long', enabled: true, color: colors[index % colors.length] };
}
function persist() {
  let total = 0; const saved = [];
  for (const d of datasets) {
    const json = JSON.stringify(d);
    if (total + json.length > 1500000) continue;
    saved.push(d); total += json.length;
  }
  try { localStorage.setItem(STORAGE, JSON.stringify({ strategies, datasets: saved, selectedId })); }
  catch { toast('浏览器本地存储空间不足，请导出行情备份。当前会话数据仍可使用。'); return; }
  if (saved.length < datasets.length) toast('部分行情较大，仅保留在当前会话，请导出 JSON 备份。');
}
function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE) || 'null');
    if (saved) {
      datasets = (saved.datasets || []).map(d => ({ ...d, candles: M.validateCandles(d.candles) }));
      const legacy = (saved.strategies || []).some(s => s.model !== 'gate-futures-v1');
      strategies = (saved.strategies || []).slice(0, 12).map(s => ({ ...M.migrateStrategy(s), id: s.id || uid(), color: color(s.color) }));
      selectedId = saved.selectedId;
      if (legacy) toast('已切换为 Gate 滚动网格。原固定区间参数保留在报告配置中，请确认参考价、间隔和合约规格。');
    }
  } catch { toast('本地保存的数据无法读取，已启动空白工作区。'); datasets = []; strategies = []; }
  if (!datasets.length) datasets.push(makeDemo());
  if (!datasets.some(d => d.id === selectedId)) selectedId = datasets[0].id;
  if (!strategies.length) { strategies.push(defaultStrategy(0)); strategies.push(defaultStrategy(1)); }
}
function setTab(tab) {
  stopDrawingGesture();
  $('draw-view').hidden = tab !== 'draw'; $('replay-view').hidden = tab !== 'replay';
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  if (tab === 'draw') pause();
  requestAnimationFrame(renderCharts);
}
document.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => setTab(b.dataset.tab));

function inkLine(stroke, from, to) {
  world.line(stroke, from, to);
}
function replayStrokes() {
  world.clear();
  for (const stroke of strokes) stroke.points.forEach((p, i) => inkLine(stroke, stroke.points[i - 1], p));
  updateDrawing();
}
function pointer(event) {
  return GridDrawing.worldPoint({ x: event.clientX, y: event.clientY }, pen.getBoundingClientRect(), view, WIDTH, HEIGHT);
}
function appendPoint(p) {
  const previous = activeStroke.points.at(-1);
  if (previous.x === p.x && previous.y === p.y) return;
  inkLine(activeStroke, previous, p); activeStroke.points.push(p);
}
function followPointer(now) {
  autoPanFrame = 0;
  if (!activeStroke || !pointerClient) return;
  const r = pen.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const dt = Math.max(0, Math.min(now - autoPanTime, 40)) / 1000; autoPanTime = now;
  const vx = GridDrawing.edgeVelocity(pointerClient.clientX - r.left, r.width);
  const vy = GridDrawing.edgeVelocity(pointerClient.clientY - r.top, r.height);
  if (!vx && !vy) return;
  view.x += vx * dt * WIDTH / r.width; view.y += vy * dt * HEIGHT / r.height;
  appendPoint(pointer(pointerClient)); scheduleDrawing();
  autoPanFrame = requestAnimationFrame(followPointer);
}
function startAutoPan() {
  if (autoPanFrame) return;
  autoPanTime = performance.now(); autoPanFrame = requestAnimationFrame(followPointer);
}
function stopDrawingGesture() {
  activeStroke = null; pointerClient = null; panDrag = null;
  cancelAnimationFrame(autoPanFrame); autoPanFrame = 0;
  const captured = strokePointer; strokePointer = null;
  if (captured != null && pen.hasPointerCapture(captured)) pen.releasePointerCapture(captured);
  pen.classList.remove('panning');
}
pen.addEventListener('pointerdown', event => {
  if (strokePointer != null || (event.pointerType === 'mouse' && ![0, 1].includes(event.button))) return;
  event.preventDefault(); pen.setPointerCapture(event.pointerId); strokePointer = event.pointerId;
  pen.focus({ preventScroll: true });
  if (spaceHeld || event.button === 1) {
    panDrag = { x: event.clientX, y: event.clientY }; pen.classList.add('panning'); return;
  }
  pointerClient = { clientX: event.clientX, clientY: event.clientY };
  activeStroke = { width: Number($('pen-width').value), points: [pointer(event)] };
  strokes.push(activeStroke); inkLine(activeStroke, null, activeStroke.points[0]); scheduleDrawing(); startAutoPan();
});
pen.addEventListener('pointermove', event => {
  if (strokePointer !== event.pointerId) return;
  if (panDrag) {
    const r = pen.getBoundingClientRect();
    view.x -= (event.clientX - panDrag.x) * WIDTH / r.width; view.y -= (event.clientY - panDrag.y) * HEIGHT / r.height;
    panDrag = { x: event.clientX, y: event.clientY }; scheduleDrawing(); return;
  }
  if (!activeStroke) return;
  const coalesced = event.getCoalescedEvents?.();
  for (const e of coalesced?.length ? coalesced : [event]) appendPoint(pointer(e));
  pointerClient = { clientX: event.clientX, clientY: event.clientY };
  scheduleDrawing(); startAutoPan();
});
for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) pen.addEventListener(type, event => {
  if (strokePointer !== event.pointerId) return;
  stopDrawingGesture(); updateDrawing();
});
pen.addEventListener('wheel', event => {
  if (event.ctrlKey || event.metaKey) return;
  event.preventDefault();
  if (strokePointer != null) return;
  const r = pen.getBoundingClientRect(), unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? r.height : 1;
  const dx = event.shiftKey && !event.deltaX ? event.deltaY : event.deltaX;
  const dy = event.shiftKey && !event.deltaX ? 0 : event.deltaY;
  view.x += dx * unit * WIDTH / r.width; view.y += dy * unit * HEIGHT / r.height;
  scheduleDrawing();
}, { passive: false });
document.addEventListener('keydown', event => {
  if (event.code !== 'Space' || event.target.closest('input,select,textarea,button') || $('draw-view').hidden) return;
  if (!pen.matches(':hover') && document.activeElement !== pen) return;
  event.preventDefault(); spaceHeld = true; pen.classList.add('pan-ready');
});
document.addEventListener('keyup', event => {
  if (event.code === 'Space') { spaceHeld = false; pen.classList.remove('pan-ready'); }
});
window.addEventListener('blur', () => {
  spaceHeld = false; pen.classList.remove('pan-ready'); stopDrawingGesture();
});
$('draw-home').onclick = () => { view.x = 0; view.y = 0; updateDrawing(); };
function scheduleDrawing() {
  if (drawQueued) return; drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; updateDrawing(); });
}
function drawBackground() {
  penContext.clearRect(0, 0, WIDTH, HEIGHT); penContext.fillStyle = '#0d151e'; penContext.fillRect(0, 0, WIDTH, HEIGHT);
  penContext.strokeStyle = '#1e2a36'; penContext.lineWidth = 1;
  for (let wx = Math.ceil(view.x / 60) * 60; wx < view.x + WIDTH; wx += 60) {
    const x = wx - view.x; penContext.beginPath(); penContext.moveTo(x + .5, 0); penContext.lineTo(x + .5, HEIGHT); penContext.stroke();
  }
  for (let wy = Math.ceil(view.y / 50) * 50; wy < view.y + HEIGHT; wy += 50) {
    const y = wy - view.y; penContext.beginPath(); penContext.moveTo(0, y + .5); penContext.lineTo(WIDTH, y + .5); penContext.stroke();
  }
  penContext.fillStyle = '#546577'; penContext.font = '12px Segoe UI';
  penContext.fillText('起点  →  时间', 14, HEIGHT - 13); penContext.fillText('价格 ↑', 14, 21);
  world.paint(penContext, view, WIDTH, HEIGHT);
  $('view-position').textContent = `视野 X ${fmt(view.x, 0)} · Y ${fmt(view.y, 0)}`;
  const b = world.bounds;
  const width = b ? Math.max(WIDTH, b.maxX) - Math.min(0, b.minX) : WIDTH;
  const height = b ? Math.max(HEIGHT, b.maxY) - Math.min(0, b.minY) : HEIGHT;
  $('canvas-size').textContent = `${fmt(width, 0)} × ${fmt(height, 0)} · 自动扩展`;
}
function generateDrawing() {
  const { columns, left } = world.sample();
  const mapping = M.pricesFromColumns(columns, $('start-price').value, $('pixel-value').value);
  const time = new Date($('start-time').value).getTime();
  if (!Number.isFinite(time)) throw new Error('请选择有效的起始时间');
  const candles = M.candlesFromPrices(mapping.prices, $('pixels-per-candle').value, time, $('interval').value);
  return { mapping, candles, originX: left };
}
function updateDrawing() {
  drawBackground(); $('draw-placeholder').hidden = strokes.length > 0; $('undo').disabled = !strokes.length;
  $('draw-error').textContent = ''; preview = [];
  if (!strokes.length) { $('draw-info').textContent = '等待绘制'; $('preview-count').textContent = '绘制后自动生成'; drawPreview(); return; }
  try {
    const { mapping, candles, originX } = generateDrawing(); preview = candles;
    penContext.strokeStyle = '#f3c779'; penContext.lineWidth = 1.4; penContext.beginPath();
    const begin = Math.max(0, Math.floor(view.x - originX)), end = Math.min(mapping.ys.length, Math.ceil(view.x - originX + WIDTH) + 1);
    for (let i = begin; i < end; i++) {
      const x = originX + i - view.x, y = mapping.ys[i] - view.y;
      if (i === begin) penContext.moveTo(x, y); else penContext.lineTo(x, y);
    }
    penContext.stroke();
    penContext.setLineDash([6, 6]); penContext.strokeStyle = '#f3c77966'; penContext.beginPath();
    penContext.moveTo(0, mapping.ys[0] - view.y); penContext.lineTo(WIDTH, mapping.ys[0] - view.y); penContext.stroke(); penContext.setLineDash([]);
    let low = Infinity, high = -Infinity;
    for (const price of mapping.prices) { low = Math.min(low, price); high = Math.max(high, price); }
    $('draw-info').textContent = `${mapping.prices.length} 列 · ${fmt(low)} — ${fmt(high)}`;
    $('preview-count').textContent = `${candles.length} 根 · 精确路径已保留`;
  } catch (error) { $('draw-error').textContent = error.message; $('preview-count').textContent = '等待有效数据'; }
  drawPreview();
}
function preset(type) {
  stopDrawingGesture(); view.x = 0; view.y = 0;
  const points = Array.from({ length: WIDTH }, (_, x) => {
    const t = x / (WIDTH - 1);
    const wave = 45 * Math.sin(t * Math.PI * 10) + 22 * Math.sin(t * Math.PI * 23);
    const y = type === 'down' ? 100 + 200 * Math.sin(t * Math.PI * .83) + wave * .55
      : type === 'up' ? 305 - 215 * t + wave * .65 : 200 + wave + 32 * Math.sin(t * Math.PI * 3);
    return { x, y };
  });
  strokes = [{ width: Number($('pen-width').value), points }]; replayStrokes();
}
$('undo').onclick = () => { strokes.pop(); replayStrokes(); };
$('clear-draw').onclick = () => { stopDrawingGesture(); strokes = []; view.x = 0; view.y = 0; replayStrokes(); };
drawingFields.forEach(id => $(id).addEventListener('input', scheduleDrawing));
document.querySelectorAll('[data-preset]').forEach(b => b.onclick = () => preset(b.dataset.preset));
function drawnDataset() {
  const drawing = generateDrawing(), bounds = world.bounds;
  return { id: uid(), name: $('data-name').value.trim() || '未命名手绘行情', source: '手绘',
    candles: drawing.candles, created: new Date().toISOString(),
    mapping: { width: drawing.mapping.prices.length, height: bounds.maxY - bounds.minY, originX: drawing.originX,
      originY: bounds.minY, viewportWidth: WIDTH, viewportHeight: HEIGHT,
      startPrice: Number($('start-price').value), pixelValue: Number($('pixel-value').value),
      pixelsPerCandle: Number($('pixels-per-candle').value), intervalSeconds: Number($('interval').value) } };
}
function download(name, content, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([content], { type })); const a = document.createElement('a');
  a.href = url; a.download = name.replace(/[<>:"/\\|?*]/g, '_'); a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function exportDataset(id, format) {
  const d = datasets.find(d => d.id === id); if (!d) return;
  if (format === 'csv') download(`${d.name}.csv`, '\uFEFF' + M.toCSV(d.candles), 'text/csv;charset=utf-8');
  else download(`${d.name}.json`, JSON.stringify({ version: 1, ...d }, null, 2));
}
$('export-draw-json').onclick = () => safe(() => { const d = drawnDataset(); download(`${d.name}.json`, JSON.stringify({ version: 1, ...d }, null, 2)); }, 'draw-error');
$('export-draw-csv').onclick = () => safe(() => { const d = drawnDataset(); download(`${d.name}.csv`, '\uFEFF' + M.toCSV(d.candles), 'text/csv;charset=utf-8'); }, 'draw-error');
$('save-data').onclick = () => safe(() => {
  const d = drawnDataset(); datasets.unshift(d); selectedId = d.id; invalidate(); persist(); renderDatasets();
  setTab('replay'); toast(`已保存 ${d.candles.length} 根 K 线，可开始回测`);
}, 'draw-error');

function makeDemo() {
  const prices = Array.from({ length: 1200 }, (_, i) => 100 + 7 * Math.sin(i / 37) + 4 * Math.sin(i / 18) - 5 * Math.sin(i / 155));
  return { id: uid(), name: '示例 · 多轮震荡', source: '内置示例',
    candles: M.candlesFromPrices(prices, 6, Date.UTC(2026, 10, 14, 0), 60) };
}
function renderDatasets() {
  $('dataset-select').innerHTML = datasets.map(d => `<option value="${esc(d.id)}">${esc(d.name)} · ${fmt(d.candles.length, 0)} 根</option>`).join('');
  $('dataset-select').value = selectedId;
  $('dataset-library').innerHTML = datasets.length ? datasets.map(d => `<article class="data-card ${d.id === selectedId ? 'selected' : ''}"><h3>${esc(d.name)}</h3><p>${esc(d.source)} · ${fmt(d.candles.length, 0)} 根 · ${d.candles.every(c => c.path) ? '含精确路径' : 'OHLC 数据'}</p><p>${esc(date(d.candles[0].time))}</p><p>起价 ${fmt(d.candles[0].open)} → 收价 ${fmt(d.candles.at(-1).close)}</p><div class="toolbar"><button data-data-action="use" data-id="${esc(d.id)}">${d.id === selectedId ? '已选中' : '用于回测'}</button><button data-data-action="json" data-id="${esc(d.id)}">JSON</button><button data-data-action="csv" data-id="${esc(d.id)}">CSV</button><button data-data-action="delete" data-id="${esc(d.id)}" aria-label="删除 ${esc(d.name)}">×</button></div></article>`).join('') : '<div class="empty-library">暂无行情，请先手绘保存或导入 CSV / JSON。</div>';
  chartBars = dataset() ? aggregate(dataset().candles, 600) : [];
  const exact = dataset()?.candles.every(c => c.path);
  const mixed = !exact && dataset()?.candles.some(c => c.path);
  $('path-mode').disabled = !!exact;
  $('path-badge').textContent = exact ? '手绘精确路径' : mixed ? '混合路径' : 'OHLC 模拟';
  renderReplay();
}
$('dataset-library').addEventListener('click', event => {
  const b = event.target.closest('[data-data-action]'); if (!b) return;
  const id = b.dataset.id;
  if (b.dataset.dataAction === 'use') {
    if (selectedId !== id) { selectedId = id; invalidate(); persist(); renderDatasets(); }
    setTab('replay');
  } else if (b.dataset.dataAction === 'delete') {
    const d = datasets.find(d => d.id === id); datasets = datasets.filter(d => d.id !== id);
    if (selectedId === id) { selectedId = datasets[0]?.id || ''; invalidate(); }
    persist(); renderDatasets();
    toast(`已从本地资料库移除「${d.name}」`);
  } else exportDataset(id, b.dataset.dataAction);
});
$('dataset-select').onchange = () => { selectedId = $('dataset-select').value; invalidate(); persist(); renderDatasets(); };
$('path-mode').onchange = () => { invalidate(); renderReplay(); };
$('import-data').onclick = () => $('file-input').click();
$('file-input').onchange = async () => {
  const file = $('file-input').files[0]; if (!file) return;
  try {
    if (file.size > 32 * 1024 * 1024) throw new Error('文件超过 32 MB，请分段导入');
    const text = await file.text();
    let candles, name = file.name.replace(/\.(csv|json)$/i, '');
    if (file.name.toLowerCase().endsWith('.json')) {
      const data = JSON.parse(text.replace(/^\uFEFF/, '')); candles = M.validateCandles(Array.isArray(data) ? data : data.candles);
      if (typeof data.name === 'string') name = data.name;
    } else candles = M.parseCSV(text);
    const d = { id: uid(), name: name.slice(0, 80), source: '文件导入', candles };
    datasets.unshift(d); selectedId = d.id; invalidate(); persist(); renderDatasets(); toast(`已导入 ${fmt(candles.length, 0)} 根 K 线`);
  } catch (error) { toast(`导入失败：${error.message}`); }
  $('file-input').value = '';
};
$('demo-data').onclick = () => { const d = makeDemo(); datasets.unshift(d); selectedId = d.id; invalidate(); persist(); renderDatasets(); toast('示例行情已载入'); };

function field(s, key, label, options = '') {
  return `<label>${label}<input data-key="${key}" type="number" step="any" ${options} value="${esc(s[key])}"></label>`;
}
function strategyCard(s) {
  const long = s.direction === 'long';
  return `<article class="strategy-card ${s.enabled ? '' : 'disabled'}" data-id="${esc(s.id)}" style="--strategy-color:${color(s.color)}">
    <div class="strategy-header"><input data-key="enabled" type="checkbox" ${s.enabled ? 'checked' : ''} aria-label="启用 ${esc(s.name)}"><input data-key="name" type="text" maxlength="40" value="${esc(s.name)}" aria-label="策略名称"><button class="delete" data-delete aria-label="删除 ${esc(s.name)}">×</button></div>
    <div class="two-fields"><label>合约方向<select data-key="direction"><option value="long" ${long ? 'selected' : ''}>做多 · 买开 / 卖平</option><option value="short" ${long ? '' : 'selected'}>做空 · 卖开 / 买平</option></select></label>${field(s, 'referencePrice', '参考价（0 = 行情起价）', 'min="0"')}</div>
    <div class="two-fields">${field(s, 'marginAmountPerOrder', '每格保证金（USDT）', 'min="0"')}${field(s, 'leverage', '杠杆倍数', 'min="1"')}</div>
    <div class="two-fields">${field(s, 'gridStepPct', '网格间隔比例', 'min="0" max="0.999999"')}${field(s, 'takeProfitPct', '独立止盈比例', 'min="0" max="0.999999"')}</div>
    <p class="strategy-ratio">比例与脚本一致：0.00042 = 0.042%<br>当前间隔 ${fmt(Number(s.gridStepPct) * 100, 6)}% · 止盈 ${fmt(Number(s.takeProfitPct) * 100, 6)}%</p>
    <div class="two-fields">${field(s, 'openGridCount', long ? '下方活动买格数' : '上方活动开空格数', 'min="1" max="300"')}${field(s, 'takeProfitCount', '活动止盈单上限', 'min="1" max="300"')}</div>
    <div class="two-fields">${field(s, 'takeProfitGridCount', '止盈检查范围（格）', 'min="1" max="1000"')}${field(s, 'fee', '单边手续费（%）', 'min="0" max="9.99"')}</div>
    <details class="contract-settings"><summary>合约规格与回测资金</summary><p>离线示例规格，请按实际合约填写。张数 = 保证金 × 杠杆 ÷（价格 × 每张面值），向下取整。</p>
    <div class="two-fields">${field(s, 'multiplier', '每张面值（标的数量）', 'min="0"')}${field(s, 'priceStep', '价格步长', 'min="0"')}</div>
    <div class="two-fields">${field(s, 'quantityStep', '张数步长', 'min="0"')}${field(s, 'minContracts', '最小下单张数', 'min="0"')}</div>
    <div class="two-fields">${field(s, 'maxContracts', '最大张数（0 不限）', 'min="0"')}${field(s, 'capital', '回测资金（USDT）', 'min="0"')}</div></details>
    <p class="strategy-ratio">每个入场价仅一个活动周期；旧仓保留，止盈仅在现价${long ? '上方' : '下方'}窗口内挂出。</p>
    <div class="strategy-bottom"><label>挂单颜色<input data-key="color" type="color" value="${color(s.color)}"></label><button data-copy class="ghost">复制策略</button></div></article>`;
}
function renderStrategies() {
  $('strategy-count').textContent = `${strategies.filter(s => s.enabled).length} / ${strategies.length} 启用`;
  $('strategy-list').innerHTML = strategies.map(strategyCard).join('');
  const filter = $('order-filter').value;
  $('order-filter').innerHTML = '<option value="all">全部策略</option>' + strategies.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
  $('order-filter').value = strategies.some(s => s.id === filter) ? filter : 'all';
  $('add-strategy').disabled = strategies.length >= 12;
}
$('add-strategy').onclick = () => {
  if (strategies.length >= 12) return;
  strategies.push(defaultStrategy()); invalidate(); persist(); renderStrategies(); renderReplay();
};
$('strategy-list').addEventListener('change', event => {
  const input = event.target, card = input.closest('[data-id]'); if (!card || !input.dataset.key) return;
  const s = strategies.find(s => s.id === card.dataset.id), key = input.dataset.key;
  const detailsOpen = card.querySelector('details')?.open;
  s[key] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? (input.value === '' ? '' : Number(input.value)) : input.value;
  if (key === 'color') { card.style.setProperty('--strategy-color', color(s.color)); persist(); renderReplay(); return; }
  invalidate(); persist(); renderStrategies();
  if (detailsOpen) $('strategy-list').querySelector(`[data-id="${CSS.escape(s.id)}"] details`).open = true;
  renderReplay();
});
$('strategy-list').addEventListener('click', event => {
  const card = event.target.closest('[data-id]'); if (!card) return;
  if (event.target.closest('[data-delete]')) strategies = strategies.filter(s => s.id !== card.dataset.id);
  else if (event.target.closest('[data-copy]')) {
    if (strategies.length >= 12) { toast('最多支持 12 个策略'); return; }
    const s = strategies.find(s => s.id === card.dataset.id);
    strategies.push({ ...s, id: uid(), name: `${s.name} 副本`, color: colors[strategies.length % colors.length] });
  } else return;
  invalidate(); persist(); renderStrategies(); renderReplay();
});
$('order-filter').onchange = renderDetails;
function pause() { playing = false; generation++; $('run').textContent = processed && processed < (dataset()?.candles.length || 0) ? '▶ 继续回测' : '▶ 开始回测'; }
function invalidate() { pause(); engines = []; processed = 0; dirty = true; initialized = false; failed = false; $('replay-error').textContent = ''; }
function initialize() {
  if (!dirty) return true;
  const d = dataset(); if (!d) throw new Error('请先选择一份行情数据');
  const active = strategies.filter(s => s.enabled); if (!active.length) throw new Error('请至少启用一个网格策略');
  const configs = active.map(s => {
    try { return M.strategy(s); } catch (error) { throw new Error(`${s.name}：${error.message}`); }
  });
  // Validate the entire set before mutating replay state.
  engines = configs.map(s => new M.GridEngine(s, d.candles[0].open, d.candles[0].time));
  processed = 0; dirty = false; initialized = true; return true;
}
function advance(count) {
  const d = dataset();
  try {
    for (let n = 0; n < count && processed < d.candles.length; n++, processed++) {
      for (const engine of engines) engine.advance(d.candles[processed], $('path-mode').value);
    }
  } catch (error) {
    failed = true; pause(); $('replay-error').textContent = `第 ${processed + 1} 根 K 线内回测中断：${error.message}。请修改配置或重置；当前统计包含该根已执行的部分路径。`;
  }
  if (processed >= d.candles.length) pause();
}
function renderReplay() {
  const d = dataset(), snapshots = engines.map(e => e.snapshot());
  $('chart-title').textContent = d?.name || '请选择行情数据';
  const current = processed ? d?.candles[processed - 1] : null;
  $('current-time').textContent = current ? date(current.time) : d ? `${date(d.candles[0].time)} · 起点` : '';
  $('current-price').textContent = d ? fmt(failed ? engines[0]?.price ?? current?.close : current?.close ?? d.candles[0].open, 6) : '—';
  $('progress').max = d?.candles.length || 1; $('progress').value = processed;
  $('progress-label').textContent = `${fmt(processed, 0)} / ${fmt(d?.candles.length || 0, 0)}`;
  $('run-state').textContent = failed ? '已中断' : playing ? '回放中' : d && processed === d.candles.length ? '已完成' : initialized ? '已暂停' : '未开始';
  const total = key => snapshots.reduce((sum, s) => sum + s[key], 0);
  for (const [id, key] of [['total-pnl', 'pnl'], ['realized-pnl', 'realized'], ['total-fees', 'fees']]) {
    $(id).textContent = initialized ? (key === 'fees' ? fmt(total(key)) : signed(total(key))) : '—';
    $(id).className = key === 'fees' ? '' : pnlClass(total(key));
  }
  $('cycle-count').textContent = initialized ? `${fmt(total('cycles'), 0)} / ${fmt(snapshots.reduce((n, s) => n + s.orders.length, 0), 0)}` : '—';
  $('results-body').innerHTML = snapshots.length ? snapshots.map(s => `<tr><td>${strategyLabel(s.id)}<br><span class="subtle">${s.direction === 'long' ? '做多' : '做空'} · 参考 ${fmt(s.reference, 6)}</span></td><td class="${pnlClass(s.pnl)}">${signed(s.pnl)}</td><td class="${pnlClass(s.realized)}">${signed(s.realized)}</td><td class="${pnlClass(s.unrealized)}">${signed(s.unrealized)}</td><td>${fmt(s.equity, 2)}</td><td>${fmt(s.available, 2)} / ${fmt(s.reserved, 2)}</td><td>${fmt(s.marginUsed, 4)}</td><td>${s.positions} / ${s.waitingTakeProfits}</td><td>${fmt(s.maxDrawdown)}</td><td>${fmt(s.fees)}</td></tr>`).join('') : '<tr><td colspan="10" class="empty">设置 Gate 网格参数后，点击开始回测</td></tr>';
  $('chart-legend').innerHTML = strategies.filter(s => s.enabled).map(s => `<span><i style="background:${color(s.color)}"></i>${esc(s.name)} · ${s.direction === 'long' ? '多' : '空'}</span>`).join('') + '<span>实线 开仓 / 虚线 止盈</span><span>▲ 买入 / ▼ 卖出</span><span>暗色 K 线为未来行情</span>';
  const complete = !!d && processed === d.candles.length;
  $('run').textContent = playing ? 'Ⅱ 暂停回测' : complete ? '↻ 重新回测' : initialized ? '▶ 继续回测' : '▶ 开始回测';
  $('run').disabled = !d || failed; $('step').disabled = !d || complete || failed; $('finish').disabled = !d || complete || failed;
  $('export-report').disabled = !initialized; $('export-trades').disabled = !initialized;
  renderDetails(); drawReplay();
}
function strategyLabel(id) {
  const s = strategies.find(s => s.id === id); return `<i class="legend-dot" style="background:${color(s?.color)}"></i>${esc(s?.name || '策略')}`;
}
function orderAction(order, direction) {
  const long = direction === 'long';
  return order.kind === 'open' ? (long ? '买开多' : '卖开空') : (long ? '卖平多' : '买平空');
}
function renderDetails() {
  const filter = $('order-filter').value;
  const shown = engines.filter(e => filter === 'all' || e.config.id === filter);
  const orders = shown.flatMap(e => e.snapshot().orders.map(o => ({ ...o, id: e.config.id, direction: e.config.direction }))).sort((a, b) => b.price - a.price);
  const waiting = shown.reduce((n, e) => n + e.snapshot().waitingTakeProfits, 0);
  $('order-count').textContent = `${orders.filter(o => o.kind === 'open').length} 开仓 / ${orders.filter(o => o.kind === 'close').length} 止盈 / ${waiting} 待挂止盈`;
  $('orders-body').innerHTML = orders.map(o => `<tr><td>${strategyLabel(o.id)}</td><td class="${o.side === 'buy' ? 'positive' : 'negative'}">${orderAction(o, o.direction)} #${o.grid}</td><td>${fmt(o.price, 8)}</td><td title="${fmt(o.quantity, 8)} 标的数量">${fmt(o.contracts, 8)}</td></tr>`).join('') || '<tr><td colspan="4" class="empty">暂无挂单</td></tr>';
  const trades = shown.flatMap(e => e.trades.map((t, sequence) => ({ ...t, sequence })))
    .sort((a, b) => b.time - a.time || b.sequence - a.sequence).slice(0, 100);
  $('trades-body').innerHTML = trades.map(t => `<tr><td>${strategyLabel(t.strategyId)}<br><span class="subtle">${esc(date(t.time))}</span></td><td class="${t.side === 'buy' ? 'positive' : 'negative'}">${orderAction(t, t.direction)}</td><td>${fmt(t.price, 8)}</td><td class="${pnlClass(t.profit || 0)}">${t.profit == null ? '—' : signed(t.profit)}</td></tr>`).join('') || '<tr><td colspan="4" class="empty">暂无成交记录</td></tr>';
}
$('run').onclick = () => safe(() => {
  if (playing) { pause(); renderReplay(); return; }
  if (processed === dataset()?.candles.length) invalidate();
  initialize(); playing = true; const token = ++generation; renderReplay();
  let last = performance.now(), accrued = 0;
  function tick(now) {
    if (!playing || token !== generation) return;
    accrued += Math.min(now - last, 200) / 1000 * Number($('speed').value); last = now;
    const count = Math.floor(accrued);
    if (count) { accrued -= count; advance(count); renderReplay(); }
    if (playing) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
});
$('step').onclick = () => safe(() => { pause(); initialize(); advance(1); renderReplay(); });
$('reset').onclick = () => { invalidate(); renderReplay(); };
$('finish').onclick = () => safe(() => {
  pause(); initialize(); playing = true; const token = ++generation; renderReplay();
  function batch() {
    if (!playing || token !== generation) return;
    const until = performance.now() + 24;
    do { advance(1); } while (playing && performance.now() < until);
    renderReplay(); if (playing) setTimeout(batch, 0);
  }
  setTimeout(batch, 0);
});
$('export-report').onclick = () => safe(() => {
  const d = dataset();
  download(`${d.name}-回测报告.json`, JSON.stringify({ version: 2, engineModel: 'gate-futures-v1', data: { name: d.name, candles: d.candles.length },
    processed, failed, complete: processed === d.candles.length && !failed, pathMode: $('path-badge').textContent === '手绘精确路径' ? 'exact' : $('path-mode').value,
    assumptions: 'Gate 锚定滚动合约网格；各策略独立资金；每个入场价一个活动周期；含杠杆、面值、数量/价格精度及双边手续费；逐路径即时维护窗口；无实盘轮询延迟、滑点、部分成交、强平与资金费率；未扣未来平仓费',
    strategies: engines.map(e => ({ config: { ...e.config, color: strategies.find(s => s.id === e.config.id)?.color },
      result: e.snapshot(), equityHistory: e.history, tradesTruncated: e.tradeCount > e.trades.length, trades: e.trades })) }, null, 2));
});
function csvCell(v) { return '"' + String(v ?? '').replace(/"/g, '""') + '"'; }
$('export-trades').onclick = () => safe(() => {
  const fields = ['strategy', 'time', 'direction', 'kind', 'side', 'reduceOnly', 'grid', 'price', 'contracts', 'quantity', 'fee', 'profit'];
  const trades = engines.flatMap(e => e.trades).sort((a, b) => a.time - b.time);
  download(`${dataset().name}-成交记录.csv`, '\uFEFF' + fields.join(',') + '\n' + trades.map(t => fields.map(k => csvCell(t[k])).join(',')).join('\n'), 'text/csv;charset=utf-8');
  if (engines.some(e => e.tradeCount > e.trades.length)) toast('成交日志超过保留上限，已导出最近记录。完整统计见回测报告。');
});

function fitCanvas(canvas) {
  const box = canvas.getBoundingClientRect(); if (!box.width || !box.height) return null;
  const ratio = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(box.width * ratio) || canvas.height !== Math.round(box.height * ratio)) {
    canvas.width = Math.round(box.width * ratio); canvas.height = Math.round(box.height * ratio);
  }
  const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, box.width, box.height);
  return { ctx, width: box.width, height: box.height };
}
function aggregate(candles, max) {
  const step = Math.max(1, Math.ceil(candles.length / max)), result = [];
  for (let start = 0; start < candles.length; start += step) {
    const end = Math.min(candles.length, start + step), chunk = candles.slice(start, end);
    result.push({ start, end, open: chunk[0].open, close: chunk.at(-1).close,
      high: Math.max(...chunk.map(c => c.high)), low: Math.min(...chunk.map(c => c.low)) });
  }
  return result;
}
function chartBase(canvas, bars, total, extra = []) {
  const fitted = fitCanvas(canvas); if (!fitted) return null;
  const { ctx, width, height } = fitted, left = 10, right = 78, top = 12, bottom = 26;
  const w = Math.max(1, width - left - right), h = height - top - bottom;
  if (!bars.length) { ctx.fillStyle = '#62788b'; ctx.font = '12px Segoe UI'; ctx.fillText('等待行情数据', 20, height / 2); return null; }
  let lo = Math.min(...bars.map(c => c.low), ...extra), hi = Math.max(...bars.map(c => c.high), ...extra);
  const padding = Math.max((hi - lo) * .1, hi * .005); lo -= padding; hi += padding;
  const x = i => left + i / Math.max(1, total) * w, y = price => top + (hi - price) / (hi - lo) * h;
  ctx.font = '10px Consolas';
  for (let i = 0; i <= 5; i++) {
    const py = top + h * i / 5, p = hi - (hi - lo) * i / 5;
    ctx.strokeStyle = '#22303e'; ctx.lineWidth = .7; ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(left + w, py); ctx.stroke();
    ctx.fillStyle = '#6d8196'; ctx.fillText(fmt(p, 4), left + w + 8, py + 4);
  }
  for (let i = 0; i <= 6; i++) {
    const px = x(total * i / 6); ctx.strokeStyle = '#1b2834'; ctx.beginPath(); ctx.moveTo(px, top); ctx.lineTo(px, top + h); ctx.stroke();
  }
  return { ...fitted, x, y, left, top, w, h, total };
}
function paintCandles(chart, bars, done = Infinity) {
  const { ctx, x, y, total, w } = chart;
  for (const c of bars) {
    const center = x((c.start + c.end) / 2), candleWidth = Math.max(1, Math.min(10, (c.end - c.start) / total * w * .65));
    const active = c.start < done;
    ctx.strokeStyle = ctx.fillStyle = active ? (c.close >= c.open ? '#54cda9' : '#ef7c8a') : '#354454';
    ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(center, y(c.high)); ctx.lineTo(center, y(c.low)); ctx.stroke();
    ctx.fillRect(center - candleWidth / 2, Math.min(y(c.open), y(c.close)), candleWidth, Math.max(1, Math.abs(y(c.open) - y(c.close))));
  }
}
function drawPreview() {
  const chart = chartBase($('preview-canvas'), aggregate(preview, 220), preview.length);
  if (chart) paintCandles(chart, aggregate(preview, 220));
}
function drawReplay() {
  const d = dataset(), orders = engines.flatMap(e => e.snapshot().orders.map(o => ({ ...o, id: e.config.id })));
  const chart = chartBase($('replay-canvas'), chartBars, d?.candles.length || 0, orders.map(o => o.price));
  chartScale = chart;
  if (!chart || !d) return;
  const { ctx, x, y, left, top, w, h, height } = chart;
  paintCandles(chart, chartBars, processed);
  const cursor = x(Math.max(0, processed - .5));
  ctx.save(); ctx.beginPath(); ctx.rect(left, top - 3, w + 1, h + 6); ctx.clip();
  // Give each strategy a separate horizontal lane, so coincident limit prices
  // remain visible in both colors. Preserve a visible projection at replay end.
  const orderStart = Math.min(cursor, left + w * .62);
  const laneWidth = (left + w - orderStart) / Math.max(1, engines.length);
  for (let lane = 0; lane < engines.length; lane++) {
    const engine = engines[lane], s = strategies.find(s => s.id === engine.config.id);
    const x1 = orderStart + lane * laneWidth + 3, x2 = orderStart + (lane + 1) * laneWidth - 3;
    let lastLabelY = -Infinity;
    const pending = orders.filter(o => o.id === engine.config.id).sort((a, b) => b.price - a.price);
    for (const o of pending) {
      const py = y(o.price);
      ctx.strokeStyle = color(s?.color) + 'bb'; ctx.lineWidth = 1; ctx.setLineDash(o.kind === 'close' ? [5, 4] : []);
      ctx.beginPath(); ctx.moveTo(x1, py); ctx.lineTo(x2, py); ctx.stroke();
      if (laneWidth > 65 && py - lastLabelY > 18) {
        ctx.fillStyle = color(s?.color); ctx.font = '9px Consolas'; ctx.textAlign = 'right';
        ctx.fillText(`${o.kind === 'open' ? 'OPEN' : 'TP'} ${fmt(o.price, 4)}`, x2 - 2, py - 4);
        lastLabelY = py;
      }
    }
  }
  ctx.setLineDash([]); ctx.textAlign = 'left';
  // Mark recent fills on the price chart. Times locate their source candle.
  for (const e of engines) {
    const s = strategies.find(s => s.id === e.config.id); ctx.fillStyle = color(s?.color);
    for (const t of e.trades.slice(-160)) {
      let low = 0, high = d.candles.length - 1;
      while (low < high) { const mid = (low + high) >> 1; if (d.candles[mid].time < t.time) low = mid + 1; else high = mid; }
      const px = x(low + .5), py = y(t.price), direction = t.side === 'buy' ? 1 : -1;
      ctx.beginPath(); ctx.moveTo(px, py - direction * 4); ctx.lineTo(px - 3, py + direction * 3); ctx.lineTo(px + 3, py + direction * 3); ctx.closePath(); ctx.fill();
    }
  }
  ctx.strokeStyle = '#c3d5e399'; ctx.setLineDash([3, 4]); ctx.beginPath(); ctx.moveTo(cursor, top); ctx.lineTo(cursor, top + h); ctx.stroke();
  const price = processed ? d.candles[processed - 1].close : d.candles[0].open;
  ctx.strokeStyle = '#f3c77999'; ctx.beginPath(); ctx.moveTo(left, y(price)); ctx.lineTo(left + w, y(price)); ctx.stroke();
  ctx.restore();
  ctx.font = '10px Consolas'; ctx.fillStyle = '#8396a8';
  for (let i = 0; i <= 4; i++) {
    const c = d.candles[Math.min(d.candles.length - 1, Math.floor(d.candles.length * i / 4))];
    const label = new Date(c.time).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
    ctx.textAlign = i === 0 ? 'left' : i === 4 ? 'right' : 'center'; ctx.fillText(label, x(d.candles.length * i / 4), height - 5);
  }
  ctx.textAlign = 'left';
  if (chartBars.length < d.candles.length) { ctx.fillStyle = '#8292a5'; ctx.fillText('显示已聚合，撮合使用全部原始数据', left + 6, top + 11); }
}
$('replay-canvas').addEventListener('mousemove', event => {
  if (!chartScale || !dataset()) return;
  const c = chartScale, rect = $('replay-canvas').getBoundingClientRect(), px = event.clientX - rect.left;
  const i = Math.max(0, Math.min(dataset().candles.length - 1, Math.floor((px - c.left) / c.w * c.total)));
  const bar = dataset().candles[i]; $('chart-tooltip').hidden = false;
  $('chart-tooltip').textContent = `${date(bar.time)} · #${i + 1}${i >= processed ? ' · 未来' : ''}\n开 ${fmt(bar.open, 6)}　高 ${fmt(bar.high, 6)}\n低 ${fmt(bar.low, 6)}　收 ${fmt(bar.close, 6)}`;
});
$('replay-canvas').addEventListener('mouseleave', () => $('chart-tooltip').hidden = true);
function renderCharts() { drawPreview(); drawReplay(); }
window.addEventListener('resize', renderCharts);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) return;
  stopDrawingGesture();
  if (playing) { pause(); renderReplay(); }
});

const now = new Date(); now.setSeconds(0, 0);
$('start-time').value = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
load(); renderStrategies(); renderDatasets(); updateDrawing();
