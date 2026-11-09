"""Delete only what the rig created, then clear the plugin store. Stop the gateway first (reset.sh does)."""

from __future__ import annotations

import urllib.parse

from common import API, HOME

NAMESPACES = ('hko.demo', 'hko.e2e', 'hko.lookalike')
FLOW_NAMESPACES = NAMESPACES + ('hko.system',)  # only the rig's forwarder and probe live there


def main() -> None:
    api = API()
    deleted = {'executions': 0, 'flows': 0}
    for namespace in NAMESPACES:
        query = urllib.parse.urlencode({'namespace': namespace, 'size': 1000})
        rows = api.request('GET', api.prefix + '/executions/search?' + query)['results']
        for row in rows:
            if row['namespace'] != namespace:
                continue
            api.request('DELETE', api.prefix + '/executions/' + row['id'])
            deleted['executions'] += 1
    for fid in ('hermes_oncall_forwarder', 'secret_probe', 'rig_selffail', 'rig_selfguard_fwd'):
        query = urllib.parse.urlencode({'namespace': 'hko.system', 'flowId': fid, 'size': 1000})
        for row in api.request('GET', api.prefix + '/executions/search?' + query)['results']:
            if row['namespace'] == 'hko.system' and row['flowId'] == fid:
                api.request('DELETE', api.prefix + '/executions/' + row['id'])
                deleted['executions'] += 1
    for namespace in FLOW_NAMESPACES:
        for flow in api.request('GET', api.prefix + f'/flows/{namespace}'):
            if namespace == 'hko.system' and flow['id'] not in ('hermes_oncall_forwarder', 'secret_probe', 'rig_selffail', 'rig_selfguard_fwd'):
                continue
            api.request('DELETE', api.prefix + f'/flows/{namespace}/{flow["id"]}')
            deleted['flows'] += 1
    removed = []
    for path in (HOME / 'plugin-data/kestra-oncall').glob('data.db*'):
        path.unlink()
        removed.append(path.name)
    print({'deleted': deleted, 'store_files_removed': removed})


if __name__ == '__main__':
    main()
