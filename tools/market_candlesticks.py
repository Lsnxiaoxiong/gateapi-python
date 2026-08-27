# coding: utf-8
"""Historical candlesticks for Gate spot and perpetual futures.

This module fetches historical OHLCV candles for either a spot currency
pair or a perpetual futures contract across a range of *intervals*
(``1h 2h 3h 4h 6h 8h 12h 1d 2d 3d 7d 30d``), normalizes the two different
Gate response shapes into a unified list of dictionaries, and de-duplicates
+ time-sorts the result.

Interval support
----------------
Gate natively supports every requested interval except ``3h`` (rejected with
``INVALID_PARAM_VALUE``). The ``3h`` interval is therefore synthesized by
fetching the underlying ``1h`` candles and merging each consecutive group of
three into a single candle (open = first open, close = last close,
high/low = max/min, volume = sum).

History depth ceiling
---------------------
Gate enforces a per-interval *point-count* ceiling: "Maximum 10000 points
recently are allowed". Concretely a request window whose ``to`` is older than
``now - 10000*interval_seconds`` is rejected with ``Candlestick too long ago``.
For ``1h`` that is ~416 days; for ``1d`` it is ~27 years (so it never binds in
practice — the contract listing date binds first). This module validates the
requested span up front and raises a clear :class:`ValueError` whenever the
look-back would exceed ``MAX_LOOKBACK_POINTS`` points for the interval, rather
than letting the API return an opaque 400.

Per-request limits
------------------
A single request returns at most 1000 points for spot and 2000 for futures.
Because the range is inclusive ``[from, to]``, a span of *N* intervals yields
*N + 1* points, so the page sizes are 999 (spot) and 1999 (futures) to stay
safely inside the ceilings.

``gate_api`` is imported lazily inside the default client factory, so
importing this module has no side effects and does not require the SDK.

Example::

    from tools.market_candlesticks import get_candlesticks

    # one year of hourly candles
    spot = get_candlesticks("spot", "BTC_USDT", interval="1h", span_days=365)
    # five years of daily candles (long interval → deep look-back)
    futures = get_candlesticks("futures", "BTC_USDT", interval="1d", span_days=365*5)
"""
from __future__ import absolute_import

import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

#: Rolling one year expressed in seconds (365 * 24 * 3600).
YEAR_SECONDS = 365 * 24 * 3600
#: Seconds covered by a single ``1h`` candle.
HOUR_SECONDS = 3600
#: ``1h`` interval value understood by both Gate candlestick endpoints.
INTERVAL = "1h"

#: Seconds covered by each supported interval. Gate natively accepts every key
#: here except ``"3h"`` (rejected with INVALID_PARAM_VALUE); ``3h`` is later
#: synthesized from ``1h`` via :func:`_aggregate_candles`.
INTERVAL_SECONDS = {
    "1h": 3600,
    "2h": 7200,
    "3h": 10800,   # synthesized from 1h (not native)
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "2d": 172800,
    "3d": 259200,
    "7d": 604800,
    "30d": 2592000,
}
#: Tuple of all supported interval strings (stable iteration order).
VALID_INTERVALS = tuple(INTERVAL_SECONDS.keys())
#: For intervals Gate does not support natively, the base interval to fetch
#: and the integer ratio to merge. Only ``3h`` needs synthesis today.
AGGREGATE_FROM = {
    "3h": ("1h", 3),
}

#: Gate enforces "Maximum 10000 points recently are allowed" per interval.
#: A request whose window ends older than ``now - 10000*interval_seconds``
#: is rejected with ``Candlestick too long ago``. We keep a 50-point safety
#: margin below the hard ceiling so boundary candles are always returned.
MAX_LOOKBACK_POINTS = 9950

#: Maximum candlesticks returned per spot request.  Gate's spot endpoint
#: enforces an inclusive ``[from, to]`` range, so a span of *N* hourly
#: intervals yields *N + 1* points and is rejected when that exceeds 1000.
#: Therefore the largest safe window spans 999 hours (== 1000 points).
SPOT_PAGE_SIZE = 999
#: Maximum candlesticks returned per futures request.  Gate documents a
#: 2000-point ceiling; a 1999-hour window yields 2000 points and stays
#: safely inside that ceiling even with the inclusive range semantics.
FUTURES_PAGE_SIZE = 1999

#: Supported market types.
VALID_MARKET_TYPES = ("spot", "futures")

#: Default total request timeout in seconds for a single page request.
DEFAULT_REQUEST_TIMEOUT = 30.0
#: Default number of retries for transient errors.
DEFAULT_MAX_RETRIES = 3
#: Base backoff in seconds for exponential retry.
RETRY_BASE_BACKOFF = 0.5

# Gate spot candlestick arrays follow the order ``[t, v, c, h, l, o, sum]`` which
# matches the FuturesCandlestick attribute order (t, v, c, h, l, o, sum).
_SPOT_INDEX = {
    "timestamp": 0,
    "volume": 1,
    "close": 2,
    "high": 3,
    "low": 4,
    "open": 5,
    "sum": 6,
}


def _normalize_market_type(market_type):
    """Validate and normalize ``market_type`` to a lower-case canonical value."""
    if market_type is None:
        raise ValueError("market_type is required (spot or futures)")
    value = str(market_type).strip().lower()
    if not value:
        raise ValueError("market_type must not be empty (spot or futures)")
    if value not in VALID_MARKET_TYPES:
        raise ValueError(
            "market_type must be 'spot' or 'futures', got %r" % market_type
        )
    return value


def _normalize_trading_pair(trading_pair):
    """Validate and normalize a trading pair/contract name.

    Whitespace is stripped, ``-`` is replaced with ``_`` and the result is
    upper-cased (e.g. ``btc-usdt`` -> ``BTC_USDT``).
    """
    if trading_pair is None:
        raise ValueError("trading_pair is required")
    value = str(trading_pair).strip().replace("-", "_").upper()
    if not value:
        raise ValueError("trading_pair must not be empty")
    return value


def _normalize_settle(settle):
    """Validate and normalize the settlement currency for futures."""
    if settle is None:
        raise ValueError("settle is required for futures market type")
    value = str(settle).strip().lower()
    if not value:
        raise ValueError("settle must not be empty for futures market type")
    return value


def _normalize_interval(interval):
    """Validate and normalize an interval string.

    Accepts ``1h 2h 3h 4h 6h 8h 12h 1d 2d 3d 7d 30d`` (case-insensitive, no
    surrounding whitespace). Returns the canonical lower-case value.
    """
    if interval is None:
        raise ValueError("interval is required (1h,2h,3h,4h,6h,8h,12h,1d,2d,3d,7d,30d)")
    value = str(interval).strip().lower()
    if value not in INTERVAL_SECONDS:
        raise ValueError(
            "interval must be one of %s, got %r"
            % (", ".join(VALID_INTERVALS), interval)
        )
    return value


def _interval_seconds(interval):
    """Return the number of seconds covered by one candle of ``interval``."""
    return INTERVAL_SECONDS[_normalize_interval(interval)]


def _native_interval(interval):
    """Return the interval string actually sent to the Gate API.

    Intervals that Gate does not support (currently only ``3h``) are fetched
    via their aggregate base interval instead.
    """
    interval = _normalize_interval(interval)
    return AGGREGATE_FROM.get(interval, (interval, None))[0]


def _resolve_end_time(end_time):
    """Resolve the end timestamp to an integer Unix second value."""
    if end_time is None:
        return int(time.time())
    value = int(end_time)
    if value <= 0:
        raise ValueError("end_time must be a positive Unix timestamp in seconds")
    return value


def _align_to_interval_boundary(end, interval_seconds):
    """Floor ``end`` to a candle-open boundary of ``interval_seconds``.

    Only fully-completed candles are kept inside the result window
    ``[start, end)``, which keeps the result stable across repeated calls.
    """
    return end - (end % interval_seconds)


def _page_windows(start, end, page_size, interval_seconds):
    """Yield successive ``[from, to)`` windows of at most ``page_size`` candles.

    Windows are half-open: a window ``[from, to)`` covers candles whose
    timestamp equals ``from`` up to but not including ``to``. The next window
    starts exactly at ``to``, so adjacent windows do not overlap. A final
    de-duplication pass guards against any API boundary surprises.
    """
    page_seconds = page_size * interval_seconds
    current = start
    while current < end:
        to = min(current + page_seconds, end)
        yield current, to
        current = to


def _parse_decimal_text(value):
    """Return a normalized decimal string, or ``None`` if not a positive number."""
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not dec.is_finite() or dec <= 0:
        return None
    return _decimal_to_text(dec)


def _decimal_to_text(dec):
    """Format a Decimal as a plain string without trailing zeros or exponents."""
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def _normalize_spot_candle(raw):
    """Convert one raw spot candlestick array into the standard dict, or None."""
    if not raw:
        return None
    try:
        timestamp = int(float(raw[_SPOT_INDEX["timestamp"]]))
    except (IndexError, TypeError, ValueError):
        return None
    return _build_candle(
        timestamp=timestamp,
        open_raw=raw[_SPOT_INDEX["open"]] if len(raw) > _SPOT_INDEX["open"] else None,
        high_raw=raw[_SPOT_INDEX["high"]] if len(raw) > _SPOT_INDEX["high"] else None,
        low_raw=raw[_SPOT_INDEX["low"]] if len(raw) > _SPOT_INDEX["low"] else None,
        close_raw=raw[_SPOT_INDEX["close"]] if len(raw) > _SPOT_INDEX["close"] else None,
        volume_raw=raw[_SPOT_INDEX["volume"]] if len(raw) > _SPOT_INDEX["volume"] else None,
    )


def _normalize_futures_candle(raw):
    """Convert one FuturesCandlestick-like object into the standard dict, or None."""
    if raw is None:
        return None
    try:
        timestamp = int(float(getattr(raw, "t")))
    except (TypeError, ValueError):
        return None
    return _build_candle(
        timestamp=timestamp,
        open_raw=getattr(raw, "o", None),
        high_raw=getattr(raw, "h", None),
        low_raw=getattr(raw, "l", None),
        close_raw=getattr(raw, "c", None),
        volume_raw=getattr(raw, "v", None),
    )


def _build_candle(timestamp, open_raw, high_raw, low_raw, close_raw, volume_raw):
    """Assemble a standard candle dict, dropping records with invalid prices."""
    open_text = _parse_decimal_text(open_raw)
    high_text = _parse_decimal_text(high_raw)
    low_text = _parse_decimal_text(low_raw)
    close_text = _parse_decimal_text(close_raw)
    # All OHLC prices must be valid and positive; volume may be zero/missing.
    if not (open_text and high_text and low_text and close_text):
        return None
    volume_text = _parse_decimal_text(volume_raw) if volume_raw is not None else None
    if volume_text is None:
        # Volume can legitimately be "0"; keep a normalized zero rather than None.
        try:
            vol_dec = Decimal(str(volume_raw)) if volume_raw is not None else Decimal(0)
        except (InvalidOperation, TypeError, ValueError):
            vol_dec = Decimal(0)
        volume_text = _decimal_to_text(vol_dec) if vol_dec.is_finite() and vol_dec >= 0 else "0"
    return {
        "timestamp": timestamp,
        "open": open_text,
        "high": high_text,
        "low": low_text,
        "close": close_text,
        "volume": volume_text,
    }


def _is_retryable_exception(exc):
    """Return True for transient network / rate-limit / server errors."""
    # urllib3 / requests style connection errors.
    if isinstance(exc, Exception):  # noqa: B019 - intentional broad base check
        name = type(exc).__name__
        if name in ("HTTPError", "ConnectionError", "Timeout", "ReadTimeout",
                    "ConnectTimeout", "MaxRetryError", "ProtocolError"):
            return True
    # Gate SDK ApiException with retryable HTTP status.
    status = getattr(exc, "status", None)
    if status is not None:
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            status_int = None
        if status_int in (429, 500, 502, 503, 504):
            return True
    # Gate SDK GateApiException exposes a ``label`` but not always ``status``;
    # treat rate-limit labels defensively.
    label = getattr(exc, "label", None)
    if isinstance(label, str) and "rate" in label.lower():
        return True
    return False


def _call_with_retry(func, *args, **kwargs):
    """Invoke ``func`` with bounded retries on transient errors."""
    max_retries = int(kwargs.pop("_max_retries", DEFAULT_MAX_RETRIES))
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - isolate retryable vs not
            last_exc = exc
            if not _is_retryable_exception(exc) or attempt >= max_retries:
                raise
            backoff = RETRY_BASE_BACKOFF * (2 ** attempt)
            time.sleep(backoff)
    raise last_exc  # pragma: no cover - loop always returns or raises earlier


def _default_spot_api(request_timeout):
    """Create a default SpotApi backed by a fresh public ApiClient.

    ``gate_api`` is imported lazily so this module remains importable without
    the SDK installed; only callers that do not inject an API touch this path.
    The per-request timeout is enforced via the ``_request_timeout`` argument
    passed by :func:`_fetch_spot_page`; the Gate SDK exposes no client-level
    timeout attribute, so nothing is set here.
    """
    from gate_api import ApiClient, Configuration, SpotApi  # noqa: delayed import
    config = Configuration()
    config.retries = False
    client = ApiClient(config)
    return SpotApi(client)


def _default_futures_api(request_timeout):
    """Create a default FuturesApi backed by a fresh public ApiClient.

    The per-request timeout is enforced via the ``_request_timeout`` argument
    passed by :func:`_fetch_futures_page`; the Gate SDK exposes no client-level
    timeout attribute, so nothing is set here.
    """
    from gate_api import ApiClient, Configuration, FuturesApi  # noqa: delayed import
    config = Configuration()
    config.retries = False
    client = ApiClient(config)
    return FuturesApi(client)


def _fetch_spot_page(spot_api, currency_pair, from_ts, to_ts, request_timeout,
                   max_retries=DEFAULT_MAX_RETRIES, interval=INTERVAL):
    """Fetch one spot candlestick page with retry."""
    kwargs = {"_from": from_ts, "to": to_ts, "interval": interval,
              "_max_retries": max_retries}
    if request_timeout is not None:
        kwargs["_request_timeout"] = request_timeout
    return _call_with_retry(
        spot_api.list_candlesticks, currency_pair, **kwargs
    )


def _fetch_futures_page(futures_api, settle, contract, from_ts, to_ts,
                         request_timeout, max_retries=DEFAULT_MAX_RETRIES,
                         interval=INTERVAL):
    """Fetch one futures candlestick page with retry."""
    kwargs = {"_from": from_ts, "to": to_ts, "interval": interval,
              "_max_retries": max_retries}
    if request_timeout is not None:
        kwargs["_request_timeout"] = request_timeout
    return _call_with_retry(
        futures_api.list_futures_candlesticks, settle, contract, **kwargs
    )


def _aggregate_candles(base_candles, base_interval, target_interval):
    """Merge consecutive ``base_interval`` candles into ``target_interval``.

    Used to synthesize intervals Gate does not support natively (e.g. ``3h``
    from ``1h``). The base candles must already be sorted ascending by
    timestamp and de-duplicated. Each consecutive group of *ratio* candles
    whose first timestamp aligns to a ``target_interval`` boundary is merged
    into one candle (open = first open, close = last close, high/low =
    max/min, volume = sum). Trailing candles that do not fill a complete
    group are dropped so the result always represents complete candles.
    """
    base_sec = INTERVAL_SECONDS[_normalize_interval(base_interval)]
    target_sec = INTERVAL_SECONDS[_normalize_interval(target_interval)]
    if target_sec <= base_sec or target_sec % base_sec != 0:
        raise ValueError(
            "cannot aggregate %s -> %s (target must be a positive multiple of base)"
            % (base_interval, target_interval))
    ratio = target_sec // base_sec
    out = []
    group = []
    group_start = None
    for candle in base_candles:
        ts = candle.get("timestamp")
        if ts is None:
            continue
        if group_start is None or ts < group_start or ts >= group_start + target_sec:
            # Start a new group at the boundary ts falls into.
            group_start = ts - (ts % target_sec)
            group = []
        group.append(candle)
        if len(group) == ratio:
            out.append(_merge_candle_group(group))
            group = []
            group_start = None
    return out


def _merge_candle_group(group):
    """Merge a list of complete candles into one OHLCV dict."""
    first = group[0]
    last = group[-1]
    high = max((Decimal(c["high"]) for c in group if c.get("high")), default=Decimal(0))
    low = min((Decimal(c["low"]) for c in group if c.get("low")), default=Decimal(0))
    volume = sum((Decimal(c["volume"]) for c in group if c.get("volume")), Decimal(0))
    return {
        "timestamp": first["timestamp"],
        "open": first["open"],
        "high": _decimal_to_text(high),
        "low": _decimal_to_text(low),
        "close": last["close"],
        "volume": _decimal_to_text(volume),
    }


def get_one_year_hourly_candlesticks(market_type, trading_pair, settle="usdt",
                                     end_time=None, spot_api=None, futures_api=None,
                                     request_timeout=DEFAULT_REQUEST_TIMEOUT,
                                     max_retries=DEFAULT_MAX_RETRIES):
    # noqa: E501 - long signature kept for clarity and explicit injection
    """Backward-compatible wrapper for one year of ``1h`` candlesticks.

    Equivalent to ``get_candlesticks(..., interval="1h", span_days=365)``.
    Kept for callers (and tests) that rely on the original name/signature.
    """
    return get_candlesticks(
        market_type=market_type,
        trading_pair=trading_pair,
        interval="1h",
        span_days=365,
        settle=settle,
        end_time=end_time,
        spot_api=spot_api,
        futures_api=futures_api,
        request_timeout=request_timeout,
        max_retries=max_retries,
    )


def get_candlesticks(market_type, trading_pair, interval="1h", span_days=365,
                     settle="usdt", end_time=None, spot_api=None, futures_api=None,
                     request_timeout=DEFAULT_REQUEST_TIMEOUT,
                     max_retries=DEFAULT_MAX_RETRIES):
    # noqa: E501 - long signature kept for clarity and explicit injection
    """Fetch historical OHLCV candles for one market across supported intervals.

    The result is a list of dictionaries sorted by ``timestamp`` ascending,
    each containing the keys ``timestamp``, ``open``, ``high``, ``low``,
    ``close`` and ``volume``. Price/volume values are decimal strings. Only
    candles whose timestamp falls in ``[start, end)`` are returned, where
    ``end`` is aligned down to the candle-open boundary of ``interval`` so
    that the in-progress candle is excluded (keeping results stable across
    repeated calls).

    Supported intervals: ``1h 2h 3h 4h 6h 8h 12h 1d 2d 3d 7d 30d``. Every
    interval is native to Gate except ``3h``, which is synthesized by merging
    consecutive groups of three ``1h`` candles.

    History-depth ceiling: Gate rejects any request window ending older than
    ~``now - 10000*interval_seconds`` with "Candlestick too long ago". This
    function validates the requested span up front and raises a clear
    :class:`ValueError` when the look-back would exceed
    :data:`MAX_LOOKBACK_POINTS` points for the interval. For ``1h`` that caps
    the look-back at ~414 days; for ``1d`` and longer the ceiling never binds
    (it corresponds to 27–833 years), so long intervals can freely look back
    to the contract listing date.

    :param str market_type: ``"spot"`` or ``"futures"`` (case/whitespace tolerant).
    :param str trading_pair: Currency pair (spot) or contract name (futures).
        ``-`` is normalized to ``_`` and the value is upper-cased.
    :param str interval: One of ``1h 2h 3h 4h 6h 8h 12h 1d 2d 3d 7d 30d``.
    :param int span_days: Look-back span in days. Defaults to 365 (one year).
        For long intervals you may safely pass much larger values (e.g.
        ``365*5``) to reach the contract listing date.
    :param str settle: Settlement currency for futures (e.g. ``"usdt"``).
        Ignored for spot.
    :param int end_time: Optional end time as Unix seconds. Defaults to now.
    :param spot_api: Optional :class:`gate_api.SpotApi` (or compatible) instance.
        When omitted, a public client is created lazily and closed afterwards.
    :param futures_api: Optional :class:`gate_api.FuturesApi` (or compatible)
        instance. When omitted, a public client is created lazily and closed
        afterwards. Injected clients are never closed by this function.
    :param request_timeout: Per-request timeout in seconds.
    :param int max_retries: Maximum retries for transient errors per page.
    :returns: List of standardized candle dicts, ascending by timestamp.
    :raises ValueError: If ``market_type``/``trading_pair``/``interval`` is
        invalid, or the requested span exceeds the interval's history ceiling.
    :raises Exception: Transient errors are retried; non-retryable errors and
        retries-exhausted errors propagate to the caller.
    """
    market = _normalize_market_type(market_type)
    pair = _normalize_trading_pair(trading_pair)
    interval = _normalize_interval(interval)
    iv_sec = INTERVAL_SECONDS[interval]
    if market == "futures":
        settle = _normalize_settle(settle)

    span_days = int(span_days)
    if span_days <= 0:
        raise ValueError("span_days must be a positive integer")

    end = _resolve_end_time(end_time)
    end = _align_to_interval_boundary(end, iv_sec)
    span_seconds = span_days * 86400
    start = end - span_seconds
    # Align start up to a candle boundary so the oldest requested candle is
    # complete; this also makes span point count exact.
    start = _align_to_interval_boundary(start, iv_sec)

    _validate_history_ceiling(start, end, end_time, iv_sec, interval)

    # The interval actually sent to the API. For synthesized intervals (e.g.
    # 3h) we fetch the base interval over the *same* span and merge after.
    native_iv, merge_ratio = AGGREGATE_FROM.get(interval, (interval, None))
    fetch_iv_sec = INTERVAL_SECONDS[native_iv]
    fetch_start = _align_to_interval_boundary(start, fetch_iv_sec)

    owns_client = False
    try:
        if market == "spot":
            if spot_api is None:
                spot_api = _default_spot_api(request_timeout)
                owns_client = True
            candles = _collect_spot(spot_api, pair, fetch_start, end,
                                    request_timeout, max_retries, native_iv,
                                    fetch_iv_sec)
        else:
            if futures_api is None:
                futures_api = _default_futures_api(request_timeout)
                owns_client = True
            candles = _collect_futures(futures_api, settle, pair, fetch_start, end,
                                       request_timeout, max_retries, native_iv,
                                       fetch_iv_sec)
        if merge_ratio is not None:
            candles = _aggregate_candles(candles, native_iv, interval)
        return _merge_and_finalize(candles, start, end, iv_sec)
    finally:
        if owns_client:
            client = getattr(spot_api, "api_client", None) or getattr(futures_api, "api_client", None)
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()


def _validate_history_ceiling(start, end, end_time, iv_sec, interval):
    """Raise ``ValueError`` if the window falls outside the 10000-point ceiling.

    Gate rejects any request whose window ends older than
    ``now - 10000*interval_seconds``. During backward paging the oldest page's
    ``to`` is approximately ``start + page_size*iv_sec``; to be safe we simply
    require the *whole* window start to lie within the recent
    :data:`MAX_LOOKBACK_POINTS` points, which is stricter but never rejects a
    fetch that the API would have accepted. ``end_time is None`` means "now",
    so the only ceiling that matters is the look-back from now; when the
    caller pins an explicit recent ``end_time`` we still guard against asking
    for more points than the interval has ever existed.
    """
    now = int(time.time())
    oldest_allowed = now - MAX_LOOKBACK_POINTS * iv_sec
    if start < oldest_allowed:
        max_days = MAX_LOOKBACK_POINTS * iv_sec // 86400
        raise ValueError(
            "Requested look-back for interval %r exceeds Gate's %d-point history "
            "ceiling (~%d days). Reduce span_days to <= %d, or use a longer "
            "interval (1d/7d/30d can look back to the contract listing date)."
            % (interval, MAX_LOOKBACK_POINTS, max_days, max_days)
        )


def _collect_spot(spot_api, pair, start, end, request_timeout, max_retries,
                  interval, iv_sec):
    """Fetch and normalize all spot candle pages for ``[start, end)``."""
    out = []
    page_size = SPOT_PAGE_SIZE
    for from_ts, to_ts in _page_windows(start, end, page_size, iv_sec):
        raw_page = _fetch_spot_page(spot_api, pair, from_ts, to_ts, request_timeout,
                                    max_retries, interval=interval)
        for raw in raw_page or []:
            candle = _normalize_spot_candle(raw)
            if candle is not None:
                out.append(candle)
    return out


def _collect_futures(futures_api, settle, contract, start, end, request_timeout,
                     max_retries, interval, iv_sec):
    """Fetch and normalize all futures candle pages for ``[start, end)``."""
    out = []
    page_size = FUTURES_PAGE_SIZE
    for from_ts, to_ts in _page_windows(start, end, page_size, iv_sec):
        raw_page = _fetch_futures_page(futures_api, settle, contract, from_ts, to_ts,
                                       request_timeout, max_retries, interval=interval)
        for raw in raw_page or []:
            candle = _normalize_futures_candle(raw)
            if candle is not None:
                out.append(candle)
    return out


def _merge_and_finalize(candles, start, end, iv_sec):
    """De-duplicate by timestamp, clip to ``[start, end)``, sort ascending."""
    by_ts = {}
    for candle in candles:
        if not candle:
            continue
        ts = candle.get("timestamp")
        if ts is None or ts < start or ts >= end:
            continue
        # Last occurrence wins; this is harmless because de-duped candles share
        # the same OHLC for a given timestamp.
        by_ts[ts] = candle
    return [by_ts[ts] for ts in sorted(by_ts)]


# ---------------------------------------------------------------------------
# Example entry point (no CLI)
# ---------------------------------------------------------------------------
# Importing the module above is side-effect free; the code below only runs when
# the file is executed directly (``python tools/market_candlesticks.py`` or
# ``python -m tools.market_candlesticks``). It is a small, self-contained demo
# of how to call :func:`get_candlesticks`; there is no argument
# parsing — edit the constants in :func:`main` to point at another pair.

def _format_ts(ts):
    """Format a Unix timestamp (seconds) as an ISO-8601 UTC string."""
    return datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%SZ")


def _format_candle(candle):
    """Return a one-line OHLCV string for a normalized candle dict."""
    return "%s  O=%s H=%s L=%s C=%s V=%s" % (
        _format_ts(candle["timestamp"]),
        candle["open"], candle["high"], candle["low"],
        candle["close"], candle["volume"],
    )


def _print_summary(candles, market, pair, settle, interval, span_days, sample):
    """Print a human-readable summary of the fetched candles."""
    label = "%s %s%s" % (market.upper(), pair, " (%s)" % settle if settle else "")
    print("=" * 78)
    print(label)
    print("interval    : %s  span_days=%d" % (interval, span_days))
    print("candle count: %d" % len(candles))
    if not candles:
        print("(no candles returned)")
        print("=" * 78)
        return
    print("time range  : %s  ..  %s" % (
        _format_ts(candles[0]["timestamp"]),
        _format_ts(candles[-1]["timestamp"]),
    ))
    print("-" * 78)
    head = candles[:sample]
    tail = candles[-sample:]
    print("first %d:" % len(head))
    for candle in head:
        print("  " + _format_candle(candle))
    print("last %d:" % len(tail))
    for candle in tail:
        print("  " + _format_candle(candle))
    print("=" * 78)


def main():
    """Example entry point: fetch candles and print a summary.

    This is a self-contained demonstration — no CLI, no argparse. Edit the
    constants below to query a different market/pair/interval, then run::

        python tools/market_candlesticks.py

    or::

        python -m tools.market_candlesticks

    The fetched candle list is returned for programmatic use.
    """
    # --- example parameters (edit as needed) --------------------------------
    market_type = "futures"
    trading_pair = "BTC_USDT"
    interval = "1d"        # 1h 2h 3h 4h 6h 8h 12h 1d 2d 3d 7d 30d
    span_days = 365 * 5     # long intervals can look back far (1d => ~27y ceiling)
    settle = "usdt"            # ignored for spot
    end_time = None            # None means "now"; set an int Unix second to pin it
    sample = 5                 # head/tail candles to print

    candles = get_candlesticks(
        market_type=market_type,
        trading_pair=trading_pair,
        interval=interval,
        span_days=span_days,
        settle=settle,
        end_time=end_time,
    )

    pair = str(trading_pair).strip().replace("-", "_").upper()
    shown_settle = settle if str(market_type).strip().lower() == "futures" else None
    _print_summary(candles, str(market_type).strip().lower(), pair,
                   shown_settle, interval, span_days, sample)
    return candles


if __name__ == "__main__":
    import os
    import sys
    # Allow running from a source checkout without installing the package:
    #   python tools/market_candlesticks.py
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
    main()
