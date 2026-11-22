/* Data conversion and Gate's anchored, rolling futures grid; also usable in Node. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GridLab = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const EPS = 1e-9;
  function number(value, name, min = 0, inclusive = false) {
    const n = Number(value);
    if (value === '' || value == null || !Number.isFinite(n) || (inclusive ? n < min : n <= min)) {
      throw new Error(`${name}必须${inclusive ? '大于或等于' : '大于'} ${min}`);
    }
    return n;
  }
  function integer(value, name, min, max) {
    const n = number(value, name, min, true);
    if (!Number.isInteger(n) || n > max) throw new Error(`${name}必须是 ${min}～${max} 的整数`);
    return n;
  }
  function columnMeans(rgba, width, height) {
    if (rgba.length !== width * height * 4) throw new Error('画布像素尺寸不匹配');
    const columns = Array(width).fill(null);
    for (let x = 0; x < width; x++) {
      let sum = 0, count = 0;
      for (let y = 0; y < height; y++) {
        if (rgba[(y * width + x) * 4 + 3] > 0) { sum += y; count++; }
      }
      if (count) columns[x] = sum / count;
    }
    return columns;
  }
  function pricesFromColumns(columns, startPrice, pixelValue) {
    const start = number(startPrice, '起始价格'), unit = number(pixelValue, '每像素幅度');
    const first = columns.findIndex(v => v != null);
    if (first < 0) throw new Error('请先在画布上绘制走势');
    let last = columns.length - 1;
    while (columns[last] == null) last--;
    if (last === first) throw new Error('请从左到右绘制至少两列像素');
    const ys = columns.slice(0, last + 1);
    for (let x = 0; x < first; x++) ys[x] = ys[first];
    let left = first;
    for (let right = first + 1; right <= last; right++) {
      if (ys[right] == null) continue;
      for (let x = left + 1; x < right; x++) ys[x] = ys[left] + (ys[right] - ys[left]) * (x - left) / (right - left);
      left = right;
    }
    const prices = ys.map(y => start + (ys[0] - y) * unit);
    if (prices.some(v => v <= 0 || !Number.isFinite(v))) throw new Error('走势价格必须大于 0，请提高起始价格或减小每像素幅度');
    return { prices, ys, first, last };
  }
  function candlesFromPrices(prices, pixelsPerCandle, startTime, intervalSeconds) {
    const size = integer(pixelsPerCandle, '每根 K 线像素数', 1, 1200);
    const interval = number(intervalSeconds, 'K 线周期');
    number(startTime, '起始时间', 0, true);
    const candles = [];
    for (let i = 0; i < prices.length; i += size) {
      const open = i === 0 ? prices[0] : prices[i - 1];
      const path = [open, ...prices.slice(i, i + size)];
      candles.push({ time: startTime + candles.length * interval * 1000, open,
        high: Math.max(...path), low: Math.min(...path), close: path.at(-1), volume: 0, path });
    }
    return validateCandles(candles);
  }
  function timestamp(value) {
    const n = Number(value);
    const time = Number.isFinite(n) && String(value).trim() !== '' ? (n < 1e11 ? n * 1000 : n) : Date.parse(value);
    if (!Number.isFinite(time) || time < 0 || time > 8.64e15) throw new Error('K 线时间无效');
    return time;
  }
  function validateCandles(rows) {
    if (!Array.isArray(rows) || !rows.length) throw new Error('没有可用的 K 线数据');
    if (rows.length > 200000) throw new Error('单个数据集最多支持 200,000 根 K 线，请分段导入');
    const candles = rows.map((r, i) => {
      if (!r || typeof r !== 'object') throw new Error(`第 ${i + 1} 根 K 线格式无效`);
      const c = { time: timestamp(r.time ?? r.timestamp ?? r.open_time),
        open: number(r.open, '开盘价'), high: number(r.high, '最高价'),
        low: number(r.low, '最低价'), close: number(r.close, '收盘价'),
        volume: number(r.volume ?? 0, '成交量', 0, true) };
      if (c.high < Math.max(c.open, c.close) || c.low > Math.min(c.open, c.close) || c.high < c.low) {
        throw new Error(`第 ${i + 1} 根 K 线的 OHLC 高低价不一致`);
      }
      if (r.path != null) {
        if (!Array.isArray(r.path) || r.path.length < 2 || r.path.length > 10000) throw new Error(`第 ${i + 1} 根 K 线路径无效`);
        c.path = r.path.map(v => number(v, '路径价格'));
        const near = (a, b) => Math.abs(a - b) <= Math.max(1, Math.abs(b)) * EPS;
        if (!near(c.path[0], c.open) || !near(c.path.at(-1), c.close) ||
            !near(Math.max(...c.path), c.high) || !near(Math.min(...c.path), c.low)) {
          throw new Error(`第 ${i + 1} 根 K 线的路径与 OHLC 不一致`);
        }
      }
      return c;
    });
    for (let i = 1; i < candles.length; i++) {
      if (candles[i].time <= candles[i - 1].time) throw new Error('K 线时间必须严格递增，不能重复');
    }
    return candles;
  }
  function parseCSV(text) {
    const rows = text.replace(/^\uFEFF/, '').trim().split(/\r?\n/).filter(l => l.trim()).map(l =>
      l.split(',').map(s => s.trim().replace(/^"|"$/g, '')));
    if (!rows.length || rows[0].length < 5) throw new Error('CSV 至少需要 time,open,high,low,close 五列');
    const header = rows[0].map(v => v.toLowerCase());
    const hasHeader = ['time', 'timestamp', 'open_time', 'open time', 'datetime', 'date'].includes(header[0]);
    let indices = [0, 1, 2, 3, 4, 5];
    if (hasHeader) {
      indices = [0, ...['open', 'high', 'low', 'close', 'volume'].map(key => header.indexOf(key))];
      if (indices.slice(1, 5).some(v => v < 0)) throw new Error('CSV 缺少 open / high / low / close 列');
      rows.shift();
    }
    return validateCandles(rows.map(r => ({ time: r[indices[0]], open: r[indices[1]], high: r[indices[2]],
      low: r[indices[3]], close: r[indices[4]], volume: indices[5] >= 0 ? (r[indices[5]] ?? 0) : 0 })));
  }
  function toCSV(candles) {
    return 'time,open,high,low,close,volume\n' + candles.map(c =>
      [c.time, c.open, c.high, c.low, c.close, c.volume].join(',')).join('\n');
  }
  function strategy(raw) {
    const s = { ...raw, model: 'gate-futures-v1', name: String(raw.name || '未命名策略'),
      referencePrice: number(raw.referencePrice, '参考价格', 0, true),
      marginAmountPerOrder: number(raw.marginAmountPerOrder, '每格保证金'),
      leverage: number(raw.leverage, '杠杆'),
      gridStepPct: number(raw.gridStepPct, '网格间隔比例'), takeProfitPct: number(raw.takeProfitPct, '止盈比例'),
      openGridCount: integer(raw.openGridCount, '活动开仓格数', 1, 300),
      takeProfitCount: integer(raw.takeProfitCount, '活动止盈单上限', 1, 300),
      takeProfitGridCount: integer(raw.takeProfitGridCount, '止盈检查网格范围', 1, 1000),
      capital: number(raw.capital, '回测资金'), fee: number(raw.fee, '手续费率', 0, true),
      quantityStep: number(raw.quantityStep, '张数步长'), priceStep: number(raw.priceStep, '价格步长'),
      multiplier: number(raw.multiplier, '每张合约面值'), minContracts: number(raw.minContracts, '最小张数'),
      maxContracts: number(raw.maxContracts ?? 0, '最大张数', 0, true) };
    if (!['long', 'short'].includes(s.direction)) throw new Error('请选择做多或做空');
    if (s.gridStepPct >= 1 || s.takeProfitPct >= 1) throw new Error('网格间隔和止盈比例必须小于 1；0.00042 表示 0.042%');
    if (s.fee >= 10) throw new Error('单边手续费率须小于 10%');
    if (s.maxContracts && s.maxContracts < s.minContracts) throw new Error('最大张数不能小于最小张数');
    return s;
  }
  function defaultStrategy() {
    // Trading defaults from example/grid_contract_ETH.py. Contract precision is
    // an offline example, and must be supplied for the actual contract.
    return { model: 'gate-futures-v1', direction: 'long', referencePrice: 0,
      marginAmountPerOrder: 2.7, leverage: 3, gridStepPct: .00042, takeProfitPct: .00042,
      openGridCount: 20, takeProfitCount: 20, takeProfitGridCount: 30,
      capital: 5000, fee: .02, quantityStep: .001, priceStep: .0001,
      multiplier: 1, minContracts: .001, maxContracts: 0 };
  }
  function migrateStrategy(raw) {
    if (raw.model === 'gate-futures-v1') return { ...raw };
    const result = { ...defaultStrategy(), id: raw.id, name: raw.name, color: raw.color, enabled: raw.enabled,
      legacyConfiguration: { ...raw } };
    if (Number(raw.capital) > 0) result.capital = Number(raw.capital);
    if (Number(raw.fee) >= 0) result.fee = Number(raw.fee);
    if (Number(raw.quantityStep) > 0) result.quantityStep = result.minContracts = Number(raw.quantityStep);
    if (Number(raw.amount) > 0) result.marginAmountPerOrder = Number(raw.amount) / result.leverage;
    return result;
  }
  function roundStep(value, step, mode = 'nearest') {
    let units = value / step;
    if (!Number.isFinite(units) || Math.abs(units) > 1e13) throw new Error('价格或数量超出支持精度，请调整参考价与步长');
    // Correct binary noise at exact decimal step boundaries, not real fractions.
    const nearest = Math.round(units), fuzz = Math.max(1, Math.abs(units)) * Number.EPSILON * 8;
    if (Math.abs(units - nearest) <= fuzz) units = nearest;
    const rounded = mode === 'up' ? Math.ceil(units) : mode === 'down' ? Math.floor(units) : Math.floor(units + .5 + fuzz);
    return Number((rounded * step).toPrecision(15));
  }
  class GridEngine {
    constructor(config, initialPrice, time = 0) {
      this.config = strategy(config);
      const s = this.config;
      this.reference = s.referencePrice || number(initialPrice, '初始行情价格');
      this.long = s.direction === 'long'; this.sign = this.long ? 1 : -1;
      this.ratio = this.long ? 1 - s.gridStepPct : 1 + s.gridStepPct;
      this.feeRate = s.fee / 100;
      this.cash = s.capital; this.realized = 0; this.fees = 0; this.cycles = 0;
      this.trades = []; this.tradeCount = 0; this.maxDrawdown = 0; this.peak = s.capital;
      this.price = number(initialPrice, '初始行情价格'); this.time = time;
      this.history = []; this.maxPositions = 0; this.maxMarginUsed = 0;
      this.positions = new Map(); this.openOrders = new Map(); this.priceCache = new Map();
      this.refresh(this.price);
      this.mark(this.price);
    }
    tolerance(p) { return Math.max(Math.abs(p) * 1e-13, this.config.priceStep * 1e-7); }
    gridPrice(index) {
      if (this.priceCache.has(index)) return this.priceCache.get(index);
      const value = roundStep(this.reference * this.ratio ** index, this.config.priceStep);
      if (value <= 0) throw new Error('网格价格小于等于 0，请调整间隔或价格精度');
      if (this.priceCache.size > 20000) this.priceCache.clear();
      this.priceCache.set(index, value); return value;
    }
    firstIndex(current) {
      let index = Math.floor(Math.log(current / this.reference) / Math.log(this.ratio)) + 1;
      let guard = 0;
      const outside = price => this.long ? price >= current : price <= current;
      while (outside(this.gridPrice(index))) {
        if (++guard > 30000) throw new Error('网格间距小于价格精度');
        index++;
      }
      while (!outside(this.gridPrice(index - 1))) {
        if (++guard > 30000) throw new Error('网格间距小于价格精度');
        index--;
      }
      return index;
    }
    desiredPrices(current) {
      const start = this.firstIndex(current), result = [];
      for (let index = start; result.length < this.config.openGridCount; index++) {
        if (index - start > this.config.openGridCount * 100) throw new Error('网格间距小于价格精度');
        const price = this.gridPrice(index);
        if (!result.length || result.at(-1).price !== price) result.push({ price, grid: index });
      }
      return result;
    }
    takeProfitPrice(price) {
      const s = this.config;
      return roundStep(price * (1 + this.sign * s.takeProfitPct), s.priceStep, this.long ? 'up' : 'down');
    }
    plan(entry) {
      const s = this.config, contracts = roundStep(s.marginAmountPerOrder * s.leverage / (entry.price * s.multiplier), s.quantityStep, 'down');
      if (contracts <= 0 || contracts < s.minContracts) throw new Error('每格保证金不足最小张数，请调整保证金、杠杆或合约规格');
      if (s.maxContracts && contracts > s.maxContracts) throw new Error('每格张数超过合约最大下单数量');
      const quantity = contracts * s.multiplier, notional = entry.price * quantity;
      const tp = this.takeProfitPrice(entry.price);
      if (tp <= 0 || (this.long ? tp <= entry.price : tp >= entry.price)) throw new Error('止盈比例小于价格精度，无法形成有效平仓价格');
      return { ...entry, contracts, quantity, tp, margin: notional / s.leverage, entryFee: notional * this.feeRate };
    }
    grossUnrealized(price) {
      let result = 0;
      for (const p of this.positions.values()) result += this.sign * (price - p.price) * p.quantity;
      return result;
    }
    refresh(price) {
      const desired = this.desiredPrices(price), desiredSet = new Set(desired.map(p => p.price));
      // Cancel window-external OPEN orders only. Old positions retain ownership
      // of their rounded entry price, even when their TP is currently inactive.
      for (const p of this.openOrders.keys()) if (!desiredSet.has(p)) this.openOrders.delete(p);
      let heldMargin = 0;
      for (const p of this.positions.values()) heldMargin += p.margin;
      let reserved = 0;
      for (const p of this.openOrders.values()) reserved += p.margin + p.entryFee;
      let available = this.cash + this.grossUnrealized(price) - heldMargin - reserved;
      for (const entry of desired) {
        if (this.positions.has(entry.price) || this.openOrders.has(entry.price)) continue;
        const order = this.plan(entry), reserve = order.margin + order.entryFee;
        // Gate stops a placement batch on insufficient available margin.
        if (reserve > available + this.tolerance(price)) break;
        this.openOrders.set(entry.price, order); available -= reserve; reserved += reserve;
      }
      this.reserved = reserved;
      const bound = this.gridPrice(this.firstIndex(price) - this.config.takeProfitGridCount);
      this.takeProfitBound = bound;
      const candidates = [];
      for (const p of this.positions.values()) {
        p.tpOpen = false;
        if (this.long ? price < p.tp && p.tp <= bound : bound <= p.tp && p.tp < price) candidates.push(p);
      }
      candidates.sort((a, b) => Math.abs(a.tp - price) - Math.abs(b.tp - price) || a.grid - b.grid);
      for (const p of candidates.slice(0, this.config.takeProfitCount)) p.tpOpen = true;
    }
    fill(order, kind, time) {
      const opening = kind === 'open', price = opening ? order.price : order.tp;
      const side = opening === this.long ? 'buy' : 'sell';
      const fee = price * order.quantity * this.feeRate;
      let profit = null;
      if (opening) {
        this.openOrders.delete(order.price); this.positions.set(order.price, { ...order, entryFee: fee, tpOpen: false });
        this.cash -= fee;
      } else {
        const gross = this.sign * (price - order.price) * order.quantity;
        profit = gross - fee - order.entryFee;
        this.realized += profit; this.cash += gross - fee; this.positions.delete(order.price); this.cycles++;
      }
      this.fees += fee; this.tradeCount++;
      this.trades.push({ strategyId: this.config.id, strategy: this.config.name,
        direction: this.config.direction, kind, reduceOnly: !opening, time, side, price,
        contracts: order.contracts, quantity: order.quantity, fee, profit, grid: order.grid });
      if (this.trades.length > 10000) this.trades.splice(0, 1000);
      this.price = price; this.mark(price);
    }
    segment(from, to, time) {
      if (from === to) { this.price = to; this.refresh(to); this.mark(to); return; }
      const rising = to > from, direction = rising ? 1 : -1;
      let cursor = from, guard = 0;
      while (direction * (to - cursor) > this.tolerance(to)) {
        if (++guard > 200000) throw new Error('单段跨越网格过多，请增大网格间隔或细分行情');
        // Crossing an anchored grid boundary changes the strict (< / >) rolling
        // window. Reconcile infinitesimally after that boundary, without moving
        // execution prices or treating inactive TPs as historical fills.
        this.refresh(cursor + direction * this.tolerance(cursor));
        const opening = rising !== this.long;
        const candidates = opening ? [...this.openOrders.values()] : [...this.positions.values()].filter(p => p.tpOpen);
        let event = null, target = to;
        for (const p of candidates) {
          const value = opening ? p.price : p.tp;
          if (direction * (value - cursor) > this.tolerance(cursor) && direction * (to - value) >= -this.tolerance(to)
              && (!event || direction * (value - target) < 0)) { event = p; target = value; }
        }
        let index = this.firstIndex(cursor + direction * this.tolerance(cursor));
        let boundary;
        const increment = (rising === this.long) ? -1 : 1;
        if (increment < 0) index--;
        for (let n = 0; ; n++, index += increment) {
          if (n > 30000) throw new Error('网格间距小于价格精度');
          boundary = this.gridPrice(index);
          if (direction * (boundary - cursor) > this.tolerance(cursor)) break;
        }
        if (direction * (boundary - target) < -this.tolerance(target)) { target = boundary; event = null; }
        // Fill all resting orders at the reached price before cancelling window
        // orders. Multiple positions may share one take-profit price.
        if (event) {
          for (const p of candidates) if (Math.abs((opening ? p.price : p.tp) - target) <= this.tolerance(target)) {
            this.fill(p, opening ? 'open' : 'close', time);
          }
        }
        cursor = target; this.price = cursor; this.mark(cursor);
      }
      this.price = to; this.refresh(to); this.mark(to);
    }
    advance(candle, pathMode = 'low-first') {
      const path = candle.path || (pathMode === 'high-first'
        ? [candle.open, candle.high, candle.low, candle.close]
        : [candle.open, candle.low, candle.high, candle.close]);
      // A gap crosses resting limits at their limit prices, without favorable gap price improvement.
      this.segment(this.price, path[0], candle.time);
      for (let i = 1; i < path.length; i++) this.segment(path[i - 1], path[i], candle.time);
      this.time = candle.time;
      const result = this.snapshot();
      this.history.push({ time: candle.time, equity: result.equity, pnl: result.pnl });
      return result;
    }
    mark(price) {
      const value = this.cash + this.grossUnrealized(price);
      this.peak = Math.max(this.peak, value); this.maxDrawdown = Math.max(this.maxDrawdown, this.peak - value);
      this.maxPositions = Math.max(this.maxPositions, this.positions.size);
      let margin = 0; for (const p of this.positions.values()) margin += p.margin;
      this.maxMarginUsed = Math.max(this.maxMarginUsed, margin);
    }
    snapshot() {
      const holdings = [...this.positions.values()];
      const contracts = holdings.reduce((sum, p) => sum + p.contracts, 0);
      const quantity = holdings.reduce((sum, p) => sum + p.quantity, 0);
      const entryFees = holdings.reduce((sum, p) => sum + p.entryFee, 0);
      const marginUsed = holdings.reduce((sum, p) => sum + p.margin, 0);
      const gross = this.grossUnrealized(this.price), equity = this.cash + gross;
      const orders = [...this.openOrders.values()].map(p => ({ grid: p.grid, kind: 'open', reduceOnly: false,
        side: this.long ? 'buy' : 'sell', price: p.price, quantity: p.quantity, contracts: p.contracts }));
      for (const p of holdings) if (p.tpOpen) orders.push({ grid: p.grid, kind: 'close', reduceOnly: true,
        side: this.long ? 'sell' : 'buy', price: p.tp, quantity: p.quantity, contracts: p.contracts });
      return { id: this.config.id, name: this.config.name, price: this.price, cash: this.cash,
        direction: this.config.direction, reference: this.reference,
        reserved: this.reserved, available: equity - marginUsed - this.reserved, quantity, contracts, marginUsed,
        positions: holdings.length, marketValue: quantity * this.price, unrealized: gross - entryFees,
        realized: this.realized, fees: this.fees, cycles: this.cycles, equity,
        pnl: equity - this.config.capital, maxDrawdown: this.maxDrawdown, maxMarginUsed: this.maxMarginUsed,
        waitingTakeProfits: holdings.filter(p => !p.tpOpen).length, takeProfitBound: this.takeProfitBound,
        maxPositions: this.maxPositions, tradeCount: this.tradeCount, orders };
    }
  }
  return { columnMeans, pricesFromColumns, candlesFromPrices, validateCandles, parseCSV, toCSV,
    strategy, defaultStrategy, migrateStrategy, roundStep, GridEngine };
});
