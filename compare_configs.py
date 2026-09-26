#!/usr/bin/env python3
"""
compare_configs.py  --  Week 10, file 1: single-path vs stock MPTCP vs controller MPTCP.

Combines a Week 6 sweep (--stock, run_sweep.py output: single-path + stock-MPTCP arms) and its
Week 9 mirror (--controller, run_sweep_ctl.py output: controller-MPTCP arm, paired cell/rep/seed)
into one per-cell table and answers the project's closing question: does subflow-level control,
with no kernel changes, recover the goodput lost in the harmful region -- without hurting the
cells where stock MPTCP already wins?

Per grid cell (gate-confirmed reps only, mean +/- 1.96*s/sqrt(n)):
    single_mean, stock_mean, ctl_mean         goodput, each arm
    recovery = (ctl_mean - stock_mean) / (single_mean - stock_mean)   only where stock < single
               0 = no help, 1 = fully closes the gap to single-path, >1 = overshoots it,
               <0 = controller made it worse
    ctl_verdict: RECOVERED (ctl interval overlaps or exceeds single), PARTIAL (better than
               stock, still below single), NONE (no better than stock), REGRESSED (ctl worse
               than stock in a cell that was NOT harmful -- a false-positive cost)
Detection numbers (median time-to-first-demote, PM errors) come straight from the meta.json
fields run_sweep_ctl.py already wrote per rep; this script does not touch controller logs.

Outputs (in --out, default <controller_dir>/comparison):
    comparison_cells.csv, comparison_report.txt, recovery_heatmap.png,
    three_way_bars.png (anchor + worst grid cells)

Usage:  python3 compare_configs.py --stock results/sweep_A --controller results/sweep_A_ctl
"""
import argparse
import csv
import json
import math
import statistics as st
import sys
from pathlib import Path

import parse_sweep as ps


# ---------------------------------------------------------------- loading --
def mean_ci(vals):
    n = len(vals)
    if n == 0:
        return (None, None, 0)
    m = st.mean(vals)
    return (m, 1.96 * st.stdev(vals) / math.sqrt(n), n) if n >= 2 else (m, None, n)


def overlap(a, b):
    if a[1] is None or b[1] is None:
        return None
    return not (a[0] + a[1] < b[0] - b[1] or b[0] + b[1] < a[0] - a[1])


def load_controller_reps(root):
    """Per grid/anchor cell: [{ctl_mbps, gate, first_demote_t, pm_errors}, ...], gate-confirmed only."""
    by_cell = {}
    for mp in sorted(Path(root, "cells").glob("*/rep*/meta.json")):
        try:
            m = json.loads(mp.read_text())
        except ValueError:
            continue
        if not m.get("complete") or not m.get("dual_subflow_confirmed"):
            continue
        by_cell.setdefault(m["cell_id"], []).append({
            "ctl_mbps": m["mptcp_mbps"], "first_demote_t": m.get("ctl_first_demote_t"),
            "pm_errors": m.get("ctl_pm_errors", 0), "n_actions": len(m.get("ctl_actions", []))})
    return by_cell


def f(x, nd=2):
    return "n/a" if x is None else f"{x:.{nd}f}"


# ------------------------------------------------------------------ merge --
def build_table(stock_cells, ctl_reps):
    rows = []
    for c in stock_cells:
        cr = ctl_reps.get(c["cell_id"], [])
        ctl = mean_ci([r["ctl_mbps"] for r in cr])
        single = (c["single_mean"], c["single_ci95"])
        stock = (c["mptcp_mean"], c["mptcp_ci95"])
        gap = None if single[0] is None or stock[0] is None else single[0] - stock[0]
        recovery = None
        if gap is not None and abs(gap) > 1e-9 and ctl[0] is not None and stock[0] is not None:
            recovery = (ctl[0] - stock[0]) / gap

        was_harmful = c["verdict"] == "HARMFUL"
        ctl_ci = (ctl[0], ctl[1])
        if ctl[0] is None:
            ctl_verdict = "NO DATA"
        elif was_harmful:
            ov_single = overlap(ctl_ci, single)
            better_than_stock = ctl[1] is not None and stock[1] is not None and ctl[0] - ctl[1] > stock[0] + stock[1]
            if ov_single or (ctl[1] is not None and single[1] is not None and ctl[0] - ctl[1] > single[0] - single[1]):
                ctl_verdict = "RECOVERED"
            elif better_than_stock:
                ctl_verdict = "PARTIAL"
            else:
                ctl_verdict = "NONE"
        else:
            worse = ctl[1] is not None and stock[1] is not None and ctl[0] + ctl[1] < stock[0] - stock[1]
            ctl_verdict = "REGRESSED" if worse else "OK"

        td = [r["first_demote_t"] for r in cr if r["first_demote_t"] is not None]
        errs = sum(r["pm_errors"] for r in cr)
        rows.append({
            "cell_id": c["cell_id"], "kind": c["kind"], "rtt_ratio": c["rtt_ratio"], "bw_ratio": c["bw_ratio"],
            "added_loss_pct": c["added_loss_pct"], "buffer": c["buffer"], "stock_verdict": c["verdict"],
            "n_single": c["n_complete"], "n_stock_gate": c["n_gate_ok"], "n_ctl_gate": ctl[2],
            "single_mean": single[0], "single_ci95": single[1],
            "stock_mean": stock[0], "stock_ci95": stock[1],
            "ctl_mean": ctl[0], "ctl_ci95": ctl[1],
            "recovery": recovery, "ctl_verdict": ctl_verdict,
            "ctl_median_first_demote_s": st.median(td) if td else None,
            "ctl_pm_errors": errs,
        })
    rows.sort(key=lambda r: (r["kind"] != "grid", r["buffer"], r["added_loss_pct"], r["rtt_ratio"], r["bw_ratio"]))
    return rows


# ---------------------------------------------------------------- outputs --
def write_csv(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in rows)


def heatmap(rows, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import TwoSlopeNorm
        from matplotlib.patches import Rectangle
    except ImportError:
        print("matplotlib not installed -> heatmap skipped")
        return False
    grid = [r for r in rows if r["kind"] == "grid" and r["recovery"] is not None]
    if not grid:
        print("no grid cells with a recovery value -> heatmap skipped")
        return False
    rtts = sorted({r["rtt_ratio"] for r in grid})
    bws = sorted({r["bw_ratio"] for r in grid})
    losses = sorted({r["added_loss_pct"] for r in grid})
    bufs = [b for b in ("small", "default") if any(r["buffer"] == b for r in grid)]
    norm = TwoSlopeNorm(vcenter=0.0, vmin=min(-0.5, min(r["recovery"] for r in grid)),
                        vmax=max(1.5, max(r["recovery"] for r in grid)))
    fig, axes = plt.subplots(len(bufs), len(losses), figsize=(4.4 * len(losses), 4.0 * len(bufs)),
                             squeeze=False, constrained_layout=True)
    im = None
    for i, buf in enumerate(bufs):
        for j, loss in enumerate(losses):
            ax = axes[i][j]
            cell = {(r["rtt_ratio"], r["bw_ratio"]): r for r in grid if r["buffer"] == buf and r["added_loss_pct"] == loss}
            data = [[cell[(rt, bw)]["recovery"] if (rt, bw) in cell else float("nan") for bw in bws] for rt in rtts]
            im = ax.imshow(data, cmap="RdYlGn", norm=norm, origin="lower", aspect="auto")
            for a, rt in enumerate(rtts):
                for b, bw in enumerate(bws):
                    r = cell.get((rt, bw))
                    if not r:
                        continue
                    label = f"{r['recovery']:.2f}" if r["stock_verdict"] == "HARMFUL" else "-"
                    ax.text(b, a, label, ha="center", va="center", fontsize=9)
                    if r["ctl_verdict"] == "REGRESSED":
                        ax.add_patch(Rectangle((b - .5, a - .5), 1, 1, fill=False, lw=2.5, ec="black"))
            ax.set_xticks(range(len(bws)), [f"x{b}" for b in bws])
            ax.set_yticks(range(len(rtts)), [f"x{r}" for r in rtts])
            ax.set_xlabel("BW ratio"); ax.set_ylabel("RTT ratio")
            ax.set_title(f"buffer={buf}, added loss {loss:g}%", fontsize=10)
    fig.colorbar(im, ax=axes.ravel().tolist(), label="recovery (0=no help, 1=closes gap to single-path)")
    fig.suptitle("Controller recovery in harmful cells (black box = regression in a non-harmful cell)", fontsize=10)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    return True


def bars(rows, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return False
    anchors = [r for r in rows if r["kind"] == "anchor"]
    worst = sorted([r for r in rows if r["kind"] == "grid" and r["stock_mean"] is not None],
                   key=lambda r: r["stock_mean"] / r["single_mean"] if r["single_mean"] else 1)[:6]
    picks = anchors + worst
    if not picks:
        return False
    x = np.arange(len(picks)); w = 0.25
    fig, ax = plt.subplots(figsize=(max(7, 1.3 * len(picks)), 4.5))
    for off, key, err, label, color in ((-w, "single_mean", "single_ci95", "single-path", "tab:gray"),
                                        (0, "stock_mean", "stock_ci95", "stock MPTCP", "tab:blue"),
                                        (w, "ctl_mean", "ctl_ci95", "controller MPTCP", "tab:green")):
        vals = [r[key] or 0 for r in picks]
        errs = [r[err] or 0 for r in picks]
        ax.bar(x + off, vals, w, yerr=errs, capsize=3, label=label, color=color)
    ax.set_xticks(x, [r["cell_id"].replace("_buf-", "\nbuf-") for r in picks], rotation=0, fontsize=8)
    ax.set_ylabel("goodput (Mbps)"); ax.legend(); ax.set_title("Anchors + worst grid cells: three-way comparison")
    fig.tight_layout(); fig.savefig(path, dpi=150)
    return True


# ------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True)
    ap.add_argument("--controller", required=True)
    ap.add_argument("--out")
    ap.add_argument("--min-n", type=int)
    a = ap.parse_args()
    stock_dir, ctl_dir = Path(a.stock), Path(a.controller)
    out = Path(a.out) if a.out else ctl_dir / "comparison"
    out.mkdir(parents=True, exist_ok=True)

    runs, _ = ps.load_runs(stock_dir)
    if not runs:
        print(f"no complete reps under {stock_dir}/cells")
        return 1
    try:
        expected = json.loads((stock_dir / "sweep_run.json").read_text())["args"]["reps"]
    except (OSError, KeyError, ValueError):
        expected = 10
    stock_cells = ps.summarize(runs, a.min_n or max(2, math.ceil(0.7 * expected)))
    ctl_reps = load_controller_reps(ctl_dir)
    if not ctl_reps:
        print(f"no complete gate-confirmed reps under {ctl_dir}/cells (run run_sweep_ctl.py first)")
        return 1

    rows = build_table(stock_cells, ctl_reps)
    write_csv(out / "comparison_cells.csv", rows)

    out_lines = []
    P = out_lines.append
    grid = [r for r in rows if r["kind"] == "grid"]
    harmful = [r for r in grid if r["stock_verdict"] == "HARMFUL"]
    no_data = [r for r in harmful if r["ctl_verdict"] == "NO DATA"]
    scored = [r for r in harmful if r["recovery"] is not None]
    P(f"stock sweep: {stock_dir}   controller sweep: {ctl_dir}")
    P(f"grid cells: {len(grid)}   harmful under stock: {len(harmful)}   "
      f"missing controller data for: {len(no_data)}")
    if scored:
        recs = [r["recovery"] for r in scored]
        P(f"\nrecovery in harmful cells (n={len(scored)}): mean {f(st.mean(recs))}, "
          f"median {f(st.median(recs))}, min {f(min(recs))}, max {f(max(recs))}")
    for v in ("RECOVERED", "PARTIAL", "NONE"):
        cells = [r["cell_id"] for r in harmful if r["ctl_verdict"] == v]
        P(f"  {v:10s} {len(cells)} cell(s)" + (f": {', '.join(cells[:6])}" + ("..." if len(cells) > 6 else "") if cells else ""))

    non_harmful = [r for r in grid if r["stock_verdict"] != "HARMFUL" and r["ctl_mean"] is not None]
    regressed = [r for r in non_harmful if r["ctl_verdict"] == "REGRESSED"]
    P(f"\nnon-harmful grid cells with controller data: {len(non_harmful)}   regressed: {len(regressed)}"
      + (f" ({', '.join(r['cell_id'] for r in regressed)})" if regressed else ""))

    P("\nanchor cells (Week 4 harmful point):")
    for r in rows:
        if r["kind"] == "anchor":
            P(f"  {r['cell_id']:32s} single {f(r['single_mean'])}+/-{f(r['single_ci95'])}  "
              f"stock {f(r['stock_mean'])}+/-{f(r['stock_ci95'])}  "
              f"controller {f(r['ctl_mean'])}+/-{f(r['ctl_ci95'])}  "
              f"recovery={f(r['recovery'])}  -> {r['ctl_verdict']}")

    td = [r["ctl_median_first_demote_s"] for r in harmful if r["ctl_median_first_demote_s"] is not None]
    errs = sum(r["ctl_pm_errors"] for r in rows)
    P(f"\ndetection: median time-to-first-demote across harmful cells = {f(st.median(td) if td else None)} s "
      f"(n={len(td)} cells with a demote)")
    P(f"PM netlink errors across the controller sweep: {errs}"
      + ("  -- investigate before trusting these numbers" if errs else ""))

    P("\nWEEK 10 CLAIM CHECK:")
    if scored and st.mean([r["recovery"] for r in scored]) >= 0.5 and not regressed:
        P("  subflow-level control recovers most of the harmful-region loss without regressing "
          "beneficial cells -- supports the project's central claim")
    elif scored and st.mean([r["recovery"] for r in scored]) >= 0.5:
        P(f"  recovery is substantial but {len(regressed)} non-harmful cell(s) regressed -- report "
          f"both the recovery and the regression, do not claim a clean win")
    else:
        P("  recovery is small or inconsistent -- report the AUC/correlation results from "
          "hol_diagnosis.py and replay_policy.py alongside this: the detection signal itself may "
          "be the limiting factor, not the control action")

    (out / "comparison_report.txt").write_text("\n".join(out_lines) + "\n")
    print("\n".join(out_lines))
    made = []
    if heatmap(rows, out / "recovery_heatmap.png"):
        made.append("recovery_heatmap.png")
    if bars(rows, out / "three_way_bars.png"):
        made.append("three_way_bars.png")
    print(f"\nwrote {out}/comparison_cells.csv, comparison_report.txt" + (", " + ", ".join(made) if made else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
