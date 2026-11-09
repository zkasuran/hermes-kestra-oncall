"""Runs INSIDE a throwaway Hermes profile (called by test_live_glue.py, never by pytest directly).

It loads kestra-oncall through Hermes's real plugin loader, then calls the tools through the real
`model_tools.handle_function_call` (pre_tool_call hook, Hermes approval gate, registry dispatch) and the
real /kestra slash command against a live Kestra. Only the human's answer in the approval prompt is
replaced: the first prompt is denied, later ones are approved. Prints one JSON document on the last line.
"""

from __future__ import annotations

import json
import sys

cfg = json.loads(sys.argv[1])
out: dict = {"prompts": []}

from hermes_cli.plugins import discover_plugins, get_plugin_manager  # noqa: E402

discover_plugins()
manager = get_plugin_manager()
out["plugins"] = manager.list_plugins()

from model_tools import handle_function_call  # noqa: E402
from tools import approval, approval_context  # noqa: E402

answers = list(cfg["answers"])


def human(command, description, **kwargs):
    out["prompts"].append(description)
    return answers.pop(0) if answers else "deny"


approval._yolo_active = lambda: False
approval_context._get_approval_mode = lambda: "manual"
approval.is_approved = lambda *_: False
approval._presence = lambda *_: (human, True, False, False)


def call(name: str, args: dict) -> dict:
    return json.loads(handle_function_call(name, args, task_id="glue-live", session_id="glue-live-session"))


out["status"] = call("kestra_status", {})
out["open"] = call("kestra_incident_open", {"execution_id": cfg["failed_execution"]})
incident_id = out["open"]["incident_id"]
out["propose"] = call("kestra_propose_fix", {"incident_id": incident_id, "new_source": cfg["fixed_source"],
                                              "rationale": "Reference the output of task one."})
sha = out["propose"]["proposal_sha256"]
out["apply_args"] = {"incident_id": incident_id, "proposal_sha256": sha}
out["apply_forged"] = call("kestra_apply_fix", {"incident_id": incident_id, "proposal_sha256": "0" * 64})
out["apply_denied"] = call("kestra_apply_fix", out["apply_args"])       # first prompt: deny
out["apply"] = call("kestra_apply_fix", out["apply_args"])              # second prompt: once
out["apply_again"] = call("kestra_apply_fix", out["apply_args"])        # third prompt: once, core no-op
out["pauses"] = call("kestra_pauses", {})
pause_args = {"execution_id": cfg["pause_execution"], "paused_task_run_id": cfg["pause_task_run"], "action": "resume",
              "inputs": {"approved": "true", "reason": "glue live test"}}
out["pause_unknown_task_run"] = call("kestra_pause_decide", {**pause_args, "paused_task_run_id": "nosuchtaskrun"})
out["pause_decide"] = call("kestra_pause_decide", pause_args)           # fourth prompt: once

command = manager._plugin_commands["kestra"]["handler"]
out["cmd_status"] = command("status")
out["cmd_incidents"] = command("incidents")
out["cmd_show"] = command("show " + incident_id)
print("RESULT " + json.dumps(out))
