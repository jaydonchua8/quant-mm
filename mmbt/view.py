"""Participant-side view of the book, built only from anonymised L2 updates."""

from __future__ import annotations

from typing import Dict, Optional

from lob.engine import BUY, SELL


class L2View:
    """Price-level totals as a participant would reconstruct them from L2 data."""

    def __init__(self) -> None:
        self._t: Dict[int, Dict[int, int]] = {BUY: {}, SELL: {}}

    def apply(self, side: int, price: int, total: int) -> None:
        if total > 0:
            self._t[side][price] = total
        else:
            self._t[side].pop(price, None)

    def total(self, side: int, price: int) -> int:
        return self._t[side].get(price, 0)

    def best_bid(self) -> Optional[int]:
        return max(self._t[BUY], default=None)

    def best_ask(self) -> Optional[int]:
        return min(self._t[SELL], default=None)
