# Matching engine + market-making backtester

Two components, built in Python 3.13 with the standard library only:

1. **`lob/`**: a price-time priority limit order book (matching engine). It supports limit, market, IOC, and post-only orders, cancels, depth snapshots, and an anonymised L2 + trade feed.
2. **`mmbt/`**: a discrete-event market-making backtester. The exchange is simulated as ground truth, and the backtester estimates its own fills from the public feed alone. Three fill models run side by side and are scored against the truth.

## Run it

```bash
python3 -m unittest discover -s tests -v     # 30 tests, about 1 second
python3 run_experiment.py                    # 20 seeds x 2 scenarios, about 45 seconds
python3 run_experiment.py --seeds 3          # quick run
```

Output goes to `results/results.md` (summary tables) and `results/results.json` (per-seed numbers and summary).

## Layout

```
lob/engine.py            matching engine (OrderBook, AddResult, Trade)
mmbt/view.py             participant-side L2 view rebuilt from feed events
mmbt/fill_models.py      NaiveTouch, QueuePessimistic, QueueProportional
mmbt/strategy.py         market-maker decision logic (targets from view + inventory)
mmbt/sim.py              discrete-event simulation, background flow, latency, results
run_experiment.py        multi-seed comparison, writes results/
tests/test_engine.py     engine unit tests, fuzz tests, accounting identities
tests/test_mmbt.py       hand-computed fill-model scenarios, cancel attribution, sim checks
results/                 generated tables and raw per-seed numbers
```

## Matching engine

- Prices are integer ticks and quantities are integer lots.
- Priority is best price first, then FIFO within a price level. Trades execute at the resting order's price.
- Order types: GTC limit (remainder rests), IOC limit, post-only limit (rejected if it would cross), and market (sweeps, never rests).
- `cancel(oid)` removes a resting order. `depth()` and `level_total()` give snapshots.
- Every public operation emits events to an optional listener, once per operation, in this order: `("TRADE", ts, price, qty, aggressor, maker_oid, taker_oid)` for each fill, then `("L2", ts, side, price, total)` for each level that changed. The L2 stream carries no order ids. This is what a participant would receive from a depth feed.
- `check_invariants()` asserts the book is never crossed and that level totals match their orders.

Tests cover price and time priority, partial fills, IOC and market behaviour, post-only rejection, cancels, and input validation. A randomised flow test checks invariants on 20 seeds, and an accounting test checks that every unit that rests is either still resting or was consumed as maker.

## Backtester

**Ground truth.** Background liquidity providers, informed and uninformed market orders, and our strategy all trade against one `OrderBook`. Queue position and fills are therefore exact on the exchange side.

**What the backtester is allowed to see.** The strategy gets the L2 feed, delayed by `l_md`. The fill models get the same public events, with no order ids in the trades, plus their own order submissions and cancels. They never read exchange internals.

**Latency.**
- Our orders reach the exchange after `l_in`.
- Cancels take `l_cancel`, and a cancel is always scheduled after its own order's arrival, so it cannot overtake it.
- Fill reports reach the strategy after `l_md`.

**Strategy.** Post-only quotes at half-spread around the mid of the delayed view. The quote shifts against inventory, and a side stops quoting when inventory reaches its limit. The strategy replaces a quote only when its target price changes.

**Fill models** (all run on the same order flow; none affects the strategy):

| Model | Idea |
|---|---|
| `naive_touch` | Any trade at our price fills us, up to the trade size. Ignores the queue. |
| `queue_pessimistic` | Tracks the queue ahead of us. Trades consume it first. Cancels ahead of us never move us forward. |
| `queue_proportional` | Same, but cancels ahead of us are spread pro-rata over the queue (probability-queue model). |

Queue ahead is initialised from the level total when our order rests. Trades consume the queue ahead before reaching us. A level's net change minus its trades gives adds minus cancels. Our own cancels are subtracted out first, and removing our own order also moves any later order of ours forward.

**Metrics.** Predicted vs true filled lots, fill error, mean absolute error per order, over- and under-filled lots, and PnL error, all marked to the final mid.

## Results (20 seeds, 100k background steps each)

Full tables are in `results/results.md`. Headline numbers, mean ± std across seeds:

| Scenario | Model | Fill error | MAE per order (lots) | Over-filled lots |
|---|---|---|---|---|
| baseline | naive_touch | +48.6% ± 2.1 | 0.243 | 2684 |
| baseline | queue_pessimistic | −0.6% ± 0.1 | 0.003 | 0 |
| baseline | queue_proportional | −0.1% ± 0.2 | 0.004 | 22 |

Ground-truth strategy PnL is 4648 ± 264 ticks-lots with informed flow and 5044 ± 339 without. That difference is 395, with Welch t ≈ 4.1. It is the adverse-selection cost of informed flow in this market.

## Limitations (read before quoting any number)

- **The market is synthetic.** Background flow is a toy: a random-walk fundamental, random placement, and random cancels. The results describe this market and not a real venue. No real market data was used.
- **The queue models look nearly exact here, and that is a property of the setup.** The feed has exact level totals and FIFO trades, so queue position can be recovered almost completely. The residual error comes only from attributing cancels. Real feeds are often less informative (aggregated, sampled, or with anonymised trades), and cancel behaviour near the touch can be strategic. Expect the real-world error of the queue models to be larger than shown here.
- **Shadow models are fed synchronously.** `l_md` affects only the strategy. It does not affect the fill-model comparison.
- **Strategy inventory comes from delayed execution reports,** but PnL is computed from ground-truth fills.
- **Post-only only.** Strategy orders never take liquidity. Marketable-order fill modelling is not implemented.
- **One instrument, integer ticks, Python.** This is an analysis tool, not a production engine. The engine does not do a C++ or lock-free implementation.

## Possible extensions

- Feed a real recorded L2 or MBO stream (for example, CME data from the order-book project) into the fill models in place of the synthetic flow.
- Add a crossing (taker) path to the fill models.
- Add a microstructure signal (order-book imbalance) to the quote logic and measure its effect on adverse selection.
- Replace the level-based cancel attribution with a learned estimate.
