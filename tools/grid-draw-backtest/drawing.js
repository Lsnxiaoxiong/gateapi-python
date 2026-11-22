/* Sparse world-space ink. Moving the viewport never moves or rescales sampled pixels. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GridDrawing = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const TILE = 256;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  function edgeVelocity(position, length, margin = 42, speed = 240) {
    // Work in displayed pixels, so auto-pan feels the same on different screens.
    const zone = Math.min(margin, length / 4);
    if (position < zone) return -speed * clamp((zone - position) / zone, 0, 1);
    if (position > length - zone) return speed * clamp((position - length + zone) / zone, 0, 1);
    return 0;
  }
  function worldPoint(client, rect, view, width, height) {
    return { x: view.x + clamp((client.x - rect.left) / rect.width, 0, 1) * width,
      y: view.y + clamp((client.y - rect.top) / rect.height, 0, 1) * height };
  }
  class InkWorld {
    constructor(createCanvas) {
      this.createCanvas = createCanvas; this.clear();
    }
    clear() { this.tiles = new Map(); this.columns = new Map(); this.dirty = new Set(); this.bounds = null; this.stamp = null; }
    line(stroke, from, to) {
      const start = from || to, pad = stroke.width / 2 + 1;
      const minX = Math.floor(Math.min(start.x, to.x) - pad), maxX = Math.ceil(Math.max(start.x, to.x) + pad);
      const minY = Math.floor(Math.min(start.y, to.y) - pad), maxY = Math.ceil(Math.max(start.y, to.y) + pad);
      const b = this.bounds;
      this.bounds = b ? { minX: Math.min(b.minX, minX), maxX: Math.max(b.maxX, maxX),
        minY: Math.min(b.minY, minY), maxY: Math.max(b.maxY, maxY) } : { minX, maxX, minY, maxY };
      // Rasterize each complete segment once before splitting into tiles. Letting
      // the browser clip a sloping stroke separately per tile changes alpha>0
      // edge pixels, which would change the column's arithmetic mean at seams.
      const width = maxX - minX + 1, height = maxY - minY + 1;
      if (!this.stamp) this.stamp = this.createCanvas();
      if (this.stamp.width !== width || this.stamp.height !== height) {
        this.stamp.width = width;
        this.stamp.height = height;
      }
      const c = this.stamp.getContext('2d', { willReadFrequently: true });
      c.clearRect(0, 0, width, height);
      c.strokeStyle = c.fillStyle = '#54dcc0'; c.lineWidth = stroke.width; c.lineCap = c.lineJoin = 'round';
      c.beginPath();
      if (from) { c.moveTo(from.x - minX, from.y - minY); c.lineTo(to.x - minX, to.y - minY); c.stroke(); }
      else { c.arc(to.x - minX, to.y - minY, stroke.width / 2, 0, Math.PI * 2); c.fill(); }
      for (let tx = Math.floor(minX / TILE); tx <= Math.floor(maxX / TILE); tx++) {
        for (let ty = Math.floor(minY / TILE); ty <= Math.floor(maxY / TILE); ty++) {
          const key = `${tx},${ty}`;
          let tile = this.tiles.get(key);
          if (!tile) {
            const canvas = this.createCanvas(); canvas.width = TILE; canvas.height = TILE;
            tile = { x: tx * TILE, y: ty * TILE, canvas, ctx: canvas.getContext('2d', { willReadFrequently: true }),
              sums: new Float64Array(TILE), counts: new Uint16Array(TILE) };
            this.tiles.set(key, tile);
          }
          tile.ctx.drawImage(this.stamp, 0, 0, width, height, minX - tile.x, minY - tile.y, width, height);
          this.dirty.add(tile);
        }
      }
    }
    flush() {
      for (const tile of this.dirty) {
        const rgba = tile.ctx.getImageData(0, 0, TILE, TILE).data;
        for (let x = 0; x < TILE; x++) {
          let sum = 0, count = 0;
          for (let y = 0; y < TILE; y++) {
            if (rgba[(y * TILE + x) * 4 + 3]) { sum += tile.y + y; count++; }
          }
          const worldX = tile.x + x, old = this.columns.get(worldX);
          const next = { sum: (old?.sum || 0) + sum - tile.sums[x], count: (old?.count || 0) + count - tile.counts[x] };
          if (next.count) this.columns.set(worldX, next); else this.columns.delete(worldX);
          tile.sums[x] = sum; tile.counts[x] = count;
        }
      }
      this.dirty.clear();
    }
    sample() {
      this.flush();
      let left = 0, right = -Infinity;
      for (const x of this.columns.keys()) { left = Math.min(left, x); right = Math.max(right, x); }
      if (right === -Infinity) return { columns: [], left: 0 };
      if (right - left + 1 > 200000) throw new Error('当前走势超过 200,000 列像素，请分段保存绘制');
      const columns = Array(right - left + 1).fill(null);
      for (const [x, value] of this.columns) columns[x - left] = value.sum / value.count;
      return { columns, left };
    }
    paint(ctx, view, width, height) {
      for (const tile of this.tiles.values()) {
        if (tile.x + TILE < view.x || tile.x > view.x + width || tile.y + TILE < view.y || tile.y > view.y + height) continue;
        ctx.drawImage(tile.canvas, tile.x - view.x, tile.y - view.y);
      }
    }
  }
  return { InkWorld, edgeVelocity, worldPoint };
});
