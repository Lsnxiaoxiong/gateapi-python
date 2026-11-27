from __future__ import annotations

import argparse
import csv
import math
from datetime import date, datetime, time
from pathlib import Path

from engine import Costs, Result, run_strategy
from market_data import (
    BAR_MS, DEFAULT_CSV, DISPLAY_TZ, MINUTE_MS, Candle, load_candles, timestamp_text,
)
from strategies import STRATEGIES


DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results"


def boundary(day: date | None) -> int | None:
    if day is None:
        return None
    return int(datetime.combine(day, time(), tzinfo=DISPLAY_TZ).timestamp() * 1000)


def write_csvs(output: Path, results: list[Result]) -> None:
    with (output / "equity.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["time", *(result.strategy.key for result in results)])
        for points in zip(*(result.equity for result in results), strict=True):
            writer.writerow([timestamp_text(points[0][0]), *(round(value, 8) for _, value in points)])

    with (output / "trades.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([
            "strategy", "entry_time", "exit_time", "btc_quantity", "entry_market_price",
            "entry_fill_price", "exit_market_price", "exit_fill_price", "entry_fee_usdt",
            "exit_fee_usdt", "slippage_usdt", "net_pnl_usdt", "net_return_pct", "exit_reason",
        ])
        for result in results:
            for trade in result.trades:
                entry = trade.entry
                writer.writerow([
                    result.strategy.key, timestamp_text(entry.time), timestamp_text(trade.exit_time),
                    entry.quantity, entry.market_price, entry.fill_price, trade.exit_market_price,
                    trade.exit_fill_price, entry.fee, trade.exit_fee, trade.slippage, trade.net_pnl,
                    trade.net_pnl / entry.starting_cash * 100, trade.reason,
                ])


def summary_row(result: Result) -> str:
    trades = result.trades
    sharpe = result.sharpe
    sharpe_text = "—" if sharpe is None else f"{sharpe:.2f}"
    win_rate = f"{sum(trade.net_pnl > 0 for trade in trades) / len(trades):.1%}" if trades else "—"
    fees = sum(trade.fees for trade in trades)
    slippage = sum(trade.slippage for trade in trades)
    return (
        f"| {result.strategy.name} | {result.equity[-1][1]:,.2f} | {result.total_return:+.2%} | "
        f"{result.annualized_return:+.2%} | {result.max_drawdown:.2%} | {sharpe_text} | "
        f"{len(trades):,} | {win_rate} | {fees:,.2f} | {slippage:,.2f} |"
    )


def make_report(path: Path, candles: list[Candle], results: list[Result], costs: Costs) -> str:
    first, last = candles[0], candles[-1]
    observed_minutes = sum(candle.minutes for candle in candles)
    expected_minutes = (last.close_time - first.open_time + 1) // MINUTE_MS
    expected_bars = last.open_time // BAR_MS - first.open_time // BAR_MS + 1
    partial = sum(not candle.complete for candle in candles)
    omitted = expected_bars - len(candles)
    start = timestamp_text(first.open_time)[:16].replace("T", " ")
    end = timestamp_text(last.close_time)[:16].replace("T", " ")
    capital = results[0].equity[0][1]
    lines = [
        "# BTCUSDT 策略回测",
        "",
        f"- 行情文件 `{path.name}`。评估区间为 {start} 至 {end}（北京时间）。",
        f"- 区间内读取 {observed_minutes:,} 根 1 分钟 K 线，时间轴缺 {expected_minutes - observed_minutes:,} 分钟。"
        f"共 {len(candles):,} 个有行情的 4 小时窗口，其中 {partial:,} 个不完整，另有 {omitted:,} 个全空窗口。",
        f"- 每个策略初始资金 {capital:,.2f} USDT，持仓时投入可用资金，仓位为 1 倍名义本金。"
        f"单边手续费 {costs.fee:.4%}，单边不利滑点 {costs.slippage:.4%}。",
        "",
        "## 全区间绩效",
        "",
        "| 策略 | 期末权益 USDT | 总收益 | 年化收益 | 最大回撤 | 日收益夏普 | 完整交易 | 胜率 | 手续费 USDT | 滑点 USDT |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *(summary_row(result) for result in results),
        "",
        "## 分年收益",
        "",
        "按北京时间分年，以连续运行的权益计算。首尾年份按实际覆盖日期统计。",
        "",
        "| 年份 | " + " | ".join(result.strategy.name for result in results) + " |",
        "| --- | " + " | ".join(["---:"] * len(results)) + " |",
    ]
    annual = [result.annual_returns for result in results]
    lines.extend(
        f"| {year} | " + " | ".join(f"{values[year]:+.2%}" for values in annual) + " |"
        for year in annual[0]
    )
    lines.extend([
        "",
        "## 策略规则",
        "",
        *(f"- **{result.strategy.name}**。{result.strategy.rule}" for result in results),
        "",
        "## 成交与统计口径",
        "",
        "- 以 1 分钟行情合成固定边界的 4 小时 K 线。指标使用完整 K 线，缺分钟的窗口仅用于成交和估值。"
        "收盘生成目标仓位，之后首个有成交的分钟 K 线开盘撮合；等待期间以最新信号为准。"
        "期末按最后一个有成交分钟的收盘价模拟结算。",
        "- 每笔交易包含一开一平，胜率按费用和滑点后的净盈亏计算。"
        "最大回撤按 4 小时权益采样计算，窗口内的下探可能更大。夏普使用北京时间的日收益，零无风险利率和 365 天年化。",
        "- 模拟仅覆盖多头和空仓。费用与滑点为固定假设，资金费率、强平、下单精度与流动性限制均未建模。"
        "规则参数固定，并在同一段历史行情中比较。",
        "- `equity.csv` 是逐窗口权益，首行为起始本金。`trades.csv` 含每笔开平仓、成本及收益，`signal` 表示信号退出，`end` 表示期末结算。",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest four BTCUSDT strategies and buy-and-hold.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", type=date.fromisoformat, help="first date in UTC+8, inclusive")
    parser.add_argument("--end", type=date.fromisoformat, help="end date in UTC+8, exclusive")
    parser.add_argument("--capital", type=float, default=10_000, help="initial USDT per strategy")
    parser.add_argument("--fee-bps", type=float, default=5, help="one-way fee in basis points")
    parser.add_argument("--slippage-bps", type=float, default=2, help="one-way slippage in basis points")
    args = parser.parse_args()

    if not math.isfinite(args.capital) or args.capital <= 0:
        parser.error("--capital must be finite and positive")
    if not all(0 <= value < 10_000 for value in (args.fee_bps, args.slippage_bps)):
        parser.error("fee and slippage must each be in [0, 10000) basis points")
    start, end = boundary(args.start), boundary(args.end)
    if start is not None and end is not None and start >= end:
        parser.error("--start must precede --end")

    try:
        candles = load_candles(args.csv, end)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    first_index = next(
        (index for index, candle in enumerate(candles) if start is None or candle.open_time >= start),
        None,
    )
    if first_index is None:
        parser.error("the selected date range contains no market data")

    costs = Costs(args.fee_bps / 10_000, args.slippage_bps / 10_000)
    results = [
        run_strategy(strategy, candles, first_index, args.capital, costs) for strategy in STRATEGIES
    ]
    report = make_report(args.csv, candles[first_index:], results, costs)
    args.output.mkdir(parents=True, exist_ok=True)
    write_csvs(args.output, results)
    (args.output / "report.md").write_text(report, encoding="utf-8")
    print(f"Results saved to {args.output.resolve()}")
    for result in results:
        print(f"{result.strategy.key:10} {result.total_return:+9.2%}  MDD {result.max_drawdown:7.2%}")


if __name__ == "__main__":
    main()
