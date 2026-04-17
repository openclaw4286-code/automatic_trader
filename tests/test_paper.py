"""DryBroker paper engine unit tests."""
from __future__ import annotations

import asyncio

from exchange.paper import DryBroker


class _FakeEx:
    def __init__(self, contract_size: float = 1.0) -> None:
        self._price = 100.0
        self._markets = {
            "BTC/USDT:USDT": {"contractSize": contract_size},
            "ETH/USDT:USDT": {"contractSize": contract_size},
        }

    def set_price(self, p: float) -> None:
        self._price = p

    async def _fetch_ticker_real(self, symbol: str):
        return {"last": self._price}


def test_entry_fills_immediately_and_creates_position():
    async def _run():
        fx = _FakeEx()
        b = DryBroker(fx)                                          # type: ignore[arg-type]
        o = await b.create_order("BTC/USDT:USDT", "buy", 10, 100.0, None)
        assert o["status"] == "closed"
        pos = await b.positions()
        assert len(pos) == 1
        assert pos[0]["contracts"] == 10
        assert pos[0]["entryPrice"] == 100.0
        assert pos[0]["side"] == "long"

    asyncio.run(_run())


def test_sl_order_tracked_then_triggered_when_price_drops():
    async def _run():
        fx = _FakeEx()
        b = DryBroker(fx)                                          # type: ignore[arg-type]
        await b.create_order("BTC/USDT:USDT", "buy", 10, 100.0, None)
        # SL: reduce-only stop-market at 99
        await b.create_order("BTC/USDT:USDT", "sell", 10, None,
                             {"reduceOnly": True, "stopPrice": 99, "triggerPrice": 99, "type": "market"})
        # TP: reduce-only limit at 103
        await b.create_order("BTC/USDT:USDT", "sell", 10, 103, {"reduceOnly": True})

        assert len(await b.open_orders()) == 2
        # price drops — SL fires
        fx.set_price(98.5)
        await b.tick()
        assert await b.positions() == []
        assert await b.open_orders() == []                          # both wiped
        # realized pnl = (99 - 100) * 10 = -10
        assert abs(b.realized_pnl - -10.0) < 1e-9

    asyncio.run(_run())


def test_tp_hit_closes_position_and_records_pnl():
    async def _run():
        fx = _FakeEx()
        b = DryBroker(fx)                                          # type: ignore[arg-type]
        await b.create_order("BTC/USDT:USDT", "buy", 10, 100.0, None)
        await b.create_order("BTC/USDT:USDT", "sell", 10, None,
                             {"reduceOnly": True, "stopPrice": 99, "triggerPrice": 99, "type": "market"})
        await b.create_order("BTC/USDT:USDT", "sell", 10, 103, {"reduceOnly": True})

        fx.set_price(103.2)
        await b.tick()
        assert await b.positions() == []
        assert abs(b.realized_pnl - 30.0) < 1e-9                   # (103-100)*10

    asyncio.run(_run())


def test_partial_tp_then_sl_sequence():
    async def _run():
        fx = _FakeEx()
        b = DryBroker(fx)                                          # type: ignore[arg-type]
        await b.create_order("BTC/USDT:USDT", "buy", 10, 100.0, None)
        # partial TP at 101 for qty 5
        await b.create_order("BTC/USDT:USDT", "sell", 5, 101, {"reduceOnly": True})
        # SL at 99.5 for the remaining 5
        await b.create_order("BTC/USDT:USDT", "sell", 5, None,
                             {"reduceOnly": True, "stopPrice": 99.5, "triggerPrice": 99.5, "type": "market"})

        fx.set_price(101.0)
        await b.tick()
        pos = await b.positions()
        assert pos and pos[0]["contracts"] == 5                    # partial filled

        fx.set_price(99.4)
        await b.tick()
        assert await b.positions() == []                           # SL hit
        # pnl = (101-100)*5 + (99.5-100)*5 = 5 - 2.5 = 2.5
        assert abs(b.realized_pnl - 2.5) < 1e-9

    asyncio.run(_run())


def test_cancel_order_removes_it_from_book():
    async def _run():
        fx = _FakeEx()
        b = DryBroker(fx)                                          # type: ignore[arg-type]
        await b.create_order("BTC/USDT:USDT", "buy", 10, 100.0, None)
        o = await b.create_order("BTC/USDT:USDT", "sell", 10, 103, {"reduceOnly": True})
        assert len(await b.open_orders()) == 1
        await b.cancel_order(o["id"], "BTC/USDT:USDT")
        assert await b.open_orders() == []

    asyncio.run(_run())


def test_emergency_market_reduce_only_closes_position():
    async def _run():
        fx = _FakeEx()
        b = DryBroker(fx)                                          # type: ignore[arg-type]
        await b.create_order("BTC/USDT:USDT", "buy", 10, 100.0, None)
        fx.set_price(100.0)
        await b.create_order("BTC/USDT:USDT", "sell", 10, None,
                             {"reduceOnly": True, "type": "market"})
        assert await b.positions() == []

    asyncio.run(_run())


def test_short_position_sl_and_tp():
    async def _run():
        fx = _FakeEx()
        b = DryBroker(fx)                                          # type: ignore[arg-type]
        await b.create_order("ETH/USDT:USDT", "sell", 10, 100.0, None)
        # SL above entry for shorts
        await b.create_order("ETH/USDT:USDT", "buy", 10, None,
                             {"reduceOnly": True, "stopPrice": 101, "triggerPrice": 101, "type": "market"})

        fx.set_price(101.5)
        await b.tick()
        assert await b.positions() == []
        # pnl short = (100 - 101) * 10 = -10
        assert abs(b.realized_pnl - -10.0) < 1e-9

    asyncio.run(_run())


def test_pnl_uses_contract_size_for_base_assets():
    """Gate BTC perp: contractSize=0.0001, so 100 contracts = 0.01 BTC."""
    async def _run():
        fx = _FakeEx(contract_size=0.0001)
        b = DryBroker(fx)                                          # type: ignore[arg-type]
        await b.create_order("BTC/USDT:USDT", "buy", 100, 75000.0, None)
        await b.create_order("BTC/USDT:USDT", "sell", 100, 75100.0, {"reduceOnly": True})

        fx.set_price(75100.0)
        await b.tick()
        assert await b.positions() == []
        # expected pnl: (75100 - 75000) * (100 * 0.0001) = 100 * 0.01 = 1.0 USDT
        assert abs(b.realized_pnl - 1.0) < 1e-9

    asyncio.run(_run())


if __name__ == "__main__":
    test_entry_fills_immediately_and_creates_position()
    test_sl_order_tracked_then_triggered_when_price_drops()
    test_tp_hit_closes_position_and_records_pnl()
    test_partial_tp_then_sl_sequence()
    test_cancel_order_removes_it_from_book()
    test_emergency_market_reduce_only_closes_position()
    test_short_position_sl_and_tp()
    test_pnl_uses_contract_size_for_base_assets()
    print("all paper tests passed")
