from __future__ import annotations

import csv
import math
from bisect import bisect_left
from collections.abc import Iterator
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

from engine import run_strategy
from experiments import CAPITAL, HOLDOUT_START, MARKET_COSTS
from market_data import BAR_MS, DEFAULT_CSV, Candle, load_candles, timestamp_text
from strategies import Strategy


DISTANCES_BP = (0, 5, 10, 20, 30, 50, 75, 100, 150, 200)
END = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1000)
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path(__file__).resolve().parent / "results"
ETH_DIR = ROOT / "grid-replay" / "data" / "ETHUSDT" / "1m"
MARKETS = {
    "ETHUSDT": (
        ETH_DIR / "ETHUSDT-1m-2020-01-01_to_2021-09-03.csv",
        ETH_DIR / "ETHUSDT-1m-2021-09-04_to_2026-09-03.csv",
    ),
    "BTCUSDT": (DEFAULT_CSV,),
}


def sma_gaps(candles: list[Candle]) -> list[float | None]:
    closes: list[float] = []
    gaps: list[float | None] = []
    for bar in candles:
        if not bar.complete:
            continue
        closes.append(bar.close)
        if len(closes) < 100:
            gaps.append(None)
        else:
            fast = sum(closes[-20:]) * 5
            slow = sum(closes[-100:])
            gaps.append((fast - slow) / slow)
    return gaps


def early_targets(
    gaps: list[float | None], entry_distance: float, exit_distance: float,
) -> Iterator[bool]:
    position = False
    previous = None
    for gap in gaps:
        if gap is None:
            yield False
            continue
        if previous is None or gap == 0 or previous == 0 or (gap > 0) != (previous > 0):
            position = gap > 0
        elif gap < 0:
            if gap < -entry_distance:
                position = False
            elif gap > previous:
                position = True
        elif gap > exit_distance:
            position = True
        elif gap < previous:
            position = False
        previous = gap
        yield position


def signal_available_at_open(candles: list[Candle], gaps: list[float | None]) -> dict[int, float | None]:
    gap_iter = iter(gaps)
    latest = None
    available = {}
    for bar in candles:
        if bar.first_trade is not None:
            available[bar.first_trade[0]] = latest
        if bar.complete:
            latest = next(gap_iter)
    return available


def drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for equity in values:
        peak = max(peak, equity)
        worst = max(worst, 1 - equity / peak)
    return worst


@dataclass(frozen=True)
class Metrics:
    symbol: str
    entry_bp: int
    exit_bp: int
    return_full: float
    drawdown_full: float
    return_through_2023: float
    return_since_2024: float
    drawdown_since_2024: float
    trades: int
    early_entries: int
    early_exits: int
    fees_usdt: float


def sweep(symbol: str, candles: list[Candle]) -> list[Metrics]:
    gaps = sma_gaps(candles)
    available = signal_available_at_open(candles, gaps)
    # Equity starts with a separate initial point before the first bar close.
    split = bisect_left([bar.close_time for bar in candles], HOLDOUT_START)
    results = []
    for entry, exit_ in product(DISTANCES_BP, repeat=2):
        strategy = Strategy(
            f"{entry}_{exit_}", "SMA20/100", "",
            lambda _, entry=entry, exit_=exit_: early_targets(gaps, entry / 10_000, exit_ / 10_000),
        )
        run = run_strategy(strategy, candles, 0, CAPITAL, MARKET_COSTS)
        equity = [value for _, value in run.equity]
        early_entries = sum((available[trade.entry.time] or 0) < 0 for trade in run.trades)
        early_exits = sum(
            trade.reason == "signal" and (available[trade.exit_time] or 0) > 0 for trade in run.trades
        )
        total_fees = math.fsum(trade.fees for trade in run.trades)
        results.append(Metrics(
            symbol, entry, exit_, run.total_return, run.max_drawdown,
            equity[split] / equity[0] - 1, equity[-1] / equity[split] - 1,
            drawdown(equity[split:]), len(run.trades), early_entries, early_exits, total_fees,
        ))
    return results


def metric_row(row: Metrics) -> str:
    return (
        f"| {row.entry_bp / 100:.2f}% | {row.exit_bp / 100:.2f}% | {row.return_full:+.2%} | "
        f"{row.drawdown_full:.2%} | {row.return_through_2023:+.2%} | {row.return_since_2024:+.2%} | "
        f"{row.drawdown_since_2024:.2%} | {row.trades} | {row.early_entries} / {row.early_exits} |"
    )


def write_report(groups: dict[str, list[Metrics]], start: int, end: int) -> None:
    columns = (
        "| 开仓阈值 | 平仓阈值 | 全程收益 | 最大回撤 | 截至 2023 年收益 | 2024 年起收益 | "
        "2024 年起回撤 | 完整交易 | 提前开 / 平仓次数 |"
    )
    separators = "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    lines = [
        "# 4h SMA20/100 提前交叉回测",
        "",
        f"区间为 {timestamp_text(start)[:16]} 至 {timestamp_text(end)[:16]}（北京时间）。"
        "每个方案独立使用 10,000 USDT。仅做多或空仓。",
        "",
        "- 距离为 `SMA20 / SMA100 - 1`。快线仍在慢线下方且距离比上一根收盘缩小时，"
        "进入开仓阈值带就提前开多。快线仍在慢线上方且距离缩小时，进入平仓阈值带就提前平多。",
        "- 提前操作后，若两线尚未交叉就反向拉开到相应阈值带之外，则恢复正式交叉规则对应的仓位。"
        "正式交叉时直接按快慢线位置定仓位。0% 代表该侧只等正式交叉。",
        "- 收盘确认后以首个有成交的分钟开盘价撮合，单边手续费 0.05%、滑点 0.02%。"
        "期末按最后成交分钟收盘价结算，未计资金费率。回撤以 4 小时收盘权益采样。",
        "- 2024 年起使用 UTC 2024-01-01 前最后一条权益为分段基准，沿用连续回测仓位。"
        "提前次数按成交前最后一条已知均线的方向统计，期末结算不计为提前平仓。",
        "",
    ]
    for symbol, results in groups.items():
        symmetric = [row for row in results if row.entry_bp == row.exit_bp]
        lines.extend([f"## {symbol}", "", columns, separators, *(metric_row(row) for row in symmetric), ""])
        winners = (
            ("全程收益最高", max(results, key=lambda row: row.return_full)),
            ("按截至 2023 年收益选出", max(results, key=lambda row: row.return_through_2023)),
            ("2024 年起收益最高", max(results, key=lambda row: row.return_since_2024)),
        )
        for label, row in winners:
            lines.append(
                f"- {label}。开仓 {row.entry_bp / 100:.2f}%，平仓 {row.exit_bp / 100:.2f}%，"
                f"全程 {row.return_full:+.2%}，2024 年起 {row.return_since_2024:+.2%}。"
            )
        lines.append("")
    lines.extend([
        "CSV 包含两个资产各 100 组非对称及对称组合。列名含 `return` 或 `drawdown` 的值用小数表示，0.10 对应 10%。",
        "",
    ])
    (OUTPUT / "sma_early_entry_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    groups = {}
    common_times = None
    for symbol, paths in MARKETS.items():
        print(f"Loading {symbol}", flush=True)
        candles = [bar for path in paths for bar in load_candles(path, end=END)]
        times = [(bar.open_time, bar.close_time) for bar in candles]
        if not times or not all(bar.complete for bar in candles) or any(
            later[0] - earlier[0] != BAR_MS for earlier, later in zip(times, times[1:])
        ) or times[-1][1] + 1 != END:
            raise ValueError(f"unexpected date coverage or gaps in {symbol}")
        if common_times is not None and times != common_times:
            raise ValueError("ETH and BTC do not cover identical periods")
        common_times = times
        groups[symbol] = sweep(symbol, candles)
        print(f"{symbol}: {len(groups[symbol])} parameter combinations", flush=True)

    with (OUTPUT / "sma_early_entry_results.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=[field.name for field in fields(Metrics)])
        writer.writeheader()
        for results in groups.values():
            writer.writerows(asdict(row) for row in results)
    write_report(groups, common_times[0][0], common_times[-1][1])


if __name__ == "__main__":
    main()
