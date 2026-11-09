#!/usr/bin/env python3
"""Run the README quick start, in order, on an empty HERMES_HOME and a brand-new Kestra container, and time it.

    python rig/quickstart_check.py [--keep]        (use the rig venv python)

What it does, with the README's own commands where they can run here:
  1 start Kestra 2.0.9 (basic auth + the two forwarder secrets) in a new container on a free loopback port
  2 install the plugin: the README line is `hermes plugins install zkasuran/hermes-kestra-oncall`; the repo is
    not published yet, so this symlinks the checkout into the new home and runs `hermes plugins enable`
  3 `hermes config set ...` for the settings and credentials, then `hermes kestra-oncall doctor`
  4 `hermes kestra-oncall setup --apply --deliver log`, WEBHOOK_SECRET, webhooks enabled, start the gateway
  5 deploy the shipped forwarder with the README's sed | curl pipeline
  6 deploy the demo flows with the README's loop and run orders_etl
  7 wait for the model's proposal, then type `/kestra apply <id> <hash>` into the real terminal UI
Each step prints its wall-clock seconds; the table is saved to evidence/quickstart-check.json. The only
substitutions: a local plugin path for step 2, `--deliver log` for the chat target in step 4 (no Telegram bot
on this machine), a model block in config.yaml (the README assumes Hermes already has a model), and a
private port pair so it cannot collide with the main rig.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'rig'))
from common import HERMES, save_json  # noqa: E402

PY = HERMES / '.venv/bin/python'
HERMES_BIN = HERMES / '.venv/bin/hermes'
NAME = 'hko-qs-kestra'
def free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


KPORT, WPORT = free_port(), free_port()  # Kestra on loopback, Hermes webhook on all interfaces
USER, PASSWORD = 'admin@example.com', 'change-me-Aa1'  # the README's example credentials, not real ones
OPENCODE = Path.home() / '.config/opencode/opencode.json'


def sh(cmd: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=kw.pop('timeout', 120), **kw)


def http(method: str, path: str, data: bytes | None = None, ctype: str = 'application/json'):
    token = base64.b64encode(f'{USER}:{PASSWORD}'.encode()).decode()
    req = urllib.request.Request(f'http://127.0.0.1:{KPORT}{path}', data=data, method=method,
                                 headers={'Authorization': 'Basic ' + token, 'Content-Type': ctype})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    return json.loads(body) if body else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--keep', action='store_true', help='leave the container, home and gateway running')
    args = parser.parse_args()
    home = Path(tempfile.mkdtemp(prefix='hko-qs-home-'))
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
    webhook_secret = secrets.token_hex(24)
    steps: list[dict] = []
    gateway = None

    def step(name: str, fn):
        t0 = time.time()
        try:
            note = fn()
            ok = True
        except Exception as exc:  # recorded, then the run stops
            note, ok = f'{type(exc).__name__}: {exc}'[:400], False
        steps.append({'step': name, 'seconds': round(time.time() - t0, 1), 'ok': ok, 'note': note})
        print(f"{'ok  ' if ok else 'FAIL'} {steps[-1]['seconds']:>6}s  {name}  {note or ''}", flush=True)
        if not ok:
            raise SystemExit(1)

    def hermes(*a: str, timeout: int = 120) -> str:
        r = subprocess.run([str(HERMES_BIN), *a], env=env, text=True, capture_output=True, timeout=timeout, cwd=str(ROOT))
        if r.returncode:
            raise RuntimeError(f"hermes {' '.join(a)[:60]} exit {r.returncode}: {(r.stderr or r.stdout)[-300:]}")
        return r.stdout

    def s1():
        sh(f'docker rm -f {NAME}')
        cmd = (f"docker run --rm -d --name {NAME} -p 127.0.0.1:{KPORT}:8080 --add-host host.docker.internal:host-gateway "
               f"-e KESTRA_CONFIGURATION='kestra: {{server: {{basic-auth: {{username: {USER}, password: {PASSWORD}}}}}}}' "
               f"-e SECRET_HERMES_WEBHOOK_URL=$(printf '%s' 'http://host.docker.internal:{WPORT}/webhooks/kestra-oncall' | base64 -w0) "
               f"-e SECRET_HERMES_WEBHOOK_TOKEN=$(printf '%s' '{webhook_secret}' | base64 -w0) kestra/kestra:v2.0.9 server local")
        r = sh(cmd)
        if r.returncode:
            raise RuntimeError(r.stderr[-300:])
        end = time.time() + 240
        while time.time() < end:
            try:
                return 'Kestra ' + http('GET', '/api/v1/configs')['version'] + ' answering'
            except Exception:
                time.sleep(3)
        raise TimeoutError('Kestra did not answer in 240 s')

    def s2():
        (home / 'plugins').mkdir(parents=True, exist_ok=True)
        link = home / 'plugins/kestra-oncall'
        if not link.exists():
            link.symlink_to(ROOT)
        for attempt in (1, 2, 3, 4, 5):  # Hermes downloads wheels while enabling; a remote 5xx is not the plugin's fault
            try:
                hermes('plugins', 'enable', 'kestra-oncall', timeout=240)
                return f'enabled on attempt {attempt} (local path instead of the GitHub install line)'
            except RuntimeError as exc:
                if attempt == 5 or ('Gateway Timeout' not in str(exc) and 'fetch' not in str(exc)):
                    raise
                time.sleep(20)
        return ''

    def s3():
        base = 'plugins.entries.kestra-oncall.settings.'
        hermes('config', 'set', base + 'kestra_url', f'http://127.0.0.1:{KPORT}')
        hermes('config', 'set', base + 'namespaces', '["demo"]')
        hermes('config', 'set', base + 'protected_flows', '["demo.protected_ledger"]')
        hermes('config', 'set', 'KESTRA_USERNAME', USER)
        hermes('config', 'set', 'KESTRA_PASSWORD', PASSWORD)
        doc = json.loads(hermes('kestra-oncall', 'doctor'))
        if not doc.get('ok'):
            raise RuntimeError('doctor not ok: ' + json.dumps(doc.get('checks')))
        return 'doctor ok ' + json.dumps(doc['checks'])

    def s4():
        global_note = hermes('kestra-oncall', 'setup', '--apply', '--deliver', 'log')
        hermes('config', 'set', 'WEBHOOK_SECRET', webhook_secret)
        hermes('config', 'set', 'platforms.webhook.enabled', 'true')
        hermes('config', 'set', 'platforms.webhook.extra.port', str(WPORT))
        # not in the README: the model, because a fresh home has none; the pool serves gpt-6-astra on /v1/responses
        key = json.loads(OPENCODE.read_text())['provider']['agentrouter']['options']['apiKey']
        with (home / '.env').open('a') as f:
            f.write(f'HKO_LLM_API_KEY={key}\n')
        (home / '.env').chmod(0o600)
        hermes('config', 'set', 'providers.agentrouter.api', 'http://127.0.0.1:20980/v1')
        hermes('config', 'set', 'providers.agentrouter.key_env', 'HKO_LLM_API_KEY')
        hermes('config', 'set', 'providers.agentrouter.transport', 'codex_responses')
        hermes('config', 'set', 'model.default', 'gpt-6-astra')
        hermes('config', 'set', 'model.provider', 'custom:agentrouter')
        # the shared pool rate-limits gpt-6-astra under load: same fallbacks as the main rig
        from ruamel.yaml import YAML
        yaml = YAML()
        cfg = yaml.load(home / 'config.yaml')
        cfg['fallback_providers'] = [{'provider': 'custom:agentrouter', 'model': 'gpt-6.1-sol'}, {'provider': 'custom:agentrouter', 'model': 'gpt-5.6-sol'}]
        with (home / 'config.yaml').open('w') as handle:
            yaml.dump(cfg, handle)
        # README requirement 1: the webhook adapter needs aiohttp, and the managed environment Hermes switches to after
        # a plugin with python_dependencies is enabled does not have it. rig/deps.py installs the pinned version.
        subprocess.run([str(PY), str(ROOT / 'rig/deps.py')], env=dict(env, HKO_RIG_HOME=str(home)), check=True, capture_output=True, timeout=300, cwd=str(ROOT / 'rig'))
        nonlocal gateway
        log = open(home / 'gateway.log', 'w')
        gateway = subprocess.Popen([str(HERMES_BIN), 'gateway', 'run', '-v'], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT))
        end = time.time() + 120
        while time.time() < end:
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{WPORT}/health', timeout=2).read()
                return 'webhook healthy on :%d' % WPORT
            except Exception:
                if gateway.poll() is not None:
                    raise RuntimeError('gateway exited: ' + (home / 'gateway.log').read_text()[-400:])
                time.sleep(2)
        raise TimeoutError('gateway webhook did not become healthy: ' + (home / 'gateway.log').read_text()[-400:])

    def s5():
        cmd = (f"""sed -e "s#\\['CHANGE_ME'\\]#['demo']#" -e 's#^  kestra_public_url: .*#  kestra_public_url: "http://127.0.0.1:{KPORT}"#' """
               f"kestra/flows/hermes_oncall_forwarder.yaml | curl -s -u '{USER}:{PASSWORD}' -X POST http://127.0.0.1:{KPORT}/api/v1/main/flows "
               "-H 'Content-Type: application/x-yaml' --data-binary @-")
        r = sh(cmd, cwd=str(ROOT))
        out = json.loads(r.stdout)
        if out.get('id') != 'hermes_oncall_forwarder':
            raise RuntimeError(r.stdout[:300])
        time.sleep(25)  # Kestra registers a new Flow trigger a few seconds after the flow is created
        return 'forwarder deployed in namespace ' + out['namespace']

    def s6():
        loop = (f"""for f in orders_etl protected_ledger; do sed 's/^namespace: .*/namespace: demo/' kestra/demo/$f.yaml | """
                f"curl -s -u '{USER}:{PASSWORD}' -X POST http://127.0.0.1:{KPORT}/api/v1/main/flows -H 'Content-Type: application/x-yaml' --data-binary @- > /dev/null; done; "
                f"curl -s -u '{USER}:{PASSWORD}' -X POST http://127.0.0.1:{KPORT}/api/v1/main/executions/demo/orders_etl -F x=1")
        r = sh(loop, cwd=str(ROOT))
        ex = json.loads(r.stdout)
        end = time.time() + 60
        while time.time() < end:
            if http('GET', f"/api/v1/main/executions/{ex['id']}")['state']['current'] == 'FAILED':
                state['execution'] = ex['id']
                return 'orders_etl FAILED as designed, execution ' + ex['id']
            time.sleep(1)
        raise TimeoutError('orders_etl did not fail')

    state: dict = {}

    def s7a():
        db = home / 'plugin-data/kestra-oncall/data.db'
        end = time.time() + 400
        while time.time() < end:
            if db.exists():
                conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
                try:
                    row = conn.execute("SELECT i.incident_id, p.proposal_sha256 FROM incidents i JOIN proposals p USING (incident_id) "
                                       "WHERE i.execution_id = ? ORDER BY p.created DESC LIMIT 1", (state['execution'],)).fetchone()
                finally:
                    conn.close()
                if row:
                    state['incident'], state['sha'] = row
                    return f'proposal for {row[0]} stored'
            time.sleep(3)
        raise TimeoutError('no proposal within 400 s; gateway log tail: ' + (home / 'gateway.log').read_text()[-300:])

    def s7b():
        py3 = shutil.which('python3') or 'python3'
        cmd = [py3, str(ROOT / 'tests/e2e/pty_cli.py'), f"/kestra apply {state['incident']} {state['sha'][:12]}|verified"]
        r = subprocess.run(cmd, env=dict(env, HKO_HERMES_DIR=str(HERMES)), text=True, capture_output=True, timeout=300, cwd=str(ROOT))
        out = json.loads([l for l in r.stdout.splitlines() if l.startswith('{')][-1])
        if not out['results'][0]['found']:
            Path('/tmp/hko-qs-screen.txt').write_text(out['results'][0]['screen'])
            raise RuntimeError('no "verified" on the screen (full screen in /tmp/hko-qs-screen.txt): ' + out['results'][0]['screen'][-300:])
        flow = http('GET', '/api/v1/main/flows/demo/orders_etl')
        replays = [e for e in http('GET', '/api/v1/main/executions/search?namespace=demo&flowId=orders_etl&size=50')['results']
                   if e.get('originalId') == state['execution'] and e['id'] != state['execution']]
        if flow['revision'] < 2 or len(replays) != 1 or replays[0]['state']['current'] != 'SUCCESS':
            raise RuntimeError(f"Kestra disagrees: revision {flow['revision']}, replays {[(r['id'], r['state']['current']) for r in replays]}")
        return f"typed in the real TUI -> verified; revision {flow['revision']}, replay {replays[0]['id']} SUCCESS"

    code = 0
    try:
        step('1 start Kestra 2.0.9 with basic auth and the two forwarder secrets', s1)
        step('2 install and enable the plugin', s2)
        step('3 configure and run `hermes kestra-oncall doctor`', s3)
        step('4 webhook route, WEBHOOK_SECRET, start the gateway', s4)
        step('5 deploy the forwarder (README sed | curl)', s5)
        step('6 deploy the demo flows and run orders_etl', s6)
        step('7a wait for the incident and proposal (model)', s7a)
        step('7b type /kestra apply <id> <hash> in the real terminal UI', s7b)
    except SystemExit:
        code = 1
    finally:
        total = round(sum(s['seconds'] for s in steps), 1)
        result = {'generated': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'steps': steps, 'total_seconds': total,
                  'substitutions': ['local plugin path instead of the GitHub install line', '--deliver log (no chat bot on this machine)',
                                    'model block and fallback models written into config.yaml', 'aiohttp installed into the managed environment (README requirement 1) by rig/deps.py', f'ports {KPORT} (Kestra) and {WPORT} (webhook)'],
                  'completed_all_steps': len(steps) == 8 and all(s['ok'] for s in steps)}
        save_json(ROOT / 'evidence/quickstart-check.json', result)
        print(f'total {total}s, all steps ok: {result["completed_all_steps"]}')
        if not args.keep:
            if gateway and gateway.poll() is None:
                gateway.terminate()
                try:
                    gateway.wait(15)
                except subprocess.TimeoutExpired:
                    gateway.kill()
            sh(f'docker rm -f {NAME}')
            shutil.rmtree(home, ignore_errors=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
