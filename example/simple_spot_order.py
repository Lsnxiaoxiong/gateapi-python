"""Gate API 最简逐仓杠杆限价买入示例。

运行前设置 GATE_API_KEY、GATE_API_SECRET 和 GATE_LIVE_CONFIRM=PLACE_MARGIN_ORDER。
请先修改下方交易对、USDT 金额和价格。本示例使用逐仓杠杆账户限价买入。
默认不自动借币；需要自动借币时，将 AUTO_BORROW 改为 True。
"""

import os
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from gate_api import (
    ApiClient,
    Configuration,
    MarginApi,
    MarginMarketLeverage,
    Order,
    SpotApi,
)
from gate_api.exceptions import GateApiException


CURRENCY_PAIR = "SOXLG_USDT"
USDT_AMOUNT = "3"  # 买入使用的 USDT 金额
PRICE = "100"      # USDT 限价
LEVERAGE = "2"       # 逐仓杠杆倍数，例如 "3" 表示 3 倍
AUTO_BORROW = True  # True 会在逐仓 USDT 不足时自动借币


def load_env_file() -> None:
    """读取 example/.env 或项目根目录 .env，且不覆盖已有环境变量。"""
    script_dir = Path(__file__).resolve().parent
    for env_file in (script_dir / ".env", script_dir.parent / ".env"):
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name:
                os.environ.setdefault(name, value)
        return


def calculate_base_amount(api):
    """按交易对数量精度，将 USDT 金额换算为基础币数量。"""
    pair = api.get_currency_pair(CURRENCY_PAIR)
    usdt_amount = Decimal(USDT_AMOUNT)
    price = Decimal(PRICE)
    if usdt_amount <= 0 or price <= 0:
        raise ValueError("USDT_AMOUNT 和 PRICE 必须大于 0")
    precision = int(pair.amount_precision)
    unit = Decimal("1").scaleb(-precision)
    amount = (usdt_amount / price).quantize(unit, rounding=ROUND_DOWN)
    if amount <= 0:
        raise ValueError("换算后的基础币数量为 0，请增加 USDT_AMOUNT")
    if pair.min_base_amount and amount < Decimal(pair.min_base_amount):
        raise ValueError(f"基础币数量不能小于 {pair.min_base_amount}")
    if pair.min_quote_amount and amount * price < Decimal(pair.min_quote_amount):
        raise ValueError(f"订单金额不能小于 {pair.min_quote_amount} USDT")
    return format(amount, "f")


def configure_leverage(margin_api):
    """设置并验证当前交易对的逐仓杠杆倍数。"""
    leverage = Decimal(LEVERAGE)
    if leverage <= 0:
        raise ValueError("LEVERAGE 必须大于 0")
    margin_api.set_user_market_leverage(
        MarginMarketLeverage(currency_pair=CURRENCY_PAIR, leverage=LEVERAGE)
    )
    accounts = margin_api.list_margin_user_account(currency_pair=CURRENCY_PAIR)
    if not accounts:
        raise RuntimeError(f"未找到 {CURRENCY_PAIR} 的逐仓杠杆账户，请先在 Gate 开通该市场")
    actual = Decimal(accounts[0].leverage or "0")
    if actual != leverage:
        raise RuntimeError(f"杠杆设置校验失败：期望 {leverage} 倍，实际 {actual} 倍")
    return accounts[0]


def main():
    load_env_file()
    live_enabled = os.getenv("GATE_LIVE_CONFIRM") == "PLACE_MARGIN_ORDER"
    config = Configuration(
        host=os.getenv("GATE_HOST", "https://api.gateio.ws/api/v4"),
    )
    if live_enabled:
        config.key = os.environ["GATE_API_KEY"]
        config.secret = os.environ["GATE_API_SECRET"]
    client = ApiClient(config)
    try:
        api = SpotApi(client)
        amount = calculate_base_amount(api)
        print(
            f"逐仓杠杆订单：使用 {USDT_AMOUNT} USDT，以 {PRICE} 限价买入 "
            f"{amount} {CURRENCY_PAIR.split('_')[0]}；杠杆={LEVERAGE}倍，"
            f"自动借币={AUTO_BORROW}。"
        )
        if not live_enabled:
            print("预览完成。设置 GATE_LIVE_CONFIRM=PLACE_MARGIN_ORDER 才会真实下单。")
            return
        margin_api = MarginApi(client)
        account = configure_leverage(margin_api)
        quote_available = Decimal(account.quote.available or "0")
        print(f"已设置并验证 {CURRENCY_PAIR} 逐仓杠杆：{LEVERAGE} 倍。")
        if not AUTO_BORROW and quote_available < Decimal(USDT_AMOUNT):
            raise RuntimeError(
                f"逐仓账户可用 USDT 为 {quote_available}，不足 {USDT_AMOUNT}；"
                "请先转入资金，或确认风险后将 AUTO_BORROW 设为 True"
            )
        order = Order(
            currency_pair=CURRENCY_PAIR,
            side="buy",
            amount=amount,
            price=PRICE,
            type="limit",
            account="margin",
            time_in_force="gtc",
            auto_borrow=AUTO_BORROW,
        )
        result = api.create_order(order)
        print(f"下单成功：id={result.id}, status={result.status}")
    except GateApiException as exc:
        print(f"下单失败：label={exc.label}, message={exc.message}")
        raise
    finally:
        client.close()


if __name__ == "__main__":
    main()
