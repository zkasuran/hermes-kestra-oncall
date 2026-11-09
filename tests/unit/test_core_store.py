"""store: schema, transactions, compare-and-set locks."""

import sqlite3
import threading
import time

import pytest

from kestra_oncall.store import Store


def make_incident(store, incident_id="INC-00000001", execution_id="exec-1"):
    return store.create_incident(incident_id=incident_id, execution_id=execution_id, namespace="hko.demo",
                                 flow_id="orders", bundle={"x": 1}, failed_task_id="t", failed_task_run_id="r",
                                 execution_revision=2)


def test_schema_is_idempotent_and_works_with_path_or_connection(tmp_path):
    path = tmp_path / "a.db"
    Store(path)
    store = Store(path)                                   # second open must not fail
    make_incident(store)
    conn = sqlite3.connect(":memory:", check_same_thread=False)   # what plugin_db hands out
    shared = Store(conn)
    Store(conn)
    assert make_incident(shared)[1] is True
    conn.execute("CREATE TABLE glue_other (x)")           # foreign tables on the same connection coexist
    assert shared.get_incident_row("INC-00000001")["bundle"] == {"x": 1}


def test_incident_insert_is_once_and_updates_validate_fields(tmp_path):
    store = Store(tmp_path / "a.db")
    row, created = make_incident(store)
    again, created2 = make_incident(store)
    assert (created, created2) == (True, False) and again["created"] == row["created"]
    with pytest.raises(ValueError, match="Unknown incident fields"):
        store.update_incident("INC-00000001", namespace="evil")
    store.update_incident("INC-00000001", status="proposed", apply_state={"step": "start"})
    got = store.get_incident_row("INC-00000001")
    assert got["status"] == "proposed" and got["apply_state"] == {"step": "start"}
    assert store.counts_by_status() == {"proposed": 1}
    assert [r["incident_id"] for r in store.list_incidents(status="proposed")] == ["INC-00000001"]
    assert store.list_incidents(status="verified") == []


def test_proposals_latest_and_by_hash(tmp_path):
    store = Store(tmp_path / "a.db")
    make_incident(store)
    common = dict(base_revision=2, base_source_sha256="b", base_source="old", rationale="r", diff="d",
                  diff_stat={"added": 1, "removed": 0}, validation=[])
    store.save_proposal("INC-00000001", proposal_sha256="h1", new_source="n1", **common)
    store.save_proposal("INC-00000001", proposal_sha256="h2", new_source="n2", **common)
    assert store.get_proposal("INC-00000001")["proposal_sha256"] == "h2"
    assert store.get_proposal("INC-00000001", "h1")["new_source"] == "n1"
    assert store.get_proposal("INC-00000001")["diff_stat"] == {"added": 1, "removed": 0}
    assert store.get_proposal("INC-missing") is None


def test_journal_is_ordered_and_scoped(tmp_path):
    store = Store(tmp_path / "a.db")
    for step in ("a", "b", "c"):
        store.journal_add("INC-1", step, {"n": step})
    store.journal_add("INC-2", "other")
    assert [e["step"] for e in store.journal("INC-1")] == ["a", "b", "c"]
    assert store.journal("INC-1")[1]["detail"] == {"n": "b"}


def test_lock_is_compare_and_set_with_ttl(tmp_path):
    store = Store(tmp_path / "a.db")
    assert store.lock("L", "alice", 60) is True
    assert store.lock("L", "bob", 60) is False                  # held
    assert store.unlock("L", "bob") is False                    # not the owner
    assert store.lock("L", "alice", 60) is True                 # owner may refresh
    assert store.unlock("L", "alice") is True
    assert store.lock("L", "bob", 60) is True
    assert store.lock("E", "alice", -1) is True                 # already expired
    assert store.lock("E", "bob", 60) is True                   # expired lock can be taken over
    assert store.lock_info("E")["owner"] == "bob"
    assert store.lock_info("nothing") is None


def test_lock_has_exactly_one_winner_under_threads(tmp_path):
    path = tmp_path / "a.db"
    Store(path)
    winners, barrier = [], threading.Barrier(8)

    def contend(n):
        s = Store(path)                                          # own connection per thread, same file
        barrier.wait()
        if s.lock("race", f"owner-{n}", 30):
            winners.append(n)

    threads = [threading.Thread(target=contend, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1


def test_pause_intent_then_finish_and_done_rows_are_immutable(tmp_path):
    store = Store(tmp_path / "a.db")
    row = store.pause_put_intent("e1", "t1", "resume", {"approved": "true"}, "alice")
    assert row["status"] == "intent" and row["inputs"] == {"approved": "true"}
    store.pause_put_intent("e1", "t1", "kill", {}, "bob")              # an unfinished intent can be replaced
    assert store.pause_get("e1", "t1")["action"] == "kill"
    store.pause_finish("e1", "t1", {"status": "killed"})
    store.pause_put_intent("e1", "t1", "resume", {}, "mallory")        # a finished decision cannot
    done = store.pause_get("e1", "t1")
    assert (done["status"], done["action"], done["actor"], done["result"]) == ("done", "kill", "bob", {"status": "killed"})
    assert store.count_pause_decisions() == 1
