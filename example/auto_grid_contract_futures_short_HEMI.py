from example.auto_grid_contract_futures_short_generic import run_short_grid, DEFAULT_DATABASE


def run_SOXL() -> None:
    run_short_grid(
        contract="HEMI_USDT",
        reference_price="0.0063",
        margin_amount_per_order="1.3",  # 每格保证金；名义仓位约为保证金×杠杆
        leverage="1",
        settle="usdt",
        short_grid_count=10,
        take_profit_count=10,
        take_profit_grid_count=20,  # 检查现价下方20个网格，最多保留10个平仓单
        grid_step_pct="0.0045",
        take_profit_pct="0.0045",
        startup_mode="resume",
        database_path=DEFAULT_DATABASE,
        poll_interval=2,
    )


if __name__ == "__main__":
    run_SOXL()
