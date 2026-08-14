from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "example"))
import auto_grid_futures_generic as grid


class DatabaseGridTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = grid.GridStore(Path(self.temp.name) / "grid.sqlite3")
        self.spec = grid.PairSpec(
            "ETH", "USDT", Decimal("0.001"), Decimal("0.01"),
            Decimal("0.001"), Decimal("3"), Decimal("1000"), Decimal("1000000")
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def gate_order(order_id="1", text="t-test"):
        return SimpleNamespace(id=order_id, text=text)

    def insert(self, pair="ETH_USDT", code="code", price="100", size="0.03"):
        return self.store.insert_buy(
            pair, code, self.gate_order(),
            grid.PlannedOrder(Decimal(price), Decimal(size)),
            Decimal(price) * Decimal("1.003"),
        )

    def test_database_separates_currency_pairs(self):
        self.insert("ETH_USDT", price="100")
        self.insert("BTC_USDT", price="200")
        self.assertEqual(1, len(self.store.active("ETH_USDT", "code")))
        self.assertEqual(1, len(self.store.active("BTC_USDT", "code")))

    def test_resume_can_detect_an_unfinished_different_strategy(self):
        self.insert("ETH_USDT", code="old-code", price="100")
        conflicts = self.store.unfinished_other_strategies(
            "ETH_USDT", "new-code"
        )
        self.assertEqual("old-code", conflicts[0]["strategy_code"])
        self.assertEqual(1, conflicts[0]["cycle_count"])

    def test_inserted_buy_has_requested_initial_states(self):
        self.insert()
        row = self.store.active("ETH_USDT", "code")[0]
        self.assertEqual(grid.WAIT_BUY, row["buy_status"])
        self.assertEqual(grid.TP_PLANNED, row["tp_status"])
        self.assertFalse(row["completed"])

    def test_buy_fill_advances_to_waiting_take_profit(self):
        self.insert()
        row = self.store.active("ETH_USDT", "code")[0]
        result = SimpleNamespace(
            status="closed", finish_as="filled", filled_amount="0.03"
        )
        grid.mark_buy_result(self.store, row, result)
        updated = self.store.active("ETH_USDT", "code")[0]
        self.assertEqual(grid.BUY_FILLED, updated["buy_status"])
        self.assertEqual(grid.TP_NOT_SET, updated["tp_status"])

    def test_take_profit_fill_completes_cycle(self):
        self.insert()
        row = self.store.active("ETH_USDT", "code")[0]
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=?,tp_order_id='2' WHERE id=?",
            (grid.BUY_FILLED, grid.WAIT_TP, row["id"]),
        )
        row = self.store.active("ETH_USDT", "code")[0]
        grid.mark_tp_result(
            self.store, row,
            SimpleNamespace(status="closed", finish_as="filled", filled_amount="0.03")
        )
        completed = self.store.rows("SELECT * FROM grid_cycles WHERE id=?", (row["id"],))[0]
        self.assertEqual(grid.TP_FILLED, completed["tp_status"])
        self.assertTrue(completed["completed"])

    def test_cancelled_zero_fill_buy_is_not_mistaken_for_a_fill(self):
        row_id = self.insert()
        row = self.store.active("ETH_USDT", "code")[0]
        grid.mark_buy_result(
            self.store, row,
            SimpleNamespace(status="closed", finish_as="cancelled", filled_amount="0"),
        )
        self.assertEqual(
            [], self.store.rows("SELECT * FROM grid_cycles WHERE id=?", (row_id,))
        )

    def test_cancelled_partial_take_profit_keeps_only_unsold_size(self):
        self.insert()
        row = self.store.active("ETH_USDT", "code")[0]
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=?,tp_order_id='2' WHERE id=?",
            (grid.BUY_FILLED, grid.WAIT_TP, row["id"]),
        )
        row = self.store.active("ETH_USDT", "code")[0]
        grid.mark_tp_result(
            self.store, row,
            SimpleNamespace(status="closed", finish_as="cancelled", filled_amount="0.01"),
        )
        updated = self.store.active("ETH_USDT", "code")[0]
        self.assertEqual("0.02", updated["remaining_size"])
        self.assertEqual(grid.TP_NOT_SET, updated["tp_status"])

    def test_same_price_two_unfinished_positions_blocks_new_buy(self):
        first = self.insert(price="100")
        second = self.insert(price="100")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=? WHERE id IN (?,?)",
            (grid.BUY_FILLED, grid.TP_NOT_SET, first, second),
        )
        self.assertEqual(2, self.store.exposure_count(
            "ETH_USDT", "code", Decimal("100")
        ))

    def test_pending_legs_sorted_by_distance(self):
        self.insert(price="90")
        self.insert(price="99")
        legs = self.store.pending_legs("ETH_USDT", "code", Decimal("100"))
        self.assertEqual(Decimal("99"), legs[0][2])

    def test_only_low_side_is_trimmed_when_window_moves(self):
        low = self.insert(price="90")
        high = self.insert(price="110")

        class FakeApi:
            def cancel_order(self, *args, **kwargs):
                return SimpleNamespace(filled_amount="0")

        grid.trim_outside_grid(
            FakeApi(), self.store, "ETH_USDT", "code",
            {Decimal("100"), Decimal("99")}
        )
        ids = {row["id"] for row in self.store.active("ETH_USDT", "code")}
        self.assertNotIn(low, ids)
        self.assertIn(high, ids)

    def test_partially_filled_trimmed_buy_is_preserved(self):
        row_id = self.insert(price="90", size="0.03")

        class FakeApi:
            def cancel_order(self, *args, **kwargs):
                return SimpleNamespace(filled_amount="0.01")

        grid.trim_outside_grid(
            FakeApi(), self.store, "ETH_USDT", "code", {Decimal("100")}
        )
        row = self.store.rows("SELECT * FROM grid_cycles WHERE id=?", (row_id,))[0]
        self.assertEqual(grid.BUY_FILLED, row["buy_status"])
        self.assertEqual(grid.TP_NOT_SET, row["tp_status"])
        self.assertEqual("0.01", row["remaining_size"])

    def test_grid_math_and_take_profit_rounding(self):
        orders = grid.desired_buy_orders(
            Decimal("100"), Decimal("101"), Decimal("0.01"), 10,
            Decimal("3"), Decimal("2"), self.spec
        )
        self.assertEqual(10, len(orders))
        self.assertTrue(all(order.price < Decimal("101") for order in orders))
        self.assertEqual(
            Decimal("100.30"),
            grid.take_profit_price(Decimal("100"), Decimal("0.003"), self.spec),
        )
        upper = grid.take_profit_window_upper(
            Decimal("100"), Decimal("99.5"), Decimal("0.01"), 20, self.spec
        )
        self.assertGreater(upper, Decimal("99.5"))

    def test_tp_above_current_is_eligible_even_when_buy_is_above_current(self):
        row_id = self.insert(price="100", size="0.03")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=? WHERE id=?",
            (grid.BUY_FILLED, grid.TP_NOT_SET, row_id),
        )

        class FakeApi:
            def create_order(self, order, **kwargs):
                return SimpleNamespace(id="tp-new", text=order.text)

        grid.reconcile_take_profits(
            FakeApi(), self.store, "ETH_USDT", "code",
            Decimal("99"), Decimal("105"), self.spec, "dt", 10,
        )
        row = self.store.active("ETH_USDT", "code")[0]
        self.assertEqual(grid.WAIT_TP, row["tp_status"])
        self.assertEqual("tp-new", row["tp_order_id"])

    def test_tp_limit_replaces_far_order_with_nearer_waiting_position(self):
        near_id = self.insert(price="100", size="0.03")
        far_id = self.insert(price="110", size="0.03")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=? WHERE id=?",
            (grid.BUY_FILLED, grid.TP_NOT_SET, near_id),
        )
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=?,tp_order_id='far' "
            "WHERE id=?",
            (grid.BUY_FILLED, grid.WAIT_TP, far_id),
        )

        class FakeApi:
            def cancel_order(self, *args, **kwargs):
                return SimpleNamespace(status="closed", filled_amount="0")

            def create_order(self, order, **kwargs):
                return SimpleNamespace(id="near", text=order.text)

        grid.reconcile_take_profits(
            FakeApi(), self.store, "ETH_USDT", "code",
            Decimal("99"), Decimal("120"), self.spec, "dt", 1,
        )
        near = self.store.rows(
            "SELECT * FROM grid_cycles WHERE id=?", (near_id,)
        )[0]
        far = self.store.rows(
            "SELECT * FROM grid_cycles WHERE id=?", (far_id,)
        )[0]
        self.assertEqual(grid.WAIT_TP, near["tp_status"])
        self.assertEqual(grid.TP_NOT_SET, far["tp_status"])

    def test_tp_outside_price_window_is_cancelled(self):
        far_id = self.insert(price="110", size="0.03")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=?,tp_order_id='far' "
            "WHERE id=?",
            (grid.BUY_FILLED, grid.WAIT_TP, far_id),
        )

        class FakeApi:
            def cancel_order(self, *args, **kwargs):
                return SimpleNamespace(status="closed", filled_amount="0")

        grid.reconcile_take_profits(
            FakeApi(), self.store, "ETH_USDT", "code",
            Decimal("99"), Decimal("105"), self.spec, "dt", 10,
        )
        row = self.store.rows(
            "SELECT * FROM grid_cycles WHERE id=?", (far_id,)
        )[0]
        self.assertEqual(grid.TP_NOT_SET, row["tp_status"])
        self.assertIsNone(row["tp_order_id"])

    def test_subminimum_take_profit_is_marked_as_dust_and_not_submitted(self):
        row_id = self.insert(price="100", size="0.004")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=? WHERE id=?",
            (grid.BUY_FILLED, grid.TP_NOT_SET, row_id),
        )

        class FakeApi:
            def create_order(self, *args, **kwargs):
                raise AssertionError("碎片止盈不应提交到交易所")

        grid.reconcile_take_profits(
            FakeApi(), self.store, "ETH_USDT", "code",
            Decimal("99"), Decimal("105"), self.spec, "dt", 10,
        )
        row = self.store.rows(
            "SELECT * FROM grid_cycles WHERE id=?", (row_id,)
        )[0]
        self.assertEqual(grid.TP_DUST, row["tp_status"])
        self.assertEqual("0.004", row["remaining_size"])
        self.assertFalse(row["completed"])

    def test_order_snapshot_uses_open_and_finished_list_queries(self):
        class Job:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeApi:
            def __init__(self):
                self.statuses = []

            def list_orders(self, pair, status, **kwargs):
                self.statuses.append((pair, status, kwargs))
                return Job([SimpleNamespace(id=status)])

        api = FakeApi()
        opened, finished = grid.list_order_snapshot(api, "ETH_USDT")
        self.assertEqual("open", opened[0].id)
        self.assertEqual("finished", finished[0].id)
        self.assertEqual(["open", "finished"], [item[1] for item in api.statuses])
        self.assertTrue(all(item[2]["async_req"] for item in api.statuses))

    def test_orphan_strategy_sell_is_cancelled_but_linked_sell_is_kept(self):
        row_id = self.insert(price="100", size="0.03")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=?,tp_order_id='linked' "
            "WHERE id=?",
            (grid.BUY_FILLED, grid.WAIT_TP, row_id),
        )

        class FakeApi:
            def __init__(self):
                self.cancelled = []

            def cancel_order(self, order_id, *args, **kwargs):
                self.cancelled.append(str(order_id))
                return SimpleNamespace(status="cancelled", filled_amount="0")

        orders = [
            SimpleNamespace(
                id="linked", side="sell", text="t-dtcode-linked",
                create_time=0, price="100.3", amount="0.03",
            ),
            SimpleNamespace(
                id="orphan", side="sell", text="t-dtcode-orphan",
                create_time=0, price="101", amount="0.03",
            ),
            SimpleNamespace(
                id="other", side="sell", text="t-other-strategy",
                create_time=0, price="102", amount="0.03",
            ),
        ]
        api = FakeApi()
        count = grid.cancel_orphan_take_profits(
            api, self.store, "ETH_USDT", "code", "dt", orders
        )
        self.assertEqual(1, count)
        self.assertEqual(["orphan"], api.cancelled)

    def test_new_orphan_sell_is_protected_during_database_write_grace(self):
        class FakeApi:
            def cancel_order(self, *args, **kwargs):
                raise AssertionError("保护期内不应撤单")

        order = SimpleNamespace(
            id="new", side="sell", text="t-dtcode-new",
            create_time=grid.time.time(), price="101", amount="0.03",
        )
        count = grid.cancel_orphan_take_profits(
            FakeApi(), self.store, "ETH_USDT", "code", "dt", [order]
        )
        self.assertEqual(0, count)

    def test_proxy_and_internal_retry_configuration(self):
        with patch.dict("os.environ", {"GATE_PROXY": "http://127.0.0.1:7890"}):
            config = grid.make_configuration("https://example.test")
        self.assertEqual("http://127.0.0.1:7890", config.proxy)
        self.assertIs(config.retries, False)

    def test_strategy_identity_changes_with_risk_parameters(self):
        args = (
            "ETH_USDT", Decimal("100"), Decimal("0.003"), Decimal("0.003"),
            Decimal("3"), Decimal("2"), "grid"
        )
        first = grid.strategy_code(*args)
        changed = grid.strategy_code(*args[:-2], Decimal("3"), args[-1])
        self.assertNotEqual(first, changed)

    def test_process_lock_rejects_second_instance(self):
        path = Path(self.temp.name) / "ETH_USDT.lock"
        first = grid.StrategyProcessLock(path)
        second = grid.StrategyProcessLock(path)
        first.acquire()
        try:
            with self.assertRaises(RuntimeError):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    def test_margin_startup_enables_auto_repay(self):
        class FakeMarginApi:
            def __init__(self):
                self.leverage = None
                self.auto_repay = None

            def set_user_market_leverage(self, setting, **kwargs):
                self.leverage = setting.leverage

            def set_auto_repay(self, status, **kwargs):
                self.auto_repay = status
                return SimpleNamespace(status=status)

        api = FakeMarginApi()
        grid.configure_margin_settings(
            api, "ETH_USDT", Decimal("2"), True
        )
        self.assertEqual("2", api.leverage)
        self.assertEqual("on", api.auto_repay)


if __name__ == "__main__":
    unittest.main()
