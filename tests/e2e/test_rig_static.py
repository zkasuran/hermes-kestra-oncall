"""Static checks on the Kestra-side files and the rig tooling. No Kestra, no Hermes. Run:

    python -m pytest -q -p no:cacheprovider -W error tests/e2e/test_rig_static.py

Each test pins a defect the pass-3 review found (the forwarder hard-coded the rig, rig/deps.py used private Hermes
imports, demo fixtures needed editing, the manifest accepted a dirty tree) so it cannot come back.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'rig'))
import common  # noqa: E402

FORWARDER = (ROOT / 'kestra/flows/hermes_oncall_forwarder.yaml').read_text()


def test_shipped_forwarder_is_a_deployable_template_not_the_rig():
    assert 'hko' not in FORWARDER and '127.0.0.1' not in FORWARDER and '28090' not in FORWARDER
    assert re.search(r'^namespace: ops$', FORWARDER, re.M) and re.search(r'^disabled: false$', FORWARDER, re.M)
    assert FORWARDER.count("['CHANGE_ME']") == 1, 'one clearly marked list to edit'
    assert 'kestra_public_url: "https://kestra.example.com"' in FORWARDER


def test_forwarder_keeps_its_safety_properties():
    assert 'condition: "{{ trigger.namespace != flow.namespace }}"' in FORWARDER      # never forwards its own namespace
    assert 'X-Request-ID: "{{ trigger.executionId }}"' in FORWARDER                   # Hermes de-duplicates on it
    assert 'X-Gitlab-Token: "{{ secret(\'HERMES_WEBHOOK_TOKEN\') }}"' in FORWARDER
    assert 'states: [FAILED]' in FORWARDER
    when = next(line for line in FORWARDER.splitlines() if line.strip().startswith('when:'))
    # measured on Kestra 2.0.9: flow variables are not visible in a Flow trigger condition, `matches` is not an operator
    assert 'vars.' not in when and ' matches ' not in when
    assert 'startsWith(p ~ \'.\')' in when and 'flow.namespace == p' in when         # a namespace and its children, not "prod2"
    assert 'SECRET_<NAME>' not in FORWARDER, 'the security scanner flags this wording as encoded exfiltration'


def test_render_forwarder_touches_only_the_marked_places():
    out = common.render_forwarder(FORWARDER)
    assert "{% for p in ['hko.demo', 'hko.e2e'] %}" in out and 'namespace: hko.system' in out and 'id: hermes_oncall_forwarder' in out
    assert 'kestra_public_url: "http://127.0.0.1:28090"' in out and 'CHANGE_ME' not in out
    diff = [l for l in out.splitlines() if l not in FORWARDER.splitlines()]
    assert len(diff) == 3, diff   # namespace, public URL and the namespace list; id and disabled stay as shipped
    assert 'disabled: true' in common.render_forwarder(FORWARDER, disabled=True)


@pytest.mark.parametrize('broken', [FORWARDER.replace("['CHANGE_ME']", "['x']"), FORWARDER.replace('namespace: ops\n', ''), FORWARDER + "\n# ['CHANGE_ME'] {% for p in ['CHANGE_ME'] %}"])
def test_render_forwarder_refuses_a_template_that_drifted(broken):
    with pytest.raises(RuntimeError, match='missing or repeated'):
        common.render_forwarder(broken)


def test_demo_fixtures_deploy_as_shipped_and_the_rig_renders_hko_copies():
    names = {p.name: p.read_text() for p in (ROOT / 'kestra/demo').glob('*.yaml')}
    assert set(names) == {'orders_etl.yaml', 'refund_approval.yaml', 'healthy_report.yaml', 'protected_ledger.yaml', 'demo_lookalike.yaml'}
    for name, text in names.items():
        expected = 'demo_lookalike' if name == 'demo_lookalike.yaml' else 'demo'
        assert re.search(rf'^namespace: {expected}$', text, re.M), name
        rendered = common.render_fixture(text)
        assert re.search(r'^namespace: hko\.(lookalike|demo)$', rendered, re.M)
    assert 'namespace: hko.lookalike' in common.render_fixture(names['demo_lookalike.yaml'])
    with pytest.raises(RuntimeError):
        common.render_fixture('id: x\nnamespace: elsewhere\n')


def test_rig_tools_do_not_import_hermes_internals():
    """`hermes plugins validate` flags private core imports; rig/deps.py used pm.environments once."""
    banned = ('pm', 'hermes_cli', 'tools', 'agent', 'gateway')
    for path in list((ROOT / 'rig').glob('*.py')):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                assert name.split('.')[0] not in banned, f'{path.name} imports {name}'


def test_secret_probe_is_a_rig_tool_not_a_shipped_flow():
    assert not (ROOT / 'kestra/flows/secret_probe.yaml').exists()
    assert (ROOT / 'rig/flows/secret_probe.yaml').exists()


def test_manifest_refuses_a_dirty_tree_and_names_the_commit():
    source = (ROOT / 'evidence/make_manifest.py').read_text()
    assert "'--untracked-files=no'" in source and 'refusing: tracked files differ from HEAD' in source
    assert "'tested_commit': state" in source and "rev-parse', 'HEAD^{tree}'" in source


def test_release_export_is_an_allow_list_built_from_the_commit():
    script = (ROOT / 'release/export.sh').read_text()
    assert 'git archive HEAD' in script and 'refusing: allow-listed paths have uncommitted changes' in script
    for stays_behind in ('rig/', 'tests/e2e', 'tests/live', 'evidence/', 'docs/screenshots'):
        assert not re.search(rf'^\s+.*\b{re.escape(stays_behind.rstrip("/"))}\b.*$', script.split('ALLOW=(')[1].split(')')[0], re.M), stays_behind
