"""Validate then install only the rig-owned fixture flows."""

from __future__ import annotations

import argparse
from common import API, ROOT, RUN, render_fixture, render_forwarder, save_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-forwarder-enable', action='store_true', help='deploy the rig forwarder disabled')
    args = parser.parse_args()
    api = API()
    records = []
    for path in (sorted((ROOT / 'kestra/demo').glob('*.yaml')) + sorted((ROOT / 'rig/flows').glob('*.yaml'))
                 + sorted((ROOT / 'kestra/flows').glob('*.yaml'))):
        source = path.read_text()
        if path.parent.name == 'demo':
            source = render_fixture(source)
        if path.stem == 'hermes_oncall_forwarder':
            # The shipped file is a user template; the rig deploys a rendered copy (hko.* namespaces).
            source = render_forwarder(source, disabled=args.no_forwarder_enable)
            RUN.mkdir(parents=True, exist_ok=True)
            (RUN / 'hermes_oncall_forwarder.yaml').write_text(source)
        result = api.deploy(source)
        records.append({'file': str(path.relative_to(ROOT)), 'namespace': result['namespace'],
                        'flow_id': result['id'], 'revision': result['revision'], 'validated': True})
    probe = api.trigger('hko.system', 'secret_probe')
    probe = api.wait(probe['id'])
    assert probe['state']['current'] == 'SUCCESS', probe['id']
    outputs = api.task_outputs(probe['id'])
    assert outputs['resolve_secrets'][0]['value'] == 'true', 'Secret resolution failed'
    evidence = {'flows': records, 'secret_probe': {'id': probe['id'], 'state': probe['state']['current'], 'outputs': outputs}, 'kestra': {k: v for k, v in api.request('GET', '/api/v1/configs').items() if k in ('version', 'edition', 'commitId', 'commitDate')}}
    save_json(ROOT / 'evidence/seed.json', evidence)
    print({'validated_flows': len(records), 'secret_probe': probe['id'], 'state': probe['state']['current']})


if __name__ == '__main__':
    main()
