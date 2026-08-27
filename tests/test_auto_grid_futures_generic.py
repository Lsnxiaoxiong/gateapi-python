from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
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
        self.next_order_id = 0
        self.spec = grid.PairSpec(
            "ETH", "USDT", Decimal("0.001"), Decimal("0.01"),
            Decimal("0.001"), Decimal("3"), Decimal("1000"), Decimal("1000000")
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def gate_order(self, order_id=None, text="t-test"):
        if order_id is None:
            self.next_order_id += 1
            order_id = str(self.next_order_id)
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
        self.assertEqual(grid.POSITION_NO_TP, row["position_status"])
        self.assertEqual(grid.TP_NOT_SET, row["tp_status"])
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
        self.assertEqual(grid.POSITION_WAIT_TP_ORDER, updated["position_status"])
        self.assertEqual(grid.TP_WAIT_PLACE, updated["tp_status"])

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
        self.assertEqual(grid.POSITION_TP_FILLED, completed["position_status"])
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
        self.assertEqual(grid.TP_WAIT_PLACE, updated["tp_status"])

    def test_duplicate_partial_take_profit_event_is_idempotent(self):
        self.insert()
        row = self.store.active("ETH_USDT", "code")[0]
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=?,tp_order_id='2' "
            "WHERE id=?",
            (grid.BUY_FILLED, grid.TP_OPEN, row["id"]),
        )
        stale = self.store.active("ETH_USDT", "code")[0]
        result = SimpleNamespace(
            status="cancelled", finish_as="cancelled", filled_amount="0.01"
        )
        grid.mark_tp_result(self.store, stale, result)
        grid.mark_tp_result(self.store, stale, result)
        updated = self.store.active("ETH_USDT", "code")[0]
        self.assertEqual("0.02", updated["remaining_size"])
        self.assertIsNone(updated["tp_order_id"])

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

    def test_activity_count_includes_pending_and_filled_unclosed(self):
        pending = self.insert(price="100")
        filled = self.insert(price="100")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,position_status=?,tp_status=? "
            "WHERE id=?",
            (
                grid.BUY_FILLED, grid.POSITION_WAIT_TP_ORDER,
                grid.TP_WAIT_PLACE, filled,
            ),
        )
        self.assertIsNotNone(self.store.one(pending))
        self.assertEqual(1, self.store.exposure_count(
            "ETH_USDT", "code", Decimal("100")
        ))
        self.assertEqual(2, self.store.activity_count(
            "ETH_USDT", "code", Decimal("100")
        ))

    def test_buy_slot_is_reserved_atomically_before_gate_order(self):
        planned = grid.PlannedOrder(Decimal("100"), Decimal("0.03"))
        first = self.store.reserve_buy_slot(
            "ETH_USDT", "code", planned, Decimal("100.3"),
            "t-dbcode-first", 1,
        )
        second = self.store.reserve_buy_slot(
            "ETH_USDT", "code", planned, Decimal("100.3"),
            "t-dbcode-second", 1,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(1, self.store.activity_count(
            "ETH_USDT", "code", Decimal("100")
        ))
        self.store.release_buy_reservation(first)
        self.assertEqual(0, self.store.activity_count(
            "ETH_USDT", "code", Decimal("100")
        ))

    def test_place_buy_reserves_activity_before_calling_gate(self):
        test_case = self

        class FakeApi:
            def create_order(self, order, **kwargs):
                self_activity = test_case.store.activity_count(
                    "ETH_USDT", "code", Decimal("100")
                )
                test_case.assertEqual(1, self_activity)
                return test_case.gate_order(text=order.text)

        funded = grid.place_missing_buys(
            FakeApi(), self.store, "ETH_USDT", "code",
            [grid.PlannedOrder(Decimal("100"), Decimal("0.03"))],
            Decimal("0.003"), self.spec, True, "db",
            max_unclosed_per_grid=1,
        )
        self.assertTrue(funded)
        row = self.store.active("ETH_USDT", "code")[0]
        self.assertIsNotNone(row["buy_order_id"])
        self.assertEqual(1, self.store.activity_count(
            "ETH_USDT", "code", Decimal("100")
        ))

    def test_uncertain_gate_error_keeps_buy_reservation(self):
        class FakeApi:
            def create_order(self, *args, **kwargs):
                raise TimeoutError("request outcome unknown")

        with self.assertRaises(TimeoutError):
            grid.place_missing_buys(
                FakeApi(), self.store, "ETH_USDT", "code",
                [grid.PlannedOrder(Decimal("100"), Decimal("0.03"))],
                Decimal("0.003"), self.spec, True, "db",
                max_unclosed_per_grid=1,
            )
        row = self.store.active("ETH_USDT", "code")[0]
        self.assertIsNone(row["buy_order_id"])
        self.assertEqual(1, self.store.activity_count(
            "ETH_USDT", "code", Decimal("100")
        ))

    def test_order_event_binds_gate_id_to_pre_submit_reservation(self):
        planned = grid.PlannedOrder(Decimal("100"), Decimal("0.03"))
        row_id = self.store.reserve_buy_slot(
            "ETH_USDT", "code", planned, Decimal("100.3"),
            "t-dbcode-reserved", 1,
        )
        listener = grid.OrderListener(
            grid.threading.Event(), self.store, object(),
            "ETH_USDT", "code", self.spec, Decimal("0.003"),
            "db", "dt", "wss://example.test/ws",
        )
        handled = listener.handle_order_update({
            "id": "gate-order-1", "text": "t-dbcode-reserved",
            "currency_pair": "ETH_USDT", "account": "margin",
            "side": "buy", "status": "open", "filled_amount": "0",
            "amount": "0.03", "left": "0.03", "price": "100",
        })
        self.assertTrue(handled)
        row = self.store.one(row_id)
        self.assertEqual("gate-order-1", row["buy_order_id"])
        self.assertEqual(1, self.store.activity_count(
            "ETH_USDT", "code", Decimal("100")
        ))

    def test_main_buy_placement_skips_grid_with_two_unfinished_exposures(self):
        first = self.insert(price="100")
        second = self.insert(price="100")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,position_status=?,tp_status=? "
            "WHERE id IN (?,?)",
            (
                grid.BUY_FILLED, grid.POSITION_WAIT_TP_ORDER,
                grid.TP_WAIT_PLACE, first, second,
            ),
        )

        class FakeApi:
            def create_order(self, *args, **kwargs):
                raise AssertionError("同一网格已有2个未止盈持仓时不应继续挂买单")

        funded = grid.place_missing_buys(
            FakeApi(), self.store, "ETH_USDT", "code",
            [grid.PlannedOrder(Decimal("100"), Decimal("0.03"))],
            Decimal("0.003"), self.spec, True, "db",
        )
        self.assertTrue(funded)
        self.assertEqual(2, self.store.exposure_count(
            "ETH_USDT", "code", Decimal("100")
        ))

    def test_configured_limit_three_allows_third_grid_cycle(self):
        first = self.insert(price="100")
        second = self.insert(price="100")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,position_status=?,tp_status=? "
            "WHERE id IN (?,?)",
            (
                grid.BUY_FILLED, grid.POSITION_WAIT_TP_ORDER,
                grid.TP_WAIT_PLACE, first, second,
            ),
        )

        test_case = self

        class FakeApi:
            def create_order(self, *args, **kwargs):
                return test_case.gate_order()

        funded = grid.place_missing_buys(
            FakeApi(), self.store, "ETH_USDT", "code",
            [grid.PlannedOrder(Decimal("100"), Decimal("0.03"))],
            Decimal("0.003"), self.spec, True, "db",
            max_unclosed_per_grid=3,
        )
        self.assertTrue(funded)
        self.assertEqual(3, len(self.store.active("ETH_USDT", "code")))

    def test_grid_checker_cancels_pending_buy_after_two_exposures_accumulate(self):
        first = self.insert(price="100")
        second = self.insert(price="100")
        pending = self.insert(price="100")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,position_status=?,tp_status=? "
            "WHERE id IN (?,?)",
            (
                grid.BUY_FILLED, grid.POSITION_WAIT_TP_ORDER,
                grid.TP_WAIT_PLACE, first, second,
            ),
        )

        class FakeApi:
            def cancel_order(self, *args, **kwargs):
                return SimpleNamespace(status="cancelled", filled_amount="0")

        grid.trim_outside_grid(
            FakeApi(), self.store, "ETH_USDT", "code", {Decimal("100")}
        )
        self.assertIsNone(self.store.one(pending))
        self.assertEqual(2, self.store.exposure_count(
            "ETH_USDT", "code", Decimal("100")
        ))

    def test_grid_checker_cancels_duplicate_pending_buy_above_activity_limit(self):
        first = self.insert(price="100")
        second = self.insert(price="100")

        class FakeApi:
            def __init__(self):
                self.cancelled = []

            def cancel_order(self, order_id, *args, **kwargs):
                self.cancelled.append(str(order_id))
                return SimpleNamespace(status="cancelled", filled_amount="0")

        api = FakeApi()
        second_order_id = str(self.store.one(second)["buy_order_id"])
        grid.trim_outside_grid(
            api, self.store, "ETH_USDT", "code", {Decimal("100")},
            max_unclosed_per_grid=1,
        )
        self.assertEqual([second_order_id], api.cancelled)
        self.assertIsNotNone(self.store.one(first))
        self.assertIsNone(self.store.one(second))
        self.assertEqual(1, self.store.activity_count(
            "ETH_USDT", "code", Decimal("100")
        ))

    def test_pending_legs_sorted_by_distance(self):
        self.insert(price="90")
        self.insert(price="99")
        legs = self.store.pending_legs("ETH_USDT", "code", Decimal("100"))
        self.assertEqual(Decimal("99"), legs[0][2])

    def test_all_open_buys_outside_current_window_are_trimmed(self):
        low = self.insert(price="90")
        high = self.insert(price="110")

        class FakeApi:
            def cancel_order(self, *args, **kwargs):
                return SimpleNamespace(status="cancelled", filled_amount="0")

        grid.trim_outside_grid(
            FakeApi(), self.store, "ETH_USDT", "code",
            {Decimal("100"), Decimal("99")}
        )
        ids = {row["id"] for row in self.store.active("ETH_USDT", "code")}
        self.assertNotIn(low, ids)
        self.assertNotIn(high, ids)

    def test_partially_filled_trimmed_buy_is_preserved(self):
        row_id = self.insert(price="90", size="0.03")

        class FakeApi:
            def cancel_order(self, *args, **kwargs):
                return SimpleNamespace(status="cancelled", filled_amount="0.01")

        grid.trim_outside_grid(
            FakeApi(), self.store, "ETH_USDT", "code", {Decimal("100")}
        )
        row = self.store.rows("SELECT * FROM grid_cycles WHERE id=?", (row_id,))[0]
        self.assertEqual(grid.BUY_FILLED, row["buy_status"])
        self.assertEqual(grid.TP_WAIT_PLACE, row["tp_status"])
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
        self.assertEqual(grid.TP_WAIT_PLACE, far["tp_status"])

    def test_tp_window_keeps_nearest_ten_and_cancels_two_farthest(self):
        row_ids = []
        for index, price in enumerate(range(91, 103), start=1):
            row_id = self.insert(price=str(price), size="0.04")
            row_ids.append(row_id)
            self.store.execute(
                "UPDATE grid_cycles SET buy_status=?,position_status=?,"
                "tp_status=?,tp_order_id=? WHERE id=?",
                (
                    grid.BUY_FILLED, grid.POSITION_WAIT_TP, grid.TP_OPEN,
                    f"tp-{index}", row_id,
                ),
            )

        class FakeApi:
            def __init__(self):
                self.cancelled = []

            def cancel_order(self, order_id, *args, **kwargs):
                self.cancelled.append(str(order_id))
                return SimpleNamespace(status="cancelled", filled_amount="0")

        api = FakeApi()
        grid.reconcile_take_profits(
            api, self.store, "ETH_USDT", "code",
            Decimal("90"), Decimal("120"), self.spec, "dt", 10,
        )
        rows = [self.store.one(row_id) for row_id in row_ids]
        self.assertEqual(10, sum(row["tp_status"] == grid.TP_OPEN for row in rows))
        self.assertEqual(2, sum(
            row["tp_status"] == grid.TP_WAIT_PLACE for row in rows
        ))
        self.assertEqual(["tp-12", "tp-11"], api.cancelled)

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
        self.assertEqual(grid.TP_WAIT_PLACE, row["tp_status"])
        self.assertIsNone(row["tp_order_id"])

    def test_subminimum_take_profit_is_submitted_at_original_tp_price(self):
        row_id = self.insert(price="100", size="0.004")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=? WHERE id=?",
            (grid.BUY_FILLED, grid.TP_NOT_SET, row_id),
        )

        class FakeApi:
            def create_order(self, order, **kwargs):
                self.order = order
                return SimpleNamespace(id="dust-tp", text=order.text)

        api = FakeApi()
        grid.reconcile_take_profits(
            api, self.store, "ETH_USDT", "code",
            Decimal("99"), Decimal("105"), self.spec, "dt", 10,
        )
        row = self.store.rows(
            "SELECT * FROM grid_cycles WHERE id=?", (row_id,)
        )[0]
        self.assertEqual(grid.TP_OPEN, row["tp_status"])
        self.assertEqual("100.3", api.order.price)
        self.assertEqual("0.004", row["remaining_size"])
        self.assertFalse(row["completed"])

    def test_dust_take_profit_does_not_consume_normal_tp_limit(self):
        dust_id = self.insert(price="100", size="0.004")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=? WHERE id=?",
            (grid.BUY_FILLED, grid.TP_DUST, dust_id),
        )
        for price in range(101, 111):
            row_id = self.insert(price=str(price), size="0.03")
            self.store.execute(
                "UPDATE grid_cycles SET buy_status=?,tp_status=? WHERE id=?",
                (grid.BUY_FILLED, grid.TP_WAIT_PLACE, row_id),
            )

        class FakeApi:
            def __init__(self):
                self.orders = []

            def create_order(self, order, **kwargs):
                self.orders.append(order)
                return SimpleNamespace(
                    id=f"tp-{len(self.orders)}", text=order.text
                )

        api = FakeApi()
        grid.reconcile_take_profits(
            api, self.store, "ETH_USDT", "code",
            Decimal("90"), Decimal("120"), self.spec, "dt", 10,
        )
        self.assertEqual(11, len(api.orders))
        self.assertEqual(
            11,
            sum(
                row["tp_status"] == grid.TP_OPEN
                for row in self.store.active("ETH_USDT", "code")
            ),
        )

    def test_gate_minimum_rejection_is_not_retried_as_open_order(self):
        row_id = self.insert(price="100", size="0.004")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,tp_status=? WHERE id=?",
            (grid.BUY_FILLED, grid.TP_DUST, row_id),
        )

        class FakeApi:
            def create_order(self, *args, **kwargs):
                parent = grid.ApiException(status=400, reason="Bad Request")
                raise grid.GateApiException(
                    "INVALID_PARAM_VALUE", "order amount is too small", exp=parent
                )

        grid.reconcile_take_profits(
            FakeApi(), self.store, "ETH_USDT", "code",
            Decimal("99"), Decimal("105"), self.spec, "dt", 10,
        )
        row = self.store.one(row_id)
        self.assertEqual(grid.TP_DUST_REJECTED, row["tp_status"])
        self.assertIsNone(row["tp_order_id"])

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
        self.assertTrue(all(item[0] == "ETH_USDT" for item in api.statuses))
        self.assertTrue(all(item[2]["async_req"] for item in api.statuses))

    def test_spot_order_snapshot_uses_spot_account(self):
        class Job:
            def get(self):
                return []

        class FakeApi:
            _grid_account_mode = "spot"

            def __init__(self):
                self.accounts = []

            def list_orders(self, pair, status, **kwargs):
                self.accounts.append(kwargs["account"])
                return Job()

        api = FakeApi()
        grid.list_order_snapshot(api, "ETH5L_USDT")
        self.assertEqual(["spot", "spot"], api.accounts)

    def test_spot_limit_order_disables_auto_borrow(self):
        class FakeApi:
            _grid_account_mode = "spot"

            def create_order(self, body, **kwargs):
                self.body = body
                return SimpleNamespace(id="1")

        api = FakeApi()
        grid.create_limit_order(
            api, "ETH5L_USDT", "buy", Decimal("0.03"), Decimal("100"),
            True, "db", "code",
        )
        self.assertEqual("spot", api.body.account)
        self.assertFalse(api.body.auto_borrow)

    def test_reconciliation_ignores_orders_from_another_pair(self):
        self.insert(price="100", size="0.03")

        class Job:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeApi:
            def list_orders(self, pair, status, **kwargs):
                foreign = SimpleNamespace(
                    id="1", text="t-test", currency_pair="BTC_USDT",
                    account="margin", side="buy", status="closed",
                    amount="0.03", left="0", filled_amount="0.03",
                    price="100",
                )
                return Job([foreign] if status == "finished" else [])

        listener = grid.OrderListener(
            grid.threading.Event(), self.store, FakeApi(),
            "ETH_USDT", "code", self.spec, Decimal("0.003"),
            "db", "dt", "wss://example.test/ws",
        )
        listener.reconcile_rest()
        row = self.store.active("ETH_USDT", "code")[0]
        self.assertEqual(grid.BUY_OPEN, row["buy_status"])

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

    def test_order_listener_drives_buy_fill_to_take_profit_fill(self):
        self.insert(price="100", size="0.03")

        class FakeApi:
            def __init__(self):
                self.created = []

            def create_order(self, order, **kwargs):
                self.created.append(order)
                return SimpleNamespace(id="tp-1", text=order.text)

        api = FakeApi()
        listener = grid.OrderListener(
            grid.threading.Event(), self.store, api, "ETH_USDT", "code",
            self.spec, Decimal("0.003"), "db", "dt", "wss://example.test/ws",
            grid.PriceState(Decimal("99")), Decimal("100"), Decimal("0.01"),
            20, 10,
        )
        handled = listener.handle_order_update({
            "id": "1",
            "text": "t-test",
            "currency_pair": "ETH_USDT",
            "account": "margin",
            "side": "buy",
            "status": "closed",
            "filled_amount": "0.03",
            "amount": "0.03",
            "left": "0",
        })
        self.assertTrue(handled)
        self.assertEqual(1, len(api.created))
        row = self.store.active("ETH_USDT", "code")[0]
        self.assertEqual(grid.BUY_FILLED, row["buy_status"])
        self.assertEqual(grid.POSITION_WAIT_TP, row["position_status"])
        self.assertEqual(grid.TP_OPEN, row["tp_status"])
        self.assertEqual("tp-1", row["tp_order_id"])

        # 重复成交推送必须幂等，不能重复挂止盈。
        listener.handle_order_update({
            "id": "1", "text": "t-test", "currency_pair": "ETH_USDT",
            "account": "margin", "side": "buy", "status": "closed",
            "filled_amount": "0.03", "amount": "0.03", "left": "0",
        })
        self.assertEqual(1, len(api.created))

        listener.handle_order_update({
            "id": "tp-1",
            "text": api.created[0].text,
            "currency_pair": "ETH_USDT",
            "account": "margin",
            "side": "sell",
            "status": "closed",
            "filled_amount": "0.03",
            "amount": "0.03",
            "left": "0",
        })
        completed = self.store.rows(
            "SELECT * FROM grid_cycles WHERE id=?", (row["id"],)
        )[0]
        self.assertTrue(completed["completed"])
        self.assertEqual(grid.POSITION_TP_FILLED, completed["position_status"])
        self.assertEqual(grid.TP_FILLED, completed["tp_status"])

    def test_buy_fill_does_not_submit_take_profit_outside_twenty_grid_window(self):
        self.insert(price="100", size="0.03")

        class FakeApi:
            def create_order(self, *args, **kwargs):
                raise AssertionError("20格止盈窗口外不应直接挂止盈单")

        listener = grid.OrderListener(
            grid.threading.Event(), self.store, FakeApi(),
            "ETH_USDT", "code", self.spec, Decimal("0.003"),
            "db", "dt", "wss://example.test/ws",
            grid.PriceState(Decimal("50")), Decimal("100"), Decimal("0.01"),
            20, 10,
        )
        listener.handle_order_update({
            "id": "1", "text": "t-test", "currency_pair": "ETH_USDT",
            "account": "margin", "side": "buy", "status": "closed",
            "filled_amount": "0.03", "amount": "0.03", "left": "0",
        })
        row = self.store.active("ETH_USDT", "code")[0]
        self.assertEqual(grid.BUY_FILLED, row["buy_status"])
        self.assertEqual(grid.TP_WAIT_PLACE, row["tp_status"])
        self.assertIsNone(row["tp_order_id"])

    def test_rest_reconciliation_recovers_missed_buy_and_take_profit_fills(self):
        self.insert(price="100", size="0.03")

        class Job:
            def __init__(self, result):
                self.result = result

            def get(self):
                return self.result

        class FakeApi:
            def __init__(self):
                self.phase = "buy-filled"
                self.created = []

            @staticmethod
            def order(order_id, side, status, filled, text, price):
                return SimpleNamespace(
                    id=order_id, text=text, currency_pair="ETH_USDT",
                    account="margin", side=side, status=status,
                    finish_as="filled" if status == "closed" else "",
                    amount="0.03", left="0" if status == "closed" else "0.03",
                    filled_amount=filled, price=price,
                )

            def list_orders(self, pair, status, **kwargs):
                buy = self.order(
                    "1", "buy", "closed", "0.03", "t-test", "100"
                )
                tp = self.order(
                    "tp-1", "sell", "closed", "0.03",
                    self.created[0].text if self.created else "t-dtcode",
                    "100.3",
                )
                rows = []
                if status == "finished":
                    rows = [buy] if self.phase == "buy-filled" else [buy, tp]
                return Job(rows)

            def create_order(self, order, **kwargs):
                self.created.append(order)
                return SimpleNamespace(id="tp-1", text=order.text)

            def get_order(self, order_id, pair, **kwargs):
                return self.order(
                    "tp-1", "sell", "open", "0", self.created[0].text,
                    "100.3",
                )

        api = FakeApi()
        reconciler = grid.OrderListener(
            grid.threading.Event(), self.store, api, "ETH_USDT", "code",
            self.spec, Decimal("0.003"), "db", "dt",
            "wss://example.test/ws",
            grid.PriceState(Decimal("99")), Decimal("100"), Decimal("0.01"),
            20, 10,
        )
        reconciler.reconcile_rest(exact_missing=True)
        row = self.store.active("ETH_USDT", "code")[0]
        self.assertEqual(grid.BUY_FILLED, row["buy_status"])
        self.assertEqual(grid.TP_OPEN, row["tp_status"])
        self.assertEqual("tp-1", row["tp_order_id"])

        api.phase = "tp-filled"
        reconciler.reconcile_rest(exact_missing=True)
        completed = self.store.rows(
            "SELECT * FROM grid_cycles WHERE id=?", (row["id"],)
        )[0]
        self.assertTrue(completed["completed"])
        self.assertEqual(grid.TP_FILLED, completed["tp_status"])

    def test_order_state_checker_runs_one_exact_reconciliation(self):
        stop = grid.threading.Event()

        class FakeReconciler:
            def __init__(self):
                self.calls = []
                self.pair = "ETH_USDT"

            def reconcile_rest(self, exact_missing=False):
                self.calls.append(exact_missing)
                stop.set()

        reconciler = FakeReconciler()
        checker = grid.OrderStateChecker(stop, reconciler, interval=1)
        checker.run()
        self.assertEqual([True], reconciler.calls)

    def test_order_state_checker_recovers_unwritten_open_take_profit(self):
        row_id = self.insert(price="100", size="0.03")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,position_status=?,tp_status=? "
            "WHERE id=?",
            (
                grid.BUY_FILLED, grid.POSITION_WAIT_TP_ORDER,
                grid.TP_SUBMITTING, row_id,
            ),
        )
        listener = grid.OrderListener(
            grid.threading.Event(), self.store, object(), "ETH_USDT", "code",
            self.spec, Decimal("0.003"), "db", "dt",
            "wss://example.test/ws",
        )
        listener._receive_event({
            "id": "tp-recovered",
            "text": "t-dtcode-recovered",
            "currency_pair": "ETH_USDT",
            "account": "margin",
            "side": "sell",
            "status": "open",
            "filled_amount": "0",
            "amount": "0.03",
            "left": "0.03",
            "price": "100.3",
        })
        row = self.store.one(row_id)
        self.assertEqual("tp-recovered", row["tp_order_id"])
        self.assertEqual(grid.TP_OPEN, row["tp_status"])
        self.assertEqual(grid.POSITION_WAIT_TP, row["position_status"])

    def test_order_listener_idempotently_adds_missing_open_buy_record(self):
        listener = grid.OrderListener(
            grid.threading.Event(), self.store, object(), "ETH_USDT", "code",
            self.spec, Decimal("0.005"), "db", "dt",
            "wss://example.test/ws",
        )
        event = {
            "id": "missing-buy",
            "text": "t-dbcode-recovered",
            "currency_pair": "ETH_USDT",
            "account": "margin",
            "side": "buy",
            "status": "open",
            "filled_amount": "0",
            "amount": "0.03",
            "left": "0.03",
            "price": "100",
        }
        listener._receive_event(event)
        rows = self.store.active("ETH_USDT", "code")
        self.assertEqual(1, len(rows))
        self.assertEqual(grid.BUY_OPEN, rows[0]["buy_status"])
        self.assertEqual(grid.POSITION_NO_TP, rows[0]["position_status"])
        self.assertEqual(grid.TP_NOT_SET, rows[0]["tp_status"])
        self.assertEqual("100.5", rows[0]["tp_price"])

        same_id = self.store.insert_buy(
            "ETH_USDT", "code",
            SimpleNamespace(id="missing-buy", text="t-dbcode-recovered"),
            grid.PlannedOrder(Decimal("100"), Decimal("0.03")),
            Decimal("100.5"),
        )
        self.assertEqual(rows[0]["id"], same_id)
        self.assertEqual(1, len(self.store.active("ETH_USDT", "code")))

    def test_take_profit_claim_prevents_duplicate_submission(self):
        row_id = self.insert(price="100", size="0.03")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,position_status=?,tp_status=? "
            "WHERE id=?",
            (
                grid.BUY_FILLED, grid.POSITION_WAIT_TP_ORDER,
                grid.TP_WAIT_PLACE, row_id,
            ),
        )
        self.assertTrue(self.store.claim_take_profit(row_id))
        self.assertFalse(self.store.claim_take_profit(row_id))

    def test_take_profit_write_is_idempotent_when_listener_attached_same_order(self):
        row_id = self.insert(price="100", size="0.03")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,remaining_size=?,position_status=?,"
            "tp_status=? WHERE id=?",
            (
                grid.BUY_FILLED, "0.03", grid.POSITION_WAIT_TP_ORDER,
                grid.TP_WAIT_PLACE, row_id,
            ),
        )
        self.assertTrue(self.store.claim_take_profit(row_id))
        attached = self.store.attach_missing_take_profit(
            "ETH_USDT", "code", "tp-same", "t-dtcode-same",
            Decimal("100.3"), Decimal("0.03"),
        )
        self.assertIsNotNone(attached)

        self.store.set_take_profit_open(
            row_id, SimpleNamespace(id="tp-same", text="t-dtcode-same")
        )
        row = self.store.one(row_id)
        self.assertEqual("tp-same", row["tp_order_id"])
        self.assertEqual(grid.TP_OPEN, row["tp_status"])

    def test_take_profit_write_still_rejects_a_different_attached_order(self):
        row_id = self.insert(price="100", size="0.03")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,remaining_size=?,position_status=?,"
            "tp_status=? WHERE id=?",
            (
                grid.BUY_FILLED, "0.03", grid.POSITION_WAIT_TP_ORDER,
                grid.TP_WAIT_PLACE, row_id,
            ),
        )
        self.assertTrue(self.store.claim_take_profit(row_id))
        self.store.attach_missing_take_profit(
            "ETH_USDT", "code", "tp-listener", "t-dtcode-listener",
            Decimal("100.3"), Decimal("0.03"),
        )

        with self.assertRaisesRegex(RuntimeError, "当前订单=tp-listener"):
            self.store.set_take_profit_open(
                row_id, SimpleNamespace(id="tp-creator", text="t-dtcode-creator")
            )

    def test_stale_take_profit_claim_can_be_recovered_after_reconciliation(self):
        row_id = self.insert(price="100", size="0.03")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,position_status=?,tp_status=?,"
            "updated_at=0 WHERE id=?",
            (
                grid.BUY_FILLED, grid.POSITION_WAIT_TP_ORDER,
                grid.TP_SUBMITTING, row_id,
            ),
        )
        recovered = self.store.recover_stale_take_profit_claims(
            "ETH_USDT", "code", 60
        )
        self.assertEqual(1, recovered)
        row = self.store.one(row_id)
        self.assertEqual(grid.TP_WAIT_PLACE, row["tp_status"])
        self.assertEqual(grid.POSITION_WAIT_TP_ORDER, row["position_status"])

    def test_order_listener_subscription_is_signed_for_current_pair(self):
        listener = grid.OrderListener(
            grid.threading.Event(), self.store, object(), "ETH_USDT", "code",
            self.spec, Decimal("0.003"), "db", "dt", "wss://example.test/ws",
        )
        with patch.object(grid.time, "time", return_value=1234567890):
            message = json.loads(listener._subscription("key", "secret"))
        expected = hmac.new(
            b"secret",
            b"channel=spot.orders&event=subscribe&time=1234567890",
            hashlib.sha512,
        ).hexdigest()
        self.assertEqual(["ETH_USDT"], message["payload"])
        self.assertEqual("spot.orders", message["channel"])
        self.assertEqual(expected, message["auth"]["SIGN"])

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
        changed_limit = grid.strategy_code(*args, max_unclosed_per_grid=3)
        self.assertNotEqual(first, changed_limit)
        spot = grid.strategy_code(*args, account_mode="spot")
        self.assertNotEqual(first, spot)

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

    def test_reset_ignores_orders_already_missing_from_gate(self):
        self.insert(price="100")
        tp_row_id = self.insert(price="99")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,position_status=?,tp_status=?,"
            "tp_order_id='missing-tp' WHERE id=?",
            (
                grid.BUY_FILLED, grid.POSITION_WAIT_TP, grid.TP_OPEN, tp_row_id,
            ),
        )

        class FakeApi:
            def __init__(self):
                self.cancelled = []

            def cancel_order(self, order_id, *args, **kwargs):
                self.cancelled.append(str(order_id))
                parent = grid.ApiException(status=404, reason="Not Found")
                raise grid.GateApiException(
                    "ORDER_NOT_FOUND", "Order not found", exp=parent
                )

            def get_order(self, *args, **kwargs):
                raise AssertionError("reset 模式不应再次查询已不存在的订单")

        api = FakeApi()
        grid.reset_pair(api, self.store, "ETH_USDT")
        self.assertEqual(2, len(api.cancelled))
        self.assertEqual(
            [], self.store.rows(
                "SELECT * FROM grid_cycles WHERE currency_pair='ETH_USDT'"
            )
        )

    def test_reconciliation_requeues_take_profit_missing_from_gate(self):
        row_id = self.insert(price="100", size="0.03")
        self.store.execute(
            "UPDATE grid_cycles SET buy_status=?,position_status=?,tp_status=?,"
            "tp_order_id='missing-tp' WHERE id=?",
            (
                grid.BUY_FILLED, grid.POSITION_WAIT_TP, grid.TP_OPEN, row_id,
            ),
        )

        class Job:
            def get(self):
                return []

        class FakeApi:
            def list_orders(self, *args, **kwargs):
                return Job()

            def get_order(self, *args, **kwargs):
                parent = grid.ApiException(status=404, reason="Not Found")
                raise grid.GateApiException(
                    "ORDER_NOT_FOUND", "Order not found", exp=parent
                )

            def cancel_order(self, *args, **kwargs):
                raise AssertionError("不存在的止盈单无需再次撤销")

        listener = grid.OrderListener(
            grid.threading.Event(), self.store, FakeApi(),
            "ETH_USDT", "code", self.spec, Decimal("0.003"),
            "db", "dt", "wss://example.test/ws",
        )
        listener.reconcile_rest(exact_missing=True)

        row = self.store.one(row_id)
        self.assertEqual(grid.TP_WAIT_PLACE, row["tp_status"])
        self.assertEqual(grid.POSITION_WAIT_TP_ORDER, row["position_status"])
        self.assertIsNone(row["tp_order_id"])

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

    def test_old_database_statuses_are_migrated_to_new_state_machine(self):
        path = Path(self.temp.name) / "old.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE grid_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                currency_pair TEXT NOT NULL,
                strategy_code TEXT NOT NULL,
                buy_price TEXT NOT NULL,
                buy_size TEXT NOT NULL,
                remaining_size TEXT NOT NULL,
                buy_order_id TEXT,
                buy_order_text TEXT,
                buy_status TEXT NOT NULL,
                tp_price TEXT NOT NULL,
                tp_order_id TEXT,
                tp_order_text TEXT,
                tp_status TEXT NOT NULL,
                in_grid INTEGER NOT NULL DEFAULT 1,
                completed INTEGER NOT NULL DEFAULT 0,
                last_checked_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO grid_cycles(
                currency_pair,strategy_code,buy_price,buy_size,remaining_size,
                buy_order_id,buy_status,tp_price,tp_status,created_at,updated_at
            ) VALUES
                ('ETH_USDT','code','100','0.03','0.03','buy-open',
                 '等待买单成交','100.3','已设置止盈单',0,0),
                ('ETH_USDT','code','99','0.03','0.03','buy-filled',
                 '买单成交','99.3','未设置止盈、等待设置止盈',0,0);
            """
        )
        db.commit()
        db.close()

        migrated = grid.GridStore(path)
        try:
            rows = migrated.active("ETH_USDT", "code")
            by_id = {row["buy_order_id"]: row for row in rows}
            self.assertEqual(grid.BUY_OPEN, by_id["buy-open"]["buy_status"])
            self.assertEqual(
                grid.POSITION_NO_TP, by_id["buy-open"]["position_status"]
            )
            self.assertEqual(grid.TP_NOT_SET, by_id["buy-open"]["tp_status"])
            self.assertEqual(grid.BUY_FILLED, by_id["buy-filled"]["buy_status"])
            self.assertEqual(
                grid.POSITION_WAIT_TP_ORDER,
                by_id["buy-filled"]["position_status"],
            )
            self.assertEqual(
                grid.TP_WAIT_PLACE, by_id["buy-filled"]["tp_status"]
            )
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
