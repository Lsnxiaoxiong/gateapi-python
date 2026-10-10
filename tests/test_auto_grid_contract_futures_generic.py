from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
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

    def get_position(self, settle, contract, **kwargs):
        return SimpleNamespace(size="2")


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


def test_futures_order_list_request_filters_current_contract():
    class RawApi:
        def __init__(self):
            self.calls = []

        def list_futures_orders(self, settle, status, **kwargs):
            self.calls.append((settle, status, kwargs))
            return []

    raw = RawApi()
    api = futures_grid.FuturesGridApi(raw, "usdt")
    assert api.list_orders("SOXL_USDT", "open") == []
    assert raw.calls == [
        ("usdt", "open", {"contract": "SOXL_USDT"})
    ]


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


def test_futures_buy_fill_does_not_place_take_profit_outside_window():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = core.GridStore(Path(temp_dir) / "futures-grid.sqlite3")
        try:
            store.insert_buy(
                "SOXL_USDT", "code",
                SimpleNamespace(id="buy-1", text="t-fbcode-buy"),
                core.PlannedOrder(Decimal("100"), Decimal("2")),
                Decimal("100.3"),
            )
            raw_api = FakeFuturesApi()
            listener = futures_grid.FuturesOrderListener(
                core.threading.Event(), store,
                futures_grid.FuturesGridApi(raw_api, "usdt"),
                "SOXL_USDT", "code", contract_spec().grid_spec,
                Decimal("0.003"), "fb", "ft", "wss://example.test/ws",
                core.PriceState(Decimal("50")), Decimal("100"),
                Decimal("0.01"), 20, 10, user_id="123",
            )
            listener._receive_event({
                "id": "buy-1",
                "contract": "SOXL_USDT",
                "size": "2",
                "left": "0",
                "price": "100",
                "status": "finished",
                "finish_as": "filled",
                "text": "t-fbcode-buy",
            })
            row = store.active("SOXL_USDT", "code")[0]
            assert row["buy_status"] == core.BUY_FILLED
            assert row["tp_status"] == core.TP_WAIT_PLACE
            assert row["tp_order_id"] is None
            assert raw_api.created == []
        finally:
            store.close()


def test_futures_grid_uses_shared_atomic_activity_limit():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = core.GridStore(Path(temp_dir) / "futures-grid.sqlite3")
        try:
            raw_api = FakeFuturesApi()
            api = futures_grid.FuturesGridApi(raw_api, "usdt")
            planned = [core.PlannedOrder(Decimal("100"), Decimal("2"))]
            for _ in range(2):
                assert core.place_missing_buys(
                    api, store, "SOXL_USDT", "code", planned,
                    Decimal("0.003"), contract_spec().grid_spec,
                    False, "fb",
                )
            assert len(raw_api.created) == 1
            assert store.activity_count(
                "SOXL_USDT", "code", Decimal("100")
            ) == 1
        finally:
            store.close()


def test_zero_actual_position_closes_stale_filled_cycles():
    class EmptyPositionApi(FakeFuturesApi):
        def get_position(self, settle, contract, **kwargs):
            return SimpleNamespace(size="0")

    with tempfile.TemporaryDirectory() as temp_dir:
        store = core.GridStore(Path(temp_dir) / "futures-grid.sqlite3")
        try:
            row_id = store.insert_buy(
                "SOXL_USDT", "code",
                SimpleNamespace(id="buy-1", text="t-fbcode-buy"),
                core.PlannedOrder(Decimal("100"), Decimal("2")),
                Decimal("100.3"),
            )
            store.execute(
                "UPDATE grid_cycles SET buy_status=?,remaining_size=?,tp_status=? "
                "WHERE id=?",
                (core.BUY_FILLED, "2", core.TP_WAIT_PLACE, row_id),
            )
            api = futures_grid.FuturesGridApi(EmptyPositionApi(), "usdt")
            assert futures_grid.reconcile_futures_position(
                api, store, "SOXL_USDT", "code", "long"
            ) == Decimal("0")
            row = store.one(row_id)
            assert row["completed"]
            assert row["tp_status"] == futures_grid.TP_EXTERNALLY_CLOSED
            assert store.active("SOXL_USDT", "code") == []
        finally:
            store.close()


def test_smaller_actual_position_releases_stale_grid_activity():
    class OneContractPositionApi(FakeFuturesApi):
        def get_position(self, settle, contract, **kwargs):
            return SimpleNamespace(size="0.1")

    with tempfile.TemporaryDirectory() as temp_dir:
        store = core.GridStore(Path(temp_dir) / "futures-grid.sqlite3")
        try:
            row_ids = []
            for index, price in enumerate(("100", "99", "98"), 1):
                row_id = store.insert_buy(
                    "SOXL_USDT", "code",
                    SimpleNamespace(
                        id="buy-%s" % index, text="t-fbcode-%s" % index
                    ),
                    core.PlannedOrder(Decimal(price), Decimal("0.1")),
                    Decimal(price) + Decimal("0.3"),
                )
                store.execute(
                    "UPDATE grid_cycles SET buy_status=?,remaining_size=?,tp_status=? "
                    "WHERE id=?",
                    (core.BUY_FILLED, "0.1", core.TP_WAIT_PLACE, row_id),
                )
                row_ids.append(row_id)
            # 实际仓位只能覆盖一个周期时，优先保留已有真实止盈挂单的记录，
            # 其余数据库残留不能继续占用对应买入网格。
            store.execute(
                "UPDATE grid_cycles SET tp_order_id=?,tp_order_text=?,tp_status=? "
                "WHERE id=?",
                ("tp-live", "t-ftcode", core.TP_OPEN, row_ids[-1]),
            )

            api = futures_grid.FuturesGridApi(OneContractPositionApi(), "usdt")
            assert futures_grid.reconcile_futures_position(
                api, store, "SOXL_USDT", "code", "long"
            ) == Decimal("0.1")

            active = store.active("SOXL_USDT", "code")
            assert len(active) == 1
            assert active[0]["id"] == row_ids[-1]
            assert active[0]["tp_order_id"] == "tp-live"
            assert Decimal(active[0]["remaining_size"]) == Decimal("0.1")
            assert sum(store.one(row_id)["completed"] for row_id in row_ids) == 2
            assert store.activity_count(
                "SOXL_USDT", "code", Decimal("100")
            ) == 0
            assert store.activity_count(
                "SOXL_USDT", "code", Decimal("99")
            ) == 0
        finally:
            store.close()


def test_position_reconcile_keeps_only_partial_last_cycle():
    class PartialPositionApi(FakeFuturesApi):
        def get_position(self, settle, contract, **kwargs):
            return SimpleNamespace(size="0.15")

    with tempfile.TemporaryDirectory() as temp_dir:
        store = core.GridStore(Path(temp_dir) / "futures-grid.sqlite3")
        try:
            for index, price in enumerate(("100", "99", "98"), 1):
                row_id = store.insert_buy(
                    "SOXL_USDT", "code",
                    SimpleNamespace(id="buy-%s" % index, text="t-fbcode"),
                    core.PlannedOrder(Decimal(price), Decimal("0.1")),
                    Decimal(price) + Decimal("0.3"),
                )
                store.execute(
                    "UPDATE grid_cycles SET buy_status=?,remaining_size=?,tp_status=? "
                    "WHERE id=?",
                    (core.BUY_FILLED, "0.1", core.TP_WAIT_PLACE, row_id),
                )

            api = futures_grid.FuturesGridApi(PartialPositionApi(), "usdt")
            futures_grid.reconcile_futures_position(
                api, store, "SOXL_USDT", "code", "long"
            )

            sizes = sorted(
                Decimal(row["remaining_size"])
                for row in store.active("SOXL_USDT", "code")
            )
            assert sizes == [Decimal("0.05"), Decimal("0.1")]
            assert sum(sizes) == Decimal("0.15")
        finally:
            store.close()
