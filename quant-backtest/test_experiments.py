from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engine import Costs, run_strategy
from experiments import CAPITAL, MARKET_COSTS, Grid, Intraday, Portfolio
from market_data import BAR_MS, MINUTE_MS, Candle, load_candles
from run_sma_comparison import HOUR_MS, Setup, simulate
from run_sma_early_entry import early_targets
from strategies import STRATEGIES


class ExecutionTests(unittest.TestCase):
    def test_zero_volume_is_not_used_for_entry_or_settlement(self) -> None:
        t = 1_577_836_800_000
        data = (
            "open_time,open,high,low,close,volume\n"
            f"{t},100,100,100,100,0\n"
            f"{t + MINUTE_MS},110,112,109,112,1\n"
            f"{t + BAR_MS},121,122,120,122,2\n"
            f"{t + BAR_MS + MINUTE_MS},122,122,122,122,0\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.csv"
            path.write_text(data, encoding="ascii")
            candles = load_candles(path, None)
        result = run_strategy(STRATEGIES[-1], candles, 0, CAPITAL, Costs(0, 0))
        trade, = result.trades
        self.assertEqual((trade.entry.time, trade.entry.market_price), (t + MINUTE_MS, 110))
        self.assertEqual((trade.exit_time, trade.exit_market_price), (t + BAR_MS + MINUTE_MS - 1, 122))

    def test_market_adjustment_includes_costs_in_target_weight(self) -> None:
        account = Portfolio("test")
        for target in (1, 0.4, 0.75, 0):
            previous_equity = account.equity(100)
            account.adjust(100, target)
            self.assertLess(account.equity(100), previous_equity)
            self.assertAlmostEqual(account.units * 100 / account.equity(100), target)
            self.assertGreaterEqual(account.cash, -1e-8)

    def test_grid_reset_waits_for_next_minute_and_accounts_for_inventory_loss(self) -> None:
        grid = Grid("test", True)
        month = (2020, 1)
        grid.on_minute(month, 100, 101, 89, 90)
        self.assertEqual(grid.account.fills, 0)
        grid.on_minute(month, 90, 101, 89, 90)
        self.assertEqual(grid.resets, 1)
        self.assertEqual(grid.account.fills, 0)
        grid.on_minute(month, 90, 100, 80, 80)
        self.assertTrue(grid.reset_pending)
        self.assertEqual(grid.account.fills, 10)
        self.assertEqual(grid.closed_pairs, 0)
        grid.on_minute(month, 78, 78, 78, 78)
        self.assertEqual(grid.resets, 2)
        self.assertEqual(grid.account.fills, 11)
        self.assertAlmostEqual(grid.account.units, 0)
        self.assertAlmostEqual(grid.account.cash - CAPITAL, grid.reset_pnl)

    def test_intraday_uses_completed_signal_and_same_window_baseline(self) -> None:
        strategy = Intraday()
        strategy.on_minute(0, 13 * 60 + 30, 100, 110)
        strategy.on_minute(0, 13 * 60 + 59, 110, 90)
        strategy.on_minute(0, 20 * 60 + 31, 100, 100)
        self.assertEqual(strategy.signal.fills, 0)
        self.assertEqual(strategy.control.fills, 1)
        strategy.on_minute(0, 21 * 60 + 4, 105, 105)
        self.assertEqual(strategy.control.fills, 2)
        self.assertAlmostEqual(strategy.control.units, 0)

    def test_sma_short_reverses_at_the_next_tradable_open(self) -> None:
        t = 1_577_836_800_000
        candles = []
        for index, (opening, closing) in enumerate(((100, 100), (100, 90), (90, 90), (85, 110), (111, 105))):
            start = t + index * HOUR_MS
            end = start + HOUR_MS - 1
            if index == 2:
                candle = Candle(start, end, opening, opening, closing, closing, 59, expected_minutes=60)
            else:
                candle = Candle(start, end, opening, max(opening, closing), min(opening, closing), closing,
                                60, (start, opening), (end, closing), 60)
            candles.append(candle)
        run = simulate(candles, Setup(1, 1, 2, True))
        short, long = run.trades
        self.assertEqual(short.position.side, -1)
        self.assertEqual(short.position.opened, candles[3].open_time)
        self.assertEqual(short.closed, candles[4].open_time)
        self.assertEqual(long.position.opened, short.closed)
        self.assertEqual(run.account.fills, 4)
        fee, slip = MARKET_COSTS.fee, MARKET_COSTS.slippage
        sale = 85 * (1 - slip) * (1 - fee)
        cover = 111 * (1 + slip) * (1 + fee)
        units = CAPITAL / (85 + 85 - sale)
        self.assertAlmostEqual(short.net_pnl, units * (sale - cover))

    def test_early_sma_signal_requires_convergence_and_reverts_outside_band(self) -> None:
        gaps = [None, 0, -.005, -.004, -.003, -.0051, .03, .004, .003, .004, .0051, -.006]
        self.assertEqual(
            list(early_targets(gaps, .005, .005)),
            [False, False, False, True, True, False, True, False, False, False, True, False],
        )

    def test_early_sma_entry_and_exit_thresholds_are_independent(self) -> None:
        gaps = [-.009, -.004, -.0009, .0009, .004, .0009, -.009]
        self.assertEqual(list(early_targets(gaps, 0, 0)), [False, False, False, True, True, True, False])
        self.assertEqual(list(early_targets(gaps, .005, 0)), [False, True, True, True, True, True, False])
        self.assertEqual(list(early_targets(gaps, 0, .005)), [False, False, False, True, True, False, False])


if __name__ == "__main__":
    unittest.main()
