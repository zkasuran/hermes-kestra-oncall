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
from common import API, HOME, RUN, save_json  # noqa: E402

PY = '/home/asuran/hko-rig/hermes-agent/.venv/bin/python'
DRIVER = ROOT / 'tests/e2e/hermes_driver.py'
DB = HOME / 'plugin-data/kestra-oncall/data.db'
GATEWAY_LOG = RUN / 'gateway.log'
EVIDENCE = ROOT / 'evidence'
FORWARDER = ('hko.system', 'hermes_oncall_forwarder')
ETL_BASE = (ROOT / 'kestra/demo/orders_etl.yaml').read_text()
TERMINAL = ('SUCCESS', 'FAILED', 'KILLED', 'WARNING', 'CANCELLED')
LOCK_TTL_S = 30 + 120  # settings replay_timeout_seconds (30) + 120, see incidents.apply_fix


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
    env = dict(os.environ, HERMES_HOME=str(HOME), PYTHONDONTWRITEBYTECODE='1')
    proc = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=timeout)
    lines = [line for line in proc.stdout.splitlines() if line.startswith('{')]
    if not lines:
        raise RuntimeError(f'driver produced no JSON (exit {proc.returncode}): {proc.stderr[-800:]}')
    out = json.loads(lines[-1])
    out['exit_code'] = proc.returncode
    return out


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
        refused = tool('kestra_propose_fix', {'incident_id': prow['incident_id'], 'new_source': (ROOT / 'kestra/demo/protected_ledger.yaml').read_text().replace('finance operator', 'finance operator.'), 'rationale': 'e2e probe'})['output']
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
    api.deploy(probe)
    try:
        before = len(api.executions(*FORWARDER))
        failed = api.wait(api.trigger(ns, fid)['id'])
        time.sleep(15)
        after = len(api.executions(*FORWARDER))
        sc.check('a FAILED execution in hko.system does not fire the forwarder', failed['state']['current'] == 'FAILED' and after == before, {'before': before, 'after': after})
        sc.ids['selffail_execution'] = failed['id']
    finally:
        for row in api.executions(ns, fid):
            api.request('DELETE', api.prefix + '/executions/' + row['id'])
        api.request('DELETE', api.prefix + f'/flows/{ns}/{fid}')
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

    denied = drive('command', f'apply {incident_id}', answer='deny')
    sc.check('human DENY: nothing written', 'Not approved' in str(denied['output']) and flow(api, ns, fid)['revision'] == base_rev and len(api.executions(ns, fid)) == n_exec, denied['output'])
    sc.check('approval prompt shows the STORED diff, base revision and hash', denied['approval_calls'] == 1 and sha_ in denied['prompts'][0]['description'] and '+      column = "total_amount"' in denied['prompts'][0]['description'] and f"Base revision: {base_rev}" in denied['prompts'][0]['description'], denied['prompts'][:1])

    for platform in ('webhook', 'cron'):
        blocked = tool('kestra_apply_fix', {'incident_id': incident_id, 'proposal_sha256': sha_}, platform=platform)
        sc.check(f'unattended {platform} run cannot apply (no prompt, no write)', blocked['approval_calls'] == 0 and 'error' in json.dumps(blocked['output']).lower() and flow(api, ns, fid)['revision'] == base_rev and len(api.executions(ns, fid)) == n_exec, blocked['output'])

    wrong = tool('kestra_apply_fix', {'incident_id': incident_id, 'proposal_sha256': 'f' * 64})
    sc.check('a hash that is not the stored proposal is blocked before any prompt', wrong['approval_calls'] == 0 and flow(api, ns, fid)['revision'] == base_rev, wrong['output'])

    applied = drive('command', f'apply {incident_id}', answer='once')
    out = applied['output']
    sc.check('human approval: apply returns verified', f'{incident_id}: verified' in str(out), str(out)[:300])
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
    again = drive('command', f"apply {s2['incident_id']}", answer='once')
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
    res = drive('command', f'apply {incident_id}', answer='once')
    after = flow(api, ns, fid)
    sc.check('apply refused as stale', 'stale' in json.dumps(res['output']).lower(), str(res['output'])[:400])
    sc.check('no write: revision and source are still the out-of-band edit', after['revision'] == oob['revision'] and after['source'] == edited_source, after['revision'])
    sc.check('no revision added and no replay started', len(revisions(api, ns, fid)) == n_rev and len(api.executions(ns, fid)) == n_exec)
    row = incident_row(failed['id'])
    sc.check('incident status stale', row and row['status'] == 'stale', row and row['status'])
    sc.ids.update(flow=f'{ns}.{fid}', original_execution=failed['id'], incident=incident_id, proposal_sha256=proposal.get('proposal_sha256'))
    # recovery path: propose again against the current source, apply, verify
    again = propose_scripted(sc, incident_id, fixed_etl(after['source']), 'e2e S4: re-propose on the current revision')
    res2 = drive('command', f'apply {incident_id}', answer='once')
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
    res = drive('command', f'apply {incident_id}', answer='once')
    out = json.dumps(res['output'])
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
    drive('command', f'apply {incident_id}', answer='once')
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
    crash = drive('command', f'apply {incident_id}', answer='once', fault='crash-before-replay')
    sc.check('apply process was killed at the replay call (exit 137)', crash['exit_code'] == 137 and crash.get('fault'), crash)
    after = flow(api, ns, fid)
    sc.check('flow update had landed (revision +1, source = proposal)', after['revision'] == base['revision'] + 1 and after['source'] == proposal['new_source'] if 'new_source' in proposal else after['revision'] == base['revision'] + 1 and after['source'] == fixed_etl(base['source']), after['revision'])
    sc.check('no replay was started', not replays_of(api, ns, fid, failed['id']))
    row = incident_row(failed['id'])
    sc.check('incident left in status applying with a persisted step', row and row['status'] == 'applying', row and row['status'])
    lock = lock_row(incident_id)
    sc.check('the dead process left a lock row (expires later)', bool(lock) and lock['expires'] > time.time(), lock)
    busy = drive('command', f'apply {incident_id}', answer='once')
    sc.check('immediate retry while the lock is live: busy, no write, no replay', 'busy' in json.dumps(busy['output']).lower() and not replays_of(api, ns, fid, failed['id']) and flow(api, ns, fid)['revision'] == after['revision'], str(busy['output'])[:300])
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
    print(f'  ... waiting {int(wait)}s for the dead process lock to expire (ttl {LOCK_TTL_S}s)', flush=True)
    time.sleep(wait)
    ns, fid, incident_id = s6['ns'], s6['fid'], s6['incident_id']
    res = drive('command', f'apply {incident_id}', answer='once')
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
    drive('command', f'apply {incident_id}', answer='once')
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

    denied = drive('command', f'resume {ex["id"]} approved=true reason=denied-by-human', answer='deny')
    sc.check('human DENY: still PAUSED', 'Not approved' in str(denied['output']) and api.execution(ex['id'])['state']['current'] == 'PAUSED', denied['output'])
    for platform in ('webhook', 'cron'):
        blocked = drive('command', f'resume {ex["id"]} approved=true reason=x', platform=platform, answer='once')
        sc.check(f'unattended {platform} run cannot resume (no prompt)', blocked['approval_calls'] == 0 and api.execution(ex['id'])['state']['current'] == 'PAUSED', str(blocked['output'])[:200])
    bad = drive('command', f'resume {ex["id"]} approved=maybe reason=x', answer='once')
    sc.check('invalid input value refused by Kestra validation, execution still PAUSED', api.execution(ex['id'])['state']['current'] == 'PAUSED', str(bad['output'])[:300])

    ok = drive('command', f'resume {ex["id"]} approved=true "reason=Customer confirmed damaged item"', answer='once')
    sc.check('approval prompt shows the question and the inputs', ok['approval_calls'] == 1 and 'Approve refund A100' in ok['prompts'][0]['description'] and 'Customer confirmed damaged item' in ok['prompts'][0]['description'], ok['prompts'][:1])
    final = api.wait(ex['id'], states=TERMINAL, timeout=60)
    sc.check('execution left PAUSED and finished SUCCESS', final['state']['current'] == 'SUCCESS', final['state']['current'])
    outputs = api.task_outputs(ex['id'])
    decision = json.dumps(outputs.get('record_decision'))
    sc.check('downstream task saw approved=true and the reason', 'true' in decision.lower() and 'Customer confirmed damaged item' in decision, outputs.get('record_decision'))
    resumed_runs = [r for r in final['taskRunList'] if r['taskId'] == 'approval']
    sc.check('the paused task run ended SUCCESS (one run, not re-created)', len(resumed_runs) == 1 and resumed_runs[0]['state']['current'] == 'SUCCESS')
    repeat = drive('command', f'resume {ex["id"]} approved=true "reason=Customer confirmed damaged item"', answer='once')
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
    killed = drive('command', f'kill {ex3["id"]}', answer='once')
    ended = api.wait(ex3['id'], states=TERMINAL, timeout=60)
    sc.check('kill (approved) ends the paused execution KILLED', bool(ended) and ended['state']['current'] == 'KILLED', ended and ended['state']['current'])
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
        futures = [pool.submit(drive, 'command', f'apply {incident_id}', answer='once') for _ in range(2)]
        outs = [f.result() for f in futures]
    texts = [json.dumps(o['output']) for o in outs]
    sc.check('both sessions were asked and approved', all(o['approval_calls'] == 1 for o in outs), [o['approval_calls'] for o in outs])
    sc.check('at least one session reports verified', any(f'{incident_id}: verified' in str(o['output']) for o in outs), [t[:160] for t in texts])
    sc.check('the other session either waited out (verified/no-op) or reported busy; none errored', all(('verified' in t) or ('busy' in t.lower()) or ('Already' in t) for t in texts), [t[:200] for t in texts])
    after = flow(api, ns, fid)
    sc.check('exactly one new flow revision', after['revision'] == base['revision'] + 1 and after['source'] == fixed_etl(base['source']), {'base': base['revision'], 'now': after['revision']})
    replays = replays_of(api, ns, fid, failed['id'])
    sc.check('exactly one replay execution', len(replays) == 1, [r['id'] for r in replays])
    steps = journal_steps(incident_id)
    sc.check('journal has one write_fix and one replay', steps.count('write_fix') == 1 and steps.count('replay') == 1, steps)
    sc.ids.update(flow=f'{ns}.{fid}', original_execution=failed['id'], incident=incident_id, replay_execution=replays[0]['id'] if replays else None)
    return sc


# ---------------------------------------------------------------- main

SCENARIOS = [('S1', s1_detect), ('S1b', s1b_hardening), ('S2', s2_repair), ('S3', s3_idempotent), ('S6a', s6_crash), ('S4', s4_stale),
             ('S5', s5_rollback), ('S7', s7_pause), ('S8', s8_concurrent), ('S6b', s6_resume)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', default='', help='comma list, e.g. S4,S5 (S6 runs both phases)')
    parser.add_argument('--out', default=str(EVIDENCE / 'e2e-results.json'))
    args = parser.parse_args()
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
            needs_quiet = sid in ('S4', 'S5', 'S6a', 'S6b', 'S8')
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
