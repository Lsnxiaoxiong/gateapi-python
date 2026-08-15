"""Gate API 最简 USDT 永续合约限价做多示例。

默认只预览，不会下单。实盘需要在 .env 中设置：

GATE_API_KEY=...
GATE_API_SECRET=...
GATE_LIVE_CONFIRM=PLACE_FUTURES_ORDER

订单数量单位是“张”。程序根据保证金、杠杆、价格和合约乘数自动计算张数。
"""

import os
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
from gate_api.exceptions import GateApiException


CONTRACT = "SKHYNIX_USDT"
SETTLE = "usdt"
MARGIN_AMOUNT = "1.5"  # 预计占用的保证金，单位 USDT
LEVERAGE = "2"       # 杠杆倍数
PRICE = "10"        # 限价，单位 USDT
LIVE_CONFIRM_TEXT = "PLACE_FUTURES_ORDER"


def load_env_file() -> None:
    """读取 example/.env 或项目根目录 .env，不覆盖已有环境变量。"""
    script_dir = Path(__file__).resolve().parent
    for env_file in (script_dir / ".env", script_dir.parent / ".env"):
        if not env_file.is_file():
            continue
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name:
                os.environ.setdefault(name, value)
        return


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def positive_decimal(name: str, value: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return result


def calculate_contract_size(contract_info) -> tuple[Decimal, Decimal]:
    """返回下单张数和名义仓位价值。"""
    margin = positive_decimal("MARGIN_AMOUNT", MARGIN_AMOUNT)
    leverage = positive_decimal("LEVERAGE", LEVERAGE)
    price = positive_decimal("PRICE", PRICE)
    multiplier = positive_decimal(
        "quanto_multiplier", str(contract_info.quanto_multiplier)
    )
    minimum = positive_decimal(
        "order_size_min", str(contract_info.order_size_min)
    )
    size_step = minimum if contract_info.enable_decimal else Decimal("1")
    size = (
        margin * leverage / (price * multiplier) / size_step
    ).to_integral_value(rounding=ROUND_DOWN) * size_step
    if size < minimum:
        minimum_margin = minimum * price * multiplier / leverage
        raise ValueError(
            f"保证金不足，当前参数至少约需 "
            f"{decimal_text(minimum_margin)} USDT"
        )
    maximum = Decimal(str(contract_info.order_size_max or "Infinity"))
    if size > maximum:
        raise ValueError(f"张数 {size} 超过最大下单张数 {maximum}")
    return size, size * price * multiplier


def main() -> None:
    load_env_file()
    live = os.getenv("GATE_LIVE_CONFIRM", "") == LIVE_CONFIRM_TEXT
    host = os.getenv("GATE_HOST", "https://api.gateio.ws/api/v4").strip()
    config = Configuration(host=host)
    proxy = os.getenv("GATE_PROXY", "").strip()
    if proxy:
        config.proxy = proxy
    if live:
        config.key = os.environ["GATE_API_KEY"]
        config.secret = os.environ["GATE_API_SECRET"]

    client = ApiClient(config)
    try:
        api = FuturesApi(client)
        contract_info = api.get_futures_contract(SETTLE, CONTRACT)
        if contract_info.status != "trading":
            raise RuntimeError(f"合约 {CONTRACT} 当前不可交易")

        leverage = positive_decimal("LEVERAGE", LEVERAGE)
        leverage_min = Decimal(str(contract_info.leverage_min or "1"))
        leverage_max = Decimal(str(contract_info.leverage_max or "1"))
        if not leverage_min <= leverage <= leverage_max:
            raise ValueError(
                f"杠杆必须在 {leverage_min} 到 {leverage_max} 之间"
            )

        size, notional = calculate_contract_size(contract_info)
        estimated_margin = notional / leverage
        print(
            f"订单：{CONTRACT} 限价做多，价格={PRICE} USDT，张数={size}，"
            f"名义仓位={decimal_text(notional)} USDT，"
            f"预计保证金={decimal_text(estimated_margin)} USDT，"
            f"杠杆={LEVERAGE}倍。"
        )
        if not live:
            print(
                f"安全预览完成。设置 GATE_LIVE_CONFIRM={LIVE_CONFIRM_TEXT} "
                "才会真实下单。"
            )
            return

        account = api.list_futures_accounts(SETTLE)
        dual_mode = bool(account.in_dual_mode) or account.position_mode in {
            "dual", "dual_plus"
        }
        if dual_mode:
            api.update_dual_mode_position_leverage(SETTLE, CONTRACT, LEVERAGE)
        else:
            api.update_position_leverage(SETTLE, CONTRACT, LEVERAGE)
        order = FuturesOrder(
            contract=CONTRACT,
            size=decimal_text(size),  # 正数表示买入/做多
            price=PRICE,
            tif="gtc",
            reduce_only=False,
            text="t-simple-futures-buy",
        )
        result = api.create_futures_order(SETTLE, order)
        print(
            f"下单成功：id={result.id}，status={result.status}，"
            f"size={result.size}，price={result.price}"
        )
    except GateApiException as exc:
        print(f"下单失败：label={exc.label}, message={exc.message}")
        raise
    finally:
        client.close()


if __name__ == "__main__":
    main()
