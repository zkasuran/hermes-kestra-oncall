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
    assert first["apply_command"] == "/kestra apply INC-0a1b2c3d"
    pause = body["pauses"][0]
    assert pause["resume_command"] == "/kestra resume 3GzyXexample4 approved=<value> note=<value>"
    assert pause["kill_command"] == "/kestra kill 3GzyXexample4"


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
    monkeypatch.setitem(incidents.ROWS[0], "logs", [{"message": "auth failed with hunter2-secret"}])
    text = client.get("/api/plugins/kestra-oncall/overview").text
    assert "hunter2-secret" not in text and "[REDACTED]" in text
