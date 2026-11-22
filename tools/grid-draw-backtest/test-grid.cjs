const { test } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const M = require('./model.js');
const TIME = Date.UTC(2026, 10, 16);
const close = (a, b, tolerance = 1e-8) => assert.ok(Math.abs(a - b) < tolerance, `${a} != ${b}`);
const config = overrides => ({ ...M.defaultStrategy(), id: 'a', name: 'test', referencePrice: 100,
  gridStepPct: .1, takeProfitPct: .1, openGridCount: 2, takeProfitCount: 2, takeProfitGridCount: 5,
  marginAmountPerOrder: 90, leverage: 1, capital: 5000, fee: .1, priceStep: .0001,
  multiplier: 1, quantityStep: .001, minContracts: .001, ...overrides });
const candle = (prices, time = TIME) => ({ time, open: prices[0], high: Math.max(...prices),
  low: Math.min(...prices), close: prices.at(-1), volume: 0, path: prices });

test('Gate starts strictly below/above current without opening at the reference price', () => {
  const long = new M.GridEngine(config(), 100), short = new M.GridEngine(config({ direction: 'short', marginAmountPerOrder: 200 }), 100);
  assert.deepEqual(long.snapshot().orders.map(p => p.price), [90, 81]);
  assert.deepEqual(short.snapshot().orders.map(p => p.price), [110, 121]);
  assert.equal(long.tradeCount, 0); assert.equal(short.tradeCount, 0);
  assert.ok(short.snapshot().orders.every(p => p.side === 'sell' && !p.reduceOnly));
  assert.equal(new M.GridEngine(config({ referencePrice: 0 }), 123).reference, 123);
});
test('rolling window cancels old pending opens and supports negative indices above/below reference', () => {
  const e = new M.GridEngine(config(), 100);
  e.advance(candle([100, 140]));
  assert.equal(e.positions.size, 0);
  assert.ok(e.snapshot().orders.every(p => p.price > 100 && p.price < 140 && p.grid < 0));
  assert.equal(e.snapshot().orders.length, 2);
});
test('independent TP ratio completes repeated cycles and charges two-sided fees', () => {
  const e = new M.GridEngine(config(), 100);
  e.advance(candle([100, 90, 99, 90, 99]));
  const s = e.snapshot(); assert.equal(s.cycles, 2); assert.equal(s.tradeCount, 4);
  close(s.realized, 17.622); close(s.fees, .378); close(s.pnl, 17.622);
  assert.equal(s.positions, 0);
  assert.deepEqual(e.trades.map(p => [p.kind, p.side, p.reduceOnly]), [
    ['open', 'buy', false], ['close', 'sell', true], ['open', 'buy', false], ['close', 'sell', true]]);
});
test('short opens by selling and closes by reduce-only buying below entry', () => {
  const e = new M.GridEngine(config({ direction: 'short', marginAmountPerOrder: 110 }), 100);
  e.advance(candle([100, 110, 99]));
  assert.equal(e.cycles, 1); close(e.realized, 10.791); close(e.fees, .209);
  assert.deepEqual(e.trades.map(p => [p.side, p.price, p.reduceOnly]), [['sell', 110, false], ['buy', 99, true]]);
});
test('contract amount uses margin, leverage, multiplier and contracts step', () => {
  const e = new M.GridEngine(config({ marginAmountPerOrder: 1.6, leverage: 2, multiplier: .01, quantityStep: 1, minContracts: 1 }), 100);
  const order = e.plan({ price: 145, grid: 0 });
  assert.equal(order.contracts, 2); close(order.quantity, .02); close(order.margin, 1.45);
  assert.throws(() => new M.GridEngine(config({ marginAmountPerOrder: .01 }), 100), /最小张数/);
  assert.throws(() => new M.GridEngine(config({ marginAmountPerOrder: 1000, maxContracts: 2 }), 100), /最大下单/);
});
test('grid HALF_UP, long TP ROUND_UP and short TP ROUND_DOWN match price steps', () => {
  close(M.roundStep(1.005, .01), 1.01);
  const a = new M.GridEngine(config({ priceStep: .1, takeProfitPct: .01 }), 100);
  const b = new M.GridEngine(config({ direction: 'short', priceStep: .1, takeProfitPct: .01, marginAmountPerOrder: 200 }), 100);
  close(a.takeProfitPrice(83.33), 84.2); close(b.takeProfitPrice(83.33), 82.4);
  close(a.takeProfitPrice(100), 101);
});
test('TP count and strict window membership retain older positions without opening duplicate slots', () => {
  const e = new M.GridEngine(config({ takeProfitPct: .2, takeProfitCount: 1, takeProfitGridCount: 3 }), 100);
  e.advance(candle([100, 90, 81]));
  const s = e.snapshot(); assert.equal(s.positions, 2); assert.equal(s.waitingTakeProfits, 1);
  const tps = s.orders.filter(p => p.kind === 'close'); assert.equal(tps.length, 1); close(tps[0].price, 97.2);
  assert.ok(s.orders.every(p => p.kind !== 'open' || !e.positions.has(p.price)));
  assert.ok(s.positions >= e.config.openGridCount);
});
test('out-of-window TP is cancelled but retained position re-arms and closes on recovery', () => {
  const e = new M.GridEngine(config({ takeProfitPct: .2, takeProfitCount: 1, takeProfitGridCount: 3, quantityStep: .001, minContracts: .001 }), 100);
  e.advance(candle([100, 50]));
  assert.ok(e.positions.has(90)); assert.equal(e.positions.get(90).tpOpen, false);
  assert.ok(e.positions.size > e.config.openGridCount);
  e.advance(candle([50, 105], TIME + 60000));
  assert.ok(e.positions.has(90)); assert.equal(e.positions.get(90).tpOpen, true);
  e.advance(candle([105, 108], TIME + 120000));
  assert.ok(!e.positions.has(90)); assert.ok(e.trades.some(t => t.kind === 'close' && t.price === 108));
});
test('insufficient funds stop new opens while existing positions and TPs remain maintained', () => {
  const e = new M.GridEngine(config({ capital: 90.09 }), 100);
  assert.equal(e.snapshot().orders.length, 1); close(e.snapshot().reserved, 90.09);
  e.advance(candle([100, 50]));
  assert.equal(e.positions.size, 1); assert.equal(e.tradeCount, 1); assert.equal(e.openOrders.size, 0);
  assert.ok(e.snapshot().available < 0);
  e.advance(candle([50, 99], TIME + 60000));
  assert.equal(e.cycles, 1); assert.ok(e.snapshot().orders.some(p => p.kind === 'open'));
});
test('OHLC path order affects results, while explicit pixel paths override it', () => {
  const row = { ...candle([100, 90, 100, 95]), path: undefined };
  const a = new M.GridEngine(config(), 100), b = new M.GridEngine(config(), 100);
  a.advance(row, 'low-first'); b.advance(row, 'high-first');
  assert.equal(a.cycles, 1); assert.equal(b.cycles, 0);
  const exact = new M.GridEngine(config(), 100); exact.advance(candle([100, 90, 99]), 'high-first');
  assert.equal(exact.cycles, 1);
});
test('equal-price ticks do not duplicate positions; rounded duplicate grid prices are skipped', () => {
  const e = new M.GridEngine(config(), 100); e.advance(candle([100, 90, 90, 90]));
  assert.equal(e.tradeCount, 1);
  const fine = new M.GridEngine(config({ gridStepPct: .0001, priceStep: .1 }), 100);
  assert.deepEqual(fine.snapshot().orders.map(p => p.price), [99.9, 99.8]);
});
test('old fixed-range configs migrate explicitly and preserve the original configuration', () => {
  const old = { id: 'old', name: 'old grid', enabled: false, lower: 30, upper: 300, amount: 100,
    grids: 50, capital: 6000, fee: .02, color: '#ffffff', quantityStep: 0 };
  const next = M.migrateStrategy(old);
  assert.deepEqual(next.legacyConfiguration, old); assert.equal(next.capital, 6000);
  close(next.marginAmountPerOrder * next.leverage, 100);
  assert.equal(next.enabled, false); assert.equal(next.fee, .02);
  M.strategy(next); assert.deepEqual(M.migrateStrategy(next), next);
});
test('reject invalid Gate parameters and contract specifications', () => {
  for (const override of [{ gridStepPct: 1 }, { takeProfitPct: 1 }, { openGridCount: 0 },
    { takeProfitCount: 301 }, { takeProfitGridCount: 1001 }, { multiplier: 0 }, { quantityStep: 0 },
    { referencePrice: -1 }, { fee: 10 }, { priceStep: 0 }, { minContracts: 0 }, { capital: '' }]) {
    assert.throws(() => M.strategy(config(override)));
  }
});
test('long and short replay conserve equity and never exceed one live cycle per rounded entry price', () => {
  for (const direction of ['long', 'short']) {
    const e = new M.GridEngine(config({ direction, gridStepPct: .02, takeProfitPct: .025,
      marginAmountPerOrder: 12, leverage: 3, quantityStep: .001, minContracts: .001 }), 100);
    const prices = Array.from({ length: 300 }, (_, i) => 100 + 18 * Math.sin(i / 8));
    for (const c of M.candlesFromPrices(prices, 5, TIME, 60)) {
      const s = e.advance(c); close(s.pnl, s.realized + s.unrealized, 1e-7);
      close(s.equity, s.cash + e.grossUnrealized(s.price));
      assert.ok(s.orders.filter(o => o.kind === 'open').length <= e.config.openGridCount);
      assert.ok(s.orders.filter(o => o.kind === 'close').length <= e.config.takeProfitCount);
      assert.ok(s.orders.every(o => o.kind === 'close' || !e.positions.has(o.price)));
      for (const p of e.positions.values()) if (p.tpOpen) {
        assert.ok(direction === 'long' ? s.price < p.tp && p.tp <= s.takeProfitBound : s.takeProfitBound <= p.tp && p.tp < s.price);
      }
    }
    assert.ok(e.cycles > 30);
    const byGrid = new Map();
    for (const trade of e.trades) {
      const count = (byGrid.get(trade.grid) || 0) + (trade.kind === 'open' ? 1 : -1);
      assert.ok(count >= 0 && count <= 1); byGrid.set(trade.grid, count);
    }
  }
});
test('prices, TP windows and contract sizes agree with the current repository Python functions', () => {
  const result = JSON.parse(execFileSync('python', [path.join(__dirname, 'verify-gate-parity.py')], { encoding: 'utf8' }));
  for (const row of result.cases) {
    const e = new M.GridEngine(config(row.config), row.current);
    const prices = e.desiredPrices(row.current).map(p => p.price);
    assert.deepEqual(prices, row.prices, JSON.stringify(row.config));
    close(e.takeProfitBound, row.bound);
    prices.forEach((price, i) => {
      close(e.takeProfitPrice(price), row.takeProfits[i]);
      close(e.plan({ price, grid: 0 }).contracts, row.contracts[i]);
    });
  }
  const d = M.defaultStrategy(), eth = result.eth;
  for (const [key, scriptKey] of Object.entries({ gridStepPct: 'grid_step_pct', takeProfitPct: 'take_profit_pct',
    marginAmountPerOrder: 'margin_amount_per_order', leverage: 'leverage', openGridCount: 'buy_grid_count',
    takeProfitCount: 'take_profit_count', takeProfitGridCount: 'take_profit_grid_count' })) close(d[key], Number(eth[scriptKey]));
});
