# Rules for every agent working in this repository

Read `SPEC.md` first. It holds the outcome, file ownership, contracts and the running rig.

## Scope and safety

- Work only inside `/home/asuran/hermes-kestra-oncall` (your owned files) and, for agent rig only,
  `/home/asuran/hko-rig/hermes-home` plus `/home/asuran/hko-rig/run`. Read anything else you need, write nothing else.
- Never edit a file another agent owns (see the ownership table). Put requests in `.work/<you>/REQUESTS.md`.
- Never touch `~/.hermes`, `~/.config/opencode`, other Docker containers, networks or volumes. The
  only container you may use is `hko-kestra` (do not stop, restart or recreate it; ask in REQUESTS.md).
  Any new container, volume or temp path must start with `hko-` (temp files under `/tmp/hko-<you>-*`).
- Never print, log, commit or write secret values (Kestra password, webhook token, LLM key). Read them
  from `/home/asuran/hko-rig/secrets/kestra.env` or the rig HERMES_HOME `.env` at runtime.
- No git commits, pushes, PRs, issues, X posts or any other external publication. The orchestrator commits.
- No em dashes or en dashes in any file you write (use commas, periods, colons or parentheses).
- Keep commands bounded (timeouts on every network call and poll loop; no command over 10 minutes).

## Code conventions

- Python 3.11+ compatible, typed, small functions, docstrings that explain why. Standard library HTTP.
  YAML through `ruamel.yaml`. No other third-party imports in plugin code.
- All imports between plugin modules are relative (`from .client import KestraClient`). Plugin modules
  other than `__init__.py`'s `register()` body must not import Hermes internals at module import time,
  so the core is testable without Hermes. `tests/conftest.py` loads the repo root as package `kestra_oncall`.
- Public functions return JSON-serialisable dicts. Error messages say what happened and what to do next.
- Run tests with `/home/asuran/hko-rig/hermes-agent/.venv/bin/python -m pytest`.

## Resumability (mandatory)

- Keep `.work/<you>/CHECKPOINT.md` (phase, owned files, completed checks with exact commands and results,
  Kestra execution ids, known failures, the exact next action) and `.work/<you>/journal.jsonl`
  (one JSON line per meaningful step). Update both after each meaningful step and before you stop.
- If you are resumed, read your CHECKPOINT.md first and continue from NEXT ACTION. Never discard work.
- When your assignment is complete, write `.work/<you>/DONE.md`: what was built, how it was verified
  (commands + results), open gaps, and anything the orchestrator must integrate.

## Quality bar (judges score usefulness, reliability, polish, creativity)

- Prove behaviour on the real system (Kestra 2.0.9 API, real Hermes CLI), not only with fakes.
- Prove selection both ways: the detector acts on the real failure and spares the healthy control,
  protected flows and lookalikes.
- Treat approval as a stale snapshot: re-read state right before every write and stop on change.
- Safe retries: running a step again must reconcile actual state, never duplicate a write or a replay.
- Claims must be exact (counts, ids, states). No invented numbers.
