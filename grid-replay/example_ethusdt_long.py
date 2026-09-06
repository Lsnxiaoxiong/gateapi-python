"""ETHUSDT 五年逐分钟做多网格示例。可传入 backtest_cli 的任意参数。"""
from pathlib import Path

from backtest_cli import make_parser, run

ROOT = Path(__file__).resolve().parent
CSV = ROOT / "data/ETHUSDT/1m/ETHUSDT-1m-2021-09-04_to_2026-09-03.csv"


def main() -> int:
    parser = make_parser()
    parser.description = "ETHUSDT U 本位合约做多网格示例（1m，五年，非参数优化）"
    parser.set_defaults(
        csv=CSV,
        timezone="UTC",
        reference_price=0,
        margin_per_grid=20,
        leverage=1,
        grid_step_pct=1,
        take_profit_pct=1,
        buy_grid_count=10,
        take_profit_count=10,
        take_profit_grid_count=20,
        max_unclosed_per_grid=1,
        price_tick=0.01,
        qty_step=0.001,
        fee_pct=0.1,
        output_json=ROOT / "results/ethusdt-long-5y.json",
    )
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
