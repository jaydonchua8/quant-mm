"""Discrete-event market-making simulation.

Architecture
------------
* Ground truth: an ``OrderBook`` (the exchange). Background participants and our
  strategy both trade against it, so queue position and fills are exact here.
* Participant view: the strategy only sees the public L2 feed, delayed by
  ``l_md_ns``. Its quotes are based on that delayed view.
* Latency: our orders reach the exchange ``l_in_ns`` after we send them. Cancels
  take ``l_cancel_ns`` and can never overtake the order they cancel. Execution
  reports reach us ``l_md_ns`` after the fill.
* Fill models: the three models in ``fill_models`` receive exactly the public
  feed plus our own order events, with no access to exchange internals. They run
  as shadows: they predict our fills on the same order flow, but never affect
  the strategy. Their predictions are then scored against ground truth.

Background flow (synthetic, so the experiment is reproducible)
--------------------------------------------------------------
* A fundamental value takes a random walk of +/-1 tick.
* Liquidity providers post limit orders around that value and cancel at random.
* Market orders arrive at random. A fraction of them are informed: they trade
  in the direction of the fundamental's next move. This creates adverse
  selection for market makers, the main risk a real MM model has to get right.

Limitations
-----------
The background flow is a toy. Results describe this market, not a real venue.
The shadow models are fed synchronously, so ``l_md_ns`` only affects the
strategy, not the fill-model comparison.
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from lob.engine import BUY, SELL, OrderBook
from .fill_models import ALL_MODELS
from .strategy import MMConfig, MarketMaker
from .view import L2View


@dataclass(frozen=True)
class SimConfig:
    seed: int = 0
    n_steps: int = 100_000
    dt_ns: int = 1_000_000            # background step: 1 ms
    fundamental_start: int = 1000     # ticks
    p_move: float = 0.20              # chance the fundamental moves in a step
    p_post: float = 0.45              # background step: post a limit order
    p_cancel: float = 0.30            # background step: cancel a resting background order
    p_mkt: float = 0.15               # background step: market order (remainder: nothing)
    p_inform: float = 0.40            # share of market orders that are informed
    max_offset: int = 4               # background limit orders: 1..max_offset ticks from fundamental
    l_in_ns: int = 300_000            # our order reaches the exchange
    l_cancel_ns: int = 300_000        # our cancel reaches the exchange
    l_md_ns: int = 200_000            # market data and fill reports reach us
    mm: MMConfig = field(default_factory=MMConfig)


@dataclass
class _Working:
    cid: int
    side: int
    price: int
    arrive_ns: int


class Simulation:
    def __init__(self, cfg: SimConfig) -> None:
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.now = 0
        self._heap: list = []
        self._seq = 0

        self.exchange = OrderBook(listener=self._on_exchange)
        self.models = [M() for M in ALL_MODELS]
        self.md = L2View()                       # strategy's delayed view
        self.mm = MarketMaker(cfg.mm)
        self.working: Dict[int, Optional[_Working]] = {BUY: None, SELL: None}

        self._next_cid = 1
        self._ex_of: Dict[int, int] = {}         # client id -> exchange oid
        self._cid_of: Dict[int, int] = {}        # exchange oid -> client id
        self._placed: List[int] = []
        self.rejects = 0
        self.truth_qty: Dict[int, int] = {}
        self.truth_cash = 0
        self.truth_inv = 0
        self.truth_fills = 0
        self.truth_side_qty = {BUY: 0, SELL: 0}

        # Fundamental path: moves[k] is applied at background step k.
        n = cfg.n_steps + 2
        self._moves = [
            (1 if self.rng.random() < 0.5 else -1) if self.rng.random() < cfg.p_move else 0
            for _ in range(n)
        ]
        self._f = cfg.fundamental_start
        self._bg_oids: List[int] = []

    # ------------------------------------------------------------ scheduling
    def _push(self, t: int, kind: str, payload) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (t, self._seq, kind, payload))

    def run(self) -> dict:
        self._seed_book()
        self._push(0, "bg", 0)
        self._push(0, "tick", None)
        end = self.cfg.n_steps * self.cfg.dt_ns
        while self._heap and self._heap[0][0] < end:
            t, _, kind, payload = heapq.heappop(self._heap)
            self.now = t
            getattr(self, f"_h_{kind}")(t, payload)
        return self.results()

    def _h_bg(self, t: int, k: int) -> None:
        self._background_step(t, k)
        if k + 1 < self.cfg.n_steps:
            self._push(t + self.cfg.dt_ns, "bg", k + 1)

    def _h_tick(self, t: int, _) -> None:
        self._quote(t)
        self._push(t + self.cfg.mm.quote_interval_ns, "tick", None)

    def _h_arrive_add(self, t: int, payload) -> None:
        cid, side, price = payload
        qty = self.cfg.mm.quote_qty
        res = self.exchange.add_limit(side, price, qty, owner="mm", ts=t, post_only=True)
        if res.rejected:
            self.rejects += 1
            w = self.working[side]
            if w is not None and w.cid == cid:
                self.working[side] = None
            return
        self._ex_of[cid] = res.oid
        self._cid_of[res.oid] = cid
        self._placed.append(cid)
        for m in self.models:
            m.on_own_add(cid, side, price, qty)

    def _h_arrive_cancel(self, t: int, cid: int) -> None:
        ex_oid = self._ex_of.get(cid)
        if ex_oid is None:
            return
        order = self.exchange.order(ex_oid)
        success = order is not None
        remaining = order.qty if success else 0
        for m in self.models:
            m.on_own_cancel(cid, remaining, success)
        if success:
            self.exchange.cancel(ex_oid, ts=t)

    def _h_md(self, t: int, ev: tuple) -> None:
        _, _, side, price, total = ev
        self.md.apply(side, price, total)

    def _h_exec(self, t: int, payload) -> None:
        cid, side, qty, remaining = payload
        self.mm.inventory += side * qty
        w = self.working[side]
        if w is not None and w.cid == cid and remaining == 0:
            self.working[side] = None

    # ------------------------------------------------------------ strategy
    def _quote(self, t: int) -> None:
        targets = self.mm.targets(self.md)
        for side in (BUY, SELL):
            tgt = targets[side]
            w = self.working[side]
            if w is not None and tgt == w.price:
                continue
            if w is not None:
                self._send_cancel(w, t)
            if tgt is not None:
                self._send_new(side, tgt, t)

    def _send_new(self, side: int, price: int, t: int) -> None:
        cid = self._next_cid
        self._next_cid += 1
        arrive = t + self.cfg.l_in_ns
        self.working[side] = _Working(cid, side, price, arrive)
        self._push(arrive, "arrive_add", (cid, side, price))

    def _send_cancel(self, w: _Working, t: int) -> None:
        self.working[w.side] = None
        at = max(t + self.cfg.l_cancel_ns, w.arrive_ns + 1)  # cannot overtake its add
        self._push(at, "arrive_cancel", w.cid)

    # ------------------------------------------------------------ exchange
    def _on_exchange(self, ev: tuple) -> None:
        if ev[0] == "TRADE":
            _, ts, price, qty, aggressor, maker_oid, _taker = ev
            cid = self._cid_of.get(maker_oid)
            if cid is not None:
                side = -aggressor
                order = self.exchange.order(maker_oid)
                remaining = order.qty if order is not None else 0
                self.truth_qty[cid] = self.truth_qty.get(cid, 0) + qty
                self.truth_cash -= side * price * qty
                self.truth_inv += side * qty
                self.truth_fills += 1
                self.truth_side_qty[side] += qty
                self._push(ts + self.cfg.l_md_ns, "exec", (cid, side, qty, remaining))
            for m in self.models:
                m.on_event(ev)
        else:
            for m in self.models:
                m.on_event(ev)
            self._push(ev[1] + self.cfg.l_md_ns, "md", ev)

    # ---------------------------------------------------------- background
    def _seed_book(self) -> None:
        f0 = self._f
        for i in range(1, 6):
            for side, px in ((BUY, f0 - i), (SELL, f0 + i)):
                res = self.exchange.add_limit(side, px, 3, owner="bg", ts=0)
                self._bg_oids.append(res.oid)

    def _background_step(self, t: int, k: int) -> None:
        cfg, rng, ex = self.cfg, self.rng, self.exchange
        self._f += self._moves[k]
        u = rng.random()
        if u < cfg.p_post:
            side = BUY if rng.random() < 0.5 else SELL
            off = rng.randint(1, cfg.max_offset)
            price = self._f - off if side == BUY else self._f + off
            res = ex.add_limit(side, price, rng.randint(1, 5), owner="bg", ts=t)
            if res.rested_qty:
                self._bg_oids.append(res.oid)
        elif u < cfg.p_post + cfg.p_cancel:
            while self._bg_oids:
                i = rng.randrange(len(self._bg_oids))
                oid = self._bg_oids[i]
                self._bg_oids[i] = self._bg_oids[-1]
                self._bg_oids.pop()
                if ex.cancel(oid, ts=t):
                    break
        elif u < cfg.p_post + cfg.p_cancel + cfg.p_mkt:
            nxt = self._moves[k + 1]
            if nxt != 0 and rng.random() < cfg.p_inform:
                side = BUY if nxt > 0 else SELL
            else:
                side = BUY if rng.random() < 0.5 else SELL
            ex.add_market(side, rng.randint(1, 4), owner="bg", ts=t)

    # ------------------------------------------------------------- results
    def results(self) -> dict:
        ex = self.exchange
        bb, ba = ex.best_bid(), ex.best_ask()
        mid = (bb + ba) / 2.0 if bb is not None and ba is not None else float(self._f)
        placed = self._placed
        truth = {cid: self.truth_qty.get(cid, 0) for cid in placed}
        true_total = sum(truth.values())

        out = {
            "seed": self.cfg.seed,
            "mid_final": mid,
            "orders_placed": len(placed),
            "orders_rejected_post_only": self.rejects,
            "truth": {
                "filled_qty": true_total,
                "buy_qty": self.truth_side_qty[BUY],
                "sell_qty": self.truth_side_qty[SELL],
                "fill_events": self.truth_fills,
                "fill_rate_per_order": (
                    sum(1 for q in truth.values() if q > 0) / len(placed) if placed else 0.0
                ),
                "inventory": self.truth_inv,
                "pnl": self.truth_cash + self.truth_inv * mid,
            },
            "models": {},
        }
        for m in self.models:
            pred = {cid: (m.orders[cid].filled if cid in m.orders else 0) for cid in placed}
            diffs = [pred[c] - truth[c] for c in placed]
            pred_total = sum(pred.values())
            pnl = m.cash + m.inventory * mid
            out["models"][m.name] = {
                "filled_qty": pred_total,
                "fill_error_pct": (
                    100.0 * (pred_total - true_total) / true_total if true_total else 0.0
                ),
                "overfill_qty": sum(max(0, d) for d in diffs),
                "underfill_qty": sum(max(0, -d) for d in diffs),
                "mae_qty_per_order": (
                    sum(abs(d) for d in diffs) / len(diffs) if diffs else 0.0
                ),
                "fill_rate_per_order": (
                    sum(1 for p in pred.values() if p > 0) / len(placed) if placed else 0.0
                ),
                "pnl": pnl,
                "pnl_error": pnl - out["truth"]["pnl"],
            }
        return out


def run_once(seed: int = 0, **overrides) -> dict:
    cfg = SimConfig(seed=seed, **overrides)
    return Simulation(cfg).run()
