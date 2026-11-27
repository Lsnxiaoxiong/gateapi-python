import sqlite3
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from gate_api import FuturesOrder
from gate_api.exceptions import ApiException, GateApiException
from urllib3.exceptions import ReadTimeoutError

from example import btc_sma_4h_long as bot


BAR = 50_000 * bot.BAR_SECONDS
SERVER_MS = (BAR + bot.BAR_SECONDS + bot.CANDLE_DELAY_SECONDS) * 1000
LONG = bot.Signal(BAR, Decimal("101"), Decimal("100"), SERVER_MS)
FLAT = bot.Signal(BAR, Decimal("100"), Decimal("100"), SERVER_MS)


def exchange():
    api = Mock()
    api.get_position.return_value = SimpleNamespace(
        user=123, mode="single", contract=bot.CONTRACT, size="0",
    )
    api.list_futures_orders.return_value = []
    api.list_price_triggered_orders.return_value = []
    return api


def make_strategy(api, db):
    return bot.Strategy(api, Mock(), bot.REST_URL, Decimal("2"), bot.State(db))


def finished(order, order_id=34, left="0"):
    return FuturesOrder(
        id=order_id, status="finished", contract=bot.CONTRACT, text=order.text,
        size=order.size, left=left, price=order.price, tif=order.tif,
    )


def test_load_credentials_reads_only_the_configured_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_API_KEY", "env-key")
    monkeypatch.setenv("GATE_API_SECRET", "env-secret")
    path = tmp_path / "account.json"
    path.write_text('{"GATE_API_KEY": " file-key ", "GATE_API_SECRET": "file-secret"}',
                    encoding="utf-8-sig")
    assert bot.load_credentials(path) == ("file-key", "file-secret")

    path.write_text('{"GATE_API_KEY": "file-key", "GATE_API_SECRET": ""}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="GATE_API_SECRET"):
        bot.load_credentials(path)


def test_live_mode_requires_credentials_file_before_creating_api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "LIVE_TRADING", True)
    monkeypatch.setattr(bot, "CREDENTIALS_PATH", tmp_path / "missing.json")
    monkeypatch.setenv("GATE_API_KEY", "env-key")
    monkeypatch.setenv("GATE_API_SECRET", "env-secret")
    client = Mock()
    monkeypatch.setattr(bot, "ApiClient", client)

    with pytest.raises(RuntimeError, match="missing.json"):
        bot.main()
    client.assert_not_called()


def test_signal_uses_contiguous_closed_candles_and_ignores_current_bar():
    candles = [
        SimpleNamespace(t=BAR - offset * bot.BAR_SECONDS, c="120" if offset < 20 else "100")
        for offset in reversed(range(100))
    ]
    candles.append(SimpleNamespace(t=BAR + bot.BAR_SECONDS, c="1"))
    signal = bot.calculate_signal(list(reversed(candles)), SERVER_MS)
    assert (signal.bar, signal.fast, signal.slow, signal.long) == (BAR, 120, 104, True)

    with pytest.raises(bot.WaitForData, match="连续"):
        bot.calculate_signal(candles[1:], SERVER_MS)
    with pytest.raises(bot.WaitForData, match="最新"):
        bot.calculate_signal(candles, SERVER_MS - 1000)


def test_crash_after_exchange_fill_recovers_without_duplicate_buy(tmp_path):
    path = tmp_path / "strategy.db"
    api = exchange()
    api.create_futures_order.side_effect = ReadTimeoutError(None, None, "lost response")
    with closing(sqlite3.connect(path)) as db:
        strategy = make_strategy(api, db)
        with pytest.raises(ReadTimeoutError):
            strategy.trade(LONG)
        owned, pending = strategy.state.read()
        assert owned == 0
        assert pending.size == 2

    api.get_position.return_value.size = "2"
    request = api.create_futures_order.call_args.args[1]
    api.get_futures_order.return_value = finished(request)
    with closing(sqlite3.connect(path)) as db:
        strategy = make_strategy(api, db)
        strategy.trade(LONG)
        strategy.trade(LONG)
        assert strategy.state.read() == (2, None)
        assert api.create_futures_order.call_count == 1
        assert api.get_futures_order.call_args.args[1] == pending.text


def test_partial_entry_and_partial_exit_use_actual_owned_size():
    api = exchange()
    with closing(sqlite3.connect(":memory:")) as db:
        strategy = make_strategy(api, db)
        api.create_futures_order.side_effect = lambda _, order, **kw: finished(order, left="0.5")
        strategy.trade(LONG)
        assert strategy.state.read() == (Decimal("1.5"), None)
        api.get_position.return_value.size = "1.5"
        strategy.trade(LONG)
        assert api.create_futures_order.call_count == 1

        strategy.trade(FLAT)
        exit_order = api.create_futures_order.call_args.args[1]
        assert (exit_order.size, exit_order.reduce_only, exit_order.price, exit_order.tif) == (
            "-1.5", True, "0", "ioc",
        )
        assert strategy.state.read() == (Decimal("0.5"), None)
        api.get_position.return_value.size = "0.5"
        api.create_futures_order.side_effect = lambda _, order, **kw: finished(order)
        strategy.trade(FLAT)
        assert api.create_futures_order.call_args.args[1].size == "-0.5"
        assert strategy.state.read() == (0, None)


def test_expired_intent_recovers_when_no_order_and_exchange_position_unchanged():
    api = exchange()
    api.get_futures_order.side_effect = GateApiException(
        "ORDER_NOT_FOUND", exp=ApiException(status=404)
    )
    api.get_orders_with_time_range.return_value = []
    with closing(sqlite3.connect(":memory:")) as db:
        strategy = make_strategy(api, db)
        strategy.state.bind(bot.REST_URL, 123)
        expires_ms = SERVER_MS - bot.RECOVERY_GRACE_MS
        pending = bot.PendingOrder("t-crashed-before-post", Decimal("2"), expires_ms - 30_000, expires_ms)
        strategy.state.reserve(pending)
        api.get_position.return_value.size = "1"
        with pytest.raises(bot.WaitForData, match="仓位已变化"):
            strategy.trade(LONG)
        api.get_position.return_value.size = "0"
        strategy.trade(LONG)
        assert strategy.state.read() == (0, None)
        api.create_futures_order.assert_not_called()


def test_existing_btc_position_or_orders_block_live_trade():
    api = exchange()
    with closing(sqlite3.connect(":memory:")) as db:
        strategy = make_strategy(api, db)
        api.get_position.return_value.size = "2"
        with pytest.raises(bot.WaitForData, match="实际持仓"):
            strategy.trade(FLAT)
        api.get_position.return_value.size = "0"
        api.list_price_triggered_orders.return_value = [object()]
        with pytest.raises(bot.WaitForData, match="其他普通挂单或触发单"):
            strategy.trade(LONG)
        api.create_futures_order.assert_not_called()
