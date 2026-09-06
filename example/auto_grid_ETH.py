from example.auto_grid_futures_generic import run_grid, DEFAULT_DATABASE


def run_ETH() -> None:
    run_grid(
        currency_pair="ETH_USDT",
        reference_price="2500",
        margin_amount_per_order="3.3",
        leverage="3",
        buy_grid_count=10,
        take_profit_count=10,
        take_profit_grid_count=20,  # 检查现价上方20个网格，最多保留10个止盈单
        max_unclosed_per_grid=1,  # 单个网格价位最多保留的已成交未止盈周期数
        grid_step_pct="0.003",
        take_profit_pct="0.003",
        auto_borrow=True,
        auto_repay=True,
        startup_mode="resume",  # resume=恢复；reset=撤单并清空当前交易对后重启
        database_path=DEFAULT_DATABASE,
        poll_interval=2,
    )


if __name__ == "__main__":
    run_ETH()
