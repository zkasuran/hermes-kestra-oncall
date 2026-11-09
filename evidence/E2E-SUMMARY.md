# End-to-end summary

Kestra 2.0.9, Hermes `c3354ea199` (main), plugin commit `6d7e9f852c01`. Run on 2026-11-09T12:44:40+0530, 345.8 s, all passed.

| Scenario | What it proves | Assertions | Key ids |
|---|---|---|---|
| S1 | detect: failure -> forwarder -> Hermes webhook run (real LLM) -> incident + proposal; healthy, lookalike, protected create no proposal | 19/19 PASS | failing=7eZeCyhn4dSx05ZFsFc3IF<br>incident=INC-fc91a049 |
| S1b | webhook hardening: wrong token rejected, duplicate delivery ignored, a failure of the forwarder namespace never loops | 6/6 PASS |  |
| S2 | approve and repair: deny, unattended refusal, then approved apply -> one revision, one replay from transform, extract reused, load ran | 25/25 PASS | original_execution=7eZeCyhn4dSx05ZFsFc3IF<br>replay_execution=3UfvbR34JO0ayEkwdLQ7oC |
| S3 | idempotent retry: apply the same proposal again -> no revision, no replay, no journal growth | 5/5 PASS |  |
| S4 | stale proposal: flow edited out of band after propose -> apply refused, no write | 11/11 PASS | original_execution=27Y9fXCnHFSF1pl7wvb9Ih<br>incident=INC-6b8998f6 |
| S5 | rollback: a proposal that validates but still fails -> replay FAILED -> flow back to the base source | 12/12 PASS | original_execution=7EO9Qj19uReLekpytcFfmJ<br>incident=INC-31c4e566<br>replay_execution=686W4sZhETVpo3ah9OJGL3 |
| S6a | interrupted apply, phase 1: process killed after the flow update, before the replay | 8/8 PASS | original_execution=5a1w2KKb9DgPOUZAk6tbCL<br>incident=INC-99a5be32 |
| S6b | interrupted apply, phase 2: next apply adopts the written fix and does exactly one replay | 15/15 PASS | original_execution=5a1w2KKb9DgPOUZAk6tbCL<br>replay_execution=cdy3rYv0ri9m0nkDuJYeF |
| S7 | pause: PAUSED refund_approval appears in /kestra pauses, resumes with inputs, downstream sees them; stale guard when resumed out of band first | 18/18 PASS | execution=BkQY3FS0wqj2W0Gpwz1nt<br>stale_execution=2cvV6tfHiL5jIU0OiOaHT7<br>killed_execution=5G1G5lLA5gDh8R9cBN35be |
| S8 | concurrent apply: two sessions approve the same proposal at the same moment -> one write, one replay | 8/8 PASS | original_execution=7rOMGeno2d3OGSWTN7Gv0<br>incident=INC-90fc69fe<br>replay_execution=2Fu06QQNK62ye4CWeavm6Z |
| S9 | slow replay: apply returns pending after the wait, the fix is NOT rolled back, the next apply follows the same replay and verifies | 21/21 PASS | original_execution=4QZSE7ibIh9BnsfEtbCX6q<br>replay_execution=4Fhdiudla0tJWyvzlaQKga<br>incident=INC-998f8631 |
| S10 | chat surface: the typed command works where no Hermes approval card can exist (telegram session, no notifier) | 7/7 PASS | incident=INC-80f0203c<br>original_execution=31pmDEgExmQPn28GzCPos |
| S11 | flow shapes: Parallel branch (fixed and verified), Loop and Subflow (diagnose-only, honest refusal), replay that reaches a Pause (pending then verified), failure that is not a task failure (refused) | 26/26 PASS | parallel: execution=3oWRJKy24PunwF4iinOvZM, replay=QWdXfyQze9sQzy9n80Two<br>loop: execution=2k8c5woov6sDZwkwgsbiKO<br>subflow: parent_execution=4JOh2pgg17C3w8srOFErNe<br>pause_after: execution=3PIbap6H3MqFMRwHk4IZRi, replay=6tnAldlOsH7mYueFqQJJcQ<br>allow_failure: execution=7KMIpThBZ59UT1mTKc9Ngz, replay=2cvVudRpyEapSwW2xQteKl |
| S12 | real terminal UI: slash commands typed into the actual `hermes chat --cli` in a pseudo-terminal (no scripted callback, no model turn) | 8/8 PASS | incident=INC-e3667dbb<br>original_execution=1CrgckQF9i9UDrmbh0kgN5 |

Total: 189/189 assertions.

Hermes v0.23.0 compatibility run (scenarios S1, S1b, S2, S3): all passed, S1 19/19, S1b 6/6, S2 25/25, S3 5/5.

## README quick start, run in order on an empty HERMES_HOME

| Step | Seconds | Result |
|---|---|---|
| 1 start Kestra 2.0.9 with basic auth and the two forwarder secrets | 7.9 | ok |
| 2 install and enable the plugin | 31.8 | ok |
| 3 configure and run `hermes kestra-oncall doctor` | 2.9 | ok |
| 4 webhook route, WEBHOOK_SECRET, start the gateway | 6.3 | ok |
| 5 deploy the forwarder (README sed / curl) | 25.0 | ok |
| 6 deploy the demo flows and run orders_etl | 1.6 | ok |
| 7a wait for the incident and proposal (model) | 117.0 | ok |
| 7b type /kestra apply <id> <hash> in the real terminal UI | 113.3 | ok |

Total 305.8 s; all steps completed: True. Substitutions: local plugin path instead of the GitHub install line; --deliver log (no chat bot on this machine); model block and fallback models written into config.yaml; aiohttp installed into the managed environment (README requirement 1) by rig/deps.py; ports 58705 (Kestra) and 54649 (webhook).

## Not covered

- The approval a person gives is the typed command (/kestra apply <id> <hash>, resume, kill). It was run through the plugin command handler, in a telegram-like session with no approval notifier (S10) and typed into the real terminal UI in a pseudo-terminal (S12). No live Telegram, Discord or Slack client was used, and no person clicked a Hermes approval card: the answer to the card on the model tool path (deny or once) is scripted through terminal_tool.set_approval_callback, with the card text captured and asserted.
- The model-driven webhook run (S1) depends on a shared LLM pool that rate-limits gpt-6-astra; the run falls back to gpt-6.1-sol or gpt-5.6-sol when the primary is limited (see evidence/logs/gateway_model_tools.txt). The proposal text is the model's and can differ between runs.
- S4 to S6 and S8 to S12 use scripted proposals (not model written) on hko.e2e copies, with the forwarder paused for those scenarios so the model cannot race them. S1 to S3 use the model proposal on hko.demo.orders_etl.
- S6 simulates the interruption by killing the apply process at the replay call (fault injected by the test driver into KestraClient.replay); a power loss or SIGKILL at other points is covered only by the unit tests of core.
- Concurrent apply (S8) uses two processes on one machine sharing one SQLite store; separate hosts with separate stores would not share the lock.
- Hermes v0.23.0 ran S1, S1b, S2 and S3 only (evidence/v023-results.json); the other scenarios ran on Hermes main only.
- Kestra OSS with basic auth only; Enterprise namespaces, RBAC and API tokens are untested. Kestra 2.0.9 only.
- The README quick start check (evidence/quickstart-check.json) substitutes a local plugin path for the GitHub install line, --deliver log for a chat bot, a model block written into config.yaml, and rig/deps.py for the aiohttp requirement.
- Observed Kestra limits, not defects of the plugin: a failed Loop cannot be replayed by Kestra 2.0.9 (diagnose only); a tolerated failure (allowFailure) ends the replay WARNING and is handed to a human, not called verified.
