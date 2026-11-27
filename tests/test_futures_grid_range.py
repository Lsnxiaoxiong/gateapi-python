from contextlib import ExitStack, closing, contextmanager
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sqlite3

import pytest

from example import auto_grid_contract_futures_generic as long_grid
from example import auto_grid_contract_futures_short_generic as short_grid
from example import auto_grid_futures_generic as core
from example import grid_contract_BTC, grid_contract_short_BTC


def new_range(lower="100", upper="110", margin="3", revision=1, side="long"):
    return core.FuturesGridRange.from_config(
        f"futures:usdt:{side}", "BTC_USDT", "test", lower, upper, margin, revision,
    )


@pytest.mark.parametrize("config", [
    {"lower": "-1"}, {"upper": "99"}, {"upper": "100"}, {"margin": "0"},
    {"lower": "sNaN"}, {"upper": "Infinity"}, {"margin": "abc"},
    {"revision": "1.5"}, {"revision": 0}, {"revision": 2**63},
])
def test_invalid_range_config_is_rejected(config):
    with pytest.raises(ValueError):
        new_range(**config)


def test_all_disabled_config_and_btc_entry_defaults():
    assert new_range(lower=-1, upper="-1.0", margin=D("-1")) is None
    for entry, start in ((grid_contract_BTC, "run_grid"), (grid_contract_short_BTC, "run_short_grid")):
        with patch.object(entry, start) as runner:
            entry.run_BTC()
        options = runner.call_args.kwargs
        assert all(D(options[name]) == -1 for name in (
            "range_lower_price", "range_upper_price", "range_margin_per_order",
        ))
        assert options["range_revision"] == 1


@pytest.mark.parametrize("outside", [D("99.9"), D("110.1")])
def test_old_db_migrates_and_range_lifecycle_survives_reopen(tmp_path, outside):
    path = tmp_path / "grid.db"
    with closing(core.GridStore(path)) as old_store:
        for i, price in enumerate((D("101"), D("102")), start=1):
            old_store.insert_buy("BTC_USDT", "old", SimpleNamespace(id=i, text=f"t-test-{i}"),
                                 core.PlannedOrder(price, D("0.3")), price + 1)
        old_store.execute("UPDATE grid_cycles SET buy_status=?, tp_order_id='3', tp_status=? WHERE id=2",
                          (core.BUY_FILLED, core.TP_OPEN))
        order_snapshot = [dict(row) for row in old_store.rows("SELECT * FROM grid_cycles ORDER BY id")]
    with closing(sqlite3.connect(path)) as old_db, old_db:
        old_db.execute("DROP TABLE futures_range_state")
        old_db.execute("INSERT INTO grid_layouts VALUES ('BTC_USDT','old','{}',NULL)")
    db = core.GridStore(path)
    try:
        assert db.rows("SELECT * FROM grid_layouts")[0]["strategy_code"] == "old"
        original = new_range()
        original.restore(db)
        original.observe(outside)
        assert original.margin_for(D("105"), D("1")) == D("1")
        assert not db.rows("SELECT * FROM futures_range_state")
        original.observe(D("100"))
        assert original.margin_for(D("100"), D("1")) == D("3")
        assert original.margin_for(D("110"), D("1")) == D("3")
        assert original.margin_for(outside, D("1")) == D("1")
    finally:
        db.close()

    db = core.GridStore(path)
    try:
        resumed = new_range(lower="100.0", margin="9")
        resumed.restore(db)
        assert resumed.phase == "active"
        assert resumed.margin_for(D("105"), D("1")) == D("9")
        resumed.observe(outside)
        assert resumed.effective_bounds == (D("-1"), D("-1"))

        for fresh in (new_range(revision=2), new_range(side="short"), new_range(upper="120")):
            fresh.restore(db)
            assert fresh.phase == "waiting"
            fresh.observe(D("105"))
            assert fresh.margin_for(D("105"), D("1")) == D("3")

        expired = new_range()
        expired.restore(db)
        expired.observe(D("105"))
        assert expired.phase == "expired"
        assert expired.margin_for(D("105"), D("2")) == D("2")
        assert [dict(row) for row in db.rows("SELECT * FROM grid_cycles ORDER BY id")] == order_snapshot
    finally:
        db.close()


def contract_spec():
    spec = core.PairSpec("BTC", "USDT", D("0.1"), D("0.1"), D("0.1"),
                         D("0"), D("100000"), D("Infinity"))
    return long_grid.FuturesContractSpec(spec, D("0.01"), D("1"), D("100"))


@contextmanager
def mock_runner(module, prices):
    short = module is short_grid
    candidates = [D("105.1"), D("110"), D("110.1")] if short else [D("99.9"), D("100"), D("104.9")]
    workers = Mock()
    workers.Thread.return_value.is_alive.return_value = False
    workers.Event.return_value.is_set.return_value = False
    with ExitStack() as stack:
        for owner, name, value in (
            (module, "ApiClient", Mock()), (module, "FuturesApi", Mock()),
            (module, "threading", workers), (module, "get_contract_spec", Mock(return_value=contract_spec())),
            (module, "configure_futures_account", Mock(return_value="single")),
            (core, "load_env_file", Mock()), (core, "required_env", Mock(return_value="fake")),
            (core, "get_last_price", Mock(side_effect=prices)), (core, "prepare_grid_resume", Mock()),
            (short_grid if short else core, "desired_short_prices" if short else "desired_buy_prices",
             Mock(return_value=candidates)),
        ):
            stack.enter_context(patch.object(owner, name, value))
        placement = stack.enter_context(patch.object(
            short_grid if short else core, "place_missing_shorts" if short else "place_missing_buys",
            return_value=True,
        ))
        yield placement, candidates


def run_grid(module, path, cycles, live=True, revision=1):
    runner = module.run_short_grid if module is short_grid else module.run_grid
    runner("BTC_USDT", "105", "1", leverage="10", grid_tag="test", database_path=path,
           live_trading=live, max_cycles=cycles, range_lower_price="100", range_upper_price="110",
           range_margin_per_order="3", range_revision=revision)


def assert_orders(placement, prices, margins):
    for call, per_order in zip(placement.call_args_list, margins, strict=True):
        planned = call.args[4]
        assert [order.price for order in planned] == prices
        for order, margin in zip(planned, per_order, strict=True):
            assert order.size == long_grid.calculate_contract_size(D(margin), D("10"), order.price, contract_spec())


@pytest.mark.parametrize("module,inside_margins", [(long_grid, (1, 3, 3)), (short_grid, (3, 3, 1))])
def test_live_entry_selects_each_price_and_resume_does_not_rearm(tmp_path, module, inside_margins):
    path = tmp_path / "grid.db"
    regular = (1, 1, 1)
    with mock_runner(module, list(map(D, ["99", "99", "99", "105", "111", "105"]))) as (place, prices):
        run_grid(module, path, cycles=4)
        assert_orders(place, prices, [regular, inside_margins, regular, regular])
    with mock_runner(module, [D("105")] * 3) as (place, prices):
        run_grid(module, path, cycles=1)
        assert_orders(place, prices, [regular])
    with mock_runner(module, [D("105")] * 3) as (place, prices):
        run_grid(module, path, cycles=1, revision=2)
        assert_orders(place, prices, [inside_margins])


@pytest.mark.parametrize("module", [long_grid, short_grid])
def test_explicit_preview_needs_no_database_or_trade_calls(tmp_path, capsys, module):
    path = tmp_path / "grid.db"
    with mock_runner(module, [D("105")]) as (place, _):
        run_grid(module, path, cycles=1, live=False)
        place.assert_not_called()
        module.configure_futures_account.assert_not_called()
    output = capsys.readouterr().out
    assert "按新配置预览" in output
    assert "每格保证金=3 USDT" in output
    assert "每格保证金=1 USDT" in output
    assert not path.exists()


@pytest.mark.parametrize("module", [long_grid, short_grid])
def test_exit_during_funding_cooldown_uses_regular_margin_on_next_submission(tmp_path, module):
    path = tmp_path / "grid.db"
    interval = core.BALANCE_RETRY_INTERVAL
    clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, interval - 1, interval]))
    inside = (3, 3, 1) if module is short_grid else (1, 3, 3)
    with mock_runner(module, list(map(D, ["105", "105", "105", "111", "105"]))) as (place, prices), \
            patch.object(module, "time", clock):
        place.side_effect = [False, True]
        run_grid(module, path, cycles=3)
        assert_orders(place, prices, [inside, (1, 1, 1)])


def test_failed_write_remembers_exit_when_next_price_returns(tmp_path):
    db = core.GridStore(tmp_path / "grid.db")
    try:
        state = new_range()
        state.restore(db)
        state.observe(D("105"))
        with patch.object(db, "save_futures_range_phase", side_effect=sqlite3.OperationalError("locked")):
            with pytest.raises(core.FuturesRangeWriteError):
                state.observe(D("111"))
        assert db.futures_range_phase(state.key) == "active"
        state.observe(D("105"))
        assert db.futures_range_phase(state.key) == "expired"
        assert state.margin_for(D("105"), D("1")) == D("1")
    finally:
        db.close()
