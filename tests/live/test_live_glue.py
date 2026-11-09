"""Glue through the REAL Hermes plugin loader against a live Kestra 2.0.x. Skipped unless HKO_LIVE=1.

    HKO_LIVE=1 /home/asuran/hko-rig/hermes-agent/.venv/bin/python -m pytest tests/live/test_live_glue.py -q

A throwaway HERMES_HOME under pytest's tmp_path gets the plugin (symlink), settings and credentials; the
rig home and ~/.hermes are never touched. A child process (glue_live_driver.py) loads the plugin with
hermes_cli.plugins.discover_plugins and calls tools via model_tools.handle_function_call. Everything is
created in namespace hko.gluetest with a per-run suffix and deleted afterwards. Credentials come from the
same env file as the core live tests and are passed to the child through its environment only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from test_live_core import ENV_FILE, _credentials  # noqa: F401  (same credential loader)

from kestra_oncall.client import KestraClient, KestraError

pytestmark = pytest.mark.skipif(os.environ.get("HKO_LIVE") != "1", reason="set HKO_LIVE=1 to run against Kestra")

NS = "hko.gluetest"
ROOT = Path(__file__).resolve().parents[2]
DRIVER = Path(__file__).with_name("glue_live_driver.py")


def failing_source(flow_id: str, message: str) -> str:
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


def pause_source(flow_id: str) -> str:
    return f"""id: {flow_id}
namespace: {NS}
tasks:
  - id: prep
    type: io.kestra.plugin.core.debug.Return
    format: ready
  - id: approve
    type: io.kestra.plugin.core.flow.Pause
    description: "Approve the glue live refund?"
    onResume:
      - id: approved
        type: BOOL
        defaults: false
      - id: reason
        type: STRING
        required: false
  - id: after
    type: io.kestra.plugin.core.debug.Return
    format: "{{{{ outputs.approve.onResume.approved }}}}"
"""


@pytest.fixture(scope="module")
def creds():
    return _credentials()


@pytest.fixture(scope="module")
def client(creds):
    return KestraClient(creds["KESTRA_URL"], creds.get("KESTRA_TENANT", "main"), username=creds["KESTRA_USERNAME"],
                        password=creds["KESTRA_PASSWORD"], timeout=30)


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


def wait_state(client, execution_id, states, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        execution = client.get_execution(execution_id)
        if execution["state"]["current"] in states:
            return execution
        time.sleep(1)
    raise AssertionError(f"{execution_id} did not reach {states}")


def test_plugin_loaded_by_hermes_runs_open_propose_approved_apply_and_pause(tmp_path, client, creds, cleanup):
    suffix = uuid.uuid4().hex[:8]
    flow_id, pause_id = f"glue_orders_{suffix}", f"glue_refund_{suffix}"
    cleanup.extend([flow_id, pause_id])
    bad = failing_source(flow_id, "{{ outputs.nosuch.value }}")
    fixed = failing_source(flow_id, "{{ outputs.one.value }}")
    client.create_flow(failing_source(flow_id, "{{ outputs.one.value }}"))
    client.update_flow(NS, flow_id, bad)
    base_revision = client.get_flow(NS, flow_id)["revision"]
    failed = client.trigger(NS, flow_id, wait=False)
    failed = wait_state(client, failed["id"], {"FAILED"})
    client.create_flow(pause_source(pause_id))
    paused = wait_state(client, client.trigger(NS, pause_id, wait=False)["id"], {"PAUSED"})
    pause_run = next(r["id"] for r in paused["taskRunList"] if r["state"]["current"] == "PAUSED")

    # a throwaway profile with the plugin symlinked in, enabled, configured; credentials only in the child env
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "kestra-oncall").symlink_to(ROOT, target_is_directory=True)
    (home / "config.yaml").write_text(json.dumps({
        "plugins": {"enabled": ["kestra-oncall"], "entries": {"kestra-oncall": {"settings": {
            "kestra_url": creds["KESTRA_URL"], "tenant": creds.get("KESTRA_TENANT", "main"),
            "namespaces": [NS], "protected_flows": [], "replay_timeout_seconds": 120}}}}}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KESTRA_", "HERMES_"))}
    env.update(HERMES_HOME=str(home), KESTRA_USERNAME=creds["KESTRA_USERNAME"], KESTRA_PASSWORD=creds["KESTRA_PASSWORD"],
               PYTHONDONTWRITEBYTECODE="1")
    config = {"failed_execution": failed["id"], "fixed_source": fixed, "pause_execution": paused["id"],
              "pause_task_run": pause_run, "answers": ["deny", "once", "once", "once"]}
    proc = subprocess.run([sys.executable, str(DRIVER), json.dumps(config)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert creds["KESTRA_PASSWORD"] not in proc.stdout + proc.stderr
    out = json.loads(next(line for line in reversed(proc.stdout.splitlines()) if line.startswith("RESULT "))[7:])

    if os.environ.get("HKO_GLUE_DUMP"):   # optional: keep the driver's JSON as evidence (secrets are redacted by the plugin)
        Path(os.environ["HKO_GLUE_DUMP"]).write_text(json.dumps(out, indent=2, sort_keys=True))

    # 1. the real loader registered everything the manifest declares
    (plugin,) = [p for p in out["plugins"] if p["name"] == "kestra-oncall"]
    assert plugin["enabled"] is True and plugin["error"] is None
    assert plugin["tools"] == 7 and plugin["hooks"] == 1 and plugin["commands"] >= 1
    assert out["status"]["connected"] is True and out["status"]["version"].startswith("2.")

    # 2. open and propose are read-only; the proposal binds the live base revision
    assert out["open"]["failed_task_id"] == "two" and out["open"]["status"] == "open"
    assert out["propose"]["base_revision"] == base_revision and "outputs.one.value" in out["propose"]["diff"]

    # 3. forged hash and the denied prompt write nothing; the approved one recovers the execution
    assert "error" in out["apply_forged"]
    assert "Not approved" in json.dumps(out["apply_denied"]) or "error" in out["apply_denied"]
    assert out["apply"]["status"] == "verified", out["apply"]
    fixed_revision = out["apply"]["fixed_revision"]
    assert fixed_revision == base_revision + 1
    assert client.get_flow(NS, flow_id, revision=fixed_revision)["source"].strip() == fixed.strip()
    replay = client.get_execution(out["apply"]["replay_execution_id"])
    assert replay["state"]["current"] == "SUCCESS" and replay["originalId"] == failed["id"]
    assert replay["flowRevision"] == fixed_revision
    two = next(r for r in replay["taskRunList"] if r["taskId"] == "two")
    assert two["state"]["current"] == "SUCCESS" and [a["state"]["current"] for a in two["attempts"]] == ["FAILED", "SUCCESS"]
    assert client.get_execution(failed["id"])["state"]["current"] == "FAILED"            # audit trail untouched
    assert out["apply_again"].get("noop") is True
    replays = [e for e in client.search_executions(namespace=NS, flow_id=flow_id, size=50) if e.get("originalId") == failed["id"] and e["id"] != failed["id"]]
    assert len(replays) == 1 and len(client.list_revisions(NS, flow_id)) == fixed_revision

    # 4. the approval prompts showed the stored diff and the exact hash, never model text
    sha = out["propose"]["proposal_sha256"]
    apply_prompts = [p for p in out["prompts"] if "Proposal SHA-256" in p]
    assert len(apply_prompts) == 3 and all(sha in p and f"Base revision: {base_revision}" in p and "outputs.one.value" in p for p in apply_prompts)

    # 5. pause: wrong task run is refused, the approved decision resumes with the declared inputs
    assert "error" in out["pause_unknown_task_run"]
    assert [p["execution_id"] for p in out["pauses"]["pauses"]] == [paused["id"]]
    assert out["pause_decide"]["status"] == "resumed", out["pause_decide"]
    resumed = wait_state(client, paused["id"], {"SUCCESS"})
    assert resumed["state"]["current"] == "SUCCESS" and [r["state"]["current"] for r in resumed["taskRunList"]] == ["SUCCESS"] * 3

    # 6. the slash command reads the same records
    assert "connected" in out["cmd_status"] and out["open"]["incident_id"] in out["cmd_incidents"]
    assert sha in out["cmd_show"] and "verified" in out["cmd_show"]
    # 7. the typed command is the approval: a wrong hash is refused, the printed hash applies (core no-op here),
    #    and no Hermes card was raised for either
    assert "does not match" in out["cmd_apply_wrong"]
    assert "verified" in out["cmd_apply"] and "no-op" in out["cmd_apply"]
    assert len([p for p in out["prompts"] if "Proposal SHA-256" in p]) == 3
    assert len(client.list_revisions(NS, flow_id)) == fixed_revision
