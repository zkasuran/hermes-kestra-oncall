# Shared by the rig scripts. Sourced, never executed. Prints nothing.
RIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$RIG_DIR")"
export HERMES_HOME=/home/asuran/hko-rig/hermes-home
export PYTHONDONTWRITEBYTECODE=1
RUN=/home/asuran/hko-rig/run
HERMES_BIN=/home/asuran/hko-rig/hermes-agent/.venv/bin/hermes
PY=/home/asuran/hko-rig/hermes-agent/.venv/bin/python
GW_PID="$RUN/gateway.pid"
GW_LOG="$RUN/gateway.log"
WEBHOOK_HOST=10.250.90.1
WEBHOOK_PORT=28644
mkdir -p "$RUN"
gw_running() { [ -f "$GW_PID" ] && kill -0 "$(cat "$GW_PID")" 2>/dev/null; }
