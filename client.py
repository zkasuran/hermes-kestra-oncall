"""Small Kestra 2.0 REST client on urllib.

Every endpoint shape here was measured on Kestra 2.0.9 (see .work/core/FACTS.md). Where Kestra's
behaviour differs from what the OpenAPI suggests, the comment says so. Credentials are never put in an
exception message: bodies are trimmed and scrubbed before they leave this module.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

TERMINAL_STATES = {"SUCCESS", "WARNING", "FAILED", "KILLED", "CANCELLED", "SKIPPED"}
_LEVELS = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "ERROR": 4}
_BODY_LIMIT = 400


class KestraError(Exception):
    """A Kestra request failed. `.status` is None for transport failures."""

    def __init__(self, message: str, *, status: int | None = None, body: str = "",
                 method: str = "", path: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body
        self.method = method
        self.path = path


class WaitTimeout(KestraError):
    """wait_terminal ran out of time. `.execution` holds the last execution read."""

    def __init__(self, message: str, execution: dict | None = None):
        super().__init__(message)
        self.execution = execution or {}


def _q(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


class KestraClient:
    def __init__(self, base_url: str, tenant: str = "main", *, username: str | None = None,
                 password: str | None = None, token: str | None = None, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.tenant = tenant
        self.timeout = timeout
        self._secrets = [s for s in (password, token) if s]
        self._auth: str | None = None
        if token:
            self._auth = f"Bearer {token}"
        elif username is not None and password is not None:
            self._auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    # ------------------------------------------------------------------ transport
    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _request(self, method: str, path: str, *, query: list[tuple[str, str]] | None = None,
                 body: bytes | None = None, content_type: str | None = None, tenant: bool = True,
                 accept: str = "application/json") -> Any:
        prefix = f"/api/v1/{_q(self.tenant)}" if tenant else "/api/v1"
        url = f"{self.base_url}{prefix}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": accept}
        if content_type:
            headers["Content-Type"] = content_type
        if self._auth:
            headers["Authorization"] = self._auth
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            with exc:  # close the response body so error paths do not leak a file handle
                text = self._scrub(exc.read().decode("utf-8", "replace"))[:_BODY_LIMIT]
            raise KestraError(f"Kestra {method} {prefix}{path} failed with HTTP {exc.code}: {text}",
                              status=exc.code, body=text, method=method, path=path) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = self._scrub(str(getattr(exc, "reason", exc)))[:200]
            raise KestraError(f"Kestra {method} {prefix}{path} could not be reached: {reason}. "
                              "Check the URL and that Kestra is running.",
                              status=None, method=method, path=path) from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise KestraError(f"Kestra {method} {prefix}{path} returned a non-JSON body.",
                              status=200, body=self._scrub(raw.decode("utf-8", "replace"))[:_BODY_LIMIT],
                              method=method, path=path) from None

    @staticmethod
    def _multipart(fields: dict[str, str] | None) -> tuple[bytes, str]:
        # Kestra answers 415 to urllib's default form-urlencoded content type, even with no fields, so these
        # endpoints always get a (possibly empty) multipart body.
        boundary = "kestraoncall" + uuid.uuid4().hex
        parts = []
        for name, value in (fields or {}).items():
            safe_name = str(name).replace('"', "").replace("\r", "").replace("\n", "")
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{safe_name}"\r\n\r\n{value}\r\n')
        parts.append(f"--{boundary}--\r\n")
        return "".join(parts).encode("utf-8"), f"multipart/form-data; boundary={boundary}"

    # ------------------------------------------------------------------ server
    def server_version(self) -> str:
        return str((self._request("GET", "/configs", tenant=False) or {}).get("version", "unknown"))

    # ------------------------------------------------------------------ executions
    def get_execution(self, execution_id: str) -> dict:
        return self._request("GET", f"/executions/{_q(execution_id)}")

    def search_executions(self, *, namespace: str | None = None, flow_id: str | None = None,
                          state: list[str] | None = None, size: int = 25,
                          sort: str = "state.startDate:desc",
                          labels: dict[str, str] | None = None) -> list[dict]:
        query: list[tuple[str, str]] = [("size", str(size)), ("page", "1"), ("sort", sort)]
        if namespace:
            query.append(("filters[namespace][EQUALS]", namespace))
        if flow_id:
            query.append(("filters[flowId][EQUALS]", flow_id))
        if state:
            query.append(("filters[state][IN]", ",".join(state)))
        for key, value in (labels or {}).items():
            query.append((f"filters[labels][EQUALS][{key}]", value))
        data = self._request("GET", "/executions/search", query=query) or {}
        return list(data.get("results", []))

    def get_logs(self, execution_id: str, *, task_run_id: str | None = None, min_level: str = "WARN",
                 limit: int = 200) -> list[dict]:
        # Kestra 2.0.9 ignores minLevel on this endpoint (FACTS.md section 1), so filter here.
        rows = self._request("GET", f"/logs/{_q(execution_id)}") or []
        floor = _LEVELS.get(min_level.upper(), 3)
        kept = [r for r in rows
                if _LEVELS.get(str(r.get("level", "")).upper(), 0) >= floor
                and (task_run_id is None or r.get("taskRunId") == task_run_id)]
        return kept[-limit:] if limit > 0 else []

    def list_task_outputs(self, execution_id: str) -> list[dict]:
        return self._request("GET", f"/outputs/tasks/{_q(execution_id)}") or []

    def get_task_outputs(self, execution_id: str, task_run_id: str) -> dict:
        return self._request("GET", f"/outputs/tasks/{_q(execution_id)}/{_q(task_run_id)}") or {}

    def delete_execution(self, execution_id: str) -> None:
        self._request("DELETE", f"/executions/{_q(execution_id)}")

    # ------------------------------------------------------------------ flows
    def get_flow(self, namespace: str, flow_id: str, *, revision: int | None = None) -> dict:
        query = [("source", "true")]
        if revision is not None:
            query.append(("revision", str(revision)))
        return self._request("GET", f"/flows/{_q(namespace)}/{_q(flow_id)}", query=query)

    def list_revisions(self, namespace: str, flow_id: str) -> list[dict]:
        return self._request("GET", f"/flows/{_q(namespace)}/{_q(flow_id)}/revisions") or []

    def validate_flow(self, source: str) -> list[dict]:
        rows = self._request("POST", "/flows/validate", body=source.encode("utf-8"),
                             content_type="application/x-yaml") or []
        return [row for row in rows if row.get("constraints")]

    def update_flow(self, namespace: str, flow_id: str, source: str) -> dict:
        # No conditional write exists (FACTS.md section 3): callers must guard with a fresh read.
        return self._request("PUT", f"/flows/{_q(namespace)}/{_q(flow_id)}", body=source.encode("utf-8"),
                             content_type="application/x-yaml")

    def create_flow(self, source: str) -> dict:
        return self._request("POST", "/flows", body=source.encode("utf-8"), content_type="application/x-yaml")

    def delete_flow(self, namespace: str, flow_id: str) -> None:
        self._request("DELETE", f"/flows/{_q(namespace)}/{_q(flow_id)}")

    # ------------------------------------------------------------------ actions
    def replay(self, execution_id: str, *, task_run_id: str | None, revision: int | None) -> dict:
        query = []
        if task_run_id:
            query.append(("taskRunId", task_run_id))
        if revision is not None:
            query.append(("revision", str(revision)))
        return self._request("POST", f"/executions/{_q(execution_id)}/actions/replay", query=query)

    def resume(self, execution_id: str, inputs: dict[str, str] | None = None) -> dict | None:
        body, ctype = self._multipart(inputs)
        return self._request("POST", f"/executions/{_q(execution_id)}/actions/resume", body=body,
                             content_type=ctype)

    def validate_resume(self, execution_id: str, inputs: dict[str, str] | None = None) -> dict:
        body, ctype = self._multipart(inputs)
        return self._request("POST", f"/executions/{_q(execution_id)}/actions/resume/validate",
                             body=body, content_type=ctype) or {}

    def kill(self, execution_id: str) -> None:
        # DELETE, not POST, and this one endpoint only produces text/json: Accept: application/json gets
        # HTTP 406 (FACTS.md section 5).
        self._request("DELETE", f"/executions/{_q(execution_id)}/actions/kill", accept="text/json")

    def trigger(self, namespace: str, flow_id: str, inputs: dict | None = None, *, wait: bool = False) -> dict:
        body, ctype = self._multipart({k: str(v) for k, v in (inputs or {}).items()})
        query = [("wait", "true")] if wait else []
        return self._request("POST", f"/executions/{_q(namespace)}/{_q(flow_id)}", query=query,
                             body=body, content_type=ctype)

    def wait_terminal(self, execution_id: str, *, timeout: float, poll: float = 2.0,
                      leave_states: set[str] | None = None) -> dict:
        """Poll until the execution is terminal (or in `leave_states`). Raises WaitTimeout otherwise."""
        deadline = time.monotonic() + timeout
        stop = TERMINAL_STATES | (leave_states or set())
        while True:
            execution = self.get_execution(execution_id)
            if execution.get("state", {}).get("current") in stop:
                return execution
            if time.monotonic() >= deadline:
                raise WaitTimeout(f"Execution {execution_id} was still "
                                  f"{execution.get('state', {}).get('current')} after {timeout:.0f}s.", execution)
            time.sleep(min(poll, max(0.05, deadline - time.monotonic())))
