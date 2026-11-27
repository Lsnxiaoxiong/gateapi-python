from __future__ import annotations

import csv
import statistics
from pathlib import Path

from experiments import CAPITAL, HOLDOUT_START, Portfolio, run_minute_experiments, run_volatility_experiment
from market_data import DEFAULT_CSV, load_candles, timestamp_text


OUTPUT = Path(__file__).resolve().parent / "results"


def performance(points: list[tuple[int, float, float]]) -> tuple[float, float]:
    peak = points[0][1]
    drawdown = 0.0
    for _, equity, _ in points:
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    return points[-1][1] / points[0][1] - 1, drawdown


def performance_row(account: Portfolio, report_exposure: bool) -> str:
    early_end = next(index for index, point in enumerate(account.points) if point[0] >= HOLDOUT_START)
    total, drawdown = performance(account.points)
    recent, recent_drawdown = performance(account.points[early_end - 1:])
    exposure_text = "— | —"
    if report_exposure:
        exposures = [weight for _, _, weight in account.points[1:]]
        exposure_text = f"{statistics.fmean(exposures):.2%} | {max(exposures):.2%}"
    return (
        f"| {account.name} | {total:+.2%} | {drawdown:.2%} | {recent:+.2%} | "
        f"{recent_drawdown:.2%} | {exposure_text} | "
        f"{account.fills:,} | {account.fees:,.2f} |"
    )


def main() -> None:
    print("Running minute grid and intraday comparisons", flush=True)
    grids, intraday = run_minute_experiments(DEFAULT_CSV)
    print("Running volatility sizing comparisons", flush=True)
    candles = load_candles(DEFAULT_CSV, end=None)
    sizing, matched = run_volatility_experiment(candles)
    portfolios = [*(grid.account for grid in grids), *sizing, intraday.signal, intraday.control]
    for grid in grids:
        total_pnl = grid.account.points[-1][1] - CAPITAL
        assert abs(total_pnl - grid.grid_pnl - grid.reset_pnl) < 1e-6

    lines = [
        "# BTCUSDT 策略实验",
        "",
        f"每个策略从 {CAPITAL:,.0f} USDT 独立起步，使用 Binance BTCUSDT 永续 1 分钟行情。"
        f"数据起止为 {timestamp_text(candles[0].open_time)[:16]} 至 "
        f"{timestamp_text(candles[-1].close_time)[:16]}（北京时间）。",
        "市价单每边手续费 0.05%、不利滑点 0.02%，网格限价单每边手续费 0.05%，按挂单价成交。"
        "模拟仅做多或空仓，未计资金费率、交易精度和部分成交。",
        "",
        "## 绩效对比",
        "",
        "2024 年起的列取连续净值在 2024-01-01 00:00 UTC 之后的子区间。"
        "权益、回撤和持仓比例都按 4 小时收盘采样，实际盘中风险可能更高。"
        "日内策略的持仓窗口位于采样点之间，持仓比例省略。"
        "成交次数统计单边成交，网格批量平仓记作一次。",
        "",
        "| 策略 | 全区间收益 | 全区间回撤 | 2024 起收益 | 2024 起回撤 | 平均持仓 | 峰值持仓 | 成交次数 | 手续费 USDT |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *(performance_row(account, True) for account in portfolios[:-2]),
        *(performance_row(account, False) for account in portfolios[-2:]),
        "",
        "## 网格盈亏拆解",
        "",
        "| 方案 | 月内重置次数 | 限价完成配对 | 配对净利润 USDT | 强制平仓净盈亏 USDT | 总净盈亏 USDT |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        *(
            f"| {grid.account.name} | {grid.resets:,} | {grid.closed_pairs:,} | "
            f"{grid.grid_pnl:+,.2f} | {grid.reset_pnl:+,.2f} | {grid.account.points[-1][1] - CAPITAL:+,.2f} |"
            for grid in grids
        ),
        "",
        "## 实验规则",
        "",
        "- **固定与动态网格**。每个 UTC 月首个有成交分钟清理上月库存，并以开盘价设中心。"
        "中心下方挂 10 档买单，相邻档距 1%，每档预算为月初权益的十分之一。"
        "每档成交后在高一格挂止盈。动态网格额外在收盘跌出下边界或涨过对称上边界后，"
        "于下个有成交分钟开盘平库存并重设中心。月内重设保留本月每档预算。"
        "重设后的限价单从下一分钟起生效。限价须被穿过才成交，同一档同一分钟最多成交一次，"
        "当分钟卖出资金从下一分钟起可用。"
        "强制平仓净盈亏包含月末换网格、月内重置和回测期末平仓，已扣开平仓成本。",
        "- **波动率目标仓位**。以 4 小时 SMA20/100 决定是否持多。若持多，使用过去 180 根 4 小时对数收益估计年化波动率，"
        "目标仓位为 40% 除以估计波动率，上限 100%。波动率预热期沿用 100% 仓位。"
        "仅在开平仓信号和 UTC 日线收盘之后调仓。"
        f"固定仓位控制组在多头状态下持 {matched:.2%}，来自 2020–2023 多头状态的目标仓位按时间加权平均。"
        "该权重从 2024 起冻结，固定仓位组的全区间结果含校准期。",
        "- **日内动量**。UTC 13:30–14:00 的首个有效开盘至最后有效收盘上涨，"
        "则于 UTC 20:30–21:00 的首个有成交分钟开盘做多，21:00 起首个有成交分钟开盘退出。"
        "对照组每天在同一时段始终做多。固定窗口为待验证的本地变体。",
        "- **零成交量**。市价单以之后首个满足规则且有成交的分钟开盘价撮合，零成交量分钟不触发限价成交。"
        "持仓在回测末尾按最后一个有成交分钟的收盘价结算。",
        "",
    ]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "experiments.md").write_text("\n".join(lines), encoding="utf-8")
    with (OUTPUT / "experiments_equity.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["time", *(account.name for account in portfolios)])
        for points in zip(*(account.points for account in portfolios), strict=True):
            assert all(point[0] == points[0][0] for point in points)
            writer.writerow([timestamp_text(points[0][0]), *(round(point[1], 8) for point in points)])
    for index, account in enumerate(portfolios, start=1):
        total, drawdown = performance(account.points)
        print(f"{index}. Return {total:+.2%}, drawdown {drawdown:.2%}")
    print(f"Report saved to {OUTPUT / 'experiments.md'}")


if __name__ == "__main__":
    main()
