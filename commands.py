"""Phone-sized slash commands with the same approval prompt as model tools."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from typing import Any

from .hooks import _flow
from .settings import error_payload, redact

USAGE = ("/kestra status | incidents | show <id> | apply <id> | pauses | "
         "resume <exec> [k=v ...] | kill <exec>")


def parse_command(raw: str) -> dict:
    """Reject ambiguous writes and duplicate inputs before doing any remote work."""
    if not isinstance(raw, str) or len(raw) > 16384:
        raise ValueError("Command is too long. " + USAGE)
    words = shlex.split(raw)
    if not words:
        return {"verb": "status"}
    verb, *rest = words
    if verb in {"status", "incidents", "pauses", "help"} and not rest:
        return {"verb": verb}
    if verb in {"show", "apply", "kill"} and len(rest) == 1:
        return {"verb": verb, "id": rest[0]}
    if verb == "resume" and rest:
        inputs = {}
        for item in rest[1:]:
            key, separator, value = item.partition("=")
            if not separator or not key or key in inputs:
                raise ValueError("Resume inputs must be unique key=value pairs. Quote values containing spaces.")
            inputs[key] = value
        return {"verb": verb, "id": rest[0], "inputs": inputs}
    raise ValueError(USAGE)


def request_approval(name: str, directive: dict) -> dict:
    """The documented ctx dispatcher bypasses hooks in 0.23; call its host gate first."""
    from tools.approval import request_tool_approval

    return request_tool_approval(name, directive["message"], rule_key=directive["rule_key"])


def _unpack(raw: str | dict) -> dict:
    result = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(result, dict):
        raise ValueError("Tool returned an invalid result. Run hermes kestra-oncall doctor.")
    return result


def _error(result: dict) -> str | None:
    if "error" in result:
        return f"{result['error']}\n{result.get('hint') or result.get('message') or ''}".strip()
    return None


def _identity(record: dict) -> str:
    try:
        return _flow(record)
    except ValueError:
        return "flow details unavailable"


def _counts(summary: dict) -> str:
    counts = summary.get("incidents") or {}
    parts = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "none"
    return f"Incidents: {parts}. Pause decisions: {summary.get('pause_decisions', 0)}."


class Commands:
    def __init__(self, ctx: Any, toolbox: Any, hook: Any, *, approval: Callable = request_approval):
        self.ctx, self.toolbox, self.hook, self.approval = ctx, toolbox, hook, approval

    def _read(self, name: str, args: dict) -> dict:
        return _unpack(self.ctx.dispatch_tool(name, args))

    def _write(self, name: str, args: dict) -> dict:
        directive = self.hook(tool_name=name, args=args)
        if not directive or directive.get("action") != "approve":
            return {"error": (directive or {}).get("message", "Approval guard was unavailable; no write was dispatched.")}
        approval = self.approval(name, directive)
        if not approval.get("approved"):
            return {"error": "Not approved. No Kestra write was dispatched.",
                    "hint": "Review the stored incident or pause and try again when ready."}
        return _unpack(self.ctx.dispatch_tool(name, args))

    def __call__(self, raw_args: str, **kwargs: Any) -> str:
        secrets = {}
        try:
            secrets = self.toolbox.secret_reader()
            parsed = parse_command(raw_args)
            reply = self.run(parsed)
            return redact(reply, secrets)[:3800]
        except Exception as exc:
            error = error_payload(exc, secrets)
            return f"{error['error']}\n{error['hint']}"

    def run(self, command: dict) -> str:
        verb = command["verb"]
        if verb == "help":
            return USAGE
        if verb == "status":
            result = self._read("kestra_status", {})
            if _error(result):
                return _error(result)
            status = f"Kestra {result.get('version', '')}: " + ("connected" if result.get("connected") else "unavailable")
            policy = "policy ready" if result.get("policy_ready") else "no namespaces allowed; configure namespaces first"
            return f"{status}\n{policy}\n{_counts(result.get('summary', {}))}"
        if verb == "incidents":
            result = self._read("kestra_incidents", {"limit": 10})
            if _error(result):
                return _error(result)
            rows = result.get("incidents", [])
            return "\n".join(f"{row['incident_id']}  {row.get('status', '?')}  {_identity(row)}" for row in rows) or "No incidents."
        if verb in {"show", "apply"}:
            result = self._read("kestra_incidents", {"incident_id": command["id"]})
            if _error(result):
                return _error(result)
            proposal = result.get("proposal") or {}
            if verb == "show":
                text = f"{command['id']}  {result.get('status', '?')}\n{_identity(result)}"
                if proposal:
                    text += (f"\nBase revision {proposal.get('base_revision')}\n{proposal.get('rationale', '')}"
                             f"\n{proposal.get('diff', '')[:1600]}\nSHA-256: {proposal.get('proposal_sha256', '')}"
                             f"\n/kestra apply {command['id']}")
                elif result.get("protected"):
                    text += "\nProtected flow: diagnose only, no fix can be proposed."
                else:
                    text += "\nNo proposal yet. Ask Hermes to inspect the incident and propose a fix."
                outcome = result.get("result") or {}
                if outcome:
                    text += f"\nResult: {outcome.get('status', '?')}. {str(outcome.get('message', ''))[:300]}"
                return text
            sha = proposal.get("proposal_sha256")
            if not sha:
                return "No stored proposal. Ask Hermes to propose a fix first."
            applied = self._write("kestra_apply_fix", {"incident_id": command["id"], "proposal_sha256": sha})
            return _error(applied) or f"{command['id']}: {applied.get('status', 'completed')}\n{json.dumps(applied, ensure_ascii=True)[:1800]}"
        result = self._read("kestra_pauses", {})
        if _error(result):
            return _error(result)
        rows = result.get("pauses", [])
        if verb == "pauses":
            return "\n".join(f"{row['execution_id']}  {_identity(row)}\n{str(row.get('description') or '')[:180]}" for row in rows[:10]) or "No paused executions."
        matches = [row for row in rows if row["execution_id"] == command["id"]]
        if len(matches) != 1:
            return "No unique current pause for that execution. Run /kestra pauses and use its execution id."
        pause = matches[0]
        task_run = pause["paused_task_run_id"]
        decision = self._write("kestra_pause_decide", {
            "execution_id": command["id"], "paused_task_run_id": task_run,
            "action": "resume" if verb == "resume" else "kill", "inputs": command.get("inputs", {}),
        })
        return _error(decision) or f"{command['id']}: {json.dumps(decision, ensure_ascii=True)[:2000]}"
