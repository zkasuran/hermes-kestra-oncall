#!/usr/bin/env python3
"""Write evidence/manifest.json: bytes, versions, ids, results and the gaps, bound together.

Run after tests/e2e/run_e2e.py. Reads evidence/e2e-results.json, hashes every plugin and fixture file,
asks Kestra for its version and the flows' revisions, and records the Hermes commit. Secrets are
never read; known secret values are redacted from anything written.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'rig'))
from common import API, HERMES, HOME, RUN, save_json  # noqa: E402

PLUGIN_FILES = ['__init__.py', 'plugin.yaml', 'settings.py', 'tools.py', 'hooks.py', 'commands.py', 'cli.py',
                'client.py', 'store.py', 'flowsrc.py', 'incidents.py', 'pauses.py', 'skills/kestra-oncall/SKILL.md']
RIG_FILES = ['kestra/flows/hermes_oncall_forwarder.yaml', 'rig/flows/secret_probe.yaml',
             'kestra/demo/orders_etl.yaml', 'kestra/demo/refund_approval.yaml', 'kestra/demo/healthy_report.yaml',
             'kestra/demo/protected_ledger.yaml', 'kestra/demo/demo_lookalike.yaml',
             'rig/common.py', 'rig/env.example', 'rig/flows/secret_probe.yaml', 'release/export.sh', 'tests/e2e/pty_cli.py', 'rig/hermes_home.py', 'rig/deps.py', 'rig/seed.py', 'rig/reset.py', 'rig/up.sh',
             'rig/down.sh', 'rig/status.sh', 'rig/seed.sh', 'rig/reset.sh', 'rig/_env.sh',
             'tests/e2e/run_e2e.py', 'tests/e2e/hermes_driver.py', 'evidence/make_manifest.py']
GAPS = [
    'The approval a person gives is the typed command (/kestra apply <id> <hash>, resume, kill). It was run through the plugin command handler, '
    'in a telegram-like session with no approval notifier (S10) and typed into the real terminal UI in a pseudo-terminal (S12). No live Telegram, '
    'Discord or Slack client was used, and no person clicked a Hermes approval card: the answer to the card on the model tool path (deny or once) is scripted '
    'through terminal_tool.set_approval_callback, with the card text captured and asserted.',
    'The model-driven webhook run (S1) depends on a shared LLM pool that rate-limits gpt-6-astra; the run falls back to gpt-6.1-sol or gpt-5.6-sol '
    'when the primary is limited (see evidence/logs/gateway_model_tools.txt). The proposal text is the model\'s and can differ between runs.',
    'S4 to S6 and S8 to S12 use scripted proposals (not model written) on hko.e2e copies, with the forwarder paused for those scenarios so the model cannot race them. '
    'S1 to S3 use the model proposal on hko.demo.orders_etl.',
    'S6 simulates the interruption by killing the apply process at the replay call (fault injected by the test driver into KestraClient.replay); '
    'a power loss or SIGKILL at other points is covered only by the unit tests of core.',
    'Concurrent apply (S8) uses two processes on one machine sharing one SQLite store; separate hosts with separate stores would not share the lock.',
    'Hermes v0.23.0 ran S1, S1b, S2 and S3 only (evidence/v023-results.json); the other scenarios ran on Hermes main only.',
    'Kestra OSS with basic auth only; Enterprise namespaces, RBAC and API tokens are untested. Kestra 2.0.9 only.',
    'The README quick start check (evidence/quickstart-check.json) substitutes a local plugin path for the GitHub install line, --deliver log for a chat bot, '
    'a model block written into config.yaml, and rig/deps.py for the aiohttp requirement.',
    'Observed Kestra limits, not defects of the plugin: a failed Loop cannot be replayed by Kestra 2.0.9 (diagnose only); a tolerated failure (allowFailure) '
    'ends the replay WARNING and is handed to a human, not called verified.',
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_ids(results: dict) -> dict:
    ids = {}
    for scenario in results['scenarios']:
        ids[scenario['id']] = scenario['ids']
    return ids


def save_gateway_excerpt() -> None:
    """Model tool calls and fallback decisions from the gateway log (no payloads, no credentials)."""
    log = RUN / 'gateway.log'
    keep = ('agent.tool_executor: tool kestra_', 'Fallback activated', 'webhook_coalesce', 'POST /webhooks/kestra-oncall', 'webhook] Skipping duplicate')
    lines = [l[:300] for l in log.read_text(errors='replace').splitlines() if any(k in l for k in keep)] if log.exists() else []
    out = ROOT / 'evidence/logs/gateway_model_tools.txt'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(lines) + '\n')


def git_state() -> dict:
    """Commit and tree of the code that was tested, and whether any tracked file differs from it."""
    def run(*a):
        return subprocess.run(['git', *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = [l for l in run('status', '--porcelain', '--untracked-files=no', '--', '.', ':!evidence', ':!.work').splitlines() if l]
    return {'commit': run('rev-parse', 'HEAD'), 'tree': run('rev-parse', 'HEAD^{tree}'), 'dirty_tracked_files': dirty,
            'branch': run('rev-parse', '--abbrev-ref', 'HEAD'), 'commit_subject': run('log', '-1', '--format=%s')}


def main() -> None:
    allow_dirty = '--allow-dirty' in sys.argv
    state = git_state()
    if state['dirty_tracked_files'] and not allow_dirty:
        raise SystemExit('refusing: tracked files differ from HEAD, so the evidence would not describe a commit:\n  '
                         + '\n  '.join(state['dirty_tracked_files']) + '\nCommit, rerun the e2e, then run this again (or --allow-dirty for a draft).')
    save_gateway_excerpt()
    api = API()
    results_path = ROOT / 'evidence/e2e-results.json'
    results = json.loads(results_path.read_text()) if results_path.exists() else None
    flows = {}
    for ns, fid in [('hko.demo', 'orders_etl'), ('hko.demo', 'refund_approval'), ('hko.demo', 'healthy_report'),
                    ('hko.demo', 'protected_ledger'), ('hko.lookalike', 'orders_etl'),
                    ('hko.system', 'hermes_oncall_forwarder'), ('hko.system', 'secret_probe'),
                    ('hko.e2e', 'e2e_orders_stale'), ('hko.e2e', 'e2e_orders_rollback'), ('hko.e2e', 'e2e_orders_crash')]:
        try:
            current = api.request('GET', api.prefix + f'/flows/{ns}/{fid}?source=true')
            flows[f'{ns}.{fid}'] = {'revision': current['revision'], 'source_sha256': hashlib.sha256(current['source'].encode()).hexdigest(),
                                    'disabled': bool(current.get('disabled', False))}
        except RuntimeError:
            flows[f'{ns}.{fid}'] = None
    git = lambda *a: subprocess.run(['git', *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    manifest = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'tested_commit': state,
        'draft': bool(state['dirty_tracked_files']),
        'versions': {
            'kestra': api.request('GET', '/api/v1/configs')['version'],
            'hermes_commit': subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=HERMES, capture_output=True, text=True).stdout.strip(),
            'model': 'gpt-6-astra (fallbacks gpt-6.1-sol, gpt-5.6-sol) via the local OpenAI-compatible pool, Responses API',
        },
        'files_sha256': {rel: sha(ROOT / rel) for rel in PLUGIN_FILES + RIG_FILES if (ROOT / rel).exists()},
        'flows_in_kestra_at_manifest_time': flows,
        'inputs_redacted': {'kestra_url': 'http://127.0.0.1:28090', 'tenant': 'main', 'namespaces': ['hko.demo', 'hko.e2e'],
                            'protected_flows': ['hko.demo.protected_ledger'], 'webhook': 'http://10.250.90.1:28644/webhooks/kestra-oncall',
                            'credentials': '[REDACTED] (Kestra basic auth, X-Gitlab-Token, LLM key)'},
        'results': None if results is None else {
            'all_passed': results['all_passed'], 'seconds': results['seconds'],
            'per_scenario': {s['id']: {'passed': s['passed'], 'assertions_ok': sum(a['ok'] for a in s['assertions']),
                                       'assertions_total': len(s['assertions'])} for s in results['scenarios']}},
        'execution_ids_by_scenario': None if results is None else collect_ids(results),
        'logs': sorted(str(p.relative_to(ROOT)) for p in (ROOT / 'evidence').rglob('*') if p.is_file() and p.suffix in ('.txt', '.log')),
        'result_files': ['evidence/e2e-results.json', 'evidence/seed.json'],
        'commands': ['rig/reset.sh --yes', 'rig/up.sh', 'python tests/e2e/run_e2e.py', 'python evidence/make_manifest.py'],
        'unverified_gaps': GAPS,
    }
    save_json(ROOT / 'evidence/manifest.json', manifest)
    print('manifest written:', len(manifest['files_sha256']), 'files hashed;',
          'all_passed =', None if results is None else results['all_passed'])


if __name__ == '__main__':
    main()
