"""Run the fill-model comparison across seeds and write results/.

    python3 run_experiment.py            # full run (about 2 minutes)
    python3 run_experiment.py --seeds 3  # quick run
"""

from __future__ import annotations

import argparse
import json
import os
import statistics as st
import time

from mmbt.sim import SimConfig, Simulation

SCENARIOS = {
    "baseline": {},
    "no_informed_flow": {"p_inform": 0.0},
}
MODELS = ("naive_touch", "queue_pessimistic", "queue_proportional")


def run_scenario(name: str, overrides: dict, seeds: int, n_steps: int) -> list:
    rows = []
    for seed in range(seeds):
        cfg = SimConfig(seed=seed, n_steps=n_steps, **overrides)
        rows.append(Simulation(cfg).run())
    return rows


def mean_std(xs):
    xs = list(xs)
    return (st.mean(xs), st.stdev(xs) if len(xs) > 1 else 0.0)


def summarise(rows: list) -> dict:
    out = {"truth": {}, "models": {}}
    tq = [r["truth"]["filled_qty"] for r in rows]
    out["truth"]["filled_qty"] = mean_std(tq)
    out["truth"]["fill_rate_per_order"] = mean_std(r["truth"]["fill_rate_per_order"] for r in rows)
    out["truth"]["pnl"] = mean_std(r["truth"]["pnl"] for r in rows)
    out["truth"]["orders_placed"] = mean_std(r["orders_placed"] for r in rows)
    for m in MODELS:
        out["models"][m] = {
            "filled_qty": mean_std(r["models"][m]["filled_qty"] for r in rows),
            "fill_error_pct": mean_std(r["models"][m]["fill_error_pct"] for r in rows),
            "mae_qty_per_order": mean_std(r["models"][m]["mae_qty_per_order"] for r in rows),
            "overfill_qty": mean_std(r["models"][m]["overfill_qty"] for r in rows),
            "underfill_qty": mean_std(r["models"][m]["underfill_qty"] for r in rows),
            "fill_rate_per_order": mean_std(r["models"][m]["fill_rate_per_order"] for r in rows),
            "pnl_error": mean_std(r["models"][m]["pnl_error"] for r in rows),
        }
    return out


def fmt(ms, digits=1, pct=False):
    mean, sd = ms
    suffix = "%" if pct else ""
    return f"{mean:.{digits}f}{suffix} ± {sd:.{digits}f}"


def markdown(name: str, s: dict, n: int) -> str:
    t = s["truth"]
    lines = [
        f"### Scenario: {name} ({n} seeds, mean ± std)",
        "",
        f"Ground truth: {fmt(t['filled_qty'], 0)} lots filled, "
        f"fill rate {fmt(t['fill_rate_per_order'], 3)} of orders, "
        f"strategy PnL {fmt(t['pnl'], 0)} ticks-lots.",
        "",
        "| Fill model | Predicted lots | Fill error | MAE per order (lots) | Over-filled lots | Under-filled lots | PnL error |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in MODELS:
        x = s["models"][m]
        lines.append(
            f"| {m} | {fmt(x['filled_qty'], 0)} | {fmt(x['fill_error_pct'], 1, pct=True)} | "
            f"{fmt(x['mae_qty_per_order'], 3)} | {fmt(x['overfill_qty'], 0)} | "
            f"{fmt(x['underfill_qty'], 0)} | {fmt(x['pnl_error'], 0)} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--steps", type=int, default=100_000)
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    all_summary, md_parts = {}, ["# Fill-model results", ""]
    t0 = time.time()
    raw = {}
    for name, overrides in SCENARIOS.items():
        rows = run_scenario(name, overrides, args.seeds, args.steps)
        raw[name] = rows
        s = summarise(rows)
        all_summary[name] = s
        md_parts.append(markdown(name, s, args.seeds))
        print(f"{name}: done ({len(rows)} seeds, {time.time() - t0:.0f}s elapsed)", flush=True)

    with open(os.path.join(args.out, "results.json"), "w") as f:
        json.dump({"config": {"seeds": args.seeds, "n_steps": args.steps},
                   "summary": all_summary, "per_seed": raw}, f, indent=1, default=float)
    with open(os.path.join(args.out, "results.md"), "w") as f:
        f.write("\n".join(md_parts) + "\n")
    print("\n".join(md_parts))


if __name__ == "__main__":
    main()
