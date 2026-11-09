#!/usr/bin/env python3
"""Merge a partial e2e run into evidence/e2e-results.json, per scenario, and record the commit each scenario ran on.

    python evidence/merge_results.py /tmp/partial.json [--commit SHA] [--unaffected FILE,FILE --reason TEXT]

Scenarios in the partial file replace the same ids in the full file; every other scenario keeps its result and
its recorded commit (the commit in evidence/e2e-run-commit.txt for results that predate this script). The top level
gets `scenario_commits`, the combined `all_passed`, and, when --unaffected is given, a statement of which runtime
files changed since the older scenarios ran and why those scenarios cannot depend on them. make_manifest.py reads it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('partial')
    parser.add_argument('--commit', default='')
    parser.add_argument('--unaffected', default='')
    parser.add_argument('--reason', default='')
    args = parser.parse_args()
    full_path = ROOT / 'evidence/e2e-results.json'
    full = json.loads(full_path.read_text())
    partial = json.loads(Path(args.partial).read_text())
    commit = args.commit or subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    base_commit = (ROOT / 'evidence/e2e-run-commit.txt').read_text().split()[0]
    commits = full.get('scenario_commits') or {s['id']: base_commit for s in full['scenarios']}
    by_id = {s['id']: s for s in full['scenarios']}
    for scenario in partial['scenarios']:
        by_id[scenario['id']] = scenario
        commits[scenario['id']] = commit
    order = [s['id'] for s in full['scenarios']]
    order += [i for i in by_id if i not in order]
    full['scenarios'] = [by_id[i] for i in order]
    full['scenario_commits'] = {i: commits[i] for i in order}
    full['all_passed'] = all(s['passed'] for s in full['scenarios'])
    full['seconds'] = round(sum(s.get('seconds', 0) for s in full['scenarios']), 1)
    full.setdefault('merged_runs', []).append({'partial': Path(args.partial).name, 'commit': commit, 'scenarios': [s['id'] for s in partial['scenarios']],
                                               'started': partial['started']})
    if args.unaffected:
        full['unaffected_runtime_changes'] = {'files': args.unaffected.split(','), 'reason': args.reason,
                                              'applies_to_scenarios_not_rerun': [i for i in order if commits[i] != commit]}
    full_path.write_text(json.dumps(full, indent=2) + '\n')
    print('merged', [s['id'] for s in partial['scenarios']], 'at', commit[:12], '| all_passed =', full['all_passed'])


if __name__ == '__main__':
    main()
