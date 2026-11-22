(function () {
  'use strict';
  const $ = id => document.getElementById(id), model = window.GridModel;
  const definitions = [
    ['dailyTrades', '每日成交次数', 'd', '轮/天', 1, 0, 100, 'primary-fields'],
    ['spacing', '网格间隔', 'a', '%', 0.05, 0.05, 5, 'primary-fields'],
    ['takeProfit', '每格止盈', 'b', '%', 0.05, 0.05, 5, 'primary-fields'],
    ['notional', '每格仓位', 'n', 'USDT', 10, 10, 1000, 'primary-fields'],
    ['fee', '单边手续费', 'm', '%', 0.005, 0, 0.5, 'primary-fields'],
    ['days', '运行天数', 'T', '天', 1, null, null, 'grid-fields'],
    ['initialPrice', '交易对开始价格', 'P₀', 'USDT', 1, null, null, 'grid-fields'],
    ['minQuantity', '交易对最小张数', 'q', '张', 0.001, null, null, 'grid-fields'],
    ['dropPercent', '价格下跌百分比', 'r', '%', 0.1, 0, 99.9, 'scenario-fields']
  ];
  const names = { ...Object.fromEntries(definitions.map(d => [d[0], d[1]])), countMode: '成交次数口径' };
  const colors = { net: '#10847b', gross: '#366ce1', fees: '#b86a21' };
  const labels = { net: '累计净收益', gross: '手续费前毛收益', fees: '累计手续费' };
  const hidden = new Set(), storageKey = 'grid-profit-model-v4';
  let result = null, timer = null, charts = [];
  const number = (value, digits = 2) => new Intl.NumberFormat('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
  const signed = value => (value > 0 ? '+' : '') + number(value);
  const priceNumber = value => Math.abs(value) > 0 && Math.abs(value) < 0.0001 ? value.toExponential(3) : number(value, Math.abs(value) < 1 ? 6 : 2);
  const quantityNumber = value => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 8 }).format(value);
  function savedParameters() {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || localStorage.getItem('grid-profit-model-v3') || localStorage.getItem('grid-profit-model-v2') || localStorage.getItem('grid-profit-model-v1'));
      if (saved) return model.validate(saved);
    } catch (_) { /* Optional browser storage. */ }
    return { ...model.defaults };
  }
  function createInputs() {
    const initial = savedParameters();
    $('countMode').value = initial.countMode;
    definitions.forEach(([id, label, symbol, unit, step, sliderMin, sliderMax, container]) => {
      const wrapper = document.createElement('div'); wrapper.className = 'field';
      const [min, max] = model.limits[id], numberStep = id === 'days' ? '1' : 'any';
      wrapper.innerHTML = `<div class="field-top"><label class="field-name" for="${id}">${label}<span class="field-symbol">${symbol}</span></label><div class="input-wrap"><input id="${id}" name="${id}" type="number" min="${min}" max="${max}" step="${numberStep}" inputmode="decimal" required aria-label="${label}（${unit}）"><span class="input-unit" id="${id}-unit">${unit}</span></div></div>` +
        (sliderMin !== null ? `<input id="${id}-range" type="range" min="${sliderMin}" max="${sliderMax}" step="${step}" aria-label="${label}滑块"><div class="range-ends" id="${id}-ends"><span>${sliderMin}${unit}</span><span>${sliderMax}${unit}</span></div>` : '');
      $(container).append(wrapper); $(id).value = initial[id];
      if (sliderMin !== null) {
        $(`${id}-range`).value = initial[id];
        $(`${id}-range`).addEventListener('input', event => { $(id).value = event.target.value; schedule(); });
      }
      $(id).addEventListener('input', () => {
        if ($(`${id}-range`)) $(`${id}-range`).value = $(id).value;
        schedule();
      });
    });
    $('countMode').addEventListener('change', schedule);
  }
  function readInputs() {
    const p = { countMode: $('countMode').value };
    for (const [id] of definitions) {
      if ($(id).value.trim() === '') throw new Error(`${names[id]}不能为空`);
      p[id] = Number($(id).value);
    }
    try { return model.validate(p); }
    catch (error) {
      let message = error.message;
      Object.keys(names).forEach(key => { message = message.replace(key, names[key]); });
      throw new Error(message);
    }
  }
  function schedule() {
    clearTimeout(timer); $('export').disabled = true;
    document.body.classList.add('busy'); $('validation').hidden = true;
    $('status').textContent = '等待计算';
    timer = setTimeout(update, 60);
  }
  function update() {
    try {
      const p = readInputs(); result = model.project(p);
      try { localStorage.setItem(storageKey, JSON.stringify(p)); } catch (_) { /* Optional. */ }
      renderResults(); document.body.classList.remove('busy');
      $('export').disabled = false; $('status').textContent = '已更新 · 按成交次数';
    } catch (error) {
      $('validation').textContent = error.message; $('validation').hidden = false;
      $('status').textContent = '参数无效 · 结果未更新';
    }
  }
  function renderResults() {
    const r = result, p = r.parameters, orders = p.countMode === 'orders', unit = orders ? '笔/天' : '轮/天';
    $('dailyTrades-unit').textContent = unit;
    $('dailyTrades').setAttribute('aria-label', `每日成交次数（${unit}）`);
    $('dailyTrades-ends').innerHTML = `<span>0${unit}</span><span>100${unit}</span>`;
    $('count-hint').textContent = `${number(p.dailyTrades)} ${unit}，折算为 ${number(r.dailyRounds)} 完整买卖轮/天。`;
    $('run-description').textContent = `${p.days} 天 · 每日 ${number(p.dailyTrades)} ${orders ? '笔单边成交' : '轮完整买卖'} · 固定仓位、不复利`;
    $('unit-profit').textContent = `${r.unitNet > 0 ? '+' : ''}${number(r.unitNet, 4)} USDT`;
    $('unit-profit').style.color = r.unitNet < 0 ? '#c34653' : '';
    $('break-even').textContent = `手续费盈亏平衡止盈：${number(r.breakEvenPct, 4)}%`;
    const warnings = [];
    if (r.unitNet <= 0) warnings.push('当前止盈不足以覆盖双边手续费，每完成一轮都会亏损。');
    if (r.scenario.skippedGrids > 0) warnings.push(`下跌范围内有 ${r.scenario.skippedGrids} 格不足最小张数，已从取整后的实际持仓中排除；预算持仓仍按每格仓位计算。`);
    $('warnings').textContent = warnings.join(' '); $('warnings').hidden = !warnings.length;
    $('total-net').textContent = signed(r.totalNet);
    $('total-period').textContent = `${p.days} 天 · 每格按预算 ${number(p.notional)} USDT 估算`;
    $('daily-net').textContent = signed(r.dailyNet) + ' USDT';
    $('daily-rounds').textContent = `${number(r.dailyRounds)} 轮完整买卖 / 天`;
    $('total-fees').textContent = number(r.totalFees) + ' USDT';
    $('total-rounds').textContent = number(r.totalRounds) + ' 轮';
    $('total-orders').textContent = `对应 ${number(r.totalOrders)} 笔单边成交`;
    $('sensitivity-description').textContent = `保持其他参数不变 · ${p.days} 天 · 当前 ${number(p.dailyTrades)} ${unit}`;
    const stats = [
      ['单轮毛收益', number(r.unitGross) + ' USDT'], ['单轮双边手续费', number(r.unitFees, 4) + ' USDT'],
      ['单轮净收益', number(r.unitNet, 4) + ' USDT'], ['累计毛收益', number(r.totalGross) + ' USDT'],
      ['单格预算含买入费', number(r.buyCost) + ' USDT'], ['跌幅内取整后买入金额', number(r.totalGridNotional) + ' USDT'],
      ['跌幅内买入所需金额（含费）', number(r.gridBudget) + ' USDT'],
      ['网格买价范围', `${priceNumber(r.levels.at(-1).buy)} ～ ${priceNumber(r.levels[0].buy)}`]
    ];
    $('account-stats').replaceChildren(...stats.map(([name, value]) => {
      const row = document.createElement('div'); row.className = 'stat-row';
      const dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = name; dd.textContent = value;
      row.append(dt, dd); return row;
    }));
    const scenario = r.scenario;
    $('drop-description').textContent = `从 ${priceNumber(p.initialPrice)} 下跌 ${number(p.dropPercent)}%，目标价格 ${priceNumber(scenario.targetPrice)} USDT`;
    $('max-open-grids').textContent = `${r.gridCount} 格`;
    $('touched-grids').textContent = `自动计算：第 0 格至第 ${r.gridCount - 1} 格`;
    $('max-notional').textContent = `${number(scenario.budgetNotional)} USDT`;
    $('budget-calculation').textContent = `${r.gridCount} 格 × ${number(p.notional)} USDT / 格`;
    $('rounded-notional').textContent = `${number(scenario.maxNotional)} USDT`;
    $('max-quantity').textContent = `实际可下单 ${scenario.maxOpenGrids} 格 · 累计 ${quantityNumber(scenario.totalQuantity)} 张`;
    $('grid-count-formula').textContent = `等比例网格格数 = floor[ln(1−${p.dropPercent}/100) ÷ ln(1−${p.spacing}/100)] + 1 = ${r.gridCount} 格（包含第 0 格）`;
    $('scenario-cost').textContent = `买入手续费 ${number(scenario.entryFees)} USDT · 买入所需金额（含费）${number(scenario.requiredFunds)} USDT`;
    const displayLimit = 2000;
    $('grid-table-summary').textContent = r.gridCount > displayLimit
      ? `共 ${r.gridCount} 格，网页显示前 ${displayLimit} 格；统计按全部网格计算，CSV 包含全部明细。`
      : `共 ${r.gridCount} 格，全部由间隔和跌幅自动计算。`;
    $('grid-prices').replaceChildren(...r.levels.slice(0, displayLimit).map(level => {
      const row = document.createElement('tr');
      if (level.grid < scenario.touchedGrids) row.className = 'within-drop';
      [level.grid, priceNumber(level.buy), priceNumber(level.sell), number(level.dropPercent, 4) + '%', quantityNumber(level.quantity), number(level.actualNotional, 4), number(level.cumulativeNotional, 4), number(level.cumulativeBudget)].forEach(value => { const td = document.createElement('td'); td.textContent = value; row.append(td); });
      return row;
    }));
    redrawCharts();
  }
  function redrawCharts() {
    charts.forEach(chart => chart.destroy()); charts = [];
    const p = result.parameters, countMax = Math.max(40, p.dailyTrades * 2);
    const sensitivity = [0, countMax].map(x => ({ x, net: x / (p.countMode === 'orders' ? 2 : 1) * p.days * result.unitNet }));
    charts.push(drawChart($('pnl-chart'), result.points, Object.keys(labels).filter(key => !hidden.has(key)), p.days, '时间（天）'));
    charts.push(drawChart($('frequency-chart'), sensitivity, ['net'], countMax, p.countMode === 'orders' ? '单边成交笔数（笔/天）' : '完成轮数（轮/天）', p.dailyTrades));
  }
  function legend() {
    for (const key of Object.keys(labels)) {
      const button = document.createElement('button'); button.type = 'button'; button.dataset.series = key;
      button.setAttribute('aria-pressed', 'true');
      button.innerHTML = `<span class="legend-swatch" style="--series-color:${colors[key]}"></span>${labels[key]}`;
      button.addEventListener('click', () => {
        if (hidden.has(key)) hidden.delete(key); else hidden.add(key);
        button.setAttribute('aria-pressed', String(!hidden.has(key)));
        if (result) redrawCharts();
      });
      $('pnl-legend').append(button);
    }
  }
  const ns = 'http://www.w3.org/2000/svg';
  function element(name, attributes, parent, text) {
    const node = document.createElementNS(ns, name);
    Object.entries(attributes || {}).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  function tickLabel(value) {
    if (Math.abs(value) >= 1e9) return number(value / 1e9, 1) + 'B';
    if (Math.abs(value) >= 1e6) return number(value / 1e6, 1) + 'M';
    if (Math.abs(value) >= 1e4) return number(value / 1000, 1) + 'k';
    if (Math.abs(value) >= 10) return number(value, 0);
    if (Math.abs(value) >= 0.01 || value === 0) return number(value);
    return value.toExponential(1);
  }
  function drawChart(container, points, keys, maxX, xLabel, selectedX = null) {
    function redraw() {
      container.replaceChildren();
      const width = container.clientWidth, height = container.clientHeight;
      // Responsive reflow can briefly report an almost collapsed container.
      if (width < 110 || height < 100) return;
      const left = width < 400 ? 57 : 66, right = width - 15, top = 25, bottom = height - 40;
      let min = 0, max = 0;
      keys.forEach(key => points.forEach(point => { min = Math.min(min, point[key]); max = Math.max(max, point[key]); }));
      const padding = (max - min || 1) * 0.12; min -= padding; max += padding;
      const x = value => left + value / maxX * (right - left), y = value => bottom - (value - min) / (max - min) * (bottom - top);
      const svg = element('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': container.getAttribute('aria-label') }, container);
      element('title', {}, svg, container.getAttribute('aria-label'));
      const clipId = 'clip-' + container.id, defs = element('defs', {}, svg), clip = element('clipPath', { id: clipId }, defs);
      element('rect', { x: left, y: top, width: right - left, height: bottom - top }, clip);
      element('rect', { x: left, y: top, width: right - left, height: bottom - top, class: 'frame' }, svg);
      for (let i = 0; i <= 4; i++) {
        const value = min + (max - min) * i / 4, yy = y(value);
        element('line', { x1: left, x2: right, y1: yy, y2: yy, class: 'grid' }, svg);
        element('text', { x: left - 10, y: yy + 4, 'text-anchor': 'end' }, svg, tickLabel(value));
      }
      const ticks = width < 400 ? 3 : 5;
      for (let i = 0; i <= ticks; i++) {
        const value = maxX * i / ticks;
        element('text', { x: x(value), y: bottom + 20, 'text-anchor': i === 0 ? 'start' : i === ticks ? 'end' : 'middle' }, svg, maxX < 5 ? number(value, 1) : tickLabel(value));
      }
      element('text', { x: left, y: 13, class: 'axis-title' }, svg, '金额（USDT）');
      element('text', { x: right, y: height - 3, 'text-anchor': 'end', class: 'axis-title' }, svg, xLabel);
      const plot = element('g', { 'clip-path': `url(#${clipId})` }, svg);
      element('line', { x1: left, x2: right, y1: y(0), y2: y(0), class: 'zero', 'stroke-dasharray': '4 4' }, plot);
      keys.forEach(key => {
        const path = points.map((point, i) => `${i ? 'L' : 'M'}${x(point.x).toFixed(2)},${y(point[key]).toFixed(2)}`).join(' ');
        element('path', { d: path, fill: 'none', stroke: colors[key], 'stroke-width': key === 'net' ? 2.2 : 1.5, 'stroke-linejoin': 'round' }, plot);
      });
      if (selectedX !== null) {
        const selectedY = points.at(-1).net * selectedX / maxX;
        element('line', { x1: x(selectedX), x2: x(selectedX), y1: top, y2: bottom, class: 'guide' }, plot);
        element('circle', { cx: x(selectedX), cy: y(selectedY), r: 4, fill: colors.net }, plot);
      }
      const guide = element('line', { y1: top, y2: bottom, class: 'guide', visibility: 'hidden' }, plot);
      const markers = keys.map(key => element('circle', { r: 3.5, fill: colors[key], stroke: '#fff', 'stroke-width': 1.5, visibility: 'hidden' }, plot));
      const overlay = element('rect', { x: left, y: top, width: right - left, height: bottom - top, class: 'overlay' }, svg);
      function hover(event) {
        const rect = svg.getBoundingClientRect(), xx = Math.max(left, Math.min(right, event.clientX - rect.left));
        const at = (xx - left) / (right - left) * maxX;
        guide.setAttribute('x1', xx); guide.setAttribute('x2', xx); guide.setAttribute('visibility', 'visible');
        const rows = keys.map((key, i) => {
          const value = points.at(-1)[key] * at / maxX;
          markers[i].setAttribute('cx', xx); markers[i].setAttribute('cy', y(value)); markers[i].setAttribute('visibility', 'visible');
          return `<div class="tip-row"><span>${labels[key]}</span><span>${number(value)} USDT</span></div>`;
        });
        const tip = $('chart-tooltip');
        tip.innerHTML = `<div class="tip-title">${selectedX === null ? '第 ' + number(at) + ' 天' : '每日 ' + number(at) + (result.parameters.countMode === 'orders' ? ' 笔单边成交' : ' 轮完整买卖')}</div>` + rows.join('');
        tip.hidden = false;
        const box = tip.getBoundingClientRect();
        tip.style.left = Math.max(8, Math.min(window.innerWidth - box.width - 8, event.clientX + 15)) + 'px';
        tip.style.top = Math.max(8, Math.min(window.innerHeight - box.height - 8, event.clientY - box.height - 12)) + 'px';
      }
      overlay.addEventListener('pointermove', hover); overlay.addEventListener('pointerdown', hover);
      overlay.addEventListener('pointerleave', () => {
        $('chart-tooltip').hidden = true; guide.setAttribute('visibility', 'hidden'); markers.forEach(marker => marker.setAttribute('visibility', 'hidden'));
      });
    }
    const observer = new ResizeObserver(redraw); observer.observe(container); redraw();
    return { destroy() { observer.disconnect(); $('chart-tooltip').hidden = true; } };
  }
  function exportCsv() {
    if (!result) return;
    const r = result, p = r.parameters;
    const rows = [['记录类型', '时间(天)', '净收益(USDT)', '毛收益(USDT)', '手续费(USDT)', '完成轮数', '参数/指标', '数值', '买入价(USDT)', '止盈价(USDT)', '下跌幅度(%)', '持仓张数', '实际买入额(USDT)', '累计实际仓位(USDT)', '累计预算仓位(USDT)']];
    Object.entries(p).forEach(([key, value]) => rows.push(['参数', '', '', '', '', '', names[key], key === 'countMode' ? (value === 'rounds' ? '完整买卖轮次' : '单边成交笔数') : value, '', '']));
    rows.push(['说明', '', '', '', '', '', '模型范围', '格数由间隔和跌幅自动计算；最大预算持仓=格数×每格预算；实际持仓按最小张数取整；从第0格开始，不限制资金', '', '']);
    const summary = { dailyRounds: '每日完成轮数', totalNet: '累计净收益', totalGross: '累计毛收益', totalFees: '累计手续费', unitNet: '单轮净收益', totalRounds: '累计完成轮数', totalOrders: '单边成交笔数', buyCost: '单格预算含买入费', gridBudget: '全网格实际买入金额含费', totalGridNotional: '全网格取整后累计仓位', totalGridQuantity: '全网格累计张数' };
    Object.entries(summary).forEach(([key, label]) => rows.push(['统计', '', '', '', '', '', label, r[key], '', '']));
    r.points.forEach(point => rows.push(['累计收益', point.x, point.net, point.gross, point.fees, point.rounds, '', '', '', '']));
    const scenarioSummary = { targetPrice: '下跌后价格', touchedGrids: '动态计算网格数', budgetNotional: '最大预算持仓金额', maxOpenGrids: '实际可下单格数', maxNotional: '取整后实际持仓金额', totalQuantity: '累计持仓张数', entryFees: '实际买入手续费', requiredFunds: '实际买入所需金额含费', skippedGrids: '不足最小张数的网格数' };
    Object.entries(scenarioSummary).forEach(([key, label]) => rows.push(['下跌情景', '', '', '', '', '', label, r.scenario[key], '', '']));
    r.levels.forEach(level => rows.push(['网格价格', '', '', '', '', '', '网格序号', level.grid, level.buy, level.sell, level.dropPercent, level.quantity, level.actualNotional, level.cumulativeNotional, level.cumulativeBudget]));
    const csv = '\uFEFF' + rows.map(row => Array.from({ length: rows[0].length }, (_, i) => '"' + String(row[i] ?? '').replaceAll('"', '""') + '"').join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a'); a.href = url; a.download = `grid-return-${p.days}d-${p.dailyTrades}-${p.countMode}.csv`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  createInputs(); legend();
  $('parameters').addEventListener('submit', event => event.preventDefault());
  $('code-example').addEventListener('click', () => {
    const example = { initialPrice: 2600, spacing: 0.05, minQuantity: 0.001, notional: 40, dropPercent: 39.33 };
    Object.entries(example).forEach(([id, value]) => { $(id).value = value; if ($(`${id}-range`)) $(`${id}-range`).value = value; });
    schedule();
  });
  $('reset').addEventListener('click', () => {
    definitions.forEach(([id]) => { $(id).value = model.defaults[id]; if ($(`${id}-range`)) $(`${id}-range`).value = model.defaults[id]; });
    $('countMode').value = model.defaults.countMode; schedule();
  });
  $('export').addEventListener('click', exportCsv);
  schedule();
})();
