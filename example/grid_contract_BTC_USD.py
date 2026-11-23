"""BTC_USD 币本位（BTC 保证金、BTC 结算）永续多头网格。

参数直接在 run_BTC() 中修改，无 CLI。
默认仅预览挂单；实盘只需将 LIVE_TRADING 设为 True。
"""

from pathlib import Path

from example.auto_grid_contract_futures_generic import run_grid


LIVE_TRADING = True  # 安全默认：仅预览，不下单、不修改杠杆
DATABASE = Path(__file__).resolve().with_name("inverse_btc_long_grid_orders.sqlite3")


def run_BTC() -> None:
    run_grid(
        contract="BTC_USD",
        reference_price="80000",  # USD/BTC；使用前按当前价格调整
        margin_amount_per_order="0.00002",  # 每格 BTC 保证金（不是 USD 或张数）
        leverage="1",
        settle="btc",
        grid_tag="btcInverseL",
        buy_client_order_prefix="ib",
        sell_client_order_prefix="it",
        buy_grid_count=5,
        take_profit_count=5,
        take_profit_grid_count=20,  # 检查现价上方 20 格，最多保留 10 个止盈单
        grid_step_pct="0.005",      # 0.3%
        take_profit_pct="0.005",
        startup_mode="resume",
        database_path=DATABASE,
        poll_interval=2,
        live_trading=LIVE_TRADING,
    )


if __name__ == "__main__":
    run_BTC()
