from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from itertools import pairwise

from market_data import Candle, local_datetime
from strategies import Strategy


DAY_MS = 86_400_000


@dataclass(frozen=True)
class Costs:
    fee: float
    slippage: float


@dataclass(frozen=True)
class Entry:
    time: int
    market_price: float
    fill_price: float
    quantity: float
    fee: float
    starting_cash: float


@dataclass(frozen=True)
class Trade:
    entry: Entry
    exit_time: int
    exit_market_price: float
    exit_fill_price: float
    exit_fee: float
    net_pnl: float
    reason: str

    @property
    def fees(self) -> float:
        return self.entry.fee + self.exit_fee

    @property
    def slippage(self) -> float:
        price_difference = (
            self.entry.fill_price - self.entry.market_price
            + self.exit_market_price - self.exit_fill_price
        )
        return self.entry.quantity * price_difference


@dataclass
class Result:
    strategy: Strategy
    equity: list[tuple[int, float]]
    trades: list[Trade]

    @property
    def total_return(self) -> float:
        return self.equity[-1][1] / self.equity[0][1] - 1

    @property
    def annualized_return(self) -> float:
        days = (self.equity[-1][0] - self.equity[0][0]) / DAY_MS
        return (1 + self.total_return) ** (365.2425 / days) - 1

    @property
    def max_drawdown(self) -> float:
        peak = self.equity[0][1]
        drawdown = 0.0
        for _, value in self.equity:
            peak = max(peak, value)
            drawdown = max(drawdown, 1 - value / peak)
        return drawdown

    @property
    def sharpe(self) -> float | None:
        daily_closes = {
            local_datetime(timestamp).date(): value for timestamp, value in self.equity[1:]
        }
        values = [self.equity[0][1], *daily_closes.values()]
        returns = [current / previous - 1 for previous, current in pairwise(values)]
        deviation = statistics.stdev(returns) if len(returns) >= 2 else 0.0
        return statistics.fmean(returns) / deviation * math.sqrt(365) if deviation else None

    @property
    def annual_returns(self) -> dict[int, float]:
        closing_equity = {
            local_datetime(timestamp).year: value for timestamp, value in self.equity[1:]
        }
        values = [self.equity[0][1], *closing_equity.values()]
        return {
            year: current / previous - 1
            for year, (previous, current) in zip(closing_equity, pairwise(values))
        }


def open_position(cash: float, time: int, market_price: float, costs: Costs) -> Entry:
    fill_price = market_price * (1 + costs.slippage)
    quantity = cash / (fill_price * (1 + costs.fee))
    return Entry(
        time, market_price, fill_price, quantity,
        quantity * fill_price * costs.fee, cash,
    )


def close_position(
    entry: Entry, time: int, market_price: float, costs: Costs, reason: str,
) -> tuple[float, Trade]:
    fill_price = market_price * (1 - costs.slippage)
    notional = entry.quantity * fill_price
    fee = notional * costs.fee
    cash = notional - fee
    return cash, Trade(entry, time, market_price, fill_price, fee, cash - entry.starting_cash, reason)


def run_strategy(
    strategy: Strategy, candles: list[Candle], first_index: int, capital: float, costs: Costs,
) -> Result:
    targets = strategy.targets([candle for candle in candles if candle.complete])
    cash = capital
    position = None
    pending = strategy.buy_at_start
    equity = [(candles[first_index].open_time, capital)]
    trades: list[Trade] = []
    terminal = next((bar.last_trade for bar in reversed(candles[first_index:]) if bar.last_trade), None)

    for index, candle in enumerate(candles):
        if index >= first_index:
            if candle.first_trade is not None:
                if pending and position is None:
                    position = open_position(cash, *candle.first_trade, costs)
                    cash = 0.0
                elif not pending and position is not None:
                    cash, trade = close_position(position, *candle.first_trade, costs, "signal")
                    trades.append(trade)
                    position = None

            if terminal is not None and candle.last_trade == terminal and position is not None:
                cash, trade = close_position(position, *terminal, costs, "end")
                trades.append(trade)
                position = None

            value = cash if position is None else position.quantity * candle.close
            equity.append((candle.close_time, value))

        if candle.complete:
            pending = next(targets)

    return Result(strategy, equity, trades)
