const { test } = require('node:test');
const assert = require('node:assert/strict');
const { edgeVelocity, worldPoint } = require('./drawing.js');

test('follow all four edges and corners, with no movement in the center', () => {
  assert.equal(edgeVelocity(400, 800), 0);
  assert.ok(edgeVelocity(4, 800) < 0);
  assert.ok(edgeVelocity(796, 800) > 0);
  assert.ok(edgeVelocity(4, 250) < 0);
  assert.ok(edgeVelocity(246, 250) > 0);
  assert.equal(edgeVelocity(-100, 800), -240);
  assert.equal(edgeVelocity(900, 800), 240);
  assert.ok(Math.abs(edgeVelocity(2, 800)) > Math.abs(edgeVelocity(30, 800)));
  assert.equal(edgeVelocity(42, 800), 0);
});
test('world-space pointer extends beyond the original canvas without clamping coordinates', () => {
  const rect = { left: 50, top: 100, width: 600, height: 200 };
  const p = { x: 350, y: 200 };
  assert.deepEqual(worldPoint(p, rect, { x: 0, y: 0 }, 1200, 400), { x: 600, y: 200 });
  assert.deepEqual(worldPoint(p, rect, { x: 1400, y: -700 }, 1200, 400), { x: 2000, y: -500 });
  assert.deepEqual(worldPoint({ x: -100, y: 900 }, rect, { x: -800, y: 500 }, 1200, 400), { x: -800, y: 900 });
});
test('resizing the displayed viewport preserves logical pointer and sample scale', () => {
  const view = { x: -700, y: 450 };
  const a = worldPoint({ x: 300, y: 100 }, { left: 0, top: 0, width: 600, height: 200 }, view, 1200, 400);
  const b = worldPoint({ x: 600, y: 200 }, { left: 0, top: 0, width: 1200, height: 400 }, view, 1200, 400);
  assert.deepEqual(a, b);
});
