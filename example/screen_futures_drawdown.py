#!/usr/bin/env python
# coding: utf-8
"""Find Gate perpetual contracts with a large six-month drawdown.

The Gate futures API supplies contracts and candlesticks.  Market cap is not a
Gate field, so this example obtains USD market caps from CoinGecko's public
``/coins/markets`` endpoint.  Coin symbols are not globally unique; use
``--coin-id-overrides`` to provide a JSON object when a symbol needs an exact
CoinGecko id, for example ``{"TON": "toncoin"}``.

Examples:
    python example/screen_futures_drawdown.py
    python example/screen_futures_drawdown.py --settles usdt btc --output result.json
"""
from __future__ import print_function

import argparse
import json
import os
import sys
import time
from datetime import datetime

import urllib3

# Permit ``python example/screen_futures_drawdown.py`` from a source checkout.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from gate_api import ApiClient, Configuration, FuturesApi


COINGECKO_API = "https://api.coingecko.com/api/v3"
SIX_MONTHS_SECONDS = 183 * 24 * 60 * 60


def base_currency(contract_name):
    """Return the underlying ticker from a Gate perpetual contract name."""
    return contract_name.rsplit("_", 1)[0].lstrip("0123456789").lower()


def load_overrides(path):
    if not path:
        return {}
    with open(path, "r") as handle:
        data = json.load(handle)
    return {key.lower(): value for key, value in data.items()}


class CoinGeckoMarketCaps(object):
    """Small read-only client which maps a ticker to the largest matching coin."""

    def __init__(self, overrides=None, http=None):
        self.overrides = overrides or {}
        self.http = http or urllib3.PoolManager()
        self._coins_by_symbol = None
        self._cache = {}

    def _get_json(self, path, fields):
        response = self.http.request("GET", COINGECKO_API + path, fields=fields,
                                     timeout=urllib3.Timeout(connect=5.0, read=30.0))
        if response.status != 200:
            raise RuntimeError("CoinGecko request failed (%s): %s" %
                               (response.status, response.data.decode("utf-8", "replace")))
        return json.loads(response.data.decode("utf-8"))

    def _coin_ids(self, symbols):
        if self._coins_by_symbol is None:
            coins = self._get_json("/coins/list", {"include_platform": "false"})
            self._coins_by_symbol = {}
            for coin in coins:
                self._coins_by_symbol.setdefault(coin["symbol"].lower(), []).append(coin["id"])
        ids = set()
        for symbol in symbols:
            ids.update(self._coins_by_symbol.get(symbol, []))
        return ids

    def get_many(self, symbols):
        """Return ``{lowercase_ticker: USD_market_cap}`` for known symbols."""
        symbols = set(symbols)
        missing = [symbol for symbol in symbols if symbol not in self._cache]
        ids = set(self.overrides[symbol] for symbol in missing if symbol in self.overrides)
        ids.update(self._coin_ids([symbol for symbol in missing if symbol not in self.overrides]))
        markets = []
        ids = sorted(ids)
        for start in range(0, len(ids), 250):
            markets.extend(self._get_json("/coins/markets", {
                "vs_currency": "usd", "ids": ",".join(ids[start:start + 250]),
                "per_page": 250, "page": 1,
            }))
        by_symbol = {}
        by_id = {}
        for market in markets:
            by_id[market["id"]] = market.get("market_cap")
            symbol = market["symbol"].lower()
            cap = market.get("market_cap")
            if cap is not None and (symbol not in by_symbol or cap > by_symbol[symbol]):
                by_symbol[symbol] = cap
        for symbol in missing:
            self._cache[symbol] = by_id.get(self.overrides[symbol]) if symbol in self.overrides \
                else by_symbol.get(symbol)
        return {symbol: self._cache[symbol] for symbol in symbols if self._cache[symbol] is not None}


def candles_for_contract(api, settle, contract, start, end):
    return api.list_futures_candlesticks(
        settle, contract, _from=start, to=end, interval="4h", limit=2000)


def screen_contracts(api, market_caps, settles=("usdt",), now=None,
                     min_market_cap=8000000, max_market_cap=100000000,
                     min_drawdown_pct=70, limit=30):
    """Return matching contracts, ordered by largest drawdown first.

    ``market_caps`` must provide a ``get_many(iterable)`` method returning USD
    market caps keyed by lower-case base currency.  This keeps the screening
    logic testable and allows replacement with a paid market-data provider.
    """
    end = int(time.time() if now is None else now)
    start = end - SIX_MONTHS_SECONDS
    contracts = []
    for settle in settles:
        contracts.extend((settle, contract) for contract in api.list_futures_contracts(settle)
                         if not contract.in_delisting)
    caps = market_caps.get_many(base_currency(contract.name) for _, contract in contracts)
    results = []
    for settle, contract in contracts:
        symbol = base_currency(contract.name)
        market_cap = caps.get(symbol)
        if market_cap is None or not min_market_cap < market_cap < max_market_cap:
            continue
        candles = candles_for_contract(api, settle, contract.name, start, end)
        if not candles:
            continue
        peak = max(float(candle.h) for candle in candles)
        latest = max(candles, key=lambda candle: candle.t)
        last_price = float(latest.c)
        if peak <= 0 or last_price <= 0:
            continue
        drawdown_pct = (peak - last_price) / peak * 100
        if drawdown_pct > min_drawdown_pct:
            results.append({
                "contract": contract.name,
                "settle": settle,
                "market_cap_usd": market_cap,
                "six_month_high": peak,
                "latest_close": last_price,
                "drawdown_pct": round(drawdown_pct, 2),
                "latest_candle_time": datetime.utcfromtimestamp(float(latest.t)).isoformat() + "Z",
            })
    return sorted(results, key=lambda item: item["drawdown_pct"], reverse=True)[:limit]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settles", nargs="+", default=["usdt", "btc"],
                        help="Gate settlement currencies, e.g. usdt btc (default: usdt btc)")
    parser.add_argument("--coin-id-overrides", help="JSON file mapping ticker to CoinGecko coin id")
    parser.add_argument("--output", help="Write JSON results to this path instead of stdout")
    args = parser.parse_args(argv)
    api = FuturesApi(ApiClient(Configuration()))
    results = screen_contracts(api, CoinGeckoMarketCaps(load_overrides(args.coin_id_overrides)),
                               settles=tuple(args.settles))
    payload = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w") as handle:
            handle.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
