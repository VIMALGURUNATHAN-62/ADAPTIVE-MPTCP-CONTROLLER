#!/usr/bin/env bash
# failover_demo.sh -- Week 8, file 2: "kill Wi-Fi mid-download" resilience demonstration.
#
# For each rep and mode it rebuilds the testbed, shapes it (baseline-symmetric), starts a long
# iperf3 download over MPTCP, takes the Wi-Fi link down mid-transfer (ip link set c-wifi down)
# and measures whether the connection survives and how long delivery stalls before resuming
# on the cellular subflow:
#   stock       in-kernel path manager, no controller (Week 3 setup)
#   controller  client namespace on the userspace PM (pm_type=1) + controller.py
# Interruption = time from Wi-Fi loss to the first interval whose throughput recovers to
# >= 50% of the steady post-failure rate (iperf3 -i 0.5 intervals; +/- 0.5 s resolution).
# The comparison is REPORTED, not assumed: the controller is not guaranteed to be faster.
#
# Usage: sudo ./failover_demo.sh [mode=both|stock|controller] [reps=3] [duration=24] [fail_at=8]
#        RESTORE_AFTER=<s>  bring Wi-Fi back up this long after the failure (default: stay down)
#        CTL_PARAMS=<file>  controller parameters (default: ./controller_params.json if present)
# Needs (same directory): testbed_bringup.sh testbed_teardown.sh netem_shaping.sh controller.py
#                         mptcp_pm_nl.py policy.py
# Output: results/failover_<timestamp>/{stock,controller}_repN/ + summary.json + failover_plot.png
set -uo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-both}"; REPS="${2:-3}"; DURATION="${3:-24}"; FAIL_AT="${4:-8}"
RESTORE_AFTER="${RESTORE_AFTER:-0}"
SERVER_WIFI_IP="10.10.1.2"

case "$MODE" in both|stock|controller) ;; *) echo "mode must be both|stock|controller"; exit 1;; esac
for f in testbed_bringup.sh testbed_teardown.sh netem_shaping.sh controller.py mptcp_pm_nl.py policy.py; do
  [[ -f "$SCRIPT_DIR/$f" ]] || { echo "REQUEST FILE: $f"; exit 1; }
done
for t in iperf3 mptcpize ip ss python3; do command -v "$t" >/dev/null || { echo "missing tool: $t"; exit 1; }; done

# shellcheck disable=SC1091
source "$SCRIPT_DIR/netem_shaping.sh"          # CLIENT_NS, SERVER_NS, IF_C_WIFI, set_scenario, ...
PARAMS_FILE="${CTL_PARAMS:-$SCRIPT_DIR/controller_params.json}"
if [[ -f "$PARAMS_FILE" ]]; then PARAMS_ARG=(--params "$PARAMS_FILE"); echo "controller parameters: $PARAMS_FILE"
else PARAMS_ARG=(); echo "controller parameters: defaults (no $PARAMS_FILE; run replay_policy.py first for tuned values)"; fi

OUT="$SCRIPT_DIR/results/failover_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
log() { echo -e "\n== $* =="; }
cleanup() {
  jobs -p | xargs -r kill 2>/dev/null || true
  pkill -9 -x iperf3 2>/dev/null || true
  "$SCRIPT_DIR/testbed_teardown.sh" >/dev/null 2>&1 || true
  [[ -n "${SUDO_UID:-}" && -d "$OUT" ]] && chown -R "$SUDO_UID:$SUDO_GID" "$OUT" 2>/dev/null || true
}
trap cleanup EXIT

run_one() {   # $1 = stock|controller, $2 = rep
  local mode="$1" rep="$2" d="$OUT/${1}_rep${2}" ctl_pid="" mon_pid="" poll_pid="" ipid=""
  mkdir -p "$d"
  log "[$mode rep $rep] rebuild testbed"
  "$SCRIPT_DIR/testbed_teardown.sh" >/dev/null 2>&1 || true
  "$SCRIPT_DIR/testbed_bringup.sh" > "$d/bringup.log" 2>&1 \
    || { echo "bring-up failed"; tail -15 "$d/bringup.log"; return 1; }
  set_scenario baseline-symmetric > /dev/null
  show_scenario > "$d/tc_qdisc_show.txt" 2>&1

  if [[ "$mode" == "controller" ]]; then
    ip netns exec "$CLIENT_NS" sysctl -qw net.mptcp.pm_type=1
    ip netns exec "$CLIENT_NS" python3 "$SCRIPT_DIR/controller.py" "${PARAMS_ARG[@]}" \
        --duration "$((DURATION + 3))" \
        --log "$d/controller.jsonl" > "$d/controller.out" 2>&1 &
    ctl_pid=$!
    sleep 1.5
    kill -0 "$ctl_pid" 2>/dev/null || { echo "controller failed to start:"; cat "$d/controller.out"; return 1; }
  fi

  ip netns exec "$SERVER_NS" mptcpize run iperf3 -s -1 -D
  sleep 1
  timeout $((DURATION + 12)) ip netns exec "$CLIENT_NS" ip mptcp monitor > "$d/monitor.log" 2>&1 &
  mon_pid=$!
  ( end=$((SECONDS + DURATION + 8))
    while [ $SECONDS -lt $end ]; do
      echo "---- $(date +%s.%N) ----"
      ip netns exec "$CLIENT_NS" ss -Mai; ip netns exec "$CLIENT_NS" ss -tni
      sleep 1
    done ) > "$d/ss_client.log" 2>&1 &
  poll_pid=$!

  log "[$mode rep $rep] download ${DURATION}s, Wi-Fi down at t=${FAIL_AT}s"
  local T0 TF
  T0=$(date +%s.%N)
  ip netns exec "$CLIENT_NS" mptcpize run iperf3 -c "$SERVER_WIFI_IP" -t "$DURATION" -i 0.5 -J \
      > "$d/iperf.json" 2> "$d/iperf.err" &
  ipid=$!
  sleep "$FAIL_AT"
  TF=$(date +%s.%N)
  ip netns exec "$CLIENT_NS" ip link set "$IF_C_WIFI" down
  echo "wifi link down at t=${FAIL_AT}s"
  if [[ "$RESTORE_AFTER" != "0" ]]; then
    sleep "$RESTORE_AFTER"; ip netns exec "$CLIENT_NS" ip link set "$IF_C_WIFI" up
    echo "wifi link restored after ${RESTORE_AFTER}s"
  fi
  wait "$ipid"; local rc=$?
  # Stop the controller immediately when the measured transfer ends.
  # Do not allow a post-test controller tick/action to race with connection teardown.
  [[ -n "$ctl_pid" ]] && { kill "$ctl_pid" 2>/dev/null; wait "$ctl_pid" 2>/dev/null; }
  kill "$mon_pid" "$poll_pid" 2>/dev/null; wait "$mon_pid" "$poll_pid" 2>/dev/null
  pkill -9 -x iperf3 2>/dev/null || true

  python3 - "$d" "$T0" "$TF" "$DURATION" "$rc" "$mode" "$rep" <<'PY'
import json, statistics as st, sys
d, T0, TF, dur, rc, mode, rep = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), int(sys.argv[5]), sys.argv[6], sys.argv[7]
res = {"mode": mode, "rep": int(rep), "iperf_rc": rc, "t_fail": round(TF - T0, 2)}
try:
    j = json.load(open(f"{d}/iperf.json"))
except Exception as e:
    res.update(ok=False, error=f"no iperf json: {e}"); print(json.dumps(res)); json.dump(res, open(f"{d}/result.json", "w")); sys.exit()
if "error" in j:
    res.update(ok=False, error=j["error"])
else:
    iv = [(x["sum"]["start"], x["sum"]["end"], x["sum"]["bits_per_second"] / 1e6) for x in j["intervals"]]
    tf = res["t_fail"]
    pre = [b for s, e, b in iv if s >= 2 and e <= tf - 0.5]
    tail_raw = [x for x in j["intervals"] if x["sum"]["start"] >= 0.7 * iv[-1][1]]
    post = [(s, e, b) for s, e, b in iv if e > tf]      # intervals overlapping or after the failure
    # Byte-weighted throughput over the tail window, not a median of per-interval rates: RTO
    # back-off after a real failure alternates real-data and near-zero intervals (confirmed in
    # practice), and a median over that alternating pattern is brittle -- it can land on either
    # side depending on how many zero-vs-nonzero intervals happen to fall in the tail slice.
    tail_secs = sum(x["sum"]["seconds"] for x in tail_raw)
    tail_bytes = sum(x["sum"]["bytes"] for x in tail_raw)
    post_med = round((tail_bytes * 8 / tail_secs) / 1e6, 2) if tail_secs > 0 else 0.0
    # Treat recovery as a 2-second rolling-throughput condition.
    # Real MPTCP failover can alternate nonzero and near-zero 0.5s iperf intervals;
    # requiring three consecutive nonzero intervals can exaggerate the interruption.
    resume = None
    window_n = 4  # 4 x 0.5 s intervals = 2 s
    for k, (s, e, b) in enumerate(post):
        if s < tf:
            continue
        window = post[k:k + window_n]
        if len(window) < window_n:
            break
        avg = sum(x[2] for x in window) / window_n
        if post_med > 1.0 and avg >= 0.5 * post_med:
            resume = s
            break
    res.update(
        pre_mbps=round(st.mean(pre), 2) if pre else None, post_mbps=round(post_med, 2),
        min_after_fail_mbps=round(min((b for s, e, b in post if s < tf + 3), default=0), 2),
        stalled_s=round(sum(e - s for s, e, b in post if b < 0.5), 1),
        interruption_s=round(max(0.0, resume - tf), 2) if resume is not None else None,
        completed=bool(iv and iv[-1][1] >= dur - 1.0 and rc == 0),
        intervals=[(round(s, 2), round(b, 2)) for s, e, b in iv])
    res["ok"] = res["completed"] and res["interruption_s"] is not None and post_med > 1.0
json.dump(res, open(f"{d}/result.json", "w"))
print(f"[{mode} rep {rep}] before {res.get('pre_mbps')} Mbps | Wi-Fi down @ {res['t_fail']}s | "
      f"interruption {res.get('interruption_s')} s | after {res.get('post_mbps')} Mbps | "
      f"completed={res.get('completed')} | {'PASS' if res.get('ok') else 'FAIL'} {res.get('error', '')}")
PY

  if [[ "$mode" == "controller" && -f "$d/controller.jsonl" ]]; then
    echo "controller decisions:"
    python3 - "$d/controller.jsonl" <<'PY'
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
t0 = next((r["ts"] for r in recs if r["ev"] == "connection"), recs[0]["ts"])
shown = 0
for r in recs:
    if r["ev"] == "action":
        shown += 1
        print(f"  +{r['ts'] - t0:5.1f}s  {r['kind']:<11s} {r['path']:<5s} score={r['score']} executed={r['executed']}  ({r['why']})")
    elif r["ev"] == "pm_call" and str(r["result"]).startswith("error"):
        shown += 1
        print(f"  +{r['ts'] - t0:5.1f}s  pm_call {r['what']} {r['path']}: {r['result']}")
if not shown:
    print(f"  (no actions; {sum(r['ev'] == 'connection' for r in recs)} connection(s) tracked, "
          f"{sum(r['ev'] == 'tick' for r in recs)} tick records)")
PY
  fi
  return 0
}

modes=(); [[ "$MODE" == "both" ]] && modes=(stock controller) || modes=("$MODE")
for rep in $(seq 1 "$REPS"); do
  for m in "${modes[@]}"; do run_one "$m" "$rep" || echo "run $m/$rep failed"; done
done

log "SUMMARY"
python3 - "$OUT" <<'PY'
import glob, json, statistics as st, sys
out = sys.argv[1]
res = [json.load(open(p)) for p in sorted(glob.glob(f"{out}/*_rep*/result.json"))]
summ = {}
for m in ("stock", "controller"):
    rs = [r for r in res if r["mode"] == m]
    if not rs:
        continue
    ints = [r["interruption_s"] for r in rs if r.get("interruption_s") is not None]
    posts = [r["post_mbps"] for r in rs if r.get("post_mbps") is not None]
    summ[m] = {"runs": len(rs), "passed": sum(bool(r.get("ok")) for r in rs),
               "completed": sum(bool(r.get("completed")) for r in rs),
               "interruption_s_median": st.median(ints) if ints else None,
               "interruption_s_all": ints, "post_mbps_median": st.median(posts) if posts else None}
    print(f"{m:11s} runs={len(rs)} completed={summ[m]['completed']}/{len(rs)} pass={summ[m]['passed']}/{len(rs)} "
          f"interruption median={summ[m]['interruption_s_median']} s (all {ints}) "
          f"post-failure goodput median={summ[m]['post_mbps_median']} Mbps")
json.dump(summ, open(f"{out}/summary.json", "w"), indent=2)
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 4))
    for m, c in (("stock", "tab:blue"), ("controller", "tab:red")):
        r = next((r for r in res if r["mode"] == m and r.get("intervals")), None)
        if r:
            ax.plot([t for t, b in r["intervals"]], [b for t, b in r["intervals"]], color=c, label=f"{m} (rep {r['rep']})")
            ax.axvline(r["t_fail"], color="k", ls="--", lw=1)
    ax.set_xlabel("time (s)"); ax.set_ylabel("goodput (Mbps)"); ax.set_title("Wi-Fi link down mid-download (dashed line)")
    ax.legend(); fig.tight_layout(); fig.savefig(f"{out}/failover_plot.png", dpi=150)
    print(f"plot: {out}/failover_plot.png")
except ImportError:
    print("matplotlib not installed: plot skipped")
PY
echo "evidence: $OUT"
