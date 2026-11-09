"""Model-facing schemas and JSON-only handlers. Core owns all Kestra mutations."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from .settings import error_payload, open_runtime, read_secrets, redact, require_attended, session_context

ID = {"type": "string", "minLength": 1, "maxLength": 160, "pattern": "^[A-Za-z0-9_.-]+$"}
INCIDENT = {**ID, "description": "Stored INC-... id returned by kestra_incident_open or kestra_incidents."}
EXECUTION = {**ID, "description": "Exact Kestra execution id, not a flow id or URL."}
HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$", "description": "Exact proposal_sha256 returned by kestra_propose_fix; never invent or abbreviate it."}


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False,
    }}


SCHEMAS = {
    "kestra_status": _schema("kestra_status", "Read connectivity, Kestra version, policy and stored incident counts. Works when Kestra is down; no Kestra writes.", {}, []),
    "kestra_incident_open": _schema("kestra_incident_open", "Open an idempotent incident for a FAILED execution in an allowed namespace. Returns error logs, failed task, execution-revision source and what changed since last success. Protected flows are diagnose-only. No Kestra writes.", {"execution_id": EXECUTION}, ["execution_id"]),
    "kestra_incidents": _schema("kestra_incidents", "List stored incidents or read one incident and its proposal by incident_id. Read before applying a fix; no Kestra writes.", {
        "incident_id": INCIDENT, "status": {"type": "string", "maxLength": 40, "description": "Optional stored incident status filter, only when listing."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20, "description": "Maximum incidents when listing."},
    }, []),
    "kestra_propose_fix": _schema("kestra_propose_fix", "Validate and store the smallest complete replacement flow YAML for an open incident. Binds a fresh base revision and returns the exact diff and proposal_sha256. Does not update a Kestra flow or replay anything. Protected flows are refused.", {
        "incident_id": INCIDENT, "new_source": {"type": "string", "minLength": 1, "maxLength": 524288, "description": "Complete revised flow YAML, same namespace and flow id. Keep the failed task id so replay can target it."},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "One root-cause hypothesis supported by the incident logs and what-changed diff; explain the smallest fix."},
    }, ["incident_id", "new_source", "rationale"]),
    "kestra_apply_fix": _schema("kestra_apply_fix", "WRITE, requires human approval in an interactive chat. Apply the exact stored proposal, replay from the failed task and verify recovery, with guarded rollback on failure. Stale proposals are refused. Never call from webhook, cron or other unattended runs. Repeating a verified proposal is a no-op. If the replay is still running when the wait ends the status is `pending`: the fix stays and nothing is rolled back; call again with the same hash later to keep following it.", {
        "incident_id": INCIDENT, "proposal_sha256": HASH,
    }, ["incident_id", "proposal_sha256"]),
    "kestra_pauses": _schema("kestra_pauses", "Read allowed PAUSED executions and save their approval snapshots. Returns execution id, paused_task_run_id, declared resume inputs and question. Call before kestra_pause_decide. No Kestra writes.", {}, []),
    "kestra_pause_decide": _schema("kestra_pause_decide", "WRITE, requires human approval in an interactive chat. Resume or kill the exact paused task run previously read by kestra_pauses. The stored question and inputs are shown before approval; live state is checked again before the write. Never call unattended.", {
        "execution_id": EXECUTION,
        "paused_task_run_id": {**ID, "description": "Exact paused_task_run_id from kestra_pauses, not the YAML task id."},
        "action": {"type": "string", "enum": ["resume", "kill"], "description": "resume supplies the human's onResume inputs; kill terminates this paused execution."},
        "inputs": {"type": "object", "maxProperties": 30, "additionalProperties": {"type": "string", "maxLength": 4096}, "description": "Human-provided resume input values as strings. Use declared input names from kestra_pauses. Empty or omitted for kill."},
    }, ["execution_id", "paused_task_run_id", "action"]),
}
WRITE_TOOLS = frozenset({"kestra_apply_fix", "kestra_pause_decide"})


def validate_args(name: str, args: Any) -> dict:
    """Validate the small schema subset here too: direct registry calls bypass JSON schemas."""
    schema = SCHEMAS[name]["parameters"]
    if not isinstance(args, dict) or set(args) - schema["properties"].keys():
        raise ValueError("Tool arguments must be an object containing only documented fields.")
    if any(key not in args for key in schema["required"]):
        raise ValueError("A required tool argument is missing. Read the tool schema and retry.")
    for key, value in args.items():
        prop = schema["properties"][key]
        if prop["type"] == "string":
            if (not isinstance(value, str) or not prop.get("minLength", 0) <= len(value) <= prop.get("maxLength", 524288)
                    or ("pattern" in prop and not re.fullmatch(prop["pattern"], value))
                    or ("enum" in prop and value not in prop["enum"])):
                raise ValueError(f"Invalid {key}. Check its format and length in the tool schema.")
        elif prop["type"] == "integer":
            if type(value) is not int or not prop["minimum"] <= value <= prop["maximum"]:
                raise ValueError(f"Invalid {key}. Use an integer within the tool's limits.")
        elif prop["type"] == "object":
            if (not isinstance(value, dict) or len(value) > 30
                    or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", k)
                           or not isinstance(v, str) or len(v) > 4096 for k, v in value.items())):
                raise ValueError("inputs must contain at most 30 named string values of at most 4096 characters.")
    if name == "kestra_incidents" and "incident_id" in args and ("status" in args or "limit" in args):
        raise ValueError("Use incident_id alone when reading one incident.")
    if name == "kestra_pause_decide" and args["action"] == "kill" and args.get("inputs"):
        raise ValueError("kill does not accept resume inputs.")
    return args


class Toolbox:
    def __init__(self, ctx: Any, *, runtime_factory: Callable = open_runtime,
                 secret_reader: Callable = read_secrets, context_reader: Callable = session_context):
        self.ctx = ctx
        self.runtime_factory = runtime_factory
        self.secret_reader = secret_reader
        self.context_reader = context_reader

    def handler(self, name: str) -> Callable:
        def handle(args: dict, **kwargs: Any) -> str:
            return self.call(name, args, **kwargs)
        handle.__name__ = name
        return handle

    def call(self, name: str, args: dict, **kwargs: Any) -> str:
        """Never let an exception or credential reach the model or Hermes dispatch logger."""
        secrets: Mapping[str, str] = {}
        try:
            secrets = self.secret_reader()
            clean = validate_args(name, args)
            with self.runtime_factory(self.ctx, secrets) as runtime:
                result = self.execute(name, clean, runtime)
            return json.dumps(redact(result, secrets), ensure_ascii=True, allow_nan=False)
        except Exception as exc:
            return json.dumps(error_payload(exc, secrets), ensure_ascii=True)

    def execute(self, name: str, args: dict, runtime: Any) -> dict:
        from .hooks import ApprovalStore, checked_proposal

        oncall = runtime.oncall
        if name == "kestra_status":
            result = {"settings": runtime.settings.public(), "policy_ready": bool(runtime.settings.namespaces),
                      "summary": oncall.summary()}
            try:
                result.update(connected=True, version=runtime.client.server_version())
            except Exception as exc:
                result.update(connected=False, **error_payload(exc, {}))
            return result
        if name == "kestra_incident_open":
            return oncall.open_incident(args["execution_id"])
        if name == "kestra_incidents":
            if "incident_id" in args:
                return oncall.get_incident(args["incident_id"])
            return {"incidents": oncall.list_incidents(status=args.get("status"), limit=args.get("limit", 20))}
        if name == "kestra_propose_fix":
            return oncall.propose_fix(args["incident_id"], args["new_source"], args["rationale"])
        approvals = ApprovalStore(runtime.conn)
        if name == "kestra_pauses":
            rows = runtime.pauses.list_pauses()
            approvals.remember_pauses(rows)
            return {"pauses": rows}
        context = self.context_reader()
        require_attended(context)
        actor = "hermes:" + (context.get("HERMES_SESSION_PLATFORM") or "local")
        actor += ":" + (context.get("HERMES_SESSION_ID") or context.get("HERMES_SESSION_CHAT_ID") or "interactive")
        if name == "kestra_apply_fix":
            checked_proposal(runtime, args)
            self._require_ticket(runtime, name, args)
            return oncall.apply_fix(args["incident_id"], args["proposal_sha256"], actor=actor)
        snapshot = approvals.decision(args)
        self._require_ticket(runtime, name, args)
        return runtime.pauses.decide(snapshot["execution_id"], snapshot["paused_task_run_id"],
                                     snapshot["action"], snapshot["inputs"], actor=actor)

    @staticmethod
    def _require_ticket(runtime: Any, name: str, args: dict) -> None:
        """Write handlers run only after the approval step issued a one-shot ticket for these exact arguments."""
        from .hooks import Tickets

        if not Tickets(runtime.conn).consume(name, args):
            raise ValueError("No approval was recorded for this exact call, so nothing was written. Writes go through "
                             "Hermes's approval card (ask Hermes to apply) or the typed /kestra apply, resume or kill command.")

    def authorize(self, name: str, args: dict, source: str = "command") -> str:
        """Typed-command path: the human's own command is the approval. Builds the same stored view the approval
        card shows, issues the one-shot ticket and returns that text. Raises ValueError when nothing safe can be
        shown. Call only for a command a person typed."""
        from .hooks import Tickets, build_request

        secrets: Mapping[str, str] = {}
        try:
            secrets = self.secret_reader()
            require_attended(self.context_reader())
            clean = validate_args(name, args)
            with self.runtime_factory(self.ctx, secrets) as runtime:
                message, _ = build_request(runtime, name, clean, dict(secrets))
                Tickets(runtime.conn).issue(name, clean, source)
            return message
        except Exception as exc:
            raise ValueError(error_payload(exc, secrets)["error"]) from None
