#!/usr/bin/env bash
set -uo pipefail

CLIENT_NS="client"
SERVER_NS="server"
IF_C_WIFI="c-wifi"; IF_S_WIFI="s-wifi"
IF_C_CELL="c-cell"; IF_S_CELL="s-cell"

apply_leg() {
  local ns="$1" iface="$2" delay="$3" jitter="$4" loss="$5" rate="$6"
  ip netns exec "$ns" tc qdisc replace dev "$iface" root netem \
    delay "${delay}ms" "${jitter}ms" distribution normal \
    loss "${loss}%" \
    rate "${rate}mbit"
}

clear_leg() {
  local ns="$1" iface="$2"
  ip netns exec "$ns" tc qdisc del dev "$iface" root 2>/dev/null || true
}

clear_scenario() {
  echo "== Clearing all path shaping =="
  clear_leg "$CLIENT_NS" "$IF_C_WIFI"
  clear_leg "$SERVER_NS" "$IF_S_WIFI"
  clear_leg "$CLIENT_NS" "$IF_C_CELL"
  clear_leg "$SERVER_NS" "$IF_S_CELL"
}

show_scenario() {
  echo "== $IF_C_WIFI (client) =="; ip netns exec "$CLIENT_NS" tc -s qdisc show dev "$IF_C_WIFI"
  echo "== $IF_S_WIFI (server) =="; ip netns exec "$SERVER_NS" tc -s qdisc show dev "$IF_S_WIFI"
  echo "== $IF_C_CELL (client) =="; ip netns exec "$CLIENT_NS" tc -s qdisc show dev "$IF_C_CELL"
  echo "== $IF_S_CELL (server) =="; ip netns exec "$SERVER_NS" tc -s qdisc show dev "$IF_S_CELL"
}

set_scenario() {
  local name="$1"
  case "$name" in

    baseline-symmetric)
      apply_leg "$CLIENT_NS" "$IF_C_WIFI" 5 1 1 100
      apply_leg "$SERVER_NS" "$IF_S_WIFI" 5 1 1 100
      apply_leg "$CLIENT_NS" "$IF_C_CELL" 25 2 0.1 20
      apply_leg "$SERVER_NS" "$IF_S_CELL" 25 2 0.1 20
      ;;

    asymmetric-cell-degraded)
      apply_leg "$CLIENT_NS" "$IF_C_WIFI" 5 1 1 100
      apply_leg "$SERVER_NS" "$IF_S_WIFI" 5 1 1 100
      apply_leg "$CLIENT_NS" "$IF_C_CELL" 90 15 6 3
      apply_leg "$SERVER_NS" "$IF_S_CELL" 90 15 6 3
      ;;

    asymmetric-wifi-lossy)
      apply_leg "$CLIENT_NS" "$IF_C_WIFI" 5 3 8 100
      apply_leg "$SERVER_NS" "$IF_S_WIFI" 5 3 8 100
      apply_leg "$CLIENT_NS" "$IF_C_CELL" 25 2 0.1 20
      apply_leg "$SERVER_NS" "$IF_S_CELL" 25 2 0.1 20
      ;;

    *)
      echo "Unknown scenario: $name" >&2
      echo "Valid scenarios: baseline-symmetric, asymmetric-cell-degraded, asymmetric-wifi-lossy" >&2
      return 1
      ;;
  esac
  echo "Applied scenario: $name"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "${1:-}" in
    set)   set_scenario "${2:?scenario name required}" ;;
    clear) clear_scenario ;;
    show)  show_scenario ;;
    *) echo "Usage: $0 {set <scenario>|clear|show}"; exit 1 ;;
  esac
fi
