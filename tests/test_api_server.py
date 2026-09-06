# coding: utf-8
"""Tests for the FastAPI service in ``tools/api_server``.

These tests use FastAPI's ``TestClient`` (httpx-backed) and monkeypatch the
synchronous ``tools`` helpers so no live Gate API network calls are made.
"""
from __future__ import absolute_import

import json
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient

import tools.api_server as srv
import tools.market_candlesticks as mc


def _fake_candle(ts, o="100", c="110", h="120", l="90", v="1000"):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


class _ApiServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(srv.app)

    def test_valid_intervals_endpoint(self):
        r = self.client.get("/api/valid_intervals")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        ivs = [x["interval"] for x in data["intervals"]]
        self.assertIn("1h", ivs)
        self.assertIn("3h", ivs)
        self.assertIn("1d", ivs)
        self.assertEqual(data["max_lookback_points"], mc.MAX_LOOKBACK_POINTS)
        # Each entry has seconds and max_lookback_days
        for x in data["intervals"]:
            self.assertIn("seconds", x)
            self.assertIn("max_lookback_days", x)

    def test_candles_endpoint_calls_get_candlesticks(self):
        captured = {}

        def fake_get(market_type, trading_pair, interval="1h", span_days=365,
                     settle="usdt", end_time=None, spot_api=None,
                     futures_api=None, request_timeout=30, max_retries=3):
            captured["args"] = {
                "market_type": market_type, "trading_pair": trading_pair,
                "interval": interval, "span_days": span_days, "settle": settle,
                "end_time": end_time,
            }
            return [_fake_candle(1700000000), _fake_candle(1700003600)]

        original = srv.get_candlesticks
        srv.get_candlesticks = fake_get
        try:
            r = self.client.get("/api/candles", params={
                "market_type": "futures", "trading_pair": "btc_usdt",
                "interval": "4h", "span_days": 7, "settle": "usdt",
                "end_time": 1700003600,
            })
        finally:
            srv.get_candlesticks = original
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["interval"], "4h")
        self.assertEqual(body["trading_pair"], "BTC_USDT")
        self.assertEqual(captured["args"]["interval"], "4h")
        self.assertEqual(captured["args"]["span_days"], 7)
        self.assertEqual(captured["args"]["end_time"], 1700003600)

    def test_candles_endpoint_value_error_is_400(self):
        def fake_get(**kw):
            raise ValueError("Requested look-back for interval '1h' exceeds ceiling")
        original = srv.get_candlesticks
        srv.get_candlesticks = fake_get
        try:
            r = self.client.get("/api/candles", params={
                "market_type": "futures", "trading_pair": "BTC_USDT",
            })
        finally:
            srv.get_candlesticks = original
        self.assertEqual(r.status_code, 400)
        self.assertIn("ceiling", r.json()["detail"])

    def test_candles_endpoint_generic_error_is_500(self):
        def fake_get(**kw):
            raise RuntimeError("boom")
        original = srv.get_candlesticks
        srv.get_candlesticks = fake_get
        try:
            r = self.client.get("/api/candles", params={
                "market_type": "futures", "trading_pair": "BTC_USDT",
            })
        finally:
            srv.get_candlesticks = original
        self.assertEqual(r.status_code, 500)
        self.assertIn("boom", r.json()["detail"])

    def test_rank_endpoint_returns_report(self):
        fake_report = {
            "stats": {"total_contracts": 5, "successful_contracts": 4,
                      "failed_contracts": 1, "elapsed_seconds": 1.2},
            "results": [
                {"contract": "BTC_USDT", "year_low": "50000", "last_price": "60000",
                 "gain_pct": 20.0, "year_low_time": 1700000000,
                 "current_price_source": "contract_last_price"},
                {"contract": "ETH_USDT", "year_low": "1000", "last_price": "1500",
                 "gain_pct": 50.0, "year_low_time": 1700000000,
                 "current_price_source": "contract_last_price"},
            ],
            "errors": [],
        }

        def fake_rank(settle="usdt", end_time=None, futures_api=None,
                      max_workers=4, progress_callback=None, limit=None,
                      result_callback=None):
            return fake_report

        original = srv.rank_futures_by_yearly_low_gain
        srv.rank_futures_by_yearly_low_gain = fake_rank
        try:
            r = self.client.get("/api/rank", params={"max_workers": 6})
        finally:
            srv.rank_futures_by_yearly_low_gain = original
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["stats"]["total_contracts"], 5)
        self.assertEqual(len(body["results"]), 2)
        self.assertIn("elapsed_seconds", body["stats"])

    def test_rank_endpoint_passes_limit(self):
        captured = {}

        def fake_rank(settle="usdt", end_time=None, futures_api=None,
                      max_workers=4, progress_callback=None, limit=None):
            captured["limit"] = limit
            return {"stats": {}, "results": [], "errors": []}

        original = srv.rank_futures_by_yearly_low_gain
        srv.rank_futures_by_yearly_low_gain = fake_rank
        try:
            r = self.client.get("/api/rank", params={"limit": 100})
        finally:
            srv.rank_futures_by_yearly_low_gain = original
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["limit"], 100)

    def test_rank_endpoint_passes_range_filters(self):
        captured = {}

        def fake_rank(**kwargs):
            captured.update(kwargs)
            return {"stats": {}, "results": [], "errors": []}

        original = srv.rank_futures_by_yearly_low_gain
        srv.rank_futures_by_yearly_low_gain = fake_rank
        try:
            r = self.client.get("/api/rank", params={
                "min_market_cap_musd": 8,
                "max_market_cap_musd": 100,
                "min_order_value": 1.5,
                "max_order_value": 25,
            })
        finally:
            srv.rank_futures_by_yearly_low_gain = original
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["min_market_cap_musd"], 8)
        self.assertEqual(captured["max_market_cap_musd"], 100)
        self.assertEqual(captured["min_order_value"], 1.5)
        self.assertEqual(captured["max_order_value"], 25)

    def test_rank_job_reports_progress_and_result(self):
        def fake_rank(settle="usdt", end_time=None, futures_api=None,
                      max_workers=4, progress_callback=None, limit=None,
                      result_callback=None, stage_callback=None):
            total = int(limit or 3)
            if stage_callback is not None:
                stage_callback({
                    "phase": "contracts", "message": "已获取合约 3/3",
                    "completed": 3, "total": 3, "contracts_fetched": 3,
                })
                stage_callback({
                    "phase": "scanning", "message": "开始下载 K 线",
                    "completed": 0, "total": total, "eligible_contracts": total,
                })
            for i in range(total):
                if progress_callback is not None:
                    progress_callback(i + 1, total, "C%d_USDT" % (i + 1), None)
                time.sleep(0.005)
            result = {"contract": "BTC_USDT", "gain_pct": "12.3"}
            if result_callback is not None:
                result_callback(result)
            return {
                "stats": {
                    "total_contracts": total,
                    "requested_contracts": total,
                    "successful_contracts": total,
                    "failed_contracts": 0,
                },
                "results": [result],
                "errors": [],
            }

        original = srv.rank_futures_by_yearly_low_gain
        srv.rank_futures_by_yearly_low_gain = fake_rank
        try:
            started = self.client.post("/api/rank/jobs", params={"limit": 3, "max_workers": 2})
            self.assertEqual(started.status_code, 202)
            job = started.json()
            self.assertIn(job["status"], ("queued", "running", "completed"))
            job_id = job["job_id"]

            deadline = time.time() + 2
            while time.time() < deadline:
                current = self.client.get("/api/rank/jobs/%s" % job_id)
                self.assertEqual(current.status_code, 200)
                job = current.json()
                if job["status"] == "completed":
                    break
                time.sleep(0.01)
        finally:
            srv.rank_futures_by_yearly_low_gain = original

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["progress"]["completed"], 3)
        self.assertEqual(job["progress"]["percent"], 100.0)
        self.assertEqual(job["progress"]["successful"], 3)
        self.assertEqual(job["progress"]["stage_percent"], 100.0)
        self.assertEqual(job["progress"]["message"], "扫描完成")
        self.assertEqual(job["progress"]["details"]["contracts_fetched"], 3)
        self.assertEqual(job["report"]["results"][0]["contract"], "BTC_USDT")
        self.assertEqual(job["partial_results"][0]["contract"], "BTC_USDT")

    def test_rank_job_unknown_id_is_404(self):
        r = self.client.get("/api/rank/jobs/not-a-real-job")
        self.assertEqual(r.status_code, 404)

    def test_rank_endpoint_error_is_500(self):
        def fake_rank(settle="usdt", end_time=None, futures_api=None,
                      max_workers=4, progress_callback=None, limit=None):
            raise RuntimeError("scan failed")
        original = srv.rank_futures_by_yearly_low_gain
        srv.rank_futures_by_yearly_low_gain = fake_rank
        try:
            r = self.client.get("/api/rank")
        finally:
            srv.rank_futures_by_yearly_low_gain = original
        self.assertEqual(r.status_code, 500)

    def test_index_html_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<div id=\"app\">", r.content)
        self.assertIn(b"/static/main.js", r.content)
        self.assertIn(b"main.js?v=20260830-1", r.content)
        self.assertEqual(r.headers["cache-control"], "no-cache, no-store, must-revalidate")

    def test_static_main_js_served(self):
        r = self.client.get("/static/main.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"createApp", r.content)

    def test_static_styles_css_served(self):
        r = self.client.get("/static/styles.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"--bg", r.content)

    def test_static_chart_js_served(self):
        r = self.client.get("/static/chart.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"CandleChart", r.content)

    def test_static_vue_bundle_served(self):
        r = self.client.get("/static/vue.esm-browser.prod.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"createApp", r.content)

    def test_spa_fallback_returns_index_for_unknown_path(self):
        r = self.client.get("/some/spa/route")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<div id=\"app\">", r.content)

    def test_api_path_not_swallowed_by_spa_fallback(self):
        r = self.client.get("/api/nonexistent")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
