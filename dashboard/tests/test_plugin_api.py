"""Dashboard routes: read-only, shaped for the tab, and tolerant of a Kestra outage.

Run with the Hermes venv: ``python -m pytest dashboard/tests -q``. The core is replaced by the stub in
``stub_core.py`` so the routes are tested on their own; the live check against the real core and Kestra
belongs to the end-to-end run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import stub_core  # noqa: E402

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SETTINGS = {"kestra_url": "http://127.0.0.1:28090", "tenant": "main", "namespaces": ["hko.demo"],
            "protected_flows": ["hko.demo.protected_ledger"]}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    plugin = stub_core.make_plugin_copy(tmp_path / "plugin")
    for name in [m for m in sys.modules if m.startswith("kestra_oncall_dashboard_pkg")]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location("ko_plugin_api_under_test", plugin / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ko_plugin_api_under_test"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_plugin_settings", lambda: dict(SETTINGS))
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/kestra-oncall")
    yield module, TestClient(app)
    sys.modules.pop("ko_plugin_api_under_test", None)


def test_overview_lists_incidents_pauses_and_commands(api):
    _, client = api
    body = client.get("/api/plugins/kestra-oncall/overview").json()
    assert body["ok"] and body["connected"] and body["version"] == "2.0.9" and body["policy_ready"]
    assert [i["incident_id"] for i in body["incidents"]] == ["INC-0a1b2c3d", "INC-9f8e7d6c", "INC-11223344"]
    first = body["incidents"][0]
    assert first["proposal"]["sha256"] == "a" * 64 and "-      rows" in first["proposal"]["diff"]
    assert first["apply_command"] == "/kestra apply INC-0a1b2c3d " + "a" * 12
    pause = body["pauses"][0]
    assert pause["resume_command"] == "/kestra resume 3GzyXexample4 4TrRunExampl approved=<value> note=<value>"
    assert pause["kill_command"] == "/kestra kill 3GzyXexample4 4TrRunExampl"


def test_protected_incident_has_no_apply_command_and_no_proposal(api):
    _, client = api
    protected = client.get("/api/plugins/kestra-oncall/incidents/INC-9f8e7d6c").json()
    assert protected["protected"] is True and protected["proposal"] is None and protected["apply_command"] == ""


def test_kestra_outage_keeps_stored_incidents_and_skips_pauses(api, monkeypatch):
    module, client = api
    monkeypatch.setattr(module, "_plugin_settings", lambda: {**SETTINGS, "kestra_url": "http://127.0.0.1:1"})
    body = client.get("/api/plugins/kestra-oncall/overview").json()
    assert body["connected"] is False and "kestra" in body["errors"]
    assert len(body["incidents"]) == 3 and body["pauses"] == []


def test_empty_namespace_policy_is_reported(api, monkeypatch):
    module, client = api
    monkeypatch.setattr(module, "_plugin_settings", lambda: {**SETTINGS, "namespaces": []})
    assert client.get("/api/plugins/kestra-oncall/overview").json()["policy_ready"] is False


def test_bad_incident_ids_are_rejected_and_unknown_ids_are_404(api):
    _, client = api
    assert client.get("/api/plugins/kestra-oncall/incidents/bad id").status_code in {400, 404}
    assert client.get("/api/plugins/kestra-oncall/incidents/INC-ffffffff").status_code == 404


def test_only_get_routes_exist(api):
    module, _ = api
    methods = {m for route in module.router.routes for m in route.methods}
    assert methods == {"GET"}


def test_credentials_in_records_are_redacted(api, monkeypatch):
    module, client = api
    settings = module._sub("settings")
    monkeypatch.setattr(settings, "read_secrets", lambda: {"KESTRA_PASSWORD": "hunter2-secret", "KESTRA_USERNAME": "svc", "KESTRA_API_TOKEN": ""})
    incidents = module._sub("incidents")
    monkeypatch.setitem(incidents.ROWS[0], "logs", [{"level": "ERROR", "message": "auth failed with hunter2-secret"}])
    text = client.get("/api/plugins/kestra-oncall/overview").text
    assert "hunter2-secret" not in text and "[REDACTED]" in text


@pytest.mark.parametrize("status,expected", [
    ("proposed", "/kestra apply INC-0a1b2c3d " + "a" * 12), ("applying", "/kestra apply INC-0a1b2c3d " + "a" * 12),
    ("verified", ""), ("rolled_back", ""), ("stale", ""), ("conflict", ""), ("needs_human", ""), ("open", ""),
])
def test_apply_command_is_only_shown_while_an_apply_is_pending(api, monkeypatch, status, expected):
    module, client = api
    incidents = module._sub("incidents")
    monkeypatch.setitem(incidents.ROWS[0], "status", status)
    body = client.get("/api/plugins/kestra-oncall/overview").json()
    assert body["incidents"][0]["apply_command"] == expected
    assert body["incidents"][0]["proposal"] is not None  # the stored diff stays visible either way


def test_pending_apply_result_is_shown_with_its_message(api, monkeypatch):
    module, client = api
    incidents = module._sub("incidents")
    monkeypatch.setitem(incidents.ROWS[0], "status", "applying")
    monkeypatch.setitem(incidents.ROWS[0], "result", {"status": "pending", "message": "The replay stopped at a Pause.",
                                                      "fixed_revision": 4, "replay_execution_id": "7RpExample"})
    shown = client.get("/api/plugins/kestra-oncall/overview").json()["incidents"][0]
    assert shown["result"] == {"status": "pending", "message": "The replay stopped at a Pause.", "fixed_revision": 4,
                               "rollback_revision": None, "replay_execution_id": "7RpExample"}
    assert shown["apply_command"] == "/kestra apply INC-0a1b2c3d " + "a" * 12  # an interrupted apply can be resumed


def test_a_renamed_core_field_is_reported_not_shown_blank(api, monkeypatch):
    module, client = api
    incidents = module._sub("incidents")
    original = incidents.Oncall.get_incident

    def renamed(self, incident_id):
        record = original(self, incident_id)
        record["flow"] = record.pop("flow_id")  # core renamed a field
        return record

    monkeypatch.setattr(incidents.Oncall, "get_incident", renamed)
    body = client.get("/api/plugins/kestra-oncall/overview").json()
    assert "unexpected shape" in body["errors"]["incidents"] and "flow_id" in body["errors"]["incidents"]


def test_overview_is_cached_for_a_few_seconds_and_per_settings(api, monkeypatch):
    module, client = api
    calls = []
    real = module.build_overview
    monkeypatch.setattr(module, "build_overview", lambda: calls.append(1) or real())
    client.get("/api/plugins/kestra-oncall/overview")
    client.get("/api/plugins/kestra-oncall/overview")
    assert len(calls) == 1
    monkeypatch.setattr(module, "_plugin_settings", lambda: {**SETTINGS, "namespaces": ["other"]})
    client.get("/api/plugins/kestra-oncall/overview")
    assert len(calls) == 2  # a different settings set (another profile) never reuses the entry
    monkeypatch.setattr(module.time, "monotonic", lambda: module._cache["at"] + module.CACHE_SECONDS + 1)
    client.get("/api/plugins/kestra-oncall/overview")
    assert len(calls) == 3


def test_a_diff_too_long_for_chat_approval_has_no_command_and_says_why(api, monkeypatch):
    module, client = api
    store = module._sub("store")
    long_diff = "\n".join(f"+line {i}" for i in range(200))
    monkeypatch.setattr(store.Store, "get_proposal", lambda self, incident_id: {
        "proposal_sha256": "b" * 64, "base_revision": 3, "diff": long_diff, "diff_stat": "+200", "rationale": "big"})
    shown = client.get("/api/plugins/kestra-oncall/overview").json()["incidents"][0]
    assert shown["apply_command"] == "" and "too long to approve in chat" in shown["apply_note"]
    assert shown["proposal"]["fits_chat_approval"] is False and shown["proposal"]["diff"].startswith("+line 0")


def test_ambiguous_pause_has_no_resume_command_but_keeps_kill(api, monkeypatch):
    module, client = api
    pauses = module._sub("pauses")
    original = pauses.PauseDesk.list_pauses
    monkeypatch.setattr(pauses.PauseDesk, "list_pauses", lambda self: [{**original(self)[0], "ambiguous": True, "paused_task_run_count": 2}])
    pause = client.get("/api/plugins/kestra-oncall/overview").json()["pauses"][0]
    assert pause["ambiguous"] is True and pause["resume_command"] == ""
    assert pause["kill_command"] == "/kestra kill 3GzyXexample4 4TrRunExampl"
