# End-to-end proof (S1 to S7)

These scenarios run the plugin inside real Hermes against a real Kestra 2.0.9. Every assertion is read back
from Kestra's API (revisions, executions, task runs, task outputs) or from the plugin's SQLite store.

## What is real, what is scripted

| Piece | In this run |
|---|---|
| Kestra | Real, `kestra/kestra:v2.0.9` in container `hko-kestra`, tenant `main` |
| Failure forwarding | Real Kestra Flow trigger and HTTP Request task, secrets from `SECRET_*` env |
| Hermes gateway and webhook | Real gateway process, real route `kestra-oncall`, shared-token check |
| Model | Real (`gpt-6-astra`, fallbacks `gpt-6.1-sol` and `gpt-5.6-sol` on the same pool). S1 to S3 use the model's own tool calls |
| Plugin loading, settings, credentials, store | Real: Hermes plugin discovery, `plugins.entries.kestra-oncall.settings`, `.env`, profile SQLite |
| Tool path | Real: `model_tools.handle_function_call` (pre_tool_call hook, approval gate, registry) |
| Human approval | Scripted answer (`once` or `deny`) to Hermes's own approval callback. The prompt text is captured and asserted |
| Scripted proposals | S4 to S6 only, so the result does not depend on model luck |

## Run it

```bash
rig/reset.sh --yes     # stops the gateway, deletes only the rig's flows/executions, clears the plugin store, re-seeds
rig/up.sh              # configures HERMES_HOME, installs the route, starts the gateway on 10.250.90.1:28644
python tests/e2e/run_e2e.py                  # all scenarios, about 8 to 10 minutes (S1 waits for the model; S6 waits for a lock to expire)
python tests/e2e/run_e2e.py --only S4,S5     # a subset (S2 and S3 need S1)
python evidence/make_manifest.py             # byte-bound manifest
```

Use the rig venv: `/home/asuran/hko-rig/hermes-agent/.venv/bin/python`. Results: `evidence/e2e-results.json`.

## Scenarios

| Id | Proves |
|---|---|
| S1 detect | Failing `orders_etl` fires the forwarder, Hermes runs the webhook turn, the model opens an incident and stores a validated one-line proposal. `healthy_report` and the `hko.lookalike` failure do not reach Hermes and leave no incident. `protected_ledger` gets a diagnosis but `propose_fix` is refused and no proposal exists |
| S2 approve and repair | Deny writes nothing. Webhook and cron runs cannot apply (no prompt). A hash that is not the stored proposal is blocked. Approved apply gives exactly one new revision equal to the proposal bytes and exactly one replay from `transform`; `extract` is reused (identical attempt timestamps and outputs), `load` ran, the original execution stays FAILED |
| S3 idempotent retry | Applying again changes nothing: same revision count, same execution count, same journal |
| S4 stale | A flow edited after the proposal makes apply refuse with no write; a fresh proposal on the new revision then verifies and keeps the out-of-band edit |
| S5 rollback | A proposal that validates but still fails: replay FAILED, flow source byte-identical to the base again, revision history keeps the failed fix, rerun is a no-op |
| S6a, S6b interrupted apply | The apply process is killed right before the replay call. The flow is already updated, no replay exists, a retry during the lock reports `busy`. After the lock expires the next apply adopts the written fix and starts exactly one replay |
| S7 pause | A paused `refund_approval` shows up in `/kestra pauses`; deny, webhook and cron cannot resume; invalid input is refused by Kestra; approved resume with inputs finishes the execution and the downstream task sees them; resume from an old snapshot after an out-of-band resume is refused with no second write; approved kill ends a paused execution |

## Notes for reviewers

- S4 to S6 pause the forwarder (restored in a `finally` block) so the model cannot race the scripted proposal on the `hko.e2e` copies.
- S6 waits up to `replay_timeout_seconds + 120 = 150` seconds for the dead process's lock. That wait is real, not simulated.
- Kestra 2.0.9 stamps `originalId` on the original execution itself after a replay, so replay counts exclude the original.
- `tests/e2e/hermes_driver.py` is the single place that plays a chat session. `--platform webhook|cron` gives an unattended session; `--fault crash-before-replay` is the only fault injection.
