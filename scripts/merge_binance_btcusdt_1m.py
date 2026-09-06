from __future__ import annotations

import csv
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "binance" / "futures" / "um" / "BTCUSDT" / "1m" / "monthly"
EXTRACT_DIR = DATA_DIR / "extracted"
OUTPUT_FILE = DATA_DIR / "BTCUSDT-1m-2020-01_to_2026-07.csv"

EXPECTED_HEADER = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]


def extract_all() -> list[Path]:
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    csv_files: list[Path] = []

    zip_files = sorted(DATA_DIR.glob("BTCUSDT-1m-*.zip"))
    if not zip_files:
        raise FileNotFoundError(f"未找到 ZIP 文件: {DATA_DIR}")

    for zip_path in zip_files:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
            if len(members) != 1:
                raise RuntimeError(f"{zip_path.name} 中 CSV 数量异常: {len(members)}")

            member = members[0]
            target = EXTRACT_DIR / Path(member).name
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            csv_files.append(target)

    return sorted(csv_files)


def normalize_header(row: list[str]) -> list[str] | None:
    if not row:
        return None

    first = row[0].strip().lower()
    if first in {"open_time", "open time"}:
        return EXPECTED_HEADER
    return None


def merge_csv(csv_files: list[Path]) -> tuple[int, int, int]:
    total_rows = 0
    written_rows = 0
    duplicate_rows = 0
    seen_open_times: set[str] = set()

    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as out_f:
        writer = csv.writer(out_f)
        writer.writerow(EXPECTED_HEADER)

        for csv_path in sorted(csv_files):
            with csv_path.open("r", newline="", encoding="utf-8") as in_f:
                reader = csv.reader(in_f)
                for row in reader:
                    if not row:
                        continue

                    total_rows += 1

                    if normalize_header(row) is not None:
                        continue

                    if len(row) < 12:
                        raise RuntimeError(
                            f"{csv_path.name} 存在字段不足的行: {len(row)} 列"
                        )

                    open_time = row[0].strip()
                    if open_time in seen_open_times:
                        duplicate_rows += 1
                        continue

                    seen_open_times.add(open_time)
                    writer.writerow(row[:12])
                    written_rows += 1

    return total_rows, written_rows, duplicate_rows


def main() -> None:
    print(f"数据目录: {DATA_DIR}")
    csv_files = extract_all()
    print(f"已解压 CSV: {len(csv_files)} 个")

    total_rows, written_rows, duplicate_rows = merge_csv(csv_files)

    print(f"读取总行数: {total_rows:,}")
    print(f"写入 K 线数: {written_rows:,}")
    print(f"重复 open_time: {duplicate_rows:,}")
    print(f"合并文件: {OUTPUT_FILE}")
    print(f"文件大小: {OUTPUT_FILE.stat().st_size / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    main()
