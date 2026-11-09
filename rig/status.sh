#!/usr/bin/env bash
# One-screen view of the rig: gateway, webhook health from the Kestra container, Kestra version,
# plugin doctor and flow counts. Prints no credentials.
. "$(dirname "$0")/_env.sh"
if gw_running; then echo "gateway: running pid $(cat "$GW_PID")"; else echo "gateway: stopped"; fi
echo "webhook (host):      $(curl -s -m 3 http://$WEBHOOK_HOST:$WEBHOOK_PORT/health || echo unreachable)"
echo "webhook (container): $(docker exec hko-kestra curl -s -m 3 http://$WEBHOOK_HOST:$WEBHOOK_PORT/health || echo unreachable)"
"$PY" - <<'PYEOF'
import sys; sys.path.insert(0, '.')

from common import API
a = API()
print('kestra:', a.request('GET', '/api/v1/configs')['version'])
for ns, fid in [('hko.system','hermes_oncall_forwarder'),('hko.demo','orders_etl'),('hko.demo','refund_approval'),
                ('hko.demo','healthy_report'),('hko.demo','protected_ledger'),('hko.lookalike','orders_etl')]:
    try:
        f = a.request('GET', a.prefix + f'/flows/{ns}/{fid}')
        print(f'flow {ns}.{fid}: rev {f["revision"]} disabled={f.get("disabled", False)}')
    except RuntimeError as e:
        print(f'flow {ns}.{fid}: missing')
PYEOF
"$HERMES_BIN" kestra-oncall doctor 2>&1 | head -20
