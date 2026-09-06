# ETHUSDT 1 分钟做多网格回测

本示例按“做多网格”实现，复用 `backtest_engine.py` 的逐分钟撮合逻辑。

## 已下载数据

- 市场：Binance ETHUSDT U 本位永续合约，与原 BTC 回测数据口径一致，并非 Gate 或现货行情。
- 时间：UTC 2021-09-04 00:00:00 至 2026-09-03 23:59:00，共五年；不包含当天尚未结束的 K 线。
- CSV：`data/ETHUSDT/1m/ETHUSDT-1m-2021-09-04_to_2026-09-03.csv`。
- 共 2,629,440 根，分钟缺口为 0；约 293 MB。
- 第一列为 UTC 毫秒时间戳，随后为 open/high/low/close/volume，保留 Binance 原始 12 列。
- 同目录 `.manifest.json` 记录时间范围、行数、缺口、CSV SHA-256，以及每个归档的来源和 SHA-256；原始 ZIP 保存在 `archives`。
- 数据来源及字段说明：[Binance 官方公共数据仓库](https://github.com/binance/binance-public-data)。下载器优先取月包，月包未发布时回退到日包；逐包核验官方校验和。遇到缺失归档直接报错，行情缺口会记录在清单中，不伪造补线。

## 运行完整五年示例

在 PowerShell 中执行（只需 Python 标准库，无需 API 密钥）：

```powershell
cd D:\source\gateapi-python\grid-replay
python example_ethusdt_long.py
```

结果打印到终端，并保存到 `results/ethusdt-long-5y.json`。本次全量运行日志为 `backtest-ethusdt.log`。

本次已完成全部 2,629,440 根 K 线回测，耗时约 113 秒：

| 指标 | 本次结果 |
| --- | ---: |
| 已实现盈亏 | +5,601.64 USDT |
| 最终浮动盈亏 | -893.17 USDT |
| 最终总盈亏（含已付手续费） | +4,708.47 USDT |
| 累计手续费 | 620.56 USDT |
| 完成网格周期 | 12,632 |
| 最终持仓数 | 68 |
| 最大同时持仓数 | 170 |
| 最大保证金占用 | 8,500.00 USDT |
| 最大回撤 | 3,619.73 USDT |

这些数字应结合文末的回测口径阅读，不能直接视为真实账户可实现的收益。

| 参数 | 示例值 |
| --- | --- |
| 方向 | 只做多 |
| 网格参考价 | 第一根 K 线开盘价 |
| 每格保证金 | 50 USDT |
| 杠杆 | 1 倍 |
| 网格间距 / 止盈 | 1% / 1% |
| 现价下方维护买格数 | 10 |
| 最多活动止盈单 | 10 |
| 止盈检查网格范围 | 20 |
| 每格最多未平仓周期 | 1 |
| 价格 / 数量步长 | 0.01 USDT / 0.001 ETH |
| 单边手续费 | 0.05% |

参数用于演示，不代表最佳参数，也未逐历史时点同步交易所费率或交易精度。买格数 10 是活动挂单窗口，不是总持仓上限；下跌中旧仓位会保留，新买单继续随价格维护。

可覆盖任意 CLI 参数。例如仅跑 2025 年，并另存报告（结束时间不包含在范围内）：

```powershell
python example_ethusdt_long.py --start 2025-01-01 --end 2026-01-01 --grid-step-pct 0.5 --take-profit-pct 0.5 --output-json results/ethusdt-long-2025.json
python example_ethusdt_long.py --help
```

## 在已有页面中查看 ETH 数据

```powershell
python serve_ethusdt.py
```

默认打开 `http://127.0.0.1:18888`，端口可以通过 `GRID_REPLAY_PORT` 环境变量修改。页面使用现有 UI 默认策略参数；若要与命令行示例比较，请按上表设置参数。页面是交互回放，完整五年统计使用命令行入口。前端构建已更新；后续改动前端后可在 `ui` 目录运行 `npm run build`。

通用服务也支持环境变量 `GRID_REPLAY_CSV` 和 `GRID_REPLAY_SYMBOL`。未设置时仍默认使用原 BTC 数据。索引保存到各 CSV 旁的 `.index.json`，CLI 导入不再依赖 BTC 文件存在。

## 重新下载或更新

复现本次固定时间范围：

```powershell
python download_ethusdt_1m.py --start 2021-09-04 --end 2026-09-04
```

不传日期则下载截至当前 UTC 日期之前五年的数据。`--end` 为不包含的日期。已缓存 ZIP 会重新核验校验和，通过后直接复用。新日期生成新文件，可用 `example_ethusdt_long.py --csv 新CSV路径` 指定；示例默认路径固定为本次五年数据。

## 回测口径

引擎每分钟先撮合已有止盈单，再撮合买单，之后按收盘价更新挂单；当分钟新买入仓位不能在同一分钟止盈。它不能还原一分钟内逐笔价格路径。

统计包含双边交易手续费，未平仓仓位按最后收盘价估值并计入已付开仓手续费，未预扣未来平仓手续费。最大回撤按分钟收盘统计。引擎没有初始资金上限、保证金不足拒单、强平、资金费率或滑点模型，因此此结果是简化策略盈亏，不是实际可交易账户收益率；最大保证金占用也不等于覆盖浮亏所需的账户本金。

验证命令：

```powershell
python -m unittest discover -s . -p test_ethusdt_data.py
```
