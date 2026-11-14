import unittest

from lob import BUY, SELL, OrderBook
from mmbt.fill_models import NaiveTouch, QueuePessimistic, QueueProportional
from mmbt.sim import SimConfig, Simulation


def feed_l2(model, ts, side, price, total):
    model.on_event(("L2", ts, side, price, total))


def feed_trade(model, ts, price, qty, aggressor):
    model.on_event(("TRADE", ts, price, qty, aggressor))


class HandComputedQueue(unittest.TestCase):
    """Scenario: 5 lots of others already bid at 100. We join with 3 lots."""

    def setUp(self):
        self.models = {m.name: m for m in (NaiveTouch(), QueuePessimistic(), QueueProportional())}
        for m in self.models.values():
            feed_l2(m, 1, BUY, 100, 5)            # others: 5 lots
            feed_l2(m, 2, BUY, 100, 8)            # our 3 lots join behind them
            m.on_own_add(1, BUY, 100, 3)          # ahead should be 8 - 3 = 5

    def test_ahead_is_queue_ahead_of_us(self):
        for name in ("queue_pessimistic", "queue_proportional"):
            self.assertEqual(self.models[name].orders[1].ahead, 5)

    def test_first_trade_is_absorbed_by_queue_ahead(self):
        # A sell aggressor takes 4 lots at 100: the 4 lots come out of the 5 ahead of us.
        for m in self.models.values():
            feed_trade(m, 3, 100, 4, SELL)
            feed_l2(m, 3, BUY, 100, 4)            # 8 - 4 traded = 4 left (1 ahead + our 3)
        self.assertEqual(self.models["naive_touch"].orders[1].filled, 3)   # ignores queue
        self.assertEqual(self.models["queue_pessimistic"].orders[1].filled, 0)
        self.assertEqual(self.models["queue_proportional"].orders[1].filled, 0)
        self.assertEqual(self.models["queue_pessimistic"].orders[1].ahead, 1)

    def test_second_trade_reaches_us(self):
        for m in self.models.values():
            feed_trade(m, 3, 100, 4, SELL)
            feed_l2(m, 3, BUY, 100, 4)
            feed_trade(m, 4, 100, 2, SELL)        # 1 more ahead, then 1 reaches us
            feed_l2(m, 4, BUY, 100, 2)            # 4 - 2 traded = 2 left, both ours
        self.assertEqual(self.models["queue_pessimistic"].orders[1].filled, 1)
        self.assertEqual(self.models["queue_proportional"].orders[1].filled, 1)
        self.assertEqual(self.models["queue_pessimistic"].inventory, 1)
        self.assertEqual(self.models["queue_pessimistic"].cash, -100)  # bought 1 at 100


class CancelAttribution(unittest.TestCase):
    """10 lots ahead of us, 5 lots behind us, we hold 1 lot. Others cancel 4 lots from ahead."""

    def _model(self, cls):
        m = cls()
        feed_l2(m, 1, BUY, 100, 10)
        feed_l2(m, 2, BUY, 100, 11)
        m.on_own_add(1, BUY, 100, 1)              # ahead = 10
        feed_l2(m, 3, BUY, 100, 16)               # 5 lots join behind us (adds are ignored)
        feed_l2(m, 4, BUY, 100, 12)               # 4 lots cancelled by others
        return m

    def test_pessimistic_cancels_are_behind_us(self):
        self.assertEqual(self._model(QueuePessimistic).orders[1].ahead, 10)

    def test_proportional_cancels_spread_over_queue(self):
        # ahead * (1 - 4/16) = 10 * 0.75 = 7.5, rounded half-to-even to 8
        self.assertEqual(self._model(QueueProportional).orders[1].ahead, 8)


class OwnCancelsMoveUsForward(unittest.TestCase):
    def test_removing_our_order_ahead_moves_later_order_up(self):
        m = QueuePessimistic()
        feed_l2(m, 1, BUY, 100, 2)
        m.on_own_add(1, BUY, 100, 2)              # A: 2 lots, ahead 0
        feed_l2(m, 2, BUY, 100, 3)
        m.on_own_add(2, BUY, 100, 1)              # B: 1 lot, ahead 2 (A is ahead of B)
        self.assertEqual(m.orders[2].ahead, 2)
        m.on_own_cancel(1, 2, True)               # we cancel A
        feed_l2(m, 3, BUY, 100, 1)                # book shows only B's 1 lot
        self.assertEqual(m.orders[2].ahead, 0)    # B is now front of queue
        self.assertEqual(m.orders[2].active, True)

    def test_own_removal_is_not_read_as_others_cancelling(self):
        m = QueueProportional()
        feed_l2(m, 1, BUY, 100, 4)
        m.on_own_add(1, BUY, 100, 4)
        m.on_own_cancel(1, 4, True)
        feed_l2(m, 2, BUY, 100, 0)
        self.assertEqual(m.orders[1].ahead, 0)
        self.assertFalse(m.orders[1].active)


class SimulationChecks(unittest.TestCase):
    def test_same_seed_gives_identical_results(self):
        a = Simulation(SimConfig(seed=3, n_steps=3000)).run()
        b = Simulation(SimConfig(seed=3, n_steps=3000)).run()
        self.assertEqual(a, b)

    def test_different_seeds_differ(self):
        a = Simulation(SimConfig(seed=3, n_steps=3000)).run()
        b = Simulation(SimConfig(seed=4, n_steps=3000)).run()
        self.assertNotEqual(a["truth"], b["truth"])

    def test_book_invariants_and_feed_completeness(self):
        sim = Simulation(SimConfig(seed=5, n_steps=5000))
        sim.run()
        sim.exchange.check_invariants()
        # The anonymised L2 stream must reconstruct the exact book for every model.
        ex = sim.exchange
        for m in sim.models:
            for side in (BUY, SELL):
                for price, total in ex.depth(side, 10_000):
                    self.assertEqual(m.view.total(side, price), total, (m.name, side, price))

    def test_model_inventory_matches_its_fills(self):
        sim = Simulation(SimConfig(seed=6, n_steps=5000))
        sim.run()
        for m in sim.models:
            self.assertEqual(m.inventory, sum(o.side * o.filled for o in m.orders.values()))
            self.assertEqual(m.cash, -sum(o.side * o.price * o.filled for o in m.orders.values()))

    def test_truth_accounting(self):
        sim = Simulation(SimConfig(seed=7, n_steps=5000))
        r = sim.run()
        t = r["truth"]
        self.assertEqual(t["filled_qty"], t["buy_qty"] + t["sell_qty"])
        self.assertEqual(t["inventory"], t["buy_qty"] - t["sell_qty"])
        self.assertEqual(sum(sim.truth_qty.values()), t["filled_qty"])

    def test_latency_cancel_never_overtakes_its_add(self):
        sim = Simulation(SimConfig(seed=8, n_steps=10, l_cancel_ns=0, l_in_ns=500))
        sim._send_new(BUY, 99, 1000)
        w = sim.working[BUY]
        sim._send_cancel(w, 1000)
        times = sorted((t, kind, payload) for t, _, kind, payload in sim._heap)
        add_t = next(t for t, k, _ in times if k == "arrive_add")
        cancel_t = next(t for t, k, _ in times if k == "arrive_cancel")
        self.assertGreater(cancel_t, add_t)


if __name__ == "__main__":
    unittest.main()
