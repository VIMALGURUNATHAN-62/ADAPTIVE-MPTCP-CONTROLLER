#!/usr/bin/env bash
set -euo pipefail
CLIENT_NS="client"; SERVER_NS="server"
VETH_C_WIFI="c-wifi"; VETH_S_WIFI="s-wifi"
IP_C_WIFI="10.10.1.1/24"; IP_S_WIFI="10.10.1.2/24"
VETH_C_CELL="c-cell"; VETH_S_CELL="s-cell"
IP_C_CELL="10.10.2.1/24"; IP_S_CELL="10.10.2.2/24"
LOGDIR="$(eval echo ~${SUDO_USER:-$USER})/mptcp-project/logs"
mkdir -p "$LOGDIR"
rm -f "$LOGDIR/bringup_monitor.log" "$LOGDIR/bringup_selftest.log"
log() { echo -e "\n== $* =="; }

log "Step 0: verify MPTCP kernel support"
uname -r
sysctl net.mptcp.enabled >/dev/null 2>&1 || { echo "MPTCP not available on this kernel."; exit 1; }

log "Step 1: create namespaces"
ip netns add "$CLIENT_NS" 2>/dev/null || echo "  (client already exists)"
ip netns add "$SERVER_NS" 2>/dev/null || echo "  (server already exists)"

log "Step 2: create veth pairs"
ip link add "$VETH_C_WIFI" type veth peer name "$VETH_S_WIFI" 2>/dev/null || true
ip link add "$VETH_C_CELL" type veth peer name "$VETH_S_CELL" 2>/dev/null || true
ip link set "$VETH_C_WIFI" netns "$CLIENT_NS"
ip link set "$VETH_S_WIFI" netns "$SERVER_NS"
ip link set "$VETH_C_CELL" netns "$CLIENT_NS"
ip link set "$VETH_S_CELL" netns "$SERVER_NS"

log "Step 3: addressing and link state"
ip netns exec "$CLIENT_NS" ip addr add "$IP_C_WIFI" dev "$VETH_C_WIFI"
ip netns exec "$CLIENT_NS" ip link set "$VETH_C_WIFI" up
ip netns exec "$CLIENT_NS" ip addr add "$IP_C_CELL" dev "$VETH_C_CELL"
ip netns exec "$CLIENT_NS" ip link set "$VETH_C_CELL" up
ip netns exec "$CLIENT_NS" ip link set lo up
ip netns exec "$SERVER_NS" ip addr add "$IP_S_WIFI" dev "$VETH_S_WIFI"
ip netns exec "$SERVER_NS" ip link set "$VETH_S_WIFI" up
ip netns exec "$SERVER_NS" ip addr add "$IP_S_CELL" dev "$VETH_S_CELL"
ip netns exec "$SERVER_NS" ip link set "$VETH_S_CELL" up
ip netns exec "$SERVER_NS" ip link set lo up

log "Step 4: enable MPTCP, register endpoints, set subflow limits"
ip netns exec "$CLIENT_NS" sysctl -qw net.mptcp.enabled=1
ip netns exec "$SERVER_NS" sysctl -qw net.mptcp.enabled=1
ip netns exec "$CLIENT_NS" sysctl -qw net.mptcp.pm_type=0
ip netns exec "$SERVER_NS" sysctl -qw net.mptcp.pm_type=0
ip netns exec "$CLIENT_NS" ip mptcp endpoint add "${IP_C_CELL%/*}" dev "$VETH_C_CELL" subflow
ip netns exec "$SERVER_NS" ip mptcp endpoint add "${IP_S_CELL%/*}" dev "$VETH_S_CELL" signal
ip netns exec "$CLIENT_NS" ip mptcp limits set subflows 1 add_addr_accepted 1

echo "-- client endpoints --"; ip netns exec "$CLIENT_NS" ip mptcp endpoint show
echo "-- server endpoints --"; ip netns exec "$SERVER_NS" ip mptcp endpoint show
echo "-- client limits --"; ip netns exec "$CLIENT_NS" ip mptcp limits show

log "Step 5: self-test - confirm both subflows actually come up"
pkill -9 -f "iperf3 --server" 2>/dev/null || true
sleep 1
ip netns exec "$SERVER_NS" mptcpize run iperf3 -s -1 -D
sleep 2
timeout 10 ip netns exec "$CLIENT_NS" ip mptcp monitor > "$LOGDIR/bringup_monitor.log" 2>&1 &
MONITOR_PID=$!
sleep 1
set +e
ip netns exec "$CLIENT_NS" mptcpize run iperf3 -c "${IP_S_WIFI%/*}" -t 8 > "$LOGDIR/bringup_selftest.log" 2>&1
wait "$MONITOR_PID" 2>/dev/null || true

log "Step 6: verdict"
if grep -q "SF_ESTABLISHED" "$LOGDIR/bringup_monitor.log"; then
  echo "PASS - second subflow joined. Testbed is ready."
else
  echo "FAIL - no SF_ESTABLISHED seen. Client iperf3 log:"
  cat "$LOGDIR/bringup_selftest.log"
  echo "MPTCP monitor log:"
  cat "$LOGDIR/bringup_monitor.log"
  exit 1
fi
echo -e "\nBring-up complete."
