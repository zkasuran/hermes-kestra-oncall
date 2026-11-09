"""Read-only routes behind the "Kestra on-call" dashboard tab and Desktop pane.

Mounted by Hermes at ``/api/plugins/kestra-oncall/``. Nothing here writes to Kestra: the routes list stored
incidents, their stored proposals and the current PAUSED executions, and tell the human which chat command
approves each one. Approval stays in the Hermes chat where the stored diff and exact proposal hash are shown.

Why plain ``def`` handlers: the Kestra client is synchronous, and Hermes runs plain handlers on a worker
thread so they never block the gateway event loop.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
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
CACHE_SECONDS = 5.0
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
    # The plugin's own sibling modules use relative imports (`from .client import ...`), which need the package to be
    # findable by name while they load. This registers only this plugin's own package under a private name; it does not
    # replace or wrap anything that belongs to Hermes.
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


def _proposal_view(proposal: Any) -> dict | None:
    """Core's stored proposal: proposal_sha256, base_revision, diff, diff_stat, rationale (new_source is never forwarded)."""
    if not isinstance(proposal, dict) or not proposal.get("proposal_sha256"):
        return None
    diff = proposal["diff"] if isinstance(proposal.get("diff"), str) else ""
    return {
        "sha256": proposal["proposal_sha256"],
        "base_revision": proposal["base_revision"],
        "rationale": _text(proposal.get("rationale")),
        "diff": diff[:MAX_DIFF_CHARS],
        "diff_truncated": len(diff) > MAX_DIFF_CHARS,
        "diff_stat": proposal.get("diff_stat"),
        # The chat commands refuse a diff nobody can read in full (glue: hooks.diff_fits), so the tab says so too.
        "fits_chat_approval": _sub("hooks").diff_fits(diff),
    }


def _log_lines(record: dict) -> list[str]:
    """Core stores logs as [{level, task_id, task_run_id, timestamp, message}]."""
    return [_text(row.get("message"), 400) for row in (record.get("logs") or [])[-MAX_LOG_LINES:]]


def _result_view(result: Any) -> dict | None:
    """The stored outcome of an apply (status, message and the revisions and replay it touched)."""
    if not isinstance(result, dict) or not result.get("status"):
        return None
    return {"status": _text(result["status"], 40), "message": _text(result.get("message"), 600),
            "fixed_revision": result.get("fixed_revision"), "rollback_revision": result.get("rollback_revision"),
            "replay_execution_id": _text(result.get("replay_execution_id"), 160)}


_APPLY_STATES = {"proposed", "applying"}


def _apply_command(incident_id: str, proposal: dict) -> str:
    """The typed command that approves exactly this proposal: incident id plus 12 characters of its SHA-256."""
    return f"/kestra apply {incident_id} {proposal['sha256'][:12]}"


def _can_apply(record: dict, proposal: dict | None) -> bool:
    """Only show the chat command while an apply makes sense: a stored proposal that is waiting or was interrupted.

    A verified or rolled-back incident has nothing left to apply, and a protected flow is never applied.
    """
    return bool(proposal and proposal["fits_chat_approval"] and not record["protected"] and record["status"] in _APPLY_STATES)


def _apply_note(record: dict, proposal: dict | None) -> str:
    if proposal and not proposal["fits_chat_approval"] and not record["protected"] and record["status"] in _APPLY_STATES:
        return "The diff is too long to approve in chat (limit 120 lines and 3000 characters). Ask Hermes for a smaller fix."
    return ""


def _incident_view(record: dict, proposal: Any = None) -> dict:
    """Core's public incident record (Oncall.get_incident). A renamed field raises KeyError, which a test catches."""
    stored = _proposal_view(proposal if proposal is not None else record.get("proposal"))
    incident_id = record["incident_id"]
    return {
        "incident_id": incident_id,
        "status": record["status"],
        "namespace": record["namespace"],
        "flow_id": record["flow_id"],
        "execution_id": record["execution_id"],
        "failed_task": _text(record.get("failed_task_id"), 200),
        "execution_revision": record.get("execution_revision"),
        "protected": bool(record["protected"]),
        "created": record.get("created"),
        "updated": record.get("updated"),
        "error_log": _log_lines(record),
        "result": _result_view(record.get("result")),
        "proposal": stored,
        "apply_command": _apply_command(incident_id, stored) if _can_apply(record, stored) else "",
        "apply_note": _apply_note(record, stored),
    }


def _pause_view(record: dict) -> dict:
    """Core's PauseDesk.list_pauses row. declared_inputs is [{id, type, required, defaults, description}]."""
    execution_id = record["execution_id"]
    inputs = [{"name": _text(row["id"], 120), "type": _text(row.get("type"), 40), "required": bool(row.get("required", True))}
              for row in (record.get("declared_inputs") or [])[:30]]
    hint = " ".join(f"{row['name']}=<value>" for row in inputs)
    task_run = record["paused_task_run_id"][:12]
    return {
        "execution_id": execution_id,
        "namespace": record["namespace"],
        "flow_id": record["flow_id"],
        "paused_task_id": _text(record.get("paused_task_id"), 200),
        "paused_task_run_id": _text(record["paused_task_run_id"], 200),
        "description": _text(record.get("description")),
        "paused_for_seconds": record.get("paused_for_seconds"),
        "resume_inputs": inputs,
        "protected": bool(record.get("protected")),
        "ambiguous": bool(record.get("ambiguous")),
        # Kestra resumes an execution as a whole, so with more than one PAUSED task run core refuses resume.
        "resume_command": "" if record.get("ambiguous") else f"/kestra resume {execution_id} {task_run} {hint}".strip(),
        "kill_command": f"/kestra kill {execution_id} {task_run}",
    }


def _safe_error(exc: Exception, secrets: dict[str, str]) -> str:
    """Use the plugin's own redaction so transport bodies and credentials never reach the browser."""
    if isinstance(exc, KeyError):
        return f"A record from the plugin core had an unexpected shape (missing field {exc}). Update the plugin and reload the page."
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
                    # The list rows are summaries; the full record carries logs, protection and the proposal.
                    record = runtime.oncall.get_incident(row["incident_id"])
                    proposal = record.get("proposal") or runtime.store.get_proposal(row["incident_id"])
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


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "at": 0.0, "value": None}


def _cache_key() -> str:
    """One entry per Hermes home (profile) and settings, so a profile never sees another profile's incidents."""
    try:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home())
    except Exception:
        home = ""
    return home + "|" + json.dumps(_plugin_settings(), sort_keys=True, default=str)


def cached_overview() -> dict:
    """The tab polls every few seconds and each build reads Kestra, so reuse a fresh result for a short time."""
    key = _cache_key()
    with _cache_lock:
        if _cache["key"] == key and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["value"]
        value = _redacted(build_overview())
        _cache.update(key=key, at=time.monotonic(), value=value)
        return value


@router.get("/overview")
def overview() -> dict:
    """Incidents, stored proposals and current pauses. Read-only."""
    return cached_overview()


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
