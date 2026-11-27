# BTCUSDT 量化策略回测

使用项目 `data` 中 Binance BTCUSDT USDT 本位永续合约的 1 分钟行情，比较双均线趋势、Donchian 通道突破、布林带均值回归、RSI 顺势回调及买入持有基准。需要 Python 3.10 或更高版本，仅使用标准库。

在仓库根目录运行全量回测：

```powershell
python .\quant-backtest\run_backtest.py
```

按北京时间指定区间、成本和输出路径：

```powershell
python .\quant-backtest\run_backtest.py --start 2024-01-01 --end 2026-01-01 --capital 10000 --fee-bps 5 --slippage-bps 2 --output .\quant-backtest\results-2024-2025
```

起始日期包含当天，结束日期排除当天。回测前的行情用于指标预热，起始日开盘按当时已知的信号决定是否建仓。`--csv` 可以更换输入文件，列前缀为 `open_time,open,high,low,close`，开盘时间使用毫秒时间戳，按分钟升序排列。

双均线、通道、布林带和 RSI 的参数定义在 `strategies.py`。每种策略独立持有一份初始本金。

默认结果位于 `results`，同一个目录下重复运行会覆盖结果文件。

| 文件 | 内容 |
| --- | --- |
| `report.md` | 数据完整性、全区间绩效、分年收益、规则和成交假设 |
| `equity.csv` | 各策略逐 4 小时的权益，单位为 USDT |
| `trades.csv` | 每笔开平仓的时间、价格、数量、成本、净盈亏和退出原因 |

运行固定与动态网格、双均线波动率调仓及日内动量的对照实验：

```powershell
python .\quant-backtest\run_experiments.py
```

实验报告和权益分别保存在 `results/experiments.md` 和 `results/experiments_equity.csv`。

对比双均线死叉空仓和反手做空，涵盖 4h SMA20/100、1h SMA20/100 及 1h SMA80/400：

```powershell
python .\quant-backtest\run_sma_comparison.py
```

结果写入 `results/sma_comparison.md` 和 `results/sma_comparison_equity.csv`。

使用 ETHUSDT 的两段分钟数据复现同区间对照，结束日期采用 UTC 且排除当天：

```powershell
python .\quant-backtest\run_sma_comparison.py --symbol ETHUSDT `
  --csv .\grid-replay\data\ETHUSDT\1m\ETHUSDT-1m-2020-01-01_to_2021-09-03.csv `
  --csv .\grid-replay\data\ETHUSDT\1m\ETHUSDT-1m-2021-09-04_to_2026-09-03.csv `
  --end 2026-08-01 --with-hold
```

ETH 结果写入 `results/ethusdt_sma_comparison.md` 和 `results/ethusdt_sma_comparison_equity.csv`。

测试 4h SMA20/100 在均线靠拢时提前开平仓，覆盖从 0% 至 2% 的提前阈值：

```powershell
python .\quant-backtest\run_sma_early_entry.py
```

ETH 和 BTC 各测试 100 组参数。对称阈值及分段结果见 `results/sma_early_entry_report.md`，全组合结果见 `results/sma_early_entry_results.csv`。
