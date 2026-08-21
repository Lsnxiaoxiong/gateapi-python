# coding: utf-8
"""Top-100 stock/ETF trading pairs by 15m kline amplitude (Gate.io).

Data sources:
- CFDApi /tradfi/symbols (category_id=2: stocks & ETFs) -> 合约/CFD trading pairs, have 15m klines.
- StockApi /stock/symbols -> real stock spot trading pairs (no kline API), used for spot availability flag.

Metric (比较值): for each trading pair, take the last 96 15m candles (24h), slide a window of 10
adjacent 15m klines, compute each window's average amplitude (per-candle (high-low)/prev_close*100),
and use the MINIMUM of those window averages as the pair's comparison value. Rank descending.

Uses ONE persistent HTTPS connection (keep-alive) to avoid per-request TLS handshake latency.
Runs at ~4 qps (under Gate's 5 qps limit). Progress printed periodically; results saved to CSV+JSON.
"""
import csv
import json
import os
import time
import http.client

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)

HOST = "api.gateio.ws"
BASE_PATH = "/api/v4"
KLINE_LIMIT = 96          # 24h of 15m candles
REQ_INTERVAL = 0.25       # ~4 qps, safe under 5 qps limit
OUT_CSV = "scripts/top100_stock_etf_15m_amplitude.csv"
OUT_JSON = "scripts/top100_stock_etf_15m_amplitude.json"
ALL_JSON = "scripts/all_stock_etf_15m_amplitude.json"


def make_conn():
    return http.client.HTTPSConnection(HOST, timeout=60)


def get(conn, path, retries=5):
    """GET with retry; rebuilds connection if it dies. Returns parsed JSON or None."""
    last = None
    for i in range(retries):
        try:
            conn.request("GET", BASE_PATH + path, headers={"Accept": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 200:
                return json.loads(body.decode("utf-8"))
            # 4xx/5xx: no point retrying the same conn necessarily
            last = f"HTTP {resp.status}: {body.decode('utf-8', 'ignore')[:200]}"
            if resp.status in (400, 404):
                return None  # permanent error
            time.sleep(1)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            # reconnect on failure
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(1.5 * (i + 1))
    # final fallback: fresh connection attempt
    try:
        c2 = make_conn()
        c2.request("GET", BASE_PATH + path, headers={"Accept": "application/json"})
        r2 = c2.getresponse()
        b2 = r2.read()
        c2.close()
        if r2.status == 200:
            return json.loads(b2.decode("utf-8"))
    except Exception as exc:
        last = f"{last}; fallback: {exc}"
    print(f"  [warn] {path}: {last}")
    return None


def load_json(name):
    with open(name, "r", encoding="utf-8") as f:
        return json.load(f)


def kline_amplitude_metrics(klines):
    """klines: list of dicts with keys o/c/h/l/t (ascending by time). Returns metrics dict or None.

    Comparison metric (per updated goal): minimum of the average amplitude of every 10-adjacent
    15m kline sliding window. Per-candle amplitude = (high - low) / prev_close * 100.
    Also keeps max per-candle amplitude and 24h range for reference.
    """
    if not klines or len(klines) < 2:
        return None
    # per-candle amplitude aligned with candle index j>=1 (amp[j] uses close of candle j-1)
    amps = []
    prev_close = None
    for k in klines:
        try:
            o, c, h, l = float(k["o"]), float(k["c"]), float(k["h"]), float(k["l"])
        except (KeyError, TypeError, ValueError):
            continue
        if prev_close and prev_close > 0 and h >= l > 0:
            amps.append((h - l) / prev_close * 100.0)
        prev_close = c
    if len(amps) < 10:
        return None

    # sliding windows of 10 adjacent candles -> average amplitude -> take the MINIMUM
    window_avgs = []
    for start in range(0, len(amps) - 9):
        window_avgs.append(sum(amps[start:start + 10]) / 10.0)
    cmp_value = min(window_avgs)

    hi = max(float(k["h"]) for k in klines if float(k["h"]) > 0)
    lo = min(float(k["l"]) for k in klines if float(k["l"]) > 0)
    first_open = None
    for k in klines:
        try:
            v = float(k["o"])
        except (KeyError, TypeError, ValueError):
            continue
        if v > 0:
            first_open = v
            break
    range24h = ((hi - lo) / first_open * 100.0) if first_open else None
    return {
        "cmp_amp_avg10min_pct": cmp_value,          # 主排序指标：10根相邻K线平均振幅的最小值
        "amp_avg_pct": sum(amps) / len(amps),       # 参考：全部K线平均振幅
        "amp_max_pct": max(amps),                   # 参考：单根最大振幅
        "amp_n": len(amps),
        "amp_24h_range_pct": range24h,              # 参考：24h 区间振幅
        "last_t": klines[-1].get("t"),
    }


def main():
    tradfi = load_json("scripts/tradfi_symbols.json")
    stocks = load_json("scripts/stock_symbols.json")
    stock_map = {s["symbol"]: s for s in stocks}

    targets = [s for s in tradfi if s.get("category_id") == 2]
    print(f"tradFi stock/ETF symbols to fetch: {len(targets)}")

    conn = make_conn()
    results = []
    failed = []
    start = time.time()
    try:
        for idx, item in enumerate(targets, 1):
            sym = item["symbol"]
            data = get(conn, f"/tradfi/symbols/{sym}/klines?kline_type=15m&limit={KLINE_LIMIT}")
            metrics = None
            if data and data.get("data", {}).get("list"):
                metrics = kline_amplitude_metrics(data["data"]["list"])
            if metrics is None:
                failed.append({"symbol": sym, "desc": item.get("symbol_desc")})
            else:
                results.append({
                    "symbol": sym,
                    "symbol_desc": item.get("symbol_desc"),
                    "category_id": item.get("category_id"),
                    "settlement_currency": item.get("settlement_currency"),
                    "trade_mode": item.get("trade_mode"),
                    "status": item.get("status"),
                    "cmp_amp_avg10min_pct": round(metrics["cmp_amp_avg10min_pct"], 4),
                    "amp_avg_pct": round(metrics["amp_avg_pct"], 4),
                    "amp_max_pct": round(metrics["amp_max_pct"], 4),
                    "amp_24h_range_pct": round(metrics["amp_24h_range_pct"], 4) if metrics["amp_24h_range_pct"] is not None else None,
                    "amp_n_candles": metrics["amp_n"],
                    "last_kline_t": metrics["last_t"],
                    "spot_available": sym in stock_map,
                    "spot_exchange": stock_map[sym].get("exchange") if sym in stock_map else None,
                    "klines": data["data"]["list"] if data and data.get("data", {}).get("list") else [],
                })
            if idx % 25 == 0 or idx == len(targets):
                elapsed = time.time() - start
                rate = idx / elapsed if elapsed > 0 else 0
                print(f"[{idx}/{len(targets)}] ok={len(results)} fail={len(failed)} rate={rate:.2f} qps elapsed={elapsed:.0f}s", flush=True)
            time.sleep(REQ_INTERVAL)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Sort by the comparison metric: minimum of 10-adjacent 15m kline average amplitude, descending
    results.sort(key=lambda r: (r["cmp_amp_avg10min_pct"] if r["cmp_amp_avg10min_pct"] is not None else -1), reverse=True)

    with open(ALL_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    with open("scripts/failed_symbols.json", "w", encoding="utf-8") as f:
        json.dump(failed, f, ensure_ascii=False, indent=1)

    top100 = results[:100]
    header = ["rank", "symbol", "symbol_desc", "cmp_amp_avg10min_pct", "amp_avg_pct", "amp_max_pct",
              "amp_24h_range_pct", "amp_n_candles", "spot_available", "spot_exchange", "trade_mode", "status"]
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for i, r in enumerate(top100, 1):
            w.writerow([i, r["symbol"], r["symbol_desc"], r["cmp_amp_avg10min_pct"], r["amp_avg_pct"],
                        r["amp_max_pct"], r["amp_24h_range_pct"], r["amp_n_candles"], r["spot_available"],
                        r["spot_exchange"], r["trade_mode"], r["status"]])
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(top100, f, ensure_ascii=False, indent=1)

    print(f"\nDONE: ok={len(results)} failed={len(failed)}")
    print(f"top100 written to {OUT_CSV} / {OUT_JSON}")
    print("\n=== TOP 20 preview (sorted by min-of-10candle-avg amplitude) ===")
    for i, r in enumerate(top100[:20], 1):
        print(f"{i:3d} {r['symbol']:10s} cmp={r['cmp_amp_avg10min_pct']:.4f}% avg={r['amp_avg_pct']:.4f}% "
              f"max={r['amp_max_pct']:.4f}% 24h={r['amp_24h_range_pct']:.3f}% spot={r['spot_available']} "
              f"{r['symbol_desc'] or ''}", flush=True)
    if failed:
        print(f"\nFailed symbols ({len(failed)}): {[f['symbol'] for f in failed]}")


if __name__ == "__main__":
    main()
