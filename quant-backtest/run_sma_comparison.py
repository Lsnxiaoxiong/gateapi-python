from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from itertools import pairwise
from pathlib import Path

from experiments import CAPITAL, HOLDOUT_START, Portfolio
from market_data import DEFAULT_CSV, MINUTE_MS, Candle, load_candles, local_datetime, timestamp_text
from run_experiments import performance
from strategies import sma_side


HOUR_MS = 60 * MINUTE_MS
OUTPUT = Path(__file__).resolve().parent / "results"


@dataclass(frozen=True)
class Setup:
    hours: int
    fast: int
    slow: int
    allow_short: bool
    buy_and_hold: bool = False

    @property
    def key(self) -> str:
        if self.buy_and_hold:
            return "buy_hold"
        side = "long_short" if self.allow_short else "long_flat"
        return f"{self.hours}h_{self.fast}_{self.slow}_{side}"

    @property
    def name(self) -> str:
        if self.buy_and_hold:
            return "买入持有"
        side = "多空反手" if self.allow_short else "仅做多"
        return f"{self.hours}h SMA{self.fast}/{self.slow} · {side}"


@dataclass(frozen=True)
class Position:
    side: int
    opened: int
    price: float
    starting_equity: float


@dataclass(frozen=True)
class Trade:
    position: Position
    closed: int
    price: float
    ending_equity: float

    @property
    def net_pnl(self) -> float:
        return self.ending_equity - self.position.starting_equity


@dataclass
class Run:
    setup: Setup
    account: Portfolio = field(init=False)
    position: Position | None = None
    trades: list[Trade] = field(default_factory=list)
    max_short_adverse_move: float = 0.0

    def __post_init__(self) -> None:
        self.account = Portfolio(self.setup.name)

    def set_direction(self, side: int, time: int, price: float) -> None:
        current = self.position.side if self.position else 0
        if side == current:
            return
        if self.position is not None:
            self.account.adjust(price, 0)
            self.trades.append(Trade(self.position, time, price, self.account.cash))
            self.position = None
        if side:
            self.position = Position(side, time, price, self.account.cash)
            self.account.adjust(price, side)

    def check_short(self, price: float) -> None:
        if self.position is not None and self.position.side < 0:
            self.max_short_adverse_move = max(self.max_short_adverse_move, price / self.position.price - 1)
            if self.account.equity(price) <= 0:
                raise ValueError(f"{self.setup.key}: short reached zero equity at {price}")


def simulate(candles: list[Candle], setup: Setup) -> Run:
    run = Run(setup)
    run.account.mark(candles[0].open_time, candles[0].open)
    terminal = next((bar.last_trade for bar in reversed(candles) if bar.last_trade is not None), None)
    if terminal is None:
        raise ValueError("no tradable candles")
    closes: list[float] = []
    interval = setup.hours * HOUR_MS
    previous_bucket = None
    complete_hours = 0
    next_side = int(setup.buy_and_hold)

    for candle in candles:
        if candle.first_trade is not None:
            run.check_short(candle.first_trade[1])
            run.set_direction(next_side, *candle.first_trade)
        run.check_short(candle.high)

        if candle.last_trade == terminal:
            run.set_direction(0, *terminal)
            next_side = 0
        run.account.mark(candle.close_time, candle.close)
        if setup.buy_and_hold or candle.close_time >= terminal[0]:
            continue

        bucket = candle.open_time // interval
        if bucket != previous_bucket:
            complete_hours = 0
            previous_bucket = bucket
        complete_hours += candle.complete
        if complete_hours == setup.hours and (candle.close_time + 1) % interval == 0:
            closes.append(candle.close)
            side = sma_side(closes, setup.fast, setup.slow)
            next_side = side if setup.allow_short else max(0, side)
    return run


def summarize(run: Run) -> str:
    points = run.account.points
    total, drawdown = performance(points)
    split = next((index for index, point in enumerate(points) if point[0] >= HOLDOUT_START), None)
    recent_text = "— | —"
    if split is not None:
        recent, recent_drawdown = performance(points[max(split - 1, 0):])
        recent_text = f"{recent:+.2%} | {recent_drawdown:.2%}"
    longs = sum(trade.position.side > 0 for trade in run.trades)
    return (
        f"| {run.setup.name} | {total:+.2%} | {drawdown:.2%} | {recent_text} | "
        f"{longs:,} | {len(run.trades) - longs:,} | {run.account.fees:,.2f} |"
    )


def annual_returns(run: Run) -> dict[int, float]:
    closes = {local_datetime(time).year: value for time, value, _ in run.account.points[1:]}
    values = [run.account.points[0][1], *closes.values()]
    return {
        year: close / start - 1
        for year, (start, close) in zip(closes, pairwise(values), strict=True)
    }


def load_market(files: list[Path], end: int | None) -> list[Candle]:
    candles: list[Candle] = []
    for file in files:
        segment = load_candles(file, end=end, bar_minutes=60)
        if candles and segment and segment[0].open_time // HOUR_MS <= candles[-1].open_time // HOUR_MS:
            raise ValueError(f"duplicate or out-of-order hourly bucket in {file}")
        candles.extend(segment)
    if not candles:
        raise ValueError("no market candles")
    return candles


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare SMA timeframes and long/short rules.")
    parser.add_argument("--csv", type=Path, action="append", help="chronological CSV, repeat for more segments")
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), default="BTCUSDT")
    parser.add_argument("--end", type=date.fromisoformat, help="exclusive UTC date")
    parser.add_argument("--with-hold", action="store_true", help="include buy-and-hold")
    args = parser.parse_args()
    files = args.csv or [DEFAULT_CSV]
    if args.symbol != "BTCUSDT" and not args.csv:
        parser.error("--csv is required for ETHUSDT")
    cutoff = int(datetime.combine(args.end, time(), timezone.utc).timestamp() * 1000) if args.end else None
    candles = load_market(files, cutoff)
    runs = [simulate(candles, Setup(hours, fast, slow, allow_short))
            for hours, fast, slow in ((4, 20, 100), (1, 20, 100), (1, 80, 400))
            for allow_short in (False, True)]
    if args.with_hold:
        runs.append(simulate(candles, Setup(4, 20, 100, False, buy_and_hold=True)))
    for run in runs:
        assert math.isclose(CAPITAL + sum(t.net_pnl for t in run.trades), run.account.cash)
    minutes = sum(candle.minutes for candle in candles)
    expected_minutes = (candles[-1].close_time - candles[0].open_time + 1) // MINUTE_MS
    lines = [
        f"# {args.symbol} 双均线多空对照",
        "",
        f"每组本金 10,000 USDT。行情覆盖 {timestamp_text(candles[0].open_time)[:16]} 至 "
        f"{timestamp_text(candles[-1].close_time)[:16]}（北京时间）。",
        f"Binance USDT 本位永续 1 分钟行情，共 {minutes:,} 根，缺失 {expected_minutes - minutes:,} 根。"
        f"数据文件为 {'、'.join(file.name for file in files)}。",
        "",
        "| 周期和模式 | 全区间收益 | 最大回撤 | 2024 年起收益 | 2024 年起回撤 | 完整多单 | 完整空单 | 手续费 USDT |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *(summarize(run) for run in runs),
        "",
        "## 空头交易",
        "",
        "| 周期与参数 | 空单胜率 | 空单净盈亏合计 USDT | 空单收益逐笔复乘 | 空单期间最大逆向价格涨幅 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for run in runs:
        shorts = [trade for trade in run.trades if trade.position.side < 0]
        if shorts:
            wins = sum(trade.net_pnl > 0 for trade in shorts)
            compounded = math.prod(trade.ending_equity / trade.position.starting_equity for trade in shorts) - 1
            lines.append(
                f"| {run.setup.name} | {wins / len(shorts):.2%} | "
                f"{sum(trade.net_pnl for trade in shorts):+,.2f} | "
                f"{compounded:+.2%} | {run.max_short_adverse_move:.2%} |"
            )
    annual = [annual_returns(run) for run in runs]
    lines.extend([
        "",
        "## 分年收益",
        "",
        "| 年份 | " + " | ".join(run.setup.name for run in runs) + " |",
        "| --- | " + " | ".join(["---:"] * len(runs)) + " |",
        *(f"| {year} | " + " | ".join(f"{returns[year]:+.2%}" for returns in annual) + " |"
          for year in annual[0]),
    ])
    lines.extend([
        "",
        "## 回测口径",
        "",
        "- 快线高于慢线持多。快线低于慢线时，仅做多组空仓，多空组反手做空。"
        "两线相等时空仓。预热结束后按首个可用信号决定方向。",
        "- 收盘确认，之后首个有成交分钟开盘撮合。反手先平仓再开仓，两笔分别计费。"
        "名义仓位在每次开仓时按 1 倍权益计算，持仓期间数量固定。期末按最后一个有成交分钟收盘价结算。",
        "- 每边手续费 0.05%，每边固定不利滑点 0.02%。空头通过线性合约损益模拟。"
        "资金费率和交易所强平机制未建模。逐小时检查空头在行情最高价下的权益，全部测试未触及零。",
        "- 每组回撤统一按 1 小时收盘权益计算。盘中回撤可能更大。2024 年起按 2024-01-01 00:00 UTC 切分连续权益。",
        "- 分年使用北京时间，首尾年份按实际数据范围统计。空单复乘仅累计做空交易的净收益率。",
        "- 1h SMA80/400 和 4h SMA20/100 覆盖同样的 80/400 小时，所用收盘价及信号更新频率不同。",
        "",
    ])
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stem = "sma_comparison" if args.symbol == "BTCUSDT" else f"{args.symbol.lower()}_sma_comparison"
    (OUTPUT / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")
    with (OUTPUT / f"{stem}_equity.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["time", *(run.setup.key for run in runs)])
        for points in zip(*(run.account.points for run in runs), strict=True):
            writer.writerow([timestamp_text(points[0][0]), *(round(point[1], 8) for point in points)])
    for run in runs:
        total, drawdown = performance(run.account.points)
        print(f"{run.setup.key:24} {total:+10.2%}  MDD {drawdown:.2%}")


if __name__ == "__main__":
    main()
