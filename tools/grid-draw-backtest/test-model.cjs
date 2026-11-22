const { test } = require('node:test');
const assert = require('node:assert/strict');
const M = require('./model.js');
const TIME = Date.UTC(2026, 10, 14);
const candle = path => ({ time: TIME, open: path[0], high: Math.max(...path),
  low: Math.min(...path), close: path.at(-1), volume: 0, path });

test('pixel averages count painted pixels, including antialiased pixels, and ignore background', () => {
  const rgba = new Uint8ClampedArray(3 * 5 * 4);
  rgba[(1 * 3 + 0) * 4 + 3] = 255;
  rgba[(3 * 3 + 0) * 4 + 3] = 1;
  rgba[(4 * 3 + 2) * 4 + 3] = 255;
  assert.deepEqual(M.columnMeans(rgba, 3, 5), [2, null, 4]);
});
test('price anchor uses first column; interpolate gaps, extend left, trim right', () => {
  const result = M.pricesFromColumns([null, 20, null, 10, null], 100, .5);
  assert.deepEqual(result.ys, [20, 20, 15, 10]);
  assert.deepEqual(result.prices, [100, 100, 102.5, 105]);
  assert.throws(() => M.pricesFromColumns([100, 300], 10, 1), /大于 0/);
  assert.throws(() => M.pricesFromColumns([null, null], 10, 1), /先在画布/);
});
test('OHLC grouping preserves exact path, bridges prior close, and retains final partial group', () => {
  const rows = M.candlesFromPrices([100, 110, 90, 105, 98], 2, TIME, 60);
  assert.equal(rows.length, 3);
  assert.deepEqual(rows[1].path, [110, 90, 105]);
  assert.deepEqual([rows[1].open, rows[1].high, rows[1].low, rows[1].close], [110, 110, 90, 105]);
  assert.equal(rows[2].time, TIME + 120000);
  assert.deepEqual(rows[2].path, [105, 98]);
});
test('CSV and JSON round trip; seconds, milliseconds, ISO and Binance headerless fields', () => {
  const rows = M.candlesFromPrices([100, 102, 99], 2, TIME, 60);
  assert.deepEqual(M.validateCandles(JSON.parse(JSON.stringify(rows))), rows);
  assert.deepEqual(M.parseCSV(M.toCSV(rows)), rows.map(({ path, ...c }) => c));
  for (const t of [String(TIME / 1000), String(TIME), new Date(TIME).toISOString()]) {
    assert.equal(M.parseCSV(`time,open,high,low,close\n${t},100,101,99,100`)[0].time, TIME);
  }
  assert.equal(M.parseCSV(`${TIME},100,101,99,100,50,0,0,0,0,0,0`)[0].volume, 50);
  assert.throws(() => M.parseCSV(`time,open,high,low,close\n${TIME},100,90,80,100`), /不一致/);
  assert.throws(() => M.validateCandles([candle([100, 110]), candle([110, 100])]), /严格递增/);
  assert.throws(() => M.validateCandles([{ ...candle([100, 110]), path: [100, 90, 110] }]), /路径与 OHLC/);
});
