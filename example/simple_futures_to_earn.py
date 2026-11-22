"""USDT 永续 → 现货 → 余币宝活期 USDT。默认预览。

在 example/.env、项目根 .env 或系统环境中设置：
GATE_API_KEY=...
GATE_API_SECRET=...
可选 GATE_PROXY=http://127.0.0.1:7890
预览：python -m example.simple_futures_to_earn
执行：python -m example.simple_futures_to_earn --execute
详见 EARN_FUTURES_TRANSFER.md。
"""
import argparse
from pathlib import Path

try:
    from .earn_futures_transfer_common import STATE_DIR, execute_transfer
except ImportError:
    from earn_futures_transfer_common import STATE_DIR, execute_transfer

AMOUNT = "10"  # 从合约可划出余额取出的 USDT 数量
MIN_RATE = None  # None 使用产品当前最低小时利率；也可填字符串，不是年化利率
STATE_FILE = STATE_DIR / "futures_to_earn.json"  # 新一笔任务请使用新文件名


def main(argv=None):
    parser = argparse.ArgumentParser(description="将 USDT 永续资金转入余币宝活期")
    parser.add_argument("--execute", action="store_true", help="实际划转；不传则仅预览")
    parser.add_argument("--state-file", type=Path, default=STATE_FILE, help="指定要恢复的进度文件")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reset", action="store_true", help="归档原进度并开始新任务")
    mode.add_argument("--retry-rejected-transfer", action="store_true",
                        help="仅在确认上次划转被拒绝后，重试原任务的划转步骤")
    mode.add_argument("--retry-rejected-lend", action="store_true",
                      help="仅在确认上次申购被拒绝后，使用已到账现货重试申购")
    args = parser.parse_args(argv)
    execute_transfer("futures_to_earn", AMOUNT, args.state_file, min_rate=MIN_RATE,
                     execute=args.execute, retry_rejected_transfer=args.retry_rejected_transfer,
                     retry_rejected_lend=args.retry_rejected_lend, reset=args.reset)


if __name__ == "__main__":
    main()
