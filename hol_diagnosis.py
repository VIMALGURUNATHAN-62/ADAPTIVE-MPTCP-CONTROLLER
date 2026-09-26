#!/usr/bin/env python3
"""
hol_diagnosis.py  --  Week 7, file 1 (mechanistic diagnosis of the harmful region).

Reads a run_sweep.py results directory (uses parse_sweep.py for loading, gating and the
per-cell verdicts) and the per-rep ss telemetry, and answers three questions:
  1. Are harmful cells receive-buffer (head-of-line) limited?
       server-side MPTCP socket skmem: occupancy = r / rb   (mptcp_ss_server.log, ss -Mmi)
  2. Or is the slow path lossy / collapsing?
       client subflow sockets: srtt, retransmit rate per path  (mptcp_ss_client_tcp.log, ss -tni)
  3. Which telemetry signal actually separates harmful from beneficial runs (AUC)?
       -> these are the candidate Sense inputs for the controller specification.

Outputs (in the results dir): hol_reps.csv, hol_cells.csv, hol_diagnosis.txt.
Field coverage is printed first: if a field parses in 0 snapshots, the ss text format on
your kernel differs from the regexes below -- paste one <arm>_ss_*.log and it is fixed.

Requires run_sweep.py's mptcp arm to poll client `ss -tni` (ARM_SS entry "client_tcp");
without that log only the buffer-occupancy analysis runs.

Usage:  python3 hol_diagnosis.py <results_dir> [--min-n N]
"""
import argparse
import csv
import json
import math
import re
import statistics as st
import sys
from pathlib import Path

import parse_sweep as ps

DATA_PORT = ":5201"
OCC_HOL = 0.40     # mean buffer occupancy at/above which a harmful cell is called HoL-limited
RETX_LOSS = 0.01   # slow-path retransmit rate at/above which it is called loss-limited

SNAP = re.compile(r"^---- ([\d.]+) ----$", re.M)
SKMEM = re.compile(r"skmem:\(r(\d+),rb(\d+)")
RTT = re.compile(r"\brtt:([\d.]+)/([\d.]+)")
RETRANS = re.compile(r"\bretrans:(\d+)/(\d+)")
SEGS = re.compile(r"\bdata_segs_out:(\d+)")
SEGS_ALL = re.compile(r"\bsegs_out:(\d+)")
CWND = re.compile(r"\bcwnd:(\d+)")


# ---------------------------------------------------------------- parsing --
def read_snapshots(path):
    if not path.exists():
        return []
    parts = SNAP.split(path.read_text(errors="replace"))
    return [(float(parts[i]), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def sockets(body):
    """Split one ss snapshot into per-socket dicts {head: [...], text: 'joined lines'}."""
    blocks, cur = [], None
    for line in body.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace() and not line.startswith("State"):
            cur = {"head": line.split(), "text": line}
            blocks.append(cur)
        elif cur is not None:
            cur["text"] += " " + line.strip()
    return blocks


def occupancy_series(path):
    out = []
    for ts, body in read_snapshots(path):
        for b in sockets(body):
            h = b["head"]
            if len(h) < 5 or h[0] != "ESTAB" or not h[3].endswith(DATA_PORT):
                continue
            m = SKMEM.search(b["text"])
            if m and int(m.group(2)) > 0:
                r, rb = int(m.group(1)), int(m.group(2))
                out.append({"t": ts, "r": r, "rb": rb, "occ": r / rb})
    return out


def subflow_series(path):
    """{'wifi': [...], 'cell': [...]} from client `ss -tni` (subflow TCP sockets only)."""
    series = {"wifi": [], "cell": []}
    for ts, body in read_snapshots(path):
        for b in sockets(body):
            h = b["head"]
            if len(h) < 5 or h[0] != "ESTAB" or "tcp-ulp-mptcp" not in b["text"]:
                continue
            leg = "wifi" if h[3].startswith("10.10.1.") else "cell" if h[3].startswith("10.10.2.") else None
            if not leg:
                continue
            rt, rx = RTT.search(b["text"]), RETRANS.search(b["text"])
            sg = SEGS.search(b["text"]) or SEGS_ALL.search(b["text"])
            cw = CWND.search(b["text"])
            series[leg].append({"t": ts,
                                "rtt": num(rt),
                                "cwnd": num(cw, int),
                                "retx": num(rx, int, 2) or 0,
                                "segs": num(sg, int)})
    return series


def num(m, cast=float, grp=1):
    """Regex match -> number, or None if absent/garbled (never crash on odd ss text)."""
    try:
        return cast(m.group(grp)) if m else None
    except ValueError:
        return None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def retx_rate(pts):
    pts = [p for p in pts if p["segs"] is not None]
    if len(pts) < 2:
        return None
    d_seg = pts[-1]["segs"] - pts[0]["segs"]
    return (pts[-1]["retx"] - pts[0]["retx"]) / d_seg if d_seg > 0 else None


def rep_features(rep_dir):
    occ = occupancy_series(rep_dir / "mptcp_ss_server.log")
    sf = subflow_series(rep_dir / "mptcp_ss_client_tcp.log")
    o = sorted(x["occ"] for x in occ)
    f = {
        "occ_mean": mean(o), "occ_max": o[-1] if o else None,
        "occ_p95": o[min(len(o) - 1, int(0.95 * len(o)))] if o else None,
        "occ_hi_frac": (sum(x >= 0.5 for x in o) / len(o)) if o else None,
        "rb_max": max((x["rb"] for x in occ), default=None),
        "n_occ": len(occ), "n_sf_wifi": len(sf["wifi"]), "n_sf_cell": len(sf["cell"]),
    }
    # skip the first sample of each path (slow-start / handshake artefacts)
    w, c = sf["wifi"][1:], sf["cell"][1:]
    f["wifi_rtt_ms"], f["cell_rtt_ms"] = mean([p["rtt"] for p in w]), mean([p["rtt"] for p in c])
    f["rtt_inflation"] = (f["cell_rtt_ms"] / f["wifi_rtt_ms"]) if f["wifi_rtt_ms"] and f["cell_rtt_ms"] else None
    f["wifi_retx_rate"], f["cell_retx_rate"] = retx_rate(sf["wifi"]), retx_rate(sf["cell"])
    f["cell_cwnd_mean"] = mean([p["cwnd"] for p in c])
    return f


# ------------------------------------------------------------------- stats --
def pearson(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pts) < 3:
        return None
    mx, my = st.mean(p[0] for p in pts), st.mean(p[1] for p in pts)
    sx = math.sqrt(sum((p[0] - mx) ** 2 for p in pts))
    sy = math.sqrt(sum((p[1] - my) ** 2 for p in pts))
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / (sx * sy) if sx and sy else None


def auc(pos, neg):
    """P(pos value > neg value) (ties count half). 0.5 = no separation."""
    pos, neg = [x for x in pos if x is not None], [x for x in neg if x is not None]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def f2(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


# -------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--min-n", type=int)
    a = ap.parse_args()
    root = Path(a.results_dir)

    runs, _ = ps.load_runs(root)
    if not runs:
        print(f"no complete reps under {root}/cells")
        return 1
    try:
        expected = json.loads((root / "sweep_run.json").read_text())["args"]["reps"]
    except (OSError, KeyError, ValueError):
        expected = 10
    cells = {c["cell_id"]: c for c in ps.summarize(runs, a.min_n or max(2, math.ceil(0.7 * expected)))}

    rep_rows = []
    for r in runs:
        d = root / "cells" / r["cell_id"] / f"rep{r['rep']:02d}"
        f = rep_features(d)
        row = {"cell_id": r["cell_id"], "kind": r["kind"], "buffer": r["buffer"], "rep": r["rep"],
               "gate": r["dual_subflow_confirmed"],
               "rep_ratio": (r["mptcp_mbps"] / r["singlepath_mbps"]) if r["singlepath_mbps"] else None,
               "verdict": cells[r["cell_id"]]["verdict"], **f}
        rep_rows.append(row)
    gated = [r for r in rep_rows if r["gate"]]

    out = []
    P = out.append
    P(f"HoL diagnosis: {len(rep_rows)} reps ({len(gated)} gate-confirmed) from {root}")
    P("field coverage (reps with >=1 parsed sample): " + ", ".join(
        f"{k}={sum(r[k] > 0 for r in rep_rows)}" for k in ("n_occ", "n_sf_wifi", "n_sf_cell")))
    if not any(r["n_occ"] for r in rep_rows):
        P("!! no buffer-occupancy samples parsed from mptcp_ss_server.log (ss -Mmi skmem format differs?)")
    if not any(r["n_sf_cell"] for r in rep_rows):
        P("!! no client subflow samples: add ('client_tcp', CLIENT_NS, ['ss','-tni']) to ARM_SS['mptcp'] "
          "in run_sweep.py and re-run, or the ss -tni format differs")

    feats = ("occ_mean", "occ_max", "occ_hi_frac", "rtt_inflation", "cell_retx_rate", "wifi_retx_rate")
    P("\nper-verdict means over gate-confirmed grid reps (harmful cells vs the rest):")
    P(f"  {'verdict':12s} {'reps':>5s} " + " ".join(f"{k:>14s}" for k in feats))
    for v in ("HARMFUL", "NS", "BENEFICIAL"):
        g = [r for r in gated if r["kind"] == "grid" and r["verdict"] == v]
        P(f"  {v:12s} {len(g):5d} " + " ".join(f"{f2(mean([r[k] for r in g])):>14s}" for k in feats))

    harm = [r for r in gated if r["kind"] == "grid" and r["verdict"] == "HARMFUL"]
    ben = [r for r in gated if r["kind"] == "grid" and r["verdict"] == "BENEFICIAL"]
    P("\ncandidate Sense signals: AUC for HARMFUL vs BENEFICIAL reps (0.5 = useless, 1.0 = perfect,"
      " <0.5 = inverted)")
    for k in feats:
        P(f"  {k:16s} AUC = {f2(auc([r[k] for r in harm], [r[k] for r in ben]), 2)}")
    P(f"  (n harmful reps = {len(harm)}, n beneficial reps = {len(ben)})")

    P("\ncorrelation with per-rep goodput ratio (MPTCP/single), gate-confirmed grid reps:")
    grid_g = [r for r in gated if r["kind"] == "grid"]
    for k in feats:
        P(f"  {k:16s} r = {f2(pearson([r[k] for r in grid_g], [r['rep_ratio'] for r in grid_g]), 2)}")

    cell_rows = []
    for cid, c in cells.items():
        rs = [r for r in gated if r["cell_id"] == cid]
        agg = {k: mean([r[k] for r in rs]) for k in feats}
        cause = ""
        if c["verdict"] == "HARMFUL":
            if agg["occ_mean"] is not None and agg["occ_mean"] >= OCC_HOL:
                cause = "HoL (receive buffer)"
            elif agg["cell_retx_rate"] is not None and agg["cell_retx_rate"] >= RETX_LOSS:
                cause = "loss on slow path"
            else:
                cause = "other/undetermined"
        cell_rows.append({"cell_id": cid, "kind": c["kind"], "buffer": c["buffer"], "verdict": c["verdict"],
                          "ratio": c["ratio"], "n_gate_ok": len(rs), **agg, "cause": cause})
    P(f"\nharmful cells by dominant cause (HoL if occ_mean >= {OCC_HOL}, loss if slow-path retx >= {RETX_LOSS:.0%}):")
    hc = [c for c in cell_rows if c["verdict"] == "HARMFUL"]
    for cause in ("HoL (receive buffer)", "loss on slow path", "other/undetermined"):
        P(f"  {cause:24s} {sum(c['cause'] == cause for c in hc)}")
    for c in sorted(hc, key=lambda c: (c["ratio"] is None, c["ratio"]))[:12]:
        P(f"    {c['cell_id']:38s} ratio {f2(c['ratio'], 2)}  occ {f2(c['occ_mean'], 2)}  "
          f"cell-retx {f2(c['cell_retx_rate'], 3)}  -> {c['cause']}")
    for b in ("small", "default"):
        n = sum(c["buffer"] == b and c["kind"] == "grid" for c in cell_rows)
        h = sum(c["buffer"] == b and c["kind"] == "grid" and c["verdict"] == "HARMFUL" for c in cell_rows)
        P(f"  harmful grid cells with buffer={b}: {h}/{n}")

    for name, rows in (("hol_reps.csv", rep_rows), ("hol_cells.csv", cell_rows)):
        with open(root / name, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows({k: (round(v, 5) if isinstance(v, float) else v) for k, v in r.items()} for r in rows)
    text = "\n".join(out)
    (root / "hol_diagnosis.txt").write_text(text + "\n")
    print(text)
    print(f"\nwrote {root}/hol_reps.csv, hol_cells.csv, hol_diagnosis.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
