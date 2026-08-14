# Example Application

This is a demo application using `gate-api` to show how Gate APIv4 works. 
Instead of running it, it is recommended to read the source code to get a general idea of
how this SDK is used. However, you can modify this code directly to implement your own logic.

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

The take-profit window defaults to the nearest 20 anchored grid intervals above the current
price. When the configured take-profit order limit is full, nearer eligible exits replace farther
ones. Order reconciliation uses one open-order list request and one finished-order list request
every 0.5 seconds, with an individual lookup only as a fallback for records absent from both
snapshots.
A per-database, per-pair process lock prevents two processes from maintaining the same grid.
Live startup explicitly enables and verifies the account-level isolated-margin auto-repayment
setting when `auto_repay=True` (the default).
Partially filled take-profit leftovers below the pair's minimum base or quote order size are
retained as auditable dust rows and excluded from take-profit selection instead of being
resubmitted indefinitely.
Each open strategy take-profit must reference an active database row whose buy has filled.
Strategy sell orders without such a link are cancelled after a short creation grace period;
orders belonging to other strategies are left untouched.
