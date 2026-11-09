"""SQLite persistence for incidents, proposals, the step journal, pause decisions and locks.

Accepts either a file path or an already open connection (Hermes `plugin_db("kestra-oncall")`). Every
write runs in its own transaction (`with conn:`), so an interrupted process leaves either the whole row
or nothing. The journal is append-only: it is the audit trail and the resume map for apply_fix.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL UNIQUE,
    namespace TEXT NOT NULL,
    flow_id TEXT NOT NULL,
    status TEXT NOT NULL,
    failed_task_id TEXT,
    failed_task_run_id TEXT,
    execution_revision INTEGER,
    bundle TEXT NOT NULL,
    apply_state TEXT,
    result TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    incident_id TEXT NOT NULL,
    proposal_sha256 TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    base_source_sha256 TEXT NOT NULL,
    base_source TEXT NOT NULL,
    new_source TEXT NOT NULL,
    rationale TEXT NOT NULL,
    diff TEXT NOT NULL,
    diff_stat TEXT NOT NULL,
    validation TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (incident_id, proposal_sha256)
);
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    subject TEXT NOT NULL,
    step TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_subject ON journal (subject, seq);
CREATE TABLE IF NOT EXISTS pause_decisions (
    execution_id TEXT NOT NULL,
    task_run_id TEXT NOT NULL,
    action TEXT NOT NULL,
    inputs TEXT NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (execution_id, task_run_id)
);
CREATE TABLE IF NOT EXISTS locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires REAL NOT NULL
);
"""

KEEP_PROPOSALS = 5   # per incident; the latest is the only one apply accepts, older ones are history

_INCIDENT_FIELDS = {"status", "failed_task_id", "failed_task_run_id", "execution_revision", "apply_state",
                    "result", "bundle"}
_JSON_FIELDS = {"bundle", "apply_state", "result"}


class Store:
    def __init__(self, conn_or_path: sqlite3.Connection | str | Path):
        self._mutex = threading.RLock()
        if isinstance(conn_or_path, sqlite3.Connection):
            self._conn = conn_or_path
            self._owns = False
        else:
            path = Path(conn_or_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._owns = True
        with self._mutex, self._conn:
            for statement in SCHEMA.strip().split(";"):
                if statement.strip():
                    self._conn.execute(statement)

    def close(self) -> None:
        """Close the connection only if this Store opened it. A connection handed in stays the caller's."""
        if self._owns:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:  # last resort so a forgotten Store never leaks its file handle
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ helpers
    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._mutex:
            cursor = self._conn.execute(sql, params)
            names = [d[0] for d in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _decode(row: dict[str, Any] | None, fields: set[str]) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        for name in fields:
            if name in out and isinstance(out[name], str):
                out[name] = json.loads(out[name])
        return out

    # ------------------------------------------------------------------ incidents
    def create_incident(self, *, incident_id: str, execution_id: str, namespace: str, flow_id: str,
                        bundle: dict, failed_task_id: str | None, failed_task_run_id: str | None,
                        execution_revision: int | None) -> tuple[dict, bool]:
        """Insert once. Returns (row, created). A second call with the same id changes nothing."""
        now = time.time()
        with self._mutex, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO incidents (incident_id, execution_id, namespace, flow_id, status, "
                "failed_task_id, failed_task_run_id, execution_revision, bundle, created, updated) "
                "VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
                (incident_id, execution_id, namespace, flow_id, failed_task_id, failed_task_run_id,
                 execution_revision, json.dumps(bundle), now, now))
            created = cursor.rowcount == 1
        row = self.get_incident_row(incident_id)
        assert row is not None
        return row, created

    def get_incident_row(self, incident_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
        return self._decode(row, _JSON_FIELDS)

    def update_incident(self, incident_id: str, **fields: Any) -> None:
        unknown = set(fields) - _INCIDENT_FIELDS
        if unknown:
            raise ValueError(f"Unknown incident fields: {sorted(unknown)}")
        if not fields:
            return
        values = [json.dumps(v) if k in _JSON_FIELDS and v is not None else v for k, v in fields.items()]
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._mutex, self._conn:
            self._conn.execute(f"UPDATE incidents SET {assignments}, updated = ? WHERE incident_id = ?",
                               (*values, time.time(), incident_id))

    def list_incidents(self, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = ("SELECT incident_id, execution_id, namespace, flow_id, status, failed_task_id, created, updated, "
               "(SELECT COUNT(*) FROM proposals p WHERE p.incident_id = i.incident_id) AS proposals "
               "FROM incidents i")
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created DESC, incident_id LIMIT ?"
        return self._all(sql, (*params, max(1, min(int(limit), 200))))

    def find_related(self, *, namespace: str, flow_id: str, failed_task_id: str | None, since: float,
                     statuses: tuple[str, ...], exclude: str) -> dict[str, Any] | None:
        """The newest still-active incident for the same flow and failed task, if one was opened after `since`."""
        marks = ",".join("?" for _ in statuses)
        return self._one(
            "SELECT incident_id, status, created, (SELECT COUNT(*) FROM proposals p WHERE p.incident_id = i.incident_id) "
            f"AS proposals FROM incidents i WHERE namespace = ? AND flow_id = ? AND failed_task_id IS ? AND created > ? "
            f"AND incident_id != ? AND status IN ({marks}) ORDER BY created DESC LIMIT 1",
            (namespace, flow_id, failed_task_id, since, exclude, *statuses))

    def counts_by_status(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) AS n FROM incidents GROUP BY status")}

    # ------------------------------------------------------------------ proposals
    def save_proposal(self, incident_id: str, *, proposal_sha256: str, base_revision: int,
                      base_source_sha256: str, base_source: str, new_source: str, rationale: str, diff: str,
                      diff_stat: dict, validation: list) -> dict[str, Any]:
        with self._mutex, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO proposals (incident_id, proposal_sha256, base_revision, "
                "base_source_sha256, base_source, new_source, rationale, diff, diff_stat, validation, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (incident_id, proposal_sha256, base_revision, base_source_sha256, base_source, new_source,
                 rationale, diff, json.dumps(diff_stat), json.dumps(validation), time.time()))
            self._conn.execute(
                "DELETE FROM proposals WHERE incident_id = ? AND rowid NOT IN "
                "(SELECT rowid FROM proposals WHERE incident_id = ? ORDER BY created DESC, rowid DESC LIMIT ?)",
                (incident_id, incident_id, KEEP_PROPOSALS))
        proposal = self.get_proposal(incident_id, proposal_sha256)
        assert proposal is not None
        return proposal

    def get_proposal(self, incident_id: str, proposal_sha256: str | None = None) -> dict[str, Any] | None:
        """The latest proposal of an incident, or the one with the given hash."""
        if proposal_sha256:
            row = self._one("SELECT * FROM proposals WHERE incident_id = ? AND proposal_sha256 = ?",
                            (incident_id, proposal_sha256))
        else:
            row = self._one("SELECT * FROM proposals WHERE incident_id = ? ORDER BY created DESC, rowid DESC LIMIT 1",
                            (incident_id,))
        return self._decode(row, {"diff_stat", "validation"})

    # ------------------------------------------------------------------ journal
    def journal_add(self, subject: str, step: str, detail: dict | None = None) -> int:
        with self._mutex, self._conn:
            cursor = self._conn.execute("INSERT INTO journal (ts, subject, step, detail) VALUES (?, ?, ?, ?)",
                                        (time.time(), subject, step, json.dumps(detail or {}, default=str)))
            return int(cursor.lastrowid)

    def journal(self, subject: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._all("SELECT seq, ts, subject, step, detail FROM journal WHERE subject = ? "
                         "ORDER BY seq DESC LIMIT ?", (subject, limit))
        rows.reverse()
        return [self._decode(r, {"detail"}) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------ pause decisions
    def pause_get(self, execution_id: str, task_run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM pause_decisions WHERE execution_id = ? AND task_run_id = ?",
                        (execution_id, task_run_id))
        return self._decode(row, {"inputs", "result"})

    def pause_put_intent(self, execution_id: str, task_run_id: str, action: str, inputs: dict,
                         actor: str) -> dict[str, Any]:
        """Record what we are about to do. Replaces only an earlier unfinished intent."""
        now = time.time()
        with self._mutex, self._conn:
            self._conn.execute(
                "INSERT INTO pause_decisions (execution_id, task_run_id, action, inputs, actor, status, created, updated) "
                "VALUES (?, ?, ?, ?, ?, 'intent', ?, ?) "
                "ON CONFLICT(execution_id, task_run_id) DO UPDATE SET action = excluded.action, "
                "inputs = excluded.inputs, actor = excluded.actor, updated = excluded.updated "
                "WHERE pause_decisions.status = 'intent'",
                (execution_id, task_run_id, action, json.dumps(inputs, sort_keys=True), actor, now, now))
        row = self.pause_get(execution_id, task_run_id)
        assert row is not None
        return row

    def pause_finish(self, execution_id: str, task_run_id: str, result: dict) -> dict[str, Any]:
        with self._mutex, self._conn:
            self._conn.execute(
                "UPDATE pause_decisions SET status = 'done', result = ?, updated = ? "
                "WHERE execution_id = ? AND task_run_id = ? AND status = 'intent'",
                (json.dumps(result, default=str), time.time(), execution_id, task_run_id))
        row = self.pause_get(execution_id, task_run_id)
        assert row is not None
        return row

    def count_pause_decisions(self) -> int:
        return int(self._all("SELECT COUNT(*) AS n FROM pause_decisions WHERE status = 'done'")[0]["n"])

    # ------------------------------------------------------------------ locks
    def lock(self, name: str, owner: str, ttl_s: float) -> bool:
        """Compare-and-set: take the lock if it is free, expired, or already ours. One atomic statement."""
        now = time.time()
        with self._mutex, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO locks (name, owner, expires) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, expires = excluded.expires "
                "WHERE locks.expires < ? OR locks.owner = excluded.owner",
                (name, owner, now + ttl_s, now))
            return cursor.rowcount == 1

    def unlock(self, name: str, owner: str) -> bool:
        with self._mutex, self._conn:
            cursor = self._conn.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))
            return cursor.rowcount == 1

    def lock_info(self, name: str) -> dict[str, Any] | None:
        row = self._one("SELECT name, owner, expires FROM locks WHERE name = ?", (name,))
        if row and row["expires"] < time.time():
            return None
        return row
