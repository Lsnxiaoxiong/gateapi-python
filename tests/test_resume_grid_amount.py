import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as N
from example import auto_grid_futures_generic as core
from example import auto_grid_contract_futures_short_generic as short


class ResumeAmountTest(unittest.TestCase):
    def test_changed_amount_keeps_existing_orders_and_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = core.GridStore(Path(tmp) / "grid.sqlite3")
            try:
                self.assertIsNone(core.resume_strategy_code(store, "ETH_USDT"))
                row_id = store.insert_buy(
                    "ETH_USDT", "80b7f14f", N(id="old", text="t-old"),
                    core.PlannedOrder(D("2500"), D("0.01")), D("2505"))
                generated = core.strategy_code(
                    "ETH_USDT", D("2500"), D("0.0022"), D("0.0022"),
                    D("8.3"), D("3"), "gdbgrid01", 1)
                self.assertNotEqual("80b7f14f", generated)
                self.assertEqual("80b7f14f", core.resume_strategy_code(store, "ETH_USDT"))
                self.assertEqual("80b7f14f", short.resume_short_strategy_code(store, "ETH_USDT"))
                self.assertEqual("0.01", store.one(row_id)["buy_size"])
                store.insert_buy("ETH_USDT", "other", N(id="second", text="t-second"),
                                 core.PlannedOrder(D("2500"), D("0.02")), D("2505"))
                with self.assertRaises(RuntimeError):
                    core.resume_strategy_code(store, "ETH_USDT")
            finally:
                store.close()
