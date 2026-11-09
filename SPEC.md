# kestra-oncall: spec and contracts

A Hermes Agent plugin that puts Hermes on call for Kestra. When a Kestra execution fails, Hermes
reads the failure, proposes a fix to the flow, and asks a human. After approval it saves the fix
as a new flow revision, replays the failed execution from the task that failed, follows the replay
and verifies it. If the replay fails it restores the previous revision. It also turns every Kestra
`Pause` (human approval) into a chat question that the human can answer from any Hermes surface.

Entry for the Hermes Agent Plugin Hackathon (deadline 2026-11-11 17:00 EST). Judged on usefulness,
reliability, polish, creativity; favours plugins that do more than a prompt or a skill.

## Outcome (state this before implementing, verify it after)

- Incident: an execution in an allowed namespace ends FAILED.
- Allowed targets: flows whose namespace matches a configured prefix list (`namespaces`), minus
  `protected_flows` (diagnose only, never propose or write).
- Bounded change: exactly one flow update (new revision) plus one replay execution per approved
  proposal. Nothing else in Kestra is written. Rollback is one more flow update back to the base source.
- Protected state: tasks that succeeded in the original execution are NOT rerun by the replay
  (Kestra replay from a task run copies earlier task runs). Other flows, other revisions, other
  executions are untouched. The original failed execution stays as it is (audit trail).
- Observable recovery: the replay execution reaches SUCCESS, its previously failed task is SUCCESS,
  and the task runs before it are reused (same ids/outputs), proven by reading the replay execution.

## Repository layout and ownership (one owner per file; never edit a file you do not own)

| Path | Owner |
|---|---|
| `client.py`, `store.py`, `incidents.py`, `pauses.py`, `flowsrc.py` | agent **core** |
| `tests/unit/test_core_*.py`, `tests/live/test_live_core.py`, `tests/conftest.py` | agent **core** |
| `__init__.py`, `plugin.yaml`, `tools.py`, `hooks.py`, `commands.py`, `cli.py`, `settings.py` | agent **glue** |
| `skills/kestra-oncall/SKILL.md`, `tests/unit/test_glue_*.py` | agent **glue** |
| `kestra/flows/*`, `kestra/demo/*`, `rig/*`, `tests/e2e/*`, `evidence/*` | agent **rig** |
| `README.md`, `docs/*`, `catalog/*`, `LICENSE`, `dashboard/*`, `desktop/*`, `assets/*` | agent **docs** |
| `SPEC.md`, `AGENTS.md`, `.work/STATE.json`, `.gitignore` | orchestrator (Kiro) |

Need a change in a file you do not own? Write the request to `.work/<you>/REQUESTS.md` (what, why,
exact proposed text) and carry on with a local shim. The orchestrator routes it.

## Runtime environment (already running, shared, do not recreate)

- Kestra **2.0.9** (latest, released 2026-11-03): container `hko-kestra`, image `kestra/kestra:v2.0.9`,
  API `http://127.0.0.1:28090`, tenant `main`, basic auth. Docker network `hko-net` (10.250.90.0/24,
  host gateway 10.250.90.1). From inside the container the host is `host.docker.internal` or 10.250.90.1.
- Credentials: `/home/asuran/hko-rig/secrets/kestra.env` (KESTRA_URL, KESTRA_USERNAME, KESTRA_PASSWORD,
  KESTRA_TENANT, HKO_WEBHOOK_TOKEN). `source` it in shell; never print, copy into the repo, or log values.
- Kestra secrets already set on the container (Kestra OSS `SECRET_*` env, base64): `HERMES_WEBHOOK_URL`
  = `http://10.250.90.1:28644/webhooks/kestra-oncall` and `HERMES_WEBHOOK_TOKEN` (= HKO_WEBHOOK_TOKEN).
  Use them in flows as `{{ secret('HERMES_WEBHOOK_URL') }}` / `{{ secret('HERMES_WEBHOOK_TOKEN') }}`.
- Kestra OpenAPI for 2.0.9: `/home/asuran/hko-rig/kestra/openapi-2.0.9.yml`. Kestra 2.0 moved actions
  under `/executions/{id}/actions/...` (replay, resume, resume/validate). Check paths there, not from memory.
- Hermes Agent: source checkout `/home/asuran/hko-rig/hermes-agent` (main @ c3354ea, newer than the
  v0.23.0 release), venv python `/home/asuran/hko-rig/hermes-agent/.venv/bin/python` (3.14, has pytest),
  CLI `/home/asuran/hko-rig/hermes-agent/.venv/bin/hermes`. Plugin docs live in
  `website/docs/developer-guide/plugins/index.md` (2,700 lines; grep it),
  `website/docs/developer-guide/plugins/catalog-submission.md`, `.../desktop-plugin-sdk.md`,
  `.../observer-hooks.md`, `website/docs/user-guide/messaging/webhooks.md`. Read the real code
  (`hermes_cli/plugins*.py`, `plugins/plugin_storage.py`, `agent/secret_scope.py`,
  `gateway/platforms/webhook.py`) when the docs are unclear.
- Rig HERMES_HOME: `/home/asuran/hko-rig/hermes-home` (owned by agent rig). NEVER touch `~/.hermes`.
- LLM for Hermes runs: OpenAI-compatible pool `http://127.0.0.1:20980/v1`, model `gpt-6-astra`.

## Dependencies

Standard library for HTTP (`urllib.request`). YAML via `ruamel.yaml` (a Hermes core dependency, pinned
0.18.16). No other third-party imports in plugin code. Tests use pytest from the rig venv.

## Kestra namespaces used for testing (isolation between agents)

- core live tests: `hko.coretest` (and children). glue tests: fakes only, or `hko.gluetest`.
- rig demo: `hko.demo`; e2e scenarios: `hko.e2e`; forwarder flow lives in `hko.system`.
- Delete only flows/executions you created, in your own namespace.

## Core module contracts (agent core implements; agent glue codes against these)

```python
# client.py
class KestraError(Exception):            # .status (int|None), .body (str), .method, .path
class KestraClient:
    def __init__(self, base_url: str, tenant: str = "main", *, username: str|None = None,
                 password: str|None = None, token: str|None = None, timeout: float = 20.0): ...
    def server_version(self) -> str                                    # GET /api/v1/configs
    def get_execution(self, execution_id: str) -> dict
    def search_executions(self, *, namespace: str|None=None, flow_id: str|None=None,
                          state: list[str]|None=None, size: int=25, sort: str="state.startDate:desc") -> list[dict]
    def get_logs(self, execution_id: str, *, task_run_id: str|None=None, min_level: str="WARN",
                 limit: int=200) -> list[dict]
    def get_flow(self, namespace: str, flow_id: str, *, revision: int|None=None) -> dict   # includes "source", "revision"
    def list_revisions(self, namespace: str, flow_id: str) -> list[dict]
    def validate_flow(self, source: str) -> list[dict]                 # [] when valid; else violations
    def update_flow(self, namespace: str, flow_id: str, source: str) -> dict   # returns flow with new revision
    def create_flow(self, source: str) -> dict
    def delete_flow(self, namespace: str, flow_id: str) -> None
    def replay(self, execution_id: str, *, task_run_id: str|None, revision: int|None) -> dict  # new execution
    def resume(self, execution_id: str, inputs: dict[str, str]|None = None) -> None   # multipart form
    def validate_resume(self, execution_id: str, inputs: dict[str, str]|None = None) -> dict
    def kill(self, execution_id: str) -> None
    def trigger(self, namespace: str, flow_id: str, inputs: dict|None=None, *, wait: bool=False) -> dict
    def wait_terminal(self, execution_id: str, *, timeout: float, poll: float = 2.0,
                      leave_states: set[str]|None=None) -> dict          # polls get_execution

# store.py  (SQLite; glue passes plugins.plugin_storage.plugin_db("kestra-oncall"), tests pass a path)
class Store:
    def __init__(self, conn_or_path): ...                               # creates schema idempotently
    # incidents, proposals, journal (append-only step log), pause_decisions, locks
    # lock(incident_id, owner, ttl_s) -> bool  is a compare-and-set on a lock row; unlock(...)

# incidents.py
@dataclass
class Policy:
    namespaces: list[str]            # allowed prefixes; EMPTY means nothing is allowed (fail closed)
    protected_flows: list[str]       # "namespace.flowId" exact ids: diagnose only
    rollback_on_failure: bool = True
    replay_timeout_s: int = 300
    max_log_lines: int = 200

class Oncall:
    def __init__(self, client: KestraClient, store: Store, policy: Policy): ...
    def open_incident(self, execution_id: str) -> dict
        # read-only. Refuses (raises PolicyError) outside allowed namespaces; refuses non-FAILED.
        # Deterministic id "INC-" + 8 hex of sha256(execution_id): reopening is idempotent.
        # Bundle: flow ns/id, execution revision, failed task id + taskRunId (the first FAILED
        # leaf task run), error logs (capped), task inputs/outputs when present, flow source at the
        # execution's revision, last SUCCESS execution of the same flow and a unified diff of the
        # flow source between that revision and the failing revision ("what changed"), protected flag.
    def list_incidents(self, *, status: str|None = None, limit: int = 20) -> list[dict]
    def get_incident(self, incident_id: str) -> dict
    def propose_fix(self, incident_id: str, new_source: str, rationale: str) -> dict
        # Refuses for protected flows. Same namespace + id required. Validates with Kestra
        # /flows/validate (must be clean). The failed task id must still exist (replay target).
        # Captures base_revision + base_source_sha256 from a FRESH get_flow. Stores the proposal and
        # returns {incident_id, proposal_sha256, base_revision, diff, diff_stat, validation}.
    def apply_fix(self, incident_id: str, proposal_sha256: str, *, actor: str) -> dict
        # The only write path. Binds to the exact proposal bytes. Idempotent and resumable, with a
        # persisted step journal: lock -> re-read flow (stale guard: revision and source hash must
        # still equal the proposal base, else status "stale" and no write) -> if current source
        # already equals the proposal adopt it, else update_flow -> read back (revision == base+1 and
        # source equal, else status "conflict", stop) -> replay(original, failed taskRunId, fixed
        # revision), persist replay id immediately (on resume without an id, adopt an existing replay
        # whose originalId == original and flowRevision == fixed) -> wait_terminal -> on SUCCESS
        # verify (failed task now SUCCESS, earlier task runs reused) -> "verified"; on FAILED/KILLED/
        # timeout and rollback_on_failure: rollback only if the flow is still at the fixed revision,
        # read back -> "rolled_back"; otherwise "needs_human". Re-running on "verified" is a no-op.
    def summary(self) -> dict                                           # counts for /kestra status

# pauses.py
class PauseDesk:
    def __init__(self, client: KestraClient, store: Store, policy: Policy): ...
    def list_pauses(self) -> list[dict]
        # PAUSED executions in allowed namespaces; for each: execution id, flow, paused task id +
        # taskRunId, pause description, declared onResume inputs (from the flow source at the
        # execution revision), paused-for seconds, execution inputs and labels.
    def decide(self, execution_id: str, paused_task_run_id: str, action: str,
               inputs: dict[str, str]|None, *, actor: str) -> dict
        # action "resume" | "kill". Stale guard: execution must still be PAUSED on the same task run.
        # resume: validate_resume first, then resume, then wait until it leaves PAUSED and report the
        # new state. Idempotent: a recorded decision for the same execution+taskRun returns the record.
```

Every public method returns plain JSON-serialisable dicts. Errors the agent should see are raised as
`PolicyError`, `StaleError`, `KestraError` (all in `incidents.py`/`client.py`) with clear messages.

## Hermes glue contracts (agent glue)

- Plugin name `kestra-oncall`, toolset `kestra_oncall`. Tools:
  `kestra_status`, `kestra_incident_open`, `kestra_incidents`, `kestra_propose_fix`,
  `kestra_apply_fix` (write), `kestra_pauses`, `kestra_pause_decide` (write).
- `pre_tool_call` hook: for the two write tools return `{"action": "approve", "message": ...}`. The
  message is built from the STORE (the real diff/inputs), never from model-supplied text. A missing or
  unknown proposal returns `{"action": "block", ...}`. Unattended runs (webhook, cron) must not hang:
  rely on Hermes's approval gate failing closed there; the webhook prompt also tells the model not to apply.
- Slash command `/kestra` with `status | incidents | show <id> | apply <id> | pauses | resume <exec> [k=v ...] | kill <exec>`;
  writes go through `ctx.dispatch_tool` so the same approval gate applies.
- CLI `hermes kestra-oncall setup|doctor`: doctor checks connectivity, version, auth, policy; setup
  prints the Hermes webhook route block and installs nothing without `--apply`.
- Secrets only via `agent.secret_scope.get_secret` (declared in the manifest). Settings via
  `config_schema` (`kestra_url`, `tenant`, `namespaces`, `protected_flows`, `rollback_on_failure`,
  `replay_timeout_seconds`, `max_log_lines`). Capabilities block must match exactly what is registered.
- Bundled skill `kestra-oncall` teaches the triage procedure (open, read, propose, never apply in an
  unattended run, summarise with the incident id and `/kestra apply <id>`).

## Kestra side (agent rig)

- `kestra/flows/hermes_oncall_forwarder.yaml` in `hko.system`: Flow trigger on FAILED executions of
  allowed namespaces -> HTTP Request POST to the Hermes webhook route `kestra-oncall` with header
  `X-Gitlab-Token` (plain shared token, supported by Hermes's webhook adapter) from a Kestra secret,
  JSON body `{event_type: "kestra.execution.failed", execution_id, namespace, flow_id, state, ...}`.
- Demo fixtures in `hko.demo`: `orders_etl` (extract succeeds, transform fails on a renamed column),
  `refund_approval` (Pause with onResume inputs), `healthy_report` (healthy control, must never trigger),
  a protected flow, and a lookalike in a non-allowed namespace (must be ignored).
- e2e scenarios with exact assertions read back from Kestra's API (counts, states, revisions, task-run
  reuse), retries (second apply is a no-op), stale proposal, refused/ineffective write, rollback,
  interrupted apply resumed, pause stale guard. Evidence manifest with SHA-256 of every file.

## Definition of done for the whole plugin

1. `hermes plugins validate <repo> --install-deps` passes; `hermes plugins doctor <repo>` passes.
2. Unit tests pass; live core tests pass against Kestra 2.0.9; e2e passes through a real Hermes
   gateway + webhook + real LLM (gpt-6-astra) with evidence.
3. README with a 5-minute quick start, disclosures (network calls, writes, stored data), screenshots.
4. Catalog entry YAML ready (`catalog/kestra-oncall.yaml`), not submitted (orchestrator submits).
