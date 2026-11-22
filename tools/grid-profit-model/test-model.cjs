const test = require('node:test');
const assert = require('node:assert/strict');
const model = require('./model.js');
function close(actual, expected, tolerance = 1e-8) { assert.ok(Math.abs(actual - expected) < tolerance, `${actual} != ${expected}`); }

test('20 completed rounds a day produce the expected net return and two-sided fees', () => {
  const r = model.project();
  close(r.unitNet, 0.9988); close(r.dailyNet, 19.976); close(r.totalNet, 599.28);
  close(r.totalFees, 120.72); close(r.totalGross, 720);
  assert.equal(r.totalRounds, 600); assert.equal(r.totalOrders, 1200);
});
test('single-sided fill count divides by two; 40 orders match 20 full rounds', () => {
  const rounds = model.project({ dailyTrades: 20 }), orders = model.project({ dailyTrades: 40, countMode: 'orders' });
  close(rounds.totalNet, orders.totalNet); close(rounds.totalFees, orders.totalFees);
  close(model.project({ dailyTrades: 20, countMode: 'orders' }).totalNet, rounds.totalNet / 2);
});
test('zero trades have zero gross profit, net profit and fees', () => {
  const r = model.project({ dailyTrades: 0 });
  close(r.totalNet, 0); close(r.totalFees, 0); close(r.totalGross, 0);
  assert.ok(r.points.every(point => point.net === 0 && point.fees === 0));
});
test('fractional daily averages and odd single-sided counts are estimates, not truncated', () => {
  const r = model.project({ dailyTrades: 3.5, days: 2 });
  close(r.totalRounds, 7); close(r.totalNet, 7 * r.unitNet);
  close(model.project({ dailyTrades: 3, countMode: 'orders', days: 1 }).totalRounds, 1.5);
});
test('spacing changes only grid prices when daily throughput and TP remain fixed', () => {
  const a = model.project({ spacing: 1 }), b = model.project({ spacing: 2 });
  close(a.totalNet, b.totalNet); assert.notEqual(a.levels[1].buy, b.levels[1].buy);
  close(a.levels[0].buy, 3000); close(a.levels[0].sell, 3036);
  close(a.levels[1].buy, 2970);
});
test('unit profit, gross profit and both-side fees reconcile on every day', () => {
  const r = model.project({ takeProfit: 2.5, fee: 0.2, notional: 250, dailyTrades: 15.5 });
  for (const point of r.points) close(point.net, point.gross - point.fees, 1e-7);
  close(r.points.at(-1).net, r.totalNet); close(r.points.at(-1).rounds, r.totalRounds);
});
test('fee breakeven and negative per-round profits are preserved', () => {
  const loss = model.project({ takeProfit: 0.1 });
  close(loss.unitNet, -0.1001); assert.ok(loss.totalNet < 0);
  close(model.project({ takeProfit: loss.breakEvenPct }).unitNet, 0);
});
test('legacy capital is removed and cannot cap the new position scenario', () => {
  const r = model.project({ capital: 1 });
  assert.ok(!('capital' in r.parameters)); assert.ok(!('capital' in model.defaults));
  assert.ok(!('returnPct' in r)); assert.ok(!('fundableGrids' in r));
  close(r.totalNet, model.project().totalNet); close(r.scenario.maxNotional, model.project().scenario.maxNotional);
});
test('old saved price parameters migrate without treating volatility as trade count', () => {
  const p = model.validate({ volatility: 5, paths: 80, seed: 123, spacing: 2 });
  assert.equal(p.dailyTrades, 20); assert.equal(p.spacing, 2); assert.ok(!('volatility' in p));
  assert.ok(!('paths' in p)); assert.ok(!('seed' in p));
});
test('invalid input is rejected', () => {
  for (const changes of [{ dailyTrades: -1 }, { dailyTrades: NaN }, { dailyTrades: Infinity },
    { countMode: 'unknown' }, { spacing: 0 }, { days: 1.5 },
    { dropPercent: -1 }, { dropPercent: 100 }, { dropPercent: 101 }, { minQuantity: 0 }]) {
    assert.throws(() => model.project(changes));
  }
});

test('zero decline includes the starting grid, rounded to minimum quantity', () => {
  const r = model.project({ dropPercent: 0 });
  assert.equal(r.levels[0].grid, 0); assert.equal(r.scenario.touchedGrids, 1);
  assert.equal(r.scenario.maxOpenGrids, 1); close(r.scenario.maxNotional, 99);
  close(r.scenario.totalQuantity, 0.033); close(r.scenario.entryFees, 0.099);
  close(r.scenario.requiredFunds, 99.099);
});
test('geometric decline counts include equality but exclude the next untouched grid', () => {
  const drop = (1 - 0.99 ** 4) * 100;
  const exact = model.project({ dropPercent: drop });
  const below = model.project({ dropPercent: drop - 1e-8 });
  assert.equal(exact.scenario.touchedGrids, 5);
  assert.equal(below.scenario.touchedGrids, 4);
  assert.equal(model.project({ dropPercent: 10 }).scenario.touchedGrids, 11);
});
test('32 percent drop dynamically generates 39 grids without the legacy 30-grid cap', () => {
  const r = model.project({ dropPercent: 32, spacing: 1, notional: 300, grids: 30 });
  assert.equal(r.gridCount, 39); assert.equal(r.levels.length, 39);
  close(r.scenario.budgetNotional, 11700);
  assert.ok(!('grids' in r.parameters)); assert.ok(!('grids' in model.defaults));
  close(r.scenario.maxNotional, r.totalGridNotional);
  assert.ok(r.levels.at(-1).dropPercent <= 32);
  assert.ok((1 - 0.99 ** r.gridCount) * 100 > 32);
});
test('unorderable lots are excluded, while lower-priced valid grids still buy', () => {
  const r = model.project({ initialPrice: 3000, notional: 1, minQuantity: 0.001, spacing: 30, dropPercent: 76 });
  assert.equal(r.scenario.touchedGrids, 5); assert.equal(r.scenario.skippedGrids, 4);
  assert.equal(r.scenario.maxOpenGrids, 1); close(r.scenario.maxNotional, 0.7203);
  close(r.scenario.totalQuantity, 0.001);
});
test('the supplied 1000-grid Python example is reproduced', () => {
  const r = model.project({ initialPrice: 2600, spacing: 0.05, minQuantity: 0.001, notional: 40, dropPercent: 39.33 });
  let expected = 0;
  for (let i = 0; i < 1000; i++) {
    const price = 2600 * 0.9995 ** i, lotValue = 0.001 * price;
    expected += Math.floor(40 / lotValue) * lotValue;
    assert.equal(r.levels[i].grid, i); close(r.levels[i].buy, price);
    assert.ok(r.levels[i].actualNotional <= 40 + 1e-9);
    close(r.levels[i].cumulativeNotional, expected, 1e-7);
    close(r.levels[i].quantity * price, r.levels[i].actualNotional);
  }
  assert.equal(r.scenario.maxOpenGrids, 1000);
  close(r.scenario.maxNotional, expected, 1e-7);
  close(r.scenario.requiredFunds, expected * 1.001, 1e-7);
});
test('fees affect required spend but not rounded quantities or position notional', () => {
  const a = model.project({ fee: 0 }), b = model.project({ fee: 0.5 });
  close(a.scenario.maxNotional, b.scenario.maxNotional);
  close(a.scenario.totalQuantity, b.scenario.totalQuantity);
  close(b.scenario.requiredFunds, b.scenario.maxNotional * 1.005);
});
test('quantity floor corrects binary rounding only at an exact step boundary', () => {
  const r = model.project({ initialPrice: 100, notional: 30, minQuantity: 0.1, dropPercent: 0 });
  close(r.levels[0].quantity, 0.3); close(r.scenario.maxNotional, 30);
  const lower = model.project({ initialPrice: 100, notional: 29.9999, minQuantity: 0.1, dropPercent: 0 });
  close(lower.levels[0].quantity, 0.2); close(lower.scenario.maxNotional, 20);
});

test('maximum budget depends on spacing, per-grid budget and drop only', () => {
  const base = { spacing: 1, notional: 300, dropPercent: 32 };
  const a = model.project(base), b = model.project({ ...base, initialPrice: 100, minQuantity: 10, takeProfit: 5, fee: 0.5, dailyTrades: 0 });
  assert.equal(a.gridCount, b.gridCount);
  close(a.scenario.budgetNotional, b.scenario.budgetNotional);
  close(model.project({ ...base, notional: 600 }).scenario.budgetNotional, 2 * a.scenario.budgetNotional);
  assert.ok(model.project({ ...base, spacing: 0.5 }).gridCount > a.gridCount);
  assert.ok(model.project({ ...base, dropPercent: 50 }).gridCount > a.gridCount);
  assert.notEqual(a.scenario.maxNotional, b.scenario.maxNotional);
});

test('automatically generated grids extend beyond the former 1000-grid limit', () => {
  const r = model.project({ spacing: 0.01, dropPercent: 32, notional: 1 });
  assert.ok(r.gridCount > 1000); assert.equal(r.levels.length, r.gridCount);
  close(r.scenario.budgetNotional, r.gridCount);
});
