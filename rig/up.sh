#!/usr/bin/env bash
# Configure the rig HERMES_HOME and start the Hermes gateway (webhook listener) in the background.
# Idempotent: a running gateway is left alone. Never prints credentials.
set -euo pipefail
. "$(dirname "$0")/_env.sh"
(cd "$RIG_DIR" && "$PY" hermes_home.py && "$PY" deps.py)
if gw_running; then echo "gateway already running pid $(cat "$GW_PID")"; else
  # Guard: nothing else may run this home (one runner rule).
  if pgrep -af "hermes.* gateway run" | grep -q "hko-rig" ; then echo "another rig gateway process exists; see rig/status.sh" >&2; exit 1; fi
  cd "$REPO"
  setsid nohup "$HERMES_BIN" gateway run -v >>"$GW_LOG" 2>&1 < /dev/null &
  echo $! >"$GW_PID"
  echo "gateway started pid $(cat "$GW_PID"), log $GW_LOG"
fi
for _ in $(seq 1 40); do
  if curl -fsS -m 2 "http://$WEBHOOK_HOST:$WEBHOOK_PORT/health" >/dev/null 2>&1; then
    echo "webhook healthy on $WEBHOOK_HOST:$WEBHOOK_PORT"; exit 0; fi
  gw_running || { echo "gateway exited; tail of log:" >&2; tail -20 "$GW_LOG" >&2; exit 1; }
  sleep 1
done
echo "webhook did not become healthy in 40s; tail of log:" >&2; tail -20 "$GW_LOG" >&2; exit 1
