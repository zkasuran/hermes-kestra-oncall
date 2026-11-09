#!/usr/bin/env bash
# Build the tree that goes into the Hermes catalog pin from an ALLOW-LIST of committed files, then validate it.
#
#   release/export.sh [OUT_DIR]        default OUT_DIR=/tmp/kestra-oncall-release
#
# What ships: the plugin runtime, its skill, the dashboard and desktop surfaces, the user-facing Kestra flows
# (forwarder + demo fixtures), assets, README, LICENSE and the unit tests that run without Kestra.
# What stays on the development branch: rig/, tests/e2e, tests/live, evidence/, docs/screenshots, catalog/,
# .work/, SPEC.md, AGENTS.md. The README links to them as "proof on branch X at commit Y".
#
# The export is taken from `git archive HEAD`, never from the working tree, so what is validated is what is
# committed. It prints the commit, the tree sha of the export and the validate output. Exit 1 if the tree is
# dirty for any allow-listed path, or if validation fails.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-/tmp/kestra-oncall-release}"
HERMES_DIR="${HKO_HERMES_DIR:-/home/asuran/hko-rig/hermes-agent}"
ALLOW=(
  __init__.py plugin.yaml settings.py tools.py hooks.py commands.py cli.py client.py store.py flowsrc.py incidents.py pauses.py
  skills dashboard desktop assets README.md LICENSE
  kestra/flows/hermes_oncall_forwarder.yaml kestra/demo
  tests/conftest.py tests/unit
  docs/DEMO.md
)
EXISTING=()
for path in "${ALLOW[@]}"; do [ -e "$path" ] && EXISTING+=("$path"); done

dirty=$(git status --porcelain --untracked-files=no -- "${EXISTING[@]}")
if [ -n "$dirty" ]; then
  echo "refusing: allow-listed paths have uncommitted changes (commit first):" >&2
  echo "$dirty" >&2
  exit 1
fi
commit=$(git rev-parse HEAD)
rm -rf "$OUT"; mkdir -p "$OUT"
git archive HEAD -- "${EXISTING[@]}" | tar -x -C "$OUT"
find "$OUT" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

# a stable digest of the exported bytes: sha256 of the sorted per-file digests
tree_sha=$(cd "$OUT" && find . -type f | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1)
echo "commit      $commit"
echo "export dir  $OUT ($(find "$OUT" -type f | wc -l) files)"
echo "tree sha256 $tree_sha"
printf '%s\n' "$commit" > "$OUT/.export-commit"   # not part of the digest above

echo "--- hermes plugins validate (isolated HERMES_HOME)"
export HERMES_HOME="$(mktemp -d /tmp/hko-release-home.XXXXXX)"
status=0
"$HERMES_DIR/.venv/bin/hermes" plugins validate "$OUT" --install-deps 2>&1 | tee "$OUT/../$(basename "$OUT").validate.txt" || status=$?
rm -rf "$HERMES_HOME"
echo "--- leftovers that must not ship"
if grep -rIl --exclude-dir=tests -E '/home/asuran|10\.250\.90\.|hko-rig|HKO_' "$OUT" 2>/dev/null; then
  echo "WARNING: the files above mention rig paths or addresses" >&2
fi
exit "$status"
