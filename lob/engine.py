"""Price-time priority limit order book (matching engine).

Conventions
-----------
* Prices are integer ticks, quantities are integer lots.
* Within a price level, orders trade in arrival order (FIFO).
* Trades execute at the resting (maker) order's price.
* Supported order types:
    - limit, GTC: any unfilled remainder rests in the book.
    - limit, IOC: any unfilled remainder is cancelled.
    - limit, post-only: rejected outright if it would cross the book.
    - market: sweeps the opposite side, remainder cancelled (IOC).
* Every public operation takes an optional ``ts`` (integer nanoseconds).
  Timestamps must be non-decreasing.

Market data
-----------
An optional ``listener`` callable receives tuples, flushed once at the end of
each public operation (trades first, then the level totals that changed):

    ("TRADE", ts, price, qty, aggressor_side, maker_oid, taker_oid)
    ("L2",    ts, side, price, total_qty_after)   # total 0 means level removed

The L2 stream is anonymised (no order ids). The backtester in ``mmbt`` consumes
this stream, the same way a participant would consume a public depth feed.
"""

from __future__ import annotations

from bisect import bisect_left, insort
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

BUY = 1
SELL = -1

Listener = Callable[[tuple], None]


@dataclass
class Order:
    oid: int
    side: int
    price: int
    qty: int
    owner: str
    ts: int


@dataclass(frozen=True)
class Trade:
    ts: int
    price: int
    qty: int
    aggressor: int      # side of the incoming (taker) order
    maker_oid: int
    taker_oid: int
    maker_owner: str


@dataclass(frozen=True)
class AddResult:
    oid: int
    trades: Tuple[Trade, ...]
    rested_qty: int
    unfilled_qty: int
    rejected: bool = False
    reason: str = ""

    @property
    def filled_qty(self) -> int:
        return sum(t.qty for t in self.trades)


def _check_side(side: int) -> None:
    if side not in (BUY, SELL):
        raise ValueError(f"side must be BUY (1) or SELL (-1), got {side!r}")


def _check_qty(qty: int) -> None:
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        raise ValueError(f"qty must be a positive int, got {qty!r}")


def _check_price(price: int) -> None:
    if not isinstance(price, int) or isinstance(price, bool):
        raise ValueError(f"price must be an int tick, got {price!r}")


class OrderBook:
    """Single-instrument central limit order book."""

    def __init__(self, listener: Optional[Listener] = None) -> None:
        self._listener = listener
        self._orders: Dict[int, Order] = {}
        # side -> price -> deque of resting oids (FIFO)
        self._levels: Dict[int, Dict[int, deque]] = {BUY: {}, SELL: {}}
        # side -> price -> total resting qty at that level
        self._totals: Dict[int, Dict[int, int]] = {BUY: {}, SELL: {}}
        # side -> sorted ascending list of active price levels
        self._px: Dict[int, List[int]] = {BUY: [], SELL: []}
        self._next_oid = 1
        self._ts = 0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def add_limit(
        self,
        side: int,
        price: int,
        qty: int,
        owner: str = "",
        ts: Optional[int] = None,
        tif: str = "GTC",
        post_only: bool = False,
    ) -> AddResult:
        _check_side(side)
        _check_price(price)
        _check_qty(qty)
        if tif not in ("GTC", "IOC"):
            raise ValueError(f"tif must be 'GTC' or 'IOC', got {tif!r}")
        ts = self._tick(ts)

        if post_only and self._crosses(side, price):
            return AddResult(-1, (), 0, qty, rejected=True, reason="post_only_would_cross")

        oid = self._new_oid()
        trades, touched, remaining = self._match(oid, side, price, qty, ts)
        rested = 0
        if remaining > 0 and tif == "GTC":
            self._rest(oid, side, price, remaining, owner, ts)
            touched.add((side, price))
            rested = remaining
            remaining = 0
        self._flush(ts, trades, touched)
        return AddResult(oid, tuple(trades), rested, remaining)

    def add_market(
        self, side: int, qty: int, owner: str = "", ts: Optional[int] = None
    ) -> AddResult:
        _check_side(side)
        _check_qty(qty)
        ts = self._tick(ts)
        oid = self._new_oid()
        trades, touched, remaining = self._match(oid, side, None, qty, ts)
        self._flush(ts, trades, touched)
        return AddResult(oid, tuple(trades), 0, remaining)

    def cancel(self, oid: int, ts: Optional[int] = None) -> bool:
        """Cancel a resting order. Returns False if it is not resting."""
        ts = self._tick(ts)
        order = self._orders.pop(oid, None)
        if order is None:
            return False
        level = self._levels[order.side][order.price]
        level.remove(oid)
        self._totals[order.side][order.price] -= order.qty
        if not level:
            self._remove_level(order.side, order.price)
        self._flush(ts, [], {(order.side, order.price)})
        return True

    def best_bid(self) -> Optional[int]:
        return self._px[BUY][-1] if self._px[BUY] else None

    def best_ask(self) -> Optional[int]:
        return self._px[SELL][0] if self._px[SELL] else None

    def level_total(self, side: int, price: int) -> int:
        return self._totals[side].get(price, 0)

    def depth(self, side: int, levels: int = 5) -> List[Tuple[int, int]]:
        """Top ``levels`` price levels for ``side`` as (price, total_qty)."""
        prices = reversed(self._px[side]) if side == BUY else self._px[side]
        out: List[Tuple[int, int]] = []
        for p in prices:
            out.append((p, self._totals[side][p]))
            if len(out) == levels:
                break
        return out

    def order(self, oid: int) -> Optional[Order]:
        return self._orders.get(oid)

    def resting_oids(self, side: int, price: int) -> Tuple[int, ...]:
        """Resting oids at a level in queue order (front first)."""
        level = self._levels[side].get(price)
        return tuple(level) if level else ()

    def check_invariants(self) -> None:
        """Raise AssertionError if internal state is inconsistent."""
        bb, ba = self.best_bid(), self.best_ask()
        if bb is not None and ba is not None:
            assert bb < ba, f"book is crossed: bid {bb} >= ask {ba}"
        for side in (BUY, SELL):
            assert self._px[side] == sorted(self._px[side])
            assert set(self._px[side]) == set(self._levels[side]) == set(self._totals[side])
            for price, level in self._levels[side].items():
                assert level, "empty level left in book"
                assert self._totals[side][price] == sum(
                    self._orders[o].qty for o in level
                ), "level total does not match its orders"
                for o in level:
                    order = self._orders[o]
                    assert order.side == side and order.price == price
                    assert order.qty > 0

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _tick(self, ts: Optional[int]) -> int:
        if ts is None:
            return self._ts
        if ts < self._ts:
            raise ValueError(f"time went backwards: {ts} < {self._ts}")
        self._ts = ts
        return ts

    def _new_oid(self) -> int:
        oid = self._next_oid
        self._next_oid += 1
        return oid

    def _best(self, side: int) -> Optional[int]:
        return self.best_bid() if side == BUY else self.best_ask()

    def _crosses(self, side: int, price: int) -> bool:
        if side == BUY:
            ask = self.best_ask()
            return ask is not None and price >= ask
        bid = self.best_bid()
        return bid is not None and price <= bid

    def _match(
        self, taker_oid: int, side: int, limit: Optional[int], qty: int, ts: int
    ) -> Tuple[List[Trade], Set[Tuple[int, int]], int]:
        opp = -side
        trades: List[Trade] = []
        touched: Set[Tuple[int, int]] = set()
        remaining = qty
        while remaining > 0:
            best = self._best(opp)
            if best is None:
                break
            if limit is not None:
                if side == BUY and best > limit:
                    break
                if side == SELL and best < limit:
                    break
            level = self._levels[opp][best]
            while remaining > 0 and level:
                maker_oid = level[0]
                maker = self._orders[maker_oid]
                fill = min(remaining, maker.qty)
                maker.qty -= fill
                remaining -= fill
                self._totals[opp][best] -= fill
                trades.append(
                    Trade(ts, best, fill, side, maker_oid, taker_oid, maker.owner)
                )
                if maker.qty == 0:
                    level.popleft()
                    del self._orders[maker_oid]
            touched.add((opp, best))
            if not level:
                self._remove_level(opp, best)
        return trades, touched, remaining

    def _rest(self, oid: int, side: int, price: int, qty: int, owner: str, ts: int) -> None:
        if price not in self._levels[side]:
            self._levels[side][price] = deque()
            self._totals[side][price] = 0
            insort(self._px[side], price)
        self._levels[side][price].append(oid)
        self._totals[side][price] += qty
        self._orders[oid] = Order(oid, side, price, qty, owner, ts)

    def _remove_level(self, side: int, price: int) -> None:
        del self._levels[side][price]
        del self._totals[side][price]
        px = self._px[side]
        i = bisect_left(px, price)
        assert i < len(px) and px[i] == price
        del px[i]

    def _flush(self, ts: int, trades: Iterable[Trade], touched: Set[Tuple[int, int]]) -> None:
        if self._listener is None:
            return
        for t in trades:
            self._listener(
                ("TRADE", ts, t.price, t.qty, t.aggressor, t.maker_oid, t.taker_oid)
            )
        for side, price in sorted(touched):
            self._listener(("L2", ts, side, price, self._totals[side].get(price, 0)))
