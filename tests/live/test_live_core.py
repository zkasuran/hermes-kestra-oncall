"""Live core tests against a real Kestra 2.0.x. Skipped unless HKO_LIVE=1.

    HKO_LIVE=1 /home/asuran/hko-rig/hermes-agent/.venv/bin/python -m pytest tests/live/test_live_core.py -q

Credentials come from KESTRA_URL / KESTRA_USERNAME / KESTRA_PASSWORD / KESTRA_TENANT in the environment, or from
the env file named by HKO_KESTRA_ENV (default /home/asuran/hko-rig/secrets/kestra.env). Values are never printed.
Everything is created in namespace hko.coretest with a per-run suffix and deleted afterwards.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from kestra_oncall.client import KestraClient, KestraError
from kestra_oncall.incidents import Oncall, PolicyError, Policy, StaleError
from kestra_oncall.pauses import PauseDesk
from kestra_oncall.store import Store

pytestmark = pytest.mark.skipif(os.environ.get("HKO_LIVE") != "1", reason="set HKO_LIVE=1 to run against Kestra")

NS = "hko.coretest"
ENV_FILE = Path(os.environ.get("HKO_KESTRA_ENV", "/home/asuran/hko-rig/secrets/kestra.env"))


def _credentials() -> dict[str, str]:
    values = {k: os.environ[k] for k in ("KESTRA_URL", "KESTRA_USERNAME", "KESTRA_PASSWORD", "KESTRA_TENANT") if k in os.environ}
    if len(values) < 4 and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip().removeprefix("export ")
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values.setdefault(key, value.strip().strip("'\""))
    return values


@pytest.fixture(scope="module")
def client():
    c = _credentials()
    return KestraClient(c["KESTRA_URL"], c.get("KESTRA_TENANT", "main"), username=c["KESTRA_USERNAME"],
                        password=c["KESTRA_PASSWORD"], timeout=30)


@pytest.fixture
def cleanup(client):
    created: list[str] = []
    yield created
    for flow_id in created:
        try:
            for ex in client.search_executions(namespace=NS, flow_id=flow_id, size=100):
                try:
                    client.delete_execution(ex["id"])
                except KestraError:
                    pass
            client.delete_flow(NS, flow_id)
        except KestraError:
            pass


def flow_source(flow_id: str, message: str) -> str:
    return f"""id: {flow_id}
namespace: {NS}
tasks:
  - id: one
    type: io.kestra.plugin.core.debug.Return
    format: "{{{{ now() }}}}"
  - id: two
    type: io.kestra.plugin.core.log.Log
    message: "{message}"
  - id: three
    type: io.kestra.plugin.core.log.Log
    message: done
"""


GOOD, BAD, STILL_BAD = "stamp={{ outputs.one.value }}", "{{ outputs.nosuch.value }}", "{{ outputs.alsomissing.value }}"


def pause_source(flow_id: str) -> str:
    return f"""id: {flow_id}
namespace: {NS}
inputs:
  - id: amount
    type: INT
    defaults: 42
tasks:
  - id: prep
    type: io.kestra.plugin.core.debug.Return
    format: "prepared {{{{ inputs.amount }}}}"
  - id: approve
    type: io.kestra.plugin.core.flow.Pause
    description: "Approve a refund of {{{{ inputs.amount }}}} EUR?"
    onResume:
      - id: approved
        type: BOOL
        defaults: false
      - id: reason
        type: STRING
        required: false
  - id: after
    type: io.kestra.plugin.core.log.Log
    message: "approved={{{{ outputs.approve.onResume.approved }}}}"
"""


@pytest.fixture
def world(client, cleanup, tmp_path):
    """A flow whose revision 1 works, revision 2 breaks task `two`, plus a FAILED execution of revision 2."""
    flow_id = "live_orders_" + uuid.uuid4().hex[:8]
    cleanup.append(flow_id)
    client.create_flow(flow_source(flow_id, GOOD))
    ok = client.trigger(NS, flow_id, wait=True)
    assert ok["state"]["current"] == "SUCCESS"
    bad = client.update_flow(NS, flow_id, flow_source(flow_id, BAD))
    failed = client.trigger(NS, flow_id, wait=True)
    assert bad["revision"] == 2 and failed["state"]["current"] == "FAILED" and failed["flowRevision"] == 2
    store = Store(tmp_path / "live.db")
    policy = Policy(namespaces=[NS], protected_flows=[], replay_timeout_s=90)
    return SimpleNamespace(flow_id=flow_id, client=client, store=store, policy=policy, oncall=Oncall(client, store, policy),
                           desk=PauseDesk(client, store, policy), failed=failed, ok=ok, exec_id=failed["id"])


def open_and_propose(w, message=GOOD, rationale="task two referenced a missing output"):
    incident = w.oncall.open_incident(w.exec_id)
    proposal = w.oncall.propose_fix(incident["incident_id"], flow_source(w.flow_id, message), rationale)
    return incident, proposal


def replays_of(w) -> list[dict]:
    rows = w.client.search_executions(labels={"system.correlationId": w.exec_id}, size=50)
    return [r for r in rows if r["id"] != w.exec_id]


def task_runs(execution: dict) -> dict[str, dict]:
    return {r["taskId"]: r for r in execution["taskRunList"]}


def attempt_key(run: dict) -> list[tuple]:
    return [(a["state"]["startDate"], a["state"]["endDate"], a["state"]["current"]) for a in run["attempts"]]


# ------------------------------------------------------------------------------------------- client facts
def test_client_basics_against_real_server(world):
    c = world.client
    assert c.server_version().startswith("2.0.")
    assert c.validate_flow(flow_source(world.flow_id, GOOD)) == []
    assert "Invalid type" in c.validate_flow("id: x\nnamespace: hko.coretest\ntasks:\n  - id: t\n    type: io.kestra.plugin.core.nope.Nope\n")[0]["constraints"]
    errors = c.get_logs(world.exec_id, min_level="WARN")
    assert errors and all(r["level"] in {"WARN", "ERROR"} for r in errors)
    assert any("nosuch" in r["message"] for r in errors)
    with pytest.raises(KestraError) as err:
        c.get_flow(NS, "does_not_exist_" + uuid.uuid4().hex[:6])
    assert err.value.status == 404
    assert [r["revision"] for r in c.list_revisions(NS, world.flow_id)] == [1, 2]


# ------------------------------------------------------------------------------------------- open
def test_open_incident_reads_the_real_failure(world):
    b = world.oncall.open_incident(world.exec_id)
    failed_run = task_runs(world.failed)["two"]
    assert (b["failed_task_id"], b["failed_task_run_id"]) == ("two", failed_run["id"])
    assert any("nosuch" in l["message"] for l in b["logs"] if l["level"] == "ERROR")
    assert set(b["outputs"]) == {"one"} and "value" in b["outputs"]["one"]
    assert b["flow"]["execution_revision"] == 2 and "outputs.nosuch" in b["flow"]["source"]
    assert b["what_changed"]["last_success_revision"] == 1 and "outputs.nosuch" in b["what_changed"]["diff"]
    assert world.oncall.open_incident(world.exec_id)["newly_opened"] is False
    other = Oncall(world.client, Store(":memory:"), Policy(namespaces=["hko.demo"]))
    with pytest.raises(PolicyError):
        other.open_incident(world.exec_id)
    with pytest.raises(PolicyError, match="not FAILED"):
        world.oncall.open_incident(world.ok["id"])


# ------------------------------------------------------------------------------------------- apply: success
def test_apply_replays_from_the_failed_task_and_reuses_earlier_work(world):
    c = world.client
    original_before = c.get_execution(world.exec_id)
    original_stamp = c.get_task_outputs(world.exec_id, task_runs(original_before)["one"]["id"])
    incident, proposal = open_and_propose(world)
    r = world.oncall.apply_fix(incident["incident_id"], proposal["proposal_sha256"], actor="live-test")
    assert r["status"] == "verified", r
    assert (r["base_revision"], r["fixed_revision"]) == (2, 3)

    flow = c.get_flow(NS, world.flow_id)
    assert flow["revision"] == 3 and flow["source"] == flow_source(world.flow_id, GOOD)
    assert [x["revision"] for x in c.list_revisions(NS, world.flow_id)] == [1, 2, 3]

    replay = c.get_execution(r["replay_execution_id"])
    assert replay["state"]["current"] == "SUCCESS" and replay["flowRevision"] == 3
    assert replay["originalId"] == world.exec_id and replay["parentId"] == world.exec_id
    new, old = task_runs(replay), task_runs(original_before)
    # earlier task: a different task run id, the SAME attempt timestamps and the SAME output (a now() stamp)
    assert new["one"]["id"] != old["one"]["id"]
    assert attempt_key(new["one"]) == attempt_key(old["one"]) and len(new["one"]["attempts"]) == 1
    assert c.get_task_outputs(replay["id"], new["one"]["id"]) == original_stamp
    # failed task: original FAILED attempt kept, a new SUCCESS attempt appended
    assert attempt_key(new["two"])[:1] == attempt_key(old["two"]) and len(new["two"]["attempts"]) == 2
    assert [a["state"]["current"] for a in new["two"]["attempts"]] == ["FAILED", "SUCCESS"]
    assert new["two"]["state"]["current"] == "SUCCESS" and "three" not in old
    assert new["three"]["state"]["current"] == "SUCCESS"
    v = r["verification"]
    assert v["ok"] and v["reused_task_ids"] == ["one"] and v["rerun_task_ids"] == [] and v["outputs_equal"] is True
    # the original execution is untouched (audit trail): Kestra only adds the label system.replayed=true
    original_after = c.get_execution(world.exec_id)
    assert original_after["labels"] == original_before["labels"] + [{"key": "system.replayed", "value": "true"}]
    strip = lambda e: json.dumps({k: v for k, v in e.items() if k != "labels"}, sort_keys=True)  # noqa: E731
    assert strip(original_after) == strip(original_before)
    assert original_after["state"]["current"] == "FAILED" and original_after["flowRevision"] == 2
    assert len(replays_of(world)) == 1


def test_second_apply_is_a_noop_on_the_real_server(world):
    c = world.client
    incident, proposal = open_and_propose(world)
    first = world.oncall.apply_fix(incident["incident_id"], proposal["proposal_sha256"], actor="live-test")
    revisions, replays = len(c.list_revisions(NS, world.flow_id)), len(replays_of(world))
    again = world.oncall.apply_fix(incident["incident_id"], proposal["proposal_sha256"], actor="live-test-2")
    assert again["noop"] is True and again["replay_execution_id"] == first["replay_execution_id"]
    assert (len(c.list_revisions(NS, world.flow_id)), len(replays_of(world))) == (revisions, replays) == (3, 1)


# ------------------------------------------------------------------------------------------- apply: stale, rollback, interruption
def test_stale_proposal_is_refused_after_an_out_of_band_edit(world):
    c = world.client
    incident, proposal = open_and_propose(world)
    c.update_flow(NS, world.flow_id, flow_source(world.flow_id, BAD) + "# edited by a colleague\n")
    r = world.oncall.apply_fix(incident["incident_id"], proposal["proposal_sha256"], actor="live-test")
    assert r["status"] == "stale" and r["current_revision"] == 3
    assert [x["revision"] for x in c.list_revisions(NS, world.flow_id)] == [1, 2, 3] and replays_of(world) == []
    with pytest.raises(StaleError):
        world.oncall.apply_fix(incident["incident_id"], "0" * 64, actor="live-test")


def test_a_fix_that_still_fails_is_rolled_back_to_the_base_source(world):
    c = world.client
    base_source = c.get_flow(NS, world.flow_id)["source"]
    incident, proposal = open_and_propose(world, message=STILL_BAD)
    r = world.oncall.apply_fix(incident["incident_id"], proposal["proposal_sha256"], actor="live-test")
    assert r["status"] == "rolled_back" and r["replay_state"] == "FAILED", r
    assert (r["fixed_revision"], r["rollback_revision"]) == (3, 4)
    flow = c.get_flow(NS, world.flow_id)
    assert flow["revision"] == 4 and flow["source"] == base_source
    replay = c.get_execution(r["replay_execution_id"])
    assert replay["state"]["current"] == "FAILED" and replay["flowRevision"] == 3
    assert c.get_execution(world.exec_id)["state"]["current"] == "FAILED"
    assert len(replays_of(world)) == 1
    again = world.oncall.apply_fix(incident["incident_id"], proposal["proposal_sha256"], actor="live-test")
    assert again["noop"] is True and len(c.list_revisions(NS, world.flow_id)) == 4


class Crash(BaseException):
    pass


def test_interrupted_apply_adopts_the_fix_and_the_replay_without_duplicates(world, monkeypatch):
    c = world.client
    incident, proposal = open_and_propose(world)
    real = world.client.replay

    def replay_then_die(*a, **k):
        real(*a, **k)
        raise Crash()
    monkeypatch.setattr(world.client, "replay", replay_then_die)
    with pytest.raises(Crash):
        world.oncall.apply_fix(incident["incident_id"], proposal["proposal_sha256"], actor="live-test")
    monkeypatch.undo()
    assert len(replays_of(world)) == 1
    r = world.oncall.apply_fix(incident["incident_id"], proposal["proposal_sha256"], actor="live-test")
    assert r["status"] == "verified" and r["adopted_existing_replay"] is True
    assert len(replays_of(world)) == 1 and len(c.list_revisions(NS, world.flow_id)) == 3


# ------------------------------------------------------------------------------------------- pauses
def start_paused(client, flow_id, amount=250) -> dict:
    execution = client.trigger(NS, flow_id, {"amount": amount})
    return client.wait_terminal(execution["id"], timeout=30, leave_states={"PAUSED"})


@pytest.fixture
def pause_world(client, cleanup, tmp_path):
    flow_id = "live_refund_" + uuid.uuid4().hex[:8]
    cleanup.append(flow_id)
    client.create_flow(pause_source(flow_id))
    store = Store(tmp_path / "pause.db")
    policy = Policy(namespaces=[NS], protected_flows=[])
    return SimpleNamespace(flow_id=flow_id, client=client, store=store, desk=PauseDesk(client, store, policy))


def test_pause_is_listed_with_its_question_and_resumed_with_inputs(pause_world):
    w = pause_world
    paused = start_paused(w.client, w.flow_id)
    assert paused["state"]["current"] == "PAUSED"
    run = task_runs(paused)["approve"]
    (row,) = [r for r in w.desk.list_pauses() if r["flow_id"] == w.flow_id]
    assert row["execution_id"] == paused["id"] and row["paused_task_run_id"] == run["id"] and row["paused_task_id"] == "approve"
    assert row["description"] == "Approve a refund of 250 EUR?" and row["execution_inputs"] == {"amount": 250}
    assert [(i["id"], i["type"]) for i in row["declared_inputs"]] == [("approved", "BOOL"), ("reason", "STRING")]
    assert isinstance(row["paused_for_seconds"], int) and row["namespace"] == NS

    with pytest.raises(StaleError):
        w.desk.decide(paused["id"], "not-the-task-run", "resume", {"approved": "true"}, actor="live")
    with pytest.raises(PolicyError, match="not declared"):
        w.desk.decide(paused["id"], run["id"], "resume", {"nope": "1"}, actor="live")
    with pytest.raises(PolicyError, match="rejected the resume inputs"):
        w.desk.decide(paused["id"], run["id"], "resume", {"approved": "maybe"}, actor="live")
    assert w.client.get_execution(paused["id"])["state"]["current"] == "PAUSED"     # refusals wrote nothing

    result = w.desk.decide(paused["id"], run["id"], "resume", {"approved": "true", "reason": "customer called"}, actor="live")
    assert result["status"] == "resumed" and result["noop"] is False
    final = w.client.wait_terminal(paused["id"], timeout=30)
    assert final["state"]["current"] == "SUCCESS" and task_runs(final)["approve"]["id"] == run["id"]
    assert task_runs(final)["approve"]["state"]["current"] == "SUCCESS"
    assert w.client.get_task_outputs(paused["id"], run["id"])["onResume"] == {"approved": True, "reason": "customer called"}
    assert w.desk.decide(paused["id"], run["id"], "resume", {"approved": "true", "reason": "customer called"}, actor="other")["noop"] is True
    assert [r for r in w.desk.list_pauses() if r["flow_id"] == w.flow_id] == []


def test_pause_stale_guard_when_resumed_elsewhere_and_empty_inputs_and_kill(pause_world):
    w = pause_world
    paused = start_paused(w.client, w.flow_id, amount=1)
    run = task_runs(paused)["approve"]
    w.client.resume(paused["id"], {"approved": "true"})                              # a colleague uses the Kestra UI
    w.client.wait_terminal(paused["id"], timeout=30)
    with pytest.raises(StaleError, match="no longer paused|now SUCCESS"):
        w.desk.decide(paused["id"], run["id"], "resume", {"approved": "true"}, actor="live")
    assert w.store.pause_get(paused["id"], run["id"]) is None

    defaults = start_paused(w.client, w.flow_id, amount=2)
    run2 = task_runs(defaults)["approve"]
    assert w.desk.decide(defaults["id"], run2["id"], "resume", None, actor="live")["status"] == "resumed"   # empty multipart works
    assert w.client.get_task_outputs(defaults["id"], run2["id"])["onResume"]["approved"] is False

    doomed = start_paused(w.client, w.flow_id, amount=3)
    run3 = task_runs(doomed)["approve"]
    killed = w.desk.decide(doomed["id"], run3["id"], "kill", None, actor="live")
    assert killed["status"] == "killed" and killed["new_state"] == "KILLED"
    assert w.client.get_execution(doomed["id"])["state"]["current"] == "KILLED"
