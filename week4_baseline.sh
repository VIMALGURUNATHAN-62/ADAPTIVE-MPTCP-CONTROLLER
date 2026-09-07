#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/netem_shaping.sh"

SCENARIO="${1:?usage: $0 <scenario> [reps] [duration_sec]}"
REPS="${2:-5}"
DURATION="${3:-15}"

CLIENT_NS="client"
SERVER_NS="server"
SERVER_WIFI_IP="10.10.1.2"

TS="$(date +%Y%m%d_%H%M%S)"
OUTDIR="$SCRIPT_DIR/results/${SCENARIO}_${TS}"
mkdir -p "$OUTDIR"

log() { echo -e "\n== $* =="; }

log "Rebuilding testbed"
"$SCRIPT_DIR/testbed_teardown.sh" >/dev/null 2>&1 || true
"$SCRIPT_DIR/testbed_bringup.sh" || { echo "bring-up failed"; exit 1; }

log "Applying shaping scenario: $SCENARIO"
set_scenario "$SCENARIO"
show_scenario | tee "$OUTDIR/tc_qdisc_show.txt" >/dev/null
echo "Shaping parameters saved to $OUTDIR/tc_qdisc_show.txt — verify by eye before trusting results."

for rep in $(seq 1 "$REPS"); do
  log "Rep $rep/$REPS — stock MPTCP run"

  ip netns exec "$SERVER_NS" mptcpize run iperf3 -s -1 -D
  sleep 2

  timeout $((DURATION + 4)) ip netns exec "$CLIENT_NS" ip mptcp monitor \
      > "$OUTDIR/mptcp_monitor_rep${rep}.log" 2>&1 &
  MON_PID=$!
  sleep 1

  (
    end=$((SECONDS + DURATION + 2))
    while [ $SECONDS -lt $end ]; do
      echo "---- $(date +%s.%N) ----"
      ip netns exec "$CLIENT_NS" ss -Mai
      sleep 1
    done
  ) > "$OUTDIR/ss_mptcp_rep${rep}.log" &
  SS_PID=$!

  ip netns exec "$CLIENT_NS" mptcpize run iperf3 -c "$SERVER_WIFI_IP" \
      -t "$DURATION" -J > "$OUTDIR/mptcp_rep${rep}.json"

  wait "$SS_PID" 2>/dev/null
  wait "$MON_PID" 2>/dev/null

  if grep -q "SF_ESTABLISHED" "$OUTDIR/mptcp_monitor_rep${rep}.log"; then
    echo "  rep$rep: monitor confirms SF_ESTABLISHED"
  else
    echo "  rep$rep: monitor shows NO SF_ESTABLISHED — see mptcp_monitor_rep${rep}.log"
  fi

  log "Rep $rep/$REPS — single-path TCP run (no mptcpize -> plain TCP)"
  ip netns exec "$SERVER_NS" iperf3 -s -1 -D
  sleep 1
  ip netns exec "$CLIENT_NS" iperf3 -c "$SERVER_WIFI_IP" \
      -t "$DURATION" -J > "$OUTDIR/singlepath_rep${rep}.json"

  sleep 1
done

log "Tearing down testbed"
"$SCRIPT_DIR/testbed_teardown.sh" >/dev/null 2>&1 || true

echo -e "\nDone. Raw results in: $OUTDIR"
echo "Next: python3 parse_baseline.py $OUTDIR"
