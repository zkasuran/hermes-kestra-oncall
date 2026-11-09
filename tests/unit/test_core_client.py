"""client against the in-process fake Kestra."""

import pytest
from test_core_support import PASSWORD, USER, FakeKestra, make_world

from kestra_oncall.client import KestraClient, KestraError, WaitTimeout


@pytest.fixture
def fake():
    server = FakeKestra().start()
    yield server
    server.stop()


def client_for(fake, **kw):
    return KestraClient(fake.url, "main", username=USER, password=PASSWORD, timeout=5, **kw)


def test_version_and_basic_auth_and_bad_credentials(fake):
    assert client_for(fake).server_version() == "2.0.9"
    bad = KestraClient(fake.url, "main", username=USER, password="wrong-password-xyz", timeout=5)
    with pytest.raises(KestraError) as err:
        bad.get_execution("nope")
    assert err.value.status == 401 and "wrong-password-xyz" not in str(err.value)


def test_secrets_never_appear_in_error_text_even_when_the_server_echoes_them(fake):
    fake.echo_secret_in_errors = True
    client = client_for(fake)
    with pytest.raises(KestraError) as err:
        client._request("GET", "/boom")
    text = f"{err.value} {err.value.body} {err.value.args} {err.value!r}"
    assert PASSWORD not in text and "***" in err.value.body
    token_client = KestraClient(fake.url, "main", token="tok-secret-123456", timeout=5)
    with pytest.raises(KestraError) as err2:
        token_client.get_execution("x")
    assert "tok-secret-123456" not in str(err2.value) + err2.value.body


def test_connection_failure_is_a_kestra_error_without_status():
    client = KestraClient("http://127.0.0.1:1", "main", username="u", password="pw-secret-1", timeout=1)
    with pytest.raises(KestraError) as err:
        client.server_version()
    assert err.value.status is None and "could not be reached" in str(err.value) and "pw-secret-1" not in str(err.value)


def test_logs_are_filtered_client_side_because_kestra_ignores_min_level(tmp_path, fake):
    w = make_world(tmp_path, fake)
    rows = w.client.get_logs(w.exec_id, min_level="WARN")
    assert [r["level"] for r in rows] == ["ERROR"]
    assert len(w.client.get_logs(w.exec_id, min_level="DEBUG")) == 2
    assert w.client.get_logs(w.exec_id, min_level="DEBUG", limit=1)[0]["level"] == "ERROR"
    assert w.client.get_logs(w.exec_id, task_run_id="nope") == []


def test_validate_flow_returns_only_violations(fake):
    client = client_for(fake)
    assert client.validate_flow("id: a\nnamespace: n\ntasks:\n  - id: t\n    type: x.y.Z\n") == []
    assert "Invalid type" in client.validate_flow("INVALID_TYPE")[0]["constraints"]
    assert client.validate_flow("id: a\nnamespace: n\n")[0]["constraints"].startswith("Validation error: tasks")


def test_flow_read_write_revisions_and_missing_flow(tmp_path, fake):
    w = make_world(tmp_path, fake)
    flow = w.client.get_flow("hko.demo", "orders")
    assert flow["revision"] == 2 and "nosuch" in flow["source"]
    assert "nosuch" not in w.client.get_flow("hko.demo", "orders", revision=1)["source"]
    updated = w.client.update_flow("hko.demo", "orders", flow["source"] + "# c\n")
    assert updated["revision"] == 3
    assert [r["revision"] for r in w.client.list_revisions("hko.demo", "orders")] == [1, 2, 3]
    with pytest.raises(KestraError) as err:
        w.client.get_flow("hko.demo", "missing")
    assert err.value.status == 404


def test_search_filters_and_label_lookup(tmp_path, fake):
    w = make_world(tmp_path, fake)
    failed = w.client.search_executions(namespace="hko.demo", flow_id="orders", state=["FAILED"])
    assert [r["id"] for r in failed] == [w.exec_id]
    assert [r["state"]["current"] for r in w.client.search_executions(state=["SUCCESS"])] == ["SUCCESS"]
    assert w.client.search_executions(labels={"system.correlationId": w.exec_id})[0]["id"] == w.exec_id
    assert w.client.search_executions(labels={"system.correlationId": "other"}) == []


def test_resume_trigger_use_multipart_even_when_empty_and_kill_uses_delete(tmp_path, fake):
    w = make_world(tmp_path, fake)
    paused = fake.add_execution("hko.demo", "refunds", 1, [("prep", "SUCCESS"), ("approve", "PAUSED")], state="PAUSED")
    w.client.validate_resume(paused["id"], None)
    w.client.validate_resume(paused["id"], {"approved": "true"})
    w.client.resume(paused["id"], {"approved": "true", "reason": "ok"})
    assert fake.resumed_with == {"approved": "true", "reason": "ok"}
    again = fake.add_execution("hko.demo", "refunds", 1, [("approve", "PAUSED")], state="PAUSED")
    w.client.kill(again["id"])
    assert ("DELETE", f"/api/v1/main/executions/{again['id']}/actions/kill") in fake.calls
    with pytest.raises(KestraError) as err:                      # Kestra answers 500 for a non-paused execution
        w.client.resume(paused["id"], None)
    assert err.value.status == 500


def test_wait_terminal_returns_and_times_out(tmp_path, fake):
    w = make_world(tmp_path, fake)
    assert w.client.wait_terminal(w.exec_id, timeout=2)["state"]["current"] == "FAILED"
    running = fake.add_execution("hko.demo", "orders", 2, [("extract", "RUNNING")], state="RUNNING")
    with pytest.raises(WaitTimeout) as err:
        w.client.wait_terminal(running["id"], timeout=0.3, poll=0.1)
    assert err.value.execution["id"] == running["id"] and "RUNNING" in str(err.value)
    assert w.client.wait_terminal(running["id"], timeout=1, leave_states={"RUNNING"})["id"] == running["id"]
