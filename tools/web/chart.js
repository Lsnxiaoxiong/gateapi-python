// Lightweight candlestick chart renderer on <canvas>.
// No external chart library — pure 2D canvas drawing.
// Draws high-DPI aware OHLC candles with price axis + time axis.

const PADDING = { top: 10, right: 56, bottom: 22, left: 8 };

export class CandleChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.data = [];
    this.opts = {};
    this._resizeObserver = new ResizeObserver(() => this._resizeAndDraw());
    this._resizeObserver.observe(canvas.parentElement || canvas);
    // Initial sizing
    this._resizeAndDraw();
  }

  setData(candles, opts = {}) {
    this.data = candles;
    this.opts = opts;
    this._resizeAndDraw();
  }

  destroy() {
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
  }

  _resizeAndDraw() {
    const parent = this.canvas.parentElement;
    if (!parent) return;
    const w = parent.clientWidth;
    const h = parent.clientHeight;
    if (w <= 0 || h <= 0) return;
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.floor(w * dpr);
    this.canvas.height = Math.floor(h * dpr);
    this.canvas.style.width = w + 'px';
    this.canvas.style.height = h + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.W = w;
    this.H = h;
    this._draw();
  }

  _draw() {
    const ctx = this.ctx;
    const { W, H } = this;
    if (!W || !H) return;
    ctx.clearRect(0, 0, W, H);

    if (!this.data.length) {
      ctx.fillStyle = '#8b95a7';
      ctx.font = '12px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('无数据', W / 2, H / 2);
      return;
    }

    const plotW = W - PADDING.left - PADDING.right;
    const plotH = H - PADDING.top - PADDING.bottom;
    if (plotW <= 0 || plotH <= 0) return;

    // Price range
    let hi = -Infinity, lo = Infinity, maxVol = 0;
    for (const c of this.data) {
      hi = Math.max(hi, parseFloat(c.high));
      lo = Math.min(lo, parseFloat(c.low));
      const v = parseFloat(c.volume);
      if (!isNaN(v)) maxVol = Math.max(maxVol, v);
    }
    if (!isFinite(hi) || !isFinite(lo) || hi === lo) { hi = (hi || 1) + 1; lo = (lo || 0) - 1; }
    const pad = (hi - lo) * 0.08;
    hi += pad; lo -= pad;
    const range = hi - lo;

    const n = this.data.length;
    // Determine how many candles are visible. We render all but if too many,
    // thin the bars to 1px lines.
    const slot = plotW / n;
    const bodyW = Math.max(1, Math.min(slot * 0.7, 14));

    // Background grid lines (5 horizontal)
    ctx.strokeStyle = '#2d3548';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#8b95a7';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    const gridLines = 5;
    for (let i = 0; i <= gridLines; i++) {
      const y = PADDING.top + (plotH * i) / gridLines;
      const price = hi - (range * i) / gridLines;
      ctx.beginPath();
      ctx.moveTo(PADDING.left, y);
      ctx.lineTo(PADDING.left + plotW, y);
      ctx.stroke();
      ctx.fillText(this._fmtPrice(price), PADDING.left + plotW + 4, y);
    }

    // Candles
    const up = '#26a69a', down = '#ef5350';
    for (let i = 0; i < n; i++) {
      const c = this.data[i];
      const o = parseFloat(c.open), cl = parseFloat(c.close);
      const h = parseFloat(c.high), l = parseFloat(c.low);
      if ([o, cl, h, l].some(v => isNaN(v))) continue;
      const x = PADDING.left + slot * (i + 0.5);
      const yO = PADDING.top + ((hi - o) / range) * plotH;
      const yC = PADDING.top + ((hi - cl) / range) * plotH;
      const yH = PADDING.top + ((hi - h) / range) * plotH;
      const yL = PADDING.top + ((hi - l) / range) * plotH;
      const bullish = cl >= o;
      ctx.strokeStyle = bullish ? up : down;
      ctx.fillStyle = bullish ? up : down;

      // Wick
      ctx.beginPath();
      ctx.moveTo(x, yH);
      ctx.lineTo(x, yL);
      ctx.stroke();

      // Body
      const bodyTop = Math.min(yO, yC);
      const bodyH = Math.max(1, Math.abs(yC - yO));
      if (slot < 2) {
        // Too narrow: just a vertical line (already drawn as wick covers it)
      } else {
        ctx.fillRect(x - bodyW / 2, bodyTop, bodyW, bodyH);
      }
    }

    // Time axis labels (show ~5 evenly spaced)
    ctx.fillStyle = '#8b95a7';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const labels = Math.min(6, n);
    for (let i = 0; i < labels; i++) {
      const idx = Math.floor((n - 1) * i / Math.max(1, labels - 1));
      const c = this.data[idx];
      const x = PADDING.left + slot * (idx + 0.5);
      const d = new Date(c.timestamp * 1000);
      const lbl = this._fmtTime(d);
      ctx.fillText(lbl, x, PADDING.top + plotH + 6);
    }
  }

  _fmtPrice(p) {
    if (p >= 1000) return p.toFixed(0);
    if (p >= 1) return p.toFixed(2);
    if (p >= 0.01) return p.toFixed(4);
    return p.toExponential(2);
  }

  _fmtTime(d) {
    const iv = this.opts.interval || '1h';
    const pad = (x) => String(x).padStart(2, '0');
    if (iv.endsWith('d') || iv === '7d' || iv === '30d' || iv === '1d' || iv === '2d' || iv === '3d') {
      return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
    }
    return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:00`;
  }
}
