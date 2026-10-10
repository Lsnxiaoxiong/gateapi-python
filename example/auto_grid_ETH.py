from example.auto_grid_futures_generic import run_grid, DEFAULT_DATABASE


def run_ETH() -> None:
    run_grid(
        currency_pair="ETH_USDT",
        reference_price="2500",
        margin_amount_per_order="8.3",  # 修改后可直接 resume；新金额仅用于新订单
        leverage="3",
        buy_grid_count=10,
        take_profit_count=10,
        take_profit_grid_count=20,  # 检查现价上方20个网格，最多保留10个止盈单
        grid_step_pct="0.0022",
        take_profit_pct="0.0022",
        auto_borrow=True,
        auto_repay=True,
        startup_mode="resume",  # 沿用数据库唯一活动策略和旧订单，支持修改每格金额
        database_path=DEFAULT_DATABASE,
        poll_interval=2,
    )


if __name__ == "__main__":
    run_ETH()
