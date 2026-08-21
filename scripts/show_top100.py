# coding: utf-8
"""Display top 100 stock/ETF pairs by the comparison metric (min of 10-candle avg amplitude)."""
import json

d = json.load(open("scripts/top100_stock_etf_15m_amplitude.json", encoding="utf-8"))
print(f"top100 entries: {len(d)}")
print()
hdr = f"{'#':>3} {'symbol':<10} {'desc':<36} {'cmp%':>9} {'avg%':>9} {'max%':>9} {'spot':>5}"
print(hdr)
print("-" * len(hdr))
for i, r in enumerate(d, 1):
    desc = (r.get("symbol_desc") or "")[:34]
    spot = "Y" if r.get("spot_available") else "N"
    print(f"{i:>3} {r['symbol']:<10} {desc:<36} {r['cmp_amp_avg10min_pct']:>9.4f} {r['amp_avg_pct']:>9.4f} {r['amp_max_pct']:>9.4f} {spot:>5}")
