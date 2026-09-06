# coding: utf-8
"""Read USD cryptocurrency market caps from public market-data APIs.

The ranking tool accepts an injectable provider so tests and callers can avoid
network access.  The default provider maps a ticker to the largest-market-cap
CoinGecko asset with that symbol, which is the safest automatic choice when
symbols are not globally unique.
"""
from __future__ import absolute_import

import json
import threading

import urllib3


COINGECKO_API = "https://api.coingecko.com/api/v3"
COINPAPRIKA_API = "https://api.coinpaprika.com/v1"
COINLORE_API = "https://api.coinlore.net/api"


def _default_http():
    """Build a direct HTTP client; market data must never use a proxy."""
    return urllib3.PoolManager()


class CoinGeckoMarketCaps(object):
    """Resolve lower-case tickers to USD market capitalisation values."""

    def __init__(self, http=None):
        self.http = http or _default_http()
        self._coins_by_symbol = None
        self._cache = {}

    def _get_json(self, path, fields):
        response = self.http.request(
            "GET", COINGECKO_API + path, fields=fields,
            headers={"Accept": "application/json", "User-Agent": "gateapi-python-ranking/1.0"},
            timeout=urllib3.Timeout(connect=5.0, read=30.0),
        )
        if response.status != 200:
            detail = response.data.decode("utf-8", "replace")
            raise RuntimeError("CoinGecko request failed (%s): %s" %
                               (response.status, detail[:500]))
        return json.loads(response.data.decode("utf-8"))

    def _coin_ids(self, symbols):
        if self._coins_by_symbol is None:
            coins = self._get_json("/coins/list", {"include_platform": "false"})
            self._coins_by_symbol = {}
            for coin in coins:
                symbol = str(coin.get("symbol") or "").lower()
                coin_id = coin.get("id")
                if symbol and coin_id:
                    self._coins_by_symbol.setdefault(symbol, []).append(coin_id)
        ids = set()
        for symbol in symbols:
            ids.update(self._coins_by_symbol.get(symbol, []))
        return sorted(ids)

    def get_many(self, symbols, progress_callback=None, **_range):
        """Return ``{lowercase_ticker: market_cap_usd}`` for known tickers."""
        symbols = set(str(symbol).lower() for symbol in symbols if symbol)
        missing = sorted(symbol for symbol in symbols if symbol not in self._cache)
        ids = self._coin_ids(missing)
        if progress_callback is not None:
            progress_callback({
                "provider": "CoinGecko", "step": "coin_list",
                "completed": 1, "total": max(1, 1 + ((len(ids) + 249) // 250)),
                "matched": 0, "requested": len(symbols),
                "message": "CoinGecko 币种映射已加载",
            })
        markets = []
        batches = list(range(0, len(ids), 250))
        for batch_index, start in enumerate(batches, 1):
            markets.extend(self._get_json("/coins/markets", {
                "vs_currency": "usd",
                "ids": ",".join(ids[start:start + 250]),
                "per_page": 250,
                "page": 1,
            }))
            if progress_callback is not None:
                progress_callback({
                    "provider": "CoinGecko", "step": "market_batch",
                    "completed": 1 + batch_index, "total": 1 + len(batches),
                    "matched": len(markets), "requested": len(symbols),
                    "message": "CoinGecko 市值批次 %d/%d" % (batch_index, len(batches)),
                })
        by_symbol = {}
        for market in markets:
            symbol = str(market.get("symbol") or "").lower()
            cap = market.get("market_cap")
            if symbol and cap is not None and (symbol not in by_symbol or cap > by_symbol[symbol]):
                by_symbol[symbol] = cap
        for symbol in missing:
            self._cache[symbol] = by_symbol.get(symbol)
        return {symbol: self._cache[symbol] for symbol in symbols
                if self._cache.get(symbol) is not None}


class CoinPaprikaMarketCaps(object):
    """Resolve many tickers from CoinPaprika's single all-tickers response."""

    def __init__(self, http=None):
        self.http = http or _default_http()
        self._caps = None

    def get_many(self, symbols, progress_callback=None, **_range):
        symbols = set(str(symbol).lower() for symbol in symbols if symbol)
        if self._caps is None:
            response = self.http.request(
                "GET", COINPAPRIKA_API + "/tickers", fields={"quotes": "USD"},
                headers={"Accept": "application/json",
                         "User-Agent": "gateapi-python-ranking/1.0"},
                timeout=urllib3.Timeout(connect=5.0, read=30.0),
            )
            if response.status != 200:
                raise RuntimeError("CoinPaprika request failed (%s)" % response.status)
            tickers = json.loads(response.data.decode("utf-8"))
            caps = {}
            for ticker in tickers:
                symbol = str(ticker.get("symbol") or "").lower()
                cap = ((ticker.get("quotes") or {}).get("USD") or {}).get("market_cap")
                if symbol and cap is not None and (symbol not in caps or cap > caps[symbol]):
                    caps[symbol] = cap
            self._caps = caps
            if progress_callback is not None:
                progress_callback({
                    "provider": "CoinPaprika", "step": "tickers",
                    "completed": 1, "total": 1,
                    "matched": len(symbols.intersection(self._caps)),
                    "requested": len(symbols),
                    "message": "CoinPaprika 全量市值已加载",
                })
        return {symbol: self._caps[symbol] for symbol in symbols if symbol in self._caps}


class CoinLoreMarketCaps(object):
    """Resolve tickers from direct, market-cap-sorted CoinLore pages."""

    PAGE_SIZE = 100
    DEFAULT_MAX_PAGES = 10

    def __init__(self, http=None, max_pages=DEFAULT_MAX_PAGES):
        self.http = http or _default_http()
        self.max_pages = max(1, int(max_pages))
        self._caps = {}
        self._next_start = 0
        self._pages_loaded = 0
        self._lowest_loaded_cap = None
        self._exhausted = False
        self._lock = threading.RLock()

    def _load_page(self):
        response = self.http.request(
            "GET", COINLORE_API + "/tickers/",
            fields={"start": self._next_start, "limit": self.PAGE_SIZE},
            headers={"Accept": "application/json",
                     "User-Agent": "gateapi-python-ranking/1.0"},
            timeout=urllib3.Timeout(connect=5.0, read=15.0),
            retries=False,
        )
        if response.status != 200:
            raise RuntimeError("CoinLore request failed (%s)" % response.status)
        payload = json.loads(response.data.decode("utf-8"))
        rows = payload.get("data") or []
        if not rows:
            self._exhausted = True
            return
        page_caps = []
        for row in rows:
            symbol = str(row.get("symbol") or "").lower()
            try:
                cap = float(row.get("market_cap_usd"))
            except (TypeError, ValueError):
                continue
            if cap <= 0 or not symbol:
                continue
            page_caps.append(cap)
            if symbol not in self._caps or cap > self._caps[symbol]:
                self._caps[symbol] = cap
        self._next_start += len(rows)
        self._pages_loaded += 1
        if page_caps:
            self._lowest_loaded_cap = min(page_caps)
        total = int((payload.get("info") or {}).get("coins_num") or 0)
        if len(rows) < self.PAGE_SIZE or (total and self._next_start >= total):
            self._exhausted = True

    def get_many(self, symbols, min_market_cap_usd=None,
                 progress_callback=None, **_range):
        symbols = set(str(symbol).lower() for symbol in symbols if symbol)
        minimum = float(min_market_cap_usd) if min_market_cap_usd is not None else None
        with self._lock:
            while not self._exhausted and self._pages_loaded < self.max_pages:
                all_found = all(symbol in self._caps for symbol in symbols)
                below_minimum = (minimum is not None and self._lowest_loaded_cap is not None
                                 and self._lowest_loaded_cap < minimum)
                if all_found or below_minimum:
                    break
                self._load_page()
                if progress_callback is not None:
                    progress_callback({
                        "provider": "CoinLore", "step": "page",
                        "completed": self._pages_loaded, "total": self.max_pages,
                        "matched": len(symbols.intersection(self._caps)),
                        "requested": len(symbols),
                        "message": "CoinLore 市值第 %d/%d 页" %
                                   (self._pages_loaded, self.max_pages),
                    })
            return {symbol: self._caps[symbol] for symbol in symbols if symbol in self._caps}


class DefaultMarketCaps(object):
    """Use the fast bulk source first and CoinGecko as a fallback."""

    def __init__(self, providers=None):
        self.providers = providers or [
            # CoinLore is reachable reliably from the target runtime and is
            # bounded by ``DEFAULT_MAX_PAGES``. CoinGecko/Paprika remain
            # fallbacks when CoinLore returns no matching symbols.
            CoinLoreMarketCaps(), CoinGeckoMarketCaps(), CoinPaprikaMarketCaps(),
        ]

    def get_many(self, symbols, progress_callback=None, **market_cap_range):
        symbols = set(str(symbol).lower() for symbol in symbols if symbol)
        errors = []
        for provider_index, provider in enumerate(self.providers, 1):
            provider_name = type(provider).__name__.replace("MarketCaps", "")
            try:
                if progress_callback is not None:
                    progress_callback({
                        "provider": provider_name, "step": "provider",
                        "completed": provider_index - 1, "total": len(self.providers),
                        "matched": 0, "requested": len(symbols),
                        "message": "正在连接市值数据源 %s" % provider_name,
                    })
                result = provider.get_many(
                    symbols, progress_callback=progress_callback, **market_cap_range
                )
                if result or not symbols:
                    if progress_callback is not None:
                        progress_callback({
                            "provider": provider_name, "step": "completed",
                            "completed": provider_index, "total": len(self.providers),
                            "matched": len(result), "requested": len(symbols),
                            "message": "%s 已匹配 %d/%d 个币种" %
                                       (provider_name, len(result), len(symbols)),
                        })
                    return result
                errors.append("%s returned no data" % type(provider).__name__)
            except Exception as exc:  # noqa: BLE001 - fallback is intentional
                errors.append("%s: %s" % (type(provider).__name__, str(exc)[:160]))
        raise RuntimeError("市值数据源暂时不可用：%s" % "; ".join(errors))
