"""Fill models: how a backtester decides whether a resting order of ours filled.

Every model sees only the public feed (anonymised TRADE and L2 events) plus our
own order submissions and cancels. None of them can see the exchange's resting
orders, so each has to infer queue position from aggregate data.

Models (all run as shadows on identical order flow; none of them affects the
strategy's behaviour, which is driven by ground truth):

* ``NaiveTouch``          fills our order from any trade at our price, up to
                          the trade size. Ignores queue position entirely.
* ``QueuePessimistic``    tracks how many shares sit ahead of us. Trades consume
                          the queue ahead first. Cancels never move us forward.
* ``QueueProportional``   same, but cancels ahead of us are assumed to be spread
                          pro-rata across the queue (probability-queue model).

Mechanics shared by all models
------------------------------
A TRADE event at price p with aggressor side A consumes resting liquidity on
side -A at price p. The L2 update for that level then reports the new total.
For each level, net = (new_total - prev_total) + trades_since_last_update
gives adds minus cancels. Negative net is cancels, and our own cancels are
subtracted out first so they are not mistaken for queue movement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .view import L2View


@dataclass
class TrackedOrder:
    oid: int
    side: int
    price: int
    qty: int
    remaining: int
    ahead: int
    active: bool = True
    filled: int = 0
    fills: List[Tuple[int, int]] = field(default_factory=list)  # (ts, qty)


class FillModel:
    name = "base"

    def __init__(self) -> None:
        self.view = L2View()
        self.orders: Dict[int, TrackedOrder] = {}
        self._live: Dict[Tuple[int, int], Dict[int, TrackedOrder]] = {}  # active orders by level
        self._pending: Dict[Tuple[int, int], int] = {}          # traded since last L2
        self._own_removed: Dict[Tuple[int, int], int] = {}      # our cancels, not yet seen in L2
        self.cash = 0      # signed cash from predicted fills (buy costs, sell adds)
        self.inventory = 0 # signed predicted position

    # ------------------------------------------------------------------ feed
    def on_event(self, ev: tuple) -> None:
        if ev[0] == "TRADE":
            _, ts, price, qty, aggressor = ev[:5]
            self._on_trade(ts, price, qty, aggressor)
        else:
            _, ts, side, price, total = ev
            self._on_l2(side, price, total)

    def _on_trade(self, ts: int, price: int, qty: int, aggressor: int) -> None:
        resting_side = -aggressor  # a sell aggressor hits resting bids
        key = (resting_side, price)
        self._pending[key] = self._pending.get(key, 0) + qty
        left = qty
        for o in self._active_at(resting_side, price):
            if left <= 0:
                break
            fill, used = self._consume(o, left)
            left -= used
            if fill > 0:
                self._record_fill(o, ts, fill)

    def _on_l2(self, side: int, price: int, total: int) -> None:
        key = (side, price)
        prev = self.view.total(side, price)
        traded = self._pending.pop(key, 0)
        self.view.apply(side, price, total)
        net = total - prev + traded            # adds minus cancels at this level
        own = self._own_removed.pop(key, 0)
        if net < 0:
            cancels = -net - own
            if cancels > 0:
                self._apply_cancels(side, price, cancels, total)

    # ------------------------------------------------------ our own orders
    def on_own_add(self, oid: int, side: int, price: int, qty: int) -> None:
        """Called after the order rested at the exchange and the L2 update was seen."""
        ahead = max(0, self.view.total(side, price) - qty)
        order = TrackedOrder(oid, side, price, qty, qty, ahead)
        self.orders[oid] = order
        self._live.setdefault((side, price), {})[oid] = order

    def on_own_cancel(self, oid: int, exchange_remaining: int, success: bool) -> None:
        """Called just before the exchange processes our cancel.

        ``success`` is False when the exchange already filled the order.
        """
        o = self.orders.get(oid)
        if o is None:
            return
        if success and exchange_remaining > 0:
            key = (o.side, o.price)
            self._own_removed[key] = self._own_removed.get(key, 0) + exchange_remaining
            # Our own order was ahead of any later order of ours at this level, so
            # removing it moves those later orders forward. Arrival order is oid order.
            for later in self._active_at(o.side, o.price):
                if later.oid > oid:
                    later.ahead = max(0, later.ahead - exchange_remaining)
        self._deactivate(o)

    # --------------------------------------------------------- helpers
    def _deactivate(self, o: TrackedOrder) -> None:
        o.active = False
        self._live.get((o.side, o.price), {}).pop(o.oid, None)

    def _active_at(self, side: int, price: int) -> List[TrackedOrder]:
        out = list(self._live.get((side, price), {}).values())
        out.sort(key=lambda o: o.oid)  # arrival order
        return out

    def _record_fill(self, o: TrackedOrder, ts: int, fill: int) -> None:
        o.remaining -= fill
        o.filled += fill
        o.fills.append((ts, fill))
        self.inventory += o.side * fill
        self.cash -= o.side * o.price * fill
        if o.remaining == 0:
            self._deactivate(o)

    def _apply_cancels(self, side: int, price: int, cancels: int, new_total: int) -> None:
        mine = self._active_at(side, price)
        ours = sum(o.remaining for o in mine)
        others = max(0, new_total - ours)
        base = new_total + cancels
        for o in mine:
            self._cancel_model(o, cancels, base, others)

    # ----------------------------------------------- model-specific hooks
    def _consume(self, o: TrackedOrder, left: int) -> Tuple[int, int]:
        """Return (fill_qty, used_qty_of_trade) for one of our orders."""
        raise NotImplementedError

    def _cancel_model(self, o: TrackedOrder, cancels: int, base: int, others: int) -> None:
        raise NotImplementedError


class NaiveTouch(FillModel):
    """Any trade at our price fills us, up to the trade size. No queue."""

    name = "naive_touch"

    def _consume(self, o: TrackedOrder, left: int) -> Tuple[int, int]:
        fill = min(o.remaining, left)
        return fill, fill

    def _cancel_model(self, o, cancels, base, others) -> None:
        return  # queue position is not modelled


class QueuePessimistic(FillModel):
    """Queue position tracked; cancels ahead of us are never assumed."""

    name = "queue_pessimistic"

    def _consume(self, o: TrackedOrder, left: int) -> Tuple[int, int]:
        ahead_used = min(o.ahead, left)
        o.ahead -= ahead_used
        rest = left - ahead_used
        fill = min(o.remaining, rest)
        return fill, ahead_used + fill

    def _cancel_model(self, o, cancels, base, others) -> None:
        # Worst case for us: every cancel was behind us. We only cap the
        # queue ahead at what is actually still there.
        o.ahead = min(o.ahead, others)


class QueueProportional(FillModel):
    """Queue position tracked; cancels are spread pro-rata over the queue."""

    name = "queue_proportional"

    def _consume(self, o: TrackedOrder, left: int) -> Tuple[int, int]:
        ahead_used = min(o.ahead, left)
        o.ahead -= ahead_used
        rest = left - ahead_used
        fill = min(o.remaining, rest)
        return fill, ahead_used + fill

    def _cancel_model(self, o, cancels, base, others) -> None:
        frac = cancels / base if base > 0 else 0.0
        o.ahead = min(others, int(round(o.ahead * (1.0 - frac))))


ALL_MODELS = (NaiveTouch, QueuePessimistic, QueueProportional)
