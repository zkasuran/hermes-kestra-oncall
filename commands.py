"""Phone-sized slash commands. A typed write command is the human's approval of exactly the stored bytes.

Why not Hermes's approval card: a plugin slash command runs outside an agent turn. Hermes registers its chat
approval notifier only while a turn runs, so on Telegram, Discord and Slack a card raised from a command is
never delivered and returns "approval_required" (.work/review/FIXES-glue.md G1). `ctx.dispatch_tool` also
skips pre_tool_call hooks (Hermes main c3354ea and v0.23.0, contrary to the plugin guide). So the command is
itself the approval: the person must type the proposal hash prefix (or the paused task run prefix) that
`/kestra show` or `/kestra pauses` printed, the command refuses if the full diff cannot be shown, and the
write handler still needs the one-shot ticket the command issues. Models cannot type slash commands.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from .hooks import MAX_DIFF_CHARS, MAX_DIFF_LINES, _flow, clean, diff_fits, too_long_message
from .settings import error_payload, redact

MIN_PREFIX = 8
USAGE = ("/kestra status | incidents | show <id> | apply <id> <hash> | pauses | "
         "resume <exec> <taskrun> [k=v ...] | kill <exec> <taskrun>")


def _prefix(word: str, what: str) -> str:
    if len(word) < MIN_PREFIX or not all(c.isalnum() or c in "_-" for c in word):
        raise ValueError(f"Give at least {MIN_PREFIX} characters of the {what}. Copy the command printed by "
                         f"{'/kestra show' if what == 'proposal hash' else '/kestra pauses'}.")
    return word


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
    if verb == "show" and len(rest) == 1:
        return {"verb": verb, "id": rest[0]}
    if verb == "apply":
        if len(rest) != 2:
            raise ValueError("Review first: /kestra show <id>, then type the printed command /kestra apply <id> <hash>. " + USAGE)
        return {"verb": verb, "id": rest[0], "prefix": _prefix(rest[1].lower(), "proposal hash")}
    if verb == "kill":
        if len(rest) != 2:
            raise ValueError("List first: /kestra pauses, then type /kestra kill <exec> <taskrun>. " + USAGE)
        return {"verb": verb, "id": rest[0], "prefix": _prefix(rest[1], "task run id")}
    if verb == "resume" and len(rest) >= 2:
        inputs = {}
        for item in rest[2:]:
            key, separator, value = item.partition("=")
            if not separator or not key or key in inputs:
                raise ValueError("Resume inputs must be unique key=value pairs. Quote values containing spaces.")
            inputs[key] = value
        return {"verb": verb, "id": rest[0], "prefix": _prefix(rest[1], "task run id"), "inputs": inputs}
    if verb == "resume":
        raise ValueError("List first: /kestra pauses, then type /kestra resume <exec> <taskrun> [k=v ...]. " + USAGE)
    raise ValueError(USAGE)


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


def _apply_reply(incident_id: str, result: dict) -> str:
    """One phone-sized line per outcome. `pending` and `busy` are not failures: say what to do next."""
    status = result.get("status", "completed")
    message = str(result.get("message") or "")
    if status == "pending":
        return (f"{incident_id}: replay still running ({result.get('replay_state', 'unknown')}). The fix stays in place and "
                f"nothing was rolled back. Type /kestra apply {incident_id} {_short(str(result.get("proposal_sha256", "")))} again in a minute to keep following it; "
                f"it verifies or rolls back when the replay ends.\nReplay: {result.get('replay_execution_id', '?')}")
    if status == "busy":
        return f"{incident_id}: another apply is running for this incident. Nothing was written. Try again shortly."
    head = f"{incident_id}: {status}" + (" (no-op, already done)" if result.get("noop") else "")
    return f"{head}\n{message}\n{json.dumps(result, ensure_ascii=True)[:1500]}".strip()


def _short(value: str, n: int = 12) -> str:
    return value[:n]


class Commands:
    def __init__(self, ctx: Any, toolbox: Any):
        self.ctx, self.toolbox = ctx, toolbox

    def _read(self, name: str, args: dict) -> dict:
        return _unpack(self.ctx.dispatch_tool(name, args))

    def _write(self, name: str, args: dict) -> dict:
        """The typed command is the approval. Issue the one-shot ticket, then dispatch the handler."""
        self.toolbox.authorize(name, args, "command")
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
        if verb == "show":
            return self._show(command["id"])
        if verb == "apply":
            return self._apply(command["id"], command["prefix"])
        return self._pauses_or_decide(command)

    def _show(self, incident_id: str) -> str:
        result = self._read("kestra_incidents", {"incident_id": incident_id})
        if _error(result):
            return _error(result)
        proposal = result.get("proposal") or {}
        text = f"{incident_id}  {result.get('status', '?')}\n{_identity(result)}"
        if proposal:
            diff = proposal.get("diff", "")
            sha = proposal.get("proposal_sha256", "")
            text += f"\nBase revision {proposal.get('base_revision')}\nWhy: {clean(proposal.get('rationale', ''))}\n{clean(diff)}"
            if diff_fits(diff):
                text += f"\nSHA-256: {sha}\nTo apply exactly this, type:\n/kestra apply {incident_id} {_short(sha)}"
            else:
                text += (f"\n[Diff is {len(diff.splitlines())} lines and {len(diff)} characters; it cannot be approved here "
                         f"(limit {MAX_DIFF_LINES} lines and {MAX_DIFF_CHARS} characters).] Ask Hermes for a smaller fix.")
        elif result.get("protected"):
            text += "\nProtected flow: diagnose only, no fix can be proposed."
        else:
            text += "\nNo proposal yet. Ask Hermes to inspect the incident and propose a fix."
        outcome = result.get("result") or {}
        if outcome:
            text += f"\nResult: {outcome.get('status', '?')}. {str(outcome.get('message', ''))[:300]}"
        return text

    def _apply(self, incident_id: str, prefix: str) -> str:
        result = self._read("kestra_incidents", {"incident_id": incident_id})
        if _error(result):
            return _error(result)
        proposal = result.get("proposal") or {}
        sha = proposal.get("proposal_sha256")
        if not sha:
            return "No stored proposal. Ask Hermes to propose a fix first."
        if not sha.startswith(prefix):
            return (f"The hash {prefix} does not match the stored proposal for {incident_id} (it may have changed "
                    f"since you looked). Nothing was written. Run /kestra show {incident_id} and type the printed command.")
        if not diff_fits(proposal.get("diff", "")):
            return too_long_message(proposal.get("diff", ""), incident_id)
        applied = self._write("kestra_apply_fix", {"incident_id": incident_id, "proposal_sha256": sha})
        return _error(applied) or _apply_reply(incident_id, applied)

    def _pauses_or_decide(self, command: dict) -> str:
        result = self._read("kestra_pauses", {})
        if _error(result):
            return _error(result)
        rows = result.get("pauses", [])
        if command["verb"] == "pauses":
            lines = []
            for row in rows[:10]:
                declared = ", ".join(f"{i.get('id')}{'' if i.get('required', True) else '?'}" for i in row.get("declared_inputs", []))
                lines.append(f"{row['execution_id']}  {_identity(row)}\n{clean(str(row.get('description') or ''))[:300]}\n"
                             f"inputs: {declared or 'none'}\n"
                             f"/kestra resume {row['execution_id']} {_short(row['paused_task_run_id'])} [k=v ...]  or  "
                             f"/kestra kill {row['execution_id']} {_short(row['paused_task_run_id'])}")
            return "\n\n".join(lines) or "No paused executions."
        matches = [row for row in rows if row["execution_id"] == command["id"]
                   and row["paused_task_run_id"].startswith(command["prefix"])]
        if len(matches) != 1:
            return "No unique current pause for that execution and task run. Run /kestra pauses and type the printed command."
        pause = matches[0]
        decision = self._write("kestra_pause_decide", {
            "execution_id": command["id"], "paused_task_run_id": pause["paused_task_run_id"],
            "action": "resume" if command["verb"] == "resume" else "kill", "inputs": command.get("inputs", {}),
        })
        return _error(decision) or f"{command['id']}: {decision.get('status', 'done')}\n{json.dumps(decision, ensure_ascii=True)[:1800]}"
