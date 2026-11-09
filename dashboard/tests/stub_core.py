"""A fake core for the dashboard tests: the same public surface as client/store/incidents/pauses.

The dashboard routes only call the documented contract in SPEC.md, so this stub lets the tab be tested
before, and independently of, a running Kestra. ``make_plugin_copy`` copies the real package files next to
the stub modules inside a scratch directory. Nothing in the repository is modified.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DIFF = """--- orders_etl r3
+++ orders_etl proposed
@@ -9,7 +9,7 @@
   - id: transform
     type: io.kestra.plugin.scripts.python.Script
     script: |
-      rows = [r["amount_usd"] for r in data]
+      rows = [r["amount"] for r in data]
"""

CLIENT = '''
class KestraError(Exception):
    status = None
    body = ""


class KestraClient:
    def __init__(self, base_url, tenant="main", *, username=None, password=None, token=None, timeout=20.0):
        self.base_url, self.tenant = base_url, tenant
        self.username, self.password, self.token = username, password, token

    def server_version(self):
        if self.base_url.endswith(":1"):
            raise KestraError("down")
        return "2.0.9"
'''

STORE = f'''
DIFF = {DIFF!r}


class Store:
    def __init__(self, conn_or_path):
        self.conn = conn_or_path

    def get_proposal(self, incident_id):
        if incident_id != "INC-0a1b2c3d":
            return None
        return {{"proposal_sha256": "a" * 64, "base_revision": 3, "diff": DIFF, "diff_stat": "+1 -1",
                "rationale": "The upstream feed renamed amount_usd to amount; read the new column."}}
'''

INCIDENTS = '''
from dataclasses import dataclass, field


class PolicyError(Exception):
    pass


class StaleError(Exception):
    pass


@dataclass
class Policy:
    namespaces: list = field(default_factory=list)
    protected_flows: list = field(default_factory=list)
    rollback_on_failure: bool = True
    replay_timeout_s: int = 300
    max_log_lines: int = 200


ROWS = [
    {"incident_id": "INC-0a1b2c3d", "status": "proposed", "namespace": "hko.demo", "flow_id": "orders_etl",
     "execution_id": "3GzyXexample1", "failed_task": {"task_id": "transform", "task_run_id": "tr1"}, "execution_revision": 3,
     "protected": False, "logs": [{"level": "ERROR", "message": "KeyError: 'amount_usd'"}, {"level": "ERROR", "message": "Task transform failed"}]},
    {"incident_id": "INC-9f8e7d6c", "status": "open", "namespace": "hko.demo", "flow_id": "protected_ledger",
     "execution_id": "3GzyXexample2", "failed_task": "post", "execution_revision": 1, "protected": True,
     "error_logs": ["HTTP 500 from ledger api"]},
    {"incident_id": "INC-11223344", "status": "verified", "namespace": "hko.demo", "flow_id": "nightly_sync",
     "execution_id": "3GzyXexample3", "failed_task": {"task_id": "load"}, "execution_revision": 2, "protected": False,
     "result": {"status": "verified", "message": "Replay succeeded; earlier tasks reused.", "fixed_revision": 3,
                "replay_execution_id": "5RpExample"}},
]


class Oncall:
    def __init__(self, client, store, policy):
        self.client, self.store, self.policy = client, store, policy

    def list_incidents(self, *, status=None, limit=20):
        return [dict(r) for r in ROWS][:limit]

    def get_incident(self, incident_id):
        for row in ROWS:
            if row["incident_id"] == incident_id:
                return dict(row)
        raise PolicyError("Unknown incident " + incident_id)

    def summary(self):
        return {"open": 2, "verified": 1}
'''

PAUSES = '''
class PauseDesk:
    def __init__(self, client, store, policy):
        self.client = client

    def list_pauses(self):
        return [{"execution_id": "3GzyXexample4", "namespace": "hko.demo", "flow_id": "refund_approval",
                 "paused_task_id": "approve", "paused_task_run_id": "4TrRunExample",
                 "description": "Refund of 120.00 EUR for order 5512 needs a human decision.",
                 "paused_for_seconds": 184.6,
                 "resume_inputs": [{"id": "approved", "type": "BOOL", "required": True},
                                   {"id": "note", "type": "STRING", "required": False}]}]
'''

MODULES = {"client.py": CLIENT, "store.py": STORE, "incidents.py": INCIDENTS, "pauses.py": PAUSES}


def make_plugin_copy(dest: Path, *, stub: bool = True) -> Path:
    """Copy the plugin (dashboard, settings, __init__) to ``dest`` and drop the stub core modules in."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "settings.py", "plugin.yaml"):
        shutil.copy2(ROOT / name, dest / name)
    shutil.copytree(ROOT / "dashboard", dest / "dashboard", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))
    if stub:
        for name, source in MODULES.items():
            (dest / name).write_text(source)
    return dest
