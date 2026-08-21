from example.auto_grid_contract_futures_short_generic import run_short_grid, DEFAULT_DATABASE


def run_AVAAI() -> None:
    run_short_grid(
        contract="AVAAI_USDT",
        reference_price="0.0179",
        margin_amount_per_order="0.41",  # 每格保证金；名义仓位约为保证金×杠杆
        leverage="1",
        settle="usdt",
        short_grid_count=10,
        take_profit_count=10,
        take_profit_grid_count=20,  # 检查现价下方20个网格，最多保留10个平仓单
        max_unclosed_per_grid=1,  # 单个网格价位最多保留的已成交未平仓周期数
        grid_step_pct="0.003",
        take_profit_pct="0.003",
        startup_mode="resume",
        database_path=DEFAULT_DATABASE,
        poll_interval=2,
    )


if __name__ == "__main__":
    run_AVAAI()
