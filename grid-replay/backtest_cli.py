from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest_engine import BacktestConfig, GridBacktester, iter_normalized_rows
from csv_index import CsvIndex
from replay_paths import DEFAULT_CSV_PATH as CSV_PATH


def parse_timezone(value: str) -> timezone:
    text = value.strip()
    if text.upper() == "UTC" or text == "Z":
        return timezone.utc
    if len(text) != 6 or text[0] not in "+-" or text[3] != ":":
        raise argparse.ArgumentTypeError("时区格式必须类似 +08:00、-05:00 或 UTC")
    try:
        hours = int(text[1:3])
        minutes = int(text[4:6])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("时区格式必须类似 +08:00") from exc
    if hours > 23 or minutes > 59:
        raise argparse.ArgumentTypeError("无效时区偏移")
    sign = 1 if text[0] == "+" else -1
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def parse_datetime_ms(value: str | None, tz: timezone) -> int | None:
    if not value:
        return None
    text = value.strip().replace("/", "-")
    dt = None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"无法解析时间: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp() * 1000)


def fmt_time(ts_ms: int, tz: timezone) -> str:
    if not ts_ms:
        return "--"
    return datetime.fromtimestamp(ts_ms / 1000, tz=tz).strftime("%Y-%m-%d %H:%M:%S")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="1m 做多网格策略无 UI 回测。严格逐根 1m K 线执行交易逻辑，只输出最终统计。"
    )
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help="1m K线 CSV 路径")
    parser.add_argument("--output-json", type=Path, help="将参数和统计结果保存为 JSON")
    parser.add_argument("--start", default=None, help="开始时间，例如 '2020-01-01 08:00'")
    parser.add_argument("--end", default=None, help="结束时间；不填则跑到数据末尾")
    parser.add_argument("--timezone", default="+08:00", help="无时区时间的解释时区，默认 +08:00")

    parser.add_argument("--reference-price", type=float, default=55555.0, help="网格参考价，默认 55555 USDT；传 0 可自动使用起点开盘价")
    parser.add_argument("--margin-per-grid", type=float, default=50.0, help="每格保证金 USDT，默认 50")
    parser.add_argument("--leverage", type=float, default=2.0, help="杠杆")
    parser.add_argument("--grid-step-pct", type=float, default=0.3, help="网格间距百分比，例如 0.3 表示 0.3%%")
    parser.add_argument("--take-profit-pct", type=float, default=0.3, help="止盈比例百分比")
    parser.add_argument("--buy-grid-count", type=int, default=10, help="现价下方维护的买格数量")
    parser.add_argument("--take-profit-count", type=int, default=10, help="最多活动止盈单数量")
    parser.add_argument("--take-profit-grid-count", type=int, default=20, help="止盈检查网格范围")
    parser.add_argument("--max-unclosed-per-grid", type=int, default=2, help="单格最大未平仓周期")
    parser.add_argument("--price-tick", type=float, default=0.1, help="价格精度/步长")
    parser.add_argument("--qty-step", type=float, default=0.001, help="数量精度/步长")
    parser.add_argument("--fee-pct", type=float, default=0.05, help="单边手续费百分比")
    parser.add_argument("--progress-every", type=int, default=100_000, help="每处理 N 根 1m 输出一次进度；默认 100000，传 0 可关闭")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    positive = {
        "margin-per-grid": args.margin_per_grid,
        "leverage": args.leverage,
        "grid-step-pct": args.grid_step_pct,
        "take-profit-pct": args.take_profit_pct,
        "buy-grid-count": args.buy_grid_count,
        "take-profit-count": args.take_profit_count,
        "take-profit-grid-count": args.take_profit_grid_count,
        "max-unclosed-per-grid": args.max_unclosed_per_grid,
        "price-tick": args.price_tick,
        "qty-step": args.qty_step,
    }
    for name, value in positive.items():
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError(f"--{name} 必须大于 0")
    if args.reference_price < 0:
        raise ValueError("--reference-price 不能小于 0")
    if args.fee_pct < 0:
        raise ValueError("--fee-pct 不能小于 0")


def run(args: argparse.Namespace) -> int:
    validate_args(args)
    tz = parse_timezone(args.timezone)
    csv_path = args.csv.resolve()
    if not csv_path.is_file():
        raise FileNotFoundError(f"K线文件不存在: {csv_path}")

    index = CsvIndex(csv_path)
    start_ms = parse_datetime_ms(args.start, tz) or index.first_time
    end_ms = parse_datetime_ms(args.end, tz) if args.end else index.last_time + 60_000
    if end_ms is not None and end_ms <= start_ms:
        raise ValueError("结束时间必须晚于开始时间")

    start_ms = max(start_ms, index.first_time)
    end_ms = min(end_ms or (index.last_time + 60_000), index.last_time + 60_000)
    start_offset = index.row_for_timestamp(start_ms)
    end_offset = index.total_rows if end_ms > index.last_time else index.row_for_timestamp(end_ms)

    payload = {
        "displayInterval": "1h",
        "referencePrice": args.reference_price,
        "marginPerGrid": args.margin_per_grid,
        "leverage": args.leverage,
        "gridStepPct": args.grid_step_pct,
        "takeProfitPct": args.take_profit_pct,
        "buyGridCount": args.buy_grid_count,
        "takeProfitCount": args.take_profit_count,
        "takeProfitGridCount": args.take_profit_grid_count,
        "maxUnclosedPerGrid": args.max_unclosed_per_grid,
        "priceTick": args.price_tick,
        "qtyStep": args.qty_step,
        "feePct": args.fee_pct,
        "frameLimit": 1,
    }
    cfg = BacktestConfig.from_payload(payload, start_offset)
    rows = iter_normalized_rows(index, start_offset)
    try:
        first = next(rows)
    except StopIteration:
        raise RuntimeError("指定范围内没有 K 线数据")

    if int(first[0]) >= end_ms:
        raise RuntimeError("指定范围内没有 K 线数据")
    if cfg.reference_price <= 0:
        cfg.reference_price = float(first[1])

    engine = GridBacktester(cfg)
    engine.current_time = int(first[0])
    engine.current_price = float(first[1])
    engine.reconcile_buys(engine.current_price)
    engine.reconcile_take_profits(engine.current_price)
    engine.update_statistics()

    processed = 0
    started_at = time.perf_counter()
    row = first
    while int(row[0]) < end_ms:
        engine.process_minute(row)
        processed += 1
        if args.progress_every > 0 and processed % args.progress_every == 0:
            elapsed_now = time.perf_counter() - started_at
            speed_now = processed / elapsed_now if elapsed_now > 0 else 0
            progress_pct = min(100.0, processed / max(1, end_offset - start_offset) * 100)
            print(
                f"进度 {progress_pct:6.2f}% | {processed:,}/{max(1, end_offset - start_offset):,} 根 1m | "
                f"{fmt_time(engine.current_time, tz)} | 持仓 {len(engine.positions)} | "
                f"已实现 {engine.realized_pnl:+,.2f} USDT | {speed_now:,.0f} 根/秒",
                file=sys.stderr,
                flush=True,
            )
        try:
            row = next(rows)
        except StopIteration:
            break

    elapsed = time.perf_counter() - started_at
    unrealized = engine.unrealized_pnl()
    net_pnl = engine.realized_pnl + unrealized
    final_margin = len(engine.positions) * cfg.margin_per_grid
    final_exposure = engine.notional_exposure()

    if args.output_json:
        result = {
            "csv": str(csv_path),
            "start_time_ms": int(first[0]),
            "last_open_time_ms": engine.current_time,
            "processed_minutes": processed,
            "elapsed_seconds": elapsed,
            "parameters": payload,
            "effective_reference_price": cfg.reference_price,
            "final_price": engine.current_price,
            "realized_pnl": engine.realized_pnl,
            "unrealized_pnl": unrealized,
            "net_pnl": net_pnl,
            "total_fees": engine.total_fees,
            "completed_cycles": engine.completed_count,
            "final_positions": len(engine.positions),
            "final_margin_used": final_margin,
            "final_notional_exposure": final_exposure,
            "max_positions": engine.max_positions,
            "max_margin_used": engine.max_margin_used,
            "max_notional_exposure": engine.max_notional_exposure,
            "max_drawdown": engine.max_drawdown,
            "model_limits": ["No capital limit or liquidation model", "No funding fees or slippage", "Take profits processed before buys; new positions cannot close in the same minute", "Drawdown sampled at minute close"],
        }
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 72)
    print("网格回测结果")
    print("=" * 72)
    print(f"数据文件          : {csv_path}")
    print(f"开始时间          : {fmt_time(int(first[0]), tz)}")
    print(f"结束时间          : {fmt_time(engine.current_time, tz)}")
    print(f"处理 1m K线       : {processed:,} 根")
    print(f"计算耗时          : {elapsed:,.3f} 秒")
    print(f"处理速度          : {(processed / elapsed if elapsed else 0):,.0f} 根/秒")
    print("-" * 72)
    print(f"参考价格          : {cfg.reference_price:,.2f} USDT")
    print(f"最终价格          : {engine.current_price:,.2f} USDT")
    print(f"每格保证金        : {cfg.margin_per_grid:,.2f} USDT")
    print(f"杠杆              : {cfg.leverage:g}x")
    print(f"网格间距          : {cfg.grid_step_pct:g}%")
    print(f"止盈比例          : {cfg.take_profit_pct:g}%")
    print("-" * 72)
    print(f"已实现盈亏        : {engine.realized_pnl:+,.2f} USDT")
    print(f"最终浮动盈亏      : {unrealized:+,.2f} USDT")
    print(f"最终总盈亏        : {net_pnl:+,.2f} USDT")
    print(f"累计手续费        : {engine.total_fees:,.2f} USDT")
    print(f"完成网格周期      : {engine.completed_count:,}")
    print("-" * 72)
    print(f"最终持仓数        : {len(engine.positions):,}")
    print(f"最终挂买单数      : {len(engine.pending_buys):,}")
    print(f"最终保证金占用    : {final_margin:,.2f} USDT")
    print(f"最终名义敞口      : {final_exposure:,.2f} USDT")
    print(f"最大同时持仓数    : {engine.max_positions:,}")
    print(f"最大保证金占用    : {engine.max_margin_used:,.2f} USDT")
    print(f"最大名义敞口      : {engine.max_notional_exposure:,.2f} USDT")
    print(f"最大回撤          : {engine.max_drawdown:,.2f} USDT")
    print("=" * 72)
    return 0


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:
        print(f"错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
