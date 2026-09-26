#!/usr/bin/env python3
"""
sweep_config.py  --  Week 5, file 1 of the sweep harness.

Single source of truth for the characterization sweep: axes, level -> netem /
receive-buffer mapping, cell enumeration, deterministic seeds, run order.
Data + pure functions only. No root, no tc, no namespaces: importable anywhere.
Imported by run_sweep.py and parse_sweep.py (next files).

Axis definitions (change them HERE, nowhere else):
  RTT ratio  = RTT_cell / RTT_wifi      Wi-Fi leg is fixed at the Week 4 values
  BW ratio   = BW_wifi  / BW_cell
  added loss = extra loss on the CELLULAR leg, on top of CELL_BASE_LOSS_PCT
  buffer     = receiver-side (server ns) receive buffer:
               'small'   -> autotuning defeated, fixed size
               'default' -> kernel autotuning untouched

Usage:
  python3 sweep_config.py          # summary + sanity checks
  python3 sweep_config.py --list   # every cell in run order with netem params
"""
import random
import sys
import zlib
from dataclasses import dataclass
from itertools import product

# ---------------------------------------------------------------- axes -----
RTT_RATIOS = (1, 2, 4)
BW_RATIOS = (1, 2, 5)
ADDED_LOSS_PCT = (0, 1, 3)
BUFFERS = ("small", "default")

REPS = 10                 # per cell (plan: ten repetitions)
DURATION_S = 15           # iperf3 -t, same as Week 4
PER_REP_OVERHEAD_S = 10   # server start, monitor, sleeps; both arms combined
INCLUDE_ANCHOR = True     # add the Week 4 'asymmetric-cell-degraded' point as a harness self-check

# ------------------------------------------------- fixed / reference legs --
# Identical to the Week 4 scenarios in netem_shaping.sh. Values are PER DIRECTION:
# netem is applied on both ends of each veth pair, so RTT = 2 x delay_ms.
WIFI = {"delay_ms": 5, "jitter_ms": 1, "loss_pct": 1.0, "rate_mbit": 100}
CELL_BASE_LOSS_PCT = 0.1    # Week 4 baseline-symmetric cellular loss
CELL_JITTER_FRAC = 0.10     # cellular jitter = 10% of delay, min 1 ms

# Week 4 asymmetric-cell-degraded cellular leg (anchor cell, explicit values)
ANCHOR_CELL_LEG = {"delay_ms": 90, "jitter_ms": 15, "loss_pct": 6.0, "rate_mbit": 3}

# ----------------------------------------------------- receive-buffer axis -
# The receiver is the server namespace (iperf3 client -> server).
# 'small' pins tcp_rmem (min, default, max) in that namespace AND is meant to be
# paired with `iperf3 -s -w <bytes>` (SO_RCVBUF), which switches autotuning off
# for that socket. NOTE: the kernel doubles SO_RCVBUF requests; the runner must
# read back the effective size (ss -m) and log it, not assume it.
RECEIVER_NS = "server"
SMALL_RCVBUF_BYTES = 131072   # 128 KiB, below the Eq.(2) bound for every grid cell
BUFFER_LEVELS = {
    "small": {
        "tcp_rmem": (4096, SMALL_RCVBUF_BYTES, SMALL_RCVBUF_BYTES),
        "iperf_window_bytes": SMALL_RCVBUF_BYTES,
    },
    # None -> runner restores the namespace's original tcp_rmem and passes no -w
    "default": {"tcp_rmem": None, "iperf_window_bytes": None},
}

BASE_SEED = 20260920  # run-order shuffle + per-run netem seeds (reuse in Week 9)


# --------------------------------------------------------------- cells -----
@dataclass(frozen=True)
class Cell:
    rtt_ratio: int
    bw_ratio: int
    added_loss_pct: float
    buffer: str
    kind: str = "grid"   # "grid" | "anchor"

    @property
    def cell_id(self):
        if self.kind == "anchor":
            return f"anchor-w4degraded_buf-{self.buffer}"
        return (f"rtt{self.rtt_ratio}_bw{self.bw_ratio}"
                f"_loss{self.added_loss_pct:g}_buf-{self.buffer}")


def grid_cells():
    return [Cell(r, b, l, buf)
            for r, b, l, buf in product(RTT_RATIOS, BW_RATIOS, ADDED_LOSS_PCT, BUFFERS)]


def anchor_cells():
    # Labels are nominal (90/5 = 18x RTT, 100/3 ~ 33x BW); legs() uses explicit values.
    return [Cell(18, 33, 6, buf, "anchor") for buf in BUFFERS]


def all_cells(include_anchor=INCLUDE_ANCHOR):
    return grid_cells() + (anchor_cells() if include_anchor else [])


def run_order(cells=None, seed=BASE_SEED):
    """Deterministic shuffle at cell level (reps of a cell stay contiguous, since
    each rep is a paired MPTCP + single-path run). Avoids time-drift bias."""
    cells = list(cells if cells is not None else all_cells())
    random.Random(seed).shuffle(cells)
    return cells


# ------------------------------------------------ cell -> concrete params --
def legs(cell):
    """Return {'wifi': {...}, 'cell': {...}} with delay_ms, jitter_ms, loss_pct, rate_mbit."""
    wifi = dict(WIFI)
    if cell.kind == "anchor":
        return {"wifi": wifi, "cell": dict(ANCHOR_CELL_LEG)}
    delay = WIFI["delay_ms"] * cell.rtt_ratio
    return {
        "wifi": wifi,
        "cell": {
            "delay_ms": delay,
            "jitter_ms": max(1, round(delay * CELL_JITTER_FRAC)),
            "loss_pct": round(CELL_BASE_LOSS_PCT + cell.added_loss_pct, 3),
            "rate_mbit": max(1, round(WIFI["rate_mbit"] / cell.bw_ratio)),
        },
    }


def netem_args(leg, seed=None):
    """Argument list for: tc qdisc replace dev <if> root netem <args...>"""
    args = ["delay", f"{leg['delay_ms']}ms", f"{leg['jitter_ms']}ms",
            "distribution", "normal",
            "loss", f"{leg['loss_pct']:g}%",
            "rate", f"{leg['rate_mbit']}mbit"]
    if seed is not None:
        args += ["seed", str(seed)]
    return args


def cell_seed(cell_id, rep):
    return (BASE_SEED + zlib.crc32(f"{cell_id}:{rep}".encode())) & 0x7FFFFFFF


# ------------------------------------------- receive-buffer bound (Eq. 2) --
def rcvbuf_bound_bytes(cell):
    """B >= (C_wifi + C_cell) x RTT_max, using configured (queue-free) RTTs."""
    l = legs(cell)
    cap_bps = (l["wifi"]["rate_mbit"] + l["cell"]["rate_mbit"]) * 1e6
    rtt_max_s = 2 * max(l["wifi"]["delay_ms"], l["cell"]["delay_ms"]) / 1000.0
    return int(cap_bps * rtt_max_s / 8)


def buffer_below_bound(cell):
    """Nominal check on the requested size. 'default' autotunes up to tcp_rmem max."""
    return cell.buffer == "small" and SMALL_RCVBUF_BYTES < rcvbuf_bound_bytes(cell)


# ------------------------------------------------------------- manifest ----
def manifest(cells=None):
    rows = []
    for i, c in enumerate(run_order(cells)):
        l = legs(c)
        row = {"order": i, "cell_id": c.cell_id, "kind": c.kind,
               "rtt_ratio": c.rtt_ratio, "bw_ratio": c.bw_ratio,
               "added_loss_pct": c.added_loss_pct, "buffer": c.buffer,
               "rcvbuf_bound_bytes": rcvbuf_bound_bytes(c),
               "buffer_below_bound": buffer_below_bound(c)}
        for name in ("wifi", "cell"):
            for k, v in l[name].items():
                row[f"{name}_{k}"] = v
        rows.append(row)
    return rows


def validate():
    cells = all_cells()
    ids = [c.cell_id for c in cells]
    assert len(ids) == len(set(ids)), "duplicate cell_id"
    assert set(BUFFERS) <= set(BUFFER_LEVELS), "buffer level without a definition"
    for c in cells:
        l = legs(c)
        assert l["cell"]["rate_mbit"] >= 1 and l["cell"]["delay_ms"] >= 1
        assert 0 <= l["cell"]["loss_pct"] < 100


def _summary():
    validate()
    grid_n, cells_n = len(grid_cells()), len(all_cells())
    pairs = cells_n * REPS
    hours = pairs * (2 * DURATION_S + PER_REP_OVERHEAD_S) / 3600
    print(f"Axes: RTT ratio {RTT_RATIOS} x BW ratio {BW_RATIOS} x added loss {ADDED_LOSS_PCT}% x buffer {BUFFERS}")
    print(f"Grid cells: {grid_n}  (+{cells_n - grid_n} anchor)  = {cells_n} cells")
    print(f"Reps/cell: {REPS}  -> {pairs} paired runs = {2 * pairs} transfers "
          f"({DURATION_S}s each), est. {hours:.1f} h unattended")
    print(f"NOTE: plan text says ~324 runs; {grid_n} grid cells x {REPS} reps = {grid_n * REPS}.")
    mild = Cell(RTT_RATIOS[0], BW_RATIOS[0], ADDED_LOSS_PCT[0], "default")
    harsh = Cell(RTT_RATIOS[-1], BW_RATIOS[-1], ADDED_LOSS_PCT[-1], "default")
    print(f"Mildest grid leg (cell):  {legs(mild)['cell']}")
    print(f"Harshest grid leg (cell): {legs(harsh)['cell']}")
    a = ANCHOR_CELL_LEG
    print(f"Week 4 degraded leg:      {a}")
    print(f"  -> Week 4 point is RTT x{a['delay_ms'] / WIFI['delay_ms']:.0f}, "
          f"BW x{WIFI['rate_mbit'] / a['rate_mbit']:.0f}, loss {a['loss_pct']:g}% "
          f"vs grid max RTT x{RTT_RATIOS[-1]}, BW x{BW_RATIOS[-1]}, "
          f"loss {legs(harsh)['cell']['loss_pct']:g}%")
    below = sum(buffer_below_bound(c) for c in all_cells())
    print(f"'small'-buffer cells below the Eq.(2) bound: {below}/{sum(c.buffer == 'small' for c in all_cells())}")
    print("validate(): OK")


if __name__ == "__main__":
    if "--list" in sys.argv:
        for r in manifest():
            print(f"{r['order']:3d}  {r['cell_id']:38s} wifi[{r['wifi_delay_ms']}ms/{r['wifi_loss_pct']:g}%/"
                  f"{r['wifi_rate_mbit']}M] cell[{r['cell_delay_ms']}ms/{r['cell_loss_pct']:g}%/"
                  f"{r['cell_rate_mbit']}M] bound={r['rcvbuf_bound_bytes']}B")
    else:
        _summary()
