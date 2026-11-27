from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path

from engine import DAY_MS, Costs
from market_data import BAR_MS, MINUTE_MS, Candle, iter_minutes
from strategies import sma_trend


CAPITAL = 10_000.0
MARKET_COSTS = Costs(0.0005, 0.0002)
GRID_LIMIT_FEE = 0.0005
HOLDOUT_START = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


@dataclass
class Portfolio:
    name: str
    cash: float = CAPITAL
    units: float = 0.0
    fees: float = 0.0
    fills: int = 0
    points: list[tuple[int, float, float]] = field(default_factory=list)

    def equity(self, price: float) -> float:
        return self.cash + self.units * price

    def fill(self, quantity: float, price: float, fee_rate: float) -> None:
        fee = abs(quantity) * price * fee_rate
        self.cash -= quantity * price + fee
        self.units += quantity
        self.fees += fee
        self.fills += 1

    def adjust(self, price: float, weight: float) -> None:
        gap = weight * self.equity(price) - self.units * price
        if abs(gap) < 1e-8:
            return
        slip, fee = MARKET_COSTS.slippage, MARKET_COSTS.fee
        if gap > 0:
            fill_price = price * (1 + slip)
            cost = (1 + slip) * (1 + fee) - 1
            notional = gap / (1 + weight * cost)
        else:
            fill_price = price * (1 - slip)
            cost = 1 - (1 - slip) * (1 - fee)
            notional = gap / (1 - weight * cost)
        self.fill(notional / price, fill_price, fee)
        if weight == 0:
            self.units = 0.0

    def mark(self, timestamp: int, price: float) -> None:
        value = self.equity(price)
        self.points.append((timestamp, value, self.units * price / value))


class Grid:
    steps = 10
    ratio = 1.01

    def __init__(self, name: str, reset_on_breakout: bool) -> None:
        self.account = Portfolio(name)
        self.reset_on_breakout = reset_on_breakout
        self.month: tuple[int, int] | None = None
        self.reset_pending = False
        self.resets = 0
        self.closed_pairs = 0
        self.reset_pnl = 0.0
        self.grid_pnl = 0.0
        self.open_lots: dict[int, float] = {}

    def start_grid(self, price: float, month: tuple[int, int]) -> None:
        self.close_inventory(price)
        if self.month != month:
            self.order_budget = self.account.cash / self.steps
        else:
            self.resets += 1
        self.month = month
        self.reset_pending = False
        self.buys = [price / self.ratio ** step for step in range(1, self.steps + 1)]
        self.sells = [buy * self.ratio for buy in self.buys]
        self.lower, self.upper = self.buys[-1], price * self.ratio ** self.steps
        self.refresh_limits()

    def refresh_limits(self) -> None:
        self.highest_buy = max(
            (price for index, price in enumerate(self.buys) if index not in self.open_lots),
            default=0.0,
        )
        self.lowest_sell = min((self.sells[index] for index in self.open_lots), default=math.inf)

    def on_minute(self, month: tuple[int, int], opening: float, high: float, low: float, close: float) -> None:
        if self.month != month or self.reset_pending:
            self.start_grid(opening, month)
        elif low < self.highest_buy or high > self.lowest_sell:
            self.fill_limits(high, low)
        if self.reset_on_breakout and (close < self.lower or close > self.upper):
            self.reset_pending = True

    def fill_limits(self, high: float, low: float) -> None:
        cash_at_open = self.account.cash
        for index, buy in enumerate(self.buys):
            quantity = self.open_lots.get(index)
            sell = self.sells[index]
            if quantity is not None:
                if high > sell:
                    self.account.fill(-quantity, sell, GRID_LIMIT_FEE)
                    self.grid_pnl += quantity * (sell * (1 - GRID_LIMIT_FEE) - buy * (1 + GRID_LIMIT_FEE))
                    self.closed_pairs += 1
                    del self.open_lots[index]
            elif low < buy and cash_at_open + 1e-8 >= self.order_budget:
                quantity = self.order_budget / (buy * (1 + GRID_LIMIT_FEE))
                self.account.fill(quantity, buy, GRID_LIMIT_FEE)
                self.open_lots[index] = quantity
                cash_at_open -= self.order_budget
        self.refresh_limits()

    def close_inventory(self, price: float) -> None:
        if not self.open_lots:
            return
        cost_basis = sum(
            self.buys[index] * quantity * (1 + GRID_LIMIT_FEE)
            for index, quantity in self.open_lots.items()
        )
        cash_before = self.account.cash
        self.account.adjust(price, 0)
        self.reset_pnl += self.account.cash - cash_before - cost_basis
        self.open_lots.clear()


class Intraday:
    def __init__(self) -> None:
        self.signal = Portfolio("日内动量")
        self.control = Portfolio("同一时段始终做多")
        self.day = None

    def on_minute(self, day: int, minute: int, opening: float, close: float) -> None:
        if day != self.day:
            self.day = day
            self.first = self.last = None
            self.entered = False
            self.signal.adjust(opening, 0)
            self.control.adjust(opening, 0)
        if 13 * 60 + 30 <= minute < 14 * 60:
            if self.first is None:
                self.first = opening
            self.last = close
        if 20 * 60 + 30 <= minute < 21 * 60 and not self.entered:
            self.entered = True
            self.control.adjust(opening, 1)
            if self.first is not None and self.last > self.first:
                self.signal.adjust(opening, 1)
        if self.entered and minute >= 21 * 60:
            self.entered = False
            self.signal.adjust(opening, 0)
            self.control.adjust(opening, 0)


def run_minute_experiments(path: Path) -> tuple[list[Grid], Intraday]:
    grids = [Grid("固定月内网格", False), Grid("动态月内网格", True)]
    intraday = Intraday()
    accounts = [*(grid.account for grid in grids), intraday.signal, intraday.control]
    last_time = last_close = last_traded_close = None
    prev_day = None
    for timestamp, opening, high, low, closing, traded in iter_minutes(path):
        if last_time is None:
            for account in accounts:
                account.mark(timestamp, opening)
        elif timestamp // BAR_MS != last_time // BAR_MS:
            for account in accounts:
                account.mark(last_time + MINUTE_MS - 1, last_close)
        day, offset = divmod(timestamp, DAY_MS)
        if day != prev_day:
            date = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            month = date.year, date.month
            prev_day = day
        if traded:
            for grid in grids:
                grid.on_minute(month, opening, high, low, closing)
            intraday.on_minute(day, offset // MINUTE_MS, opening, closing)
            last_traded_close = closing
        last_time, last_close = timestamp, closing

    if last_traded_close is None:
        raise ValueError("no tradable minutes")
    for grid in grids:
        grid.close_inventory(last_traded_close)
    for account in (intraday.signal, intraday.control):
        account.adjust(last_traded_close, 0)
    for account in accounts:
        account.mark(last_time + MINUTE_MS - 1, last_close)
    return grids, intraday


def volatility_orders(candles: list[Candle]) -> list[tuple[int, float, bool, float]]:
    complete = [bar for bar in candles if bar.complete]
    trends = iter(sma_trend(complete))
    returns: list[float] = []
    previous_close = None
    invested = False
    target = 0.0
    pending = False
    orders = []
    for candle in candles:
        if candle.first_trade is not None and pending:
            orders.append((*candle.first_trade, invested, target))
            pending = False
        if candle.complete:
            if previous_close is not None:
                returns.append(math.log(candle.close / previous_close))
            previous_close = candle.close
            trend = next(trends)
            vol = statistics.stdev(returns[-180:]) * math.sqrt(365 * 6) if len(returns) >= 180 else 0.0
            target = min(1.0, 0.40 / vol) if vol else 1.0
            day_close = (candle.close_time + 1) % DAY_MS == 0
            pending = pending or trend != invested or (trend and day_close)
            invested = trend
    return orders


def run_volatility_experiment(candles: list[Candle]) -> tuple[list[Portfolio], float]:
    orders = volatility_orders(candles)
    calibration = [(time, weight if active else None) for time, _, active, weight in orders
                   if time < HOLDOUT_START]
    calibration.append((HOLDOUT_START, None))
    weighted_minutes = [(weight, (end - start) // MINUTE_MS)
                        for (start, weight), (end, _) in pairwise(calibration) if weight is not None]
    total_duration = sum(duration for _, duration in weighted_minutes)
    if not total_duration:
        raise ValueError("fixed position calibration requires active signals before 2024")
    matched = sum(w * duration for w, duration in weighted_minutes) / total_duration
    portfolios = [Portfolio("原双均线"), Portfolio("双均线＋波动率目标"), Portfolio("双均线＋固定仓位")]
    orders_by_time = {order[0]: order for order in orders}
    terminal_time, terminal_price = next(bar.last_trade for bar in reversed(candles) if bar.last_trade)
    for account in portfolios:
        account.mark(candles[0].open_time, candles[0].open)
    for candle in candles:
        if candle.first_trade is not None and candle.first_trade[0] in orders_by_time:
            _, price, active, target = orders_by_time[candle.first_trade[0]]
            for account, weight in zip(portfolios, (1.0, target, matched), strict=True):
                account.adjust(price, weight if active else 0)
        for account in portfolios:
            if candle.last_trade is not None and candle.last_trade[0] == terminal_time:
                account.adjust(terminal_price, 0)
            account.mark(candle.close_time, candle.close)
    return portfolios, matched
