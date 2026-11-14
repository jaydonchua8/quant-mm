"""Passive market-making strategy: quote around fair value, skew by inventory."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional

from lob.engine import BUY, SELL


@dataclass(frozen=True)
class MMConfig:
    quote_qty: int = 2                    # lots per quote
    half_spread: int = 2                  # ticks from fair value to each quote
    skew: float = 0.5                     # ticks of shift per unit of inventory
    max_inventory: int = 10               # stop quoting the side that adds risk
    quote_interval_ns: int = 10_000_000   # 10 ms


class MarketMaker:
    """Pure decision logic. Holds only what it can know: its (delayed) view and fills."""

    def __init__(self, cfg: MMConfig) -> None:
        self.cfg = cfg
        self.inventory = 0  # updated from execution reports, so it lags the exchange

    def targets(self, view) -> Dict[int, Optional[int]]:
        """Desired resting price per side, or None for no quote."""
        bb, ba = view.best_bid(), view.best_ask()
        if bb is None or ba is None or ba <= bb:
            return {BUY: None, SELL: None}
        c = self.cfg
        fair = (bb + ba) / 2.0
        shift = c.skew * self.inventory
        bid: Optional[int] = math.floor(fair - c.half_spread - shift)
        ask: Optional[int] = math.ceil(fair + c.half_spread - shift)
        # Stay passive: never quote through the touch (post-only would reject anyway).
        bid = min(bid, ba - 1)
        ask = max(ask, bb + 1)
        if self.inventory >= c.max_inventory:
            bid = None
        if self.inventory <= -c.max_inventory:
            ask = None
        if bid is not None and ask is not None:
            assert bid < ask, (bid, ask)
        return {BUY: bid, SELL: ask}
