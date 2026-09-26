#!/usr/bin/env python3
"""
parse_sweep.py  --  Week 5, file 4 of the sweep harness (analysis).

Reads <results_dir>/cells/*/rep*/meta.json written by run_sweep.py and produces, in the
results dir:
  runs.csv                    one row per rep (all complete reps, gate flag included)
  cells.csv                   per-cell gated statistics + verdict
  harmful_region.csv          grid cells whose verdict is HARMFUL
  harmful_region_heatmap.png  goodput-ratio heatmaps (needs matplotlib; skipped if absent)

Statistics (project rule, identical to parse_baseline.py):
  mean +/- 1.96*s/sqrt(n) per arm. Two arms differ significantly only when their
  95% intervals do not overlap.
  MPTCP arm   : ONLY reps whose kernel event log confirmed SF_ESTABLISHED (gate).
  Single-path : all complete reps (it has no second subflow to confirm).
  Ratio       : MPTCP mean / single-path mean, CI by first-order error propagation.
Verdicts: HARMFUL (MPTCP interval entirely below), BENEFICIAL (entirely above),
NS (intervals overlap), INSUFFICIENT (n < 2 in either arm).

Usage:  python3 parse_sweep.py <results_dir> [--min-n N] [--no-plot]
"""
import argparse
import csv
import json
import math
import statistics as st
import sys
from pathlib import Path


# ---------------------------------------------------------------- loading --
def load_runs(root):
    runs, incomplete = [], 0
    for mp in sorted(Path(root, "cells").glob("*/rep*/meta.json")):
        try:
            m = json.loads(mp.read_text())
        except ValueError:
            incomplete += 1
            continue
        if not m.get("complete"):
            incomplete += 1
            continue
        lg = m.get("legs", {})
        runs.append({
            "cell_id": m["cell_id"], "kind": m["kind"], "rtt_ratio": m["rtt_ratio"],
            "bw_ratio": m["bw_ratio"], "added_loss_pct": m["added_loss_pct"],
            "buffer": m["buffer"], "rep": m["rep"], "attempt": m.get("attempt", 1),
            "mptcp_mbps": m["mptcp_mbps"], "singlepath_mbps": m["singlepath_mbps"],
            "mptcp_retransmits": m.get("mptcp_retransmits"),
            "singlepath_retransmits": m.get("singlepath_retransmits"),
            "dual_subflow_confirmed": bool(m.get("dual_subflow_confirmed")),
            "sf_established_count": m.get("sf_established_count", 0),
            "ss_subflows_max": m.get("ss_subflows_max", 0),
            "cell_delay_ms": lg.get("cell", {}).get("delay_ms"),
            "cell_loss_pct": lg.get("cell", {}).get("loss_pct"),
            "cell_rate_mbit": lg.get("cell", {}).get("rate_mbit"),
            "netem_seed_used": m.get("netem", {}).get("netem_seed_used"),
            "rmem": " ".join(map(str, m.get("receive_buffer", {}).get("tcp_rmem", ()))),
        })
    return runs, incomplete


# ------------------------------------------------------------- statistics --
def mean_ci(vals):
    """(mean, ci95 half-width or None if n<2, n)"""
    n = len(vals)
    if n == 0:
        return (None, None, 0)
    m = st.mean(vals)
    if n < 2:
        return (m, None, n)
    return (m, 1.96 * st.stdev(vals) / math.sqrt(n), n)


def ratio_ci(m, s):
    """MPTCP/single ratio with first-order (delta-method) 95% CI."""
    if m[0] is None or s[0] in (None, 0) or m[1] is None or s[1] is None or m[0] == 0:
        return (None if m[0] is None or not s[0] else m[0] / s[0], None)
    r = m[0] / s[0]
    rel = math.sqrt((m[1] / m[0]) ** 2 + (s[1] / s[0]) ** 2)
    return (r, r * rel)


def verdict(m, s):
    if m[1] is None or s[1] is None:
        return "INSUFFICIENT"
    if m[0] + m[1] < s[0] - s[1]:
        return "HARMFUL"
    if s[0] + s[1] < m[0] - m[1]:
        return "BENEFICIAL"
    return "NS"


def fmt(x, nd=2):
    return "" if x is None else f"{x:.{nd}f}"


def summarize(runs, min_n):
    by_cell = {}
    for r in runs:
        by_cell.setdefault(r["cell_id"], []).append(r)
    rows = []
    for cid, rs in by_cell.items():
        gated = [r for r in rs if r["dual_subflow_confirmed"]]
        m = mean_ci([r["mptcp_mbps"] for r in gated])
        s = mean_ci([r["singlepath_mbps"] for r in rs])
        rt, rt_ci = ratio_ci(m, s)
        rx = lambda k, xs: st.mean([x[k] for x in xs if x[k] is not None]) \
            if any(x[k] is not None for x in xs) else None
        f = rs[0]
        rows.append({
            "cell_id": cid, "kind": f["kind"], "rtt_ratio": f["rtt_ratio"], "bw_ratio": f["bw_ratio"],
            "added_loss_pct": f["added_loss_pct"], "buffer": f["buffer"],
            "n_complete": len(rs), "n_gate_ok": len(gated), "n_gate_fail": len(rs) - len(gated),
            "mptcp_mean": m[0], "mptcp_ci95": m[1], "single_mean": s[0], "single_ci95": s[1],
            "ratio": rt, "ratio_ci95": rt_ci, "verdict": verdict(m, s),
            "low_n": len(gated) < min_n,
            "mptcp_retx_mean": rx("mptcp_retransmits", gated),
            "single_retx_mean": rx("singlepath_retransmits", rs),
        })
    rows.sort(key=lambda r: (r["kind"] != "grid", r["buffer"], r["added_loss_pct"],
                             r["rtt_ratio"], r["bw_ratio"]))
    return rows


# ---------------------------------------------------------------- outputs --
def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def heatmap(rows, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import TwoSlopeNorm
        from matplotlib.patches import Rectangle
    except ImportError:
        print("matplotlib not installed -> heatmap skipped (pip install matplotlib)")
        return False
    grid = [r for r in rows if r["kind"] == "grid" and r["ratio"] is not None]
    if not grid:
        print("no grid cells with a ratio -> heatmap skipped")
        return False
    rtts = sorted({r["rtt_ratio"] for r in grid})
    bws = sorted({r["bw_ratio"] for r in grid})
    losses = sorted({r["added_loss_pct"] for r in grid})
    bufs = [b for b in ("small", "default") if any(r["buffer"] == b for r in grid)]
    ratios = [r["ratio"] for r in grid]
    lo, hi = min(0.5, min(ratios)), max(1.5, max(ratios))
    norm = TwoSlopeNorm(vcenter=1.0, vmin=lo, vmax=hi)
    fig, axes = plt.subplots(len(bufs), len(losses), figsize=(4.4 * len(losses), 4.0 * len(bufs)),
                             squeeze=False, constrained_layout=True)
    im = None
    for i, buf in enumerate(bufs):
        for j, loss in enumerate(losses):
            ax = axes[i][j]
            cell = {(r["rtt_ratio"], r["bw_ratio"]): r for r in grid
                    if r["buffer"] == buf and r["added_loss_pct"] == loss}
            data = [[cell[(rt, bw)]["ratio"] if (rt, bw) in cell else float("nan") for bw in bws]
                    for rt in rtts]
            im = ax.imshow(data, cmap="RdYlGn", norm=norm, origin="lower", aspect="auto")
            for a, rt in enumerate(rtts):
                for b, bw in enumerate(bws):
                    r = cell.get((rt, bw))
                    if not r:
                        continue
                    tag = {"HARMFUL": "H", "BENEFICIAL": "B"}.get(r["verdict"], "")
                    tag += "?" if r["low_n"] else ""
                    ax.text(b, a, f"{r['ratio']:.2f}\n{tag}", ha="center", va="center", fontsize=9)
                    if r["verdict"] == "HARMFUL":
                        ax.add_patch(Rectangle((b - .5, a - .5), 1, 1, fill=False, lw=2.5, ec="black"))
            ax.set_xticks(range(len(bws)), [f"x{b}" for b in bws])
            ax.set_yticks(range(len(rtts)), [f"x{r}" for r in rtts])
            ax.set_xlabel("BW ratio (Wi-Fi / cell)")
            ax.set_ylabel("RTT ratio (cell / Wi-Fi)")
            ax.set_title(f"buffer={buf}, added loss {loss:g}%", fontsize=10)
    fig.colorbar(im, ax=axes.ravel().tolist(), label="goodput ratio: MPTCP / single-path")
    fig.suptitle("MPTCP-harmful region (H = significantly worse, B = significantly better, "
                 "? = low n; black box = harmful)", fontsize=10)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    return True


# ------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--min-n", type=int, help="min gate-confirmed reps for a cell to be trusted")
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args()
    root = Path(a.results_dir)

    runs, incomplete = load_runs(root)
    if not runs:
        print(f"no complete reps under {root}/cells")
        return 1
    expected = None
    try:
        expected = json.loads((root / "sweep_run.json").read_text())["args"]["reps"]
    except (OSError, KeyError, ValueError):
        pass
    min_n = a.min_n or max(2, math.ceil(0.7 * (expected or 10)))
    rows = summarize(runs, min_n)

    write_csv(root / "runs.csv", runs)
    write_csv(root / "cells.csv", [{k: (fmt(v, 4) if isinstance(v, float) else v)
                                    for k, v in r.items()} for r in rows])
    harmful = [r for r in rows if r["kind"] == "grid" and r["verdict"] == "HARMFUL"]
    write_csv(root / "harmful_region.csv", [{k: (fmt(v, 4) if isinstance(v, float) else v)
                                             for k, v in r.items()} for r in harmful])

    total, gate_ok = len(runs), sum(r["dual_subflow_confirmed"] for r in runs)
    disagree = sum((r["ss_subflows_max"] >= 2) != r["dual_subflow_confirmed"] for r in runs)
    print(f"reps loaded: {total} complete, {incomplete} incomplete/failed"
          + (f", expected {expected}/cell" if expected else ""))
    print(f"dual-subflow gate (monitor log): {gate_ok}/{total} confirmed; "
          f"ss-vs-monitor disagreements: {disagree} (ss is informational only)")
    if expected:
        short = [r["cell_id"] for r in rows if r["n_complete"] < expected]
        if short:
            print(f"cells with fewer than {expected} complete reps: {len(short)} (e.g. {short[0]})")
    grid = [r for r in rows if r["kind"] == "grid"]
    cnt = {v: sum(r["verdict"] == v for r in grid) for v in ("HARMFUL", "BENEFICIAL", "NS", "INSUFFICIENT")}
    print(f"grid cells: {len(grid)}  " + "  ".join(f"{k}={v}" for k, v in cnt.items())
          + f"  low-n(<{min_n} gated)={sum(r['low_n'] for r in grid)}")

    print("\nworst 15 grid cells by goodput ratio (MPTCP/single):")
    print(f"  {'cell_id':38s} {'n(gate/all)':>11s} {'MPTCP':>14s} {'single':>14s} {'ratio':>6s}  verdict")
    for r in sorted([g for g in grid if g["ratio"] is not None], key=lambda g: g["ratio"])[:15]:
        print(f"  {r['cell_id']:38s} {r['n_gate_ok']:>5d}/{r['n_complete']:<5d} "
              f"{fmt(r['mptcp_mean'])}+/-{fmt(r['mptcp_ci95']):>5s} "
              f"{fmt(r['single_mean'])}+/-{fmt(r['single_ci95']):>5s} {fmt(r['ratio']):>6s}  "
              f"{r['verdict']}{' (low n)' if r['low_n'] else ''}")

    print("\nharness self-check (Week 4 point, expect MPTCP significantly worse ~13 vs ~17 Mbps):")
    for r in rows:
        if r["kind"] == "anchor":
            ok = "PASS" if r["verdict"] == "HARMFUL" else "CHECK"
            print(f"  {r['cell_id']:32s} MPTCP {fmt(r['mptcp_mean'])}+/-{fmt(r['mptcp_ci95'])} "
                  f"vs {fmt(r['single_mean'])}+/-{fmt(r['single_ci95'])} -> {r['verdict']}"
                  f"{'  [' + ok + ']' if r['buffer'] == 'default' else ''}")

    if not a.no_plot and heatmap(rows, root / "harmful_region_heatmap.png"):
        print(f"\nwrote {root / 'harmful_region_heatmap.png'}")
    print(f"wrote {root / 'runs.csv'}, cells.csv, harmful_region.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
