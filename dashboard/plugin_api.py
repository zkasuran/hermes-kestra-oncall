"""Read-only routes behind the "Kestra on-call" dashboard tab and Desktop pane.

Mounted by Hermes at ``/api/plugins/kestra-oncall/``. Nothing here writes to Kestra: the routes list stored
incidents, their stored proposals and the current PAUSED executions, and tell the human which chat command
approves each one. Approval stays in the Hermes chat where the stored diff and exact proposal hash are shown.

Why plain ``def`` handlers: the Kestra client is synchronous, and Hermes runs plain handlers on a worker
thread so they never block the gateway event loop.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

PLUGIN = "kestra-oncall"
PACKAGE = "kestra_oncall_dashboard_pkg"
MAX_ROWS = 20
MAX_DIFF_CHARS = 20000
MAX_TEXT_CHARS = 2000
MAX_LOG_LINES = 12
_INCIDENT_ID = re.compile(r"[A-Za-z0-9_.-]{1,160}\Z")


def _package() -> Any:
    """Load the plugin package that sits one directory above ``dashboard/``.

    Hermes imports this file on its own, so the package is loaded here under a private name. Its
    ``__init__`` has no side effects at import time, which keeps this safe to call from any thread.
    """
    module = sys.modules.get(PACKAGE)
    if module is not None:
        return module
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)])
    if spec is None or spec.loader is None:
        raise RuntimeError("The kestra-oncall package next to dashboard/ could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(PACKAGE, None)
        raise
    return module


def _sub(name: str) -> Any:
    _package()
    import importlib

    return importlib.import_module(f"{PACKAGE}.{name}")


class _ConfigContext:
    """The only thing the runtime factory asks a context for is ``get_config``."""

    def __init__(self, values: dict[str, Any]):
        self.values = values

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def _plugin_settings() -> dict[str, Any]:
    """Settings live at ``plugins.entries.kestra-oncall.settings`` in the active profile's config."""
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly() or {}
    entry = ((config.get("plugins") or {}).get("entries") or {}).get(PLUGIN) or {}
    values = entry.get("settings")
    if not isinstance(values, dict):
        values = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    return dict(values)


def _open_runtime() -> tuple[Any, dict[str, str]]:
    """Return (context manager yielding the runtime, secrets). Separate so tests can replace it."""
    settings = _sub("settings")
    secrets = settings.read_secrets()
    return settings.open_runtime(_ConfigContext(_plugin_settings()), secrets), secrets


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    return value[:limit] if isinstance(value, str) else ("" if value is None else str(value)[:limit])


def _first(record: dict, *names: str) -> Any:
    for name in names:
        if record.get(name) not in (None, ""):
            return record[name]
    return None


def _flow_parts(record: dict) -> tuple[str, str]:
    namespace = _first(record, "namespace", "flow_namespace")
    flow_id = _first(record, "flow_id", "flowId")
    flow = record.get("flow")
    if isinstance(flow, dict):
        namespace = namespace or flow.get("namespace")
        flow_id = flow_id or flow.get("id") or flow.get("flow_id")
    elif isinstance(flow, str) and flow and not (namespace and flow_id):
        namespace, _, flow_id = flow.rpartition(".")
    return _text(namespace, 200), _text(flow_id, 200)


def _proposal_view(proposal: Any) -> dict | None:
    if not isinstance(proposal, dict) or not proposal.get("proposal_sha256"):
        return None
    diff = _text(proposal.get("diff"), MAX_DIFF_CHARS)
    return {
        "sha256": _text(proposal["proposal_sha256"], 64),
        "base_revision": proposal.get("base_revision"),
        "rationale": _text(proposal.get("rationale")),
        "diff": diff,
        "diff_truncated": isinstance(proposal.get("diff"), str) and len(proposal["diff"]) > MAX_DIFF_CHARS,
        "diff_stat": proposal.get("diff_stat"),
    }


def _log_lines(record: dict) -> list[str]:
    lines = []
    for row in (record.get("error_logs") or record.get("logs") or [])[-MAX_LOG_LINES:]:
        message = row.get("message") if isinstance(row, dict) else row
        lines.append(_text(message, 400))
    return lines


def _result_view(result: Any) -> dict | None:
    """The stored outcome of an apply (status, message and the revisions and replay it touched)."""
    if not isinstance(result, dict) or not result.get("status"):
        return None
    return {"status": _text(result["status"], 40), "message": _text(result.get("message"), 600),
            "fixed_revision": result.get("fixed_revision"), "rollback_revision": result.get("rollback_revision"),
            "replay_execution_id": _text(result.get("replay_execution_id"), 160)}


def _incident_view(record: dict, proposal: Any = None) -> dict:
    namespace, flow_id = _flow_parts(record)
    failed = record.get("failed_task")
    failed_id = failed.get("id") or failed.get("task_id") if isinstance(failed, dict) else failed
    incident_id = _first(record, "incident_id", "id")
    return {
        "incident_id": _text(incident_id, 160),
        "status": _text(record.get("status") or "open", 40),
        "namespace": namespace,
        "flow_id": flow_id,
        "execution_id": _text(_first(record, "execution_id", "executionId"), 160),
        "failed_task": _text(failed_id or _first(record, "failed_task_id"), 200),
        "execution_revision": record.get("execution_revision") or record.get("revision"),
        "protected": bool(record.get("protected")),
        "created": _first(record, "created", "created_at", "opened_at"),
        "updated": _first(record, "updated", "updated_at"),
        "error_log": _log_lines(record),
        "result": _result_view(record.get("result")),
        "proposal": _proposal_view(proposal if proposal is not None else record.get("proposal")),
        "apply_command": f"/kestra apply {incident_id}" if incident_id and not record.get("protected") else "",
    }


def _declared_inputs(raw: Any) -> list[dict]:
    """Declared onResume inputs arrive as a list of dicts or a mapping of name to type; show both."""
    rows: list[dict] = []
    if isinstance(raw, dict):
        for name, spec in list(raw.items())[:30]:
            kind = spec.get("type") if isinstance(spec, dict) else spec
            rows.append({"name": _text(name, 120), "type": _text(kind, 40), "required": bool(isinstance(spec, dict) and spec.get("required"))})
    elif isinstance(raw, list):
        for spec in raw[:30]:
            if isinstance(spec, dict):
                rows.append({"name": _text(spec.get("id") or spec.get("name"), 120), "type": _text(spec.get("type"), 40),
                             "required": bool(spec.get("required", True))})
            elif isinstance(spec, str):
                rows.append({"name": _text(spec, 120), "type": "", "required": True})
    return rows


def _pause_view(record: dict) -> dict:
    namespace, flow_id = _flow_parts(record)
    execution_id = _text(_first(record, "execution_id", "id"), 160)
    inputs = _declared_inputs(_first(record, "declared_inputs", "resume_inputs", "on_resume", "inputs_declared"))
    hint = " ".join(f"{row['name']}=<value>" for row in inputs)
    return {
        "execution_id": execution_id,
        "namespace": namespace,
        "flow_id": flow_id,
        "paused_task_id": _text(_first(record, "paused_task_id", "task_id"), 200),
        "paused_task_run_id": _text(_first(record, "paused_task_run_id", "task_run_id", "taskRunId"), 200),
        "description": _text(_first(record, "description", "pause_description")),
        "paused_for_seconds": _first(record, "paused_for_seconds", "paused_for"),
        "resume_inputs": inputs,
        "protected": bool(record.get("protected")),
        "resume_command": f"/kestra resume {execution_id} {hint}".strip(),
        "kill_command": f"/kestra kill {execution_id}",
    }


def _safe_error(exc: Exception, secrets: dict[str, str]) -> str:
    """Use the plugin's own redaction so transport bodies and credentials never reach the browser."""
    try:
        payload = _sub("settings").error_payload(exc, secrets)
        return f"{payload['error']} {payload['hint']}"[:600]
    except Exception:
        return f"The request could not be completed ({type(exc).__name__})."


def build_overview() -> dict:
    """Collect everything the tab shows. Each section fails on its own so a Kestra outage still shows incidents."""
    out: dict[str, Any] = {"ok": True, "generated_at": time.time(), "connected": False, "version": None,
                           "settings": {}, "summary": {}, "incidents": [], "pauses": [], "errors": {}}
    try:
        manager, secrets = _open_runtime()
    except Exception as exc:
        out.update(ok=False)
        out["errors"]["setup"] = _safe_error(exc, {})
        return out
    try:
        with manager as runtime:
            settings = runtime.settings
            out["settings"] = {"kestra_url": settings.kestra_url, "tenant": settings.tenant,
                               "namespaces": list(settings.namespaces), "protected_flows": list(settings.protected_flows),
                               "rollback_on_failure": settings.rollback_on_failure}
            out["policy_ready"] = bool(settings.namespaces)
            try:
                out["summary"] = runtime.oncall.summary()
            except Exception as exc:
                out["errors"]["summary"] = _safe_error(exc, secrets)
            try:
                out["version"] = runtime.client.server_version()
                out["connected"] = True
            except Exception as exc:
                out["errors"]["kestra"] = _safe_error(exc, secrets)
            try:
                for row in runtime.oncall.list_incidents(limit=MAX_ROWS):
                    incident_id = _first(row, "incident_id", "id")
                    # The list rows are summaries; the full record carries logs, protection and the proposal.
                    record = runtime.oncall.get_incident(incident_id) if incident_id else row
                    proposal = record.get("proposal")
                    if proposal is None and incident_id:
                        proposal = runtime.store.get_proposal(incident_id)
                    out["incidents"].append(_incident_view(record, proposal))
            except Exception as exc:
                out["errors"]["incidents"] = _safe_error(exc, secrets)
            if out["connected"]:
                try:
                    out["pauses"] = [_pause_view(row) for row in runtime.pauses.list_pauses()[:MAX_ROWS]]
                except Exception as exc:
                    out["errors"]["pauses"] = _safe_error(exc, secrets)
    except Exception as exc:
        out.update(ok=False)
        out["errors"]["setup"] = _safe_error(exc, secrets)
    return out


def build_incident(incident_id: str) -> dict:
    manager, secrets = _open_runtime()
    with manager as runtime:
        record = runtime.oncall.get_incident(incident_id)
        proposal = record.get("proposal") or runtime.store.get_proposal(incident_id)
        return _incident_view(record, proposal)


def _redacted(value: Any) -> Any:
    """Final pass: known credentials and credential-named fields never leave this process."""
    try:
        secrets = _sub("settings").read_secrets()
        return _sub("settings").redact(value, secrets)
    except Exception:
        return value


@router.get("/overview")
def overview() -> dict:
    """Incidents, stored proposals and current pauses. Read-only."""
    return _redacted(build_overview())


@router.get("/incidents/{incident_id}")
def incident(incident_id: str) -> dict:
    """One stored incident with its proposal. Read-only."""
    if not _INCIDENT_ID.match(incident_id):
        raise HTTPException(status_code=400, detail="Invalid incident id.")
    try:
        return _redacted(build_incident(incident_id))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=404, detail=_safe_error(exc, {})) from None
