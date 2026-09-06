from __future__ import annotations

import concurrent.futures
import datetime as dt
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://data.binance.vision/data/futures/um"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
START_YEAR = 2020
START_MONTH = 1
WORKERS = 6

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "data" / "binance" / "futures_um" / SYMBOL / INTERVAL
MONTHLY_DIR = OUT_ROOT / "monthly"
DAILY_DIR = OUT_ROOT / "daily"


def month_iter(start_year: int, start_month: int, end_year: int, end_month: int):
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        yield y, m
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1


def download(url: str, dest: Path) -> tuple[str, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return "skip", dest.name

    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            total = resp.headers.get("Content-Length")
            copied = 0
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                copied += len(chunk)
            if total is not None and copied != int(total):
                raise IOError(f"size mismatch: expected {total}, got {copied}")
        os.replace(tmp, dest)
        return "ok", dest.name
    except urllib.error.HTTPError as e:
        if e.code == 404:
            if tmp.exists():
                tmp.unlink()
            return "404", dest.name
        if tmp.exists():
            tmp.unlink()
        return "error", f"{dest.name}: HTTP {e.code}"
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        return "error", f"{dest.name}: {e}"


def monthly_jobs(today: dt.date):
    # 只下载完整月份；当前月用 daily 数据补齐到昨天。
    first_of_this_month = today.replace(day=1)
    prev_month_last_day = first_of_this_month - dt.timedelta(days=1)
    for y, m in month_iter(START_YEAR, START_MONTH, prev_month_last_day.year, prev_month_last_day.month):
        name = f"{SYMBOL}-{INTERVAL}-{y:04d}-{m:02d}.zip"
        url = f"{BASE_URL}/monthly/klines/{SYMBOL}/{INTERVAL}/{name}"
        yield url, MONTHLY_DIR / name


def daily_jobs(today: dt.date):
    # 当前月份从 1 号下载到昨天，避免当天尚未完整造成数据不完整。
    current = today.replace(day=1)
    end = today - dt.timedelta(days=1)
    while current <= end:
        name = f"{SYMBOL}-{INTERVAL}-{current:%Y-%m-%d}.zip"
        url = f"{BASE_URL}/daily/klines/{SYMBOL}/{INTERVAL}/{name}"
        yield url, DAILY_DIR / name
        current += dt.timedelta(days=1)


def run_jobs(jobs, title: str):
    jobs = list(jobs)
    print(f"\n[{title}] 共 {len(jobs)} 个文件")
    if not jobs:
        return True

    ok = skip = not_found = err = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        future_map = {pool.submit(download, url, dest): dest for url, dest in jobs}
        for idx, fut in enumerate(concurrent.futures.as_completed(future_map), 1):
            status, msg = fut.result()
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            elif status == "404":
                not_found += 1
            else:
                err += 1
            print(f"[{idx:>3}/{len(jobs)}] {status:<5} {msg}")

    print(f"[{title}] 完成：下载 {ok}，已存在 {skip}，404 {not_found}，失败 {err}")
    return err == 0


def main() -> int:
    today = dt.date.today()
    print("Binance BTCUSDT U本位合约 1分钟K线下载")
    print(f"范围：2020-01-01 ~ {today - dt.timedelta(days=1)}")
    print(f"保存目录：{OUT_ROOT}")

    monthly_ok = run_jobs(monthly_jobs(today), "monthly")
    daily_ok = run_jobs(daily_jobs(today), "daily/current-month")

    print("\n目录说明：")
    print(f"  完整月份：{MONTHLY_DIR}")
    print(f"  当前月份：{DAILY_DIR}")
    print("说明：当前日期当天的数据尚未完整，因此下载到昨天为止。")
    return 0 if monthly_ok and daily_ok else 1


if __name__ == "__main__":
    sys.exit(main())
