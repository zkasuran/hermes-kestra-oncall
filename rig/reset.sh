#!/usr/bin/env bash
# Return the demo to a clean start: delete executions and flows in hko.demo, hko.e2e, hko.lookalike
# (created by the rig only), clear the plugin store, then re-seed. Never touches other namespaces.
# Stops the gateway first so no runner holds the store. Usage: rig/reset.sh --yes
set -euo pipefail
. "$(dirname "$0")/_env.sh"
[ "${1:-}" = "--yes" ] || { echo "refusing without --yes (deletes rig flows and executions)"; exit 2; }
"$RIG_DIR/down.sh"
cd "$RIG_DIR"
"$PY" reset.py
"$RIG_DIR/seed.sh"
