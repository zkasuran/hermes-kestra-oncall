# End-to-end proof (S1 to S12)

These scenarios run the plugin inside real Hermes against a real Kestra 2.0.9. Every assertion is read back
from Kestra's API (revisions, executions, task runs, task outputs) or from the plugin's SQLite store.

## What is real, what is scripted

| Piece | In this run |
|---|---|
| Kestra | Real, `kestra/kestra:v2.0.9` in container `hko-kestra`, tenant `main` |
| Failure forwarding | Real Kestra Flow trigger and HTTP Request task, secrets from `SECRET_*` env. The shipped `kestra/flows/hermes_oncall_forwarder.yaml` is rendered for the `hko.*` namespaces by `rig/common.py::render_forwarder` |
| Hermes gateway and webhook | Real gateway process, real route `kestra-oncall`, shared-token check |
| Model | Real (`gpt-6-astra`, fallbacks `gpt-6.1-sol` and `gpt-5.6-sol` on the same pool). S1 to S3 use the model's own tool calls |
| Plugin loading, settings, credentials, store | Real: Hermes plugin discovery, `plugins.entries.kestra-oncall.settings`, `.env`, profile SQLite |
| The human's approval | Typed command, `/kestra apply <id> <hash>`, `/kestra resume ...`, `/kestra kill ...`. Run through the plugin's real command handler (`hermes_driver.py command ...`), through a telegram-like session with no approval notifier (S10) and typed into the real terminal UI in a pseudo-terminal (S12) |
| Model tool path | Real: `model_tools.handle_function_call` (pre_tool_call hook, Hermes approval gate, registry). The answer to Hermes's approval card (`deny` or `once`) is scripted (S2, S5, S7); the card text is captured and asserted |
| Scripted proposals | S4 to S6 and S8 to S12, so the result does not depend on model luck |

## Run it

```bash
rig/reset.sh --yes     # stops the gateway, deletes only the rig's flows/executions, clears the plugin store, re-seeds
rig/up.sh              # configures HERMES_HOME, installs the route, starts the gateway on 10.250.90.1:28644
python tests/e2e/run_e2e.py                  # all scenarios, about 15 minutes (S1 waits for the model; S6b waits for a lock to expire; S9 waits for a slow replay)
python tests/e2e/run_e2e.py --only S4,S5     # a subset (S2 and S3 need S1)
python tests/e2e/run_e2e.py --skip-lock-wait # quick re-runs: S6b forces the dead lock to expire instead of waiting 150 s
python evidence/make_manifest.py             # byte-bound manifest; refuses a tree whose tracked files differ from HEAD
python evidence/make_summary.py              # evidence/E2E-SUMMARY.md
```

Use the rig venv: `/home/asuran/hko-rig/hermes-agent/.venv/bin/python`. Results: `evidence/e2e-results.json`.
`S12` needs `pexpect` in the system `python3` (the Hermes venv has none): `python3 -c 'import pexpect'`.
Paths, addresses and ports come from environment variables; see `rig/env.example`.

### Other checks

- `rig/quickstart_check.sh`: the README quick start, step by step, on an empty HERMES_HOME and a new Kestra container, with timings (`evidence/quickstart-check.json`).
- Hermes v0.23.0: stop the main gateway, then
  `HKO_HERMES_DIR=/home/asuran/hko-rig/hermes-v0.23.0 HKO_RIG_HOME=/home/asuran/hko-rig/hermes-home-v023 HKO_RIG_RUN=/home/asuran/hko-rig/run-v023 rig/up.sh`
  and `python tests/e2e/run_e2e.py --only S1,S2,S3 --out evidence/v023-results.json` with the same variables. Only one gateway may own the webhook port.
- `release/export.sh`: builds the catalog pin from an allow-list of committed files and runs `hermes plugins validate` on it.

## Scenarios

| Id | Proves |
|---|---|
| S1 detect | Failing `orders_etl` fires the forwarder, Hermes runs the webhook turn, the model opens an incident and stores a validated one-line proposal. `healthy_report` and the `hko.lookalike` failure do not reach Hermes and leave no incident. `protected_ledger` gets a diagnosis but `propose_fix` is refused and no proposal exists |
| S1b hardening | Wrong or missing token = 401, a repeated delivery id is ignored, a failure in the forwarder's own namespace does not fire it, and a copy misconfigured to match its own namespace skips the HTTP call and ends SUCCESS |
| S2 approve and repair | Typed apply without a hash or with a wrong hash is refused. Model tool path: deny writes nothing, the card shows the stored diff, base revision, summary and hash, webhook and cron runs cannot apply. Typed apply with the printed hash gives exactly one new revision equal to the proposal bytes and one replay from `transform`; `extract` is reused (identical attempt timestamps and outputs), `load` ran, the original execution stays FAILED |
| S3 idempotent retry | Applying again changes nothing: same revision count, same execution count, same journal |
| S4 stale | A flow edited after the proposal makes apply refuse with no write; a fresh proposal on the new revision then verifies and keeps the out-of-band edit |
| S5 rollback | A proposal that validates but still fails (approved through the model tool path): replay FAILED, flow source byte-identical to the base again, revision history keeps the failed fix, rerun is a no-op |
| S6a, S6b interrupted apply | The apply process is killed right before the replay call. The flow is already updated, no replay exists, a retry during the lock says another apply is running. After the lock expires the next apply resumes from the persisted step and starts exactly one replay |
| S7 pause | A paused `refund_approval` shows up in `/kestra pauses`; deny (tool path), webhook and cron cannot resume; a typed resume needs the task run prefix; invalid input is refused by Kestra; a typed resume with inputs finishes the execution and the downstream task sees them; a decision from an old snapshot after an out-of-band resume is refused with no second write; a typed kill ends a paused execution |
| S8 concurrent apply | Two sessions type the same apply at the same moment: one write, one replay, the other is told to wait or sees it verified |
| S9 slow replay | A correct fix whose task outlasts the wait: the first apply says the replay is still running and rolls nothing back, the second apply follows the same replay and verifies, with one write and one replay |
| S10 chat surface | In a telegram-like session with no approval notifier, `/kestra show` prints the command, a wrong hash is refused and the typed apply verifies |
| S11 flow shapes | Parallel failing branch fixed and verified with reuse; failed Loop and Subflow are diagnose-only with a pointer to the failing child; the child is fixed and verified; a fix whose replay reaches a Pause returns pending with an explanation, then verifies after the human resumes; a failure that is not a task failure (concurrency limit) opens read-only and refuses a fix; a tolerated failure (`allowFailure`) ends WARNING and is handed to a human instead of being called verified |
| S12 real terminal UI | The slash commands typed into the actual `hermes chat --cli` in a pseudo-terminal: status, show, a wrong hash refused, the right hash verified; Kestra agrees |

## Notes for reviewers

- Scenarios that use scripted proposals pause the forwarder (restored in a `finally` block) so the model cannot race them on the `hko.e2e` copies.
- S6b waits up to `replay_timeout_seconds + 120 = 150` seconds for the dead process's lock unless `--skip-lock-wait` is given. Evidence runs do not use the flag.
- Kestra 2.0.9 stamps `originalId` on the original execution itself after a replay, so replay counts exclude the original.
- `tests/e2e/hermes_driver.py` plays one chat session per call. `--platform webhook|cron` gives an unattended session, `--platform telegram` a chat session without an approval notifier; `--fault crash-before-replay` is the only fault injection.
