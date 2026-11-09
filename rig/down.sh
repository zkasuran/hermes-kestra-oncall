#!/usr/bin/env bash
# Stop the rig gateway. Leaves HERMES_HOME, the plugin store and Kestra untouched.
set -euo pipefail
. "$(dirname "$0")/_env.sh"
if gw_running; then
  pid=$(cat "$GW_PID"); kill "$pid"
  for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" || true
  echo "gateway stopped"
else echo "gateway not running"; fi
rm -f "$GW_PID"
