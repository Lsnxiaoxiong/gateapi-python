# coding: utf-8
"""Summary stats for top100 results (new comparison metric)."""
import json

d = json.load(open("scripts/top100_stock_etf_15m_amplitude.json", encoding="utf-8"))
all_d = json.load(open("scripts/all_stock_etf_15m_amplitude.json", encoding="utf-8"))

print("=== Summary ===")
print(f"total ranked (合约/tradFi stock&ETF): {len(all_d)}")
print(f"top100 shown: {len(d)}")
spot = sum(1 for r in d if r["spot_available"])
print(f"top100 with spot available: {spot}, contract-only: {len(d)-spot}")
etf = sum(1 for r in d if "ETF" in (r.get("symbol_desc") or "").upper())
print(f"top100 ETF-desc entries: {etf}")
print(f"top100 cmp value range: {d[-1]['cmp_amp_avg10min_pct']:.4f}% .. {d[0]['cmp_amp_avg10min_pct']:.4f}%")

print("\n=== Consistency checks ===")
bad = [r for r in d if r["cmp_amp_avg10min_pct"] is None or r["cmp_amp_avg10min_pct"] <= 0]
print(f"entries with invalid cmp amplitude: {len(bad)}")
sorted_ok = all(d[i]["cmp_amp_avg10min_pct"] >= d[i+1]["cmp_amp_avg10min_pct"] for i in range(len(d)-1))
print(f"descending order verified: {sorted_ok}")
# cmp should be <= avg (min of windows <= mean of all candles approximately)
below = [r for r in d if r["cmp_amp_avg10min_pct"] > r["amp_avg_pct"] + 1e-9]
print(f"entries where cmp > overall avg (should be 0): {len(below)}")
# users' known symbols
for sym in ["SOXL", "SKHYNIX", "NVDA", "TSLA", "QQQ", "TQQQ", "SOXX", "GLD"]:
    hit = next((r for r in all_d if r["symbol"] == sym), None)
    if hit:
        rank = next((i+1 for i, r in enumerate(d) if r["symbol"] == sym), None)
        print(f"{sym}: cmp={hit['cmp_amp_avg10min_pct']:.4f}% spot={hit['spot_available']} top100_rank={rank}")
