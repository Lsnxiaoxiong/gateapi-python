from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import sys


EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "example"
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

import auto_grid_contract_futures_generic as futures_grid
import auto_grid_futures_generic as core


def contract_spec():
    return futures_grid.FuturesContractSpec(
        grid_spec=core.PairSpec(
            base_currency="SOXL",
            quote_currency="USDT",
            amount_step=Decimal("1"),
            price_step=Decimal("0.01"),
            min_base_amount=Decimal("1"),
            min_quote_amount=Decimal("0"),
            max_base_amount=Decimal("100000"),
            max_quote_amount=Decimal("Infinity"),
        ),
        multiplier=Decimal("0.01"),
        leverage_min=Decimal("1"),
        leverage_max=Decimal("50"),
    )


def test_contract_size_uses_margin_leverage_price_and_multiplier():
    size = futures_grid.calculate_contract_size(
        Decimal("1.6"), Decimal("2"), Decimal("145"), contract_spec()
    )
    assert size == Decimal("2")


class FakeFuturesApi:
    def __init__(self):
        self.created = []

    def create_futures_order(self, settle, order, **kwargs):
        self.created.append((settle, order))
        return SimpleNamespace(
            id=123,
            text=order.text,
            contract=order.contract,
            size=order.size,
            left=order.size,
            price=order.price,
            status="open",
            finish_as=None,
        )


def spot_style_order(side):
    return SimpleNamespace(
        currency_pair="SOXL_USDT",
        side=side,
        amount="2",
        price="145",
        text="t-test123",
    )


def test_buy_is_positive_futures_size_and_not_reduce_only():
    raw = FakeFuturesApi()
    api = futures_grid.FuturesGridApi(raw, "usdt")
    result = api.create_order(spot_style_order("buy"))
    _, submitted = raw.created[0]
    assert submitted.size == "2"
    assert submitted.reduce_only is False
    assert result.side == "buy"


def test_take_profit_is_negative_and_reduce_only():
    raw = FakeFuturesApi()
    api = futures_grid.FuturesGridApi(raw, "usdt")
    result = api.create_order(spot_style_order("sell"))
    _, submitted = raw.created[0]
    assert submitted.size == "-2"
    assert submitted.reduce_only is True
    assert result.side == "sell"


def test_finished_futures_order_is_normalised_for_grid_state_machine():
    filled = futures_grid.FuturesGridApi._normalise(SimpleNamespace(
        id=1, contract="SOXL_USDT", size="-2", left="0", price="146",
        status="finished", finish_as="filled", text="t-test",
    ))
    cancelled = futures_grid.FuturesGridApi._normalise(SimpleNamespace(
        id=2, contract="SOXL_USDT", size="2", left="1", price="145",
        status="finished", finish_as="cancelled", text="t-test",
    ))
    assert filled.status == "closed"
    assert filled.filled_amount == "2"
    assert cancelled.status == "cancelled"
    assert cancelled.filled_amount == "1"


def test_futures_websocket_event_is_normalised():
    event = futures_grid.FuturesOrderListener._normalise_event({
        "id": 88,
        "id_string": "88",
        "contract": "SOXL_USDT",
        "size": "-3",
        "left": "-1",
        "price": "146",
        "status": "finished",
        "finish_as": "cancelled",
        "text": "t-ft123",
    })
    assert event["id"] == "88"
    assert event["side"] == "sell"
    assert event["amount"] == "3"
    assert event["left"] == "1"
    assert event["filled_amount"] == "2"
    assert event["status"] == "cancelled"
