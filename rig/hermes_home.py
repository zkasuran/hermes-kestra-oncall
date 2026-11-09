"""Create or refresh the rig HERMES_HOME without ever printing a credential.

Idempotent: running it twice leaves the same config.yaml and .env. Credentials are copied
programmatically from the opencode config (LLM key) and the rig secrets file (Kestra login and
the shared webhook token) into the rig .env with mode 600.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from ruamel.yaml import YAML

from common import HOME, HERMES, PYTHON, REPLAY_TIMEOUT_S, ROOT, RUN, env_file

OPENCODE = Path.home() / '.config/opencode/opencode.json'
HOST = os.environ.get('HKO_WEBHOOK_HOST', '10.250.90.1')  # the docker network gateway: reachable from hko-kestra
PORT = int(os.environ.get('HKO_WEBHOOK_PORT', '28644'))
PROTECTED = ['hko.demo.protected_ledger']
NAMESPACES = ['hko.demo', 'hko.e2e']


def write_env() -> None:
    """Merge managed keys into .env (mode 600) and keep any other line untouched."""
    options = json.loads(OPENCODE.read_text())['provider']['agentrouter']['options']
    secrets = env_file()
    managed = {
        'HKO_LLM_API_KEY': options['apiKey'],
        'KESTRA_USERNAME': secrets['KESTRA_USERNAME'],
        'KESTRA_PASSWORD': secrets['KESTRA_PASSWORD'],
        'WEBHOOK_ENABLED': 'true',
        'WEBHOOK_PORT': str(PORT),
        'WEBHOOK_SECRET': secrets['HKO_WEBHOOK_TOKEN'],
    }
    path = HOME / '.env'
    kept = []
    if path.exists():
        kept = [line for line in path.read_text().splitlines()
                if line.split('=', 1)[0].strip() not in managed]
    lines = kept + [f'{key}={value}' for key, value in managed.items()]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write('\n'.join(lines) + '\n')
    path.chmod(0o600)


def hermes(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    env = dict(os.environ, HERMES_HOME=str(HOME), PYTHONDONTWRITEBYTECODE='1')
    return subprocess.run([str(HERMES / '.venv/bin/hermes'), *args], env=env, text=True,
                          capture_output=True, timeout=timeout)


def write_config() -> None:
    """Set model, plugin settings and the webhook listener, preserving everything else."""
    yaml = YAML()
    yaml.preserve_quotes = True
    path = HOME / 'config.yaml'
    config = yaml.load(path) if path.exists() else {}
    config = config or {}
    # The pool serves gpt-6-astra on /v1/responses only; a plain "custom" provider ignores
    # codex_responses for non-OpenAI URLs, so declare a named provider with an explicit transport.
    config['providers'] = {'agentrouter': {
        'api': 'http://127.0.0.1:20980/v1', 'key_env': 'HKO_LLM_API_KEY',
        'transport': 'codex_responses', 'default_model': 'gpt-6-astra'}}
    config['model'] = {'default': 'gpt-6-astra', 'provider': 'custom:agentrouter',
                       'context_length': 128000}
    # The shared pool rate-limits gpt-6-astra under load; the same pool serves these two models.
    config['fallback_providers'] = [{'provider': 'custom:agentrouter', 'model': 'gpt-6.1-sol'},
                                    {'provider': 'custom:agentrouter', 'model': 'gpt-5.6-sol'}]
    plugins = config.setdefault('plugins', {})
    enabled = plugins.setdefault('enabled', [])
    if 'kestra-oncall' not in enabled:
        enabled.append('kestra-oncall')
    entries = plugins.setdefault('entries', {})
    entries['kestra-oncall'] = {'settings': {
        'kestra_url': 'http://127.0.0.1:28090', 'tenant': 'main',
        'namespaces': NAMESPACES, 'protected_flows': PROTECTED,
        'rollback_on_failure': True, 'replay_timeout_seconds': REPLAY_TIMEOUT_S, 'max_log_lines': 100}}
    webhook = config.setdefault('platforms', {}).setdefault('webhook', {})
    # The rig owns this home: drop an older route so `setup --apply` installs the current one (it refuses to
    # replace a route that differs, which is the right behaviour for a user's profile).
    (webhook.get('extra') or {}).get('routes', {}).pop('kestra-oncall', None)
    webhook['enabled'] = True
    extra = webhook.setdefault('extra', {})
    extra['host'], extra['port'] = HOST, PORT
    with path.open('w') as handle:
        yaml.dump(config, handle)
    path.chmod(0o600)


def main() -> int:
    HOME.mkdir(parents=True, exist_ok=True)
    RUN.mkdir(parents=True, exist_ok=True)
    (HOME / 'plugins').mkdir(exist_ok=True)
    link = HOME / 'plugins/kestra-oncall'
    if link.is_symlink() or link.exists():
        if not (link.is_symlink() and link.resolve() == ROOT):
            raise SystemExit(f'{link} exists and is not a symlink to the repo; refusing to replace it')
    else:
        link.symlink_to(ROOT)
    write_env()
    write_config()
    enable = hermes('plugins', 'enable', 'kestra-oncall')
    print('plugin enable exit', enable.returncode)
    # The route comes from the plugin's own setup command so the rig exercises the shipped path.
    setup = hermes('kestra-oncall', 'setup', '--apply', '--deliver', 'log')
    print('setup --apply exit', setup.returncode, setup.stdout[-200:].strip().replace('\n', ' | '))
    if setup.returncode:
        print(setup.stderr[-600:], file=sys.stderr)
    return setup.returncode


if __name__ == '__main__':
    raise SystemExit(main())
