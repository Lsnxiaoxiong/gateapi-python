# coding: utf-8
"""Tests for tools.market_candlesticks using injected fake APIs (no network).

These tests inject lightweight fake Spot/Futures API objects, so they do not
require the ``gate_api`` SDK to be installed and never touch the network.
Run with: ``python -m pytest tests/test_market_candlesticks.py`` or, without
pytest, ``python -m unittest tests.test_market_candlesticks``.
"""
from __future__ import absolute_import

import sys
import time
import unittest
from types import SimpleNamespace

PROJECT_ROOT = __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools import market_candlesticks as mc

#: A recent, hour-aligned "now" used as the fixed end time in tests. Using a
#: rolling value (rather than a hard-coded 2023 timestamp) keeps the 365-day
#: 1h look-back inside Gate's 10000-point ceiling no matter when the suite runs.
RECENT_NOW = (int(time.time()) // mc.HOUR_SECONDS) * mc.HOUR_SECONDS


def spot_candle(ts, o="1", h="2", l="0.5", c="1.5", v="10", amount="15"):
    """A raw spot candlestick array in Gate order [t, v, c, h, l, o, sum]."""
    return [ts, v, c, h, l, o, amount]


def futures_candle(ts, o="1", h="2", l="0.5", c="1.5", v="10", amount="15"):
    """A FuturesCandlestick-like object."""
    return SimpleNamespace(t=ts, v=v, c=c, h=h, l=l, o=o, sum=amount)


class RecordingSpotApi(object):
    """Fake SpotApi that records page requests and returns scripted data."""

    def __init__(self, responses):
        # responses: dict[(from_ts, to_ts)] -> list of raw arrays
        self.responses = responses
        self.calls = []
        self.list_candlesticks_count = 0

    def list_candlesticks(self, currency_pair, **kwargs):
        self.calls.append((currency_pair, kwargs))
        self.list_candlesticks_count += 1
        key = (kwargs.get("_from"), kwargs.get("to"))
        return self.responses.get(key, [])


class RecordingFuturesApi(object):
    """Fake FuturesApi that records candle requests and contract listings."""

    def __init__(self, candle_responses=None, contracts=None):
        self.candle_responses = candle_responses or {}
        self.contracts = contracts or []
        self.candle_calls = []
        self.contracts_calls = 0

    def list_futures_candlesticks(self, settle, contract, **kwargs):
        self.candle_calls.append((settle, contract, kwargs))
        key = (kwargs.get("_from"), kwargs.get("to"))
        return self.candle_responses.get((contract, key), [])

    def list_futures_contracts(self, settle):
        self.contracts_calls += 1
        return list(self.contracts)


class PerPageFailingApi(object):
    """Fake SpotApi that fails the first N attempts of EACH page, then succeeds.

    Tracks per-page attempt counts keyed by ``(from, to)`` so retry behaviour
    can be asserted independently of the total page count. On success it
    returns a single candle at the page ``_from`` timestamp, which always
    falls inside the requested ``[start, end)`` range.
    """

    def __init__(self, fail_times, then_value=None, exc_class=None):
        self.fail_times = fail_times
        self.then_value = then_value
        self.exc_class = exc_class or ConnectionError
        self.page_attempts = {}

    def list_candlesticks(self, currency_pair, **kwargs):
        key = (kwargs.get("_from"), kwargs.get("to"))
        n = self.page_attempts.get(key, 0) + 1
        self.page_attempts[key] = n
        if n <= self.fail_times:
            raise self.exc_class("transient boom")
        if self.then_value is not None:
            return self.then_value
        # Default: a candle at the page start timestamp (always in range).
        return [spot_candle(kwargs.get("_from"), c="1")]


class PermanentErrorApi(object):
    """Fake API that always raises a non-retryable error."""

    def list_candlesticks(self, currency_pair, **kwargs):
        exc = ValueError("bad param")
        return exc  # returned, not raised, to test non-exception paths

    def list_futures_candlesticks(self, settle, contract, **kwargs):
        raise ValueError("bad param")


class FakeCloseable(object):
    """Fake ApiClient that records close() calls."""

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class FakeSpotApiWithCloseableClient(object):
    """A spot API whose ``api_client`` is a FakeCloseable.

    Also records how many candle calls were made so we can assert that the
    fetch happened and that the injected client was not closed.
    """

    def __init__(self, closeable_client, response=None):
        self.api_client = closeable_client
        self._response = response if response is not None else []
        self.calls = 0

    def list_candlesticks(self, currency_pair, **kwargs):
        self.calls += 1
        return self._response


class _ImportSideEffectTests(unittest.TestCase):
    """Importing the modules must perform no network I/O (step 1 verification #1)."""

    def test_importing_modules_makes_no_api_calls(self):
        import importlib
        import tools.market_candlesticks as mod
        importlib.reload(mod)
        # No client factory was called: no attributes created on import.
        self.assertFalse(hasattr(mod, "_created_client_during_import"))


class _ResourceLifecycleTests(unittest.TestCase):
    """Owned clients are closed on success and failure; injected ones are not."""

    def _patch_default_spot(self, response):
        closeable = FakeCloseable()
        api = FakeSpotApiWithCloseableClient(closeable, response=response)
        original = mc._default_spot_api
        mc._default_spot_api = lambda timeout: api
        return closeable, api, original

    def test_owned_client_closed_on_success(self):
        closeable, api, original = self._patch_default_spot(
            [spot_candle(100, c="1")])
        try:
            mc.get_one_year_hourly_candlesticks(
                "spot", "BTC_USDT", end_time=RECENT_NOW)
        finally:
            mc._default_spot_api = original
        # The fetch happened and the owned client was closed exactly once.
        self.assertGreater(api.calls, 0)
        self.assertEqual(closeable.closed, 1)

    def test_owned_client_closed_on_failure(self):
        closeable = FakeCloseable()

        class _RaisingApi(object):
            api_client = closeable

            def list_candlesticks(self, currency_pair, **kwargs):
                raise ValueError("bad param")

        api = _RaisingApi()
        original = mc._default_spot_api
        mc._default_spot_api = lambda timeout: api
        try:
            with self.assertRaises(ValueError):
                mc.get_one_year_hourly_candlesticks(
                    "spot", "BTC_USDT", end_time=RECENT_NOW)
        finally:
            mc._default_spot_api = original
        # Even on failure the owned client is closed.
        self.assertEqual(closeable.closed, 1)

    def test_injected_client_not_closed(self):
        closeable = FakeCloseable()
        api = FakeSpotApiWithCloseableClient(closeable,
                                              response=[spot_candle(100, c="1")])
        mc.get_one_year_hourly_candlesticks(
            "spot", "BTC_USDT", end_time=RECENT_NOW, spot_api=api)
        # Injected client must not be closed by this function.
        self.assertEqual(closeable.closed, 0)


class _ValidationTests(unittest.TestCase):

    def test_market_type_normalized_lower_and_stripped(self):
        self.assertEqual(mc._normalize_market_type("  FUTURES "), "futures")
        self.assertEqual(mc._normalize_market_type("Spot"), "spot")

    def test_invalid_market_type_raises(self):
        for bad in (None, "", "  ", "margin", "options"):
            with self.assertRaises(ValueError) as ctx:
                mc._normalize_market_type(bad)
            self.assertIn("market_type", str(ctx.exception))

    def test_trading_pair_normalized(self):
        self.assertEqual(mc._normalize_trading_pair(" btc-usdt "), "BTC_USDT")
        self.assertEqual(mc._normalize_trading_pair("eth_usdt"), "ETH_USDT")

    def test_empty_trading_pair_raises(self):
        for bad in (None, "", "   "):
            with self.assertRaises(ValueError) as ctx:
                mc._normalize_trading_pair(bad)
            self.assertIn("trading_pair", str(ctx.exception))

    def test_settle_required_for_futures(self):
        self.assertEqual(mc._normalize_settle(" USDT "), "usdt")
        for bad in (None, ""):
            with self.assertRaises(ValueError):
                mc._normalize_settle(bad)

    def test_end_time_defaults_to_now_and_must_be_positive(self):
        now = int(time.time())
        self.assertAlmostEqual(mc._resolve_end_time(None), now, delta=5)
        with self.assertRaises(ValueError):
            mc._resolve_end_time(0)
        with self.assertRaises(ValueError):
            mc._resolve_end_time(-1)


class _PaginationTests(unittest.TestCase):

    def test_spot_one_year_uses_at_least_nine_pages(self):
        end = RECENT_NOW
        aligned_end = end - (end % mc.HOUR_SECONDS)
        start = aligned_end - mc.YEAR_SECONDS
        windows = list(mc._page_windows(start, aligned_end, mc.SPOT_PAGE_SIZE,
                                       mc.HOUR_SECONDS))
        self.assertGreaterEqual(len(windows), 9)
        # Each window spans at most SPOT_PAGE_SIZE hours (999), staying inside
        # Gate's 1000-points-per-request ceiling.
        for from_ts, to_ts in windows:
            self.assertLessEqual(to_ts - from_ts, mc.SPOT_PAGE_SIZE * mc.HOUR_SECONDS)
        # Windows tile [start, end) without overlap or gaps.
        self.assertEqual(windows[0][0], start)
        self.assertEqual(windows[-1][1], aligned_end)
        for prev, nxt in zip(windows, windows[1:]):
            self.assertEqual(prev[1], nxt[0])

    def test_futures_one_year_uses_at_least_five_pages(self):
        end = RECENT_NOW
        aligned_end = end - (end % mc.HOUR_SECONDS)
        start = aligned_end - mc.YEAR_SECONDS
        windows = list(mc._page_windows(start, aligned_end, mc.FUTURES_PAGE_SIZE,
                                       mc.HOUR_SECONDS))
        self.assertGreaterEqual(len(windows), 5)
        for from_ts, to_ts in windows:
            self.assertLessEqual(to_ts - from_ts, mc.FUTURES_PAGE_SIZE * mc.HOUR_SECONDS)

    def test_all_requests_use_1h_interval_and_no_limit(self):
        end = RECENT_NOW
        aligned_end = end - (end % mc.HOUR_SECONDS)
        start = aligned_end - mc.YEAR_SECONDS
        windows = list(mc._page_windows(start, aligned_end, mc.SPOT_PAGE_SIZE,
                                       mc.HOUR_SECONDS))
        responses = {w: [spot_candle(w[0])] for w in windows}
        api = RecordingSpotApi(responses)
        mc.get_one_year_hourly_candlesticks(
            "spot", "BTC_USDT", end_time=aligned_end, spot_api=api)
        self.assertGreater(api.list_candlesticks_count, 0)
        for pair, kwargs in api.calls:
            self.assertEqual(kwargs.get("interval"), "1h")
            self.assertIsNone(kwargs.get("limit"))
            self.assertEqual(pair, "BTC_USDT")


class _NormalizationTests(unittest.TestCase):

    def test_spot_array_and_futures_object_produce_same_fields(self):
        ts = 123
        spot = mc._normalize_spot_candle(spot_candle(ts))
        fut = mc._normalize_futures_candle(futures_candle(ts))
        self.assertEqual(spot, fut)
        self.assertEqual(set(spot.keys()),
                         {"timestamp", "open", "high", "low", "close", "volume"})
        self.assertEqual(spot["timestamp"], ts)
        self.assertEqual(spot["open"], "1")
        self.assertEqual(spot["high"], "2")
        self.assertEqual(spot["low"], "0.5")
        self.assertEqual(spot["close"], "1.5")
        self.assertEqual(spot["volume"], "10")

    def test_invalid_candles_are_skipped(self):
        self.assertIsNone(mc._normalize_spot_candle(None))
        self.assertIsNone(mc._normalize_spot_candle([]))
        # Bad timestamp.
        self.assertIsNone(mc._normalize_spot_candle(["abc", "1", "1", "1", "1", "1", "1"]))
        # Zero / non-positive prices dropped.
        bad = [100, "1", "0", "0", "0", "0", "0"]
        self.assertIsNone(mc._normalize_spot_candle(bad))
        # Missing optional fields still parsed where present.
        partial = mc._normalize_spot_candle([200, "1", "1", "1", "1", "1"])
        self.assertIsNotNone(partial)
        self.assertEqual(partial["timestamp"], 200)

    def test_empty_response_yields_empty_list(self):
        end = RECENT_NOW
        aligned_end = end - (end % mc.HOUR_SECONDS)
        api = RecordingSpotApi({})
        out = mc.get_one_year_hourly_candlesticks(
            "spot", "BTC_USDT", end_time=aligned_end, spot_api=api)
        self.assertEqual(out, [])


class _MergeAndRangeTests(unittest.TestCase):

    def test_duplicate_boundary_timestamp_kept_once(self):
        ts = 500
        candle_a = mc._normalize_spot_candle(spot_candle(ts, c="1"))
        candle_b = mc._normalize_spot_candle(spot_candle(ts, c="2"))
        merged = mc._merge_and_finalize([candle_a, candle_b], ts, ts + 1,
                                        mc.HOUR_SECONDS)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["timestamp"], ts)

    def test_out_of_range_candles_clipped(self):
        start, end = 1000, 2000
        candles = [
            mc._normalize_spot_candle(spot_candle(500)),   # before start
            mc._normalize_spot_candle(spot_candle(1500)),  # inside
            mc._normalize_spot_candle(spot_candle(2500)),  # after end
        ]
        merged = mc._merge_and_finalize(candles, start, end, mc.HOUR_SECONDS)
        self.assertEqual([c["timestamp"] for c in merged], [1500])

    def test_result_sorted_ascending_even_if_input_unsorted(self):
        ts1, ts2, ts3 = 100, 300, 200
        candles = [
            mc._normalize_spot_candle(spot_candle(ts3)),
            mc._normalize_spot_candle(spot_candle(ts1)),
            mc._normalize_spot_candle(spot_candle(ts2)),
        ]
        # Range spans [min, max+1) so all three candles are kept.
        merged = mc._merge_and_finalize(candles, ts1, ts2 + 1, mc.HOUR_SECONDS)
        self.assertEqual([c["timestamp"] for c in merged],
                         sorted([ts1, ts2, ts3]))


class _RetryTests(unittest.TestCase):

    def test_transient_error_retried_then_succeeds(self):
        # Fail the first 2 attempts of EACH page, then succeed; record per-page
        # attempt counts so we can assert retry happened on every page without
        # depending on the total page count. On success the fake returns a
        # candle at the page ``_from`` timestamp (always in range).
        original_sleep = mc.time.sleep
        mc.time.sleep = lambda *_: None
        try:
            api = PerPageFailingApi(fail_times=2)
            out = mc.get_one_year_hourly_candlesticks(
                "spot", "BTC_USDT", end_time=RECENT_NOW, spot_api=api,
                max_retries=3)
            self.assertGreater(len(api.page_attempts), 0)
            # Every page was retried exactly twice before succeeding (2 + 1).
            self.assertTrue(all(n == 3 for n in api.page_attempts.values()),
                            msg=api.page_attempts)
            # One candle per page, all retained because they sit in range.
            self.assertEqual(len(out), len(api.page_attempts))
        finally:
            mc.time.sleep = original_sleep

    def test_retry_gives_up_after_max_retries(self):
        original_sleep = mc.time.sleep
        mc.time.sleep = lambda *_: None
        try:
            api = PerPageFailingApi(fail_times=10)  # never succeeds
            with self.assertRaises(ConnectionError):
                mc.get_one_year_hourly_candlesticks(
                    "spot", "BTC_USDT", end_time=RECENT_NOW, spot_api=api,
                    max_retries=2)
            # First page tried max_retries+1 times then re-raised (2 + 1).
            first_key = next(iter(api.page_attempts))
            self.assertEqual(api.page_attempts[first_key], 3)
        finally:
            mc.time.sleep = original_sleep

    def test_non_retryable_error_propagates(self):
        api = PermanentErrorApi()
        with self.assertRaises(ValueError):
            mc.get_one_year_hourly_candlesticks(
                "futures", "BTC_USDT", end_time=RECENT_NOW, futures_api=api,
                max_retries=3)


class _FuturesIntegrationTests(unittest.TestCase):

    def test_futures_candles_merged_across_pages(self):
        end = RECENT_NOW
        aligned_end = end - (end % mc.HOUR_SECONDS)
        start = aligned_end - mc.YEAR_SECONDS
        windows = list(mc._page_windows(start, aligned_end, mc.FUTURES_PAGE_SIZE,
                                       mc.HOUR_SECONDS))
        responses = {}
        for i, w in enumerate(windows):
            # one candle per page at the page start timestamp; use i+1 so the
            # close price is always positive (a zero/invalid close is dropped).
            responses[("BTC_USDT", w)] = [futures_candle(w[0], c=str(i + 1))]
        api = RecordingFuturesApi(candle_responses=responses)
        out = mc.get_one_year_hourly_candlesticks(
            "futures", "BTC_USDT", settle="usdt", end_time=aligned_end,
            futures_api=api)
        self.assertEqual(len(out), len(windows))
        timestamps = [c["timestamp"] for c in out]
        self.assertEqual(timestamps, sorted(timestamps))
        # Each request carried settle/contract/interval.
        for settle, contract, kwargs in api.candle_calls:
            self.assertEqual(settle, "usdt")
            self.assertEqual(contract, "BTC_USDT")
            self.assertEqual(kwargs.get("interval"), "1h")


class _IntervalAndCeilingTests(unittest.TestCase):
    """Tests for the generalized interval support and the 10000-point ceiling."""

    def test_valid_intervals_constant_complete(self):
        self.assertEqual(set(mc.VALID_INTERVALS),
                         {"1h", "2h", "3h", "4h", "6h", "8h", "12h",
                          "1d", "2d", "3d", "7d", "30d"})
        # 3h is the only non-native interval and aggregates from 1h.
        self.assertEqual(mc.AGGREGATE_FROM, {"3h": ("1h", 3)})

    def test_normalize_interval_accepts_supported_and_rejects_others(self):
        for iv in ("1h", "2H", " 3h ", "1D", "30d"):
            self.assertIn(mc._normalize_interval(iv), mc.VALID_INTERVALS)
        with self.assertRaises(ValueError):
            mc._normalize_interval("5m")
        with self.assertRaises(ValueError):
            mc._normalize_interval(None)

    def test_native_interval_resolves_3h_to_1h(self):
        self.assertEqual(mc._native_interval("3h"), "1h")
        self.assertEqual(mc._native_interval("1d"), "1d")

    def test_page_windows_use_interval_seconds(self):
        # 1d windows of size 1999 should span 1999 days each.
        start, end = 0, 5 * 86400
        wins = list(mc._page_windows(start, end, mc.FUTURES_PAGE_SIZE, 86400))
        self.assertEqual(wins[0][0], 0)
        self.assertEqual(wins[-1][1], end)
        for a, b in wins:
            self.assertLessEqual(b - a, mc.FUTURES_PAGE_SIZE * 86400)

    def test_aggregate_candles_merges_1h_into_3h(self):
        # Six hourly candles -> two 3h candles. Align base to a 3h boundary so
        # both groups are complete.
        base = 3_000_000
        base -= base % (3 * 3600)
        candles = []
        for i in range(6):
            candles.append({
                "timestamp": base + i * 3600,
                "open": str(10 + i), "high": str(12 + i),
                "low": str(9 + i), "close": str(11 + i),
                "volume": str(100 + i),
            })
        merged = mc._aggregate_candles(candles, "1h", "3h")
        self.assertEqual(len(merged), 2)
        # First group: hours 0,1,2 -> open=10, close=13, high=14, low=9, vol=303
        self.assertEqual(merged[0]["timestamp"], base)
        self.assertEqual(merged[0]["open"], "10")
        self.assertEqual(merged[0]["close"], "13")
        self.assertEqual(merged[0]["high"], "14")
        self.assertEqual(merged[0]["low"], "9")
        self.assertEqual(merged[0]["volume"], "303")
        # Second group: hours 3,4,5 -> open=13, close=16, high=17, low=12, vol=312
        self.assertEqual(merged[1]["timestamp"], base + 3 * 3600)
        self.assertEqual(merged[1]["close"], "16")
        self.assertEqual(merged[1]["volume"], "312")

    def test_aggregate_candles_drops_incomplete_trailing_group(self):
        base = 3_000_000
        base -= base % (3 * 3600)
        candles = []
        for i in range(4):  # only one full 3h group + 1 leftover
            candles.append({
                "timestamp": base + i * 3600,
                "open": "1", "high": "2", "low": "0.5",
                "close": "1.5", "volume": "10",
            })
        merged = mc._aggregate_candles(candles, "1h", "3h")
        self.assertEqual(len(merged), 1)

    def test_ceiling_rejects_too_deep_1h_lookback(self):
        # span_days beyond the ~414-day 1h ceiling must raise a clear ValueError
        # before any network call.
        api = RecordingFuturesApi(candle_responses={})
        with self.assertRaises(ValueError) as ctx:
            mc.get_candlesticks("futures", "BTC_USDT", interval="1h",
                                span_days=500, futures_api=api)
        self.assertIn("ceiling", str(ctx.exception).lower())
        self.assertEqual(api.candle_calls, [])

    def test_ceiling_allows_deep_daily_lookback(self):
        # 5 years of 1d candles is well inside the 10000-day ceiling.
        end = RECENT_NOW - (RECENT_NOW % 86400)  # align to day boundary
        start = end - 365 * 5 * 86400
        pages = list(mc._page_windows(start, end, mc.FUTURES_PAGE_SIZE, 86400))
        responses = {}
        for w in pages:
            responses[("BTC_USDT", w)] = [futures_candle(w[0], c="1")]
        api = RecordingFuturesApi(candle_responses=responses)
        out = mc.get_candlesticks("futures", "BTC_USDT", interval="1d",
                                  span_days=365 * 5, end_time=end,
                                  futures_api=api)
        self.assertEqual(len(out), len(pages))
        # Every request used interval=1d.
        for _, _, kwargs in api.candle_calls:
            self.assertEqual(kwargs.get("interval"), "1d")

    def test_3h_interval_fetches_1h_and_merges(self):
        # span small enough for 1h ceiling: 2 days -> 48 hourly -> 16 three-hourly.
        end = RECENT_NOW - (RECENT_NOW % (3 * 3600))  # align to 3h boundary
        iv_sec = 3600
        start = end - 2 * 86400
        start -= start % iv_sec
        pages = list(mc._page_windows(start, end, mc.FUTURES_PAGE_SIZE, iv_sec))
        responses = {}
        for w in pages:
            # one hourly candle per page at page start, plus the next hour
            responses[("BTC_USDT", w)] = [
                futures_candle(w[0], c="1"),
                futures_candle(w[0] + iv_sec, c="2"),
            ]
        api = RecordingFuturesApi(candle_responses=responses)
        out = mc.get_candlesticks("futures", "BTC_USDT", interval="3h",
                                  span_days=2, end_time=end, futures_api=api)
        # All fetch calls must target the 1h native interval (not 3h).
        for _, _, kwargs in api.candle_calls:
            self.assertEqual(kwargs.get("interval"), "1h")
        # Result timestamps are spaced 3h apart and aligned to 3h boundaries.
        ts = [c["timestamp"] for c in out]
        for t in ts:
            self.assertEqual(t % (3 * 3600), 0)
        if len(ts) >= 2:
            self.assertEqual(ts[1] - ts[0], 3 * 3600)

    def test_get_one_year_wrapper_equivalent_to_get_candlesticks_1h_365(self):
        # Same inputs (via fake API) -> same output for the wrapper vs direct call.
        end = RECENT_NOW
        start = end - 365 * 86400
        start -= start % 3600
        pages = list(mc._page_windows(start, end, mc.FUTURES_PAGE_SIZE, 3600))
        def build():
            responses = {("BTC_USDT", w): [futures_candle(w[0], c="7")] for w in pages}
            return RecordingFuturesApi(candle_responses=responses)
        a = mc.get_one_year_hourly_candlesticks(
            "futures", "BTC_USDT", settle="usdt", end_time=end, futures_api=build())
        b = mc.get_candlesticks(
            "futures", "BTC_USDT", interval="1h", span_days=365, settle="usdt",
            end_time=end, futures_api=build())
        self.assertEqual([c["timestamp"] for c in a], [c["timestamp"] for c in b])


if __name__ == "__main__":
    unittest.main()
