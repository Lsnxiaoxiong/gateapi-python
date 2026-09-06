from __future__ import annotations

import csv
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS
WEEK_MS = 7 * DAY_MS
MONDAY_EPOCH_MS = int(datetime(1970, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)
SUPPORTED_INTERVALS = {"1h", "4h", "12h", "1d", "2d", "3d", "1w", "2w", "3w", "1mo"}


def interval_bucket_start(ts: int, interval: str) -> int:
    if interval == "1mo":
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        return int(datetime(dt.year, dt.month, 1, tzinfo=timezone.utc).timestamp() * 1000)
    fixed = {
        "1h": HOUR_MS,
        "4h": 4 * HOUR_MS,
        "12h": 12 * HOUR_MS,
        "1d": DAY_MS,
        "2d": 2 * DAY_MS,
        "3d": 3 * DAY_MS,
    }.get(interval)
    if fixed:
        return (ts // fixed) * fixed
    weeks = {"1w": 1, "2w": 2, "3w": 3}.get(interval, 1)
    span = weeks * WEEK_MS
    return MONDAY_EPOCH_MS + ((ts - MONDAY_EPOCH_MS) // span) * span


def interval_bucket_end(start: int, interval: str) -> int:
    if interval == "1mo":
        dt = datetime.fromtimestamp(start / 1000, tz=timezone.utc)
        if dt.month == 12:
            end = datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(dt.year, dt.month + 1, 1, tzinfo=timezone.utc)
        return int(end.timestamp() * 1000)
    fixed = {
        "1h": HOUR_MS,
        "4h": 4 * HOUR_MS,
        "12h": 12 * HOUR_MS,
        "1d": DAY_MS,
        "2d": 2 * DAY_MS,
        "3d": 3 * DAY_MS,
        "1w": WEEK_MS,
        "2w": 2 * WEEK_MS,
        "3w": 3 * WEEK_MS,
    }[interval]
    return start + fixed


def _positive_float(name: str, value, default: float | None = None) -> float:
    if value in (None, "") and default is not None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return result


def _positive_int(name: str, value, default: int) -> int:
    try:
        result = int(value if value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if result <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return result


@dataclass(slots=True)
class BacktestConfig:
    display_interval: str
    start_offset: int
    reference_price: float
    margin_per_grid: float
    leverage: float
    grid_step_pct: float
    take_profit_pct: float
    buy_grid_count: int
    take_profit_count: int
    take_profit_grid_count: int
    max_unclosed_per_grid: int
    price_tick: float
    qty_step: float
    fee_pct: float
    frame_limit: int

    @classmethod
    def from_payload(cls, payload: dict, start_offset: int) -> "BacktestConfig":
        interval = str(payload.get("displayInterval", "1h"))
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"不支持的显示级别: {interval}")
        grid_step_pct = _positive_float("gridStepPct", payload.get("gridStepPct"), 0.3)
        take_profit_pct = _positive_float("takeProfitPct", payload.get("takeProfitPct"), 0.3)
        if grid_step_pct >= 100 or take_profit_pct >= 100:
            raise ValueError("网格间距和止盈比例必须小于 100%")
        fee_pct = float(payload.get("feePct", 0.05) or 0)
        if not math.isfinite(fee_pct) or fee_pct < 0 or fee_pct > 10:
            raise ValueError("feePct 必须在 0~10 之间")
        frame_limit = min(5000, _positive_int("frameLimit", payload.get("frameLimit"), 2000))
        reference_price = float(payload.get("referencePrice", 0) or 0)
        if not math.isfinite(reference_price) or reference_price < 0:
            raise ValueError("referencePrice 不能小于 0")
        return cls(
            display_interval=interval,
            start_offset=max(0, int(start_offset)),
            reference_price=reference_price,
            margin_per_grid=_positive_float("marginPerGrid", payload.get("marginPerGrid"), 100),
            leverage=_positive_float("leverage", payload.get("leverage"), 2),
            grid_step_pct=grid_step_pct,
            take_profit_pct=take_profit_pct,
            buy_grid_count=min(100, _positive_int("buyGridCount", payload.get("buyGridCount"), 10)),
            take_profit_count=min(100, _positive_int("takeProfitCount", payload.get("takeProfitCount"), 10)),
            take_profit_grid_count=min(500, _positive_int("takeProfitGridCount", payload.get("takeProfitGridCount"), 20)),
            max_unclosed_per_grid=min(20, _positive_int("maxUnclosedPerGrid", payload.get("maxUnclosedPerGrid"), 2)),
            price_tick=_positive_float("priceTick", payload.get("priceTick"), 0.1),
            qty_step=_positive_float("qtyStep", payload.get("qtyStep"), 0.001),
            fee_pct=fee_pct,
            frame_limit=frame_limit,
        )


class GridBacktester:
    def __init__(self, config: BacktestConfig) -> None:
        self.cfg = config
        self.pending_buys: list[dict] = []
        self.positions: list[dict] = []
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.completed_count = 0
        self.current_time = 0
        self.current_price = 0.0
        self.current_offset = config.start_offset
        self.next_id = 1
        self.frame_events: deque[list] = deque(maxlen=100)
        self.max_positions = 0
        self.max_margin_used = 0.0
        self.max_notional_exposure = 0.0
        self.peak_net_pnl = 0.0
        self.max_drawdown = 0.0

    def round_price(self, value: float) -> float:
        tick = self.cfg.price_tick
        return round(value / tick) * tick

    def round_qty(self, value: float) -> float:
        step = self.cfg.qty_step
        return math.floor(value / step + 1e-10) * step

    def grid_price(self, index: int) -> float:
        ratio = 1.0 - self.cfg.grid_step_pct / 100.0
        return self.round_price(self.cfg.reference_price * (ratio ** index))

    def first_buy_index(self, current: float) -> int:
        ratio = current / self.cfg.reference_price
        step_ratio = 1.0 - self.cfg.grid_step_pct / 100.0
        index = math.floor(math.log(ratio) / math.log(step_ratio)) + 1
        guard = 0
        while self.grid_price(index) >= current and guard < 1000:
            index += 1
            guard += 1
        guard = 0
        while self.grid_price(index - 1) < current and guard < 1000:
            index -= 1
            guard += 1
        return index

    def desired_grid_indices(self, current: float) -> list[int]:
        result: list[int] = []
        index = self.first_buy_index(current)
        guard = 0
        last_price = None
        while len(result) < self.cfg.buy_grid_count and guard < self.cfg.buy_grid_count * 100:
            price = self.grid_price(index)
            if price != last_price:
                result.append(index)
                last_price = price
            index += 1
            guard += 1
        return result

    def reconcile_buys(self, current: float) -> None:
        desired = set(self.desired_grid_indices(current))
        self.pending_buys = [order for order in self.pending_buys if order["grid"] in desired]
        existing = {order["grid"] for order in self.pending_buys}
        unclosed: dict[int, int] = {}
        for pos in self.positions:
            unclosed[pos["grid"]] = unclosed.get(pos["grid"], 0) + 1
        for grid_index in desired:
            if grid_index in existing or unclosed.get(grid_index, 0) >= self.cfg.max_unclosed_per_grid:
                continue
            price = self.grid_price(grid_index)
            size = self.round_qty(self.cfg.margin_per_grid * self.cfg.leverage / price)
            if size <= 0:
                continue
            self.pending_buys.append({"id": self.next_id, "grid": grid_index, "price": price, "size": size})
            self.next_id += 1
        self.pending_buys.sort(key=lambda row: row["price"], reverse=True)

    def take_profit_window_upper(self, current: float) -> float:
        return self.grid_price(self.first_buy_index(current) - self.cfg.take_profit_grid_count)

    def reconcile_take_profits(self, current: float) -> None:
        upper = self.take_profit_window_upper(current)
        candidates = [p for p in self.positions if p["tp"] <= upper]
        candidates.sort(key=lambda p: abs(p["tp"] - current))
        open_ids = {p["id"] for p in candidates[: self.cfg.take_profit_count]}
        for pos in self.positions:
            pos["tp_open"] = pos["id"] in open_ids

    def process_take_profits(self, minute: list[float | int]) -> None:
        ts, open_price, high = int(minute[0]), float(minute[1]), float(minute[2])
        fee_ratio = self.cfg.fee_pct / 100.0
        if not self.positions:
            return
        remaining: list[dict] = []
        for pos in self.positions:
            if not pos["tp_open"] or high < pos["tp"]:
                remaining.append(pos)
                continue
            exit_price = max(pos["tp"], open_price if open_price >= pos["tp"] else pos["tp"])
            exit_fee = exit_price * pos["size"] * fee_ratio
            net = (exit_price - pos["buy"]) * pos["size"] - pos["entry_fee"] - exit_fee
            self.realized_pnl += net
            self.total_fees += exit_fee
            self.completed_count += 1
            self.frame_events.append(["sell", ts, f"止盈成交 {pos['buy']:.2f} → {exit_price:.2f}，PnL {net:.2f} USDT"])
        self.positions = remaining

    def process_buy_fills(self, minute: list[float | int]) -> None:
        ts, low = int(minute[0]), float(minute[3])
        filled = [order for order in self.pending_buys if low <= order["price"]]
        if not filled:
            return
        filled_ids = {order["id"] for order in filled}
        self.pending_buys = [order for order in self.pending_buys if order["id"] not in filled_ids]
        fee_ratio = self.cfg.fee_pct / 100.0
        for order in filled:
            entry_fee = order["price"] * order["size"] * fee_ratio
            tp = self.round_price(order["price"] * (1.0 + self.cfg.take_profit_pct / 100.0))
            self.positions.append({
                "id": self.next_id,
                "grid": order["grid"],
                "buy": order["price"],
                "size": order["size"],
                "tp": tp,
                "tp_open": False,
                "entry_fee": entry_fee,
            })
            self.next_id += 1
            self.total_fees += entry_fee
            self.frame_events.append(["buy", ts, f"买单成交 @ {order['price']:.2f}，数量 {order['size']:.4f}，计划止盈 {tp:.2f}"])

    def unrealized_pnl(self) -> float:
        return sum(
            (self.current_price - pos["buy"]) * pos["size"] - pos["entry_fee"]
            for pos in self.positions
        )

    def notional_exposure(self) -> float:
        return sum(pos["buy"] * pos["size"] for pos in self.positions)

    def update_statistics(self) -> None:
        position_count = len(self.positions)
        margin_used = position_count * self.cfg.margin_per_grid
        exposure = self.notional_exposure()
        net_pnl = self.realized_pnl + self.unrealized_pnl()
        self.max_positions = max(self.max_positions, position_count)
        self.max_margin_used = max(self.max_margin_used, margin_used)
        self.max_notional_exposure = max(self.max_notional_exposure, exposure)
        self.peak_net_pnl = max(self.peak_net_pnl, net_pnl)
        self.max_drawdown = max(self.max_drawdown, self.peak_net_pnl - net_pnl)

    def process_minute(self, minute: list[float | int]) -> None:
        self.current_time = int(minute[0])
        self.current_price = float(minute[4])
        self.process_take_profits(minute)
        self.process_buy_fills(minute)
        self.reconcile_buys(self.current_price)
        self.reconcile_take_profits(self.current_price)
        self.current_offset += 1
        self.update_statistics()

    def compact_buys(self) -> list[list[float | int]]:
        return [[o["grid"], o["price"], o["size"]] for o in self.pending_buys]

    def compact_positions(self) -> list[list[float | int | bool]]:
        return [
            [p["grid"], p["buy"], p["tp"], p["size"], bool(p["tp_open"]), p["entry_fee"]]
            for p in self.positions
        ]

    def capture_frame(self, candle: list[float | int]) -> dict:
        return {
            "k": candle,
            "o": self.current_offset,
            "t": self.current_time,
            "p": self.current_price,
            "r": self.realized_pnl,
            "f": self.total_fees,
            "cc": self.completed_count,
            "b": self.compact_buys(),
            "ps": self.compact_positions(),
            "e": list(self.frame_events),
        }


def iter_normalized_rows(index, offset: int) -> Iterable[list[float | int]]:
    if offset >= index.total_rows:
        return
    byte_offset, base_row = index.seek_offset_for_row(offset)
    with index.path.open("rb") as fh:
        fh.seek(byte_offset)
        current = base_row
        while current < offset:
            if not fh.readline():
                return
            current += 1
        while current < index.total_rows:
            raw = fh.readline()
            if not raw:
                return
            text = raw.decode("utf-8").rstrip("\r\n")
            if not text:
                continue
            row = next(csv.reader([text]))
            yield [int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])]
            current += 1


def run_backtest(index, payload: dict, progress_callback: Callable[[dict], None] | None = None) -> dict:
    requested_ts = int(payload.get("startTime", index.first_time) or index.first_time)
    interval = str(payload.get("displayInterval", "1h"))
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"不支持的显示级别: {interval}")
    aligned_ts = max(index.first_time, interval_bucket_start(requested_ts, interval))
    start_offset = index.row_for_timestamp(aligned_ts)
    cfg = BacktestConfig.from_payload(payload, start_offset)
    rows = iter_normalized_rows(index, start_offset)
    try:
        first = next(rows)
    except StopIteration:
        return {"initial": {}, "frames": [], "summary": {"frameCount": 0}}

    if cfg.reference_price <= 0:
        cfg.reference_price = float(first[1])
    engine = GridBacktester(cfg)
    engine.current_time = int(first[0])
    engine.current_price = float(first[1])
    engine.reconcile_buys(engine.current_price)
    engine.reconcile_take_profits(engine.current_price)

    initial = {
        "offset": start_offset,
        "time": int(first[0]),
        "price": float(first[1]),
        "referencePrice": cfg.reference_price,
        "buys": engine.compact_buys(),
        "positions": [],
    }

    frames: list[dict] = []
    current_row = first
    ended = False

    target_bucket_start = interval_bucket_start(int(first[0]), cfg.display_interval)
    target_bucket_end = target_bucket_start
    for _ in range(cfg.frame_limit):
        target_bucket_end = interval_bucket_end(target_bucket_end, cfg.display_interval)
    target_offset = min(index.total_rows, index.row_for_timestamp(target_bucket_end))
    target_minutes = max(1, target_offset - start_offset)
    last_progress_offset = start_offset

    def report_progress(force: bool = False) -> None:
        nonlocal last_progress_offset
        processed = max(0, engine.current_offset - start_offset)
        if not force and engine.current_offset - last_progress_offset < 2000:
            return
        last_progress_offset = engine.current_offset
        if progress_callback is not None:
            progress_callback({
                "progress": min(99.9, processed / target_minutes * 100),
                "processedMinutes": processed,
                "targetMinutes": target_minutes,
                "generatedFrames": len(frames),
                "frameLimit": cfg.frame_limit,
                "currentTime": engine.current_time,
            })

    report_progress(force=True)
    while len(frames) < cfg.frame_limit and not ended:
        bucket_start = interval_bucket_start(int(current_row[0]), cfg.display_interval)
        bucket_end = interval_bucket_end(bucket_start, cfg.display_interval)
        aggregated: list[float | int] | None = None
        engine.frame_events.clear()

        while True:
            if int(current_row[0]) >= bucket_end:
                break
            engine.process_minute(current_row)
            report_progress()
            if aggregated is None:
                aggregated = [bucket_start, current_row[1], current_row[2], current_row[3], current_row[4], current_row[5]]
            else:
                aggregated[2] = max(float(aggregated[2]), float(current_row[2]))
                aggregated[3] = min(float(aggregated[3]), float(current_row[3]))
                aggregated[4] = current_row[4]
                aggregated[5] = float(aggregated[5]) + float(current_row[5])
            try:
                current_row = next(rows)
            except StopIteration:
                ended = True
                break

        if aggregated is not None:
            frames.append(engine.capture_frame(aggregated))
            report_progress(force=True)

    if progress_callback is not None:
        progress_callback({
            "progress": 100.0,
            "processedMinutes": max(0, engine.current_offset - start_offset),
            "targetMinutes": target_minutes,
            "generatedFrames": len(frames),
            "frameLimit": cfg.frame_limit,
            "currentTime": engine.current_time,
        })

    return {
        "initial": initial,
        "frames": frames,
        "summary": {
            "frameCount": len(frames),
            "processedMinutes": max(0, engine.current_offset - start_offset),
            "startOffset": start_offset,
            "nextOffset": engine.current_offset,
            "ended": ended,
            "displayInterval": cfg.display_interval,
            "referencePrice": cfg.reference_price,
            "realizedPnl": engine.realized_pnl,
            "fees": engine.total_fees,
            "completedCount": engine.completed_count,
        },
    }
