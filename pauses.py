"""Pause desk: turn Kestra `Pause` tasks into questions a human can answer from chat.

Stale guard: a decision is bound to (execution id, paused task run id). Right before the write the
execution is read again and must still be PAUSED on that exact task run, otherwise nothing is written.
Idempotent: a recorded decision for the same execution and task run is returned instead of acting twice.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from .client import KestraClient, KestraError, WaitTimeout
from .flowsrc import FlowSourceError, PauseTask, parse_flow, render_inputs
from .incidents import Policy, PolicyError, StaleError, _state, iso
from .store import Store

SETTLE_TIMEOUT_S = 30.0
PAUSE_PAGE_SIZE = 100
MAX_PAUSE_PAGES = 5


def _seconds_since(date: str | None) -> int | None:
    if not date:
        return None
    try:
        then = datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - then).total_seconds()))


def _paused_since(run: dict) -> str | None:
    for item in reversed((run.get("state") or {}).get("histories") or []):
        if item.get("state") == "PAUSED":
            return item.get("date")
    return None


class PauseDesk:
    def __init__(self, client: KestraClient, store: Store, policy: Policy):
        self.client = client
        self.store = store
        self.policy = policy
        self.last_truncated = False

    # ----------------------------------------------------------------- read
    def _pause_tasks(self, namespace: str, flow_id: str, revision: int | None,
                     cache: dict[tuple, dict[str, PauseTask]]) -> dict[str, PauseTask]:
        key = (namespace, flow_id, revision)
        if key not in cache:
            try:
                source = self.client.get_flow(namespace, flow_id, revision=revision).get("source", "")
                cache[key] = parse_flow(source).pause_tasks
            except (KestraError, FlowSourceError):
                cache[key] = {}
        return cache[key]

    def _rows_for(self, execution: dict, cache: dict[tuple, dict[str, PauseTask]]) -> list[dict]:
        namespace, flow_id = execution["namespace"], execution["flowId"]
        revision = execution.get("flowRevision")
        pauses = self._pause_tasks(namespace, flow_id, revision, cache)
        rows = []
        paused_runs = [r for r in execution.get("taskRunList") or [] if _state(r) == "PAUSED"]
        for run in paused_runs:
            task = pauses.get(run.get("taskId"))
            inputs = execution.get("inputs") or {}
            rows.append({
                "execution_id": execution["id"], "namespace": namespace, "flow_id": flow_id,
                "flow": f"{namespace}.{flow_id}", "revision": revision,
                "paused_task_run_id": run.get("id"), "paused_task_id": run.get("taskId"),
                "description": render_inputs(task.description, inputs) if task else "",
                "declared_inputs": task.on_resume if task else [],
                "paused_for_seconds": _seconds_since(_paused_since(run)),
                "execution_inputs": inputs,
                "labels": {l.get("key"): l.get("value") for l in execution.get("labels") or []
                           if not str(l.get("key", "")).startswith("system.")},
                "protected": self.policy.is_protected(namespace, flow_id),
                # Kestra's resume takes no task run id: with several paused task runs it acts on the execution.
                "paused_task_run_count": len(paused_runs), "ambiguous": len(paused_runs) > 1,
            })
        return rows

    def list_pauses(self) -> list[dict]:
        """PAUSED executions in allowed namespaces. The search is filtered per allowed prefix on the server (so pauses
        elsewhere cannot push ours off the first page) and paged up to a hard cap; `self.last_truncated` says if the cap hit."""
        self.last_truncated = False
        if not self.policy.namespaces:
            return []  # fail closed: nothing is allowed, so nothing is listed
        rows: list[dict] = []
        cache: dict[tuple, dict[str, PauseTask]] = {}
        seen: set[str] = set()
        prefixes = sorted({p.strip().rstrip(".") for p in self.policy.namespaces if p and p.strip().strip(".")})
        for prefix in prefixes:
            for page in range(1, MAX_PAUSE_PAGES + 1):
                batch = self.client.search_executions(state=["PAUSED"], size=PAUSE_PAGE_SIZE, namespace_prefix=prefix, page=page)
                for light in batch:
                    if light["id"] in seen or not self.policy.namespace_allowed(light.get("namespace", "")):
                        continue
                    seen.add(light["id"])
                    rows.extend(self._rows_for(self.client.get_execution(light["id"]), cache))
                if len(batch) < PAUSE_PAGE_SIZE:
                    break
            else:
                self.last_truncated = True
        return rows

    # ----------------------------------------------------------------- decide
    def decide(self, execution_id: str, paused_task_run_id: str, action: str,
               inputs: dict[str, str] | None, *, actor: str) -> dict:
        if action not in {"resume", "kill"}:
            raise ValueError("action must be 'resume' or 'kill'.")
        inputs = {str(k): str(v) for k, v in (inputs or {}).items()}
        if action == "kill" and inputs:
            raise ValueError("kill does not accept resume inputs.")
        recorded = self.store.pause_get(execution_id, paused_task_run_id)
        if recorded and recorded["status"] == "done":
            return self._record(recorded, noop=True)

        owner = f"{actor}#{uuid.uuid4().hex[:8]}"
        lock_name = f"pause:{execution_id}"
        if not self.store.lock(lock_name, owner, 120):
            return {"execution_id": execution_id, "paused_task_run_id": paused_task_run_id, "status": "busy",
                    "noop": True, "message": "Another decision for this execution is running. Nothing was written."}
        try:
            recorded = self.store.pause_get(execution_id, paused_task_run_id)
            if recorded and recorded["status"] == "done":
                return self._record(recorded, noop=True)
            return self._decide_locked(execution_id, paused_task_run_id, action, inputs, actor, recorded)
        finally:
            self.store.unlock(lock_name, owner)

    @staticmethod
    def _record(row: dict, *, noop: bool) -> dict:
        result = dict(row.get("result") or {})
        result.update(noop=noop, recorded_action=row["action"], decided_by=row["actor"], decided_at=iso(row["updated"]))
        if noop:
            result["message"] = f"Already decided ({row['action']}) for this paused task run. Nothing was written."
        return result

    def _decide_locked(self, execution_id: str, task_run_id: str, action: str, inputs: dict[str, str],
                       actor: str, recorded: dict | None) -> dict:
        execution = self.client.get_execution(execution_id)  # fresh read right before the write
        namespace, flow_id = execution.get("namespace", ""), execution.get("flowId", "")
        self.policy.check_namespace(namespace)
        if self.policy.is_protected(namespace, flow_id):
            raise PolicyError(f"{namespace}.{flow_id} is a protected flow: its pauses are diagnose-only here.")
        run = next((r for r in execution.get("taskRunList") or [] if r.get("id") == task_run_id), None)
        still_paused = _state(execution) == "PAUSED" and run is not None and _state(run) == "PAUSED"
        journal = self.store.journal_add
        subject = f"pause:{execution_id}:{task_run_id}"

        if not still_paused:
            # An earlier call may have written and been interrupted before recording. Adopt only if its
            # recorded intent equals this request; otherwise the world moved on and we must not act.
            if (recorded and recorded["status"] == "intent" and recorded["action"] == action
                    and recorded["inputs"] == inputs and run is not None and _state(run) != "PAUSED"):
                journal(subject, "adopt_decision", {"action": action, "task_run_state": _state(run)})
                return self._finish(execution_id, task_run_id, action, execution, run, adopted=True)
            found = _state(execution) if run is not None else "a different task run"
            raise StaleError(f"Execution {execution_id} is no longer paused on task run {task_run_id} "
                             f"(now {_state(execution)}, task run state {found}). Nothing was written. "
                             "Refresh the pauses list and ask again.")

        paused_count = sum(1 for r in execution.get("taskRunList") or [] if _state(r) == "PAUSED")
        if action == "resume" and paused_count > 1:
            raise PolicyError(f"This execution has {paused_count} paused task runs; Kestra's resume applies to the whole "
                              "execution, not to one task run, so the exact task run cannot be enforced. Resolve it in the "
                              "Kestra UI. (kill is still possible and ends the whole execution.)")
        if action == "resume":
            declared = self._declared_inputs(namespace, flow_id, execution.get("flowRevision"), run.get("taskId"))
            unknown = sorted(set(inputs) - {d["id"] for d in declared})
            if unknown:
                raise PolicyError(f"Inputs {unknown} are not declared by this Pause. Declared: "
                                  f"{[d['id'] for d in declared]}. Kestra would silently ignore them, so nothing was sent.")
            check = self.client.validate_resume(execution_id, inputs)
            errors = [f"{i.get('input', {}).get('id')}: {e.get('message')}"
                      for i in check.get("inputs", []) for e in i.get("errors", [])]
            if errors:
                raise PolicyError("Kestra rejected the resume inputs: " + "; ".join(errors)[:600])

        self.store.pause_put_intent(execution_id, task_run_id, action, inputs, actor)
        journal(subject, f"{action}_requested", {"actor": actor, "inputs": sorted(inputs)})
        try:
            if action == "resume":
                self.client.resume(execution_id, inputs or None)
            else:
                self.client.kill(execution_id)
        except KestraError as exc:
            # Resume on a non-paused execution is HTTP 500 in Kestra 2.0.9: re-read instead of trusting the code.
            after = self.client.get_execution(execution_id)
            moved = next((r for r in after.get("taskRunList") or [] if r.get("id") == task_run_id), None)
            if moved is not None and _state(moved) != "PAUSED":
                journal(subject, "write_error_but_moved", {"error": str(exc)[:200]})
                return self._finish(execution_id, task_run_id, action, after, moved)
            journal(subject, "write_refused", {"error": str(exc)[:200]})
            raise
        settled, settled_run = self._settle(execution_id, task_run_id, action)
        if action == "kill":
            # Kestra passes through KILLING before KILLED; report the final state, not the transient one.
            try:
                settled = self.client.wait_terminal(execution_id, timeout=SETTLE_TIMEOUT_S, poll=0.4)
                settled_run = next((r for r in settled.get("taskRunList") or [] if r.get("id") == task_run_id), settled_run)
            except WaitTimeout:
                pass
        return self._finish(execution_id, task_run_id, action, settled, settled_run)

    def _declared_inputs(self, namespace: str, flow_id: str, revision: int | None, task_id: str | None) -> list[dict]:
        pauses = self._pause_tasks(namespace, flow_id, revision, {})
        task = pauses.get(task_id or "")
        return task.on_resume if task else []

    def _settle(self, execution_id: str, task_run_id: str, action: str) -> tuple[dict, dict | None]:
        deadline = time.monotonic() + SETTLE_TIMEOUT_S
        while True:
            execution = self.client.get_execution(execution_id)
            run = next((r for r in execution.get("taskRunList") or [] if r.get("id") == task_run_id), None)
            if run is None or _state(run) != "PAUSED":
                return execution, run
            if time.monotonic() >= deadline:
                return execution, run
            time.sleep(0.4)

    def _finish(self, execution_id: str, task_run_id: str, action: str, execution: dict, run: dict | None,
                *, adopted: bool = False) -> dict:
        run_state = _state(run) if run else "UNKNOWN"
        pending = run_state == "PAUSED"
        result = {
            "execution_id": execution_id, "paused_task_run_id": task_run_id, "action": action,
            "status": "pending" if pending else ("resumed" if action == "resume" else "killed"),
            "new_state": _state(execution), "paused_task_state": run_state,
            "namespace": execution.get("namespace"), "flow_id": execution.get("flowId"),
            "adopted_interrupted_decision": adopted, "noop": False,
        }
        if action == "kill":
            result["message"] = "Kill ends the whole execution, not only this task run."
        if pending:
            result["message"] = "Kestra accepted the request but the task is still PAUSED; check again shortly."
            return result  # leave the intent open: calling again reconciles
        row = self.store.pause_finish(execution_id, task_run_id, result)
        self.store.journal_add(f"pause:{execution_id}:{task_run_id}", "decided",
                               {"action": action, "new_state": result["new_state"]})
        result["decided_at"] = iso(row["updated"])
        return result
