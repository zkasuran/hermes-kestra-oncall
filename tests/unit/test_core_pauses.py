"""PauseDesk: listing, stale guard, idempotent decisions, input validation."""

from datetime import datetime, timedelta, timezone

import pytest
from test_core_support import PAUSE_SOURCE, Crash, FakeKestra, make_world

from kestra_oncall.incidents import PolicyError, StaleError


@pytest.fixture
def fake():
    server = FakeKestra().start()
    yield server
    server.stop()


def add_pause(fake, *, namespace="hko.demo", flow_id="refunds", source=PAUSE_SOURCE):
    fake.put_flow(namespace, flow_id, source.replace("id: refunds", f"id: {flow_id}").replace("namespace: hko.demo", f"namespace: {namespace}"))
    ex = fake.add_execution(namespace, flow_id, 1, [("prep", "SUCCESS"), ("approve", "PAUSED")], state="PAUSED",
                            inputs={"amount": 250})
    ex["labels"].append({"key": "team", "value": "finance"})
    ex["taskRunList"][1]["state"]["histories"] = [{"state": "PAUSED", "date": (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat().replace("+00:00", "Z")}]
    return ex


@pytest.fixture
def w(tmp_path, fake):
    return make_world(tmp_path, fake)


def pause_writes(fake):
    return fake.count("POST", "/actions/resume") - fake.count("POST", "/actions/resume/validate") + fake.count("DELETE", "/actions/kill")


def test_list_pauses_returns_question_inputs_and_context(w):
    ex = add_pause(w.fake)
    (row,) = w.desk.list_pauses()
    assert row["execution_id"] == ex["id"] and row["paused_task_run_id"] == ex["taskRunList"][1]["id"]
    assert (row["namespace"], row["flow_id"], row["paused_task_id"]) == ("hko.demo", "refunds", "approve")
    assert row["description"] == "Approve a refund of 250 EUR?"
    assert [(i["id"], i["type"], i["required"]) for i in row["declared_inputs"]] == [("approved", "BOOL", True), ("reason", "STRING", False)]
    assert 85 <= row["paused_for_seconds"] <= 200
    assert row["execution_inputs"] == {"amount": 250} and row["labels"] == {"team": "finance"} and row["protected"] is False


def test_list_pauses_ignores_other_namespaces_and_non_paused_and_fails_closed(tmp_path, fake):
    w = make_world(tmp_path, fake)
    add_pause(fake)
    add_pause(fake, namespace="hko.elsewhere", flow_id="lookalike")
    add_pause(fake, namespace="hko.demonstration", flow_id="boundary")
    fake.add_execution("hko.demo", "orders", 2, [("extract", "RUNNING")], state="RUNNING")
    assert [r["flow_id"] for r in w.desk.list_pauses()] == ["refunds"]
    closed = make_world(tmp_path / "closed", fake, namespaces=())
    assert closed.desk.list_pauses() == []


def test_resume_validates_then_resumes_and_reports_the_new_state(w):
    ex = add_pause(w.fake)
    run_id = ex["taskRunList"][1]["id"]
    r = w.desk.decide(ex["id"], run_id, "resume", {"approved": "true", "reason": "ok"}, actor="hermes:cli")
    assert r["status"] == "resumed" and r["new_state"] == "SUCCESS" and r["paused_task_state"] == "SUCCESS" and r["noop"] is False
    assert w.fake.resumed_with == {"approved": "true", "reason": "ok"}
    calls = [c for c in w.fake.calls if "/actions/resume" in c[1]]
    assert calls[0][1].endswith("/resume/validate") and calls[1][1].endswith("/resume")      # validate first
    steps = [e["step"] for e in w.store.journal(f"pause:{ex['id']}:{run_id}")]
    assert steps == ["resume_requested", "decided"]


def test_decision_is_idempotent_and_a_contradicting_call_does_not_act(w):
    ex = add_pause(w.fake)
    run_id = ex["taskRunList"][1]["id"]
    w.desk.decide(ex["id"], run_id, "resume", {"approved": "true"}, actor="alice")
    before = len(w.fake.calls)
    again = w.desk.decide(ex["id"], run_id, "resume", {"approved": "true"}, actor="alice")
    flip = w.desk.decide(ex["id"], run_id, "kill", None, actor="mallory")
    assert again["noop"] and flip["noop"] and flip["recorded_action"] == "resume" and flip["decided_by"] == "alice"
    assert len(w.fake.calls) == before and w.fake.executions[ex["id"]]["state"]["current"] == "SUCCESS"


def test_stale_guard_when_the_execution_moved_or_pauses_elsewhere(w):
    ex = add_pause(w.fake)
    run_id = ex["taskRunList"][1]["id"]
    with pytest.raises(StaleError, match="no longer paused on task run"):                    # wrong task run id
        w.desk.decide(ex["id"], "some-other-task-run", "resume", None, actor="t")
    ex["state"]["current"] = "SUCCESS"                                                       # someone answered in the UI
    ex["taskRunList"][1]["state"]["current"] = "SUCCESS"
    with pytest.raises(StaleError, match="now SUCCESS"):
        w.desk.decide(ex["id"], run_id, "resume", None, actor="t")
    assert pause_writes(w.fake) == 0 and w.store.pause_get(ex["id"], run_id) is None


def test_undeclared_and_invalid_inputs_are_refused_before_any_write(w):
    ex = add_pause(w.fake)
    run_id = ex["taskRunList"][1]["id"]
    with pytest.raises(PolicyError, match=r"not declared.*approved.*reason"):
        w.desk.decide(ex["id"], run_id, "resume", {"approvd": "true"}, actor="t")           # typo Kestra would drop silently
    with pytest.raises(PolicyError, match="Kestra rejected the resume inputs: approved"):
        w.desk.decide(ex["id"], run_id, "resume", {"approved": "maybe"}, actor="t")
    assert pause_writes(w.fake) == 0 and w.fake.executions[ex["id"]]["state"]["current"] == "PAUSED"
    assert w.store.pause_get(ex["id"], run_id) is None


def test_kill_and_argument_validation(w):
    ex = add_pause(w.fake)
    run_id = ex["taskRunList"][1]["id"]
    with pytest.raises(ValueError, match="action must be"):
        w.desk.decide(ex["id"], run_id, "approve", None, actor="t")
    with pytest.raises(ValueError, match="kill does not accept"):
        w.desk.decide(ex["id"], run_id, "kill", {"a": "b"}, actor="t")
    r = w.desk.decide(ex["id"], run_id, "kill", None, actor="t")
    assert r["status"] == "killed" and r["new_state"] == "KILLED" and r["paused_task_state"] == "KILLED"


def test_protected_and_foreign_pauses_are_refused(tmp_path, fake):
    w = make_world(tmp_path, fake, protected=("hko.demo.refunds",))
    ex = add_pause(fake)
    (row,) = w.desk.list_pauses()
    assert row["protected"] is True
    with pytest.raises(PolicyError, match="protected flow"):
        w.desk.decide(ex["id"], ex["taskRunList"][1]["id"], "resume", None, actor="t")
    other = add_pause(fake, namespace="hko.elsewhere", flow_id="lookalike")
    with pytest.raises(PolicyError, match="not in the allowed namespaces"):
        w.desk.decide(other["id"], other["taskRunList"][1]["id"], "resume", None, actor="t")
    assert pause_writes(fake) == 0


def test_interrupted_decision_is_adopted_not_repeated(w, monkeypatch):
    ex = add_pause(w.fake)
    run_id = ex["taskRunList"][1]["id"]

    def die(*a, **k):
        raise Crash()
    monkeypatch.setattr(w.store, "pause_finish", die)                       # resume landed, record not written
    with pytest.raises(Crash):
        w.desk.decide(ex["id"], run_id, "resume", {"approved": "true"}, actor="alice")
    assert w.store.pause_get(ex["id"], run_id)["status"] == "intent" and w.store.lock_info(f"pause:{ex['id']}") is None
    monkeypatch.undo()
    r = w.desk.decide(ex["id"], run_id, "resume", {"approved": "true"}, actor="alice")
    assert r["status"] == "resumed" and r["adopted_interrupted_decision"] is True
    assert pause_writes(w.fake) == 1                                          # only the first call wrote
    assert w.desk.decide(ex["id"], run_id, "resume", {"approved": "true"}, actor="alice")["noop"] is True


def test_interrupted_intent_with_different_inputs_is_not_adopted(w, monkeypatch):
    ex = add_pause(w.fake)
    run_id = ex["taskRunList"][1]["id"]
    monkeypatch.setattr(w.store, "pause_finish", lambda *a, **k: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        w.desk.decide(ex["id"], run_id, "resume", {"approved": "true"}, actor="alice")
    monkeypatch.undo()
    with pytest.raises(StaleError):
        w.desk.decide(ex["id"], run_id, "resume", {"approved": "false"}, actor="bob")


def test_resume_error_after_someone_else_resumed_is_reconciled_by_rereading(w, monkeypatch):
    ex = add_pause(w.fake)
    run_id = ex["taskRunList"][1]["id"]
    real = w.client.resume

    def lose_race(execution_id, inputs=None):
        ex["state"]["current"] = "SUCCESS"                                    # a colleague clicked first in the UI
        ex["taskRunList"][1]["state"]["current"] = "SUCCESS"
        return real(execution_id, inputs)                                     # Kestra answers HTTP 500
    monkeypatch.setattr(w.client, "resume", lose_race)
    r = w.desk.decide(ex["id"], run_id, "resume", {"approved": "true"}, actor="alice")
    assert r["status"] == "resumed" and r["new_state"] == "SUCCESS"
    steps = [e["step"] for e in w.store.journal(f"pause:{ex['id']}:{run_id}")]
    assert "write_error_but_moved" in steps


def test_resume_error_while_still_paused_is_raised_and_nothing_is_recorded(w, monkeypatch):
    from kestra_oncall.client import KestraError
    ex = add_pause(w.fake)
    run_id = ex["taskRunList"][1]["id"]
    monkeypatch.setattr(w.client, "resume", lambda *a, **k: (_ for _ in ()).throw(KestraError("down", status=503)))
    with pytest.raises(KestraError):
        w.desk.decide(ex["id"], run_id, "resume", {"approved": "true"}, actor="alice")
    assert w.store.pause_get(ex["id"], run_id)["status"] == "intent"          # retry is allowed
    monkeypatch.undo()
    assert w.desk.decide(ex["id"], run_id, "resume", {"approved": "true"}, actor="alice")["status"] == "resumed"


def test_busy_lock_does_not_write(w):
    ex = add_pause(w.fake)
    assert w.store.lock(f"pause:{ex['id']}", "other", 60)
    r = w.desk.decide(ex["id"], ex["taskRunList"][1]["id"], "resume", None, actor="t")
    assert r["status"] == "busy" and pause_writes(w.fake) == 0


def test_pause_rows_and_decisions_are_strictly_json_serialisable_and_carry_the_glue_keys(w):
    import json
    ex = add_pause(w.fake)
    rows = w.desk.list_pauses()
    json.dumps(rows, allow_nan=False)
    assert {"execution_id", "paused_task_run_id", "paused_task_id", "namespace", "flow_id", "description"} <= set(rows[0])
    json.dumps(w.desk.decide(ex["id"], ex["taskRunList"][1]["id"], "resume", {"approved": "true"}, actor="t"), allow_nan=False)


def add_two_pauses(fake):
    fake.put_flow("hko.demo", "parallel", PAUSE_SOURCE.replace("id: refunds", "id: parallel"))
    ex = fake.add_execution("hko.demo", "parallel", 1, [("approve_a", "PAUSED"), ("approve_b", "PAUSED")], state="PAUSED")
    return ex


def test_C5_two_paused_task_runs_are_flagged_and_resume_is_refused(w):
    ex = add_two_pauses(w.fake)
    rows = [r for r in w.desk.list_pauses() if r["execution_id"] == ex["id"]]
    assert len(rows) == 2 and all(r["ambiguous"] and r["paused_task_run_count"] == 2 for r in rows)
    with pytest.raises(PolicyError, match="2 paused task runs.*Kestra UI"):
        w.desk.decide(ex["id"], rows[0]["paused_task_run_id"], "resume", None, actor="t")
    assert pause_writes(w.fake) == 0 and w.store.pause_get(ex["id"], rows[0]["paused_task_run_id"]) is None
    killed = w.desk.decide(ex["id"], rows[0]["paused_task_run_id"], "kill", None, actor="t")
    assert killed["status"] == "killed" and "whole execution" in killed["message"]


def test_C5_a_single_pause_is_not_ambiguous(w):
    add_pause(w.fake)
    (row,) = w.desk.list_pauses()
    assert row["ambiguous"] is False and row["paused_task_run_count"] == 1


def test_C10_pauses_elsewhere_cannot_push_allowed_ones_off_the_first_page(w):
    for n in range(120):
        w.fake.add_execution("hko.elsewhere", f"noise{n}", 1, [("approve", "PAUSED")], state="PAUSED")
    mine = add_pause(w.fake)                                      # the oldest of all 121 paused executions
    rows = w.desk.list_pauses()
    assert [r["execution_id"] for r in rows] == [mine["id"]] and w.desk.last_truncated is False


def test_C10_paging_reaches_every_allowed_pause_and_reports_truncation(w, monkeypatch):
    import kestra_oncall.pauses as pauses
    for n in range(5):
        add_pause(w.fake, flow_id=f"mine{n}")
    monkeypatch.setattr(pauses, "PAUSE_PAGE_SIZE", 2)
    monkeypatch.setattr(pauses, "MAX_PAUSE_PAGES", 3)
    assert len(w.desk.list_pauses()) == 5 and w.desk.last_truncated is False
    monkeypatch.setattr(pauses, "MAX_PAUSE_PAGES", 2)
    assert len(w.desk.list_pauses()) == 4 and w.desk.last_truncated is True
