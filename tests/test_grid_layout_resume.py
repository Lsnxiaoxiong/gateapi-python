"""No network: verify resume/repricing against a real temporary SQLite database."""
from decimal import Decimal as D
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "example"))
import auto_grid_futures_generic as core
import auto_grid_contract_futures_short_generic as short


class GridLayoutResumeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "grid.sqlite3"
        self.store = core.GridStore(self.path)
        self.spec = core.PairSpec("ETH", "USDT", D("0.001"), D("0.01"),
                                  D("0.001"), D("0"), D("1000"), D("1000000"))
        self.settings = core.grid_layout_settings(D("100"), D("0.02"), D("0.02"),
                                                  D("5"), D("2"), "futures:usdt:long")
        self.api = Mock()
        self.api.cancel_order.side_effect = lambda *a, **k: self.result()
        self.reconciler = Mock(price_state=object())
        self.printer = patch("builtins.print")
        self.printer.start()

    def tearDown(self):
        self.printer.stop()
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def result(filled="0", status="cancelled", finish_as="cancelled"):
        return SimpleNamespace(status=status, filled_amount=filled, finish_as=finish_as)

    def insert(self, price="100", tp="101", filled=True, linked=False, pair="ETH_USDT"):
        order_id = str(len(self.store.rows("SELECT id FROM grid_cycles")) + 1)
        row_id = self.store.insert_buy(
            pair, "code", SimpleNamespace(id=order_id, text="open-" + order_id),
            core.PlannedOrder(D(price), D("2")), D(tp),
        )
        if filled:
            self.store.execute(
                "UPDATE grid_cycles SET buy_status=?,tp_status=?,tp_order_id=? WHERE id=?",
                (core.BUY_FILLED, core.TP_OPEN if linked else core.TP_WAIT_PLACE,
                 "tp-" + order_id if linked else None, row_id),
            )
        return row_id

    def prepare(self, settings=None):
        core.prepare_grid_resume(self.api, self.store, "ETH_USDT", "code",
                                 settings or self.settings, self.spec, self.reconciler)

    def test_first_upgrade_long_sorts_unique_targets_and_preserves_history(self):
        ids = [self.insert(tp=tp, linked=True) for tp in ("103", "101", "101")]
        self.prepare()
        rows = [self.store.one(i) for i in (ids[1], ids[2], ids[0])]
        self.assertEqual(["102.04", "104.12", "106.25"], [r["tp_price"] for r in rows])
        self.assertEqual(["100", "102.04", "104.12"], [r["grid_open_price"] for r in rows])
        for row in rows:
            self.assertEqual("100", row["buy_price"])
            self.assertEqual("2", row["buy_size"])
            self.assertEqual("2", row["remaining_size"])
            self.assertIsNone(row["tp_order_id"])
            self.assertEqual(core.TP_WAIT_PLACE, row["tp_status"])
        self.assertEqual(3, self.api.cancel_order.call_count)

    def test_short_targets_descend_from_highest_and_reserve_higher_opening(self):
        self.settings["market"] = "futures:usdt:short"
        ids = [self.insert(tp=t) for t in ("99", "101", "100")]
        self.prepare()
        rows = [self.store.one(i) for i in (ids[1], ids[2], ids[0])]
        self.assertEqual(["100", "98.04", "96.12"], [r["tp_price"] for r in rows])
        self.assertEqual(["102", "100", "98.04"], [r["grid_open_price"] for r in rows])

    def test_same_spacing_and_amount_or_profit_changes_do_not_reprice(self):
        row_id = self.insert(linked=True)
        self.prepare()
        self.store.execute("UPDATE grid_cycles SET tp_order_id='new-tp',tp_status=? WHERE id=?",
                           (core.TP_OPEN, row_id))
        original = dict(self.store.one(row_id))
        changed = dict(self.settings, margin="50", tp_pct="0.04", leverage="3")
        self.api.cancel_order.reset_mock()
        self.prepare(changed)
        self.assertEqual(original, dict(self.store.one(row_id)))
        self.api.cancel_order.assert_not_called()

    def test_spacing_change_reprices_once_even_across_database_reopen(self):
        row_id = self.insert()
        self.prepare()
        changed = dict(self.settings, step_pct="0.03")
        self.prepare(changed)
        row = dict(self.store.one(row_id))
        self.assertEqual("103.09", row["tp_price"])
        self.store.close()
        self.store = core.GridStore(self.path)
        self.prepare(changed)
        self.assertEqual(row, dict(self.store.one(row_id)))

    def test_partial_tp_fill_changes_remaining_only(self):
        row_id = self.insert(linked=True)
        self.api.cancel_order.return_value = self.result("0.5")
        self.api.cancel_order.side_effect = None
        self.prepare()
        row = self.store.one(row_id)
        self.assertEqual("2", row["buy_size"])
        self.assertEqual("1.5", row["remaining_size"])
        self.assertEqual("102.04", row["tp_price"])

    def test_tp_filled_while_cancelling_is_completed_not_reopened(self):
        row_id = self.insert(linked=True)
        self.api.cancel_order.side_effect = lambda *a, **k: self.result("2", "closed", "filled")
        self.prepare()
        row = self.store.one(row_id)
        self.assertTrue(row["completed"])
        self.assertEqual("0", row["remaining_size"])
        self.assertIsNone(row["grid_open_price"])

    def test_failed_cancel_does_not_commit_prices_and_resume_finishes(self):
        first = self.insert(linked=True)
        second = self.insert(tp="103", linked=True)
        self.api.cancel_order.side_effect = [self.result("0.5"), TimeoutError("unknown")]
        with self.assertRaises(TimeoutError):
            self.prepare()
        self.assertEqual("101", self.store.one(first)["tp_price"])
        self.assertEqual("103", self.store.one(second)["tp_price"])
        self.assertTrue(self.store.rows("SELECT pending_settings FROM grid_layouts")[0][0])
        self.store.close()
        self.store = core.GridStore(self.path)
        self.api.cancel_order.side_effect = lambda *a, **k: self.result()
        self.prepare()
        self.assertEqual("1.5", self.store.one(first)["remaining_size"])
        self.assertEqual("102.04", self.store.one(first)["tp_price"])
        self.assertIsNone(self.store.rows("SELECT pending_settings FROM grid_layouts")[0][0])

    def test_conflicting_pending_partial_fill_is_included_in_plan(self):
        self.insert()
        pending = self.insert(filled=False)
        self.api.cancel_order.side_effect = lambda *a, **k: self.result("0.5")
        self.prepare()
        row = self.store.one(pending)
        self.assertEqual(core.BUY_FILLED, row["buy_status"])
        self.assertEqual("0.5", row["remaining_size"])
        self.assertEqual("104.12", row["tp_price"])
        self.assertEqual("102.04", row["grid_open_price"])

    def test_nonconflicting_new_grid_pending_order_keeps_size_and_id(self):
        self.insert()
        pending = self.insert(price="98", tp="99", filled=False)
        before = dict(self.store.one(pending))
        self.prepare()
        self.assertEqual(before, dict(self.store.one(pending)))

    def test_old_off_grid_pending_is_cancelled_not_moved_or_resized(self):
        pending = self.insert(price="99", filled=False)
        self.prepare()
        self.assertIsNone(self.store.one(pending))
        self.api.cancel_order.assert_called_once()

    def test_repriced_slot_is_exclusive_and_released_on_close(self):
        row_id = self.insert(price="99")
        self.prepare()
        planned = core.PlannedOrder(D("100"), D("3"))
        self.assertEqual(0, self.store.activity_count("ETH_USDT", "code", D("99")))
        self.assertEqual(1, self.store.activity_count("ETH_USDT", "code", D("100")))
        self.assertIsNone(self.store.reserve_buy_slot("ETH_USDT", "code", planned,
                                                     D("102.04"), "new"))
        # A different opening grid must not share an existing reserved TP price either.
        self.assertIsNone(self.store.reserve_buy_slot(
            "ETH_USDT", "code", core.PlannedOrder(D("98"), D("3")), D("102.04"), "new2"))
        self.store.execute("UPDATE grid_cycles SET completed=1,tp_status=? WHERE id=?",
                           (core.TP_FILLED, row_id))
        self.assertIsNotNone(self.store.reserve_buy_slot("ETH_USDT", "code", planned,
                                                        D("102.04"), "new3"))

    def test_long_and_short_order_placement_skip_reserved_slot_without_api_call(self):
        for short_side in (False, True):
            with self.subTest(short=short_side):
                self.store.clear_pair("ETH_USDT")
                self.settings["market"] = "futures:usdt:short" if short_side else "spot:long"
                row_id = self.insert()
                self.prepare()
                price = D(self.store.one(row_id)["grid_open_price"])
                args = (self.api, self.store, "ETH_USDT", "code",
                        [core.PlannedOrder(price, D("3"))], D("0.02"), self.spec)
                if short_side:
                    short.place_missing_shorts(*args, "ss")
                else:
                    core.place_missing_buys(*args, False, "db")
                    core.place_crossed_market_buys(*args[:5], D("30"), *args[5:], False, "db")
        self.api.create_order.assert_not_called()

    def test_pair_isolation(self):
        other = self.insert(pair="BTC_USDT", linked=True)
        before = dict(self.store.one(other))
        self.insert()
        self.prepare()
        self.assertEqual(before, dict(self.store.one(other)))

    def test_no_take_profit_submission_during_startup_reconciliation(self):
        original = self.reconciler.price_state
        self.reconciler.reconcile_rest.side_effect = lambda **kw: self.assertIsNone(self.reconciler.price_state)
        self.prepare()
        self.assertIs(original, self.reconciler.price_state)

    def test_unknown_submission_blocks_migration_instead_of_releasing_slot(self):
        row_id = self.insert()
        self.store.execute("UPDATE grid_cycles SET tp_status=? WHERE id=?", (core.TP_SUBMITTING, row_id))
        with self.assertRaisesRegex(RuntimeError, "待核对"):
            self.prepare()
        self.assertEqual("101", self.store.one(row_id)["tp_price"])
        self.api.cancel_order.assert_not_called()

    def test_account_or_direction_change_is_not_silently_resumed(self):
        self.prepare()
        with self.assertRaisesRegex(RuntimeError, "多空方向"):
            self.prepare(dict(self.settings, market="futures:usdt:short"))

    def test_reset_clears_layout_metadata(self):
        self.prepare()
        self.store.clear_pair("ETH_USDT")
        self.assertEqual([], self.store.rows("SELECT * FROM grid_layouts"))

    def test_schema_upgrade_preserves_existing_database(self):
        self.store.close()
        # Recreate the pre-feature schema by removing the optional coordinate only.
        db = sqlite3.connect(str(self.path))
        db.execute("DROP INDEX idx_grid_slot")
        db.execute("ALTER TABLE grid_cycles DROP COLUMN grid_open_price")
        db.execute("DROP TABLE grid_layouts")
        db.commit()
        db.close()
        self.store = core.GridStore(self.path)
        self.insert()
        self.prepare()
        self.assertEqual("100", self.store.active("ETH_USDT", "code")[0]["grid_open_price"])

    def test_pending_open_fill_during_cancel_uses_actual_quantity(self):
        pending = self.insert(price="99", tp="100", filled=False)
        self.api.cancel_order.side_effect = lambda *a, **k: self.result("2", "closed", "filled")
        self.prepare()
        row = self.store.one(pending)
        self.assertEqual(core.BUY_FILLED, row["buy_status"])
        self.assertEqual("99", row["buy_price"])
        self.assertEqual("2", row["remaining_size"])
        self.assertEqual("102.04", row["tp_price"])

    def test_grouped_tp_cancellation_does_not_double_count_fills(self):
        ids = [self.insert(linked=True), self.insert(tp="103", linked=True)]
        self.store.execute("UPDATE grid_cycles SET tp_order_id='shared',tp_order_text='shared'")
        self.api.cancel_order.side_effect = lambda *a, **k: self.result("2.5")
        self.prepare()
        self.api.cancel_order.assert_called_once()
        self.assertTrue(self.store.one(ids[0])["completed"])
        self.assertEqual("1.5", self.store.one(ids[1])["remaining_size"])

    def test_cancel_not_confirmed_preserves_old_prices(self):
        row_id = self.insert(linked=True)
        self.api.cancel_order.side_effect = lambda *a, **k: self.result(status="open")
        with self.assertRaisesRegex(RuntimeError, "撤单未确认"):
            self.prepare()
        self.assertEqual("101", self.store.one(row_id)["tp_price"])
        self.assertIsNone(self.store.one(row_id)["grid_open_price"])

    def test_new_opening_uses_new_amount_without_resizing_old_pending(self):
        old = self.insert(price="98", tp="99", filled=False)
        self.prepare()
        changed = dict(self.settings, margin="50")
        self.prepare(changed)
        size = core.calculate_order_size(D("50"), D("2"), D("96.04"), self.spec)
        with patch.object(core, "create_limit_order", return_value=SimpleNamespace(id="new", text="new")) as create:
            core.place_missing_buys(
                self.api, self.store, "ETH_USDT", "code", [core.PlannedOrder(D("96.04"), size)],
                D("0.02"), self.spec, False, "db",
            )
        self.assertEqual(size, create.call_args.args[4])
        self.assertEqual("2", self.store.one(old)["buy_size"])

    def test_second_connection_cannot_reserve_migrated_slot(self):
        self.insert()
        self.prepare()
        other = core.GridStore(self.path)
        try:
            self.assertIsNone(other.reserve_buy_slot(
                "ETH_USDT", "code", core.PlannedOrder(D("100"), D("1")), D("102"), "new",
            ))
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
