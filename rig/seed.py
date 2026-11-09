"""Validate then install only the rig-owned fixture flows."""

from __future__ import annotations

import argparse
from common import API, ROOT, RUN, save_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--enable-forwarder', action='store_true')
    args = parser.parse_args()
    api = API()
    records = []
    for path in sorted((ROOT / 'kestra/demo').glob('*.yaml')) + sorted((ROOT / 'kestra/flows').glob('*.yaml')):
        source = path.read_text()
        if path.stem == 'hermes_oncall_forwarder' and args.enable_forwarder:
            source = source.replace('disabled: true', 'disabled: false', 1)
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
    evidence = {'flows': records, 'secret_probe': {'id': probe['id'], 'state': probe['state']['current'], 'outputs': outputs}, 'configs': api.request('GET', '/api/v1/configs')}
    save_json(ROOT / 'evidence/seed.json', evidence)
    print({'validated_flows': len(records), 'secret_probe': probe['id'], 'state': probe['state']['current']})


if __name__ == '__main__':
    main()
