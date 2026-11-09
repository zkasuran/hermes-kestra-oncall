"""Incident engine: open (read-only), propose (stores bytes), apply (the only write path).

State machine of apply_fix, persisted in `incidents.apply_state` after every step so an interrupted run
can be resumed by calling apply_fix again with the same proposal hash:

    start -> written (flow revision N+1 holds the proposal) -> replayed (replay id known) -> finished

A replay that is still running when `replay_timeout_s` ends is NOT a failure: apply_fix returns status
"pending", keeps the fix, leaves the incident in status "applying" at step "replayed", and the next apply_fix
with the same hash resumes at the follow step (no new write, no new replay) and then verifies or rolls back.

Kestra facts this relies on (all measured, see .work/core/FACTS.md):
  * flow PUT has no conditional write, so we re-read right before and verify right after;
  * a replay returns a new execution whose task run ids differ, but whose reused tasks keep identical
    attempt timestamps and outputs, which is how reuse is proven;
  * Kestra does not de-duplicate replays, so an existing replay is adopted before a new one is made.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .client import KestraClient, KestraError, WaitTimeout
from .flowsrc import (FlowSourceError, diff_stat, parse_flow, sha256_text, unified_diff)
from .store import Store

MAX_SOURCE_BYTES = 256 * 1024
CHILD_LOOKUP_TRIES, CHILD_LOOKUP_PAUSE_S = 6, 1.0
RELATED_WINDOW_S = 6 * 3600
RELATED_STATUSES = ("open", "proposed", "applying", "needs_human", "stale", "conflict")
MAX_RATIONALE = 2000
_SUCCESS_STATES = {"SUCCESS", "WARNING"}


class PolicyError(Exception):
    """The request is outside what this plugin may do. The message says why and what is allowed."""


class StaleError(Exception):
    """The world changed since the human looked. Nothing was written. Refresh and decide again."""


class UnknownIncident(ValueError):
    """No incident with this id is stored."""


class _LostLock(Exception):
    """The apply lock expired and someone else took it: stop without writing."""


NO_FAILED_RUN = ("No failed task run was found in this execution (the failure is not a task failure), so there is "
                 "nothing to replay from. Replaying the whole execution would run succeeded tasks again, so this "
                 "incident is diagnose-only.")


@dataclass
class Policy:
    namespaces: list[str] = field(default_factory=list)
    protected_flows: list[str] = field(default_factory=list)
    rollback_on_failure: bool = True
    replay_timeout_s: int = 300
    max_log_lines: int = 200

    def namespace_allowed(self, namespace: str) -> bool:
        """Dot-boundary prefix match. An empty list allows nothing (fail closed)."""
        for prefix in self.namespaces:
            prefix = (prefix or "").strip().rstrip(".")
            if not prefix:
                continue
            if namespace == prefix or namespace.startswith(prefix + "."):
                return True
        return False

    def is_protected(self, namespace: str, flow_id: str) -> bool:
        return f"{namespace}.{flow_id}" in set(self.protected_flows)

    def check_namespace(self, namespace: str) -> None:
        if not self.namespaces or not any((p or "").strip() for p in self.namespaces):
            raise PolicyError("No namespaces are allowed yet. Set the plugin setting `namespaces` "
                              "(for example [\"hko.demo\"]) so the plugin knows which flows it may touch.")
        if not self.namespace_allowed(namespace):
            raise PolicyError(f"Namespace {namespace!r} is not in the allowed namespaces "
                              f"{sorted(self.namespaces)}. Nothing was read or written.")


def incident_id_for(execution_id: str) -> str:
    return "INC-" + hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:8]


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _state(obj: dict | None) -> str:
    return str(((obj or {}).get("state") or {}).get("current", "UNKNOWN"))


def proposal_commitment(*, incident_id: str, execution_id: str, failed_task_id: str | None, base_revision: int,
                        base_source_sha256: str, new_source: str) -> str:
    """The hash a human approves. It covers the incident, the base the fix was written against and the
    exact replacement bytes, so a re-proposal on a new base can never reuse an old approval."""
    payload = {"incident_id": incident_id, "execution_id": execution_id, "failed_task_id": failed_task_id,
               "base_revision": base_revision, "base_source_sha256": base_source_sha256,
               "new_source_sha256": sha256_text(new_source)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --------------------------------------------------------------------------- execution analysis
def failed_leaf(execution: dict) -> dict | None:
    """The first FAILED task run that has no FAILED child (nested tasks fail from the inside out)."""
    runs = execution.get("taskRunList") or []
    failed = [r for r in runs if _state(r) == "FAILED"]
    parents = {r.get("parentTaskRunId") for r in failed if r.get("parentTaskRunId")}
    leaves = [r for r in failed if r.get("id") not in parents]
    if not leaves:
        return None
    return sorted(leaves, key=lambda r: str((r.get("state") or {}).get("startDate") or ""))[0]


def _run_key(run: dict) -> tuple[str, str]:
    return str(run.get("taskId")), str(run.get("value") or "")


def _attempt_fingerprint(run: dict) -> list[tuple[str, str, str]]:
    out = []
    for attempt in run.get("attempts") or []:
        st = attempt.get("state") or {}
        out.append((str(st.get("startDate")), str(st.get("endDate")), str(st.get("current"))))
    return out


def verify_replay(original: dict, replay: dict, failed_task_id: str) -> dict[str, Any]:
    """Protected-state proof. Task run ids are remapped by Kestra, so match by (taskId, value) and compare
    attempt timestamps: a reused task keeps the original attempts untouched, a re-executed one does not."""
    orig_runs = {_run_key(r): r for r in original.get("taskRunList") or []}
    new_runs = {_run_key(r): r for r in replay.get("taskRunList") or []}
    reused, rerun, missing = [], [], []
    for key, run in orig_runs.items():
        if _state(run) != "SUCCESS":
            continue
        match = new_runs.get(key)
        if match is None:
            missing.append(key[0])
        elif _attempt_fingerprint(match) == _attempt_fingerprint(run) and _state(match) == "SUCCESS":
            reused.append(key[0])
        else:
            rerun.append(key[0])
    failed_ok = not failed_task_id  # no failed task run (flow-level failure): nothing task-specific to prove
    failed_attempts = {"original": 0, "replay": 0}
    for key, run in orig_runs.items():
        if key[0] != failed_task_id or _state(run) != "FAILED":
            continue
        match = new_runs.get(key)
        before, after = _attempt_fingerprint(run), _attempt_fingerprint(match) if match else []
        failed_attempts = {"original": len(before), "replay": len(after)}
        if (match is not None and _state(match) in _SUCCESS_STATES and len(after) > len(before)
                and after[:len(before)] == before and after[-1][2] in _SUCCESS_STATES):
            failed_ok = True
    ok = failed_ok and not rerun and _state(replay) in _SUCCESS_STATES
    return {"ok": ok, "failed_task_id": failed_task_id, "failed_task_recovered": failed_ok,
            "failed_task_attempts": failed_attempts, "reused_task_ids": sorted(reused),
            "rerun_task_ids": sorted(rerun), "removed_task_ids": sorted(missing),
            "replay_state": _state(replay)}


def continues_original(original: dict, candidate: dict, failed_task_id: str) -> bool:
    """True if `candidate` looks like a replay of `original` started from the failed task: the failed task keeps its
    original attempts as a prefix and every earlier success is reused untouched."""
    new_runs = {_run_key(r): r for r in candidate.get("taskRunList") or []}
    seen_failed = False
    for key, run in ((_run_key(r), r) for r in original.get("taskRunList") or []):
        match = new_runs.get(key)
        if _state(run) == "SUCCESS":
            if match is None or _attempt_fingerprint(match) != _attempt_fingerprint(run):
                return False
        elif key[0] == failed_task_id and _state(run) == "FAILED":
            before = _attempt_fingerprint(run)
            if match is None or _attempt_fingerprint(match)[:len(before)] != before:
                return False
            seen_failed = True
    return seen_failed


# --------------------------------------------------------------------------- engine
class Oncall:
    def __init__(self, client: KestraClient, store: Store, policy: Policy):
        self.client = client
        self.store = store
        self.policy = policy

    # ----------------------------------------------------------------- open
    def open_incident(self, execution_id: str) -> dict:
        """Read-only toward Kestra. Writes only a local incident row."""
        execution = self.client.get_execution(execution_id)
        namespace, flow_id = execution.get("namespace", ""), execution.get("flowId", "")
        self.policy.check_namespace(namespace)
        state = _state(execution)
        if state != "FAILED":
            raise PolicyError(f"Execution {execution_id} is {state}, not FAILED. Only failed executions "
                              "become incidents.")
        original_id = execution.get("originalId")
        if original_id and original_id != execution_id:
            # A failed replay that WE started is the tail of an existing incident, not a new one. Without this
            # the Kestra failure webhook would open a second incident for every rolled-back fix.
            parent = self.store.get_incident_row(incident_id_for(original_id))
            if parent and (parent.get("apply_state") or {}).get("replay_execution_id") == execution_id:
                out = self._public(parent, created=False)
                out["is_replay_of_incident"] = parent["incident_id"]
                out["note"] = (f"Execution {execution_id} is the replay started by {parent['incident_id']}; "
                               "no new incident was created.")
                return out
        incident_id = incident_id_for(execution_id)
        existing = self.store.get_incident_row(incident_id)
        if existing:
            return self._public(existing, created=False)

        leaf = failed_leaf(execution)
        revision = execution.get("flowRevision")
        bundle: dict[str, Any] = {
            "execution_id": execution_id,
            "execution_state": state,
            "execution_revision": revision,
            "execution_inputs": execution.get("inputs") or {},
            "labels": {l.get("key"): l.get("value") for l in execution.get("labels") or []
                       if not str(l.get("key", "")).startswith("system.")},
            "failed_task": ({"task_id": leaf.get("taskId"), "task_run_id": leaf.get("id"),
                             "attempts": len(leaf.get("attempts") or []), "value": leaf.get("value")}
                            if leaf else None),
            "task_runs": [{"task_id": r.get("taskId"), "task_run_id": r.get("id"), "state": _state(r),
                           "parent_task_run_id": r.get("parentTaskRunId")}
                          for r in execution.get("taskRunList") or []],
            "logs": self._logs(execution_id),
            "outputs": self._outputs(execution_id),
        }
        current = self._flow_or_gone(namespace, flow_id)
        at_exec = (current if current.get("revision") == revision
                   else self._flow_or_gone(namespace, flow_id, revision=revision))
        bundle["flow"] = {
            "execution_revision": revision, "current_revision": current.get("revision"),
            "source": at_exec.get("source", ""),
            # Only sent when different: the proposal must be written against the CURRENT source.
            "current_source": current.get("source", "") if current.get("revision") != revision else None,
        }
        bundle["what_changed"] = self._what_changed(namespace, flow_id, revision, at_exec.get("source", ""))
        bundle["protected"] = self.policy.is_protected(namespace, flow_id)
        if not leaf:
            bundle["diagnose_only_reason"] = NO_FAILED_RUN
        else:
            self._add_children(bundle, leaf, at_exec.get("source", ""), execution_id)
        related = self.store.find_related(namespace=namespace, flow_id=flow_id,
                                          failed_task_id=leaf.get("taskId") if leaf else None,
                                          since=time.time() - RELATED_WINDOW_S, statuses=RELATED_STATUSES,
                                          exclude=incident_id)
        if related:
            bundle["related_incident"] = {"incident_id": related["incident_id"], "status": related["status"],
                                          "opened": iso(related["created"]), "has_proposal": bool(related["proposals"])}
            bundle["note"] = (f"The same flow and task already failed in {related['incident_id']} ({related['status']}) "
                              f"in the last {RELATED_WINDOW_S // 3600}h. Read that incident first and reuse its proposal "
                              "instead of diagnosing from scratch.")
        row, created = self.store.create_incident(
            incident_id=incident_id, execution_id=execution_id, namespace=namespace, flow_id=flow_id,
            bundle=bundle, failed_task_id=leaf.get("taskId") if leaf else None,
            failed_task_run_id=leaf.get("id") if leaf else None, execution_revision=revision)
        if created:
            self.store.journal_add(incident_id, "opened", {"execution_id": execution_id, "flow": f"{namespace}.{flow_id}"})
        return self._public(row, created=created)

    def _add_children(self, bundle: dict, leaf: dict, source: str, execution_id: str) -> None:
        """Subflow and Loop failures happen in another execution. Measured on 2.0.9: a Subflow task run's outputs carry
        `executionId`; a Loop logs 'Loop iteration N ended in FAILED in sub-execution [[link execution="ID" ...]]'; and a
        Loop cannot be replayed ('No restartable loop sub-execution found'). Such incidents are diagnose-only."""
        try:
            task_type = parse_flow(source).task_types.get(leaf.get("taskId"), "")
        except FlowSourceError:
            return
        kind = "Subflow" if task_type.endswith(".flow.Subflow") else "Loop" if task_type.endswith(".flow.Loop") else None
        if not kind:
            return
        ids: list[str] = []
        for attempt in range(CHILD_LOOKUP_TRIES):
            own = (bundle.get("outputs") or {}).get(leaf.get("taskId")) or {}
            if isinstance(own, dict) and isinstance(own.get("executionId"), str):
                ids.append(own["executionId"])
            for entry in bundle.get("logs") or []:
                ids.extend(re.findall(r'execution="([A-Za-z0-9]+)"', str(entry.get("message", ""))))
            if ids or attempt == CHILD_LOOKUP_TRIES - 1:
                break
            # Kestra can mark the execution FAILED a moment before the child link is in the logs/outputs: look again.
            time.sleep(CHILD_LOOKUP_PAUSE_S)
            bundle["logs"], bundle["outputs"] = self._logs(execution_id), self._outputs(execution_id)
        children = []
        for child_id in list(dict.fromkeys(ids))[:5]:
            try:
                child = self.client.get_execution(child_id)
                errors = [str(r.get("message", ""))[:600] for r in self.client.get_logs(child_id, min_level="ERROR", limit=5)]
                children.append({"execution_id": child_id, "namespace": child.get("namespace"), "flow_id": child.get("flowId"),
                                 "state": _state(child), "errors": errors})
            except KestraError as exc:
                children.append({"execution_id": child_id, "error": f"unavailable: {str(exc)[:160]}"})
        bundle["child_executions"] = children
        where = ", ".join(c["execution_id"] for c in children) or "a child execution (id not found in the logs)"
        if kind == "Subflow":
            bundle["diagnose_only_reason"] = (
                f"Task {leaf.get('taskId')!r} runs another flow and the failure happened in {where}. A fix to this parent flow "
                "cannot repair the child. Open an incident on the child execution (kestra_incident_open) to diagnose and fix "
                "the flow that actually failed. This incident is diagnose-only.")
        else:
            bundle["diagnose_only_reason"] = (
                f"Task {leaf.get('taskId')!r} is a Loop and the failing iteration ran as {where}. Kestra 2.0.9 cannot replay a "
                "failed Loop ('No restartable loop sub-execution found'), so a fix could not be verified or its earlier work "
                "reused. This incident is diagnose-only: read the child execution's errors and fix the flow by hand.")

    def _logs(self, execution_id: str) -> list[dict]:
        try:
            rows = self.client.get_logs(execution_id, min_level="WARN", limit=self.policy.max_log_lines)
        except KestraError as exc:
            return [{"level": "NOTE", "message": f"logs unavailable: {str(exc)[:200]}"}]
        return [{"level": r.get("level"), "task_id": r.get("taskId"), "task_run_id": r.get("taskRunId"),
                 "timestamp": r.get("timestamp"), "message": str(r.get("message", ""))[:1500]} for r in rows]

    def _outputs(self, execution_id: str) -> dict[str, Any]:
        """Best effort: task outputs live on a separate endpoint in Kestra 2.0 and are never needed to open."""
        result: dict[str, Any] = {}
        try:
            for item in self.client.list_task_outputs(execution_id)[:20]:
                value = self.client.get_task_outputs(execution_id, item["taskRunId"])
                text = json.dumps(value, default=str)
                result[item["taskId"]] = value if len(text) <= 2000 else {"truncated": text[:2000]}
        except (KestraError, KeyError, TypeError) as exc:
            result["_error"] = f"outputs unavailable: {str(exc)[:200]}"
        return result

    def _what_changed(self, namespace: str, flow_id: str, revision: int | None, source: str) -> dict | None:
        try:
            rows = self.client.search_executions(namespace=namespace, flow_id=flow_id, state=["SUCCESS"], size=1)
            if not rows:
                return None
            last = rows[0]
            last_revision = last.get("flowRevision")
            if last_revision == revision:
                return {"last_success_execution_id": last.get("id"), "last_success_revision": last_revision,
                        "failing_revision": revision, "diff": "",
                        "note": "Same flow revision succeeded before: the failure is data or environment, not a flow edit."}
            old = self.client.get_flow(namespace, flow_id, revision=last_revision).get("source", "")
            return {"last_success_execution_id": last.get("id"), "last_success_revision": last_revision,
                    "failing_revision": revision,
                    "diff": unified_diff(old, source, fromfile=f"rev {last_revision}", tofile=f"rev {revision}")}
        except KestraError as exc:
            return {"note": f"unavailable: {str(exc)[:200]}"}

    # ----------------------------------------------------------------- read
    def _public(self, row: dict, *, created: bool | None = None) -> dict:
        bundle = dict(row["bundle"])
        out = {
            "incident_id": row["incident_id"], "status": row["status"], "namespace": row["namespace"],
            "flow_id": row["flow_id"], "execution_id": row["execution_id"],
            "protected": bool(bundle.get("protected")), "created": iso(row["created"]),
            "updated": iso(row["updated"]),
            "failed_task_id": row.get("failed_task_id"), "failed_task_run_id": row.get("failed_task_run_id"),
            **{k: v for k, v in bundle.items() if k not in {"execution_id"}},
        }
        if created is not None:
            out["newly_opened"] = created
        proposal = self.store.get_proposal(row["incident_id"])
        if proposal:
            out["proposal"] = self._public_proposal(proposal)
        if row.get("apply_state"):
            out["apply_state"] = row["apply_state"]
        if row.get("result"):
            out["result"] = row["result"]
        return out

    @staticmethod
    def _public_proposal(p: dict) -> dict:
        return {"proposal_sha256": p["proposal_sha256"], "base_revision": p["base_revision"],
                "base_source_sha256": p["base_source_sha256"], "new_source": p["new_source"],
                "rationale": p["rationale"], "diff": p["diff"], "diff_stat": p["diff_stat"],
                "validation": p["validation"], "created": iso(p["created"])}

    def get_incident(self, incident_id: str) -> dict:
        row = self._row(incident_id)
        return self._public(row)

    def _row(self, incident_id: str) -> dict:
        row = self.store.get_incident_row(incident_id)
        if row is None:
            raise UnknownIncident(f"Unknown incident {incident_id!r}. List incidents or open one from a failed execution id.")
        return row

    def list_incidents(self, *, status: str | None = None, limit: int = 20) -> list[dict]:
        rows = self.store.list_incidents(status=status, limit=limit)
        return [{**r, "created": iso(r["created"]), "updated": iso(r["updated"])} for r in rows]

    def summary(self) -> dict:
        counts = self.store.counts_by_status()
        return {"incidents": counts, "total_incidents": sum(counts.values()),
                "pause_decisions": self.store.count_pause_decisions(),
                "namespaces": list(self.policy.namespaces), "protected_flows": list(self.policy.protected_flows)}

    # ----------------------------------------------------------------- propose
    @staticmethod
    def _diagnose_only_reason(row: dict) -> str | None:
        """Why this incident can be diagnosed but never proposed/applied (None when it can)."""
        if not row.get("failed_task_run_id"):
            return NO_FAILED_RUN
        return (row.get("bundle") or {}).get("diagnose_only_reason")

    def _nothing_in_flight(self, row: dict) -> bool:
        """An interrupted apply that never started a replay can be abandoned by proposing again (no lock held)."""
        st = row.get("apply_state") or {}
        return (st.get("step") in {"start", "written"} and not st.get("replay_execution_id")
                and self.store.lock_info(row["incident_id"]) is None)

    def propose_fix(self, incident_id: str, new_source: str, rationale: str) -> dict:
        row = self._row(incident_id)
        namespace, flow_id = row["namespace"], row["flow_id"]
        self.policy.check_namespace(namespace)
        if self.policy.is_protected(namespace, flow_id):
            raise PolicyError(f"{namespace}.{flow_id} is a protected flow: diagnose only. No proposal is stored.")
        reason = self._diagnose_only_reason(row)
        if reason:
            raise PolicyError(reason)
        if row["status"] == "applying" and not self._nothing_in_flight(row):
            raise PolicyError("An apply is in progress or was interrupted for this incident. Run apply with the "
                              "stored proposal hash to finish it before proposing again.")
        if row["status"] == "verified":
            raise PolicyError("This incident is already fixed and verified. Open a new incident for a new failure.")
        if not isinstance(new_source, str) or not new_source.strip():
            raise PolicyError("new_source is empty. Provide the complete new YAML of the flow.")
        if len(new_source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise PolicyError(f"new_source is larger than {MAX_SOURCE_BYTES // 1024} KB. Propose a smaller flow.")
        try:
            info = parse_flow(new_source)
        except FlowSourceError as exc:
            raise PolicyError(str(exc)) from None
        if info.namespace != namespace or info.flow_id != flow_id:
            raise PolicyError(f"The proposal must keep namespace and id ({namespace}.{flow_id}); it declares "
                              f"{info.namespace}.{info.flow_id}. A fix may not move or rename a flow.")
        failed_task = row.get("failed_task_id")
        if failed_task and failed_task not in info.task_ids:
            raise PolicyError(f"The proposal removes the failed task {failed_task!r}. The replay restarts from that "
                              "task, so it must still exist (change its content instead of removing it).")
        violations = self.client.validate_flow(new_source)
        if violations:
            raise PolicyError("Kestra rejects this flow: "
                              + "; ".join(str(v.get("constraints", "")).strip()[:300] for v in violations[:3]))
        base = self._flow_or_gone(namespace, flow_id)
        base_source, base_revision = base.get("source", ""), int(base.get("revision"))
        if new_source == base_source:
            raise PolicyError(f"The proposal is identical to the current revision {base_revision}. Change something.")
        base_sha = sha256_text(base_source)
        sha = proposal_commitment(incident_id=incident_id, execution_id=row["execution_id"],
                                  failed_task_id=failed_task, base_revision=base_revision,
                                  base_source_sha256=base_sha, new_source=new_source)
        diff = unified_diff(base_source, new_source, fromfile=f"{flow_id} rev {base_revision}", tofile=f"{flow_id} proposed")
        stat = diff_stat(base_source, new_source)
        proposal = self.store.save_proposal(
            incident_id, proposal_sha256=sha, base_revision=base_revision, base_source_sha256=base_sha,
            base_source=base_source, new_source=new_source, rationale=str(rationale or "")[:MAX_RATIONALE],
            diff=diff, diff_stat=stat, validation=[])
        if row["status"] != "needs_human":
            self.store.update_incident(incident_id, status="proposed")
        self.store.journal_add(incident_id, "proposed", {"proposal_sha256": sha, "base_revision": base_revision, "diff_stat": stat})
        return {"incident_id": incident_id, "proposal_sha256": sha, "base_revision": base_revision,
                "base_source_sha256": base_sha, "diff": diff, "diff_stat": stat, "validation": [],
                "rationale": proposal["rationale"],
                "next": f"Ask the human to review the diff, then call kestra_apply_fix with proposal_sha256={sha}."}

    # ----------------------------------------------------------------- apply
    def apply_fix(self, incident_id: str, proposal_sha256: str, *, actor: str) -> dict:
        row = self._row(incident_id)
        namespace, flow_id = row["namespace"], row["flow_id"]
        self.policy.check_namespace(namespace)
        if self.policy.is_protected(namespace, flow_id):
            raise PolicyError(f"{namespace}.{flow_id} is a protected flow: diagnose only. Nothing was written.")
        reason = self._diagnose_only_reason(row)
        if reason:
            raise PolicyError(reason)
        latest = self.store.get_proposal(incident_id)
        if latest is None:
            raise StaleError("No proposal is stored for this incident. Call kestra_propose_fix first.")
        if latest["proposal_sha256"] != proposal_sha256:
            raise StaleError("The proposal hash does not match the latest stored proposal. The human approved a "
                             "different version; read the incident again and ask for approval of the current one.")
        state = row.get("apply_state") or {}
        if state.get("proposal_sha256") == proposal_sha256 and state.get("step") == "finished" \
                and state.get("outcome") in {"verified", "rolled_back"}:
            return {**(row.get("result") or {}), "noop": True,
                    "message": f"Already {state['outcome']}. Nothing was written."}
        owner = f"{actor}#{uuid.uuid4().hex[:8]}"
        ttl = self.policy.replay_timeout_s + 120
        if not self.store.lock(incident_id, owner, ttl):
            return self._busy(incident_id, proposal_sha256)
        try:
            return self._apply_locked(row, latest, actor, owner, ttl)
        except _LostLock:
            return self._busy(incident_id, proposal_sha256)
        finally:
            self.store.unlock(incident_id, owner)

    def _busy(self, incident_id: str, proposal_sha256: str) -> dict:
        info = self.store.lock_info(incident_id) or {}
        return {"incident_id": incident_id, "status": "busy", "proposal_sha256": proposal_sha256, "noop": True,
                "message": "Another apply for this incident is running (or was interrupted and its lock has not "
                           "expired). Retry later; the next apply resumes from the saved step.",
                "lock_expires_in_s": max(0, int(info.get("expires", 0) - time.time()))}

    def _renew(self, incident_id: str, owner: str, ttl: int) -> None:
        """Keep the lock alive across the slow phases (follow, verify, rollback). Same-owner CAS."""
        if not self.store.lock(incident_id, owner, ttl):
            raise _LostLock()

    def _save(self, incident_id: str, st: dict, status: str | None = None, **extra: Any) -> None:
        fields: dict[str, Any] = {"apply_state": st}
        if status:
            fields["status"] = status
        fields.update(extra)
        self.store.update_incident(incident_id, **fields)

    def _result(self, row: dict, proposal: dict, st: dict, status: str, message: str, **extra: Any) -> dict:
        out = {"incident_id": row["incident_id"], "status": status, "proposal_sha256": proposal["proposal_sha256"],
               "base_revision": proposal["base_revision"], "fixed_revision": st.get("fixed_revision"),
               "rollback_revision": st.get("rollback_revision"), "original_execution_id": row["execution_id"],
               "replay_execution_id": st.get("replay_execution_id"), "adopted_existing_write": bool(st.get("adopted_write")),
               "adopted_existing_replay": bool(st.get("adopted_replay")), "actor": st.get("actor"),
               "noop": False, "message": message, **extra}
        return out

    def _pending(self, row: dict, proposal: dict, st: dict, message: str, **extra: Any) -> dict:
        """Not finished, not failed: the fix stays, the saved step is kept, apply again resumes from it."""
        self.store.journal_add(row["incident_id"], "pending", {"step": st.get("step"), "message": message[:300]})
        result = self._result(row, proposal, st, "pending", message, **extra)
        self._save(row["incident_id"], st, "applying", result=result)
        return result

    def _needs_human(self, row: dict, proposal: dict, st: dict, message: str, journal: str, **extra: Any) -> dict:
        st.update(step="finished", outcome="needs_human")
        self.store.journal_add(row["incident_id"], journal, {"message": message[:300]})
        result = self._result(row, proposal, st, "needs_human", message, **extra)
        self._save(row["incident_id"], st, "needs_human", result=result)
        return result

    def _apply_locked(self, row: dict, proposal: dict, actor: str, owner: str, ttl: int) -> dict:
        incident_id = row["incident_id"]
        namespace, flow_id = row["namespace"], row["flow_id"]
        sha = proposal["proposal_sha256"]
        # Re-read the incident under the lock: another process may have finished while we waited.
        row = self._row(incident_id)
        st = dict(row.get("apply_state") or {})
        if st.get("proposal_sha256") != sha:
            st = {"proposal_sha256": sha, "step": "start", "actor": actor}
        if st.get("step") == "finished" and st.get("outcome") in {"verified", "rolled_back"}:
            return {**(row.get("result") or {}), "noop": True, "message": f"Already {st['outcome']}. Nothing was written."}
        if st.get("step") == "finished":
            # needs_human keeps what was already done (fix written, replay started) and resumes after it, so a retry
            # reconciles instead of replaying again. stale / conflict wrote nothing to keep: evaluate from the top.
            if st.get("outcome") == "needs_human" and st.get("replay_execution_id") and st.get("fixed_revision"):
                st.update(step="replayed")
            elif st.get("outcome") == "needs_human" and st.get("fixed_revision"):
                st.update(step="written")
            else:
                st = {"proposal_sha256": sha, "step": "start", "actor": actor}
            st.pop("outcome", None)
        st["actor"] = actor
        J = self.store.journal_add

        # ---- step 1: write the fix (with stale guard and read-back)
        if st["step"] == "start":
            self._renew(incident_id, owner, ttl)
            current = self._flow_or_gone(namespace, flow_id)
            cur_source, cur_revision = current.get("source", ""), int(current.get("revision"))
            if cur_source == proposal["new_source"]:
                st.update(fixed_revision=cur_revision, adopted_write=True, step="written")
                J(incident_id, "adopt_fix", {"revision": cur_revision})
                self._save(incident_id, st, "applying")
            elif cur_revision != proposal["base_revision"] or sha256_text(cur_source) != proposal["base_source_sha256"]:
                st.update(step="finished", outcome="stale")
                J(incident_id, "stale", {"expected_revision": proposal["base_revision"], "found_revision": cur_revision})
                result = self._result(row, proposal, st, "stale",
                                      f"The flow changed since the proposal: it was written against revision "
                                      f"{proposal['base_revision']}, now it is {cur_revision}. Nothing was written. "
                                      "Propose again against the current source.",
                                      current_revision=cur_revision)
                self._save(incident_id, st, "stale", result=result)
                return result
            else:
                self._save(incident_id, st, "applying")
                J(incident_id, "write_fix", {"base_revision": cur_revision, "actor": actor})
                try:
                    self.client.update_flow(namespace, flow_id, proposal["new_source"])
                except KestraError as exc:
                    J(incident_id, "write_refused", {"error": str(exc)[:300]})
                    self._save(incident_id, st, "proposed")
                    raise
                try:
                    back = self.client.get_flow(namespace, flow_id)
                except KestraError as exc:
                    return self._pending(row, proposal, st,
                                         f"The flow update was sent but could not be read back ({str(exc)[:160]}). Run apply "
                                         "again with the same proposal hash: it adopts the write if it landed and reports "
                                         "a conflict if someone else wrote.")
                landed = back.get("source", "") == proposal["new_source"]
                if not landed or int(back.get("revision")) != proposal["base_revision"] + 1:
                    st.update(step="finished", outcome="conflict")
                    J(incident_id, "conflict", {"found_revision": back.get("revision"), "source_landed": landed})
                    result = self._result(row, proposal, st, "conflict",
                                          "The write did not land cleanly: " +
                                          ("the flow no longer holds the proposal." if not landed else
                                           f"expected revision {proposal['base_revision'] + 1}, found {back.get('revision')} "
                                           "(someone else wrote at the same time).") +
                                          " No replay was started and nothing was rolled back over the other change.",
                                          found_revision=back.get("revision"), source_landed=landed)
                    self._save(incident_id, st, "conflict", result=result)
                    return result
                st.update(fixed_revision=int(back["revision"]), step="written")
                J(incident_id, "fix_written", {"revision": st["fixed_revision"]})
                self._save(incident_id, st, "applying")

        # ---- step 2: replay from the failed task run
        if st["step"] == "written":
            self._renew(incident_id, owner, ttl)
            if not st.get("replay_execution_id"):
                try:
                    existing = (self._find_replay(row, st["fixed_revision"], sha)
                                if self._replay_intent_open(incident_id, sha) else None)
                except KestraError as exc:
                    return self._pending(row, proposal, st, f"Could not check for an existing replay ({str(exc)[:160]}). "
                                         "The fix stays live. Run apply again with the same proposal hash.")
                if existing:
                    st.update(replay_execution_id=existing["id"], adopted_replay=True)
                    J(incident_id, "adopt_replay", {"replay_execution_id": existing["id"]})
                else:
                    J(incident_id, "replay", {"from_task_run": row.get("failed_task_run_id"),
                                              "revision": st["fixed_revision"], "proposal_sha256": sha})
                    try:
                        replay = self.client.replay(row["execution_id"], task_run_id=row.get("failed_task_run_id"),
                                                    revision=st["fixed_revision"])
                    except KestraError as exc:
                        status = exc.status
                        if status is not None and 400 <= status < 500 and status not in (408, 429):
                            # Kestra said no (original purged, not replayable, task run gone): no replay exists.
                            J(incident_id, "replay_refused", {"status": status, "error": str(exc)[:300]})
                            return self._after_failed_replay(
                                row, proposal, st, "REFUSED", f"Kestra refused the replay request (HTTP {status}: {str(exc.body)[:160]})")
                        J(incident_id, "replay_unknown", {"status": status, "error": str(exc)[:300]})
                        return self._pending(row, proposal, st,
                                             f"The replay request failed without a clear answer ({str(exc)[:160]}); it may or may "
                                             f"not have started. The fix (revision {st['fixed_revision']}) stays live. Run apply "
                                             "again with the same proposal hash: it adopts a replay that did start, otherwise it "
                                             "starts one.")
                    st["replay_execution_id"] = replay["id"]
                    self._save(incident_id, st, "applying")  # persist the id immediately
                    J(incident_id, "replay_started", {"replay_execution_id": replay["id"]})
            st["step"] = "replayed"
            self._save(incident_id, st, "applying")

        # ---- step 3: follow the replay, verify or roll back
        self._renew(incident_id, owner, ttl)
        replay_id = st["replay_execution_id"]
        try:
            final = self.client.wait_terminal(replay_id, timeout=self.policy.replay_timeout_s, leave_states={"PAUSED"})
        except WaitTimeout as exc:
            # A slow replay is not a failed replay. Rolling back now could revert a fix that is about to succeed,
            # so keep the fix, keep step "replayed" and let the next apply with the same hash keep following.
            seen = _state(exc.execution) if exc.execution else "UNKNOWN"
            J(incident_id, "replay_pending", {"replay_execution_id": replay_id, "state": seen,
                                              "waited_s": self.policy.replay_timeout_s})
            return self._pending(row, proposal, st,
                                 f"Replay {replay_id} was still {seen} after {self.policy.replay_timeout_s}s. The fix "
                                 f"(revision {st['fixed_revision']}) stays in place and nothing was rolled back. Run "
                                 "apply again with the same proposal hash to keep following the replay; it will verify "
                                 "or roll back once the replay ends. If the replay is stuck, kill it in Kestra: the next "
                                 "apply then rolls the fix back.", replay_state=seen)
        except KestraError as exc:
            return self._pending(row, proposal, st, f"Could not read replay {replay_id} ({str(exc)[:160]}). The fix stays "
                                 "in place. Run apply again with the same proposal hash.")
        replay_state = _state(final)
        if replay_state == "PAUSED":
            J(incident_id, "replay_paused", {"replay_execution_id": replay_id})
            return self._pending(row, proposal, st,
                                 f"Replay {replay_id} is waiting on a Pause task. The fix (revision {st['fixed_revision']}) "
                                 "stays in place. Answer the pause (see the pauses list), then run apply again with the "
                                 "same proposal hash to verify.", replay_state=replay_state)
        J(incident_id, "replay_finished", {"replay_execution_id": replay_id, "state": replay_state})

        if replay_state in _SUCCESS_STATES:
            self._renew(incident_id, owner, ttl)
            try:
                original = self.client.get_execution(row["execution_id"])
            except KestraError as exc:
                return self._pending(row, proposal, st, f"Replay {replay_id} ended {replay_state} but the original execution "
                                     f"could not be read to verify it ({str(exc)[:160]}). Run apply again with the same hash.",
                                     replay_state=replay_state)
            verification = verify_replay(original, final, row.get("failed_task_id") or "")
            verification["outputs_equal"] = self._outputs_equal(row["execution_id"], replay_id, verification["reused_task_ids"])
            if verification["ok"]:
                st.update(step="finished", outcome="verified")
                J(incident_id, "verified", verification)
                result = self._result(row, proposal, st, "verified",
                                      f"Fix applied as revision {st['fixed_revision']}. Replay {replay_id} ended {replay_state}; "
                                      f"{len(verification['reused_task_ids'])} earlier task(s) were reused, not re-run.",
                                      replay_state=replay_state, verification=verification)
                self._save(incident_id, st, "verified", result=result)
                return result
            return self._needs_human(
                row, proposal, st, "The replay succeeded but the reuse proof failed (an earlier task ran again or the failed "
                "task did not recover as expected). The fix stays in place; a human should inspect "
                f"replay {replay_id}.", "verification_failed", replay_state=replay_state, verification=verification)

        return self._after_failed_replay(row, proposal, st, replay_state, f"Replay {replay_id} ended {replay_state}")

    def _after_failed_replay(self, row: dict, proposal: dict, st: dict, replay_state: str, why: str) -> dict:
        """The replay failed, was killed or was refused: roll back when allowed, else hand over to a human."""
        if not self.policy.rollback_on_failure:
            return self._needs_human(row, proposal, st, f"{why} and rollback_on_failure is off. The fix "
                                     f"(revision {st['fixed_revision']}) stays in place.", "needs_human", replay_state=replay_state)
        return self._rollback(row, proposal, st, replay_state, why)

    def _rollback(self, row: dict, proposal: dict, st: dict, replay_state: str, why: str) -> dict:
        incident_id = row["incident_id"]
        namespace, flow_id = row["namespace"], row["flow_id"]
        J = self.store.journal_add
        try:
            current = self.client.get_flow(namespace, flow_id)
        except KestraError as exc:
            return self._needs_human(
                row, proposal, st, f"{why}; the rollback could not be performed because the flow could not be read: "
                f"{str(exc)[:160]}. The fixed revision {st['fixed_revision']} may still be live. Check the flow, then run "
                "apply again with the same proposal hash.", "rollback_read_failed", replay_state=replay_state)
        cur_source, cur_revision = current.get("source", ""), int(current.get("revision"))
        if cur_source == proposal["base_source"] and cur_revision > int(st["fixed_revision"]):
            # An earlier, interrupted rollback already landed: adopt it instead of writing again.
            st.update(step="finished", outcome="rolled_back", rollback_revision=cur_revision)
            J(incident_id, "adopt_rollback", {"revision": cur_revision})
        elif cur_revision != int(st["fixed_revision"]) or cur_source != proposal["new_source"]:
            return self._needs_human(
                row, proposal, st, f"{why}, but the flow was changed by someone else after the fix (now revision "
                f"{cur_revision}). It was NOT rolled back. A human should decide.", "needs_human",
                replay_state=replay_state, current_revision=cur_revision)
        else:
            J(incident_id, "rollback", {"from_revision": cur_revision})
            try:
                self.client.update_flow(namespace, flow_id, proposal["base_source"])
                back = self.client.get_flow(namespace, flow_id)
            except KestraError as exc:
                J(incident_id, "rollback_failed", {"error": str(exc)[:300]})
                return self._needs_human(
                    row, proposal, st, f"{why} and the rollback write failed: {str(exc)[:200]}. The failed fix is still "
                    f"live. Restore revision {proposal['base_revision']} by hand.", "rollback_failed", replay_state=replay_state)
            if back.get("source", "") != proposal["base_source"]:
                return self._needs_human(
                    row, proposal, st, f"{why}. The rollback write did not read back as the base source (revision "
                    f"{back.get('revision')}). A human should check the flow.", "needs_human", replay_state=replay_state)
            st.update(step="finished", outcome="rolled_back", rollback_revision=int(back["revision"]))
            J(incident_id, "rolled_back", {"revision": st["rollback_revision"]})
        result = self._result(row, proposal, st, "rolled_back",
                              f"{why}. The flow source was restored to the base source "
                              f"(revision {proposal['base_revision']}) as new revision {st['rollback_revision']}.",
                              replay_state=replay_state)
        self._save(incident_id, st, "rolled_back", result=result)
        return result

    def _flow_or_gone(self, namespace: str, flow_id: str, **kwargs: Any) -> dict:
        try:
            return self.client.get_flow(namespace, flow_id, **kwargs)
        except KestraError as exc:
            if exc.status == 404:
                raise PolicyError(f"The flow {namespace}.{flow_id} no longer exists in Kestra"
                                  f"{' at that revision' if kwargs.get('revision') else ''}; nothing can be proposed "
                                  "or applied.") from None
            raise

    def _replay_intent_open(self, incident_id: str, sha: str) -> bool:
        """True only in the one crash window adoption exists for: we journaled the replay call for this proposal
        but never recorded its result. Anything else means any replay we find was not started by us."""
        open_intent = False
        for entry in self.store.journal(incident_id, limit=500):
            detail = entry["detail"]
            if entry["step"] == "replay" and detail.get("proposal_sha256") == sha:
                open_intent = True
            elif entry["step"] in {"replay_started", "adopt_replay", "replay_refused"} and open_intent:
                open_intent = False
        return open_intent

    def _find_replay(self, row: dict, fixed_revision: int, sha: str) -> dict | None:
        """Kestra does not de-duplicate replays. Adopt a candidate only if it really continues the original from the
        failed task: same original attempts as a prefix, earlier successes reused. Earliest candidate wins."""
        original_id = row["execution_id"]
        rows = self.client.search_executions(labels={"system.correlationId": original_id}, size=50)
        found = [r for r in rows if r.get("originalId") == original_id and r.get("id") != original_id
                 and r.get("flowRevision") == fixed_revision]
        if not found:
            return None
        original = self.client.get_execution(original_id)
        good = []
        for candidate in sorted(found, key=lambda r: r["id"])[:10]:
            execution = self.client.get_execution(candidate["id"])
            if continues_original(original, execution, row.get("failed_task_id") or ""):
                good.append(execution)
        good.sort(key=lambda e: (max([x[0] for r in e.get("taskRunList") or [] if r.get("taskId") == row.get("failed_task_id")
                                      for x in _attempt_fingerprint(r)] or [""]), e["id"]))
        return good[0] if good else None

    def _outputs_equal(self, original_id: str, replay_id: str, task_ids: list[str]) -> bool | None:
        """Informational second proof that reused tasks kept their outputs. Never gates the verdict and never
        takes longer than 10 s in total."""
        deadline = time.monotonic() + 10.0
        try:
            def by_task(execution_id: str) -> dict[str, Any]:
                out = {}
                for item in self.client.list_task_outputs(execution_id):
                    if item["taskId"] in task_ids:
                        if time.monotonic() > deadline:
                            raise TimeoutError
                        out[item["taskId"]] = self.client.get_task_outputs(execution_id, item["taskRunId"])
                return out
            return by_task(original_id) == by_task(replay_id)
        except (KestraError, KeyError, TypeError, TimeoutError):
            return None
