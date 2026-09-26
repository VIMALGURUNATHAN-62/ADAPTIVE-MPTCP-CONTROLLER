#!/usr/bin/env python3
"""
run_sweep.py  --  Week 5, file 3 of the sweep harness (orchestrator).

For every (cell, rep) in sweep_config's deterministic run order:
  shape -> verify tc read-back -> set receive buffer -> MPTCP arm -> single-path arm.
Same arm order and delays as week4_baseline.sh (MPTCP first, then plain TCP over the
Wi-Fi address). Per rep it stores raw evidence under
  <out>/cells/<cell_id>/rep<NN>/
    mptcp.json  singlepath.json          iperf3 -J output
    mptcp_monitor.log                    ip mptcp monitor (AUTHORITATIVE dual-subflow gate)
    <arm>_ss_client.log / _ss_server.log 1 Hz ss snapshots (informational; server side has skmem)
    tc_before.txt / tc_after.txt         tc -s qdisc show, all four ends (netem verified)
    meta.json                            parameters, seeds, effective buffer, goodputs, gate

Gate rule (Week 4 lesson): dual-subflow = SF_ESTABLISHED in the monitor log. The ss
count is recorded but never used as the gate. Gate failures are RECORDED, not silently
retried (retrying only those reps would bias the harmful-region result); use
--retry-gate to opt in. Infrastructure failures (iperf error, tc mismatch) are retried.

Resumable: re-run the same command with the same --out; reps with meta.complete=true
are skipped.

Usage:
  python3 run_sweep.py --plan                      # what would run, no root needed
  sudo python3 run_sweep.py --dry-run              # 7 cells x 3 reps, verify by hand first
  sudo python3 run_sweep.py --out results/sweep_A  # full sweep, unattended
  sudo python3 run_sweep.py --out results/sweep_A --only anchor   # subset by cell_id substring
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import sweep_config as cfg
import sweep_env as env

SCRIPT_DIR = Path(__file__).resolve().parent


def restore_ownership(path):
    """When run under sudo, hand the output tree back to the invoking user, so a later
    non-root command (parse_sweep.py, etc.) can read and write it. No-op outside sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid or not gid:
        return
    uid, gid = int(uid), int(gid)
    try:
        for p in [Path(path), *Path(path).rglob("*")]:
            os.chown(p, uid, gid)
    except OSError:
        pass
SETTLE_S = 1
SERVER_UP_MPTCP_S = 2     # as Week 4
SERVER_UP_TCP_S = 1
MONITOR_UP_S = 1
POLL_S = 1.0
MAX_CONSECUTIVE_FAILURES = 3

# per arm: (side label, namespace, command) polled at 1 Hz
ARM_SS = {
    "mptcp": [("client", env.CLIENT_NS, ["ss", "-Mai"]),
              ("server", env.SERVER_NS, ["ss", "-Mmi"]),
              ("server_tcp", env.SERVER_NS, ["ss", "-tmi"]),
              ("client_tcp", env.CLIENT_NS, ["ss", "-tni"])],
    "singlepath": [("client", env.CLIENT_NS, ["ss", "-tai"]),
                   ("server", env.SERVER_NS, ["ss", "-tmi"])],
}


# ------------------------------------------------------------------ utils --
class Log:
    def __init__(self, path=None):
        self.f = open(path, "a", buffering=1) if path else None

    def __call__(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        if self.f:
            self.f.write(line + "\n")


def select_cells(args):
    cells = cfg.all_cells()
    if args.dry_run:
        R, B, L = cfg.RTT_RATIOS, cfg.BW_RATIOS, cfg.ADDED_LOSS_PCT
        corner = lambda c: c.rtt_ratio in (R[0], R[-1]) and c.bw_ratio in (B[0], B[-1]) \
            and c.added_loss_pct == L[0]
        worst = lambda c: c.rtt_ratio == R[-1] and c.bw_ratio == B[-1] and c.added_loss_pct == L[0]
        cells = [c for c in cells
                 if (c.kind == "anchor")
                 or (c.buffer == "default" and corner(c))
                 or (c.buffer == "small" and worst(c))]
    if args.only:
        cells = [c for c in cells if args.only in c.cell_id]
    return cfg.run_order(cells)


def parse_iperf(text, rc):
    try:
        d = json.loads(text)
    except ValueError:
        return {"ok": False, "mbps": None, "retransmits": None, "error": f"invalid JSON (rc={rc})"}
    if "error" in d:
        return {"ok": False, "mbps": None, "retransmits": None, "error": str(d["error"])}
    end = d.get("end", {})
    recv, sent = end.get("sum_received") or {}, end.get("sum_sent") or {}
    bps = recv.get("bits_per_second") or sent.get("bits_per_second")
    if rc != 0 or not bps:
        return {"ok": False, "mbps": None, "retransmits": None, "error": f"no goodput (rc={rc})"}
    return {"ok": True, "mbps": bps / 1e6, "retransmits": sent.get("retransmits", 0), "error": None}


def read_gate(rep_dir):
    mon = rep_dir / "mptcp_monitor.log"
    n = mon.read_text().count("SF_ESTABLISHED") if mon.exists() else 0
    ss = rep_dir / "mptcp_ss_client.log"
    vals = [int(x) for x in re.findall(r"subflows:(\d+)", ss.read_text())] if ss.exists() else []
    return {"dual_subflow_confirmed": n > 0, "sf_established_count": n,
            "ss_subflows_max": max(vals) if vals else 0}


class SsPoller(threading.Thread):
    def __init__(self, arm, rep_dir):
        super().__init__(daemon=True)
        self.arm, self.rep_dir, self.stop_evt = arm, rep_dir, threading.Event()

    def run(self):
        files = {side: open(self.rep_dir / f"{self.arm}_ss_{side}.log", "w")
                 for side, _, _ in ARM_SS[self.arm]}
        try:
            while not self.stop_evt.is_set():
                t = time.time()
                for side, ns, cmd in ARM_SS[self.arm]:
                    try:
                        out = env.ns_capture(ns, cmd, timeout=5)
                    except Exception as e:  # never let telemetry kill a run
                        out = f"ERROR: {e}\n"
                    files[side].write(f"---- {t:.6f} ----\n{out}")
                self.stop_evt.wait(max(0.0, POLL_S - (time.time() - t)))
        finally:
            for f in files.values():
                f.close()


# ------------------------------------------------------------- one arm/rep -
def run_arm(cell, arm, rep_dir, duration):
    mptcp = (arm == "mptcp")
    env.start_server(cell, mptcp)
    time.sleep(SERVER_UP_MPTCP_S if mptcp else SERVER_UP_TCP_S)
    mon, mon_f = None, None
    if mptcp:
        mon_f = open(rep_dir / "mptcp_monitor.log", "w")
        mon = subprocess.Popen(["ip", "netns", "exec", env.CLIENT_NS, "ip", "mptcp", "monitor"],
                               stdout=mon_f, stderr=subprocess.STDOUT)
        time.sleep(MONITOR_UP_S)
    poller = SsPoller(arm, rep_dir)
    poller.start()
    try:
        p = subprocess.run(["ip", "netns", "exec", env.CLIENT_NS, *env.client_cmd(mptcp, duration)],
                           capture_output=True, text=True, timeout=duration + 40)
    finally:
        poller.stop_evt.set()
        poller.join(timeout=10)
        if mon:
            time.sleep(1)
            mon.terminate()
            try:
                mon.wait(5)
            except subprocess.TimeoutExpired:
                mon.kill()
            mon_f.close()
    (rep_dir / f"{arm}.json").write_text(p.stdout)
    if p.stderr.strip():
        (rep_dir / f"{arm}.stderr").write_text(p.stderr)
    return parse_iperf(p.stdout, p.returncode)


def tc_stats_text():
    parts = []
    for ns, iface, _ in env.IFACES:
        parts.append(f"== {iface} ({ns}) ==\n" +
                     env.ns_capture(ns, ["tc", "-s", "qdisc", "show", "dev", iface]))
    return "\n".join(parts)


def run_rep(cell, rep, rep_dir, duration, attempt):
    rep_dir.mkdir(parents=True, exist_ok=True)
    for f in rep_dir.iterdir():          # discard partial data from an interrupted attempt
        f.unlink()
    t_start = time.time()
    env.kill_iperf3()
    env.flush_tcp_metrics()
    shaping = env.apply_shaping(cell, rep)
    tc_before = env.verify_shaping(cell)   # raises on any mismatch
    buf = env.set_receive_buffer(cell)
    (rep_dir / "tc_before.txt").write_text(
        "\n".join(f"== {k} ==\n{v}" for k, v in tc_before.items()))
    time.sleep(SETTLE_S)

    m = run_arm(cell, "mptcp", rep_dir, duration)
    env.kill_iperf3()
    env.flush_tcp_metrics()
    s = run_arm(cell, "singlepath", rep_dir, duration)
    env.kill_iperf3()

    (rep_dir / "tc_after.txt").write_text(tc_stats_text())
    meta = {
        "cell_id": cell.cell_id, "kind": cell.kind, "rtt_ratio": cell.rtt_ratio,
        "bw_ratio": cell.bw_ratio, "added_loss_pct": cell.added_loss_pct,
        "buffer": cell.buffer, "rep": rep, "attempt": attempt, "legs": cfg.legs(cell),
        "duration_s": duration, "netem": shaping, "receive_buffer": buf,
        "mptcp_ok": m["ok"], "mptcp_mbps": m["mbps"], "mptcp_retransmits": m["retransmits"],
        "mptcp_error": m["error"],
        "singlepath_ok": s["ok"], "singlepath_mbps": s["mbps"],
        "singlepath_retransmits": s["retransmits"], "singlepath_error": s["error"],
        **read_gate(rep_dir),
        "started": datetime.fromtimestamp(t_start).isoformat(timespec="seconds"),
        "elapsed_s": round(time.time() - t_start, 1),
        "complete": bool(m["ok"] and s["ok"]),
    }
    (rep_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def run_rep_with_retries(cell, rep, rep_dir, args, log):
    meta = None
    for attempt in range(1, args.max_attempts + 1):
        try:
            meta = run_rep(cell, rep, rep_dir, args.duration, attempt)
        except (env.EnvError, subprocess.TimeoutExpired, OSError) as e:
            log(f"  attempt {attempt} infrastructure error: {e}")
            meta = {"complete": False, "error": str(e), "cell_id": cell.cell_id, "rep": rep,
                    "attempt": attempt}
            (rep_dir).mkdir(parents=True, exist_ok=True)
            (rep_dir / "meta.json").write_text(json.dumps(meta, indent=2))
            log("  recovering: full testbed rebuild")
            try:
                env.bringup()
            except env.EnvError as e2:
                log(f"  rebuild failed: {e2}")
            continue
        need_retry = (not meta["complete"]) or (args.retry_gate and not meta["dual_subflow_confirmed"])
        if not need_retry:
            break
        log(f"  attempt {attempt} not usable "
            f"(complete={meta['complete']}, gate={meta['dual_subflow_confirmed']}); retrying")
    return meta


# -------------------------------------------------------------------- main -
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="results dir (existing dir = resume)")
    ap.add_argument("--reps", type=int, help=f"reps per cell (default {cfg.REPS}; dry-run 3)")
    ap.add_argument("--duration", type=int, default=cfg.DURATION_S)
    ap.add_argument("--dry-run", action="store_true", help="7-cell, 3-rep verification subset")
    ap.add_argument("--only", help="only cells whose cell_id contains this substring")
    ap.add_argument("--plan", action="store_true", help="print plan and exit (no root needed)")
    ap.add_argument("--max-attempts", type=int, default=2, help="attempts on infrastructure failure")
    ap.add_argument("--retry-gate", action="store_true",
                    help="also retry reps whose monitor gate failed (default: record, don't retry)")
    args = ap.parse_args()
    args.reps = args.reps or (3 if args.dry_run else cfg.REPS)

    cells = select_cells(args)
    total = len(cells) * args.reps
    est_h = total * (2 * args.duration + cfg.PER_REP_OVERHEAD_S) / 3600
    if args.plan:
        print(f"{len(cells)} cells x {args.reps} reps = {total} paired runs, est. {est_h:.1f} h")
        for c in cells:
            print(f"  {c.cell_id:38s} {cfg.legs(c)['cell']}")
        return 0

    env.require_root()
    missing = [t for t in ("iperf3", "mptcpize", "ip", "tc", "ss", "pkill") if not shutil.which(t)]
    if missing:
        print(f"missing tools: {', '.join(missing)}")
        return 1
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else \
        SCRIPT_DIR / "results" / f"{'sweepdry' if args.dry_run else 'sweep'}_{stamp}"
    (out / "cells").mkdir(parents=True, exist_ok=True)
    log = Log(out / "sweep.log")
    kernel = subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip()
    (out / "sweep_run.json").write_text(json.dumps({
        "started": stamp, "kernel": kernel, "args": vars(args),
        "axes": {"rtt_ratios": cfg.RTT_RATIOS, "bw_ratios": cfg.BW_RATIOS,
                 "added_loss_pct": cfg.ADDED_LOSS_PCT, "buffers": cfg.BUFFERS},
        "wifi": cfg.WIFI, "cell_base_loss_pct": cfg.CELL_BASE_LOSS_PCT,
        "small_rcvbuf_bytes": cfg.SMALL_RCVBUF_BYTES, "base_seed": cfg.BASE_SEED,
        "manifest": cfg.manifest(cells)}, indent=2, default=list))
    log(f"sweep: {len(cells)} cells x {args.reps} reps = {total} runs, est. {est_h:.1f} h -> {out}")

    env.bringup()
    log("testbed up (bring-up self-test PASS)")
    done = skipped = failures = usable = gate_fail = 0
    t_session = time.time()
    try:
        for cell in cells:
            for rep in range(1, args.reps + 1):
                rep_dir = out / "cells" / cell.cell_id / f"rep{rep:02d}"
                mp = rep_dir / "meta.json"
                if mp.exists() and json.loads(mp.read_text()).get("complete"):
                    skipped += 1
                    continue
                meta = run_rep_with_retries(cell, rep, rep_dir, args, log)
                done += 1
                if not meta.get("complete"):
                    failures += 1
                    log(f"[{done + skipped}/{total}] {cell.cell_id} rep {rep}: FAILED "
                        f"({meta.get('error') or meta.get('mptcp_error') or meta.get('singlepath_error')})")
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        log(f"aborting: {failures} consecutive failed reps")
                        restore_ownership(out)
                        return 2
                    continue
                failures = 0
                usable += 1
                gate = meta["dual_subflow_confirmed"]
                gate_fail += (not gate)
                eta_h = (time.time() - t_session) / done * (total - done - skipped) / 3600
                log(f"[{done + skipped}/{total}] {cell.cell_id} rep {rep}/{args.reps}: "
                    f"MPTCP {meta['mptcp_mbps']:.2f} | TCP {meta['singlepath_mbps']:.2f} Mbps | "
                    f"gate {'OK' if gate else 'FAIL'} (ss max {meta['ss_subflows_max']}) | "
                    f"ETA {eta_h:.1f} h")
    except KeyboardInterrupt:
        log("interrupted; re-run with the same --out to resume")
        restore_ownership(out)
        return 130
    finally:
        env.kill_iperf3()
        env.clear_shaping()
        env.teardown()
    log(f"finished: {usable} usable reps this session, {skipped} skipped (already done), "
        f"{gate_fail} gate failures. Next: python3 parse_sweep.py {out}")
    restore_ownership(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
