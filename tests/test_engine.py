import random
import unittest

from lob import BUY, SELL, OrderBook


class PriorityAndMatching(unittest.TestCase):
    def test_price_priority_then_time_priority(self):
        b = OrderBook()
        a1 = b.add_limit(SELL, 102, 5, "a1").oid
        a2 = b.add_limit(SELL, 101, 5, "a2").oid
        a3 = b.add_limit(SELL, 101, 5, "a3").oid
        r = b.add_market(BUY, 12)
        # Best price first (101), then FIFO within 101 (a2 before a3), then 102.
        self.assertEqual([t.maker_oid for t in r.trades], [a2, a3, a1])
        self.assertEqual([t.qty for t in r.trades], [5, 5, 2])
        self.assertEqual([t.price for t in r.trades], [101, 101, 102])
        self.assertEqual(r.unfilled_qty, 0)
        self.assertEqual(b.level_total(SELL, 102), 3)
        self.assertEqual(b.best_ask(), 102)  # 101 fully consumed, 102 partly left
        b.check_invariants()

    def test_trade_executes_at_resting_price(self):
        b = OrderBook()
        b.add_limit(SELL, 100, 3, "maker")
        r = b.add_limit(BUY, 105, 3, "taker")  # aggressive buy far above the ask
        self.assertEqual(len(r.trades), 1)
        self.assertEqual(r.trades[0].price, 100)
        self.assertEqual(r.rested_qty, 0)

    def test_partial_fill_remainder_rests_at_limit(self):
        b = OrderBook()
        b.add_limit(SELL, 100, 4, "maker")
        r = b.add_limit(BUY, 101, 10, "taker")
        self.assertEqual(r.filled_qty, 4)
        self.assertEqual(r.rested_qty, 6)
        self.assertEqual(b.best_bid(), 101)
        self.assertEqual(b.level_total(BUY, 101), 6)
        b.check_invariants()

    def test_ioc_cancels_remainder(self):
        b = OrderBook()
        b.add_limit(SELL, 100, 4, "maker")
        r = b.add_limit(BUY, 101, 10, "taker", tif="IOC")
        self.assertEqual(r.filled_qty, 4)
        self.assertEqual(r.rested_qty, 0)
        self.assertEqual(r.unfilled_qty, 6)
        self.assertIsNone(b.best_bid())

    def test_limit_does_not_trade_through_its_price(self):
        b = OrderBook()
        b.add_limit(SELL, 105, 4, "maker")
        r = b.add_limit(BUY, 100, 4, "taker")
        self.assertEqual(r.trades, ())
        self.assertEqual(b.best_bid(), 100)
        self.assertEqual(b.best_ask(), 105)

    def test_market_order_sweeps_and_never_rests(self):
        b = OrderBook()
        b.add_limit(SELL, 100, 2, "m")
        b.add_limit(SELL, 101, 2, "m")
        r = b.add_market(BUY, 10)
        self.assertEqual(r.filled_qty, 4)
        self.assertEqual(r.unfilled_qty, 6)
        self.assertIsNone(b.best_bid())
        self.assertIsNone(b.best_ask())

    def test_post_only_rejects_when_crossing(self):
        b = OrderBook()
        b.add_limit(SELL, 100, 2, "m")
        r = b.add_limit(BUY, 100, 2, "t", post_only=True)
        self.assertTrue(r.rejected)
        self.assertEqual(r.reason, "post_only_would_cross")
        self.assertEqual(b.level_total(SELL, 100), 2)  # nothing traded
        r2 = b.add_limit(BUY, 99, 2, "t", post_only=True)
        self.assertFalse(r2.rejected)
        self.assertEqual(b.best_bid(), 99)


class Cancels(unittest.TestCase):
    def test_cancel_removes_order_and_level(self):
        b = OrderBook()
        oid = b.add_limit(BUY, 100, 5, "x").oid
        self.assertTrue(b.cancel(oid))
        self.assertIsNone(b.best_bid())
        self.assertFalse(b.cancel(oid))  # second cancel is a no-op

    def test_cancel_preserves_queue_order_of_others(self):
        b = OrderBook()
        o1 = b.add_limit(BUY, 100, 1, "a").oid
        o2 = b.add_limit(BUY, 100, 1, "b").oid
        o3 = b.add_limit(BUY, 100, 1, "c").oid
        b.cancel(o2)
        self.assertEqual(b.resting_oids(BUY, 100), (o1, o3))

    def test_cancelled_order_cannot_trade(self):
        b = OrderBook()
        oid = b.add_limit(SELL, 100, 5, "m").oid
        b.cancel(oid)
        r = b.add_market(BUY, 5)
        self.assertEqual(r.trades, ())
        self.assertEqual(r.unfilled_qty, 5)

    def test_fully_filled_order_is_gone(self):
        b = OrderBook()
        oid = b.add_limit(SELL, 100, 5, "m").oid
        b.add_market(BUY, 5)
        self.assertIsNone(b.order(oid))
        self.assertFalse(b.cancel(oid))


class Depth(unittest.TestCase):
    def test_depth_ordering(self):
        b = OrderBook()
        for p in (99, 98, 97):
            b.add_limit(BUY, p, 1, "b")
        for p in (101, 102, 103):
            b.add_limit(SELL, p, 2, "s")
        self.assertEqual(b.depth(BUY, 2), [(99, 1), (98, 1)])
        self.assertEqual(b.depth(SELL, 5), [(101, 2), (102, 2), (103, 2)])


class Validation(unittest.TestCase):
    def test_bad_inputs_raise(self):
        b = OrderBook()
        with self.assertRaises(ValueError):
            b.add_limit(0, 100, 1)
        with self.assertRaises(ValueError):
            b.add_limit(BUY, 100, 0)
        with self.assertRaises(ValueError):
            b.add_limit(BUY, 100.5, 1)
        with self.assertRaises(ValueError):
            b.add_limit(BUY, 100, 1, tif="DAY")
        b.add_limit(BUY, 100, 1, ts=10)
        with self.assertRaises(ValueError):
            b.add_limit(BUY, 100, 1, ts=5)  # time went backwards


class MarketDataFeed(unittest.TestCase):
    def test_trade_then_l2_events_are_emitted(self):
        events = []
        b = OrderBook(listener=events.append)
        b.add_limit(SELL, 100, 5, "m", ts=1)
        self.assertEqual(events, [("L2", 1, SELL, 100, 5)])
        events.clear()
        b.add_market(BUY, 2, ts=2)
        self.assertEqual(events[0][0], "TRADE")
        self.assertEqual(events[0][2:5], (100, 2, BUY))
        self.assertEqual(events[1], ("L2", 2, SELL, 100, 3))

    def test_cancel_emits_zero_total_when_level_removed(self):
        events = []
        b = OrderBook(listener=events.append)
        oid = b.add_limit(BUY, 100, 5, "m", ts=1).oid
        events.clear()
        b.cancel(oid, ts=2)
        self.assertEqual(events, [("L2", 2, BUY, 100, 0)])


class Fuzz(unittest.TestCase):
    """Random order flow: the book must never cross and totals must add up."""

    def test_random_flow_keeps_invariants(self):
        for seed in range(20):
            rng = random.Random(seed)
            b = OrderBook()
            live = []
            ts = 0
            for _ in range(2000):
                ts += rng.randint(0, 3)
                roll = rng.random()
                if roll < 0.5:
                    side = rng.choice((BUY, SELL))
                    price = 100 + rng.randint(-5, 5)
                    res = b.add_limit(side, price, rng.randint(1, 10), "f", ts=ts,
                                      tif=rng.choice(("GTC", "GTC", "IOC")),
                                      post_only=rng.random() < 0.2)
                    if res.rested_qty:
                        live.append(res.oid)
                elif roll < 0.6:
                    b.add_market(rng.choice((BUY, SELL)), rng.randint(1, 15), "f", ts=ts)
                elif live:
                    b.cancel(live.pop(rng.randrange(len(live))), ts=ts)
                b.check_invariants()

    def test_quantity_conservation(self):
        """Accounting identities that must hold for any order flow.

        Per order:  qty = filled + rested + unfilled   (rejected orders excluded)
        Globally:   total rested ever = resting now + qty consumed as maker
                    qty consumed as maker = qty consumed as taker = total traded
        """
        rng = random.Random(7)
        b = OrderBook()
        submitted = rested_ever = unfilled = taker_filled = maker_consumed = 0
        ts = 0
        for _ in range(3000):
            ts += 1
            side = rng.choice((BUY, SELL))
            q = rng.randint(1, 8)
            if rng.random() < 0.7:
                r = b.add_limit(side, 100 + rng.randint(-3, 3), q, "f", ts=ts,
                                post_only=rng.random() < 0.1)
                if r.rejected:
                    continue
            else:
                r = b.add_market(side, q, "f", ts=ts)
            self.assertEqual(q, r.filled_qty + r.rested_qty + r.unfilled_qty)
            submitted += q
            rested_ever += r.rested_qty
            unfilled += r.unfilled_qty
            taker_filled += r.filled_qty
            # qty this order consumed from resting makers is traded qty from the maker side
            maker_consumed += sum(t.qty for t in r.trades)
        resting_now = sum(
            b.level_total(s, p) for s in (BUY, SELL) for p, _ in b.depth(s, 10_000)
        )
        self.assertEqual(submitted, taker_filled + rested_ever + unfilled)
        # every unit that ever rested is either still resting or was consumed as maker
        self.assertEqual(rested_ever, resting_now + maker_consumed)
        self.assertGreater(maker_consumed, 0)
        b.check_invariants()


if __name__ == "__main__":
    unittest.main()
