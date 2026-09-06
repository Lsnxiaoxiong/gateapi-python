# coding: utf-8
"""Rank Gate perpetual contracts by yearly low-to-current gain.

This module provides :func:`rank_futures_by_yearly_low_gain`, a non-CLI entry
point that scans all active (trading, not delisting) perpetual contracts under
a given settlement currency, fetches one year of ``1h`` candlesticks for each
via :mod:`tools.market_candlesticks`, and ranks them by the percentage gain
from the year's lowest ``low`` to the current price:

.. code-block:: text

    gain_pct = (current_price - year_low) / year_low * 100

The current price defaults to the contract's ``last_price`` and falls back to
the most recent candlestick ``close`` when ``last_price`` is missing or
invalid. Gain math uses :class:`decimal.Decimal`; the source of the current
price is reported per result as ``current_price_source``.

A single contract failing never aborts the whole scan: failures are collected
into the ``errors`` list with the contract name and a reason, while the
``stats`` summary lets the caller judge completeness. If the contract-list
request itself fails, the function raises rather than returning a misleading
empty ranking.

Concurrency is bounded by an injectable ``max_workers`` (default 4) so a full
market scan does not fan out unboundedly. Results are always sorted in the
caller thread, so output order is stable regardless of task completion order.

``gate_api`` is imported lazily, so importing this module has no side effects
and does not require the SDK to be installed.

Example::

    from tools.rank_futures_yearly_gain import rank_futures_by_yearly_low_gain

    report = rank_futures_by_yearly_low_gain(settle="usdt")
    for item in report["results"]:
        print(item["contract"], item["gain_pct"])
"""
from __future__ import absolute_import

from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation

from tools.market_candlesticks import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUEST_TIMEOUT,
    YEAR_SECONDS,
    _call_with_retry,
    _decimal_to_text,
    _normalize_settle,
    get_one_year_hourly_candlesticks,
)

#: Marker placed in ``current_price_source`` when ``last_price`` was used.
SOURCE_CONTRACT_LAST_PRICE = "contract_last_price"
#: Marker for the fallback to the most recent candle close.
SOURCE_LATEST_CLOSE = "latest_candle_close"

#: Default bounded concurrency for a full market scan.
DEFAULT_MAX_WORKERS = 4
CONTRACT_PAGE_SIZE = 100

_DEFAULT_MARKET_CAP_PROVIDER = None


def _get_default_market_cap_provider():
    """Return one process-wide provider so downloaded market-cap pages are cached."""
    global _DEFAULT_MARKET_CAP_PROVIDER
    if _DEFAULT_MARKET_CAP_PROVIDER is None:
        from tools.market_caps import DefaultMarketCaps
        _DEFAULT_MARKET_CAP_PROVIDER = DefaultMarketCaps()
    return _DEFAULT_MARKET_CAP_PROVIDER


def _is_active_contract(contract):
    """Return True only for tradable, non-delisting contracts."""
    if contract is None:
        return False
    if getattr(contract, "in_delisting", False):
        return False
    status = getattr(contract, "status", None)
    # ``status`` is typically ``"trading"``; accept any explicit truthy trading
    # status but reject delisted/empty. Missing status is treated conservatively
    # as inactive only if it is explicitly falsy.
    if status is None:
        return True
    if not isinstance(status, str):
        status = str(status)
    status = status.strip().lower()
    if not status:
        return True
    return status == "trading"


def _contract_name(contract):
    """Return the contract name, or None if it cannot be read."""
    name = getattr(contract, "name", None)
    if not name:
        return None
    return str(name).strip() or None


def _resolve_current_price(contract, candles):
    """Pick the current price and its source for a contract.

    Prefers ``Contract.last_price``; falls back to the most recent candle
    ``close`` when ``last_price`` is missing, non-numeric or not positive.
    Returns ``(price_text, source)`` or ``(None, None)`` when no usable price.
    """
    last_price = getattr(contract, "last_price", None)
    price = _parse_positive_decimal(last_price)
    if price is not None:
        return _decimal_to_text(price), SOURCE_CONTRACT_LAST_PRICE
    # Fallback: newest candle close (candles are ascending by timestamp).
    for candle in reversed(candles):
        close = _parse_positive_decimal(candle.get("close"))
        if close is not None:
            return _decimal_to_text(close), SOURCE_LATEST_CLOSE
    return None, None


def _parse_positive_decimal(value):
    """Return a Decimal for a positive finite value, else None."""
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not dec.is_finite() or dec <= 0:
        return None
    return dec


def _parse_optional_nonnegative_decimal(value, name):
    """Return an optional finite non-negative Decimal or raise ValueError."""
    if value is None or value == "":
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("%s must be a non-negative number" % name)
    if not dec.is_finite() or dec < 0:
        raise ValueError("%s must be a non-negative number" % name)
    return dec


def _validate_range(minimum, maximum, label):
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minimum %s cannot exceed maximum %s" % (label, label))


def _base_currency(contract):
    """Return the CoinGecko-style base ticker for a Gate contract."""
    base = (_contract_name(contract) or "").rsplit("_", 1)[0].lower()
    # Gate uses multiplier tickers such as 1000PEPE; CoinGecko uses PEPE.
    for prefix in ("1000000", "10000", "1000"):
        if base.startswith(prefix) and len(base) > len(prefix):
            return base[len(prefix):]
    return base


def _minimum_order_value(contract, current_price=None):
    """Return minimum order notional in settlement currency, or None."""
    price = _parse_positive_decimal(
        current_price if current_price is not None else getattr(contract, "last_price", None))
    size = _parse_positive_decimal(getattr(contract, "order_size_min", None))
    multiplier = _parse_positive_decimal(getattr(contract, "quanto_multiplier", None))
    if price is None or size is None or multiplier is None:
        return None
    return size * multiplier * price


def _optional_decimal_text(value):
    return _decimal_to_text(value) if value is not None else None


def _in_optional_range(value, minimum, maximum):
    if value is None:
        return minimum is None and maximum is None
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def _compute_year_low(candles):
    """Return ``(year_low Decimal, year_low_time int)`` for the lowest ``low``.

    The first timestamp at which the minimum low occurs is recorded so the
    result is stable when a low repeats.
    """
    year_low = None
    year_low_time = None
    for candle in candles:
        low = _parse_positive_decimal(candle.get("low"))
        if low is None:
            continue
        if year_low is None or low < year_low:
            year_low = low
            year_low_time = candle["timestamp"]
    return year_low, year_low_time


def _build_result(contract, settle, candles):
    """Compute the gain result dict for one contract with valid candles."""
    year_low, year_low_time = _compute_year_low(candles)
    if year_low is None or year_low_time is None:
        return None
    current_text, source = _resolve_current_price(contract, candles)
    if current_text is None:
        return None
    current_price = _parse_positive_decimal(current_text)
    if current_price is None:
        return None
    gain_pct = (current_price - year_low) / year_low * Decimal(100)
    timestamps = [c["timestamp"] for c in candles]
    min_order_value = _minimum_order_value(contract, current_price)
    return {
        "contract": _contract_name(contract),
        "settle": settle,
        "year_low": _decimal_to_text(year_low),
        "year_low_time": year_low_time,
        "current_price": _decimal_to_text(current_price),
        "current_price_source": source,
        "gain_pct": _decimal_to_text(gain_pct),
        "data_start_time": min(timestamps) if timestamps else None,
        "data_end_time": max(timestamps) if timestamps else None,
        "candle_count": len(candles),
        "order_size_min": _optional_decimal_text(
            _parse_positive_decimal(getattr(contract, "order_size_min", None))),
        "quanto_multiplier": _optional_decimal_text(
            _parse_positive_decimal(getattr(contract, "quanto_multiplier", None))),
        "min_order_value": _optional_decimal_text(min_order_value),
    }


def _default_futures_api(request_timeout):
    """Create a default FuturesApi backed by a public ApiClient (lazy import).

    The per-request timeout is enforced via the ``_request_timeout`` argument
    passed down to :func:`get_one_year_hourly_candlesticks`; the Gate SDK
    exposes no client-level timeout attribute, so nothing is set here.
    """
    from gate_api import ApiClient, Configuration, FuturesApi  # noqa: delayed import
    config = Configuration()
    config.retries = False
    client = ApiClient(config)
    return FuturesApi(client)


def _describe_error(exc):
    """Return a short human-readable reason string for an exception."""
    label = getattr(exc, "label", None)
    message = getattr(exc, "message", None)
    if label or message:
        parts = []
        if label:
            parts.append("label=%s" % label)
        if message:
            parts.append("message=%s" % message)
        return "%s: %s" % (type(exc).__name__, ", ".join(parts))
    return "%s: %s" % (type(exc).__name__, exc)


def _notify_stage(callback, phase, message, completed=0, total=0, **details):
    """Emit a structured, optional progress event for long setup stages."""
    if callback is None:
        return
    event = {
        "phase": phase,
        "message": message,
        "completed": int(completed or 0),
        "total": int(total or 0),
    }
    event.update(details)
    callback(event)


def _list_futures_contracts(api, settle, limit, request_timeout, max_retries,
                            stage_callback=None):
    """Fetch contracts in bounded pages for real Gate SDK clients.

    Gate currently accepts at most 100 contracts per request even though this
    generated SDK validates up to 1000. Injected test/custom APIs keep their
    original one-call behaviour so dependency injection remains lightweight.
    """
    is_gate_sdk = callable(getattr(getattr(api, "api_client", None), "call_api", None))
    common = {"_max_retries": max_retries}
    if request_timeout is not None:
        common["_request_timeout"] = request_timeout
    if not is_gate_sdk:
        page = list(_call_with_retry(api.list_futures_contracts, settle, **common) or [])
        _notify_stage(
            stage_callback, "contracts", "已获取 %d 个 Gate 合约" % len(page),
            len(page), int(limit or len(page)), contracts_fetched=len(page), page=1,
        )
        return page

    target = int(limit) if limit is not None and int(limit) > 0 else None
    contracts = []
    seen = set()
    offset = 0
    while target is None or len(contracts) < target:
        remaining = target - len(contracts) if target is not None else CONTRACT_PAGE_SIZE
        page_size = min(CONTRACT_PAGE_SIZE, remaining)
        kwargs = dict(common)
        kwargs.update({"limit": page_size, "offset": offset})
        page = list(_call_with_retry(
            api.list_futures_contracts, settle, **kwargs
        ) or [])
        if not page:
            break
        added = 0
        for contract in page:
            name = _contract_name(contract)
            identity = name or id(contract)
            if identity in seen:
                continue
            seen.add(identity)
            contracts.append(contract)
            added += 1
            if target is not None and len(contracts) >= target:
                break
        _notify_stage(
            stage_callback, "contracts",
            "Gate 合约列表：已获取 %d 个（第 %d 页）" %
            (len(contracts), (offset // CONTRACT_PAGE_SIZE) + 1),
            len(contracts), target or len(contracts),
            contracts_fetched=len(contracts),
            page=(offset // CONTRACT_PAGE_SIZE) + 1,
            page_size=page_size,
        )
        if len(page) < page_size or added == 0:
            break
        offset += len(page)
    return contracts


def _rank_key(item):
    """Sort key: gain_pct descending, then contract name ascending."""
    gain = _parse_positive_decimal(item.get("gain_pct"))
    # ``Decimal`` sorts ascending; negate for descending. Names sort ascending.
    return (-(gain if gain is not None else Decimal(0)), item.get("contract") or "")


def _scan_one_contract(contract, settle, end_time, futures_api, request_timeout,
                       max_retries, market_cap_usd=None, stage_callback=None):
    """Fetch candles and build the result for one contract.

    Returns a ``(contract_name, result_dict_or_None, error_dict_or_None)``
    tuple so the caller can route success/skip/failure uniformly.
    """
    name = _contract_name(contract)
    if not name:
        return None, None, {"contract": None, "reason": "missing contract name"}
    if stage_callback is not None:
        stage_callback({
            "phase": "scanning",
            "message": "正在下载 %s 的近一年 1h K 线" % name,
            "current_contract": name,
        })
    try:
        candles = get_one_year_hourly_candlesticks(
            market_type="futures",
            trading_pair=name,
            settle=settle,
            end_time=end_time,
            futures_api=futures_api,
            request_timeout=request_timeout,
            max_retries=max_retries,
        )
    except Exception as exc:  # noqa: BLE001 - isolated per-contract failure
        return name, None, {"contract": name, "reason": _describe_error(exc)}
    if not candles:
        return name, None, {"contract": name, "reason": "no_candles"}
    try:
        result = _build_result(contract, settle, candles)
    except Exception as exc:  # noqa: BLE001 - isolate compute failures too
        return name, None, {"contract": name, "reason": _describe_error(exc)}
    if result is None:
        return name, None, {"contract": name, "reason": "no_valid_price"}
    result["market_cap_usd"] = (_decimal_to_text(market_cap_usd)
                                if market_cap_usd is not None else None)
    result["market_cap_musd"] = (_decimal_to_text(market_cap_usd / Decimal(1000000))
                                 if market_cap_usd is not None else None)
    return name, result, None


def rank_futures_by_yearly_low_gain(settle="usdt", end_time=None,
                                    futures_api=None, max_workers=DEFAULT_MAX_WORKERS,
                                    request_timeout=DEFAULT_REQUEST_TIMEOUT,
                                    max_retries=DEFAULT_MAX_RETRIES,
                                    progress_callback=None, limit=None,
                                    min_market_cap_musd=None, max_market_cap_musd=None,
                                    min_order_value=None, max_order_value=None,
                                    market_cap_provider=None, result_callback=None,
                                    stage_callback=None):
    # noqa: E501 - long signature kept for explicit dependency injection
    """Rank active perpetual contracts by yearly low-to-current gain.

    Scans contracts returned by ``FuturesApi.list_futures_contracts(settle)``
    that are in trading status and not in delisting, fetches one year of ``1h``
    candlesticks for each, computes the gain from the year's lowest ``low`` to
    the current price, and returns the results sorted by ``gain_pct`` descending
    (ties broken by contract name ascending for stability).

    A single contract failing is isolated: it is recorded in ``errors`` and
    does not affect other contracts. If the contract-list request itself
    fails, the exception propagates so the caller is never misled by an empty
    ranking masquerading as a complete scan.

    When ``limit`` is given, only the most actively traded contracts (by
    ``trade_size``) are scanned, which keeps the request fast enough for an
    interactive preview.

    :param str settle: Settlement currency (e.g. ``"usdt"``). One call scans
        only one settlement market.
    :param int end_time: Optional end Unix timestamp shared by all contracts.
        Defaults to the same "now" for every contract.
    :param futures_api: Optional :class:`gate_api.FuturesApi` (or compatible)
        instance. When omitted a public client is created lazily and closed
        afterwards; injected clients are never closed.
    :param int max_workers: Bounded concurrency for per-contract candle fetches.
    :param request_timeout: Per-request timeout in seconds.
    :param int max_retries: Max retries for transient errors per candle page.
    :param progress_callback: Optional ``callback(completed, total, contract,
        error)`` invoked once per contract. Default None means no logging.
    :param int limit: Optional maximum number of contracts to scan. When set,
        the most actively traded contracts are picked first.
    :returns: ``{"results": [...], "errors": [...], "stats": {...}}`` where
        ``stats`` has ``total_contracts``, ``requested_contracts``,
        ``successful_contracts`` and ``failed_contracts``.
    :raises Exception: If ``list_futures_contracts`` itself fails.
    """
    min_cap_m = _parse_optional_nonnegative_decimal(
        min_market_cap_musd, "min_market_cap_musd")
    max_cap_m = _parse_optional_nonnegative_decimal(
        max_market_cap_musd, "max_market_cap_musd")
    min_order = _parse_optional_nonnegative_decimal(min_order_value, "min_order_value")
    max_order = _parse_optional_nonnegative_decimal(max_order_value, "max_order_value")
    _validate_range(min_cap_m, max_cap_m, "market cap")
    _validate_range(min_order, max_order, "minimum order value")

    settle = _normalize_settle(settle)
    owns_client = futures_api is None
    try:
        if futures_api is None:
            futures_api = _default_futures_api(request_timeout)
        _notify_stage(
            stage_callback, "contracts", "正在获取 Gate 合约列表", 0,
            int(limit or 0), contracts_fetched=0, page=0,
        )
        # The contract list is the first potentially slow operation. Paginate
        # it and apply the preview limit at the API boundary so the UI does not
        # download every Gate contract before it can show progress.
        contracts = _list_futures_contracts(
            futures_api, settle, limit, request_timeout, max_retries,
            stage_callback=stage_callback,
        )
    except Exception:
        # The contract-list request failing is fatal; do not fake completeness.
        if owns_client and futures_api is not None:
            _close_client(futures_api)
        raise

    active = [c for c in (contracts or []) if _is_active_contract(c) and _contract_name(c)]
    total_contracts = len(active)
    # ``limit`` is an interactive preview limit, so apply it before expensive
    # market-cap lookup. The previous order queried market caps for every Gate
    # contract even when the UI requested only 10/50/400 candidates, leaving
    # refresh stuck at 0% while external market-data pages were exhausted.
    if limit is not None and limit > 0:
        active = sorted(
            active,
            key=lambda c: _parse_positive_decimal(getattr(c, "trade_size", None)) or Decimal(0),
            reverse=True,
        )[:int(limit)]
    candidate_contracts = len(active)
    cap_filter_enabled = min_cap_m is not None or max_cap_m is not None
    order_filter_enabled = min_order is not None or max_order is not None
    caps_by_contract = {}
    if cap_filter_enabled:
        uses_default_market_caps = market_cap_provider is None
        if market_cap_provider is None:
            market_cap_provider = _get_default_market_cap_provider()
        symbols = {_base_currency(contract) for contract in active}
        _notify_stage(
            stage_callback, "market_caps",
            "正在查询 %d 个币种的市值" % len(symbols), 0, len(symbols),
            symbols_requested=len(symbols), symbols_matched=0,
        )
        if uses_default_market_caps:
            def _market_cap_progress(event):
                _notify_stage(
                    stage_callback, "market_caps", event.get("message") or "正在查询市值",
                    event.get("completed", 0), event.get("total", 0),
                    provider=event.get("provider"),
                    provider_step=event.get("step"),
                    symbols_requested=event.get("requested", len(symbols)),
                    symbols_matched=event.get("matched", 0),
                )

            caps = market_cap_provider.get_many(
                symbols,
                min_market_cap_usd=(min_cap_m * Decimal(1000000)
                                    if min_cap_m is not None else None),
                max_market_cap_usd=(max_cap_m * Decimal(1000000)
                                    if max_cap_m is not None else None),
                progress_callback=_market_cap_progress,
            )
        else:
            caps = market_cap_provider.get_many(symbols)
        caps_by_contract = {
            _contract_name(contract): _parse_positive_decimal(caps.get(_base_currency(contract)))
            for contract in active
        }
        _notify_stage(
            stage_callback, "market_caps",
            "市值查询完成：匹配 %d/%d 个币种" % (len(caps), len(symbols)),
            len(caps), len(symbols), symbols_requested=len(symbols),
            symbols_matched=len(caps),
        )

    _notify_stage(
        stage_callback, "filtering", "正在应用市值和最小下单价值筛选",
        0, candidate_contracts, candidate_contracts=candidate_contracts,
    )
    eligible = []
    for contract in active:
        cap_usd = caps_by_contract.get(_contract_name(contract))
        cap_musd = cap_usd / Decimal(1000000) if cap_usd is not None else None
        order_value = _minimum_order_value(contract)
        if cap_filter_enabled and not _in_optional_range(cap_musd, min_cap_m, max_cap_m):
            continue
        if order_filter_enabled and not _in_optional_range(order_value, min_order, max_order):
            continue
        eligible.append(contract)
    active = eligible
    eligible_contracts = len(active)
    total = len(active)
    _notify_stage(
        stage_callback, "filtering",
        "筛选完成：%d/%d 个合约进入 K 线扫描" %
        (eligible_contracts, candidate_contracts),
        candidate_contracts, candidate_contracts,
        candidate_contracts=candidate_contracts,
        eligible_contracts=eligible_contracts,
    )
    _notify_stage(
        stage_callback, "scanning",
        "开始下载近一年 1h K 线", 0, total,
        candidate_contracts=candidate_contracts,
        eligible_contracts=eligible_contracts,
    )
    results = []
    errors = []

    # Use a serial path for max_workers <= 1 to keep behaviour deterministic and
    # avoid thread overhead for tests / single-contract scans.
    if max_workers is None or max_workers <= 1:
        for contract in active:
            name, result, error = _scan_one_contract(
                contract, settle, end_time, futures_api, request_timeout, max_retries,
                caps_by_contract.get(_contract_name(contract)), stage_callback)
            if result is not None:
                results.append(result)
                if result_callback is not None:
                    result_callback(result)
            if error is not None:
                errors.append(error)
            if progress_callback is not None:
                progress_callback(len(results) + len(errors), total, name, error)
    else:
        # Shared injected/default client is used across bounded worker threads.
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(_scan_one_contract, contract, settle, end_time,
                            futures_api, request_timeout, max_retries,
                            caps_by_contract.get(_contract_name(contract)),
                            stage_callback): contract
                for contract in active
            }
            for future in as_completed(future_map):
                name, result, error = future.result()
                if result is not None:
                    results.append(result)
                    if result_callback is not None:
                        result_callback(result)
                if error is not None:
                    errors.append(error)
                completed += 1
                if progress_callback is not None:
                    progress_callback(completed, total, name, error)

    results.sort(key=_rank_key)
    if owns_client and futures_api is not None:
        _close_client(futures_api)

    return {
        "results": results,
        "errors": errors,
        "stats": {
            "total_contracts": total_contracts,
            "candidate_contracts": candidate_contracts,
            "eligible_contracts": eligible_contracts,
            "requested_contracts": len(active),
            "successful_contracts": len(results),
            "failed_contracts": len(errors),
            "filters": {
                "min_market_cap_musd": (_decimal_to_text(min_cap_m) if min_cap_m is not None else None),
                "max_market_cap_musd": (_decimal_to_text(max_cap_m) if max_cap_m is not None else None),
                "min_order_value": (_decimal_to_text(min_order) if min_order is not None else None),
                "max_order_value": (_decimal_to_text(max_order) if max_order is not None else None),
            },
        },
    }


def _close_client(futures_api):
    """Close the ApiClient owned by a default FuturesApi, if any."""
    client = getattr(futures_api, "api_client", None)
    if client is not None:
        close = getattr(client, "close", None)
        if callable(close):
            close()
