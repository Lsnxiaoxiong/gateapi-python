from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile


EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "example"
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

import auto_grid_contract_futures_generic as long_grid
import auto_grid_contract_futures_short_generic as short_grid
import auto_grid_futures_generic as core


def pair_spec():
    return core.PairSpec(
        base_currency="SOXL",
        quote_currency="USDT",
        amount_step=Decimal("1"),
        price_step=Decimal("0.01"),
        min_base_amount=Decimal("1"),
        min_quote_amount=Decimal("0"),
        max_base_amount=Decimal("100000"),
        max_quote_amount=Decimal("Infinity"),
    )


class FakeFuturesApi:
    def __init__(self):
        self.created = []

    def create_futures_order(self, settle, order, **kwargs):
        self.created.append((settle, order, kwargs))
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


def test_short_prices_and_take_profit_are_mirrored_from_long_direction():
    prices = short_grid.desired_short_prices(
        Decimal("100"), Decimal("101"), Decimal("0.003"), 10, Decimal("0.01")
    )
    assert prices == sorted(prices)
    assert len(prices) == len(set(prices)) == 10
    assert all(price > Decimal("101") for price in prices)
    assert all(
        short_grid.short_take_profit_price(
            price, Decimal("0.003"), pair_spec()
        ) < price
        for price in prices
    )


def test_short_open_and_cover_have_correct_sign_and_reduce_only():
    raw = FakeFuturesApi()
    api = short_grid.ShortFuturesGridApi(raw, "usdt")
    api.create_order(spot_style_order("sell"))
    _, opened, _ = raw.created[-1]
    assert opened.size == "-2"
    assert opened.reduce_only is False

    api.create_order(spot_style_order("buy"))
    _, covered, _ = raw.created[-1]
    assert covered.size == "2"
    assert covered.reduce_only is True


def test_short_limit_order_has_network_timeout():
    raw = FakeFuturesApi()
    api = short_grid.ShortFuturesGridApi(raw, "usdt")
    short_grid.create_futures_limit_order(
        api, "SOXL_USDT", "sell", Decimal("145"), Decimal("2"),
        "ss", "code",
    )
    assert raw.created[-1][2]["_request_timeout"] == core.NETWORK_TIMEOUT


def test_short_strategy_code_cannot_collide_with_long_strategy_code():
    args = (
        "usdt", "SOXL_USDT", Decimal("150"), Decimal("0.003"),
        Decimal("0.003"), Decimal("1.6"), Decimal("2"), "same-tag", 1,
    )
    assert short_grid.short_futures_strategy_code(*args) != (
        long_grid.futures_strategy_code(*args)
    )


def test_resume_uses_the_only_unfinished_database_strategy_code():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = core.GridStore(Path(temp_dir) / "short-grid.sqlite3")
        try:
            planned = core.PlannedOrder(Decimal("101"), Decimal("2"))
            order = SimpleNamespace(id="open-1", text="t-sslegacy-order")
            store.insert_buy(
                "SOXL_USDT", "legacy", order, planned, Decimal("100.69")
            )
            assert short_grid.resume_short_strategy_code(
                store, "SOXL_USDT"
            ) == "legacy"
        finally:
            store.close()


def test_resume_rejects_only_when_multiple_database_strategies_are_ambiguous():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = core.GridStore(Path(temp_dir) / "short-grid.sqlite3")
        try:
            planned = core.PlannedOrder(Decimal("101"), Decimal("2"))
            for code in ("old-one", "old-two"):
                store.insert_buy(
                    "SOXL_USDT", code,
                    SimpleNamespace(id=code, text=f"t-ss{code}-order"),
                    planned, Decimal("100.69"),
                )
            try:
                short_grid.resume_short_strategy_code(store, "SOXL_USDT")
            except RuntimeError as exc:
                assert "多个未完成策略" in str(exc)
            else:
                raise AssertionError("多个未完成策略时必须拒绝模糊恢复")
        finally:
            store.close()
