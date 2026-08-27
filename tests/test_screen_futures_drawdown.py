from example.screen_futures_drawdown import screen_contracts


class Item(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeFuturesApi(object):
    def list_futures_contracts(self, settle):
        return [
            Item(name="AAA_USDT", in_delisting=False),
            Item(name="BBB_USDT", in_delisting=False),
            Item(name="CCC_USDT", in_delisting=True),
        ]

    def list_futures_candlesticks(self, settle, contract, **kwargs):
        candles = {
            "AAA_USDT": [Item(t=100, h="10", c="2"), Item(t=200, h="8", c="2")],
            "BBB_USDT": [Item(t=100, h="10", c="4"), Item(t=200, h="5", c="4")],
        }
        return candles[contract]


class FakeMarketCaps(object):
    def get_many(self, symbols):
        assert set(symbols) == {"aaa", "bbb"}
        return {"aaa": 9000000, "bbb": 90000000}


def test_screen_contracts_filters_orders_and_limits_results():
    results = screen_contracts(FakeFuturesApi(), FakeMarketCaps(), now=1000, limit=1)

    assert len(results) == 1
    assert results[0]["contract"] == "AAA_USDT"
    assert results[0]["drawdown_pct"] == 80.0


def test_screen_contracts_excludes_market_cap_boundaries_and_70_percent():
    class CapsAtBounds(FakeMarketCaps):
        def get_many(self, symbols):
            return {"aaa": 8000000, "bbb": 100000000}

    assert screen_contracts(FakeFuturesApi(), CapsAtBounds(), now=1000) == []
