# Example Application

This is a demo application using `gate-api` to show how Gate APIv4 works. 
Instead of running it, it is recommended to read the source code to get a general idea of
how this SDK is used. However, you can modify this code directly to implement your own logic.

## USDT 永续合约数据库网格

`auto_grid_contract_futures_generic.py` 是独立的 USDT 永续合约入口。它复用
现有 SQLite 网格状态机，但订单全部通过 `FuturesApi` 提交，默认合约为
`SOXL_USDT`。数据库中的数量代表合约张数，止盈卖单使用负张数并强制
`reduce_only=True`。

先在 `example/.env` 或项目根目录 `.env` 配置：

```ini
GATE_API_KEY=...
GATE_API_SECRET=...
GATE_USER_ID=...
# 可选：GATE_PROXY=http://127.0.0.1:7890
```

不设置实盘确认值时只读取公开合约规格和行情并打印预览：

```bash
python example/auto_grid_contract_futures_generic.py
```

确认账户使用单向持仓模式、参数及预览无误后，实盘确认值为：

```ini
GATE_LIVE_CONFIRM=RUN_REAL_FUTURES_DB_GRID
```

合约网格使用独立数据库 `example/futures_grid_orders.sqlite3`，不会读取现货
杠杆网格的 `grid_orders.sqlite3`。

## USDT 永续合约 做空 数据库网格

`auto_grid_contract_futures_short_generic.py` 是**做空方向**的永续合约网格入口，
与上面的多头版本互为镜像：

- 网格价位从参考价**向上**排列（`reference*(1+step)^index`），在现价上方
  挂 **SELL** 开空单（负张数、`reduce_only=False`）开空；
- 开空成交后，在开空价**下方**挂 **BUY** 平仓单（正张数、`reduce_only=True`）
  平掉空仓，低买平空获利；
- 平仓窗口为现价**下方**区间 `[lower, current)`，只维护落在窗口内的平仓单；
- 使用独立数据库 `example/futures_short_grid_orders.sqlite3` 与独立策略码
  （默认 `grid_tag=futshort01`），不会与多头网格或现货网格冲突。

安全预览（默认合约为 `SOXL_USDT`）：

```bash
python example/auto_grid_contract_futures_short_generic.py
```

实盘确认值（设置于 `.env`）：

```ini
GATE_LIVE_CONFIRM=RUN_REAL_FUTURES_SHORT_DB_GRID
```

做空网格与多头网格一样只支持单向持仓模式；开空单和平仓单使用不同的订单
前缀（默认开空 `ss`、平仓 `sb`），便于 WebSocket 与 REST 对账区分。


## Build

1. Clone this project and make sure it is named with `gateapi-python`
2. Run `./build.sh`, then your demo application will be created beside `gateapi-python`

## Run

**READ THIS BEFORE YOU RUN ANYTHING**

**This application is shown for demo only. It will try to use your input API key and secret to
trade, lend and borrow, etc. Make sure you know exactly what it does before running it.**

> The build.sh script will try to initiate a virtualenv environment if it can find virtualenv
> executable. Follow what the script prints before running the demo application 

```bash
# run futures demo against TestNet
python app.py futures -k <YOUR_TESTNET_API_KEY> -s <YOUR_TESTNET_API_SECRET> -u fx-api-testnet.gateio.ws

# run futures demo against real trading
python app.py futures -k <YOUR_API_KEY> -s <YOUR_API_SECRET>

# run spot demo
python app.py spot -k <YOUR_API_KEY> -s <YOUR_API_SECRET>

# run margin demo
python app.py margin -k <YOUR_API_KEY> -s <YOUR_API_SECRET>
```

## SQLite-persistent isolated-margin grid example

`auto_grid_futures_generic.py` is a USDT-quoted isolated-margin spot grid example. The historic
filename is retained for compatibility. It is dry-run by default. In live mode it stores each
buy/take-profit cycle in `example/grid_orders.sqlite3`; rows are separated by currency pair and
strategy code. Edit `run_SOXLG` first and choose `startup_mode="resume"` to continue from the
database or `startup_mode="reset"` to cancel database-recorded open orders and rebuild the pair.

```powershell
# From the repository root: install the local SDK, then preview using public market data.
python -m pip install -e .
python -m example.auto_grid_futures_generic

# Run against Spot TestNet after confirming the isolated-margin settings on Gate.
$env:GATE_HOST = "https://api-testnet.gateapi.io/api/v4"
$env:GATE_API_KEY = "YOUR_TESTNET_API_KEY"
$env:GATE_API_SECRET = "YOUR_TESTNET_API_SECRET"
$env:GATE_LIVE_CONFIRM = "RUN_REAL_MARGIN_DB_GRID"
python -m example.auto_grid_futures_generic
```

For production, omit `GATE_HOST` (the SDK default host is used), set production API
credentials, and keep the explicit confirmation variable. The program sets the configured
isolated-margin leverage before starting. A planned take-profit price is persisted with every
buy, but the real sell order is submitted only after that buy fills. Stopping it does not cancel
existing orders; use the same parameters with `resume` on the next start. If unfinished rows
belong to different parameters, `resume` stops and requires an explicit `reset`.

The take-profit window defaults to the nearest 10 anchored grid intervals above the current
price. `GridChecker` cancels buy and take-profit orders outside their current 10-grid windows;
when the take-profit limit is full, nearer eligible exits replace farther ones. `OrderListener`
subscribes to Gate's authenticated `spot.orders` WebSocket channel and updates SQLite on order
creation, fill, close, and cancellation. REST open/finished snapshots run only at startup, after
reconnection, and every 30 seconds as loss-recovery checks.

The persisted cycle states now follow this sequence:

```text
buy_status=已挂单、未成交 | position_status=未设置止盈单 | tp_status=未设置止盈单
buy_status=已成交         | position_status=等待挂止盈单 | tp_status=等待挂单
buy_status=已成交         | position_status=已设置止盈、等待止盈 | tp_status=止盈单已挂单
buy_status=已成交         | position_status=止盈已成交 | tp_status=止盈已成交
```
A per-database, per-pair process lock prevents two processes from maintaining the same grid.
Live startup explicitly enables and verifies the account-level isolated-margin auto-repayment
setting when `auto_repay=True` (the default).
Partially filled take-profit leftovers below the pair's minimum base or quote order size are
retained as auditable dust rows and excluded from take-profit selection instead of being
resubmitted indefinitely.
Each open strategy take-profit must reference an active database row whose buy has filled.
Strategy sell orders without such a link are cancelled after a short creation grace period;
orders belonging to other strategies are left untouched.
