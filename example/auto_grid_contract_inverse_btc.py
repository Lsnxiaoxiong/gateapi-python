"""Gate BTC_USD 币本位（反向）永续合约网格：多头/空头独立运行入口。

安全默认：只读取公开行情并打印挂单预览，不会创建订单或修改杠杆。
实盘必须同时显式添加 --live，并设置方向对应的 GATE_LIVE_CONFIRM。
每张 BTC_USD 固定 1 USD 面值；--margin-btc 为单个网格的 BTC 保证金预算。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from decimal import Decimal

try:
    from . import auto_grid_futures_generic as core
    from . import auto_grid_contract_futures_generic as long_grid
    from . import auto_grid_contract_futures_short_generic as short_grid
except ImportError:
    import auto_grid_futures_generic as core
    import auto_grid_contract_futures_generic as long_grid
    import auto_grid_contract_futures_short_generic as short_grid


SCRIPT_DIR = Path(__file__).resolve().parent
LONG_DATABASE = SCRIPT_DIR / "inverse_btc_long_grid_orders.sqlite3"
SHORT_DATABASE = SCRIPT_DIR / "inverse_btc_short_grid_orders.sqlite3"
CONTRACT = "BTC_USD"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gate BTC_USD 币本位永续网格（不加 --live 时仅预览）"
    )
    parser.add_argument("--direction", choices=("long", "short"), default="long",
                        help="long: 低买高卖；short: 高卖低买")
    parser.add_argument("--reference", required=True,
                        help="网格锚定价格（USD/BTC，如 80000）")
    parser.add_argument("--margin-btc", required=True,
                        help="每格的 BTC 保证金预算，如 0.0001")
    parser.add_argument("--leverage", default="2", help="杠杆（默认 2 倍）")
    parser.add_argument("--open-count", type=int, default=10, help="挂开仓单数量")
    parser.add_argument("--tp-count", type=int, default=10, help="最多挂止盈单数量")
    parser.add_argument("--tp-window", type=int, default=20, help="止盈维护窗口网格数")
    parser.add_argument("--step", default="0.003", help="网格间距小数，如 0.003=0.3%%")
    parser.add_argument("--take-profit", default="0.003", help="止盈比例")
    parser.add_argument("--startup-mode", choices=("resume", "reset"), default="resume")
    parser.add_argument("--database", type=Path, help="SQLite 文件；默认为多空分别独立")
    parser.add_argument("--poll-interval", type=float, default=2.5)
    parser.add_argument("--live", action="store_true",
                        help="真实下单；还必须在环境变量中设置对应确认字符串")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    for name, value in (("reference", args.reference),
                        ("margin-btc", args.margin_btc),
                        ("leverage", args.leverage),
                        ("step", args.step),
                        ("take-profit", args.take_profit)):
        core.decimal_value(name, value)

    # 必须在读 .env 之后检查，并由 --live 兜底关闭任何遗留的实盘确认开关。
    core.load_env_file()
    module = long_grid if args.direction == "long" else short_grid
    if args.live:
        if os.getenv("GATE_LIVE_CONFIRM", "") != module.LIVE_CONFIRM_TEXT:
            raise RuntimeError(
                "拒绝实盘：必须同时设置 GATE_LIVE_CONFIRM="
                + module.LIVE_CONFIRM_TEXT
            )
    else:
        os.environ["GATE_LIVE_CONFIRM"] = ""

    db = args.database or (
        LONG_DATABASE if args.direction == "long" else SHORT_DATABASE
    )
    params = dict(
        contract=CONTRACT,
        settle="btc",
        reference_price=args.reference,
        margin_amount_per_order=args.margin_btc,
        leverage=args.leverage,
        grid_tag="btcInverseL" if args.direction == "long" else "btcInverseS",
        take_profit_count=args.tp_count,
        take_profit_grid_count=args.tp_window,
        grid_step_pct=args.step,
        take_profit_pct=args.take_profit,
        startup_mode=args.startup_mode,
        database_path=db,
        poll_interval=args.poll_interval,
    )
    if args.direction == "long":
        long_grid.run_grid(
            **params, buy_grid_count=args.open_count,
            buy_client_order_prefix="ib",
            sell_client_order_prefix="it",
        )
    else:
        short_grid.run_short_grid(
            **params, short_grid_count=args.open_count,
            open_client_order_prefix="is",
            cover_client_order_prefix="ic",
        )


if __name__ == "__main__":
    main()
