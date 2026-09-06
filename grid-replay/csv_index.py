from __future__ import annotations

import csv
import json
from bisect import bisect_right
from pathlib import Path

INDEX_STRIDE = 10_000
MAX_LIMIT = 100_000

class CsvIndex:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.cache_path = self.path.with_suffix(self.path.suffix + ".index.json")
        self.header: list[str] = []
        self.offsets: list[int] = []
        self.timestamps: list[int] = []
        self.total_rows = 0
        self.first_time = 0
        self.last_time = 0
        if not self._load_cache():
            self._build()
            self._save_cache()

    def _load_cache(self) -> bool:
        if not self.cache_path.is_file() or not self.path.is_file():
            return False
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            stat = self.path.stat()
            if payload.get("size") != stat.st_size or payload.get("mtimeNs") != stat.st_mtime_ns:
                return False
            self.header = list(payload["header"])
            self.offsets = [int(v) for v in payload["offsets"]]
            self.timestamps = [int(v) for v in payload["timestamps"]]
            self.total_rows = int(payload["totalRows"])
            self.first_time = int(payload["firstTime"])
            self.last_time = int(payload["lastTime"])
            return bool(self.offsets and self.total_rows)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False

    def _save_cache(self) -> None:
        stat = self.path.stat()
        self.cache_path.write_text(json.dumps({
            "size": stat.st_size,
            "mtimeNs": stat.st_mtime_ns,
            "header": self.header,
            "offsets": self.offsets,
            "timestamps": self.timestamps,
            "totalRows": self.total_rows,
            "firstTime": self.first_time,
            "lastTime": self.last_time,
        }, separators=(",", ":")), encoding="utf-8")

    def _build(self) -> None:
        if not self.path.is_file():
            raise FileNotFoundError(f"K线文件不存在: {self.path}")
        print("首次启动：正在为大 CSV 建立随机访问索引...")
        with self.path.open("rb") as fh:
            header_line = fh.readline().decode("utf-8-sig").rstrip("\r\n")
            self.header = next(csv.reader([header_line]))
            row_index = 0
            while True:
                offset = fh.tell()
                raw = fh.readline()
                if not raw:
                    break
                try:
                    ts = int(raw.split(b",", 1)[0])
                except ValueError:
                    continue
                if row_index == 0:
                    self.first_time = ts
                if row_index % INDEX_STRIDE == 0:
                    self.offsets.append(offset)
                    self.timestamps.append(ts)
                self.last_time = ts
                row_index += 1
            self.total_rows = row_index

    def seek_offset_for_row(self, row: int) -> tuple[int, int]:
        row = max(0, min(row, max(0, self.total_rows - 1)))
        block = row // INDEX_STRIDE
        return self.offsets[block], block * INDEX_STRIDE

    def row_for_timestamp(self, timestamp: int) -> int:
        if timestamp <= self.first_time:
            return 0
        if timestamp >= self.last_time:
            return max(0, self.total_rows - 1)
        block = max(0, bisect_right(self.timestamps, timestamp) - 1)
        base_row = block * INDEX_STRIDE
        with self.path.open("rb") as fh:
            fh.seek(self.offsets[block])
            row = base_row
            while row < self.total_rows:
                raw = fh.readline()
                if not raw:
                    break
                try:
                    ts = int(raw.split(b",", 1)[0])
                except ValueError:
                    continue
                if ts >= timestamp:
                    return row
                row += 1
                if row >= base_row + INDEX_STRIDE and block + 1 < len(self.offsets):
                    break
        return min(row, self.total_rows - 1)

    def rows(self, offset: int, limit: int) -> list[list[str]]:
        offset = max(0, min(offset, self.total_rows))
        limit = max(1, min(limit, MAX_LIMIT))
        if offset >= self.total_rows:
            return []
        byte_offset, base_row = self.seek_offset_for_row(offset)
        result: list[list[str]] = []
        with self.path.open("rb") as fh:
            fh.seek(byte_offset)
            current = base_row
            while current < offset:
                if not fh.readline():
                    return result
                current += 1
            while len(result) < limit:
                raw = fh.readline()
                if not raw:
                    break
                text = raw.decode("utf-8").rstrip("\r\n")
                if text:
                    result.append(next(csv.reader([text])))
        return result

