from example.auto_grid_contract_futures_generic import run_grid, DEFAULT_DATABASE


def run_ETH() -> None:
    run_grid(
        contract="ETH_USDT",
        reference_price="2385",
        margin_amount_per_order="3.7",  # 每格保证金；名义仓位约为保证金×杠杆
        leverage="3",
        settle="usdt",
        buy_grid_count=20,
        take_profit_count=20,
        take_profit_grid_count=30,  # 检查现价上方20个网格，最多保留10个止盈单
        grid_step_pct="0.00065",
        take_profit_pct="0.00065",
        startup_mode="resume",
        database_path=DEFAULT_DATABASE,
        poll_interval=2,
    )


if __name__ == "__main__":
    run_ETH()
