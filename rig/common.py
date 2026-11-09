"""Isolated rig I/O, independent of the plugin so fixtures can precede core."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shlex
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
# Defaults are the main rig; export HKO_RIG_HOME / HKO_HERMES_DIR / HKO_RIG_RUN to run a second Hermes
# checkout (for example v0.23.0) with its own home. Only one gateway may own webhook port 28644 at a time.
HOME = Path(os.environ.get('HKO_RIG_HOME', '/home/asuran/hko-rig/hermes-home'))
RUN = Path(os.environ.get('HKO_RIG_RUN', '/home/asuran/hko-rig/run'))
HERMES = Path(os.environ.get('HKO_HERMES_DIR', '/home/asuran/hko-rig/hermes-agent'))
PYTHON = HERMES / '.venv/bin/python'
SECRETS = Path('/home/asuran/hko-rig/secrets/kestra.env')


def env_file(path: Path = SECRETS) -> dict[str, str]:
    """Read simple shell assignments without echoing credentials or invoking a shell."""
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip().removeprefix('export ')
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key] = ''.join(shlex.split(value, comments=True))
    return values


def redact(text: str) -> str:
    """Keep accidental HTTP diagnostics from disclosing runtime credentials."""
    for path in (SECRETS, HOME / '.env'):
        if path.exists():
            for key, value in env_file(path).items():
                if value and any(term in key for term in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET')):
                    text = text.replace(value, '[REDACTED]')
    return text


def save_json(path: Path, value: Any) -> None:
    """Persist readable evidence, redacting known secret values at the boundary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(json.dumps(value, indent=2, default=str)) + '\n')


def yaml_text(value: dict) -> str:
    """Use the same YAML implementation as the plugin."""
    import io
    stream = io.StringIO()
    YAML().dump(value, stream)
    return stream.getvalue()


class API:
    """Bounded HTTP against only the provisioned local Kestra instance."""

    def __init__(self) -> None:
        values = env_file()
        self.url = values['KESTRA_URL'].rstrip('/')
        if self.url != 'http://127.0.0.1:28090':
            raise RuntimeError('Refusing to operate outside the provisioned rig Kestra URL')
        self.prefix = '/api/v1/' + values.get('KESTRA_TENANT', 'main')
        self.auth = base64.b64encode((values['KESTRA_USERNAME'] + ':' + values['KESTRA_PASSWORD']).encode()).decode()

    def request(self, method: str, path: str, data: bytes | None = None,
                content_type: str = 'application/json') -> Any:
        """Never include request headers in exceptions or evidence."""
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={'Authorization': 'Basic ' + self.auth,
                                                  'Content-Type': content_type})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read(8_000_000)
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read(10000).decode(errors='replace'))
            raise RuntimeError(f'{method} {path}: HTTP {exc.code}: {detail}') from None
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return body.decode()

    def validate(self, source: str) -> list:
        rows = self.request('POST', self.prefix + '/flows/validate', source.encode(), 'application/x-yaml')
        errors = [row for row in rows if row.get('constraints') or row.get('outdated') or row.get('deprecationPaths')]
        if errors:
            raise RuntimeError('Flow validation rejected source: ' + json.dumps(errors))
        return rows

    def deploy(self, source: str) -> dict:
        """Validate every source before any create or update, including rig overrides."""
        definition = YAML(typ='safe').load(source)
        namespace, flow_id = definition['namespace'], definition['id']
        if namespace not in ('hko.demo', 'hko.e2e', 'hko.lookalike', 'hko.system', 'hko.demoextra'):
            raise RuntimeError('Refusing to write another agent namespace')
        self.validate(source)
        path = self.prefix + f'/flows/{namespace}/{flow_id}'
        try:
            current = self.request('GET', path)
        except RuntimeError as exc:
            if 'HTTP 404:' not in str(exc):
                raise
            current = None
        if current and current.get('source') == source:
            return current
        return self.request('PUT' if current else 'POST', path if current else self.prefix + '/flows',
                            source.encode(), 'application/x-yaml')

    def trigger(self, namespace: str, flow_id: str, inputs: dict | None = None) -> dict:
        boundary = 'hko-rig-form'
        body = b''
        for key, value in (inputs or {}).items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
        body += f'--{boundary}--\r\n'.encode()
        return self.request('POST', self.prefix + f'/executions/{namespace}/{flow_id}', body,
                            'multipart/form-data; boundary=' + boundary)

    def execution(self, execution_id: str) -> dict:
        return self.request('GET', self.prefix + '/executions/' + execution_id)

    def wait(self, execution_id: str, *, states: tuple = ('SUCCESS', 'FAILED', 'KILLED', 'WARNING'),
             timeout: float = 90) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            execution = self.execution(execution_id)
            if execution['state']['current'] in states:
                return execution
            time.sleep(1)
        raise TimeoutError(f'Execution {execution_id} did not reach {states} in {timeout}s')

    def task_outputs(self, execution_id: str) -> dict:
        """Kestra 2.0 keeps task outputs out of the execution: {taskId: [outputs per task run]}."""
        listing = self.request('GET', self.prefix + '/outputs/tasks/' + execution_id) or []
        found: dict[str, list] = {}
        for row in listing:
            detail = self.request('GET', self.prefix + f"/outputs/tasks/{execution_id}/{row['taskRunId']}")
            found.setdefault(row['taskId'], []).append(detail)
        return found

    def executions(self, namespace: str, flow_id: str) -> list[dict]:
        query = urllib.parse.urlencode({'namespace': namespace, 'flowId': flow_id, 'size': 1000,
                                       'sort': 'state.startDate:desc'})
        result = self.request('GET', self.prefix + '/executions/search?' + query)
        return [row for row in result['results'] if row['namespace'] == namespace and row['flowId'] == flow_id]


RIG_NAMESPACES = "['hko.demo', 'hko.e2e']"
RIG_KESTRA_URL = 'http://127.0.0.1:28090'


def render_forwarder(source: str, *, flow_id: str = 'hermes_oncall_forwarder', namespace: str = 'hko.system',
                     namespaces: str = RIG_NAMESPACES, url: str = RIG_KESTRA_URL, disabled: bool = False) -> str:
    """Turn the shipped forwarder into the rig copy by editing only its marked places, id and namespace.

    Each edited line must exist exactly once, so a change to the shipped file cannot be silently skipped.
    """
    edits = [('id: hermes_oncall_forwarder\n', f'id: {flow_id}\n'),
             ('namespace: ops\n', f'namespace: {namespace}\n'),
             ("{% for p in ['CHANGE_ME'] %}", "{% for p in " + namespaces + " %}"),
             ('  kestra_public_url: "https://kestra.example.com"\n', f'  kestra_public_url: "{url}"\n'),
             ('disabled: false\n', f'disabled: {"true" if disabled else "false"}\n')]
    for old, new in edits:
        if source.count(old) != 1:
            raise RuntimeError(f'forwarder template line missing or repeated: {old.strip()!r}')
        source = source.replace(old, new)
    return source


def render_fixture(source: str) -> str:
    """The shipped demo flows use namespaces `demo` and `demo_lookalike`; the rig runs them as hko.* ones."""
    for old, new in (('namespace: demo_lookalike\n', 'namespace: hko.lookalike\n'), ('namespace: demo\n', 'namespace: hko.demo\n')):
        if source.count(old) == 1:
            return source.replace(old, new)
    raise RuntimeError('fixture has neither namespace: demo nor namespace: demo_lookalike')
