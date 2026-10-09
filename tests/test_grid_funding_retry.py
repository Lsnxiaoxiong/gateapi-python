"""Offline regressions for insufficient margin; never start trading clients."""
import ast
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "example"))
import auto_grid_futures_generic as core
import auto_grid_contract_futures_generic as long_grid
import auto_grid_contract_futures_short_generic as short_grid
from gate_api.exceptions import ApiException, GateApiException


class FundingRetryTest(unittest.TestCase):
    def test_rejected_open_releases_slot_preserves_orders_and_can_retry(self):
        for direction in ("long", "short"):
            for label in core.FUNDING_ERROR_LABELS:
                with self.subTest(direction=direction, label=label):
                    with tempfile.TemporaryDirectory() as directory:
                        store = core.GridStore(Path(directory) / "grid.sqlite3")
                        try:
                            spec = core.PairSpec(
                                "ETH", "USDT", Decimal("1"), Decimal("0.01"),
                                Decimal("1"), Decimal("0"),
                                Decimal("1000"), Decimal("1000000"),
                            )
                            old_id = store.insert_buy(
                                "ETH_USDT", "code", SimpleNamespace(id="old", text="old"),
                                core.PlannedOrder(Decimal("99"), Decimal("1")),
                                Decimal("100"),
                            )
                            store.execute(
                                "UPDATE grid_cycles SET buy_status=?,tp_status=?,"
                                "tp_order_id='existing-tp' WHERE id=?",
                                (core.BUY_FILLED, core.WAIT_TP, old_id),
                            )
                            existing = dict(store.one(old_id))
                            args = (
                                Mock(), store, "ETH_USDT", "code",
                                [core.PlannedOrder(Decimal("100"), Decimal("1"))],
                                Decimal("0.003"), spec,
                            )
                            if direction == "long":
                                place = core.place_missing_buys
                                target, method = core, "create_limit_order"
                                args += (False, "db", 1)
                            else:
                                place = short_grid.place_missing_shorts
                                target, method = short_grid, "create_futures_limit_order"
                                args += ("db", 1)
                            error = GateApiException(
                                label, "insufficient margin", exp=ApiException(status=400),
                            )
                            with patch.object(target, method, side_effect=error) as create:
                                for _ in range(6):
                                    self.assertFalse(place(*args))
                                    self.assertEqual(0, store.activity_count(
                                        "ETH_USDT", "code", Decimal("100"),
                                    ))
                                    self.assertEqual(existing, dict(store.one(old_id)))
                                self.assertEqual(6, create.call_count)
                            with patch.object(target, method, return_value=SimpleNamespace(
                                id="new", text="new",
                            )):
                                self.assertTrue(place(*args))
                            self.assertEqual(1, store.activity_count(
                                "ETH_USDT", "code", Decimal("100"),
                            ))
                            self.assertEqual(existing, dict(store.one(old_id)))
                        finally:
                            store.close()

    def run_main_loop(self, module, function, outcomes, max_cycles):
        # Execute the actual loop and its initializers, excluding live setup/threads.
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
        body = next(n.body for n in reversed(run.body) if isinstance(n, ast.Try) and n.finalbody)
        start = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "cycle" for t in n.targets))
        script = compile(ast.fix_missing_locations(ast.Module(
            body=body[start:], type_ignores=[],
        )), module.__file__, "exec")
        ticks = [0.0]
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = lambda seconds: ticks.__setitem__(0, ticks[0] + seconds)
        placement = Mock(side_effect=outcomes)
        store = Mock()
        store.active.return_value = []
        price_state = Mock()
        env = dict(vars(module))
        env.update(
            stop=stop, max_cycles=max_cycles, apis=[Mock()], contract="ETH_USDT",
            code="code", price_state=price_state, reference=Decimal("100"),
            step_pct=Decimal("0.003"), buy_grid_count=10, short_grid_count=10,
            margin=Decimal("1"), leverage_value=Decimal("2"),
            contract_spec=SimpleNamespace(grid_spec=Mock()), store=store,
            tp_pct=Decimal("0.003"), buy_client_order_prefix="db",
            open_client_order_prefix="db", max_unclosed_per_grid=1,
            take_profit_grid_count=20, take_profit_count=10, poll_interval=1,
            time=SimpleNamespace(monotonic=lambda: ticks[0]),
            desired_buy_orders=Mock(return_value=[]),
            desired_short_orders=Mock(return_value=[]),
            short_take_profit_window_lower=Mock(return_value=Decimal("90")),
            place_missing_shorts=placement,
        )
        with patch.object(core, "get_last_price", return_value=Decimal("100")), \
                patch.object(core, "take_profit_window_upper", return_value=Decimal("110")), \
                patch.object(core, "place_missing_buys", placement), patch("builtins.print"):
            exec(script, env)
        return env, placement, price_state, store, stop

    def test_main_loops_retry_after_cooldown_without_stopping_after_five_shortages(self):
        for module, function in ((long_grid, "run_grid"), (short_grid, "run_short_grid")):
            with self.subTest(module=module.__name__):
                env, placement, prices, store, stop = self.run_main_loop(
                    module, function, [False] * 6 + [True], 181,
                )
                self.assertEqual(7, placement.call_count)  # t=0,30,...,180
                self.assertEqual(181, prices.set.call_count)
                self.assertEqual(181, store.active.call_count)
                self.assertEqual(0, env["failures"])
                stop.set.assert_not_called()

    def test_unrelated_errors_still_trigger_original_safety_stop(self):
        for module, function in ((long_grid, "run_grid"), (short_grid, "run_short_grid")):
            with self.subTest(module=module.__name__):
                error = GateApiException(
                    "INVALID_PARAM_VALUE", "bad order", exp=ApiException(status=400),
                )
                with self.assertRaisesRegex(RuntimeError, "连续 5 轮失败") as caught:
                    self.run_main_loop(module, function, error, 10)
                self.assertIs(error, caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
