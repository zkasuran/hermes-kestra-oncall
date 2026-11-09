"""In-process fake Kestra (http.server on 127.0.0.1) used by the core unit tests.

It mirrors the behaviours measured on the real server (.work/core/FACTS.md): flow PUT has no conditional
check and stores source verbatim; identical source makes no new revision; replay returns a new execution
with remapped task run ids but copied attempts; resume on a non-paused execution is HTTP 500; the logs
endpoint ignores minLevel. Hooks let a test inject races, refusals and failures.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from ruamel.yaml import YAML

USER, PASSWORD = "oncall", "fake-kestra-password-9d41"


def _iso(n: int) -> str:
    return f"2026-01-01T00:{n // 60 % 60:02d}:{n % 60:02d}.000000Z"


def new_id() -> str:
    return uuid.uuid4().hex[:22]


class FakeKestra:
    def __init__(self) -> None:
        self.flows: dict[tuple[str, str], list[dict]] = {}
        self.executions: dict[str, dict] = {}
        self.logs: dict[str, list[dict]] = {}
        self.outputs: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.clock = 100
        self.replay_state = "SUCCESS"        # state the next replay ends in; "RUNNING" never finishes
        self.rerun_earlier = False           # make the replay re-execute earlier tasks (breaks reuse proof)
        self.on_put: Callable[["FakeKestra", str, str, str], None] | None = None  # race injection before write
        self.refuse_put: int | None = None   # HTTP status to refuse PUT with (write has no effect)
        self.swallow_put = False             # answer 200 but do not store (ineffective write)
        self.echo_secret_in_errors = False
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "FakeKestra":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ------------------------------------------------------------------ seeding helpers
    def tick(self) -> str:
        self.clock += 1
        return _iso(self.clock)

    def put_flow(self, namespace: str, flow_id: str, source: str) -> dict:
        revisions = self.flows.setdefault((namespace, flow_id), [])
        if revisions and revisions[-1]["source"] == source:
            return revisions[-1]
        flow = {"id": flow_id, "namespace": namespace, "revision": len(revisions) + 1, "source": source}
        revisions.append(flow)
        return flow

    def _attempt(self, state: str) -> dict:
        start = self.tick()
        end = self.tick()
        return {"state": {"current": state, "startDate": start, "endDate": end, "histories": []}}

    def task_run(self, task_id: str, state: str, *, parent: str | None = None, attempts: list | None = None) -> dict:
        run = {"id": new_id(), "taskId": task_id, "state": {"current": state, "startDate": self.tick(), "histories": []},
               "attempts": attempts if attempts is not None else [self._attempt(state)], "outputs": None}
        if parent:
            run["parentTaskRunId"] = parent
        return run

    def add_execution(self, namespace: str, flow_id: str, revision: int, runs: list[tuple[str, str]],
                      state: str = "FAILED", **extra: Any) -> dict:
        eid = new_id()
        execution = {"id": eid, "namespace": namespace, "flowId": flow_id, "flowRevision": revision,
                     "taskRunList": [self.task_run(t, s) for t, s in runs], "inputs": extra.pop("inputs", None),
                     "labels": [{"key": "system.correlationId", "value": eid}],
                     "state": {"current": state, "startDate": self.tick(), "histories": []}, "originalId": eid, **extra}
        self.executions[eid] = execution
        self.logs[eid] = [
            {"level": "DEBUG", "taskId": runs[0][0], "taskRunId": execution["taskRunList"][0]["id"], "message": "debug noise",
             "timestamp": self.tick()},
            {"level": "ERROR", "taskId": runs[-1][0], "taskRunId": execution["taskRunList"][-1]["id"],
             "message": "Unable to find `nosuch` used in the expression", "timestamp": self.tick()},
        ]
        self.outputs[eid] = {r["id"]: {"value": f"out-{r['taskId']}"} for r in execution["taskRunList"] if r["state"]["current"] == "SUCCESS"}
        return execution

    def current(self, namespace: str, flow_id: str) -> dict:
        return self.flows[(namespace, flow_id)][-1]

    def replays_of(self, original_id: str) -> list[dict]:
        return [e for e in self.executions.values() if e["originalId"] == original_id and e["id"] != original_id]

    def count(self, method: str, fragment: str) -> int:
        return sum(1 for m, p in self.calls if m == method and fragment in p)

    # ------------------------------------------------------------------ behaviour
    def _replay(self, original: dict, task_run_id: str | None, revision: int | None) -> dict:
        eid = new_id()
        runs = []
        mapping: dict[str, str] = {}
        failed_ids = set()
        reached_failed = False
        for run in original["taskRunList"]:
            if run["id"] == task_run_id:
                reached_failed = True
            new_run = json.loads(json.dumps(run))
            new_run["id"] = new_id()                 # Kestra remaps task run ids on replay
            mapping[run["id"]] = new_run["id"]
            if run["id"] == task_run_id:
                final = "SUCCESS" if self.replay_state in {"SUCCESS", "WARNING"} else ("FAILED" if self.replay_state == "FAILED" else "RUNNING")  # PAUSED/RUNNING never finish
                new_run["attempts"].append(self._attempt("SUCCESS" if final == "SUCCESS" else "FAILED"))
                new_run["state"]["current"] = final if final != "RUNNING" else "RUNNING"
                failed_ids.add(new_run["id"])
            elif self.rerun_earlier and run["state"]["current"] == "SUCCESS":
                new_run["attempts"] = [self._attempt("SUCCESS")]
            runs.append(new_run)
        if task_run_id is None or not reached_failed:
            runs = []
        if self.replay_state in {"SUCCESS", "WARNING"} and task_run_id:
            runs.append(self.task_run("after", "SUCCESS"))
        state = self.replay_state if self.replay_state in {"SUCCESS", "WARNING", "FAILED", "KILLED", "RUNNING", "PAUSED"} else "FAILED"
        execution = {"id": eid, "namespace": original["namespace"], "flowId": original["flowId"],
                     "flowRevision": revision if revision is not None else original["flowRevision"],
                     "taskRunList": runs, "inputs": original.get("inputs"),
                     "labels": original["labels"] + [{"key": "system.replay", "value": "true"}],
                     "state": {"current": state, "startDate": original["state"]["startDate"], "histories": []},
                     "originalId": original["id"], "parentId": original["id"]}
        original["labels"] = original["labels"] + [{"key": "system.replayed", "value": "true"}]  # real Kestra does this
        self.executions[eid] = execution
        self.logs[eid] = []
        self.outputs[eid] = {r["id"]: {"value": f"out-{r['taskId']}"} for r in runs if r["state"]["current"] == "SUCCESS"}
        return execution

    def finish_replay(self, replay_id: str, state: str) -> None:
        """Let a replay that was left RUNNING end later, as a slow real replay would."""
        ex = self.executions[replay_id]
        ex["state"]["current"] = state
        for run in ex["taskRunList"]:
            if run["state"]["current"] == "RUNNING":
                run["state"]["current"] = "SUCCESS" if state == "SUCCESS" else "FAILED"
                run["attempts"][-1]["state"]["current"] = run["state"]["current"]
        if state == "SUCCESS":
            ex["taskRunList"].append(self.task_run("after", "SUCCESS"))
            self.outputs[replay_id] = {r["id"]: {"value": f"out-{r['taskId']}"} for r in ex["taskRunList"]
                                       if r["state"]["current"] == "SUCCESS"}

    def validate(self, source: str) -> list[dict]:
        if "INVALID_TYPE" in source:
            return [{"index": 0, "constraints": "Validation error: Invalid type: io.kestra.plugin.core.nope.Nope"}]
        try:
            data = YAML(typ="safe").load(source)
        except Exception as exc:  # noqa: BLE001
            return [{"index": 0, "constraints": f"Validation error: Illegal Flow source: {str(exc)[:80]}"}]
        if not isinstance(data, dict) or not data.get("tasks"):
            return [{"index": 0, "constraints": "Validation error: tasks: must not be empty\n"}]
        return [{"index": 0, "namespace": data.get("namespace"), "flow": data.get("id"), "outdated": False}]

    # ------------------------------------------------------------------ http
    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _send(self, status: int, payload: Any = None) -> None:
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth_ok(self) -> bool:
                import base64
                return self.headers.get("Authorization") == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                fake.calls.append((method, parsed.path + ("?" + parsed.query if parsed.query else "")))
                if not self._auth_ok():
                    return self._send(401, {"title": "Authentication required"})
                query = parse_qs(parsed.query, keep_blank_values=True)
                path = unquote(parsed.path)
                if fake.echo_secret_in_errors and path.endswith("/boom"):
                    return self._send(500, {"detail": f"upstream said password={PASSWORD} authorization=Basic"})
                try:
                    status, payload = fake.route(method, path, query, body, self.headers)
                except KeyError:
                    status, payload = 404, {"title": "Resource not found", "status": 404}
                self._send(status, payload)

            def do_GET(self) -> None: self._dispatch("GET")        # noqa: E704
            def do_POST(self) -> None: self._dispatch("POST")      # noqa: E704
            def do_PUT(self) -> None: self._dispatch("PUT")        # noqa: E704
            def do_DELETE(self) -> None: self._dispatch("DELETE")  # noqa: E704

        return Handler

    def route(self, method: str, path: str, query: dict, body: str, headers: Any) -> tuple[int, Any]:
        p = path.removeprefix("/api/v1")
        if p == "/configs":
            return 200, {"version": "2.0.9"}
        p = p.removeprefix("/main")
        if method == "GET" and p == "/executions/search":
            return 200, self._search(query)
        m = re.fullmatch(r"/executions/([^/]+)", p)
        if m and method == "GET":
            return 200, self.executions[m.group(1)]
        if m and method == "DELETE":
            self.executions.pop(m.group(1), None)
            return 204, None
        m = re.fullmatch(r"/logs/([^/]+)", p)
        if m and method == "GET":
            return 200, self.logs[m.group(1)]  # minLevel ignored, like Kestra 2.0.9
        m = re.fullmatch(r"/outputs/tasks/([^/]+)", p)
        if m and method == "GET":
            ex = self.executions[m.group(1)]
            return 200, [{"taskId": r["taskId"], "taskRunId": r["id"], "inline": True}
                         for r in ex["taskRunList"] if r["id"] in self.outputs[ex["id"]]]
        m = re.fullmatch(r"/outputs/tasks/([^/]+)/([^/]+)", p)
        if m and method == "GET":
            return 200, self.outputs[m.group(1)][m.group(2)]
        if method == "POST" and p == "/flows/validate":
            return 200, self.validate(body)
        m = re.fullmatch(r"/flows/([^/]+)/([^/]+)/revisions", p)
        if m and method == "GET":
            return 200, self.flows[(m.group(1), m.group(2))]
        m = re.fullmatch(r"/flows/([^/]+)/([^/]+)", p)
        if m:
            key = (m.group(1), m.group(2))
            if method == "GET":
                revisions = self.flows[key]
                wanted = int(query["revision"][0]) if "revision" in query else None
                flow = next((f for f in revisions if f["revision"] == wanted), None) if wanted else revisions[-1]
                if flow is None:
                    return 404, {"detail": f"Unable to find revision {wanted}"}
                return 200, dict(flow) if "source" in query else {k: v for k, v in flow.items() if k != "source"}
            if method == "PUT":
                if self.on_put:
                    self.on_put(self, key[0], key[1], body)
                if self.refuse_put:
                    return self.refuse_put, {"detail": "refused by test"}
                if self.swallow_put:
                    return 200, dict(self.flows[key][-1])
                return 200, dict(self.put_flow(key[0], key[1], body))
            if method == "DELETE":
                self.flows.pop(key, None)
                return 204, None
        m = re.fullmatch(r"/executions/([^/]+)/actions/replay", p)
        if m and method == "POST":
            original = self.executions[m.group(1)]
            rev = int(query["revision"][0]) if "revision" in query else None
            if rev is not None and not any(f["revision"] == rev for f in self.flows[(original["namespace"], original["flowId"])]):
                return 404, {"detail": f"Unable to find revision {rev}"}
            return 200, self._replay(original, (query.get("taskRunId") or [None])[0], rev)
        m = re.fullmatch(r"/executions/([^/]+)/actions/(resume|resume/validate)", p)
        if m and method == "POST":
            return self._resume(self.executions[m.group(1)], m.group(2), body)
        m = re.fullmatch(r"/executions/([^/]+)/actions/kill", p)
        if m and method == "DELETE":
            ex = self.executions[m.group(1)]
            ex["state"]["current"] = "KILLED"
            for r in ex["taskRunList"]:
                if r["state"]["current"] == "PAUSED":
                    r["state"]["current"] = "KILLED"
            return 200, ex
        return 404, {"detail": f"no route {method} {p}"}

    def _search(self, query: dict) -> dict:
        def one(name: str) -> str | None:
            return (query.get(name) or [None])[0]

        results = []
        for ex in self.executions.values():
            if one("filters[namespace][EQUALS]") and ex["namespace"] != one("filters[namespace][EQUALS]"):
                continue
            if one("filters[flowId][EQUALS]") and ex["flowId"] != one("filters[flowId][EQUALS]"):
                continue
            prefix = one("filters[namespace][PREFIX]")
            if prefix and not (ex["namespace"] == prefix or ex["namespace"].startswith(prefix + ".")):
                continue
            states = one("filters[state][IN]")
            if states and ex["state"]["current"] not in states.split(","):
                continue
            ok = True
            for key, values in query.items():
                m = re.fullmatch(r"filters\[labels\]\[EQUALS\]\[(.+)\]", key)
                if m and {"key": m.group(1), "value": values[0]} not in ex["labels"]:
                    ok = False
            if ok:
                results.append({k: ex.get(k) for k in ("id", "namespace", "flowId", "flowRevision", "state", "labels", "originalId")})
        results.sort(key=lambda r: r["state"]["startDate"], reverse=True)
        size, page = int(one("size") or 10), int(one("page") or 1)
        return {"results": results[(page - 1) * size:page * size], "total": len(results)}

    def _resume(self, ex: dict, action: str, body: str) -> tuple[int, Any]:
        if ex["state"]["current"] != "PAUSED":
            return 500, {"title": "Internal server error", "status": 500}   # real Kestra 2.0.9 behaviour
        fields = dict(re.findall(r'name="([^"]+)"\r\n\r\n(.*?)\r\n--', body, flags=re.S))
        if action == "resume/validate":
            inputs = []
            for name, value in fields.items():
                errs = [{"message": f"Invalid value for input `{name}`"}] if name == "approved" and value not in {"true", "false"} else []
                inputs.append({"input": {"id": name}, "value": value, "errors": errs})
            return 200, {"id": ex["flowId"], "inputs": inputs, "checks": []}
        self.resumed_with = fields
        for r in ex["taskRunList"]:
            if r["state"]["current"] == "PAUSED":
                r["state"]["current"] = "SUCCESS"
        ex["state"]["current"] = "SUCCESS"
        return 200, ex


# ---------------------------------------------------------------------------------------- shared world
NS, FLOW = "hko.demo", "orders"
GOOD_SOURCE = f"""id: {FLOW}
namespace: {NS}
tasks:
  - id: extract
    type: io.kestra.plugin.core.debug.Return
    format: ok
  - id: transform
    type: io.kestra.plugin.core.log.Log
    message: "{{{{ outputs.extract.value }}}}"
"""
BASE_SOURCE = GOOD_SOURCE.replace("outputs.extract.value", "outputs.nosuch.value")      # revision 2, failing
FIXED_SOURCE = GOOD_SOURCE.replace("outputs.extract.value", "outputs.extract.value | upper")
PAUSE_SOURCE = """id: refunds
namespace: hko.demo
inputs:
  - id: amount
    type: INT
tasks:
  - id: prep
    type: io.kestra.plugin.core.debug.Return
    format: ready
  - id: approve
    type: io.kestra.plugin.core.flow.Pause
    description: "Approve a refund of {{ inputs.amount }} EUR?"
    onResume:
      - id: approved
        type: BOOL
        defaults: false
      - id: reason
        type: STRING
        required: false
"""


class Crash(BaseException):
    """Simulates the process dying mid-step. BaseException so no handler in the engine swallows it."""


def make_world(tmp_path, kestra: FakeKestra, *, namespaces=("hko.demo",), protected=(), rollback=True, timeout=5):
    from types import SimpleNamespace

    from kestra_oncall.client import KestraClient
    from kestra_oncall.incidents import Oncall, Policy
    from kestra_oncall.pauses import PauseDesk
    from kestra_oncall.store import Store

    kestra.put_flow(NS, FLOW, GOOD_SOURCE)                       # revision 1
    ok_run = kestra.add_execution(NS, FLOW, 1, [("extract", "SUCCESS"), ("transform", "SUCCESS")], state="SUCCESS")
    kestra.put_flow(NS, FLOW, BASE_SOURCE)                       # revision 2 breaks it
    failed = kestra.add_execution(NS, FLOW, 2, [("extract", "SUCCESS"), ("transform", "FAILED")])
    client = KestraClient(kestra.url, "main", username=USER, password=PASSWORD, timeout=5)
    store = Store(tmp_path / "oncall.db")
    policy = Policy(namespaces=list(namespaces), protected_flows=list(protected), rollback_on_failure=rollback,
                    replay_timeout_s=timeout)
    return SimpleNamespace(fake=kestra, client=client, store=store, policy=policy, oncall=Oncall(client, store, policy),
                           desk=PauseDesk(client, store, policy), failed=failed, ok_run=ok_run,
                           exec_id=failed["id"])
