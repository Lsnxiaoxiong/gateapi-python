from __future__ import annotations

import csv
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


MINUTE_MS = 60_000
BAR_MINUTES = 240
BAR_MS = BAR_MINUTES * MINUTE_MS
DISPLAY_TZ = timezone(timedelta(hours=8))
DEFAULT_CSV = Path(__file__).resolve().parents[1] / (
    "data/binance/futures/um/BTCUSDT/1m/monthly/merged/"
    "BTCUSDT-1m-2020-01_to_2026-07.csv"
)


@dataclass
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    minutes: int = 1
    first_trade: tuple[int, float] | None = None
    last_trade: tuple[int, float] | None = None
    expected_minutes: int = BAR_MINUTES

    @property
    def complete(self) -> bool:
        return self.minutes == self.expected_minutes


def local_datetime(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp / 1000, tz=DISPLAY_TZ)


def timestamp_text(timestamp: int) -> str:
    return local_datetime(timestamp).isoformat(timespec="milliseconds")


def iter_minutes(path: Path, end: int | None = None) -> Iterator[tuple[int, float, float, float, float, bool]]:
    previous_time = None
    with path.open(encoding="utf-8-sig", newline="") as source:
        rows = csv.reader(source)
        expected = ["open_time", "open", "high", "low", "close"]
        header = next(rows, [])
        if header[:5] != expected:
            raise ValueError(f"{path.name}: expected CSV columns {', '.join(expected)}")
        volume_field = header.index("volume") if "volume" in header else None

        for row in rows:
            if not row:
                continue
            try:
                timestamp = int(row[0])
                if timestamp % MINUTE_MS or (
                    previous_time is not None and timestamp <= previous_time
                ):
                    raise ValueError("minute timestamps must be aligned and strictly increasing")
                if end is not None and timestamp >= end:
                    break
                opening, high, low, closing = map(float, row[1:5])
                volume = float(row[volume_field]) if volume_field is not None else 1.0
                if not (
                    0 < low <= opening <= high
                    and low <= closing <= high
                    and math.isfinite(high)
                ):
                    raise ValueError("invalid OHLC prices")
                if not math.isfinite(volume) or volume < 0:
                    raise ValueError("invalid base volume")
            except (IndexError, ValueError) as exc:
                raise ValueError(f"{path.name}, line {rows.line_num}: {exc}") from exc
            previous_time = timestamp
            yield timestamp, opening, high, low, closing, volume > 0


def load_candles(path: Path, end: int | None, *, bar_minutes: int = BAR_MINUTES) -> list[Candle]:
    """Load chronological Binance 1m rows into UTC-aligned buckets."""
    candles: list[Candle] = []
    bar_ms = bar_minutes * MINUTE_MS
    for timestamp, opening, high, low, closing, traded in iter_minutes(path, end):
        close_time = timestamp + MINUTE_MS - 1
        if candles and timestamp // bar_ms == candles[-1].open_time // bar_ms:
            candle = candles[-1]
            candle.high = max(candle.high, high)
            candle.low = min(candle.low, low)
            candle.close = closing
            candle.close_time = close_time
            candle.minutes += 1
        else:
            candle = Candle(
                timestamp, close_time, opening, high, low, closing, expected_minutes=bar_minutes,
            )
            candles.append(candle)
        if traded:
            if candle.first_trade is None:
                candle.first_trade = timestamp, opening
            candle.last_trade = close_time, closing
    return candles
