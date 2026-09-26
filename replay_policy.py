#!/usr/bin/env python3
"""
replay_policy.py  --  Week 7, file 3: offline validation and tuning of the controller policy.

Replays policy.Policy over the Week 6 traces (the 1 Hz client `ss -tni` snapshots that
run_sweep.py stored per rep: mptcp_ss_client_tcp.log) and answers the Week 7 validation
question: "would the specified thresholds demote the degraded path in the harmful cells,
without oscillating -- and without demoting it where MPTCP is beneficial?"

Ground truth comes from parse_sweep's per-cell verdicts (non-overlapping 95% CIs):
    HARMFUL cell    -> policy SHOULD  mark the cellular subflow backup   (true positive)
    BENEFICIAL cell -> policy should NOT touch it                         (false positive if it does)
    NS cells        -> neutral (reported, not scored)
Only gate-confirmed reps are used. Objective J = TPR - 2*FPR, and any flap (MARK_BACKUP <->
RESTORE reversal closer than min_interval_s) disqualifies a parameter set.

Traces are stock-MPTCP runs, so this measures DETECTION (would it fire, how fast, on which
cells); the closed-loop benefit is measured live in Weeks 8-9.

Outputs (in the results dir): controller_params.json (best parameters, load with
Params.from_dict), replay_report.txt, replay_reps.csv (per-rep decisions for the best set).

Usage:  python3 replay_policy.py <results_dir> [--fast] [--min-tpr 0.8] [--max-fpr 0.1]
"""
import argparse
import csv
import itertools
import json
import math
import statistics as st
import sys
from dataclasses import asdict, replace
from pathlib import Path

import hol_diagnosis as hol
import parse_sweep as ps
from policy import MARK_BACKUP, WITHDRAW, Params, Policy, Sample, count_flaps


# ---------------------------------------------------------------- traces ----
def rep_stream(rep_dir):
    """[(t_rel, {'wifi': Sample|None, 'cell': Sample|None}), ...] from client ss -tni."""
    snaps = hol.read_snapshots(rep_dir / "mptcp_ss_client_tcp.log")
    if not snaps:
        return []
    t0, out = snaps[0][0], []
    for ts, body in snaps:
        row = {"wifi": None, "cell": None}
        for b in hol.sockets(body):
            h = b["head"]
            if len(h) < 5 or h[0] != "ESTAB" or "tcp-ulp-mptcp" not in b["text"]:
                continue
            leg = "wifi" if h[3].startswith("10.10.1.") else "cell" if h[3].startswith("10.10.2.") else None
            if not leg:
                continue
            rt, rx = hol.RTT.search(b["text"]), hol.RETRANS.search(b["text"])
            sg = hol.SEGS.search(b["text"]) or hol.SEGS_ALL.search(b["text"])
            cw = hol.CWND.search(b["text"])
            row[leg] = Sample(ts - t0, hol.num(rt), hol.num(cw, int),
                              hol.num(rx, int, 2) or 0, hol.num(sg, int))
        out.append((ts - t0, row))
    return out


def load_reps(root, min_n):
    runs, _ = ps.load_runs(root)
    cells = {c["cell_id"]: c for c in ps.summarize(runs, min_n)}
    reps, no_trace = [], 0
    for r in runs:
        if not r["dual_subflow_confirmed"]:
            continue
        stream = rep_stream(root / "cells" / r["cell_id"] / f"rep{r['rep']:02d}")
        if not any(row["cell"] for _, row in stream):
            no_trace += 1
            continue
        reps.append({"cell_id": r["cell_id"], "kind": r["kind"], "rep": r["rep"],
                     "verdict": cells[r["cell_id"]]["verdict"], "stream": stream})
    return reps, no_trace


# ---------------------------------------------------------------- replay ----
def replay(stream, params):
    pol, acts = Policy(["wifi", "cell"], params), []
    for t, row in stream:
        acts += pol.step(t, row)
    return acts


def evaluate(reps, params, detail=False):
    tp = fn = fp = tn = ns_dem = ns_n = flaps = wifi_touch = 0
    rows = []
    for r in reps:
        acts = replay(r["stream"], params)
        dem = [a for a in acts if a.kind == MARK_BACKUP and a.name == "cell"]
        demoted = bool(dem)
        flaps += count_flaps(acts, params.min_interval_s)
        wifi_touch += any(a.name == "wifi" and a.kind in (MARK_BACKUP, WITHDRAW) for a in acts)
        v = r["verdict"]
        if v == "HARMFUL":
            tp += demoted
            fn += (not demoted)
        elif v == "BENEFICIAL":
            fp += demoted
            tn += (not demoted)
        else:
            ns_n += 1
            ns_dem += demoted
        if detail:
            rows.append({"cell_id": r["cell_id"], "kind": r["kind"], "rep": r["rep"], "verdict": v,
                         "demoted": demoted, "t_demote": dem[0].t if dem else "",
                         "actions": ";".join(f"{a.kind}:{a.name}@{a.t:.0f}" for a in acts),
                         "flaps": count_flaps(acts, params.min_interval_s)})
    tpr = tp / (tp + fn) if tp + fn else None
    fpr = fp / (fp + tn) if fp + tn else None
    J = (tpr or 0) - 2 * (fpr or 0) - (10.0 * flaps / max(1, len(reps)))
    return {"tpr": tpr, "fpr": fpr, "tp": tp, "fn": fn, "fp": fp, "tn": tn, "ns_demoted": ns_dem,
            "ns_n": ns_n, "flaps": flaps, "wifi_touch": wifi_touch, "J": J, "rows": rows}


def param_grid(fast):
    base = Params()
    if fast:
        space = {"w_rtt": (0.0, 0.2), "w_retx": (1.0,), "theta": (2, 4, 6, 10), "hyst": (1.5,), "confirm_n": (3,)}
    else:
        space = {"w_rtt": (0.0, 0.1, 0.2, 0.4), "w_retx": (0.5, 1.0, 2.0),
                 "theta": (2, 3, 4, 5, 6, 8, 10, 14), "hyst": (1.0, 1.5, 2.0), "confirm_n": (2, 3)}
    keys = list(space)
    for combo in itertools.product(*(space[k] for k in keys)):
        yield replace(base, **dict(zip(keys, combo)))


def f(x, nd=2):
    return "n/a" if x is None else f"{x:.{nd}f}"


def line(p, m):
    return (f"w_rtt={p.w_rtt} w_retx={p.w_retx} theta={p.theta} hyst={p.hyst} confirm_n={p.confirm_n}"
            f"  TPR={f(m['tpr'])} FPR={f(m['fpr'])} flaps={m['flaps']} J={f(m['J'])}")


# ------------------------------------------------------------------ main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--fast", action="store_true", help="small parameter grid")
    ap.add_argument("--min-n", type=int)
    ap.add_argument("--min-tpr", type=float, default=0.8)
    ap.add_argument("--max-fpr", type=float, default=0.1)
    a = ap.parse_args()
    root = Path(a.results_dir)

    try:
        expected = json.loads((root / "sweep_run.json").read_text())["args"]["reps"]
    except (OSError, KeyError, ValueError):
        expected = 10
    reps, no_trace = load_reps(root, a.min_n or max(2, math.ceil(0.7 * expected)))
    out = []
    P = out.append
    n_h = sum(r["verdict"] == "HARMFUL" for r in reps)
    n_b = sum(r["verdict"] == "BENEFICIAL" for r in reps)
    P(f"replay set: {len(reps)} gate-confirmed reps with client subflow traces "
      f"(HARMFUL {n_h}, BENEFICIAL {n_b}, NS {len(reps) - n_h - n_b}); {no_trace} reps had no usable trace")
    if not reps:
        print("\n".join(out))
        print("!! no client `ss -tni` traces: add ('client_tcp', CLIENT_NS, ['ss','-tni']) to ARM_SS['mptcp'] "
              "in run_sweep.py, re-run the sweep, or paste one mptcp_ss_client_tcp.log if the format differs")
        return 1
    if not n_h or not n_b:
        P("!! need both HARMFUL and BENEFICIAL cells to tune thresholds; showing default-parameter behaviour only")

    default = Params()
    m0 = evaluate(reps, default)
    P("\ndefault parameters:  " + line(default, m0))

    best = (default, m0)
    if n_h and n_b:
        grid = list(param_grid(a.fast))
        print(f"searching {len(grid)} parameter sets over {len(reps)} reps ...", flush=True)
        scored = []
        for i, p in enumerate(grid):
            m = evaluate(reps, p)
            scored.append((m["J"], -p.theta, p, m))
            if i % 50 == 0:
                print(f"  {i}/{len(grid)}", flush=True)
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[0][0]
        # many sets tie on J: take the median of the tied set (by theta) for robustness to noise
        tied = sorted((x for x in scored if x[0] >= top - 1e-9),
                      key=lambda x: (x[2].theta, x[2].w_rtt, x[2].w_retx, x[2].hyst, x[2].confirm_n))
        best = (tied[len(tied) // 2][2], tied[len(tied) // 2][3])
        P(f"{len(tied)} parameter sets tie for the best J; median of the tied set chosen")
        P("\ntop 5 parameter sets (J = TPR - 2*FPR, flaps penalised):")
        for _, _, p, m in scored[:5]:
            P("  " + line(p, m))

    bp, bm = best
    bm = evaluate(reps, bp, detail=True)
    P("\nchosen parameters: " + line(bp, bm))
    P(f"  harmful reps demoted (cell -> backup): {bm['tp']}/{bm['tp'] + bm['fn']}")
    P(f"  beneficial reps wrongly demoted:       {bm['fp']}/{bm['fp'] + bm['tn']}")
    P(f"  NS reps demoted (neutral):             {bm['ns_demoted']}/{bm['ns_n']}")
    P(f"  flaps (reversal < {bp.min_interval_s:.0f}s): {bm['flaps']}    Wi-Fi demoted/withdrawn in {bm['wifi_touch']} reps")
    td = [r["t_demote"] for r in bm["rows"] if r["verdict"] == "HARMFUL" and r["t_demote"] != ""]
    if td:
        P(f"  time to demote in harmful reps: median {st.median(td):.1f}s, p90 {sorted(td)[int(0.9 * (len(td) - 1))]:.1f}s")

    by_cell = {}
    for r in bm["rows"]:
        by_cell.setdefault((r["cell_id"], r["verdict"]), []).append(r["demoted"])
    missed = [(c, sum(d) / len(d)) for (c, v), d in by_cell.items() if v == "HARMFUL" and sum(d) / len(d) < 0.5]
    falsep = [(c, sum(d) / len(d)) for (c, v), d in by_cell.items() if v == "BENEFICIAL" and sum(d) > 0]
    P(f"  harmful cells mostly NOT demoted ({len(missed)}): " + ", ".join(f"{c} ({r:.0%})" for c, r in missed[:6]))
    P(f"  beneficial cells with false demotions ({len(falsep)}): " + ", ".join(f"{c} ({r:.0%})" for c, r in falsep[:6]))
    anchors = [(c, sum(d) / len(d)) for (c, v), d in by_cell.items() if c.startswith("anchor")]
    for c, r in anchors:
        P(f"  anchor {c}: demoted in {r:.0%} of reps")

    ok = (bm["tpr"] is not None and bm["tpr"] >= a.min_tpr and (bm["fpr"] or 0) <= a.max_fpr and bm["flaps"] == 0)
    P(f"\nWEEK 7 VALIDATION (TPR >= {a.min_tpr}, FPR <= {a.max_fpr}, no flaps): {'PASS' if ok else 'FAIL'}")
    if not ok:
        P("  -> if TPR is low, harmful cells are not separable by RTT/retransmits alone (see AUCs in "
          "hol_diagnosis.txt); report that as a finding and lower the ambition of the recovery claim.")

    (root / "controller_params.json").write_text(bp.to_json())
    (root / "replay_report.txt").write_text("\n".join(out) + "\n")
    with open(root / "replay_reps.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["cell_id", "kind", "rep", "verdict", "demoted", "t_demote", "actions", "flaps"])
        w.writeheader()
        w.writerows(bm["rows"])
    print("\n".join(out))
    print(f"\nwrote {root}/controller_params.json, replay_report.txt, replay_reps.csv")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
