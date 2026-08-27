# coding: utf-8
"""Tests for direct market-cap HTTP setup and provider fallback."""
from __future__ import absolute_import

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import urllib3

from tools import market_caps


class _MarketCapHttpTests(unittest.TestCase):

    def test_default_http_is_always_direct(self):
        client = market_caps._default_http()
        self.assertIsInstance(client, urllib3.PoolManager)
        self.assertNotIsInstance(client, urllib3.ProxyManager)


class _Response(object):
    def __init__(self, status, payload):
        import json
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


class _Http(object):
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class _MarketCapProviderTests(unittest.TestCase):

    def test_coinpaprika_maps_largest_market_cap_for_duplicate_symbol(self):
        http = _Http(_Response(200, [
            {"symbol": "AAA", "quotes": {"USD": {"market_cap": 10}}},
            {"symbol": "AAA", "quotes": {"USD": {"market_cap": 25}}},
            {"symbol": "BBB", "quotes": {"USD": {"market_cap": 5}}},
        ]))
        provider = market_caps.CoinPaprikaMarketCaps(http=http)
        self.assertEqual(provider.get_many(["aaa", "missing"]), {"aaa": 25})
        self.assertEqual(provider.get_many(["bbb"]), {"bbb": 5})
        self.assertEqual(len(http.calls), 1)

    def test_coinlore_maps_market_caps_and_reuses_loaded_page(self):
        http = _Http(_Response(200, {
            "data": [
                {"symbol": "AAA", "market_cap_usd": "25000000"},
                {"symbol": "BBB", "market_cap_usd": "5000000"},
            ],
            "info": {"coins_num": 2},
        }))
        provider = market_caps.CoinLoreMarketCaps(http=http)
        self.assertEqual(provider.get_many(
            ["aaa", "missing"], min_market_cap_usd=4000000), {"aaa": 25000000.0})
        self.assertEqual(provider.get_many(["bbb"]), {"bbb": 5000000.0})
        self.assertEqual(len(http.calls), 1)
        self.assertIs(http.calls[0][2]["retries"], False)

    def test_default_provider_falls_back_after_primary_failure(self):
        class Provider(object):
            def __init__(self, result=None, error=None):
                self.result = result
                self.error = error

            def get_many(self, symbols, **kwargs):
                if self.error:
                    raise self.error
                return self.result

        provider = market_caps.DefaultMarketCaps([
            Provider(error=RuntimeError("primary down")),
            Provider(result={"btc": 123}),
        ])
        self.assertEqual(provider.get_many(["btc"]), {"btc": 123})


if __name__ == "__main__":
    unittest.main()
