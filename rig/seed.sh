#!/usr/bin/env bash
# Validate and deploy the fixture flows and the forwarder (enabled in the rig copy only),
# then prove both Kestra secrets resolve. Usage: rig/seed.sh [--no-forwarder]
set -euo pipefail
. "$(dirname "$0")/_env.sh"
cd "$RIG_DIR"
if [ "${1:-}" = "--no-forwarder" ]; then "$PY" seed.py; else "$PY" seed.py --enable-forwarder; fi
