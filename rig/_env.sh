# Shared by the rig scripts. Sourced, never executed. Prints nothing.
RIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$RIG_DIR")"
# Override these three to drive a second Hermes checkout with its own home (see tests/e2e/README.md).
HKO_HERMES_DIR="${HKO_HERMES_DIR:-/home/asuran/hko-rig/hermes-agent}"
export HKO_RIG_HOME="${HKO_RIG_HOME:-/home/asuran/hko-rig/hermes-home}"
export HKO_RIG_RUN="${HKO_RIG_RUN:-/home/asuran/hko-rig/run}"
export HKO_HERMES_DIR
export HERMES_HOME="$HKO_RIG_HOME"
export PYTHONDONTWRITEBYTECODE=1
RUN="$HKO_RIG_RUN"
HERMES_BIN="$HKO_HERMES_DIR/.venv/bin/hermes"
PY="$HKO_HERMES_DIR/.venv/bin/python"
GW_PID="$RUN/gateway.pid"
GW_LOG="$RUN/gateway.log"
WEBHOOK_HOST="${HKO_WEBHOOK_HOST:-10.250.90.1}"
WEBHOOK_PORT="${HKO_WEBHOOK_PORT:-28644}"
export HKO_WEBHOOK_HOST="$WEBHOOK_HOST" HKO_WEBHOOK_PORT="$WEBHOOK_PORT"
mkdir -p "$RUN"
gw_running() { [ -f "$GW_PID" ] && kill -0 "$(cat "$GW_PID")" 2>/dev/null; }
