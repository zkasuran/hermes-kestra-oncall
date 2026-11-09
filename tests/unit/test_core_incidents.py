"""Incident engine: policy, proposal, apply state machine, retries and recovery, against a fake Kestra."""

import hashlib
import json

import pytest
from test_core_support import (BASE_SOURCE, FIXED_SOURCE, FLOW, GOOD_SOURCE, NS, PASSWORD, Crash, FakeKestra,
                               make_world)

from kestra_oncall import incidents as inc
from kestra_oncall.client import KestraError
from kestra_oncall.flowsrc import sha256_text
from kestra_oncall.incidents import PolicyError, StaleError, UnknownIncident


@pytest.fixture
def fake():
    server = FakeKestra().start()
    yield server
    server.stop()


@pytest.fixture
def w(tmp_path, fake):
    return make_world(tmp_path, fake)


def proposed(w, source=FIXED_SOURCE):
    incident = w.oncall.open_incident(w.exec_id)
    proposal = w.oncall.propose_fix(incident["incident_id"], source, "use the extract output")
    return incident["incident_id"], proposal


def writes(fake):
    return fake.count("PUT", "/flows/"), fake.count("POST", "/actions/replay")


# ------------------------------------------------------------------------------------ policy
def test_empty_allowlist_fails_closed(tmp_path, fake):
    w = make_world(tmp_path, fake, namespaces=())
    with pytest.raises(PolicyError, match="No namespaces are allowed"):
        w.oncall.open_incident(w.exec_id)
    assert w.desk.list_pauses() == [] and w.store.list_incidents() == []
    assert not inc.Policy(namespaces=["", "  ", "."]).namespace_allowed("hko.demo")   # blank prefix must not match all


def test_prefix_match_respects_dot_boundary(tmp_path, fake):
    policy = inc.Policy(namespaces=["hko.demo"])
    assert policy.namespace_allowed("hko.demo") and policy.namespace_allowed("hko.demo.sub")
    assert not policy.namespace_allowed("hko.demonstration") and not policy.namespace_allowed("hko")
    assert inc.Policy(namespaces=["hko.demo."]).namespace_allowed("hko.demo.sub")


def test_outside_prefix_is_refused_and_nothing_is_stored(tmp_path, fake):
    w = make_world(tmp_path, fake, namespaces=("hko.other",))
    with pytest.raises(PolicyError, match="not in the allowed namespaces"):
        w.oncall.open_incident(w.exec_id)
    assert w.store.list_incidents() == [] and writes(fake) == (0, 0)


def test_non_failed_execution_is_refused(w):
    with pytest.raises(PolicyError, match="is SUCCESS, not FAILED"):
        w.oncall.open_incident(w.ok_run["id"])
    assert w.store.list_incidents() == []


def test_protected_flow_is_diagnose_only(tmp_path, fake):
    w = make_world(tmp_path, fake, protected=(f"{NS}.{FLOW}",))
    incident = w.oncall.open_incident(w.exec_id)
    assert incident["protected"] is True and incident["failed_task_id"] == "transform"
    with pytest.raises(PolicyError, match="protected flow"):
        w.oncall.propose_fix(incident["incident_id"], FIXED_SOURCE, "x")
    w.store.save_proposal(incident["incident_id"], proposal_sha256="h", base_revision=2, base_source_sha256="b",
                          base_source=BASE_SOURCE, new_source=FIXED_SOURCE, rationale="", diff="", diff_stat={}, validation=[])
    with pytest.raises(PolicyError, match="protected flow"):                      # even a smuggled proposal
        w.oncall.apply_fix(incident["incident_id"], "h", actor="t")
    assert writes(fake) == (0, 0)


# ------------------------------------------------------------------------------------ open
def test_incident_id_is_deterministic_and_reopening_is_idempotent(w):
    first = w.oncall.open_incident(w.exec_id)
    expected = "INC-" + hashlib.sha256(w.exec_id.encode()).hexdigest()[:8]
    assert first["incident_id"] == expected and first["newly_opened"] is True
    second = w.oncall.open_incident(w.exec_id)
    assert second["incident_id"] == expected and second["newly_opened"] is False
    assert len(w.store.list_incidents()) == 1 and len(w.store.journal(expected)) == 1
    assert w.oncall.get_incident(expected)["execution_id"] == w.exec_id


def test_bundle_has_failed_task_logs_outputs_and_what_changed(w):
    b = w.oncall.open_incident(w.exec_id)
    failed_run = next(r for r in w.failed["taskRunList"] if r["taskId"] == "transform")
    assert (b["failed_task_id"], b["failed_task_run_id"]) == ("transform", failed_run["id"])
    assert (b["namespace"], b["flow_id"], b["execution_revision"]) == (NS, FLOW, 2)
    assert [l["level"] for l in b["logs"]] == ["ERROR"] and "nosuch" in b["logs"][0]["message"]
    assert b["outputs"] == {"extract": {"value": "out-extract"}}
    assert b["flow"]["source"] == BASE_SOURCE and b["flow"]["current_source"] is None
    change = b["what_changed"]
    assert (change["last_success_revision"], change["failing_revision"]) == (1, 2)
    assert "-    message: \"{{ outputs.extract.value }}\"" in change["diff"] and "+    message: \"{{ outputs.nosuch.value }}\"" in change["diff"]
    assert b["protected"] is False and b["status"] == "open"


def test_bundle_sends_current_source_when_flow_moved_on(w):
    w.fake.put_flow(NS, FLOW, BASE_SOURCE + "# newer\n")
    b = w.oncall.open_incident(w.exec_id)
    assert b["flow"]["current_revision"] == 3 and b["flow"]["source"] == BASE_SOURCE
    assert b["flow"]["current_source"].endswith("# newer\n")


def test_failed_leaf_picks_the_innermost_failure():
    runs = [{"id": "g", "taskId": "group", "state": {"current": "FAILED", "startDate": "1"}},
            {"id": "i", "taskId": "inner", "parentTaskRunId": "g", "state": {"current": "FAILED", "startDate": "2"}},
            {"id": "ok", "taskId": "fine", "state": {"current": "SUCCESS"}}]
    assert inc.failed_leaf({"taskRunList": runs})["taskId"] == "inner"
    assert inc.failed_leaf({"taskRunList": [runs[2]]}) is None


# ------------------------------------------------------------------------------------ propose
@pytest.mark.parametrize("mutate,message", [
    (lambda s: s.replace(f"namespace: {NS}", "namespace: hko.other"), "keep namespace and id"),
    (lambda s: s.replace(f"id: {FLOW}", "id: renamed", 1), "keep namespace and id"),
    (lambda s: s.replace("format: ok", "format: INVALID_TYPE"), "Kestra rejects this flow"),
    (lambda s: s.split("  - id: transform")[0], "removes the failed task 'transform'"),
    (lambda s: s, "identical to the current revision 2"),
    (lambda s: "", "new_source is empty"),
    (lambda s: "a: [b", "not valid YAML"),
])
def test_propose_refusals_store_nothing(w, mutate, message):
    incident = w.oncall.open_incident(w.exec_id)
    with pytest.raises(PolicyError, match=message):
        w.oncall.propose_fix(incident["incident_id"], mutate(BASE_SOURCE), "r")
    assert w.store.get_proposal(incident["incident_id"]) is None and writes(w.fake) == (0, 0)


def test_unknown_incident(w):
    with pytest.raises(UnknownIncident):
        w.oncall.propose_fix("INC-deadbeef", FIXED_SOURCE, "r")


def test_proposal_record_and_commitment_hash(w):
    incident_id, p = proposed(w)
    stored = w.store.get_proposal(incident_id)
    assert p["proposal_sha256"] == stored["proposal_sha256"] and p["base_revision"] == 2
    assert stored["new_source"] == FIXED_SOURCE and stored["base_source"] == BASE_SOURCE
    assert p["diff_stat"] == {"added": 1, "removed": 1} and "| upper" in p["diff"]
    recomputed = hashlib.sha256(json.dumps({
        "incident_id": incident_id, "execution_id": w.exec_id, "failed_task_id": "transform", "base_revision": 2,
        "base_source_sha256": sha256_text(BASE_SOURCE), "new_source_sha256": sha256_text(FIXED_SOURCE)},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert p["proposal_sha256"] == recomputed
    assert w.oncall.get_incident(incident_id)["proposal"]["proposal_sha256"] == recomputed
    assert w.oncall.get_incident(incident_id)["status"] == "proposed"
    assert w.oncall.propose_fix(incident_id, FIXED_SOURCE, "again")["proposal_sha256"] == recomputed


def test_hash_changes_with_new_bytes_and_with_a_new_base(w):
    incident_id, p1 = proposed(w)
    p2 = w.oncall.propose_fix(incident_id, FIXED_SOURCE + "# tweak\n", "r")
    assert p2["proposal_sha256"] != p1["proposal_sha256"]
    w.fake.put_flow(NS, FLOW, BASE_SOURCE + "# someone edited\n")                 # base moves to revision 3
    p3 = w.oncall.propose_fix(incident_id, FIXED_SOURCE, "r")
    assert p3["base_revision"] == 3 and p3["proposal_sha256"] != p1["proposal_sha256"]
    with pytest.raises(StaleError, match="does not match the latest"):             # the old approval is dead
        w.oncall.apply_fix(incident_id, p1["proposal_sha256"], actor="t")
    assert writes(w.fake) == (0, 0)


# ------------------------------------------------------------------------------------ apply: happy path and retries
def test_apply_verifies_with_reuse_proof_and_leaves_the_original_untouched(w):
    incident_id, p = proposed(w)
    strip = lambda e: json.dumps({k: v for k, v in e.items() if k != "labels"}, sort_keys=True)  # noqa: E731
    original_before = strip(w.fake.executions[w.exec_id])
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="hermes:test")
    assert r["status"] == "verified" and r["noop"] is False
    assert (r["base_revision"], r["fixed_revision"]) == (2, 3) and r["rollback_revision"] is None
    assert w.fake.current(NS, FLOW)["source"] == FIXED_SOURCE and w.fake.current(NS, FLOW)["revision"] == 3
    assert writes(w.fake) == (1, 1)
    replay = w.fake.executions[r["replay_execution_id"]]
    assert replay["originalId"] == w.exec_id and replay["flowRevision"] == 3
    v = r["verification"]
    assert v["ok"] and v["reused_task_ids"] == ["extract"] and v["rerun_task_ids"] == [] and v["failed_task_recovered"]
    assert v["failed_task_attempts"] == {"original": 1, "replay": 2} and v["outputs_equal"] is True
    assert strip(w.fake.executions[w.exec_id]) == original_before   # audit trail unchanged (Kestra only adds a label)
    assert {"key": "system.replayed", "value": "true"} in w.fake.executions[w.exec_id]["labels"]
    assert w.oncall.get_incident(incident_id)["status"] == "verified"
    steps = [e["step"] for e in w.store.journal(incident_id)]
    assert steps == ["opened", "proposed", "write_fix", "fix_written", "replay", "replay_started", "replay_finished", "verified"]


def test_second_apply_is_a_noop_with_no_kestra_calls(w):
    incident_id, p = proposed(w)
    first = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    calls_before = len(w.fake.calls)
    again = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="someone-else")
    assert again["noop"] is True and again["status"] == "verified" and again["replay_execution_id"] == first["replay_execution_id"]
    assert len(w.fake.calls) == calls_before and writes(w.fake) == (1, 1)
    with pytest.raises(PolicyError, match="already fixed and verified"):
        w.oncall.propose_fix(incident_id, FIXED_SOURCE + "# x\n", "r")


def test_apply_guards_unknown_and_missing_proposals(w):
    incident = w.oncall.open_incident(w.exec_id)
    with pytest.raises(StaleError, match="No proposal is stored"):
        w.oncall.apply_fix(incident["incident_id"], "f" * 64, actor="t")
    incident_id, p = proposed(w)
    with pytest.raises(StaleError, match="does not match the latest"):
        w.oncall.apply_fix(incident_id, "0" * 64, actor="t")
    assert writes(w.fake) == (0, 0)


# ------------------------------------------------------------------------------------ apply: stale, conflict, refused, ineffective
def test_stale_guard_after_out_of_band_edit_writes_nothing(w):
    incident_id, p = proposed(w)
    w.fake.put_flow(NS, FLOW, BASE_SOURCE + "# hotfix by a colleague\n")
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "stale" and r["current_revision"] == 3
    assert writes(w.fake) == (0, 0) and w.fake.current(NS, FLOW)["revision"] == 3
    assert w.oncall.get_incident(incident_id)["status"] == "stale"
    p2 = w.oncall.propose_fix(incident_id, FIXED_SOURCE + "# on the new base\n", "r")       # recovery path
    assert w.oncall.apply_fix(incident_id, p2["proposal_sha256"], actor="t")["status"] == "verified"


def test_stale_when_source_changed_even_if_revision_number_matches(w):
    incident_id, p = proposed(w)
    w.fake.flows[(NS, FLOW)][-1]["source"] = BASE_SOURCE + "# same revision, different bytes\n"
    assert w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")["status"] == "stale"
    assert writes(w.fake) == (0, 0)


def test_conflict_when_someone_writes_between_our_read_and_write(w):
    incident_id, p = proposed(w)
    w.fake.on_put = lambda fake, ns, fid, body: fake.put_flow(ns, fid, BASE_SOURCE + "# racing writer\n")
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "conflict" and r["found_revision"] == 4 and r["source_landed"] is True
    assert writes(w.fake) == (1, 0)                                   # no replay, no rollback over the other change
    assert w.oncall.get_incident(incident_id)["status"] == "conflict"
    assert len(w.store.journal(incident_id)) and w.store.journal(incident_id)[-1]["step"] == "conflict"


def test_ineffective_write_is_detected_by_read_back(w):
    incident_id, p = proposed(w)
    w.fake.swallow_put = True
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "conflict" and r["source_landed"] is False and "no longer holds the proposal" in r["message"]
    assert writes(w.fake) == (1, 0)


def test_refused_write_raises_leaves_proposal_usable_and_retry_succeeds(w):
    incident_id, p = proposed(w)
    w.fake.refuse_put = 403
    with pytest.raises(KestraError) as err:
        w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert err.value.status == 403 and PASSWORD not in str(err.value)
    assert writes(w.fake) == (1, 0) and w.fake.current(NS, FLOW)["revision"] == 2
    assert w.oncall.get_incident(incident_id)["status"] == "proposed"
    assert w.store.lock_info(incident_id) is None                     # lock released after the failure
    w.fake.refuse_put = None
    assert w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")["status"] == "verified"
    assert writes(w.fake) == (2, 1)


# ------------------------------------------------------------------------------------ apply: adoption and interruption
def test_adopts_an_already_applied_source_without_writing_again(w):
    incident_id, p = proposed(w)
    w.fake.put_flow(NS, FLOW, FIXED_SOURCE)                           # a colleague applied exactly this fix
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "verified" and r["adopted_existing_write"] is True and r["fixed_revision"] == 3
    assert writes(w.fake) == (0, 1)


def test_crash_after_write_resumes_by_adopting_the_fix(w, monkeypatch):
    incident_id, p = proposed(w)
    real = w.client.update_flow

    def write_then_die(*a, **k):
        real(*a, **k)
        raise Crash()
    monkeypatch.setattr(w.client, "update_flow", write_then_die)
    with pytest.raises(Crash):
        w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert w.oncall.get_incident(incident_id)["status"] == "applying" and w.store.lock_info(incident_id) is None
    monkeypatch.undo()
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "verified" and r["adopted_existing_write"] is True
    assert writes(w.fake) == (1, 1)


def test_crash_after_replay_before_persist_adopts_the_existing_replay(w, monkeypatch):
    incident_id, p = proposed(w)
    real = w.client.replay

    def replay_then_die(*a, **k):
        real(*a, **k)
        raise Crash()
    monkeypatch.setattr(w.client, "replay", replay_then_die)
    with pytest.raises(Crash):
        w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert w.oncall.get_incident(incident_id)["apply_state"].get("replay_execution_id") is None
    monkeypatch.undo()
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "verified" and r["adopted_existing_replay"] is True
    assert writes(w.fake) == (1, 1) and len(w.fake.replays_of(w.exec_id)) == 1


def test_crash_while_following_the_replay_resumes_without_new_writes(w, monkeypatch):
    incident_id, p = proposed(w)

    def die(*a, **k):
        raise Crash()
    monkeypatch.setattr(w.client, "wait_terminal", die)
    with pytest.raises(Crash):
        w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    state = w.oncall.get_incident(incident_id)["apply_state"]
    assert state["step"] == "replayed" and state["replay_execution_id"]
    monkeypatch.undo()
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "verified" and writes(w.fake) == (1, 1) and len(w.fake.replays_of(w.exec_id)) == 1


def test_lock_contention_returns_busy_and_writes_nothing(w):
    incident_id, p = proposed(w)
    assert w.store.lock(incident_id, "another-process", 60)
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "busy" and r["lock_expires_in_s"] > 0 and writes(w.fake) == (0, 0)
    w.store.unlock(incident_id, "another-process")
    assert w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")["status"] == "verified"


def test_expired_lock_does_not_block(w):
    incident_id, p = proposed(w)
    assert w.store.lock(incident_id, "dead-process", -1)
    assert w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")["status"] == "verified"


# ------------------------------------------------------------------------------------ apply: failure and rollback
def test_failing_fix_is_rolled_back_to_the_base_source(w):
    incident_id, p = proposed(w)
    w.fake.replay_state = "FAILED"
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "rolled_back" and r["replay_state"] == "FAILED"
    assert (r["fixed_revision"], r["rollback_revision"]) == (3, 4)
    flow = w.fake.current(NS, FLOW)
    assert flow["source"] == BASE_SOURCE and flow["revision"] == 4          # same bytes as the base, new revision
    assert writes(w.fake) == (2, 1)
    assert w.oncall.get_incident(incident_id)["status"] == "rolled_back"
    again = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert again["noop"] is True and again["status"] == "rolled_back" and writes(w.fake) == (2, 1)


def test_rollback_happens_only_while_the_flow_is_still_at_the_fixed_revision(w, monkeypatch):
    incident_id, p = proposed(w)
    w.fake.replay_state = "FAILED"
    real = w.client.replay

    def replay_then_concurrent_edit(*a, **k):
        out = real(*a, **k)
        w.fake.put_flow(NS, FLOW, FIXED_SOURCE + "# a colleague kept improving it\n")
        return out
    monkeypatch.setattr(w.client, "replay", replay_then_concurrent_edit)
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "needs_human" and r["current_revision"] == 4 and "NOT rolled back" in r["message"]
    assert writes(w.fake) == (1, 1) and "kept improving" in w.fake.current(NS, FLOW)["source"]


def test_rollback_disabled_leaves_the_fix_and_asks_a_human(tmp_path, fake):
    w = make_world(tmp_path, fake, rollback=False)
    incident_id, p = proposed(w)
    fake.replay_state = "FAILED"
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "needs_human" and "rollback_on_failure is off" in r["message"]
    assert fake.current(NS, FLOW)["source"] == FIXED_SOURCE and writes(fake) == (1, 1)


def test_replay_timeout_rolls_back_and_says_the_replay_may_still_run(tmp_path, fake):
    w = make_world(tmp_path, fake, timeout=1)
    incident_id, p = proposed(w)
    fake.replay_state = "RUNNING"
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "rolled_back" and r["replay_state"] == "TIMEOUT" and "may still be running" in r["note"]
    assert fake.current(NS, FLOW)["source"] == BASE_SOURCE


def test_rollback_write_that_fails_is_reported_not_hidden(w, monkeypatch):
    incident_id, p = proposed(w)
    w.fake.replay_state = "FAILED"
    real = w.client.update_flow
    count = {"n": 0}

    def fail_second(*a, **k):
        count["n"] += 1
        if count["n"] == 2:
            raise KestraError("boom", status=503)
        return real(*a, **k)
    monkeypatch.setattr(w.client, "update_flow", fail_second)
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "needs_human" and "failed fix is still live" in r["message"]
    monkeypatch.undo()
    r2 = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")          # safe retry reconciles
    assert r2["status"] == "rolled_back" and w.fake.current(NS, FLOW)["source"] == BASE_SOURCE
    assert len(w.fake.replays_of(w.exec_id)) == 1                                     # no second replay


def test_replay_that_reran_earlier_tasks_is_not_called_verified(w):
    incident_id, p = proposed(w)
    w.fake.rerun_earlier = True
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "needs_human" and r["verification"]["rerun_task_ids"] == ["extract"]
    assert w.fake.current(NS, FLOW)["source"] == FIXED_SOURCE and writes(w.fake) == (1, 1)    # fix kept, not reverted


def test_verify_replay_unit_matches_by_task_id_not_by_task_run_id():
    def run(task, state, attempts, rid):
        return {"id": rid, "taskId": task, "state": {"current": state},
                "attempts": [{"state": {"startDate": s, "endDate": e, "current": c}} for s, e, c in attempts]}
    original = {"taskRunList": [run("a", "SUCCESS", [("1", "2", "SUCCESS")], "o1"), run("b", "FAILED", [("3", "4", "FAILED")], "o2")]}
    good = {"state": {"current": "SUCCESS"}, "taskRunList": [run("a", "SUCCESS", [("1", "2", "SUCCESS")], "n1"),
            run("b", "SUCCESS", [("3", "4", "FAILED"), ("5", "6", "SUCCESS")], "n2")]}
    assert inc.verify_replay(original, good, "b")["ok"] is True
    reran = {"state": {"current": "SUCCESS"}, "taskRunList": [run("a", "SUCCESS", [("7", "8", "SUCCESS")], "n1"), good["taskRunList"][1]]}
    assert inc.verify_replay(original, reran, "b")["rerun_task_ids"] == ["a"]
    not_recovered = {"state": {"current": "SUCCESS"}, "taskRunList": [good["taskRunList"][0], run("b", "SUCCESS", [("9", "9", "SUCCESS")], "n2")]}
    assert inc.verify_replay(original, not_recovered, "b")["failed_task_recovered"] is False


def test_summary_counts(w):
    incident_id, p = proposed(w)
    w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    s = w.oncall.summary()
    assert s["incidents"] == {"verified": 1} and s["total_incidents"] == 1 and s["namespaces"] == ["hko.demo"]
    assert [r["status"] for r in w.oncall.list_incidents()] == ["verified"]
    assert w.oncall.list_incidents(status="open") == []


def test_every_public_result_is_strictly_json_serialisable(w):
    """Glue serialises with allow_nan=False; a datetime or tuple key would break every tool call."""
    dump = lambda value: json.dumps(value, allow_nan=False)  # noqa: E731
    incident = w.oncall.open_incident(w.exec_id)
    dump(incident)
    proposal = w.oncall.propose_fix(incident["incident_id"], FIXED_SOURCE, "r")
    dump(proposal)
    dump(w.oncall.get_incident(incident["incident_id"]))
    dump(w.oncall.apply_fix(incident["incident_id"], proposal["proposal_sha256"], actor="t"))
    dump(w.oncall.get_incident(incident["incident_id"]))
    dump(w.oncall.list_incidents())
    dump(w.oncall.summary())


def test_failed_replay_started_by_us_does_not_open_a_second_incident(w):
    incident_id, p = proposed(w)
    w.fake.replay_state = "FAILED"
    r = w.oncall.apply_fix(incident_id, p["proposal_sha256"], actor="t")
    assert r["status"] == "rolled_back"
    again = w.oncall.open_incident(r["replay_execution_id"])           # the failure webhook fires for the failed replay
    assert again["incident_id"] == incident_id and again["is_replay_of_incident"] == incident_id
    assert len(w.store.list_incidents()) == 1
    stranger = w.fake.add_execution(NS, FLOW, 2, [("extract", "SUCCESS"), ("transform", "FAILED")])
    stranger["originalId"] = w.exec_id                                  # a replay somebody else started
    assert w.oncall.open_incident(stranger["id"])["incident_id"] != incident_id
