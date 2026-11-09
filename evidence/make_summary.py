#!/usr/bin/env python3
"""Render evidence/E2E-SUMMARY.md from evidence/e2e-results.json, manifest.json and the optional extra runs.

The README pastes this table. Every number is read from the result files; nothing is typed by hand.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
ORDER = ['S1', 'S1b', 'S2', 'S3', 'S4', 'S5', 'S6a', 'S6b', 'S7', 'S8', 'S9', 'S10', 'S11', 'S12']


def flat_ids(ids: dict) -> str:
    keep = []
    for key, value in ids.items():
        if isinstance(value, dict):
            inner = ', '.join(f'{k}={v}' for k, v in value.items() if k in ('execution', 'replay', 'parent_execution'))
            if inner:
                keep.append(f'{key}: {inner}')
        elif key in ('failing', 'incident', 'original_execution', 'replay_execution', 'execution', 'killed_execution', 'stale_execution') and value:
            keep.append(f'{key}={value}')
    return '<br>'.join(keep[:5])


def main() -> None:
    results_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'evidence/e2e-results.json'
    results = json.loads(results_path.read_text())
    manifest_path = ROOT / 'evidence/manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    by = {s['id']: s for s in results['scenarios']}
    lines = ['# End-to-end summary', '']
    commit = (manifest.get('tested_commit') or {})
    lines.append(f"Kestra {results['kestra_version']}, Hermes `{(manifest.get('versions') or {}).get('hermes_commit', '?')[:10]}` (main), plugin commit "
                 f"`{commit.get('commit', '?')[:12]}`{' (DRAFT: tracked files differ from this commit)' if manifest.get('draft') else ''}. "
                 f"Run on {results['started']}, {results['seconds']} s, {'all passed' if results['all_passed'] else 'FAILURES'}.")
    lines += ['', '| Scenario | What it proves | Assertions | Key ids |', '|---|---|---|---|']
    total_ok = total = 0
    for sid in ORDER:
        s = by.get(sid)
        if not s:
            continue
        ok = sum(a['ok'] for a in s['assertions'])
        total_ok += ok
        total += len(s['assertions'])
        lines.append(f"| {sid} | {s['title']} | {ok}/{len(s['assertions'])} {'PASS' if s['passed'] else 'FAIL'} | {flat_ids(s['ids'])} |")
    lines += ['', f'Total: {total_ok}/{total} assertions.', '']
    extra = ROOT / 'evidence/v023-results.json'
    if extra.exists():
        v = json.loads(extra.read_text())
        lines.append(f"Hermes v0.23.0 compatibility run (scenarios {', '.join(s['id'] for s in v['scenarios'])}): "
                     f"{'all passed' if v['all_passed'] else 'FAILURES'}, "
                     + ', '.join(f"{s['id']} {sum(a['ok'] for a in s['assertions'])}/{len(s['assertions'])}" for s in v['scenarios']) + '.')
        lines.append('')
    qs = ROOT / 'evidence/quickstart-check.json'
    if qs.exists():
        q = json.loads(qs.read_text())
        lines += ['## README quick start, run in order on an empty HERMES_HOME', '', '| Step | Seconds | Result |', '|---|---|---|']
        for step in q['steps']:
            lines.append(f"| {step['step'].replace('|', '/')} | {step['seconds']} | {'ok' if step['ok'] else 'FAIL'} |")
        lines += ['', f"Total {q['total_seconds']} s; all steps completed: {q['completed_all_steps']}. Substitutions: " + '; '.join(q['substitutions']) + '.', '']
    lines += ['## Not covered', ''] + [f'- {g}' for g in manifest.get('unverified_gaps', [])]
    (ROOT / 'evidence/E2E-SUMMARY.md').write_text('\n'.join(lines) + '\n')
    print('wrote evidence/E2E-SUMMARY.md', total_ok, '/', total)


if __name__ == '__main__':
    main()
