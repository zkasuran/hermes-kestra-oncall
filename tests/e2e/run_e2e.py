#!/usr/bin/env python3
"""End-to-end scenarios S1..S7 for kestra-oncall against the live rig.

Needs: Kestra 2.0.9 (hko-kestra), the rig gateway (rig/up.sh) and a clean state (rig/reset.sh --yes then
rig/up.sh). Every scenario reads its facts back from Kestra's API (revisions, executions, task runs,
outputs) and from the plugin's SQLite store; nothing is taken from the plugin's own reply alone.

    python tests/e2e/run_e2e.py                  # all scenarios
    python tests/e2e/run_e2e.py --only S4,S5     # a subset (S2/S3 need S1 first)

Result: evidence/e2e-results.json (and one line per assertion on stdout). Exit code 1 if any assertion fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'rig'))
from common import API, HERMES, HOME, RUN, render_forwarder, save_json  # noqa: E402

PY = str(HERMES / '.venv/bin/python')
DRIVER = ROOT / 'tests/e2e/hermes_driver.py'
DB = HOME / 'plugin-data/kestra-oncall/data.db'
GATEWAY_LOG = RUN / 'gateway.log'
EVIDENCE = ROOT / 'evidence'
FORWARDER = ('hko.system', 'hermes_oncall_forwarder')
ETL_BASE = (ROOT / 'kestra/demo/orders_etl.yaml').read_text()
TERMINAL = ('SUCCESS', 'FAILED', 'KILLED', 'WARNING', 'CANCELLED')
SLOW_SECONDS = 45
REPLAY_TIMEOUT_S = 30  # rig/hermes_home.py: replay_timeout_seconds
LOCK_TTL_S = REPLAY_TIMEOUT_S + 120  # incidents.apply_fix: replay_timeout_s + 120
SKIP_LOCK_WAIT = False  # --skip-lock-wait: expire the dead process's lock row instead of waiting (quick re-runs only)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Scenario:
    """Collects assertions; a failed assertion never aborts later ones."""

    def __init__(self, sid: str, title: str) -> None:
        self.sid, self.title = sid, title
        self.assertions: list[dict] = []
        self.ids: dict = {}
        self.notes: list[str] = []
        self.started = time.time()

    def check(self, name: str, ok: bool, detail=None) -> bool:
        self.assertions.append({'name': name, 'ok': bool(ok), 'detail': detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {self.sid} {name}" + ('' if ok else f'  -> {json.dumps(detail, default=str)[:400]}'), flush=True)
        return bool(ok)

    def result(self) -> dict:
        return {'id': self.sid, 'title': self.title, 'passed': all(a['ok'] for a in self.assertions) and bool(self.assertions),
                'seconds': round(time.time() - self.started, 1), 'ids': self.ids, 'notes': self.notes,
                'assertions': self.assertions}


# ---------------------------------------------------------------- helpers

def drive(*argv: str, platform: str = 'cli', answer: str = 'once', fault: str = '', timeout: int = 240) -> dict:
    """Run one Hermes-side action in its own process and return the parsed JSON line."""
    cmd = [PY, str(DRIVER), '--platform', platform, '--answer', answer]
    if fault:
        cmd += ['--fault', fault]
    cmd += list(argv)
    env = dict(os.environ, HERMES_HOME=str(HOME), HKO_HERMES_DIR=str(HERMES), PYTHONDONTWRITEBYTECODE='1')
    proc = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=timeout)
    lines = [line for line in proc.stdout.splitlines() if line.startswith('{')]
    if not lines:
        raise RuntimeError(f'driver produced no JSON (exit {proc.returncode}): {proc.stderr[-800:]}')
    out = json.loads(lines[-1])
    out['exit_code'] = proc.returncode
    return out


def typed_apply(incident_id: str, proposal_sha: str, **kw) -> dict:
    """What a person types in chat: the typed command with the printed hash prefix is the approval."""
    return drive('command', f'apply {incident_id} {proposal_sha[:12]}', **kw)


def tool(name: str, args: dict, **kw) -> dict:
    result = drive('tool', name, json.dumps(args), **kw)
    if isinstance(result['output'], str):
        try:
            result['output'] = json.loads(result['output'])
        except ValueError:
            pass
    return result


def db() -> sqlite3.Connection:
    if not DB.exists():  # the plugin creates its store on first use
        conn = sqlite3.connect(':memory:')
        for ddl in ('incidents(incident_id, execution_id, namespace, flow_id, status, failed_task_id, failed_task_run_id)',
                    'proposals(incident_id)', 'journal(seq, subject, step, detail)', 'locks(name, owner, expires)'):
            conn.execute('CREATE TABLE ' + ddl)
        conn.row_factory = sqlite3.Row
        return conn
    conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def journal_steps(subject: str) -> list[str]:
    with db() as conn:
        return [r['step'] for r in conn.execute('SELECT step FROM journal WHERE subject=? ORDER BY seq', (subject,))]


def incident_row(execution_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute('SELECT * FROM incidents WHERE execution_id=?', (execution_id,)).fetchone()
    return dict(row) if row else None


def proposal_count(incident_id: str) -> int:
    with db() as conn:
        return conn.execute('SELECT COUNT(*) FROM proposals WHERE incident_id=?', (incident_id,)).fetchone()[0]


def lock_row(incident_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute('SELECT * FROM locks WHERE name=?', (incident_id,)).fetchone()
    return dict(row) if row else None


def flow(api: API, ns: str, fid: str, revision: int | None = None) -> dict:
    suffix = f'&revision={revision}' if revision else ''
    return api.request('GET', api.prefix + f'/flows/{ns}/{fid}?source=true{suffix}')


def revisions(api: API, ns: str, fid: str) -> list[dict]:
    return api.request('GET', api.prefix + f'/flows/{ns}/{fid}/revisions')


def replays_of(api: API, ns: str, fid: str, original_id: str) -> list[dict]:
    # Kestra 2.0.9 stamps originalId on the original itself once it has been replayed; exclude it.
    return [e for e in api.executions(ns, fid) if e.get('originalId') == original_id and e['id'] != original_id]


def runs_by_task(execution: dict) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for run in execution.get('taskRunList') or []:
        found.setdefault(run['taskId'], []).append(run)
    return found


def attempt_fingerprint(run: dict) -> list[tuple]:
    return [(a['state']['current'], a['state'].get('startDate'), a['state'].get('endDate')) for a in run.get('attempts') or []]


def deploy_copy(api: API, base: str, ns: str, fid: str, replace: tuple[str, str] | None = None) -> dict:
    """Install a scenario copy of a fixture flow under hko.e2e (id and namespace rewritten)."""
    source = re.sub(r'^id: .*$', f'id: {fid}', base, count=1, flags=re.M)
    source = re.sub(r'^namespace: .*$', f'namespace: {ns}', source, count=1, flags=re.M)
    if replace:
        assert replace[0] in source
        source = source.replace(*replace)
    return api.deploy(source)


def set_forwarder(api: API, enabled: bool) -> int:
    ns, fid = FORWARDER
    current = api.request('GET', api.prefix + f'/flows/{ns}/{fid}?source=true')
    src = current['source']
    wanted = src.replace('disabled: true', 'disabled: false', 1) if enabled else src.replace('disabled: false', 'disabled: true', 1)
    if wanted != src:
        return api.request('PUT', api.prefix + f'/flows/{ns}/{fid}', wanted.encode(), 'application/x-yaml')['revision']
    return current['revision']


def fixed_etl(source: str) -> str:
    assert 'column = "amount"' in source
    return source.replace('column = "amount"', 'column = "total_amount"')


def uuid_tag() -> str:
    import uuid
    return uuid.uuid4().hex[:6]


def poll(fn, timeout: float, every: float = 2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(every)
    return None


def fail_run(api: API, ns: str, fid: str) -> dict:
    ex = api.trigger(ns, fid)
    return api.wait(ex['id'])


def open_scripted(sc: Scenario, execution_id: str) -> str:
    """Open an incident through the real tool path (what the model does first)."""
    opened = tool('kestra_incident_open', {'execution_id': execution_id})['output']
    sc.check('incident opened', isinstance(opened, dict) and opened.get('incident_id', '').startswith('INC-'), opened if not isinstance(opened, dict) else opened.get('error'))
    return opened['incident_id']


def propose_scripted(sc: Scenario, incident_id: str, new_source: str, rationale: str) -> dict:
    out = tool('kestra_propose_fix', {'incident_id': incident_id, 'new_source': new_source, 'rationale': rationale})['output']
    sc.check('proposal stored with hash', isinstance(out, dict) and len(out.get('proposal_sha256', '')) == 64, out)
    return out


def replay_proof(api: API, sc: Scenario, original_id: str, replay_id: str, failed_task: str, load_task: str | None) -> None:
    """Protected-state proof: earlier tasks are reused, the failed task recovered, later tasks ran, original untouched."""
    original, replay = api.execution(original_id), api.execution(replay_id)
    o_runs, r_runs = runs_by_task(original), runs_by_task(replay)
    sc.check('original execution still FAILED (audit trail)', original['state']['current'] == 'FAILED', original['state']['current'])
    sc.check('replay is a new execution of the fixed revision', replay['id'] != original['id'] and replay.get('originalId') == original['id'], {'replay': replay['id'], 'originalId': replay.get('originalId')})
    sc.check('replay execution SUCCESS', replay['state']['current'] == 'SUCCESS', replay['state']['current'])
    extract_same = attempt_fingerprint(o_runs['extract'][0]) == attempt_fingerprint(r_runs['extract'][0])
    sc.check('extract reused: identical attempt timestamps in original and replay', extract_same,
             {'original': attempt_fingerprint(o_runs['extract'][0]), 'replay': attempt_fingerprint(r_runs['extract'][0])})
    sc.check('extract not re-run: exactly one attempt', len(r_runs['extract'][0]['attempts']) == 1, len(r_runs['extract'][0]['attempts']))
    o_out, r_out = api.task_outputs(original_id), api.task_outputs(replay_id)
    sc.check('extract outputs identical in original and replay', o_out.get('extract') == r_out.get('extract') and bool(r_out.get('extract')),
             {'original': o_out.get('extract'), 'replay': r_out.get('extract')})
    attempts = [a[0] for a in attempt_fingerprint(r_runs[failed_task][0])]
    sc.check(f'{failed_task} history FAILED then SUCCESS in the replay', attempts == ['FAILED', 'SUCCESS'], attempts)
    if load_task:
        load = r_runs.get(load_task)
        sc.check(f'{load_task} ran after the repair', bool(load) and load[0]['state']['current'] == 'SUCCESS', load and load[0]['state']['current'])
        sc.check(f'{load_task} did not exist in the original', load_task not in o_runs, list(o_runs))
        summary = api.request('GET', api.prefix + f"/outputs/tasks/{replay_id}")
        sc.check('load wrote its summary output', any(row['taskId'] == load_task for row in summary or []), summary)
    sc.ids.update(original_execution=original_id, replay_execution=replay_id)


# ---------------------------------------------------------------- S1 detect

def s1_detect(api: API, state: dict) -> Scenario:
    sc = Scenario('S1', 'detect: failure -> forwarder -> Hermes webhook run (real LLM) -> incident + proposal; healthy, lookalike, protected create no proposal')
    t0 = time.time()
    failing = api.wait(api.trigger('hko.demo', 'orders_etl')['id'])
    healthy = api.wait(api.trigger('hko.demo', 'healthy_report')['id'])
    lookalike = api.wait(api.trigger('hko.lookalike', 'orders_etl')['id'])
    protected = api.wait(api.trigger('hko.demo', 'protected_ledger')['id'])
    states = {'orders_etl': failing['state']['current'], 'healthy_report': healthy['state']['current'],
              'lookalike': lookalike['state']['current'], 'protected_ledger': protected['state']['current']}
    sc.ids.update(failing=failing['id'], healthy=healthy['id'], lookalike=lookalike['id'], protected=protected['id'])
    sc.check('fixture end states', states == {'orders_etl': 'FAILED', 'healthy_report': 'SUCCESS', 'lookalike': 'FAILED', 'protected_ledger': 'FAILED'}, states)

    watched = {failing['id'], healthy['id'], lookalike['id'], protected['id']}

    def forwarded() -> dict:
        seen: dict[str, dict] = {}
        for ex in api.executions(*FORWARDER):
            full = api.execution(ex['id'])
            target = (full.get('trigger') or {}).get('variables', {}).get('executionId')
            if target in watched:
                seen[target] = full
        return seen if {failing['id'], protected['id']} <= set(seen) else {}

    seen = poll(forwarded, 90) or {}
    time.sleep(8)  # give a wrongly fired forwarder time to show up for the healthy and lookalike runs
    seen = forwarded() or seen
    sc.check('forwarder fired for orders_etl and protected_ledger', {failing['id'], protected['id']} <= set(seen), sorted(seen))
    sc.check('forwarder did NOT fire for healthy_report or the hko.lookalike failure', healthy['id'] not in seen and lookalike['id'] not in seen, sorted(seen))
    sc.check('each forwarder run succeeded (webhook accepted)', all(v['state']['current'] == 'SUCCESS' for v in seen.values()), {k: v['state']['current'] for k, v in seen.items()})
    sc.ids['forwarder_executions'] = {k: v['id'] for k, v in seen.items()}

    log = GATEWAY_LOG.read_text(errors='replace') if GATEWAY_LOG.exists() else ''
    mine = [line for line in log.splitlines() if failing['id'] in line or protected['id'] in line]
    sc.check('Hermes gateway received and coalesced the webhook for orders_etl', any('coalesce settled' in l and failing['id'] in l for l in mine), len(mine))
    sc.check('no webhook run for healthy or lookalike', not any(healthy['id'] in l or lookalike['id'] in l for l in log.splitlines()))
    EVIDENCE.joinpath('logs').mkdir(parents=True, exist_ok=True)
    EVIDENCE.joinpath('logs/s1_gateway.txt').write_text('\n'.join(l[:400] for l in mine) + '\n')

    row = poll(lambda: (incident_row(failing['id']) if (incident_row(failing['id']) and proposal_count(incident_row(failing['id'])['incident_id'])) else None), 420, 5)
    sc.check('incident + proposal exist in the plugin store for orders_etl (written by the model through the tools)', bool(row), None)
    if row:
        incident_id = row['incident_id']
        sc.ids['incident'] = incident_id
        shown = tool('kestra_incidents', {'incident_id': incident_id})['output']
        proposal = shown.get('proposal') or {}
        sc.check('incident bundle names the failed task', row['failed_task_id'] == 'transform', row['failed_task_id'])
        sc.check('proposal is the one-line column fix', '-      column = "amount"' in proposal.get('diff', '') and '+      column = "total_amount"' in proposal.get('diff', ''), proposal.get('diff'))
        sc.check('proposal stats: 1 line added, 1 removed', proposal.get('diff_stat', {}).get('added') == 1 and proposal.get('diff_stat', {}).get('removed') == 1, proposal.get('diff_stat'))
        sc.check('unattended run did not change the flow', flow(api, 'hko.demo', 'orders_etl')['revision'] == proposal.get('base_revision'), flow(api, 'hko.demo', 'orders_etl')['revision'])
        sc.check('no replay exists yet (nothing applied by the webhook run)', not replays_of(api, 'hko.demo', 'orders_etl', failing['id']))
        state['s1'] = {'incident_id': incident_id, 'execution_id': failing['id'], 'proposal': proposal, 'base_revision': proposal.get('base_revision')}

    # protected flow: diagnose only
    prow = incident_row(protected['id'])
    if prow is None:  # the model may not have opened it yet; open it through the same tool path
        tool('kestra_incident_open', {'execution_id': protected['id']})
        prow = incident_row(protected['id'])
    sc.check('protected_ledger incident exists (diagnosis allowed)', prow is not None)
    if prow:
        refused = tool('kestra_propose_fix', {'incident_id': prow['incident_id'], 'new_source': (ROOT / 'kestra/demo/protected_ledger.yaml').read_text().replace('namespace: demo', 'namespace: hko.demo').replace('finance operator', 'finance operator.'), 'rationale': 'e2e probe'})['output']
        sc.check('propose_fix refuses a protected flow', isinstance(refused, dict) and 'protected' in json.dumps(refused).lower(), refused)
        sc.check('no proposal stored for the protected flow', proposal_count(prow['incident_id']) == 0, proposal_count(prow['incident_id']))
        sc.check('protected_ledger flow untouched (revision unchanged)', flow(api, 'hko.demo', 'protected_ledger')['revision'] == state['revisions']['protected_ledger'], flow(api, 'hko.demo', 'protected_ledger')['revision'])
        sc.ids['protected_incident'] = prow['incident_id']

    # lookalike and healthy are refused by policy and leave no incident rows
    bad1 = tool('kestra_incident_open', {'execution_id': lookalike['id']})['output']
    bad2 = tool('kestra_incident_open', {'execution_id': healthy['id']})['output']
    sc.check('lookalike (hko.lookalike) refused by namespace policy', isinstance(bad1, dict) and 'error' in bad1, bad1)
    sc.check('healthy SUCCESS execution refused (not FAILED)', isinstance(bad2, dict) and 'error' in bad2, bad2)
    sc.check('no incident rows for lookalike or healthy', incident_row(lookalike['id']) is None and incident_row(healthy['id']) is None)
    sc.notes.append(f'seconds from trigger to proposal: {round(time.time() - t0)}')
    return sc


# ---------------------------------------------------------------- S1b webhook hardening and no loops

def s1b_hardening(api: API, state: dict) -> Scenario:
    sc = Scenario('S1b', 'webhook hardening: wrong token rejected, duplicate delivery ignored, a failure of the forwarder namespace never loops')
    import urllib.error
    import urllib.request
    from common import env_file, SECRETS
    token = env_file(SECRETS)['HKO_WEBHOOK_TOKEN']
    url = 'http://10.250.90.1:28644/webhooks/kestra-oncall'
    s1 = state.get('s1')
    if not s1:
        sc.check('S1 provided an execution id', False, 'run S1 first')
        return sc
    body = json.dumps({'event_type': 'kestra.execution.failed', 'execution_id': s1['execution_id'], 'namespace': 'hko.demo',
                       'flow_id': 'orders_etl', 'flow_revision': 1, 'state': 'FAILED'}).encode()

    def post(header_token: str | None, request_id: str):
        headers = {'Content-Type': 'application/json', 'X-Request-ID': request_id}
        if header_token is not None:
            headers['X-Gitlab-Token'] = header_token
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method='POST'), timeout=15) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    status, _ = post('not-the-token', 'e2e-bad-token')
    sc.check('wrong token is rejected (401)', status == 401, status)
    status, _ = post(None, 'e2e-no-token')
    sc.check('missing token is rejected (401)', status == 401, status)
    status, payload = post(token, s1['execution_id'])
    sc.check('same delivery id as the real forward is ignored as duplicate (no second Hermes run)', status == 200 and payload.get('status') == 'duplicate', {'status': status, 'payload': payload})

    # a failure inside hko.system (the forwarder's own namespace) must not trigger the forwarder
    ns, fid = 'hko.system', 'rig_selffail'
    probe = 'id: rig_selffail\nnamespace: hko.system\ntasks:\n  - id: boom\n    type: io.kestra.plugin.core.execution.Fail\n    errorMessage: forwarder-namespace failure, must not loop\n'
    template = (ROOT / 'kestra/flows/hermes_oncall_forwarder.yaml').read_text()
    # Misconfigured on purpose: a scratch copy whose pattern DOES match its own namespace. Only the
    # in-flow guard (trigger.namespace != flow.namespace) stands between this and an endless loop.
    scratch = render_forwarder(template, flow_id='rig_selfguard_fwd', namespaces="['hko.system']")
    api.deploy(probe)
    api.deploy(scratch)
    try:
        before = len(api.executions(*FORWARDER))
        time.sleep(20)  # let Kestra register both Flow triggers
        failed = api.wait(api.trigger(ns, fid)['id'])
        time.sleep(20)
        after = len(api.executions(*FORWARDER))
        sc.check('a FAILED execution in hko.system does not fire the real forwarder (pattern excludes it)', failed['state']['current'] == 'FAILED' and after == before, {'before': before, 'after': after})
        guard_runs = api.executions('hko.system', 'rig_selfguard_fwd')
        sc.check('misconfigured copy (pattern matches its own namespace): fires once, ends SUCCESS, no loop', len(guard_runs) == 1 and guard_runs[0]['state']['current'] == 'SUCCESS', [(r['id'], r['state']['current']) for r in guard_runs])
        if guard_runs:
            tasks = {r['taskId']: r['state']['current'] for r in api.execution(guard_runs[0]['id'])['taskRunList']}
            sc.check('guard skipped the HTTP call (no notify_hermes task run)', 'notify_hermes' not in tasks and 'skip_own_namespace' in tasks, tasks)
        sc.ids['selffail_execution'] = failed['id']
    finally:
        for f in (fid, 'rig_selfguard_fwd'):
            for row in api.executions(ns, f):
                api.request('DELETE', api.prefix + '/executions/' + row['id'])
            api.request('DELETE', api.prefix + f'/flows/{ns}/{f}')
    return sc


# ---------------------------------------------------------------- S2 approve and repair, S3 idempotent retry

def s2_repair(api: API, state: dict) -> Scenario:
    sc = Scenario('S2', 'approve and repair: deny, unattended refusal, then approved apply -> one revision, one replay from transform, extract reused, load ran')
    s1 = state.get('s1')
    if not s1:
        sc.check('S1 produced a proposal to apply', False, 'run S1 first')
        return sc
    incident_id, original_id, proposal = s1['incident_id'], s1['execution_id'], s1['proposal']
    ns, fid = 'hko.demo', 'orders_etl'
    base_rev = flow(api, ns, fid)['revision']
    n_exec = len(api.executions(ns, fid))
    sha_ = proposal['proposal_sha256']

    nohash = drive('command', f'apply {incident_id}')
    sc.check('typed apply without the hash is refused (review first)', 'Review first' in str(nohash['output']) and flow(api, ns, fid)['revision'] == base_rev, str(nohash['output'])[:200])
    badhash = drive('command', f'apply {incident_id} deadbeef0000')
    sc.check('typed apply with a hash that is not the stored proposal: refused, nothing written', 'does not match' in str(badhash['output']) and flow(api, ns, fid)['revision'] == base_rev and len(api.executions(ns, fid)) == n_exec, str(badhash['output'])[:200])
    denied = tool('kestra_apply_fix', {'incident_id': incident_id, 'proposal_sha256': sha_}, answer='deny')
    sc.check('model tool path, human DENY on the approval card: nothing written', 'error' in json.dumps(denied['output']).lower() and flow(api, ns, fid)['revision'] == base_rev and len(api.executions(ns, fid)) == n_exec, denied['output'])
    card = denied['prompts'][0]['description'] if denied['prompts'] else ''
    sc.check('approval card shows the STORED diff, base revision, change summary and hash', denied['approval_calls'] == 1 and sha_ in card and '+      column = "total_amount"' in card and f"Base revision: {base_rev}" in card and 'Summary:' in card, card[:600])

    for platform in ('webhook', 'cron'):
        blocked = tool('kestra_apply_fix', {'incident_id': incident_id, 'proposal_sha256': sha_}, platform=platform)
        sc.check(f'unattended {platform} run cannot apply (no prompt, no write)', blocked['approval_calls'] == 0 and 'error' in json.dumps(blocked['output']).lower() and flow(api, ns, fid)['revision'] == base_rev and len(api.executions(ns, fid)) == n_exec, blocked['output'])

    wrong = tool('kestra_apply_fix', {'incident_id': incident_id, 'proposal_sha256': 'f' * 64})
    sc.check('a hash that is not the stored proposal is blocked before any prompt', wrong['approval_calls'] == 0 and flow(api, ns, fid)['revision'] == base_rev, wrong['output'])

    applied = typed_apply(incident_id, sha_)
    out = applied['output']
    sc.check('typed command with the printed hash prefix (the human approval): apply returns verified', f'{incident_id}: verified' in str(out), str(out)[:300])
    fixed = flow(api, ns, fid)
    sc.check('exactly one new flow revision', fixed['revision'] == base_rev + 1, {'before': base_rev, 'after': fixed['revision']})
    sc.check('flow source equals the approved proposal bytes', fixed['source'] == proposal['new_source'])
    replays = replays_of(api, ns, fid, original_id)
    sc.check('exactly one replay execution of the original', len(replays) == 1, [r['id'] for r in replays])
    sc.check('total executions grew by exactly one', len(api.executions(ns, fid)) == n_exec + 1, {'before': n_exec, 'after': len(api.executions(ns, fid))})
    if replays:
        replay_proof(api, sc, original_id, replays[0]['id'], 'transform', 'load')
        sc.check('replay ran the fixed revision', replays[0]['flowRevision'] == base_rev + 1, replays[0]['flowRevision'])
    steps = journal_steps(incident_id)
    sc.check('store journal records write then replay then verified', [s for s in steps if s in ('write_fix', 'fix_written', 'replay', 'replay_started', 'verified')] == ['write_fix', 'fix_written', 'replay', 'replay_started', 'verified'], steps)
    row = incident_row(original_id)
    sc.check('incident status verified', row and row['status'] == 'verified', row and row['status'])
    state['s2'] = {'rev_after': fixed['revision'], 'executions_after': len(api.executions(ns, fid)), 'steps': steps, 'incident_id': incident_id, 'sha': sha_, 'replay_id': replays[0]['id'] if replays else None}
    return sc


def s3_idempotent(api: API, state: dict) -> Scenario:
    sc = Scenario('S3', 'idempotent retry: apply the same proposal again -> no revision, no replay, no journal growth')
    s2 = state.get('s2')
    if not s2:
        sc.check('S2 completed', False, 'run S2 first')
        return sc
    ns, fid = 'hko.demo', 'orders_etl'
    before = {'rev': flow(api, ns, fid)['revision'], 'revisions': len(revisions(api, ns, fid)), 'execs': len(api.executions(ns, fid)), 'steps': journal_steps(s2['incident_id'])}
    again = typed_apply(s2['incident_id'], s2['sha'])
    after = {'rev': flow(api, ns, fid)['revision'], 'revisions': len(revisions(api, ns, fid)), 'execs': len(api.executions(ns, fid)), 'steps': journal_steps(s2['incident_id'])}
    sc.check('second apply reports a no-op', '"noop": true' in str(again['output']).lower() or 'Already verified' in str(again['output']), str(again['output'])[:300])
    sc.check('no new flow revision', after['rev'] == before['rev'] and after['revisions'] == before['revisions'], {'before': before, 'after': after})
    sc.check('no new execution (no second replay)', after['execs'] == before['execs'], {'before': before['execs'], 'after': after['execs']})
    sc.check('journal did not grow with write or replay steps', after['steps'] == before['steps'], after['steps'][len(before['steps']):])
    tool_again = tool('kestra_apply_fix', {'incident_id': s2['incident_id'], 'proposal_sha256': s2['sha']})
    sc.check('same via the model tool path: still a no-op', len(api.executions(ns, fid)) == before['execs'] and flow(api, ns, fid)['revision'] == before['rev'], str(tool_again['output'])[:200])
    return sc


# ---------------------------------------------------------------- S4 stale proposal

def s4_stale(api: API, state: dict) -> Scenario:
    sc = Scenario('S4', 'stale proposal: flow edited out of band after propose -> apply refused, no write')
    ns, fid = 'hko.e2e', 'e2e_orders_stale'
    deploy_copy(api, ETL_BASE, ns, fid)
    failed = fail_run(api, ns, fid)
    sc.check('scenario flow failed as designed', failed['state']['current'] == 'FAILED', failed['state']['current'])
    incident_id = open_scripted(sc, failed['id'])
    base = flow(api, ns, fid)
    proposal = propose_scripted(sc, incident_id, fixed_etl(base['source']), 'e2e S4: rename column mapping')
    # out-of-band edit: someone else changes the flow (description) after the proposal was made
    edited_source = base['source'].replace('description: A supplier renamed', 'description: Edited out of band. A supplier renamed', 1)
    oob = api.request('PUT', api.prefix + f'/flows/{ns}/{fid}', edited_source.encode(), 'application/x-yaml')
    sc.check('out-of-band edit created a new revision', oob['revision'] == base['revision'] + 1, oob['revision'])
    n_exec, n_rev = len(api.executions(ns, fid)), len(revisions(api, ns, fid))
    res = typed_apply(incident_id, proposal['proposal_sha256'])
    after = flow(api, ns, fid)
    sc.check('apply refused as stale', 'stale' in json.dumps(res['output']).lower(), str(res['output'])[:400])
    sc.check('no write: revision and source are still the out-of-band edit', after['revision'] == oob['revision'] and after['source'] == edited_source, after['revision'])
    sc.check('no revision added and no replay started', len(revisions(api, ns, fid)) == n_rev and len(api.executions(ns, fid)) == n_exec)
    row = incident_row(failed['id'])
    sc.check('incident status stale', row and row['status'] == 'stale', row and row['status'])
    sc.ids.update(flow=f'{ns}.{fid}', original_execution=failed['id'], incident=incident_id, proposal_sha256=proposal.get('proposal_sha256'))
    # recovery path: propose again against the current source, apply, verify
    again = propose_scripted(sc, incident_id, fixed_etl(after['source']), 'e2e S4: re-propose on the current revision')
    res2 = typed_apply(incident_id, again['proposal_sha256'])
    sc.check('after a fresh proposal on the current revision the apply verifies', f'{incident_id}: verified' in str(res2['output']), str(res2['output'])[:300])
    sc.check('that fix landed on top of the out-of-band edit (edit preserved)', 'Edited out of band' in flow(api, ns, fid)['source'] and 'total_amount' in flow(api, ns, fid)['source'])
    return sc


# ---------------------------------------------------------------- S5 rollback

def s5_rollback(api: API, state: dict) -> Scenario:
    sc = Scenario('S5', 'rollback: a proposal that validates but still fails -> replay FAILED -> flow back to the base source')
    ns, fid = 'hko.e2e', 'e2e_orders_rollback'
    deploy_copy(api, ETL_BASE, ns, fid)
    failed = fail_run(api, ns, fid)
    incident_id = open_scripted(sc, failed['id'])
    base = flow(api, ns, fid)
    wrong = base['source'].replace('column = "amount"', 'column = "net_amount"')  # valid YAML and plugin config, still fails at runtime
    proposal = propose_scripted(sc, incident_id, wrong, 'e2e S5: deliberately wrong column to exercise rollback')
    sc.check('Kestra validation accepts the wrong fix', proposal.get('validation') == [], proposal.get('validation'))
    n_rev = len(revisions(api, ns, fid))
    res = tool('kestra_apply_fix', {'incident_id': incident_id, 'proposal_sha256': proposal['proposal_sha256']}, answer='once')
    out = json.dumps(res['output'])
    sc.check('model tool path, human approves the card once: exactly one prompt', res['approval_calls'] == 1, res['prompts'][:1])
    after = flow(api, ns, fid)
    sc.check('result is rolled_back', 'rolled_back' in out, out[:400])
    sc.check('flow source is byte-identical to the base source again', after['source'] == base['source'], {'base_sha': sha(base['source']), 'now_sha': sha(after['source'])})
    sc.check('revision history: fix then rollback (exactly two new revisions)', after['revision'] == base['revision'] + 2 and len(revisions(api, ns, fid)) == n_rev + 2, {'base': base['revision'], 'now': after['revision']})
    sc.check('the middle revision holds the wrong fix (history preserved)', flow(api, ns, fid, base['revision'] + 1)['source'] == wrong)
    replays = replays_of(api, ns, fid, failed['id'])
    sc.check('exactly one replay and it FAILED', len(replays) == 1 and replays[0]['state']['current'] == 'FAILED', [(r['id'], r['state']['current']) for r in replays])
    sc.check('original execution unchanged (FAILED)', api.execution(failed['id'])['state']['current'] == 'FAILED')
    row = incident_row(failed['id'])
    sc.check('incident status rolled_back', row and row['status'] == 'rolled_back', row and row['status'])
    n_exec = len(api.executions(ns, fid))
    typed_apply(incident_id, proposal['proposal_sha256'])
    sc.check('re-running the same apply after rollback is a no-op', len(api.executions(ns, fid)) == n_exec and flow(api, ns, fid)['revision'] == after['revision'])
    sc.ids.update(flow=f'{ns}.{fid}', original_execution=failed['id'], incident=incident_id, replay_execution=replays[0]['id'] if replays else None)
    return sc


# ---------------------------------------------------------------- S6 interrupted apply (two phases)

def s6_crash(api: API, state: dict) -> Scenario:
    sc = Scenario('S6a', 'interrupted apply, phase 1: process killed after the flow update, before the replay')
    ns, fid = 'hko.e2e', 'e2e_orders_crash'
    deploy_copy(api, ETL_BASE, ns, fid)
    failed = fail_run(api, ns, fid)
    incident_id = open_scripted(sc, failed['id'])
    base = flow(api, ns, fid)
    proposal = propose_scripted(sc, incident_id, fixed_etl(base['source']), 'e2e S6: fix, then crash before the replay')
    crash = typed_apply(incident_id, proposal['proposal_sha256'], fault='crash-before-replay')
    sc.check('apply process was killed at the replay call (exit 137)', crash['exit_code'] == 137 and crash.get('fault'), crash)
    after = flow(api, ns, fid)
    sc.check('flow update had landed (revision +1, source = proposal)', after['revision'] == base['revision'] + 1 and after['source'] == proposal['new_source'] if 'new_source' in proposal else after['revision'] == base['revision'] + 1 and after['source'] == fixed_etl(base['source']), after['revision'])
    sc.check('no replay was started', not replays_of(api, ns, fid, failed['id']))
    row = incident_row(failed['id'])
    sc.check('incident left in status applying with a persisted step', row and row['status'] == 'applying', row and row['status'])
    lock = lock_row(incident_id)
    sc.check('the dead process left a lock row (expires later)', bool(lock) and lock['expires'] > time.time(), lock)
    busy = typed_apply(incident_id, proposal['proposal_sha256'])
    sc.check('immediate retry while the lock is live: busy, no write, no replay', 'another apply is running' in str(busy['output']) and not replays_of(api, ns, fid, failed['id']) and flow(api, ns, fid)['revision'] == after['revision'], str(busy['output'])[:300])
    state['s6'] = {'ns': ns, 'fid': fid, 'incident_id': incident_id, 'original': failed['id'], 'fixed_revision': after['revision'], 'sha': proposal['proposal_sha256'], 'lock_expires': lock['expires'] if lock else None, 'n_revisions': len(revisions(api, ns, fid))}
    sc.ids.update(flow=f'{ns}.{fid}', original_execution=failed['id'], incident=incident_id)
    return sc


def s6_resume(api: API, state: dict) -> Scenario:
    sc = Scenario('S6b', 'interrupted apply, phase 2: next apply adopts the written fix and does exactly one replay')
    s6 = state.get('s6')
    if not s6:
        sc.check('phase 1 completed', False, 'run S6a first')
        return sc
    wait = max(0, (s6['lock_expires'] or 0) - time.time()) + 3
    if SKIP_LOCK_WAIT:
        conn = sqlite3.connect(str(DB))
        with conn:
            conn.execute('UPDATE locks SET expires = ? WHERE name = ?', (time.time() - 1, s6['incident_id']))
        conn.close()
        sc.notes.append('lock expiry forced with --skip-lock-wait (not the real wait)')
    else:
        print(f'  ... waiting {int(wait)}s for the dead process lock to expire (ttl {LOCK_TTL_S}s)', flush=True)
        time.sleep(wait)
    ns, fid, incident_id = s6['ns'], s6['fid'], s6['incident_id']
    res = typed_apply(incident_id, s6['sha'])
    out = json.dumps(res['output'])
    sc.check('resumed apply verified', f'{incident_id}: verified' in str(res['output']), out[:400])
    steps = journal_steps(incident_id)
    # 'replay' is the intent entry written before the client call; the killed process wrote one, the resume wrote one.
    sc.check('resumed from the persisted step: one write_fix, one fix_written, one replay_started in the whole journal (no second update, no second replay)',
             steps.count('write_fix') == 1 and steps.count('fix_written') == 1 and steps.count('replay_started') == 1 and steps[-1] == 'verified', steps)
    sc.check('flow revision unchanged since the crash (no extra update)', flow(api, ns, fid)['revision'] == s6['fixed_revision'] and len(revisions(api, ns, fid)) == s6['n_revisions'])
    replays = replays_of(api, ns, fid, s6['original'])
    sc.check('exactly one replay execution', len(replays) == 1, [r['id'] for r in replays])
    if replays:
        replay_proof(api, sc, s6['original'], replays[0]['id'], 'transform', 'load')
    count_before = len(api.executions(ns, fid))
    typed_apply(incident_id, s6['sha'])
    sc.check('another apply afterwards is a no-op', len(api.executions(ns, fid)) == count_before)
    return sc


# ---------------------------------------------------------------- S7 pause

def s7_pause(api: API, state: dict) -> Scenario:
    sc = Scenario('S7', 'pause: PAUSED refund_approval appears in /kestra pauses, resumes with inputs, downstream sees them; stale guard when resumed out of band first')
    ns, fid = 'hko.demo', 'refund_approval'
    ex = api.trigger(ns, fid)
    paused = api.wait(ex['id'], states=('PAUSED', 'SUCCESS', 'FAILED', 'KILLED'))
    sc.check('execution reached PAUSED at the approval task', paused['state']['current'] == 'PAUSED', paused['state']['current'])
    listing = drive('command', 'pauses')
    sc.check('/kestra pauses lists the execution and the question', ex['id'] in str(listing['output']) and 'Approve refund A100' in str(listing['output']), str(listing['output'])[:300])
    rows = tool('kestra_pauses', {})['output']
    mine = [r for r in rows.get('pauses', []) if r.get('execution_id') == ex['id']]
    sc.check('pause row carries task run id, declared inputs and flow identity', len(mine) == 1 and bool(mine[0].get('paused_task_run_id')) and 'approved' in json.dumps(mine[0]) and 'reason' in json.dumps(mine[0]) and mine[0].get('flow_id') == fid, mine[:1])

    trun = mine[0]['paused_task_run_id'] if mine else ''
    short = trun[:12]
    inputs = {'approved': 'true', 'reason': 'Customer confirmed damaged item'}
    denied = tool('kestra_pause_decide', {'execution_id': ex['id'], 'paused_task_run_id': trun, 'action': 'resume', 'inputs': inputs}, answer='deny')
    sc.check('model tool path, human DENY on the card: still PAUSED', 'error' in json.dumps(denied['output']).lower() and api.execution(ex['id'])['state']['current'] == 'PAUSED', denied['output'])
    card = denied['prompts'][0]['description'] if denied['prompts'] else ''
    sc.check('approval card shows the question and the inputs', denied['approval_calls'] == 1 and 'Approve refund A100' in card and 'Customer confirmed damaged item' in card, card[:500])
    for platform in ('webhook', 'cron'):
        blocked = tool('kestra_pause_decide', {'execution_id': ex['id'], 'paused_task_run_id': trun, 'action': 'resume', 'inputs': inputs}, platform=platform)
        typed = drive('command', f'resume {ex["id"]} {short} approved=true reason=x', platform=platform)
        sc.check(f'unattended {platform} run cannot resume (tool and typed command; no prompt)', blocked['approval_calls'] == 0 and typed['approval_calls'] == 0 and api.execution(ex['id'])['state']['current'] == 'PAUSED', [str(blocked['output'])[:120], str(typed['output'])[:120]])
    noprefix = drive('command', f'resume {ex["id"]} approved=true reason=x')
    sc.check('typed resume without the task run prefix is refused', 'Give at least' in str(noprefix['output']) or 'task run' in str(noprefix['output']) and api.execution(ex['id'])['state']['current'] == 'PAUSED', str(noprefix['output'])[:200])
    bad = drive('command', f'resume {ex["id"]} {short} approved=maybe reason=x')
    sc.check('invalid input value refused by Kestra validation, execution still PAUSED', api.execution(ex['id'])['state']['current'] == 'PAUSED', str(bad['output'])[:300])

    ok = drive('command', f'resume {ex["id"]} {short} approved=true "reason=Customer confirmed damaged item"')
    sc.check('typed resume (the human approval) writes once, no Hermes card', ok['approval_calls'] == 0, str(ok['output'])[:200])
    final = api.wait(ex['id'], states=TERMINAL, timeout=60)
    sc.check('execution left PAUSED and finished SUCCESS', final['state']['current'] == 'SUCCESS', final['state']['current'])
    outputs = api.task_outputs(ex['id'])
    decision = json.dumps(outputs.get('record_decision'))
    sc.check('downstream task saw approved=true and the reason', 'true' in decision.lower() and 'Customer confirmed damaged item' in decision, outputs.get('record_decision'))
    resumed_runs = [r for r in final['taskRunList'] if r['taskId'] == 'approval']
    sc.check('the paused task run ended SUCCESS (one run, not re-created)', len(resumed_runs) == 1 and resumed_runs[0]['state']['current'] == 'SUCCESS')
    repeat = drive('command', f'resume {ex["id"]} {short} approved=true "reason=Customer confirmed damaged item"')
    sc.check('repeating the resume writes nothing (execution is no longer paused)', api.execution(ex['id'])['state']['current'] == 'SUCCESS' and 'No unique current pause' in str(repeat['output']), str(repeat['output'])[:200])
    sc.ids.update(execution=ex['id'], flow=f'{ns}.{fid}')

    # stale guard: snapshot, someone else resumes first, then the plugin tries with its old snapshot
    ex2 = api.trigger(ns, fid)
    p2 = api.wait(ex2['id'], states=('PAUSED', 'SUCCESS', 'FAILED', 'KILLED'))
    snap = tool('kestra_pauses', {})['output']
    row = next(r for r in snap['pauses'] if r['execution_id'] == ex2['id'])
    oob = api.request('POST', api.prefix + f'/executions/{ex2["id"]}/actions/resume',
                      b'--b\r\nContent-Disposition: form-data; name="approved"\r\n\r\ntrue\r\n--b\r\nContent-Disposition: form-data; name="reason"\r\n\r\nresumed out of band\r\n--b--\r\n',
                      'multipart/form-data; boundary=b')
    settled = api.wait(ex2['id'], states=TERMINAL, timeout=60)
    sc.check('out-of-band resume happened first', bool(settled) and settled['state']['current'] == 'SUCCESS', settled and settled['state']['current'])
    runs_before = len(api.execution(ex2['id'])['taskRunList'])
    late = tool('kestra_pause_decide', {'execution_id': ex2['id'], 'paused_task_run_id': row['paused_task_run_id'], 'action': 'resume', 'inputs': {'approved': 'false', 'reason': 'late human'}})
    sc.check('late decision from the old snapshot is refused: no longer paused', 'no longer paused' in json.dumps(late['output']), str(late['output'])[:300])
    final2 = api.execution(ex2['id'])
    sc.check('no second write: same task runs, the out-of-band inputs stand', len(final2['taskRunList']) == runs_before and 'resumed out of band' in json.dumps(api.task_outputs(ex2['id']).get('record_decision')), api.task_outputs(ex2['id']).get('record_decision'))
    sc.ids['stale_execution'] = ex2['id']
    # kill path
    ex3 = api.trigger(ns, fid)
    api.wait(ex3['id'], states=('PAUSED', 'SUCCESS', 'FAILED', 'KILLED'))
    kill_row = next(r for r in tool('kestra_pauses', {})['output']['pauses'] if r['execution_id'] == ex3['id'])
    killed = drive('command', f'kill {ex3["id"]} {kill_row["paused_task_run_id"][:12]}')
    ended = api.wait(ex3['id'], states=TERMINAL, timeout=60)
    sc.check('typed kill ends the paused execution KILLED', bool(ended) and ended['state']['current'] == 'KILLED', ended and ended['state']['current'])
    sc.ids['killed_execution'] = ex3['id']
    return sc


# ---------------------------------------------------------------- S8 concurrent apply

def s8_concurrent(api: API, state: dict) -> Scenario:
    sc = Scenario('S8', 'concurrent apply: two sessions approve the same proposal at the same moment -> one write, one replay')
    import concurrent.futures
    ns, fid = 'hko.e2e', 'e2e_orders_concurrent'
    deploy_copy(api, ETL_BASE, ns, fid)
    failed = fail_run(api, ns, fid)
    incident_id = open_scripted(sc, failed['id'])
    base = flow(api, ns, fid)
    proposal = propose_scripted(sc, incident_id, fixed_etl(base['source']), 'e2e S8: two sessions race to apply')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(typed_apply, incident_id, proposal['proposal_sha256']) for _ in range(2)]
        outs = [f.result() for f in futures]
    texts = [json.dumps(o['output']) for o in outs]
    sc.check('both sessions typed the command (no Hermes card is involved)', all(o['approval_calls'] == 0 for o in outs), [o['approval_calls'] for o in outs])
    sc.check('at least one session reports verified', any(f'{incident_id}: verified' in str(o['output']) for o in outs), [t[:160] for t in texts])
    sc.check('the other session either waited out (verified/no-op) or reported busy; none errored', all(('verified' in t) or ('another apply is running' in t) or ('Already' in t) for t in texts), [t[:200] for t in texts])
    after = flow(api, ns, fid)
    sc.check('exactly one new flow revision', after['revision'] == base['revision'] + 1 and after['source'] == fixed_etl(base['source']), {'base': base['revision'], 'now': after['revision']})
    replays = replays_of(api, ns, fid, failed['id'])
    sc.check('exactly one replay execution', len(replays) == 1, [r['id'] for r in replays])
    steps = journal_steps(incident_id)
    sc.check('journal has one write_fix and one replay', steps.count('write_fix') == 1 and steps.count('replay') == 1, steps)
    sc.ids.update(flow=f'{ns}.{fid}', original_execution=failed['id'], incident=incident_id, replay_execution=replays[0]['id'] if replays else None)
    return sc


# ---------------------------------------------------------------- S9 slow replay, S10 chat surface

SLOW_PATCH = ('      import csv\n', '      import csv\n      import time\n      time.sleep(SECONDS)\n')


def s9_slow_replay(api: API, state: dict) -> Scenario:
    sc = Scenario('S9', 'slow replay: apply returns pending after the wait, the fix is NOT rolled back, the next apply follows the same replay and verifies')
    ns, fid = 'hko.e2e', 'e2e_orders_slow'
    deploy_copy(api, ETL_BASE, ns, fid)
    failed = fail_run(api, ns, fid)
    incident_id = open_scripted(sc, failed['id'])
    base = flow(api, ns, fid)
    # The fixed task sleeps longer than the rig's replay_timeout_seconds (30), a legitimate slow-but-correct fix.
    fixed = fixed_etl(base['source']).replace(SLOW_PATCH[0], SLOW_PATCH[1].replace('SECONDS', str(SLOW_SECONDS)), 1)
    proposal = propose_scripted(sc, incident_id, fixed, 'e2e S9: correct column, task takes longer than the wait')
    t0 = time.time()
    first = typed_apply(incident_id, proposal['proposal_sha256'])
    waited = time.time() - t0
    out = str(first['output'])
    sc.check('first apply returns pending (not rolled back, not verified)', 'replay still running' in out and 'nothing was rolled back' in out, out[:400])
    sc.check('the apply waited about the configured replay timeout before giving up', 25 <= waited <= 90, round(waited, 1))
    after = flow(api, ns, fid)
    sc.check('the fix is still live: revision base+1 and source = proposal', after['revision'] == base['revision'] + 1 and after['source'] == fixed, after['revision'])
    replays = replays_of(api, ns, fid, failed['id'])
    sc.check('exactly one replay exists and it is still RUNNING at that moment', len(replays) == 1 and replays[0]['state']['current'] in ('RUNNING', 'CREATED', 'SUBMITTED'), [(r['id'], r['state']['current']) for r in replays])
    row = incident_row(failed['id'])
    sc.check('incident status applying (following the replay)', row and row['status'] == 'applying', row and row['status'])
    time.sleep(max(0, SLOW_SECONDS - waited) + 12)
    second = typed_apply(incident_id, proposal['proposal_sha256'])
    out2 = json.dumps(second['output'])
    sc.check('second apply with the same hash: verified', f'{incident_id}: verified' in str(second['output']), out2[:400])
    replays = replays_of(api, ns, fid, failed['id'])
    sc.check('still exactly one replay, same id as before', len(replays) == 1 and (not replays or replays[0]['id'] in out2), [r['id'] for r in replays])
    final = flow(api, ns, fid)
    sc.check('no second write and no rollback: revision unchanged, three journal markers absent', final['revision'] == base['revision'] + 1 and 'rollback' not in journal_steps(incident_id), journal_steps(incident_id))
    steps = journal_steps(incident_id)
    sc.check('journal: one write_fix, one replay_started, ends verified', steps.count('write_fix') == 1 and steps.count('replay_started') == 1 and steps[-1] == 'verified', steps)
    if replays:
        replay_proof(api, sc, failed['id'], replays[0]['id'], 'transform', 'load')
    sc.ids.update(flow=f'{ns}.{fid}', incident=incident_id)
    return sc


def s10_chat_surface(api: API, state: dict) -> Scenario:
    sc = Scenario('S10', 'chat surface: the typed command works where no Hermes approval card can exist (telegram session, no notifier)')
    ns, fid = 'hko.e2e', 'e2e_orders_chat'
    deploy_copy(api, ETL_BASE, ns, fid)
    failed = fail_run(api, ns, fid)
    incident_id = open_scripted(sc, failed['id'])
    base = flow(api, ns, fid)
    proposal = propose_scripted(sc, incident_id, fixed_etl(base['source']), 'e2e S10: chat surface')
    shown = drive('command', f'show {incident_id}', platform='telegram')
    sc.check('/kestra show prints the diff and the ready-to-type apply command', f"/kestra apply {incident_id} {proposal['proposal_sha256'][:12]}" in str(shown['output']) and '+      column = "total_amount"' in str(shown['output']), str(shown['output'])[-300:])
    tool_try = tool('kestra_apply_fix', {'incident_id': incident_id, 'proposal_sha256': proposal['proposal_sha256']}, platform='telegram')
    sc.check('the model tool path on this surface cannot use the approval card it has no way to show: nothing written', flow(api, ns, fid)['revision'] == base['revision'] and 'error' in json.dumps(tool_try['output']).lower(), str(tool_try['output'])[:300])
    wrong = drive('command', f'apply {incident_id} 0123456789ab', platform='telegram')
    sc.check('wrong hash on the chat surface: refused, nothing written', 'does not match' in str(wrong['output']) and flow(api, ns, fid)['revision'] == base['revision'], str(wrong['output'])[:200])
    ok = typed_apply(incident_id, proposal['proposal_sha256'], platform='telegram')
    sc.check('typed apply on the chat surface: verified, no approval card raised', f'{incident_id}: verified' in str(ok['output']) and ok['approval_calls'] == 0, str(ok['output'])[:300])
    sc.check('one revision and one replay', flow(api, ns, fid)['revision'] == base['revision'] + 1 and len(replays_of(api, ns, fid, failed['id'])) == 1)
    sc.ids.update(flow=f'{ns}.{fid}', incident=incident_id, original_execution=failed['id'])
    return sc


def s12_real_tui(api: API, state: dict) -> Scenario:
    sc = Scenario('S12', 'real terminal UI: slash commands typed into the actual `hermes chat --cli` in a pseudo-terminal (no scripted callback, no model turn)')
    import shutil
    ns, fid = 'hko.e2e', 'e2e_orders_tui'
    deploy_copy(api, ETL_BASE, ns, fid)
    failed = fail_run(api, ns, fid)
    incident_id = open_scripted(sc, failed['id'])
    base = flow(api, ns, fid)
    proposal = propose_scripted(sc, incident_id, fixed_etl(base['source']), 'e2e S12: real TUI')
    short = proposal['proposal_sha256'][:12]
    env = dict(os.environ, HERMES_HOME=str(HOME), HKO_HERMES_DIR=str(HERMES))
    cmd = [shutil.which('python3') or 'python3', str(ROOT / 'tests/e2e/pty_cli.py'),
           '/kestra status|connected', f'/kestra show {incident_id}|To apply exactly this',
           f'/kestra apply {incident_id} 0123456789ab|does not match', f'/kestra apply {incident_id} {short}|verified']
    proc = subprocess.run(cmd, env=env, cwd=str(ROOT), text=True, capture_output=True, timeout=420)
    lines = [l for l in proc.stdout.splitlines() if l.startswith('{')]
    if not lines:
        sc.check('pty driver produced output (pexpect available in system python3?)', False, proc.stderr[-500:])
        return sc
    out = json.loads(lines[-1])
    by = {r['typed']: r for r in out['results']}
    sc.check('real TUI: /kestra status answered in the terminal', by['/kestra status|connected'.split('|')[0]]['found'])
    sc.check('real TUI: /kestra show printed the diff and the typed apply command', by[f'/kestra show {incident_id}']['found'] and '+      column = "total_amount"' in by[f'/kestra show {incident_id}']['screen'], by[f'/kestra show {incident_id}']['screen'][-300:])
    sc.check('real TUI: a wrong hash is refused (and the journal shows only the one later write)', by[f'/kestra apply {incident_id} 0123456789ab']['found'] and journal_steps(incident_id).count('write_fix') == 1, by[f'/kestra apply {incident_id} 0123456789ab']['screen'][-300:])
    sc.check('real TUI: the typed command with the printed hash prefix applied the fix (verified)', by[f'/kestra apply {incident_id} {short}']['found'], by[f'/kestra apply {incident_id} {short}']['screen'][-300:])
    sc.check('Kestra agrees: one revision, one replay, execution SUCCESS', flow(api, ns, fid)['revision'] == base['revision'] + 1 and len(replays_of(api, ns, fid, failed['id'])) == 1 and replays_of(api, ns, fid, failed['id'])[0]['state']['current'] == 'SUCCESS')
    sc.check('the terminal UI process was still alive after the commands', out.get('alive_at_end') is True)
    sc.ids.update(flow=f'{ns}.{fid}', incident=incident_id, original_execution=failed['id'])
    return sc


# ---------------------------------------------------------------- S11 flow shapes the engine assumes

PARALLEL = """id: FID
namespace: hko.e2e
tasks:
  - id: first
    type: io.kestra.plugin.core.debug.Return
    format: "{{ now() }}"
  - id: par
    type: io.kestra.plugin.core.flow.Parallel
    tasks:
      - id: ok_branch
        type: io.kestra.plugin.core.debug.Return
        format: "{{ now() }}"
      - id: bad_branch
        type: io.kestra.plugin.core.log.Log
        message: "{{ outputs.nosuch.value }}"
  - id: last
    type: io.kestra.plugin.core.log.Log
    message: last
"""
LOOP = """id: FID
namespace: hko.e2e
tasks:
  - id: each
    type: io.kestra.plugin.core.flow.Loop
    values: ["a", "b"]
    concurrencyLimit: 1
    tasks:
      - id: item_task
        type: io.kestra.plugin.core.log.Log
        message: "{{ item.value == 'b' ? outputs.nosuch.value : item.value }}"
"""
CHILD = """id: FID
namespace: hko.e2e
tasks:
  - id: child_task
    type: io.kestra.plugin.core.log.Log
    message: "{{ outputs.nosuch.value }}"
"""
PARENT = """id: FID
namespace: hko.e2e
tasks:
  - id: first
    type: io.kestra.plugin.core.debug.Return
    format: x
  - id: sub
    type: io.kestra.plugin.core.flow.Subflow
    namespace: hko.e2e
    flowId: CHILD
    wait: true
    transmitFailed: true
"""
PAUSE_AFTER = """id: FID
namespace: hko.e2e
tasks:
  - id: one
    type: io.kestra.plugin.core.debug.Return
    format: "{{ now() }}"
  - id: two
    type: io.kestra.plugin.core.log.Log
    message: "{{ outputs.nosuch.value }}"
  - id: gate
    type: io.kestra.plugin.core.flow.Pause
    description: "Looks right?"
"""
WARN = """id: FID
namespace: hko.e2e
tasks:
  - id: first
    type: io.kestra.plugin.core.debug.Return
    format: "{{ now() }}"
  - id: flaky
    type: io.kestra.plugin.core.execution.Fail
    errorMessage: upstream said no
  - id: after
    type: io.kestra.plugin.core.debug.Return
    format: "{{ now() }}"
"""
SINGLE = """id: FID
namespace: hko.e2e
concurrency:
  limit: 1
  behavior: FAIL
tasks:
  - id: hold
    type: io.kestra.plugin.core.flow.Sleep
    duration: PT25S
"""


def s11_shapes(api: API, state: dict) -> Scenario:
    sc = Scenario('S11', 'flow shapes: Parallel branch (fixed and verified), Loop and Subflow (diagnose-only, honest refusal), replay that reaches a Pause (pending then verified), failure that is not a task failure (refused)')
    ns = 'hko.e2e'
    tag = uuid_tag()

    def make(source: str, fid: str, **subs) -> str:
        text = source.replace('FID', fid)
        for k, v in subs.items():
            text = text.replace(k, v)
        api.deploy(text)
        return text

    # Parallel
    fid = 'e2e_shape_par_' + tag
    src = make(PARALLEL, fid)
    failed = fail_run(api, ns, fid)
    inc = open_scripted(sc, failed['id'])
    row = incident_row(failed['id'])
    sc.check('Parallel: the incident names the failing LEAF branch, not the Parallel parent', row and row['failed_task_id'] == 'bad_branch', row and row['failed_task_id'])
    prop = propose_scripted(sc, inc, src.replace('outputs.nosuch.value', 'outputs.first.value'), 'e2e S11 parallel')
    res = typed_apply(inc, prop['proposal_sha256'])
    sc.check('Parallel: apply verified', f'{inc}: verified' in str(res['output']), str(res['output'])[:300])
    reps = replays_of(api, ns, fid, failed['id'])
    if reps:
        rr = runs_by_task(api.execution(reps[0]['id']))
        sc.check('Parallel: bad_branch FAILED then SUCCESS; first and ok_branch reused (one attempt)', [a[0] for a in attempt_fingerprint(rr['bad_branch'][0])] == ['FAILED', 'SUCCESS'] and len(rr['first'][0]['attempts']) == 1 and len(rr['ok_branch'][0]['attempts']) == 1, {k: len(v[0]['attempts']) for k, v in rr.items()})
    sc.ids['parallel'] = {'flow': fid, 'execution': failed['id'], 'replay': reps[0]['id'] if reps else None}

    # Loop
    fid = 'e2e_shape_loop_' + tag
    src = make(LOOP, fid)
    failed = fail_run(api, ns, fid)
    inc = open_scripted(sc, failed['id'])
    shown = tool('kestra_incidents', {'incident_id': inc})['output']
    sc.check('Loop: the incident says why it is diagnose-only and lists the failing sub-execution', 'cannot replay a failed Loop' in json.dumps(shown) and bool(shown.get('child_executions')), json.dumps(shown)[:300])
    n_rev = len(revisions(api, ns, fid))
    refused = tool('kestra_propose_fix', {'incident_id': inc, 'new_source': src.replace('outputs.nosuch.value', 'item.value'), 'rationale': 'e2e S11 loop'})['output']
    sc.check('Loop: propose_fix refused, nothing written', 'cannot replay a failed Loop' in json.dumps(refused) and len(revisions(api, ns, fid)) == n_rev, str(refused)[:300])
    sc.ids['loop'] = {'flow': fid, 'execution': failed['id']}

    # Subflow: parent is diagnose-only, the child is fixed
    child, parent = 'e2e_shape_child_' + tag, 'e2e_shape_parent_' + tag
    csrc = make(CHILD, child)
    make(PARENT, parent, CHILD=child)
    failed = fail_run(api, ns, parent)
    inc = open_scripted(sc, failed['id'])
    shown = tool('kestra_incidents', {'incident_id': inc})['output']
    kids = shown.get('child_executions') or []
    sc.check('Subflow: the parent incident lists the failing child execution', len(kids) == 1 and kids[0].get('flow_id') == child, kids)
    refused = tool('kestra_propose_fix', {'incident_id': inc, 'new_source': PARENT.replace('FID', parent).replace('CHILD', child) + '# x\n', 'rationale': 'e2e S11 subflow'})['output']
    sc.check('Subflow: parent proposal refused with the pointer to the child', 'Open an incident on the child execution' in json.dumps(refused), str(refused)[:300])
    if kids:
        cinc = open_scripted(sc, kids[0]['execution_id'])
        cprop = propose_scripted(sc, cinc, csrc.replace('"{{ outputs.nosuch.value }}"', 'ok'), 'e2e S11 child fix')
        cres = typed_apply(cinc, cprop['proposal_sha256'])
        sc.check('Subflow: the child incident is fixed and verified', f'{cinc}: verified' in str(cres['output']), str(cres['output'])[:300])
    sc.ids['subflow'] = {'parent': parent, 'child': child, 'parent_execution': failed['id']}

    # replay reaches a Pause
    fid = 'e2e_shape_pause_' + tag
    src = make(PAUSE_AFTER, fid)
    failed = fail_run(api, ns, fid)
    inc = open_scripted(sc, failed['id'])
    prop = propose_scripted(sc, inc, src.replace('outputs.nosuch.value', 'outputs.one.value'), 'e2e S11 pause after the fixed task')
    pending = typed_apply(inc, prop['proposal_sha256'])
    sc.check('Pause after the fixed task: apply returns pending with the Pause explanation, nothing rolled back', 'replay still running' in str(pending['output']) and 'PAUSED' in str(pending['output']) and flow(api, ns, fid)['revision'] == prop['base_revision'] + 1, str(pending['output'])[:400])
    reps = replays_of(api, ns, fid, failed['id'])
    if reps:
        api.request('POST', api.prefix + f"/executions/{reps[0]['id']}/actions/resume", b'--b--\r\n', 'multipart/form-data; boundary=b')
        api.wait(reps[0]['id'], states=TERMINAL, timeout=60)
    done = typed_apply(inc, prop['proposal_sha256'])
    sc.check('Pause after the fixed task: after the human resumes the replay, the next apply verifies', f'{inc}: verified' in str(done['output']), str(done['output'])[:300])
    sc.check('Pause after the fixed task: one replay, one write', len(replays_of(api, ns, fid, failed['id'])) == 1 and journal_steps(inc).count('write_fix') == 1, journal_steps(inc))
    sc.ids['pause_after'] = {'flow': fid, 'execution': failed['id'], 'replay': reps[0]['id'] if reps else None}

    # failure that is not a task failure: concurrency limit with behavior FAIL
    fid = 'e2e_shape_conc_' + tag
    make(SINGLE, fid)
    holder = api.trigger(ns, fid)
    time.sleep(2)
    rejected = api.trigger(ns, fid)
    rejected = api.wait(rejected['id'], timeout=30)
    sc.check('Concurrency: the second execution FAILED without any failed task run', rejected['state']['current'] == 'FAILED' and not any(t['state']['current'] == 'FAILED' for t in rejected.get('taskRunList') or []), [t['state']['current'] for t in rejected.get('taskRunList') or []])
    n_rev = len(revisions(api, ns, fid))
    opened = tool('kestra_incident_open', {'execution_id': rejected['id']})['output']
    inc = opened.get('incident_id')
    sc.check('Concurrency: the incident opens read-only and says why no fix is possible', bool(inc) and bool(opened.get('diagnose_only_reason')), json.dumps(opened)[:300])
    refused = tool('kestra_propose_fix', {'incident_id': inc, 'new_source': SINGLE.replace('FID', fid).replace('PT25S', 'PT20S'), 'rationale': 'e2e S11 concurrency'})['output'] if inc else {}
    sc.check('Concurrency: propose refused, no write', 'error' in json.dumps(refused).lower() and len(revisions(api, ns, fid)) == n_rev, str(refused)[:300])
    api.wait(holder['id'], timeout=60)

    # allowFailure: the accepted fix turns the failing task into a WARNING (replay ends WARNING, core C11)
    fid = 'e2e_shape_warn_' + tag
    src = make(WARN, fid)
    failed = fail_run(api, ns, fid)
    inc = open_scripted(sc, failed['id'])
    prop = propose_scripted(sc, inc, src.replace("  - id: flaky\n    type: io.kestra.plugin.core.execution.Fail\n", "  - id: flaky\n    type: io.kestra.plugin.core.execution.Fail\n    allowFailure: true\n"), 'e2e S11 allowFailure')
    res = typed_apply(inc, prop['proposal_sha256'])
    reps = replays_of(api, ns, fid, failed['id'])
    # Observed on 2.0.9: a tolerated failure ends the replay WARNING but the failed task's last attempt is still FAILED,
    # so the engine does not call it recovered. It keeps the fix and hands the call to a human (needs_human).
    sc.check('allowFailure: replay ends WARNING; a tolerated failure is NOT reported as verified (needs_human, fix kept)', bool(reps) and reps[0]['state']['current'] == 'WARNING' and f'{inc}: needs_human' in str(res['output']) and flow(api, ns, fid)['revision'] == prop['base_revision'] + 1, [str(res['output'])[:300], [r['state']['current'] for r in reps]])
    if reps:
        sc.check('allowFailure: the task after the tolerated failure ran in the replay (WARNING path)', runs_by_task(api.execution(reps[0]['id'])).get('after', [{}])[0].get('state', {}).get('current') == 'SUCCESS')
    sc.ids['allow_failure'] = {'flow': fid, 'execution': failed['id'], 'replay': reps[0]['id'] if reps else None}
    sc.ids['not_a_task_failure'] = {'flow': fid, 'execution': rejected['id']}
    return sc


# ---------------------------------------------------------------- main

SCENARIOS = [('S1', s1_detect), ('S1b', s1b_hardening), ('S2', s2_repair), ('S3', s3_idempotent), ('S6a', s6_crash), ('S4', s4_stale),
             ('S5', s5_rollback), ('S7', s7_pause), ('S8', s8_concurrent), ('S9', s9_slow_replay), ('S10', s10_chat_surface), ('S11', s11_shapes), ('S12', s12_real_tui), ('S6b', s6_resume)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', default='', help='comma list, e.g. S4,S5 (S6 runs both phases)')
    parser.add_argument('--out', default=str(EVIDENCE / 'e2e-results.json'))
    parser.add_argument('--skip-lock-wait', action='store_true', help='S6b: force the dead lock to expire instead of waiting about 150 s (not for evidence runs)')
    args = parser.parse_args()
    global SKIP_LOCK_WAIT
    SKIP_LOCK_WAIT = args.skip_lock_wait
    only = {s.strip().upper() for s in args.only.split(',') if s.strip()}
    if 'S6' in only:
        only |= {'S6A', 'S6B'}
    api = API()
    state: dict = {'revisions': {}}
    for fid in ('protected_ledger', 'orders_etl', 'healthy_report', 'refund_approval'):
        state['revisions'][fid] = flow(api, 'hko.demo', fid)['revision']
    version = api.request('GET', '/api/v1/configs')['version']
    results, started = [], time.time()
    fwd_rev_before = flow(api, *FORWARDER)['revision']
    fwd_disabled_before = 'disabled: false' not in flow(api, *FORWARDER)['source']
    print(f'Kestra {version}; forwarder revision {fwd_rev_before}; plugin store {DB}', flush=True)
    quiet = False
    try:
        for sid, fn in SCENARIOS:
            if only and sid.upper() not in only:
                continue
            needs_quiet = sid in ('S4', 'S5', 'S6a', 'S6b', 'S8', 'S9', 'S10', 'S11', 'S12')
            if needs_quiet and not quiet:
                # Scripted proposals must not race the model: pause the forwarder for hko.e2e scenarios only.
                set_forwarder(api, False)
                quiet = True
                time.sleep(3)
            print(f'== {sid}', flush=True)
            try:
                sc = fn(api, state)
            except Exception as exc:  # a crashed scenario is a failed scenario, with the cause recorded
                sc = Scenario(sid, 'crashed')
                sc.check('scenario ran to completion', False, ''.join(traceback.format_exception_only(type(exc), exc)).strip() + ' | ' + traceback.format_exc()[-600:])
            results.append(sc.result())
    finally:
        if quiet:
            set_forwarder(api, not fwd_disabled_before)
    passed = all(r['passed'] for r in results)
    document = {'kestra_version': version, 'started': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(started)),
                'seconds': round(time.time() - started, 1), 'forwarder': {'revision_before': fwd_rev_before, 'revision_after': flow(api, *FORWARDER)['revision'],
                'disabled_during_S4_S5_S6_to_keep_the_model_out_of_scripted_proposals': True},
                'all_passed': passed, 'scenarios': results}
    save_json(Path(args.out), document)
    for r in results:
        bad = [a['name'] for a in r['assertions'] if not a['ok']]
        print(f"{r['id']:4} {'PASS' if r['passed'] else 'FAIL'}  {len(r['assertions']) - len(bad)}/{len(r['assertions'])}  {r['title'][:90]}" + (f'  failed: {bad}' if bad else ''))
    print('ALL PASSED' if passed else 'FAILURES', '->', args.out)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
