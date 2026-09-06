# coding: utf-8
"""Tests for tools.rank_futures_yearly_gain using injected fakes (no network).

Run with: ``python -m pytest tests/test_rank_futures_yearly_gain.py`` or, without
pytest, ``python -m unittest tests.test_rank_futures_yearly_gain``.
"""
from __future__ import absolute_import

import sys
import unittest
from types import SimpleNamespace

PROJECT_ROOT = __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools import rank_futures_yearly_gain as rg
from tools import market_candlesticks as mc

#: A recent, hour-aligned "now" used as the fixed end time in tests. Using a
#: rolling value (rather than a hard-coded 2023 timestamp) keeps the 365-day
#: 1h look-back inside Gate's 10000-point ceiling no matter when the suite runs.
import time as _time
RECENT_NOW = (int(_time.time()) // mc.HOUR_SECONDS) * mc.HOUR_SECONDS


def contract(name, last_price=None, in_delisting=False, status="trading", **kwargs):
    values = dict(name=name, last_price=last_price,
                  in_delisting=in_delisting, status=status)
    values.update(kwargs)
    return SimpleNamespace(**values)


def candle(ts, low, close):
    return {"timestamp": ts, "open": close, "high": close, "low": low,
            "close": close, "volume": "1"}


class StubCandlesApi(object):
    """FuturesApi stub: returns a scripted contract list and per-contract candles.

    ``candles_map`` maps contract name -> list of normalized candle dicts (so the
    rank module's candle fetch is short-circuited via a patched helper in tests
    that need it). For module-level integration, candle fetching goes through
    ``list_futures_candlesticks`` instead.
    """

    def __init__(self, contracts, candle_responses=None, contracts_error=False):
        self.contracts = contracts
        self.candle_responses = candle_responses or {}
        self.contracts_error = contracts_error
        self.candle_calls = []
        self.contract_list_kwargs = None

    def list_futures_contracts(self, settle, **kwargs):
        self.contract_list_kwargs = kwargs
        if self.contracts_error:
            raise RuntimeError("contract list unavailable")
        return list(self.contracts)

    def list_futures_candlesticks(self, settle, contract, **kwargs):
        self.candle_calls.append((settle, contract, kwargs))
        return self.candle_responses.get(contract, [])


def make_futures_candle(ts, low="1", high="2", open_p="1", close="1.5", v="10"):
    return SimpleNamespace(t=ts, v=v, c=close, h=high, l=low, o=open_p, sum="10")


class _ContractFilterTests(unittest.TestCase):

    def test_contract_list_request_uses_bounded_timeout(self):
        api = StubCandlesApi([])
        rg.rank_futures_by_yearly_low_gain(
            futures_api=api, max_workers=1, request_timeout=12, max_retries=0)
        self.assertEqual(api.contract_list_kwargs["_request_timeout"], 12)

    def test_real_sdk_style_contract_list_is_paginated_by_100(self):
        class PagedApi(object):
            def __init__(self):
                self.api_client = SimpleNamespace(call_api=lambda: None)
                self.contracts = [
                    contract("C%03d_USDT" % i, last_price="2")
                    for i in range(350)
                ]
                self.calls = []

            def list_futures_contracts(self, settle, **kwargs):
                self.calls.append(kwargs.copy())
                offset = kwargs["offset"]
                return self.contracts[offset:offset + kwargs["limit"]]

        api = PagedApi()
        rows = rg._list_futures_contracts(
            api, "usdt", 250, request_timeout=9, max_retries=0)
        self.assertEqual(len(rows), 250)
        self.assertEqual(
            [(call["offset"], call["limit"]) for call in api.calls],
            [(0, 100), (100, 100), (200, 50)],
        )
        self.assertTrue(all(call["_request_timeout"] == 9 for call in api.calls))

    def test_only_active_trading_non_delisting_contracts_scanned(self):
        api = StubCandlesApi([
            contract("AAA_USDT", last_price="2"),
            contract("BBB_USDT", last_price="2", status="delisted"),
            contract("CCC_USDT", last_price="2", in_delisting=True),
            contract("DDD_USDT", last_price="2", status=""),
        ])
        # Patch the candle fetch to return a single candle so scanning completes.
        original = rg.get_one_year_hourly_candlesticks

        def fake_fetch(market_type, trading_pair, **kwargs):
            return [candle(100, "1", "1.5")]
        rg.get_one_year_hourly_candlesticks = fake_fetch
        try:
            report = rg.rank_futures_by_yearly_low_gain(
                settle="usdt", futures_api=api, max_workers=1)
        finally:
            rg.get_one_year_hourly_candlesticks = original

        names = sorted(r["contract"] for r in report["results"])
        # status="" treated as active (missing/unknown); DDD included.
        self.assertEqual(names, ["AAA_USDT", "DDD_USDT"])


class _GainComputationTests(unittest.TestCase):

    def test_gain_formula_uses_year_low_and_current_price(self):
        c = contract("AAA_USDT", last_price="25")
        candles = [
            candle(100, "10", "12"),
            candle(200, "10", "20"),   # lowest low at 100 (first occurrence)
            candle(300, "15", "25"),
        ]
        result = rg._build_result(c, "usdt", candles)
        self.assertIsNotNone(result)
        self.assertEqual(result["contract"], "AAA_USDT")
        self.assertEqual(result["year_low"], "10")
        self.assertEqual(result["year_low_time"], 100)
        self.assertEqual(result["current_price"], "25")
        self.assertEqual(result["current_price_source"], rg.SOURCE_CONTRACT_LAST_PRICE)
        self.assertEqual(result["gain_pct"], "150")
        self.assertEqual(result["candle_count"], 3)
        self.assertEqual(result["data_start_time"], 100)
        self.assertEqual(result["data_end_time"], 300)

    def test_current_price_falls_back_to_latest_close(self):
        c = contract("AAA_USDT", last_price=None)
        candles = [candle(100, "10", "11"), candle(200, "12", "13")]
        result = rg._build_result(c, "usdt", candles)
        self.assertEqual(result["current_price"], "13")
        self.assertEqual(result["current_price_source"], rg.SOURCE_LATEST_CLOSE)

    def test_current_price_falls_back_for_zero_and_nonnumeric(self):
        for bad in ("0", "abc", "-5", ""):
            c = contract("AAA_USDT", last_price=bad)
            candles = [candle(100, "10", "7")]
            result = rg._build_result(c, "usdt", candles)
            self.assertEqual(result["current_price"], "7", msg="bad=%r" % bad)
            self.assertEqual(result["current_price_source"], rg.SOURCE_LATEST_CLOSE)

    def test_no_valid_price_returns_none(self):
        c = contract("AAA_USDT", last_price=None)
        # All closes invalid (zero) -> no current price.
        candles = [{"timestamp": 100, "open": "1", "high": "1", "low": "1",
                    "close": "0", "volume": "1"}]
        self.assertIsNone(rg._build_result(c, "usdt", candles))

    def test_year_low_time_uses_first_occurrence(self):
        c = contract("AAA_USDT", last_price="2")
        candles = [
            candle(100, "10", "1"),
            candle(200, "10", "1"),  # same low, later
            candle(300, "10", "1"),  # same low, latest
        ]
        result = rg._build_result(c, "usdt", candles)
        self.assertEqual(result["year_low_time"], 100)

    def test_negative_gain_returned_and_sorted(self):
        # current_price below year_low -> negative gain.
        c = contract("AAA_USDT", last_price="5")
        candles = [candle(100, "10", "8")]
        result = rg._build_result(c, "usdt", candles)
        self.assertEqual(result["gain_pct"], "-50")

    def test_minimum_order_value_uses_size_multiplier_and_current_price(self):
        c = contract("AAA_USDT", last_price="25", order_size_min="2",
                     quanto_multiplier="0.01")
        result = rg._build_result(c, "usdt", [candle(100, "10", "12")])
        self.assertEqual(result["order_size_min"], "2")
        self.assertEqual(result["quanto_multiplier"], "0.01")
        self.assertEqual(result["min_order_value"], "0.5")


class _RangeFilterTests(unittest.TestCase):

    def setUp(self):
        self.original_fetch = rg.get_one_year_hourly_candlesticks
        self.calls = []

        def fake_fetch(market_type, trading_pair, **kwargs):
            self.calls.append(trading_pair)
            return [candle(100, "1", "1.5")]
        rg.get_one_year_hourly_candlesticks = fake_fetch

    def tearDown(self):
        rg.get_one_year_hourly_candlesticks = self.original_fetch

    def test_minimum_order_value_range_filters_before_candle_scan(self):
        api = StubCandlesApi([
            contract("LOW_USDT", last_price="10", order_size_min="1",
                     quanto_multiplier="0.1"),
            contract("MATCH_USDT", last_price="20", order_size_min="2",
                     quanto_multiplier="0.5"),
            contract("HIGH_USDT", last_price="100", order_size_min="10",
                     quanto_multiplier="1"),
        ])
        report = rg.rank_futures_by_yearly_low_gain(
            futures_api=api, max_workers=1, min_order_value="10", max_order_value="30")
        self.assertEqual(self.calls, ["MATCH_USDT"])
        self.assertEqual(report["stats"]["eligible_contracts"], 1)
        self.assertEqual(report["results"][0]["min_order_value"], "20")

    def test_market_cap_range_uses_injected_provider_and_reports_millions(self):
        class Caps(object):
            def get_many(self, symbols):
                return {"aaa": 5000000, "bbb": 25000000, "pepe": 12000000}

        api = StubCandlesApi([
            contract("AAA_USDT", last_price="2"),
            contract("BBB_USDT", last_price="2"),
            contract("1000PEPE_USDT", last_price="2"),
        ])
        report = rg.rank_futures_by_yearly_low_gain(
            futures_api=api, max_workers=1, min_market_cap_musd=10,
            max_market_cap_musd=20, market_cap_provider=Caps())
        self.assertEqual(self.calls, ["1000PEPE_USDT"])
        self.assertEqual(report["results"][0]["market_cap_musd"], "12")

    def test_invalid_ranges_fail_before_network_calls(self):
        api = StubCandlesApi([contract("AAA_USDT", last_price="2")])
        with self.assertRaisesRegex(ValueError, "minimum market cap"):
            rg.rank_futures_by_yearly_low_gain(
                futures_api=api, min_market_cap_musd=20, max_market_cap_musd=10)
        self.assertEqual(self.calls, [])

    def test_preview_limit_is_applied_before_market_cap_lookup(self):
        requested_symbols = []

        class Caps(object):
            def get_many(self, symbols):
                requested_symbols.extend(sorted(symbols))
                return {symbol: 12000000 for symbol in symbols}

        api = StubCandlesApi([
            contract("LOW_USDT", last_price="2", trade_size="1"),
            contract("MID_USDT", last_price="2", trade_size="20"),
            contract("HIGH_USDT", last_price="2", trade_size="100"),
        ])
        report = rg.rank_futures_by_yearly_low_gain(
            futures_api=api, max_workers=1, limit=2,
            max_market_cap_musd=50, market_cap_provider=Caps())

        self.assertEqual(requested_symbols, ["high", "mid"])
        self.assertEqual(self.calls, ["HIGH_USDT", "MID_USDT"])
        self.assertEqual(report["stats"]["total_contracts"], 3)
        self.assertEqual(report["stats"]["candidate_contracts"], 2)
        self.assertEqual(report["stats"]["requested_contracts"], 2)


class _SortingTests(unittest.TestCase):

    def test_results_sorted_by_gain_desc_then_name_asc(self):
        items = [
            {"contract": "B_USDT", "gain_pct": "100"},
            {"contract": "A_USDT", "gain_pct": "100"},
            {"contract": "C_USDT", "gain_pct": "200"},
            {"contract": "D_USDT", "gain_pct": "-10"},
        ]
        items.sort(key=rg._rank_key)
        self.assertEqual([i["contract"] for i in items],
                         ["C_USDT", "A_USDT", "B_USDT", "D_USDT"])


class _ErrorIsolationTests(unittest.TestCase):

    def test_failing_contract_recorded_in_errors_others_continue(self):
        api = StubCandlesApi([
            contract("AAA_USDT", last_price="2"),
            contract("BBB_USDT", last_price="2"),
        ])
        original = rg.get_one_year_hourly_candlesticks

        def fake_fetch(market_type, trading_pair, **kwargs):
            if trading_pair == "BBB_USDT":
                raise RuntimeError("network down")
            return [candle(100, "1", "1.5")]
        rg.get_one_year_hourly_candlesticks = fake_fetch
        try:
            report = rg.rank_futures_by_yearly_low_gain(
                settle="usdt", futures_api=api, max_workers=1)
        finally:
            rg.get_one_year_hourly_candlesticks = original

        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["contract"], "AAA_USDT")
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["contract"], "BBB_USDT")
        self.assertIn("network down", report["errors"][0]["reason"])
        self.assertEqual(report["stats"]["requested_contracts"], 2)
        self.assertEqual(report["stats"]["successful_contracts"], 1)
        self.assertEqual(report["stats"]["failed_contracts"], 1)

    def test_empty_candles_recorded_as_no_candles(self):
        api = StubCandlesApi([contract("AAA_USDT", last_price="2")])
        original = rg.get_one_year_hourly_candlesticks
        rg.get_one_year_hourly_candlesticks = lambda *a, **k: []
        try:
            report = rg.rank_futures_by_yearly_low_gain(
                settle="usdt", futures_api=api, max_workers=1)
        finally:
            rg.get_one_year_hourly_candlesticks = original
        self.assertEqual(report["results"], [])
        self.assertEqual(report["errors"][0]["reason"], "no_candles")

    def test_compute_failure_isolated_not_crash_scan(self):
        # A bug inside _build_result for one contract must not crash the scan;
        # it should be recorded in errors like any other per-contract failure.
        api = StubCandlesApi([
            contract("AAA_USDT", last_price="2"),
            contract("BBB_USDT", last_price="2"),
        ])
        original = rg.get_one_year_hourly_candlesticks
        rg.get_one_year_hourly_candlesticks = lambda *a, **k: [candle(100, "1", "1.5")]
        original_build = rg._build_result

        def fake_build(contract_obj, settle, candles):
            if rg._contract_name(contract_obj) == "BBB_USDT":
                raise RuntimeError("compute boom")
            return original_build(contract_obj, settle, candles)
        rg._build_result = fake_build
        try:
            report = rg.rank_futures_by_yearly_low_gain(
                settle="usdt", futures_api=api, max_workers=1)
        finally:
            rg.get_one_year_hourly_candlesticks = original
            rg._build_result = original_build
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["contract"], "AAA_USDT")
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["contract"], "BBB_USDT")
        self.assertIn("compute boom", report["errors"][0]["reason"])
        self.assertEqual(report["stats"]["failed_contracts"], 1)

    def test_contract_list_failure_propagates(self):
        api = StubCandlesApi([], contracts_error=True)
        with self.assertRaises(RuntimeError):
            rg.rank_futures_by_yearly_low_gain(settle="usdt", futures_api=api)

    def test_no_active_contracts_returns_empty_with_zero_stats(self):
        api = StubCandlesApi([
            contract("X_USDT", status="delisted"),
            contract("Y_USDT", in_delisting=True),
        ])
        report = rg.rank_futures_by_yearly_low_gain(
            settle="usdt", futures_api=api, max_workers=1)
        self.assertEqual(report["results"], [])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["stats"]["requested_contracts"], 0)


class _ConcurrencyAndCallbackTests(unittest.TestCase):

    def test_stage_callback_reports_setup_and_scan_phases(self):
        api = StubCandlesApi([contract("AAA_USDT", last_price="2")])
        original = rg.get_one_year_hourly_candlesticks
        rg.get_one_year_hourly_candlesticks = lambda *a, **k: [candle(100, "1", "1.5")]
        events = []
        try:
            rg.rank_futures_by_yearly_low_gain(
                futures_api=api, max_workers=1, stage_callback=events.append)
        finally:
            rg.get_one_year_hourly_candlesticks = original
        phases = [event["phase"] for event in events]
        self.assertIn("contracts", phases)
        self.assertIn("filtering", phases)
        self.assertIn("scanning", phases)
        scan_start = next(event for event in events
                          if event["phase"] == "scanning" and "total" in event)
        self.assertEqual(scan_start["total"], 1)
        self.assertEqual(events[-1]["current_contract"], "AAA_USDT")

    def test_progress_callback_invoked_once_per_contract(self):
        api = StubCandlesApi([
            contract("AAA_USDT", last_price="2"),
            contract("BBB_USDT", last_price="3"),
        ])
        original = rg.get_one_year_hourly_candlesticks
        rg.get_one_year_hourly_candlesticks = lambda *a, **k: [candle(100, "1", "1.5")]
        events = []
        try:
            rg.rank_futures_by_yearly_low_gain(
                settle="usdt", futures_api=api, max_workers=1,
                progress_callback=lambda completed, total, contract, error:
                    events.append((completed, total, contract, error)))
        finally:
            rg.get_one_year_hourly_candlesticks = original
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][1], 2)  # total
        self.assertEqual(events[-1][0], 2)  # final completed count

    def test_result_callback_receives_each_successful_result(self):
        api = StubCandlesApi([
            contract("AAA_USDT", last_price="2"),
            contract("BBB_USDT", last_price="3"),
        ])
        original = rg.get_one_year_hourly_candlesticks
        rg.get_one_year_hourly_candlesticks = lambda *a, **k: [candle(100, "1", "1.5")]
        partial = []
        try:
            report = rg.rank_futures_by_yearly_low_gain(
                futures_api=api, max_workers=1, result_callback=partial.append)
        finally:
            rg.get_one_year_hourly_candlesticks = original
        self.assertEqual(len(partial), 2)
        self.assertEqual({item["contract"] for item in partial}, {"AAA_USDT", "BBB_USDT"})
        self.assertEqual(len(report["results"]), 2)

    def test_max_workers_bounds_concurrency(self):
        import threading
        api = StubCandlesApi([contract("C%d_USDT" % i, last_price="2")
                              for i in range(10)])
        original = rg.get_one_year_hourly_candlesticks
        in_flight = {"n": 0, "peak": 0}
        lock = threading.Lock()

        def slow_fetch(market_type, trading_pair, **kwargs):
            with lock:
                in_flight["n"] += 1
                in_flight["peak"] = max(in_flight["peak"], in_flight["n"])
            import time as _t
            _t.sleep(0.02)
            with lock:
                in_flight["n"] -= 1
            return [candle(100, "1", "1.5")]

        rg.get_one_year_hourly_candlesticks = slow_fetch
        try:
            rg.rank_futures_by_yearly_low_gain(
                settle="usdt", futures_api=api, max_workers=3)
        finally:
            rg.get_one_year_hourly_candlesticks = original
        self.assertLessEqual(in_flight["peak"], 3)
        self.assertGreaterEqual(in_flight["peak"], 1)


class _IntegrationTests(unittest.TestCase):

    def test_full_scan_results_errors_stats_consistent(self):
        # Two contracts with scripted raw candle pages via the real fetch path.
        end = RECENT_NOW
        aligned_end = end - (end % mc.HOUR_SECONDS)
        start = aligned_end - mc.YEAR_SECONDS
        # Use a single futures page window so only one candle call per contract.
        windows = list(mc._page_windows(start, aligned_end, mc.FUTURES_PAGE_SIZE,
                                       mc.HOUR_SECONDS))
        first_window = windows[0]
        aaa_candles = [
            make_futures_candle(first_window[0], low="10", close="12"),
            make_futures_candle(first_window[0] + mc.HOUR_SECONDS, low="12", close="25"),
        ]
        bbb_candles = [
            make_futures_candle(first_window[0], low="5", close="6"),
            make_futures_candle(first_window[0] + mc.HOUR_SECONDS, low="6", close="8"),
        ]
        api = StubCandlesApi(
            contracts=[contract("AAA_USDT", last_price="25"),
                       contract("BBB_USDT", last_price="8")],
            candle_responses={"AAA_USDT": aaa_candles, "BBB_USDT": bbb_candles},
        )
        report = rg.rank_futures_by_yearly_low_gain(
            settle="usdt", end_time=aligned_end, futures_api=api, max_workers=1)
        self.assertEqual(len(report["results"]), 2)
        # AAA: (25-10)/10 = 150 ; BBB: (8-5)/5 = 60 -> AAA first.
        self.assertEqual([r["contract"] for r in report["results"]],
                         ["AAA_USDT", "BBB_USDT"])
        self.assertEqual(report["results"][0]["gain_pct"], "150")
        self.assertEqual(report["results"][1]["gain_pct"], "60")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["stats"]["successful_contracts"], 2)

    def test_same_inputs_produce_same_output(self):
        end = RECENT_NOW
        aligned_end = end - (end % mc.HOUR_SECONDS)
        start = aligned_end - mc.YEAR_SECONDS
        windows = list(mc._page_windows(start, aligned_end, mc.FUTURES_PAGE_SIZE,
                                       mc.HOUR_SECONDS))
        fw = windows[0]
        candles = [make_futures_candle(fw[0], low="10", close="20")]
        api1 = StubCandlesApi(contracts=[contract("AAA_USDT", last_price="30")],
                              candle_responses={"AAA_USDT": candles})
        api2 = StubCandlesApi(contracts=[contract("AAA_USDT", last_price="30")],
                              candle_responses={"AAA_USDT": candles})
        r1 = rg.rank_futures_by_yearly_low_gain(
            settle="usdt", end_time=aligned_end, futures_api=api1, max_workers=1)
        r2 = rg.rank_futures_by_yearly_low_gain(
            settle="usdt", end_time=aligned_end, futures_api=api2, max_workers=1)
        self.assertEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
