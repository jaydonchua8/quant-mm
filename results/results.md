# Fill-model results

### Scenario: baseline (20 seeds, mean ± std)

Ground truth: 5532 ± 174 lots filled, fill rate 0.277 ± 0.006 of orders, strategy PnL 4648 ± 264 ticks-lots.

| Fill model | Predicted lots | Fill error | MAE per order (lots) | Over-filled lots | Under-filled lots | PnL error |
|---|---|---|---|---|---|---|
| naive_touch | 8216 ± 191 | 48.6% ± 2.1 | 0.243 ± 0.007 | 2684 ± 79 | 0 ± 0 | 1803 ± 6304 |
| queue_pessimistic | 5500 ± 171 | -0.6% ± 0.1 | 0.003 ± 0.001 | 0 ± 0 | 32 ± 6 | -10 ± 350 |
| queue_proportional | 5528 ± 173 | -0.1% ± 0.2 | 0.004 ± 0.001 | 22 ± 7 | 27 ± 6 | 176 ± 561 |

### Scenario: no_informed_flow (20 seeds, mean ± std)

Ground truth: 5567 ± 164 lots filled, fill rate 0.279 ± 0.006 of orders, strategy PnL 5044 ± 339 ticks-lots.

| Fill model | Predicted lots | Fill error | MAE per order (lots) | Over-filled lots | Under-filled lots | PnL error |
|---|---|---|---|---|---|---|
| naive_touch | 8255 ± 171 | 48.3% ± 2.1 | 0.244 ± 0.007 | 2689 ± 75 | 0 ± 0 | 3979 ± 6148 |
| queue_pessimistic | 5529 ± 165 | -0.7% ± 0.1 | 0.003 ± 0.001 | 0 ± 0 | 37 ± 6 | 56 ± 630 |
| queue_proportional | 5558 ± 164 | -0.2% ± 0.1 | 0.005 ± 0.001 | 22 ± 5 | 31 ± 5 | 175 ± 756 |

