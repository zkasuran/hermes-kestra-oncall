#!/usr/bin/env bash
# Run the README quick start on an empty HERMES_HOME and a new Kestra container; see quickstart_check.py.
set -euo pipefail
. "$(dirname "$0")/_env.sh"
exec "$PY" "$RIG_DIR/quickstart_check.py" "$@"
