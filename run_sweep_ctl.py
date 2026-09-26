#!/usr/bin/env python3
"""
run_sweep_ctl.py  --  Week 9, file 1: the same sweep, with the controller ACTIVE.

Re-runs exactly the Week 6 conditions -- same cells, same run order, same reps, same duration,
same per-(cell, rep) netem seeds, same receive-buffer settings -- but the MPTCP arm runs on the
userspace path manager with controller.py driving it (client namespace pm_type=1).

The output directory has the SAME layout as a run_sweep.py results dir (mptcp.json,
mptcp_monitor.log, mptcp_ss_*.log, tc_before.txt, meta.json ...) so parse_sweep.py,
hol_diagnosis.py etc. run on it unchanged. Extra per rep:
    ctl_events.jsonl      controller log slice for this rep (connection, ticks, actions, PM calls)
    meta.json additions   arm='controller', ctl_actions, ctl_first_demote_t, ctl_pm_errors,
                          stock_mbps and singlepath_mbps copied from the paired Week 6 rep
                          (so parse_sweep.py compares controller-MPTCP vs single-path directly)
Gate rule unchanged: SF_ESTABLISHED in `ip mptcp monitor` (the controller creates the second
subflow itself, so this also proves the controller's path-manager duty worked).

Usage (root):
  sudo python3 run_sweep_ctl.py --src results/sweep_A                # full mirror of the Week 6 sweep
  sudo python3 run_sweep_ctl.py --src results/sweepdry_X --dry-run   # same 7-cell subset
Options: --out DIR (default <src>_ctl; existing dir = resume), --params FILE (default
         <src>/controller_params.json), --reps N, --duration S, --only SUBSTR, --plan
Needs (same directory): run_sweep.py sweep_env.py sweep_config.py controller.py policy.py
         mptcp_pm_nl.py testbed_bringup.sh testbed_teardown.sh
"""
import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import run_sweep as rs
import sweep_config as cfg
import sweep_env as env

SCRIPT_DIR = Path(__file__).resolve().parent
CTL_START_WAIT_S = 1.5


class CtlDaemon:
    """Long-lived controller.py in the client namespace (restartable)."""

    def __init__(self, params_file, log_path):
        self.params_file, self.log_path, self.proc = params_file, log_path, None

    def start(self):
        cmd = ["ip", "netns", "exec", env.CLIENT_NS, sys.executable, str(SCRIPT_DIR / "controller.py"),
               "--log", str(self.log_path)]
        if self.params_file:
            cmd += ["--params", str(self.params_file)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        time.sleep(CTL_START_WAIT_S)
        if self.proc.poll() is not None:
            raise env.EnvError(f"controller.py exited at start: {self.proc.stderr.read()[-500:]}")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def setup_env(daemon):
    env.bringup()
    env.sh(["sysctl", "-qw", "net.mptcp.pm_type=1"], ns=env.CLIENT_NS)
    daemon.stop()
    daemon.start()


def slice_log(log_path, t0, t1):
    out = []
    try:
        for line in open(log_path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if t0 - 1.0 <= r.get("ts", 0) <= t1 + 3.0:
                out.append(r)
    except OSError:
        pass
    return out


def load_src(src):
    """{(cell_id, rep): src meta} from the Week 6 results dir."""
    m = {}
    for p in Path(src, "cells").glob("*/rep*/meta.json"):
        try:
            d = json.loads(p.read_text())
        except ValueError:
            continue
        if d.get("complete"):
            m[(d["cell_id"], d["rep"])] = d
    return m


def run_rep_ctl(cell, rep, rep_dir, duration, attempt, src_meta, daemon):
    rep_dir.mkdir(parents=True, exist_ok=True)
    for f in rep_dir.iterdir():
        f.unlink()
    if not daemon.alive():
        raise env.EnvError("controller daemon is not running")
    t_start = time.time()
    env.kill_iperf3()
    env.flush_tcp_metrics()
    shaping = env.apply_shaping(cell, rep)
    tc_before = env.verify_shaping(cell)
    buf = env.set_receive_buffer(cell)
    (rep_dir / "tc_before.txt").write_text("\n".join(f"== {k} ==\n{v}" for k, v in tc_before.items()))
    time.sleep(rs.SETTLE_S)
    m = rs.run_arm(cell, "mptcp", rep_dir, duration)
    env.kill_iperf3()
    (rep_dir / "tc_after.txt").write_text(rs.tc_stats_text())
    t_end = time.time()

    ev = slice_log(daemon.log_path, t_start, t_end)
    (rep_dir / "ctl_events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ev))
    acts = [r for r in ev if r["ev"] == "action"]
    demotes = [a for a in acts if a["kind"] == "MARK_BACKUP" and a["executed"]]
    perr = [r for r in ev if r["ev"] == "pm_call" and str(r["result"]).startswith("error")]
    meta = {
        "cell_id": cell.cell_id, "kind": cell.kind, "rtt_ratio": cell.rtt_ratio, "bw_ratio": cell.bw_ratio,
        "added_loss_pct": cell.added_loss_pct, "buffer": cell.buffer, "rep": rep, "attempt": attempt,
        "arm": "controller", "legs": cfg.legs(cell), "duration_s": duration, "netem": shaping,
        "receive_buffer": buf,
        "mptcp_ok": m["ok"], "mptcp_mbps": m["mbps"], "mptcp_retransmits": m["retransmits"], "mptcp_error": m["error"],
        # paired Week 6 measurements (same cell, same rep, same netem seed)
        "stock_mbps": (src_meta or {}).get("mptcp_mbps"),
        "stock_gate": (src_meta or {}).get("dual_subflow_confirmed"),
        "singlepath_mbps": (src_meta or {}).get("singlepath_mbps"),
        "singlepath_ok": bool(src_meta and src_meta.get("singlepath_ok")),
        "singlepath_retransmits": (src_meta or {}).get("singlepath_retransmits"),
        "singlepath_error": None,
        "ctl_actions": [{"kind": a["kind"], "path": a["path"], "t": a["t"], "executed": a["executed"]} for a in acts],
        "ctl_first_demote_t": demotes[0]["t"] if demotes else None,
        "ctl_pm_errors": len(perr),
        **rs.read_gate(rep_dir),
        "started": datetime.fromtimestamp(t_start).isoformat(timespec="seconds"),
        "elapsed_s": round(t_end - t_start, 1),
        # 'complete' needs the paired single-path value so parse_sweep.py can score this rep
        "complete": bool(m["ok"] and src_meta and src_meta.get("singlepath_ok")),
    }
    (rep_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="Week 6 results dir to mirror")
    ap.add_argument("--out")
    ap.add_argument("--params")
    ap.add_argument("--reps", type=int)
    ap.add_argument("--duration", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--max-attempts", type=int, default=2)
    a = ap.parse_args()

    src = Path(a.src)
    try:
        src_run = json.loads((src / "sweep_run.json").read_text())
    except (OSError, ValueError):
        print(f"{src}/sweep_run.json not found: --src must be a run_sweep.py results directory")
        return 1
    a.reps = a.reps or src_run["args"]["reps"]
    a.duration = a.duration or src_run["args"]["duration"]
    a.max_attempts = max(1, a.max_attempts)
    cells = rs.select_cells(a)
    total = len(cells) * a.reps
    est_h = total * (a.duration + 12) / 3600
    if a.plan:
        print(f"{len(cells)} cells x {a.reps} reps = {total} controller-arm runs, est. {est_h:.1f} h "
              f"(mirrors {src}, base_seed {src_run.get('base_seed')})")
        return 0

    env.require_root()
    missing = [t for t in ("iperf3", "mptcpize", "ip", "tc", "ss", "pkill") if not shutil.which(t)]
    if missing:
        print(f"missing tools: {', '.join(missing)}")
        return 1
    if src_run.get("base_seed") not in (None, cfg.BASE_SEED):
        print(f"WARNING: source sweep used base_seed {src_run['base_seed']} but sweep_config has {cfg.BASE_SEED}: "
              f"conditions would NOT match; aborting")
        return 1
    params = Path(a.params) if a.params else src / "controller_params.json"
    if not params.exists():
        print(f"controller parameters not found ({params}): run replay_policy.py on {src} first "
              f"or pass --params (using defaults would make the comparison untuned)")
        return 1

    out = Path(a.out) if a.out else src.parent / (src.name + "_ctl")
    (out / "cells").mkdir(parents=True, exist_ok=True)
    log = rs.Log(out / "sweep.log")
    src_meta = load_src(src)
    (out / "sweep_run.json").write_text(json.dumps({
        "started": datetime.now().strftime("%Y%m%d_%H%M%S"), "mode": "controller", "mirrors": str(src),
        "args": {"reps": a.reps, "duration": a.duration, "dry_run": a.dry_run, "only": a.only},
        "base_seed": cfg.BASE_SEED, "controller_params": json.loads(params.read_text()),
        "manifest": cfg.manifest(cells)}, indent=2, default=list))
    shutil.copy(params, out / "controller_params.json")
    log(f"controller sweep: {len(cells)} cells x {a.reps} reps = {total} runs, est. {est_h:.1f} h -> {out}")
    log(f"mirroring {src} ({len(src_meta)} complete paired reps found); params {params}")

    daemon = CtlDaemon(params, out / "controller.jsonl")
    done = skipped = failures = usable = gate_fail = unpaired = 0
    t_session = time.time()
    try:
        setup_env(daemon)
        log("testbed up, client on userspace PM, controller running")
        for cell in cells:
            for rep in range(1, a.reps + 1):
                rep_dir = out / "cells" / cell.cell_id / f"rep{rep:02d}"
                mp = rep_dir / "meta.json"
                if mp.exists() and json.loads(mp.read_text()).get("complete"):
                    skipped += 1
                    continue
                sm = src_meta.get((cell.cell_id, rep))
                if sm is None:          # no Week 6 rep to pair with: nothing to compare against
                    unpaired += 1
                    continue
                # Fresh controller state for every repetition.
                # This prevents connections from a previous repetition from
                # remaining in the long-lived controller state.
                daemon.stop()
                daemon.start()

                meta = None
                for attempt in range(1, a.max_attempts + 1):
                    try:
                        meta = run_rep_ctl(cell, rep, rep_dir, a.duration, attempt, sm, daemon)
                        if meta["complete"]:
                            break
                        log(f"  attempt {attempt} not complete (mptcp_ok={meta['mptcp_ok']})")
                    except (env.EnvError, subprocess.TimeoutExpired, OSError) as e:
                        log(f"  attempt {attempt} infrastructure error: {e}; rebuilding")
                        meta = {"complete": False, "error": str(e)}
                        try:
                            setup_env(daemon)
                        except env.EnvError as e2:
                            log(f"  rebuild failed: {e2}")
                done += 1
                if not meta or not meta.get("complete"):
                    failures += 1
                    log(f"[{done + skipped}/{total}] {cell.cell_id} rep {rep}: FAILED "
                        f"({(meta or {}).get('error') or (meta or {}).get('mptcp_error') or 'unknown'})")
                    if failures >= rs.MAX_CONSECUTIVE_FAILURES:
                        log(f"aborting: {failures} consecutive failed reps")
                        return 2
                    continue
                failures = 0
                usable += 1
                gate_fail += (not meta["dual_subflow_confirmed"])
                eta_h = (time.time() - t_session) / done * (total - done - skipped) / 3600
                stock = meta["stock_mbps"]
                log(f"[{done + skipped}/{total}] {cell.cell_id} rep {rep}/{a.reps}: "
                    f"controller {meta['mptcp_mbps']:.2f} | stock {stock if stock is None else round(stock, 2)} | "
                    f"single {meta['singlepath_mbps']:.2f} Mbps | actions "
                    f"{[x['kind'] for x in meta['ctl_actions']]} first-demote t={meta['ctl_first_demote_t']} | "
                    f"gate {'OK' if meta['dual_subflow_confirmed'] else 'FAIL'} | ETA {eta_h:.1f} h")
    except KeyboardInterrupt:
        log("interrupted; re-run with the same --out to resume")
        return 130
    except env.EnvError as e:
        log(f"fatal: {e}")
        return 1
    finally:
        daemon.stop()
        env.kill_iperf3()
        env.clear_shaping()
        env.teardown()
    log(f"finished: {usable} usable reps, {skipped} skipped (already done), {unpaired} skipped (no paired "
        f"Week 6 rep), {gate_fail} gate failures. "
        f"Next: python3 parse_sweep.py {out}   then   python3 compare_configs.py --stock {src} --controller {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
