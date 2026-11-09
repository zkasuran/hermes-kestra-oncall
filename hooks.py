"""Store-backed approval messages, one-shot approval tickets and immutable pause-request snapshots."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from collections import deque
from typing import Any

from .flowsrc import FlowSourceError, load_flow
from .settings import BYPASS_MESSAGE, SettingsError, approvals_bypassed, error_payload, redact, require_attended
from .tools import WRITE_TOOLS, validate_args

# A human must be able to read what they approve. Longer diffs are refused, never shown truncated.
MAX_DIFF_LINES = 120
MAX_DIFF_CHARS = 3000
# C0 (except tab and newline), DEL, C1, bidi marks and overrides: they can hide or reorder text in a prompt.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")
TICKET_TTL_S = 600
# Top-level flow keys that hold executable tasks. Triggers also carry id and a dotted type but are not tasks.
_TASK_ROOTS = ("tasks", "errors", "finally", "afterExecution")


def clean(text: Any) -> str:
    """Untrusted text (flow source, logs, pause question) with terminal and bidi control characters removed."""
    return _CONTROL.sub("", str(text))


def diff_fits(diff: str) -> bool:
    return len(diff.splitlines()) <= MAX_DIFF_LINES and len(diff) <= MAX_DIFF_CHARS


def too_long_message(diff: str, incident_id: str) -> str:
    return (f"The diff for {incident_id} is {len(diff.splitlines())} lines and {len(diff)} characters. A person can approve "
            f"at most {MAX_DIFF_LINES} lines and {MAX_DIFF_CHARS} characters here, because an approval of text you "
            "did not see is not an approval. Ask Hermes for a smaller fix and propose it again.")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _sections(source: str) -> dict[str, Any]:
    try:
        return load_flow(source)
    except FlowSourceError:
        return {}


def _task_map(node: Any, found: dict[str, Any] | None = None) -> dict[str, Any]:
    """Task id -> definition for every nested mapping that looks like a task (id plus a dotted plugin type)."""
    found = {} if found is None else found
    if isinstance(node, list):
        for item in node:
            _task_map(item, found)
    elif isinstance(node, dict):
        if isinstance(node.get("id"), str) and isinstance(node.get("type"), str) and "." in node["type"]:
            found.setdefault(node["id"], node)
        for value in node.values():
            _task_map(value, found)
    return found


def change_summary(base_source: str, new_source: str) -> str:
    """One line computed from the stored sources: which tasks and top-level sections the diff touches."""
    base, new = _sections(base_source), _sections(new_source)
    if not base or not new:
        return "Change summary unavailable: a stored source could not be parsed. Read the diff."
    base_tasks = _task_map([base.get(k) for k in _TASK_ROOTS])
    new_tasks = _task_map([new.get(k) for k in _TASK_ROOTS])
    added = sorted(set(new_tasks) - set(base_tasks))
    removed = sorted(set(base_tasks) - set(new_tasks))
    changed = sorted(t for t in set(base_tasks) & set(new_tasks) if base_tasks[t] != new_tasks[t])
    sections = sorted(k for k in set(base) | set(new) if base.get(k) != new.get(k))
    parts = [f"tasks added {added or 'none'}", f"removed {removed or 'none'}", f"changed {changed or 'none'}",
             "triggers " + ("CHANGED" if base.get("triggers") != new.get("triggers") else "unchanged"),
             f"top-level sections changed {sections or 'none'}"]
    return clean("; ".join(parts))


def checked_proposal(runtime: Any, args: dict) -> tuple[dict, dict]:
    """Resolve real stored content, never accept a caller-supplied diff or flow name."""
    incident = runtime.oncall.get_incident(args["incident_id"])
    proposal = incident.get("proposal")
    if not isinstance(proposal, dict) or not proposal.get("proposal_sha256"):
        raise ValueError("No stored proposal for this incident. Propose a fix first.")
    if not hmac.compare_digest(proposal["proposal_sha256"], args["proposal_sha256"]):
        raise ValueError("Proposal hash mismatch. Read and approve the current stored proposal.")
    if incident.get("protected"):
        raise ValueError("This flow is protected and may only be diagnosed.")
    return incident, proposal


class Tickets:
    """One-shot proof that the approval step ran for exactly these arguments.

    The pre_tool_call hook (a model-requested write) or a typed /kestra command (the human's own act) issues a
    ticket; the write handler consumes it before touching Kestra. `ctx.dispatch_tool` and `registry.dispatch`
    skip hooks, so without this a second plugin or a future Hermes path could reach the handler with no
    approval. A ticket proves the gate was consulted. It does not prove a human said yes: Hermes's own bypass
    settings are refused separately (see settings.approvals_bypassed).
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS glue_tickets (ticket_key TEXT PRIMARY KEY, source TEXT NOT NULL, created REAL NOT NULL)")

    @staticmethod
    def key(tool: str, args: dict) -> str:
        return digest({"tool": tool, "args": args})

    def issue(self, tool: str, args: dict, source: str) -> None:
        now = time.time()
        with self.conn:
            self.conn.execute("DELETE FROM glue_tickets WHERE created < ?", (now - TICKET_TTL_S,))
            self.conn.execute("INSERT OR REPLACE INTO glue_tickets VALUES (?, ?, ?)", (self.key(tool, args), source, now))

    def consume(self, tool: str, args: dict) -> bool:
        with self.conn:
            cursor = self.conn.execute("DELETE FROM glue_tickets WHERE ticket_key = ? AND created >= ?",
                                       (self.key(tool, args), time.time() - TICKET_TTL_S))
        return cursor.rowcount == 1


class ApprovalStore:
    """Glue-owned tables coexist with core's journal in the profile-scoped database."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        with conn:
            # Keyed by execution AND task run: a Parallel flow can have two PAUSED task runs in one execution.
            conn.execute("CREATE TABLE IF NOT EXISTS glue_pause_rows (execution_id TEXT NOT NULL, task_run_id TEXT NOT NULL, "
                         "body TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (execution_id, task_run_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS glue_pause_requests (request_key TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL)")

    def remember_pauses(self, rows: list[dict]) -> None:
        now = time.time()
        with self.conn:
            self.conn.execute("DELETE FROM glue_pause_rows")
            self.conn.execute("DELETE FROM glue_pause_requests WHERE created < ?", (now - 600,))
            for row in rows:
                execution_id, task_run_id = row.get("execution_id"), row.get("paused_task_run_id")
                if not execution_id or not task_run_id:
                    raise ValueError("Pause response has no execution or task run id. Refresh pauses after checking Kestra.")
                self.conn.execute("INSERT OR REPLACE INTO glue_pause_rows VALUES (?, ?, ?, ?)",
                                  (execution_id, task_run_id, json.dumps(row), now))

    @staticmethod
    def request_key(args: dict) -> str:
        return digest({key: args.get(key, {}) for key in ("execution_id", "paused_task_run_id", "action", "inputs")})

    def prepare(self, args: dict) -> dict:
        row = self.conn.execute("SELECT body, created FROM glue_pause_rows WHERE execution_id = ? AND task_run_id = ?",
                                (args["execution_id"], args["paused_task_run_id"])).fetchone()
        if row is None or time.time() - row[1] > 600:
            raise ValueError("Unknown or expired pause snapshot for that task run. Run kestra_pauses and review the current question.")
        pause = json.loads(row[0])
        if pause.get("protected"):
            raise ValueError("This paused flow is protected and may only be diagnosed.")
        request = {"execution_id": args["execution_id"], "paused_task_run_id": args["paused_task_run_id"],
                   "action": args["action"], "inputs": dict(args.get("inputs", {})), "pause": pause}
        request["decision_sha256"] = digest(request)
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO glue_pause_requests VALUES (?, ?, ?)",
                              (self.request_key(args), json.dumps(request), time.time()))
        return self.decision(args)

    def decision(self, args: dict) -> dict:
        row = self.conn.execute("SELECT body, created FROM glue_pause_requests WHERE request_key = ?", (self.request_key(args),)).fetchone()
        if row is None or time.time() - row[1] > 600:
            raise ValueError("No current stored pause decision. Refresh pauses and request approval again.")
        result = json.loads(row[0])
        sha = result.pop("decision_sha256")
        if not hmac.compare_digest(digest(result), sha):
            raise ValueError("Stored pause decision hash mismatch. Refresh pauses and request approval again.")
        result["decision_sha256"] = sha
        return result


def _flow(record: dict) -> str:
    """Core incident and pause records carry namespace and flow_id."""
    namespace, flow_id = record.get("namespace"), record.get("flow_id")
    if namespace and flow_id:
        return f"{namespace}.{flow_id}"
    raise ValueError("Stored record has no flow identity. Refresh the incident or pause.")


def build_request(runtime: Any, tool_name: str, args: dict, secrets: dict) -> tuple[str, str]:
    """The text a human approves and the allow-list grain, both built from stored records only.

    Raises ValueError when nothing safe can be shown (unknown or changed proposal, protected flow, a diff or
    question too long to read in full).
    """
    if tool_name == "kestra_apply_fix":
        incident, proposal = checked_proposal(runtime, args)
        diff = proposal["diff"]
        if not diff_fits(diff):
            raise ValueError(too_long_message(diff, args["incident_id"]))
        sha = proposal["proposal_sha256"]
        raw = runtime.store.get_proposal(args["incident_id"], sha) or {}
        in_progress = incident.get("status") == "applying"
        message = (f"Apply fix for {args['incident_id']} to {_flow(incident)}?\n"
                   f"Base revision: {proposal['base_revision']}\n"
                   f"Diff stat: {json.dumps(proposal['diff_stat'], ensure_ascii=True)}\n"
                   f"Summary: {change_summary(raw.get('base_source', ''), proposal.get('new_source', ''))}\n"
                   f"Proposal SHA-256: {sha}\n"
                   f"Diff (untrusted flow text, shown in full):\n{clean(diff)}\n"
                   + ("Resumes an apply that is already in progress: follows the existing replay and verifies it or rolls "
                      "back. It does not write the flow or start another replay again."
                      if in_progress else
                      "Writes one flow revision and replays the failed task. On failure, rollback may restore the base source."))
        rule_key = f"kestra-oncall:{tool_name}:{args['incident_id']}:{proposal['base_revision']}:{sha}"
    else:
        request = ApprovalStore(runtime.conn).prepare(args)
        pause = request["pause"]
        sha = request["decision_sha256"]
        question = str(pause.get("description") or "")
        if len(question) > MAX_DIFF_CHARS:
            raise ValueError(f"The pause question is {len(question)} characters, more than the {MAX_DIFF_CHARS} a person can approve here.")
        message = (f"{request['action'].capitalize()} {_flow(pause)}?\n"
                   f"Execution: {request['execution_id']}\nPaused task run: {request['paused_task_run_id']}\n"
                   f"Paused task: {clean(pause.get('paused_task_id') or '(see task run)')}\n"
                   f"Question (untrusted flow text): {clean(question)}\n"
                   f"Inputs: {clean(json.dumps(redact(request['inputs'], secrets), ensure_ascii=True, sort_keys=True))}\n"
                   f"Decision SHA-256: {sha}\nLive pause state is checked again immediately before the write.")
        rule_key = f"kestra-oncall:{tool_name}:{sha}"
    return redact(message, secrets), rule_key


class ApprovalHook:
    def __init__(self, toolbox: Any):
        self.toolbox = toolbox
        # tool_call_ids for which this hook returned "approve" (kept for diagnostics and tests).
        self.approved_calls: deque[str] = deque(maxlen=128)

    def __call__(self, tool_name: str = "", args: dict | None = None, **kwargs: Any) -> dict | None:
        if tool_name not in WRITE_TOOLS:
            return None
        secrets = {}
        try:
            secrets = self.toolbox.secret_reader()
            require_attended(self.toolbox.context_reader())
            if approvals_bypassed():
                raise SettingsError(BYPASS_MESSAGE)
            args = validate_args(tool_name, args)
            with self.toolbox.runtime_factory(self.toolbox.ctx, secrets) as runtime:
                message, rule_key = build_request(runtime, tool_name, args, secrets)
                Tickets(runtime.conn).issue(tool_name, args, "hook")
            call_id = kwargs.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                self.approved_calls.append(call_id)
            return {"action": "approve", "message": message, "rule_key": rule_key}
        except Exception as exc:
            error = error_payload(exc, secrets)
            return {"action": "block", "message": f"{error['error']} {error['hint']}"}
