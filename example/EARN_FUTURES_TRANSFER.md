# USDT 余币宝活期与永续合约划转示例

两个入口默认预览，不发送网络请求：

```powershell
python -m example.simple_earn_to_futures
python -m example.simple_futures_to_earn
```

在项目根目录运行。也可在 IDE 中直接运行这两个文件，解释器需要安装本项目及依赖。

- `simple_earn_to_futures.py`：赎回 USDT → 等待现货余额增加 → 转入 USDT 永续 → 查询划转成功。
- `simple_futures_to_earn.py`：USDT 永续转出现货 → 查询划转成功及到账 → 申购 USDT 余币宝。
- 修改入口中的 `AMOUNT` 设置实际 USDT 数量，最多 8 位小数。
- 反向入口的 `MIN_RATE=None` 表示采用当前产品最低小时利率。可填字符串指定小时利率；不是年化百分比。

环境变量或 `example/.env`、项目根目录 `.env`：

```ini
GATE_API_KEY=你的密钥
GATE_API_SECRET=你的私钥
# 可选：
# GATE_PROXY=http://127.0.0.1:7890
```

系统已有环境变量优先于 `.env`。密钥需要对应账户查询、余币宝申赎和钱包划转权限。
两个方向均无需配置确认环境变量。实际划转时，为所需方向添加 `--execute`：

```powershell
python -m example.simple_earn_to_futures --execute
python -m example.simple_futures_to_earn --execute
```

原网格的 `GATE_LIVE_CONFIRM` 不会启用这两个示例。

示例面向经典合约账户，检查 `margin_mode=0`。统一账户保证金模式不走此流程。
永续划出金额还受真实可划出额度限制，最终由服务端校验，不能用总资产推算。

## 进度及重启

也可以运行自动余额管理程序。该入口不再使用命令行参数：

```bash
python3 -u -m example.auto_earn_futures_balance
```

修改 `example/auto_earn_futures_balance.py` 文件顶部的 `EXECUTE` 常量：默认 `False` 为只读监控，`True` 为实际申购、赎回和划转。服务/程序每 60 秒检查一次，使用经典 USDT 永续的 `available` 可用余额：
严格大于 200 时划出 50 USDT 并申购，严格小于 100 时赎回 50 USDT 并划入，边界值不操作。
余币宝有赎回等待到账或当前 USDT 数量不足 50 时，不再提交新赎回。
如果 Gate 未返回 `current_amount`，用总出借数量 `amount` 做初筛；实际可赎回数量由 Gate 校验。
每轮最多启动一笔，未完成的任务使用 `example/.transfer_state/auto/<账户标识>/` 中的原文件续跑，
结果不明时保留进度并阻止新任务。实际运行期间会持有和手动脚本共用的账户锁。
若需手动处理自动任务，先停止自动程序，再按日志中的路径用 `--state-file` 指定原进度文件；
手动入口的 `AMOUNT` 必须和进度里的数量一致（默认自动任务为 50）。

### Linux systemd 开机自启

在部署后的 **Linux 项目根目录**运行（不是在 Windows / WSL 开发环境中安装系统服务）：

```bash
# 查看生成的服务配置；不会安装服务
bash example/script/create_auto_earn_futures_service.sh print

# 安装服务、开机自启并启动。默认使用当前普通用户及 PATH 中的 python3
bash example/script/create_auto_earn_futures_service.sh install

# 如果服务器使用 ubuntu 用户和虚拟环境：
bash example/script/create_auto_earn_futures_service.sh install --user ubuntu --python /usr/app/venv/bin/python

# 管理服务
bash example/script/create_auto_earn_futures_service.sh status
bash example/script/create_auto_earn_futures_service.sh logs
bash example/script/create_auto_earn_futures_service.sh stop
bash example/script/create_auto_earn_futures_service.sh restart
bash example/script/create_auto_earn_futures_service.sh uninstall
```

安装脚本自动使用自己的项目位置生成 `WorkingDirectory`，固定执行
`python -u -m example.auto_earn_futures_balance`，不会传 CLI 参数；需要 sudo 安装系统级 unit。
安装前要在 `example/.env` 或根目录 `.env` 中配置 API Key/Secret，并确保服务用户可读、可写资金进度目录。
**服务安装与开机启动不会自动打开实盘开关**，需在 Python 文件中明确设置 `EXECUTE = True` 后重启服务。
不要同时手动运行另一个相同账户的自动程序。服务卸载不会删除 `.transfer_state` 的资金任务进度文件。

### 手动示例

进度保存在 `example/.transfer_state/`，已加入 Git 忽略列表，不包含 API 私钥。
默认每个方向只代表一笔任务：完成后反复运行会显示已完成，避免再次转钱。
要做新的一笔任务，在相应命令中加 `--execute --reset`：先备份原进度，再从第一步开始。
`--reset` 忽略旧任务状态，即使旧任务仍在等待到账、划转确认或申购，也会开始一笔新任务。
归档只影响本地进度，不会取消或撤回交易所上已经提交的操作。
如果原任务已赎回但尚未划转，`--reset` 会开始一笔新的赎回，不会使用原任务在现货中的资金。
可通过 `--state-file example/.transfer_state/earn_to_futures_02.json` 续跑指定文件。

- 等待赎回或划转超时：用原金额、原 `STATE_FILE` 重启，继续查询。
- 遇到明确的 Wallet 权限 `403` 拒绝时，新版会保存为 `transfer_ready`，开通读写权限后直接重跑。
- 若旧版在这次明确拒绝后留下了 `transfer_submitting`，可在对应命令的 `--execute` 后加
  `--retry-rejected-transfer`，使用原进度文件重试划转，不会再次赎回。不要将此参数用于超时等结果未知的情况。
- 收到 `*_submitting` 提示：表示请求可能已经被服务器接受，或请求响应还未保存。
  示例不会自动重发。先人工核对记录，再恢复进度；不要直接删除文件重跑。
- 已确认赎回成功但停在 `redeem_submitting`：核对金额后可将进度改为 `redeem_wait`。
- 已确认划转提交成功但停在 `transfer_submitting`：核对交易记录后补入正确的
  `tx_id`，将进度改为 `transfer_wait`。
- 已确认申购成功但停在 `lend_submitting`：可将进度改为 `done`。
- 旧版若因 `min_rate` 科学计数法收到明确的 `400` 拒绝，使用原进度文件运行
  `python3 -m example.simple_futures_to_earn --execute --retry-rejected-lend`，只重试申购。
  新版用普通小数字符串发送利率；收到明确的利率参数拒绝时会回到 `lend_ready`。
  如果明确确认请求被拒绝且完全没有执行，恢复到该请求之前的状态再重试。

程序默认每 2 秒查询，单次等待最长 65 分钟。赎回响应成功与实际资金到账是两步。
赎回到账使用执行前余额加赎回金额作为判断基线。合约转现货由钱包回执确认，确认成功后，
现货可用余额足够即申购原金额。运行期间请避免其他程序或手工操作
对该账户现货 USDT 进行交易、申赎或资金转入转出；否则余额变化无法可靠归因。
两个示例使用共同账户锁，但不能约束其他程序。

接口来源：

- [Gate 余币宝 API](https://www.gate.com/docs/developers/apiv4/en/earnuni/)：`POST /earn/uni/lends`。
- [Gate 钱包 API](https://www.gate.com/docs/developers/apiv4/en/wallet/)：`POST /wallet/transfers`、`GET /wallet/transfers?tx_id=...`。
- [Gate 现货 API](https://www.gate.com/docs/developers/apiv4/en/spot/)：`GET /spot/accounts`。
