"""Store-backed approval messages and immutable pause-request snapshots."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from typing import Any

from .settings import error_payload, redact, require_attended
from .tools import WRITE_TOOLS, validate_args


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def checked_proposal(runtime: Any, args: dict) -> tuple[dict, dict]:
    """Resolve real stored content, never accept a caller-supplied diff or flow name."""
    incident = runtime.oncall.get_incident(args["incident_id"])
    proposal = incident.get("proposal")
    if not isinstance(proposal, dict):
        proposal = runtime.store.get_proposal(args["incident_id"])
    if not isinstance(proposal, dict) or not proposal.get("proposal_sha256"):
        raise ValueError("No stored proposal for this incident. Propose a fix first.")
    if not hmac.compare_digest(proposal["proposal_sha256"], args["proposal_sha256"]):
        raise ValueError("Proposal hash mismatch. Read and approve the current stored proposal.")
    if incident.get("protected"):
        raise ValueError("This flow is protected and may only be diagnosed.")
    return incident, proposal


class ApprovalStore:
    """Glue-owned tables coexist with core's journal in the profile-scoped database."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS glue_pause_snapshots (execution_id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS glue_pause_requests (request_key TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL)")

    def remember_pauses(self, rows: list[dict]) -> None:
        now = time.time()
        with self.conn:
            self.conn.execute("DELETE FROM glue_pause_snapshots")
            self.conn.execute("DELETE FROM glue_pause_requests WHERE created < ?", (now - 600,))
            for row in rows:
                execution_id = row.get("execution_id") or row.get("id")
                if not execution_id:
                    raise ValueError("Pause response has no execution id. Refresh pauses after checking Kestra.")
                self.conn.execute("INSERT INTO glue_pause_snapshots VALUES (?, ?, ?)",
                                  (execution_id, json.dumps(row), now))

    @staticmethod
    def request_key(args: dict) -> str:
        return digest({key: args.get(key, {}) for key in ("execution_id", "paused_task_run_id", "action", "inputs")})

    def prepare(self, args: dict) -> dict:
        row = self.conn.execute("SELECT body, created FROM glue_pause_snapshots WHERE execution_id = ?", (args["execution_id"],)).fetchone()
        if row is None or time.time() - row[1] > 600:
            raise ValueError("Unknown or expired pause snapshot. Run kestra_pauses before deciding.")
        pause = json.loads(row[0])
        task_run = pause.get("paused_task_run_id") or pause.get("task_run_id") or pause.get("taskRunId")
        if task_run != args["paused_task_run_id"]:
            raise ValueError("Paused task run changed. Refresh kestra_pauses and review the current question.")
        if pause.get("protected"):
            raise ValueError("This paused flow is protected and may only be diagnosed.")
        request = {"execution_id": args["execution_id"], "paused_task_run_id": task_run,
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
    namespace = record.get("namespace") or record.get("flow_namespace")
    flow_id = record.get("flow_id") or record.get("flowId")
    flow = record.get("flow")
    if isinstance(flow, dict):
        namespace = namespace or flow.get("namespace")
        flow_id = flow_id or flow.get("id") or flow.get("flow_id")
    if namespace and flow_id:
        return f"{namespace}.{flow_id}"
    if isinstance(flow, str) and flow:
        return flow
    raise ValueError("Stored record has no flow identity. Refresh the incident or pause.")


def _preview(text: str, *, lines: int = 16, chars: int = 1800) -> str:
    selected = "\n".join(text.splitlines()[:lines])[:chars]
    return selected + ("\n[Preview truncated. Read the full stored proposal before approving.]" if selected != text.rstrip("\n") else "")


class ApprovalHook:
    def __init__(self, toolbox: Any):
        self.toolbox = toolbox

    def __call__(self, tool_name: str = "", args: dict | None = None, **kwargs: Any) -> dict | None:
        if tool_name not in WRITE_TOOLS:
            return None
        secrets = {}
        try:
            secrets = self.toolbox.secret_reader()
            require_attended(self.toolbox.context_reader())
            args = validate_args(tool_name, args)
            with self.toolbox.runtime_factory(self.toolbox.ctx, secrets) as runtime:
                if tool_name == "kestra_apply_fix":
                    incident, proposal = checked_proposal(runtime, args)
                    sha = proposal["proposal_sha256"]
                    rule_key = f"kestra-oncall:{tool_name}:{args['incident_id']}:{proposal['base_revision']}:{sha}"
                    message = (f"Apply fix for {args['incident_id']} to {_flow(incident)}?\n"
                               f"Base revision: {proposal['base_revision']}\n"
                               f"Diff stat: {json.dumps(proposal['diff_stat'], ensure_ascii=True)}\n"
                               f"Proposal SHA-256: {sha}\n"
                               f"Stored diff (untrusted flow text):\n{_preview(proposal['diff'])}\n"
                               "Writes one flow revision and replays the failed task. On failure, rollback may restore the base source.")
                else:
                    request = ApprovalStore(runtime.conn).prepare(args)
                    pause = request["pause"]
                    sha = request["decision_sha256"]
                    rule_key = f"kestra-oncall:{tool_name}:{sha}"
                    message = (f"{request['action'].capitalize()} {_flow(pause)}?\n"
                               f"Execution: {request['execution_id']}\nPaused task run: {request['paused_task_run_id']}\n"
                               f"Paused task: {pause.get('paused_task_id') or pause.get('task_id') or '(see task run)'}\n"
                               f"Question (untrusted flow text): {_preview(str(pause.get('description') or pause.get('pause_description') or ''))}\n"
                               f"Inputs: {json.dumps(redact(request['inputs'], secrets), ensure_ascii=True, sort_keys=True)}\n"
                               f"Decision SHA-256: {sha}\nLive pause state is checked again immediately before the write.")
            return {"action": "approve", "message": redact(message, secrets), "rule_key": rule_key}
        except Exception as exc:
            error = error_payload(exc, secrets)
            return {"action": "block", "message": f"{error['error']} {error['hint']}"}
