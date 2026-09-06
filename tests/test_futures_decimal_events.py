"""小数合约数量丢失时不得释放网格占位。全部使用本地模拟。"""
import sys
import tempfile
import threading
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "example"))
import auto_grid_futures_generic as core
import auto_grid_contract_futures_generic as long_grid
import auto_grid_contract_futures_short_generic as short_grid


class DecimalEventsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = core.GridStore(Path(self.temp.name) / "test.sqlite3")
        self.spec = core.PairSpec("ETH", "USDT", D("0.1"), D("0.01"),
                                  D("0.1"), D("0"), D("10000"), D("Infinity"))
        self.row_id = self.store.insert_buy(
            "ETH_USDT", "code", N(id="open", text="t-sscode-test"),
            core.PlannedOrder(D("2510.01"), D("0.1")), D("2504.98"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def listener(self, cls):
        return cls(threading.Event(), self.store, object(), "ETH_USDT", "code",
                   self.spec, D("0.002"), "ss", "sb", "wss://test", user_id="test")

    def test_short_zero_push_then_rest_fill_keeps_slot(self):
        listener = self.listener(short_grid.ShortOrderListener)
        event = listener._normalise_event(dict(
            id="open", text="t-sscode-test", contract="ETH_USDT",
            size=0, left=0, status="finished", finish_as="filled", price="2510.01"))
        self.assertFalse(listener.handle_order_update(event))
        self.assertEqual(1, self.store.activity_count("ETH_USDT", "code", D("2510.01")))
        order = short_grid.ShortFuturesGridApi._normalise(N(
            id="open", text="t-sscode-test", contract="ETH_USDT", size="-0.1",
            left="0", status="finished", finish_as="filled", price="2510.01"))
        self.assertTrue(listener.handle_order_update(listener._event_from_order(order)))
        self.assertEqual(core.BUY_FILLED, self.store.one(self.row_id)["buy_status"])
        self.assertEqual("0.1", self.store.one(self.row_id)["remaining_size"])

    def test_contradictory_filled_zero_does_not_delete(self):
        core.mark_buy_result(self.store, self.store.one(self.row_id),
                             N(status="closed", finish_as="filled", filled_amount="0"))
        self.assertIsNotNone(self.store.one(self.row_id))

    def test_filled_position_survives_stale_zero_cancel(self):
        self.store.execute("UPDATE grid_cycles SET buy_status=? WHERE id=?",
                           (core.BUY_FILLED, self.row_id))
        core.mark_buy_result(self.store, self.store.one(self.row_id),
                             N(status="cancelled", finish_as="cancelled", filled_amount="0"))
        self.assertEqual(core.BUY_FILLED, self.store.one(self.row_id)["buy_status"])

    def test_real_unfilled_cancel_still_releases_slot(self):
        core.mark_buy_result(self.store, self.store.one(self.row_id),
                             N(status="cancelled", finish_as="cancelled", filled_amount="0"))
        self.assertIsNone(self.store.one(self.row_id))

    def test_zero_filled_cover_preserves_order_link(self):
        self.store.execute("UPDATE grid_cycles SET buy_status=?,tp_status=?,tp_order_id='tp' WHERE id=?",
                           (core.BUY_FILLED, core.TP_OPEN, self.row_id))
        core.mark_tp_result(self.store, self.store.one(self.row_id),
                            N(status="closed", finish_as="filled", filled_amount="0"))
        self.assertEqual("tp", self.store.one(self.row_id)["tp_order_id"])

    def test_rest_verified_zero_cancel_releases_incorrect_filled_slot(self):
        self.store.execute("UPDATE grid_cycles SET buy_status=? WHERE id=?",
                           (core.BUY_FILLED, self.row_id))
        result = N(id="open", status="cancelled", finish_as="cancelled",
                   filled_amount="0", amount="0.1", left="0.1", _rest_snapshot=True)
        api = N(get_order=Mock(return_value=result))
        core.apply_buy_update(api, self.store, "ETH_USDT", self.store.one(self.row_id), result)
        api.get_order.assert_called_once()
        self.assertIsNone(self.store.one(self.row_id))

    def test_anomaly_is_included_in_exact_rest_pending_legs(self):
        self.store.execute("UPDATE grid_cycles SET buy_status=? WHERE id=?",
                           (core.BUY_FILLED, self.row_id))
        core.mark_buy_result(self.store, self.store.one(self.row_id),
                             N(status="closed", finish_as="filled", filled_amount="0"))
        self.assertEqual("buy", self.store.pending_legs("ETH_USDT", "code", D("2500"))[0][1])

    def test_failed_rest_verification_keeps_slot(self):
        self.store.execute("UPDATE grid_cycles SET buy_status=? WHERE id=?",
                           (core.BUY_FILLED, self.row_id))
        result = N(status="cancelled", finish_as="cancelled", filled_amount="0", _rest_snapshot=True)
        api = N(get_order=Mock(side_effect=RuntimeError("unavailable")))
        with self.assertRaises(RuntimeError):
            core.apply_buy_update(api, self.store, "ETH_USDT", self.store.one(self.row_id), result)
        self.assertIsNotNone(self.store.one(self.row_id))

    def test_zero_cancel_clears_cover_before_releasing_slot(self):
        self.store.execute("UPDATE grid_cycles SET buy_status=?,tp_status=?,tp_order_id='tp' WHERE id=?",
                           (core.BUY_FILLED, core.TP_OPEN, self.row_id))
        result = N(id="open", status="cancelled", finish_as="cancelled", filled_amount="0",
                   amount="0.1", left="0.1", _rest_snapshot=True)
        api = N(get_order=Mock(return_value=result), cancel_order=Mock(return_value=result))
        core.apply_buy_update(api, self.store, "ETH_USDT", self.store.one(self.row_id), result)
        self.assertEqual("tp", api.cancel_order.call_args.args[0])
        self.assertIsNone(self.store.one(self.row_id))

    def test_both_futures_connections_request_decimal_sizes(self):
        for cls in (short_grid.ShortOrderListener, long_grid.FuturesOrderListener):
            with self.subTest(listener=cls.__name__):
                listener = self.listener(cls)
                socket = Mock()
                socket.send.side_effect = lambda _: listener.stop.set()
                connect = Mock(return_value=socket)
                with patch.dict(sys.modules, {"websocket": N(create_connection=connect)}):
                    listener.run("dummy", "dummy")
                self.assertEqual({"X-Gate-Size-Decimal": "1"}, connect.call_args.kwargs["header"])


if __name__ == "__main__":
    unittest.main()
