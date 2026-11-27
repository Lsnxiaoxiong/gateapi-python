from __future__ import annotations

import statistics
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from market_data import Candle


Targets = Callable[[list[Candle]], Iterator[bool]]


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    rule: str
    targets: Targets
    buy_at_start: bool = False


def buy_and_hold(candles: list[Candle]) -> Iterator[bool]:
    for _ in candles:
        yield True


def sma_trend(candles: list[Candle]) -> Iterator[bool]:
    closes: list[float] = []
    for candle in candles:
        closes.append(candle.close)
        yield sma_side(closes, 20, 100) == 1


def sma_side(closes: list[float], fast: int, slow: int) -> int:
    if len(closes) < slow:
        return 0
    fast_total = sum(closes[-fast:]) * (slow / fast)
    slow_total = sum(closes[-slow:])
    return int(fast_total > slow_total) - int(fast_total < slow_total)


def donchian_breakout(candles: list[Candle]) -> Iterator[bool]:
    invested = False
    for index, candle in enumerate(candles):
        if index >= 55:
            previous = candles[index - 55:index]
            if invested:
                invested = candle.close >= min(bar.low for bar in previous[-20:])
            else:
                invested = candle.close > max(bar.high for bar in previous)
        yield invested


def bollinger_reversion(candles: list[Candle]) -> Iterator[bool]:
    invested = False
    closes: list[float] = []
    for candle in candles:
        closes.append(candle.close)
        if len(closes) >= 20:
            window = closes[-20:]
            mean = statistics.fmean(window)
            deviation = statistics.pstdev(window, mu=mean)
            invested = candle.close < (mean if invested else mean - 2 * deviation)
        yield invested


def rsi_pullback(candles: list[Candle]) -> Iterator[bool]:
    invested = False
    closes: list[float] = []
    gain = loss = 0.0
    period = 14
    for index, candle in enumerate(candles):
        closes.append(candle.close)
        if index:
            change = closes[-1] - closes[-2]
            # Seed from the first 14 changes, then apply Wilder smoothing.
            weight = min(index, period)
            gain = (gain * (weight - 1) + max(change, 0.0)) / weight
            loss = (loss * (weight - 1) + max(-change, 0.0)) / weight
        if len(closes) >= 100:
            rsi = 100 * gain / (gain + loss) if gain + loss else 50.0
            above_trend = candle.close > statistics.fmean(closes[-100:])
            invested = above_trend and (rsi < 55 if invested else rsi <= 40)
        yield invested


STRATEGIES = (
    Strategy("sma", "双均线趋势", "SMA20 高于 SMA100 持多，否则空仓。", sma_trend),
    Strategy(
        "donchian", "Donchian 通道突破",
        "收盘突破前 55 根最高价做多，跌破前 20 根最低价退出。", donchian_breakout,
    ),
    Strategy(
        "bollinger", "布林带均值回归",
        "收盘低于布林下轨做多，收盘回到中轨或更高时退出。周期 20，带宽为两倍总体标准差。",
        bollinger_reversion,
    ),
    Strategy(
        "rsi", "RSI 顺势回调",
        "收盘高于 SMA100 且 Wilder RSI(14) ≤ 40 时做多。RSI ≥ 55 或收盘不再高于 SMA100 时退出。",
        rsi_pullback,
    ),
    Strategy("hold", "买入持有（基准）", "首个开盘买入，回测结束时卖出。", buy_and_hold, True),
)
