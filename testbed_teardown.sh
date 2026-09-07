#!/usr/bin/env bash
set -uo pipefail
for ns in client server; do
  if ip netns list | grep -q "^${ns}\b"; then
    ip netns del "$ns"
    echo "Removed namespace: $ns"
  else
    echo "Namespace $ns not present"
  fi
done
