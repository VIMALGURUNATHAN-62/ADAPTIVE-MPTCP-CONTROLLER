#!/usr/bin/env bash
# pm_userspace_probe.sh -- Week 5, file 6.
#
# Proves the userspace path-manager control path end to end on the Week 3/4 testbed:
#   1. bring the testbed up (in-kernel PM, as in Week 3) and shape it (baseline-symmetric)
#   2. switch ONLY the client namespace to the userspace PM (net.mptcp.pm_type=1)
#   3. start iperf3 traffic; mptcp_pm_nl.py probe then, over the MPTCP netlink PM API:
#        - creates the cellular subflow (SUBFLOW_CREATE)   -> SF_ESTABLISHED event
#        - marks it backup, then restores it (SET_FLAGS)   -> MP_PRIO, SF_PRIORITY events
#        - checks ss subflow flags on both ends
#   4. cross-checks with `ip mptcp monitor` (SF_ESTABLISHED, the project's gate) and that
#      the iperf3 transfer survived the backup toggle
# Server namespace stays on the in-kernel PM (it announces its cellular address and accepts
# the join, exactly as in Week 3/4).
#
# Usage:  sudo ./pm_userspace_probe.sh [duration_sec=20]
# Needs (same directory): testbed_bringup.sh testbed_teardown.sh netem_shaping.sh mptcp_pm_nl.py
# Evidence: results/pm_probe_<timestamp>/  (probe.json, probe.log, ss_*.log, monitor log ...)
set -uo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION="${1:-20}"
CLIENT_NS="client"; SERVER_NS="server"; SERVER_WIFI_IP="10.10.1.2"

for f in testbed_bringup.sh testbed_teardown.sh netem_shaping.sh mptcp_pm_nl.py; do
  [[ -f "$SCRIPT_DIR/$f" ]] || { echo "REQUEST FILE: $f"; exit 1; }
done
for t in iperf3 mptcpize ip ss python3; do
  command -v "$t" >/dev/null || { echo "missing tool: $t"; exit 1; }
done

OUT="$SCRIPT_DIR/results/pm_probe_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
log() { echo -e "\n== $* =="; }

cleanup() {
  jobs -p | xargs -r kill 2>/dev/null || true
  pkill -9 -x iperf3 2>/dev/null || true
  "$SCRIPT_DIR/testbed_teardown.sh" >/dev/null 2>&1 || true
}
trap cleanup EXIT

poll_ss() {  # 1 Hz ss snapshots of one namespace (MPTCP sockets + TCP subflows)
  local end=$((SECONDS + DURATION + 6))
  while [ $SECONDS -lt $end ]; do
    echo "---- $(date +%s.%N) ----"
    ip netns exec "$1" ss -Mai
    ip netns exec "$1" ss -tni
    sleep 1
  done
}

log "Step 0: offline selftest of the netlink client"
python3 "$SCRIPT_DIR/mptcp_pm_nl.py" selftest || { echo "netlink client selftest failed"; exit 1; }

log "Step 1: rebuild testbed (in-kernel PM) and shape paths"
"$SCRIPT_DIR/testbed_teardown.sh" >/dev/null 2>&1 || true
"$SCRIPT_DIR/testbed_bringup.sh" > "$OUT/bringup.log" 2>&1 \
  || { echo "bring-up failed:"; tail -20 "$OUT/bringup.log"; exit 1; }
tail -2 "$OUT/bringup.log"
source "$SCRIPT_DIR/netem_shaping.sh"
set_scenario baseline-symmetric
show_scenario > "$OUT/tc_qdisc_show.txt" 2>&1

log "Step 2: switch client namespace to the userspace path manager"
if ip netns exec "$CLIENT_NS" sysctl -qw net.mptcp.pm_type=1 2>/dev/null; then
  PM_KNOB="net.mptcp.pm_type=1"
elif ip netns exec "$CLIENT_NS" sysctl -qw net.mptcp.path_manager=userspace 2>/dev/null; then
  PM_KNOB="net.mptcp.path_manager=userspace"
else
  echo "FAIL: neither net.mptcp.pm_type nor net.mptcp.path_manager is settable in the client namespace"
  exit 1
fi
{ echo "kernel: $(uname -r)"; echo "set: $PM_KNOB (client ns)";
  echo "-- client ns --"; ip netns exec "$CLIENT_NS" sysctl net.mptcp;
  echo "-- server ns --"; ip netns exec "$SERVER_NS" sysctl net.mptcp; } | tee "$OUT/pm_mode.txt"
if command -v mptcpd >/dev/null; then
  echo "mptcpd installed: $(mptcpd --version 2>&1 | head -1) (not used: it has no command to mark a subflow backup;" \
       "control is done through the same MPTCP netlink PM API it wraps)"
else
  echo "mptcpd not installed (not required for this probe)"
fi

log "Step 3: traffic + probe (duration ${DURATION}s)"
ip netns exec "$SERVER_NS" mptcpize run iperf3 -s -1 -D
sleep 2
timeout $((DURATION + 8)) ip netns exec "$CLIENT_NS" ip mptcp monitor > "$OUT/client_ip_mptcp_monitor.log" 2>&1 &
poll_ss "$CLIENT_NS" > "$OUT/ss_client.log" 2>&1 &
poll_ss "$SERVER_NS" > "$OUT/ss_server.log" 2>&1 &
ip netns exec "$CLIENT_NS" python3 "$SCRIPT_DIR/mptcp_pm_nl.py" probe \
    --server-ns "$SERVER_NS" --wait $((DURATION + 5)) --out "$OUT/probe.json" > "$OUT/probe.log" 2>&1 &
PROBE_PID=$!
sleep 2   # probe opens its event socket (and the server-side monitor) before the connection exists
ip netns exec "$CLIENT_NS" mptcpize run iperf3 -c "$SERVER_WIFI_IP" -t "$DURATION" -J \
    > "$OUT/iperf_client.json" 2> "$OUT/iperf_client.err"
IPERF_RC=$?
wait "$PROBE_PID"; PROBE_RC=$?
wait 2>/dev/null

log "Step 4: results"
cat "$OUT/probe.log"

IPERF_INFO="$(python3 - "$OUT/iperf_client.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    if "error" in d:
        print("ERR", d["error"]); sys.exit()
    e = d["end"]; r = e.get("sum_received") or e.get("sum_sent") or {}
    bps = r.get("bits_per_second", 0)
    print("OK" if bps > 0 else "ERR", f"{bps / 1e6:.2f} Mbps")
except Exception as ex:
    print("ERR", ex)
PY
)"
SF_MON="$(grep -c SF_ESTABLISHED "$OUT/client_ip_mptcp_monitor.log" 2>/dev/null || true)"
SS_TWO="$(grep -c 'subflows:2' "$OUT/ss_client.log" 2>/dev/null || true)"

log "VERDICT"
echo "userspace PM knob (client ns)             : $PM_KNOB"
echo "probe (create + backup/restore via netlink): $([ "$PROBE_RC" -eq 0 ] && echo PASS || echo FAIL)"
echo "ip mptcp monitor SF_ESTABLISHED (gate)     : ${SF_MON:-0}"
echo "iperf3 during control (rc=$IPERF_RC)            : $IPERF_INFO"
echo "ss -Mai snapshots showing subflows:2 (info): ${SS_TWO:-0}  (polling is informational only)"
echo "evidence: $OUT"
if [ "$PROBE_RC" -eq 0 ] && [ "${SF_MON:-0}" -gt 0 ] && [[ "$IPERF_INFO" == OK* ]]; then
  echo "WEEK 5 PM CHECK: PASS"; exit 0
fi
echo "WEEK 5 PM CHECK: FAIL (see $OUT/probe.log)"; exit 1
