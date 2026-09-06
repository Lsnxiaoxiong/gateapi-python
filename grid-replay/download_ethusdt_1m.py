"""Download verified Binance USD-M ETHUSDT minute archives and merge them."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "https://data.binance.vision/data/futures/um"
HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore".split(",")


def replace_file(source: Path, destination: Path) -> None:
    # Windows scanners may briefly hold a newly written large file open.
    for attempt in range(10):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(1)


def fetch(url: str) -> bytes:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            if attempt == 3:
                raise
        except (OSError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError(url)


def archive(kind: str, period: str, cache: Path) -> dict | None:
    name = f"ETHUSDT-1m-{period}.zip"
    url = f"{BASE}/{kind}/klines/ETHUSDT/1m/{name}"
    try:
        checksum = fetch(url + ".CHECKSUM").decode().split()[0]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    path = cache / kind / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
        content = fetch(url)
        if hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError(f"Checksum mismatch: {url}")
        temp = path.with_suffix(".zip.part")
        temp.write_bytes(content)
        replace_file(temp, path)
    return {"path": str(path), "url": url, "sha256": checksum}


def next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def get_month(month: date, start: date, end: date, cache: Path) -> list[dict]:
    following = next_month(month)
    if following <= end:
        item = archive("monthly", month.strftime("%Y-%m"), cache)
        if item:
            return [item]
    items = []
    day = max(month, start)
    while day < min(following, end):
        item = archive("daily", day.isoformat(), cache)
        if item is None:
            raise RuntimeError(f"Daily archive unavailable: {day}; refusing an incomplete dataset")
        items.append(item)
        day += timedelta(days=1)
    return items


def ms(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp() * 1000)


def merge(items: list[dict], output: Path, start: date, end: date) -> dict:
    lower, upper = ms(start), ms(end)
    previous = None
    first = None
    count = 0
    gaps = []
    temp = output.with_suffix(".csv.part")
    with temp.open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(HEADER)
        for item in sorted(items, key=lambda x: Path(x["path"]).name):
            with zipfile.ZipFile(item["path"]) as zf:
                names = [n for n in zf.namelist() if n.endswith(".csv")]
                if len(names) != 1:
                    raise ValueError(f"Unexpected archive members: {item['path']}")
                with zf.open(names[0]) as raw:
                    for row in csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig")):
                        if not row or row[0] == "open_time":
                            continue
                        ts = int(row[0])
                        if ts >= 10 ** 14:
                            ts //= 1000
                            row[0], row[6] = str(ts), str(int(row[6]) // 1000)
                        if not lower <= ts < upper:
                            continue
                        if len(row) != 12 or ts % 60_000:
                            raise ValueError(f"Invalid row: {row}")
                        o, h, l, c, v = map(float, row[1:6])
                        if not all(math.isfinite(x) for x in (o, h, l, c, v)) or not (0 < l <= min(o, c) <= max(o, c) <= h and v >= 0):
                            raise ValueError(f"Invalid OHLCV: {row}")
                        if previous is not None and ts <= previous:
                            raise ValueError(f"Duplicate or unordered timestamp: {ts}")
                        expected = lower if previous is None else previous + 60_000
                        if ts != expected:
                            gaps.append({"start_ms": expected, "end_exclusive_ms": ts, "missing_minutes": (ts - expected) // 60_000})
                        if first is None:
                            first = ts
                        previous = ts
                        writer.writerow(row)
                        count += 1
    if previous is None:
        raise ValueError("No candles downloaded")
    if previous + 60_000 != upper:
        gaps.append({"start_ms": previous + 60_000, "end_exclusive_ms": upper, "missing_minutes": (upper - previous - 60_000) // 60_000})
    replace_file(temp, output)
    return {"symbol": "ETHUSDT", "market": "Binance USD-M perpetual futures", "interval": "1m", "timezone": "UTC", "start_inclusive": start.isoformat(), "end_exclusive": end.isoformat(), "rows": count, "expected_minutes": (upper-lower)//60_000, "first_open_time_ms": first, "last_open_time_ms": previous, "gaps": gaps, "missing_minutes": sum(g['missing_minutes'] for g in gaps), "csv": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "archives": items}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    today = datetime.now(timezone.utc).date()
    parser.add_argument("--end", type=date.fromisoformat, default=today, help="Exclusive UTC date")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "ETHUSDT" / "1m")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    start = args.start or args.end.replace(year=args.end.year - 5, day=min(args.end.day, 28) if args.end.month == 2 else args.end.day)
    if start >= args.end or args.end > today or args.workers < 1:
        parser.error("Require start < end <= today and workers >= 1")
    folder = args.output_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    months = []
    month = start.replace(day=1)
    while month < args.end:
        months.append(month)
        month = next_month(month)
    items = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(get_month, m, start, args.end, folder / "archives"): m for m in months}
        for i, future in enumerate(as_completed(futures), 1):
            items.extend(future.result())
            print(f"[{i}/{len(months)}] verified {futures[future]:%Y-%m}", flush=True)
    output = folder / f"ETHUSDT-1m-{start}_to_{args.end - timedelta(days=1)}.csv"
    report = merge(items, output, start, args.end)
    output.with_suffix(".manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "archives"}, indent=2), flush=True)


if __name__ == "__main__":
    main()
