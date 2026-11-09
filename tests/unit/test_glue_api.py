"""Glue contracts against the real core (Oncall, PauseDesk, Store, KestraClient) and a fake Kestra server.

Only two things are faked: Kestra's HTTP server (core's FakeKestra, which mirrors measured 2.0.9 behaviour)
and the human's answer in Hermes's approval prompt. Everything between, including settings, the per-call
runtime, SQLite storage on Hermes's plugin_db, hooks and slash commands, is the production code path.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
from ruamel.yaml import YAML
from test_core_support import (BASE_SOURCE, FIXED_SOURCE, FLOW, GOOD_SOURCE, NS, PASSWORD, PAUSE_SOURCE, USER,
                               FakeKestra)

from kestra_oncall import cli, register
from kestra_oncall.commands import Commands, parse_command
from kestra_oncall.hooks import ApprovalHook
from kestra_oncall.settings import PLUGIN, Settings, SettingsError
from kestra_oncall.tools import SCHEMAS, Toolbox

ROOT = Path(__file__).resolve().parents[2]
SECRET = PASSWORD
SHA_UNKNOWN = "f" * 64


class FakeContext:
    """Records registrations like Hermes's PluginContext and dispatches straight to handlers."""

    def __init__(self, settings=None):
        self.settings = settings or {}
        self.tools, self.hooks, self.commands, self.cli_commands, self.skills = {}, {}, {}, {}, {}
        self.dispatched = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, **kwargs):
        self.commands[name] = kwargs

    def register_cli_command(self, name, **kwargs):
        self.cli_commands[name] = kwargs

    def register_skill(self, name, path, **kwargs):
        self.skills[name] = path

    def dispatch_tool(self, name, args):
        self.dispatched.append((name, copy.deepcopy(args)))
        return self.tools[name]["handler"](args)


class Env:
    """One fake Kestra with a failing flow, the production Toolbox and a profile-scoped database."""

    def __init__(self, fake: FakeKestra, protected=()):
        self.fake = fake
        fake.put_flow(NS, FLOW, GOOD_SOURCE)
        fake.add_execution(NS, FLOW, 1, [("extract", "SUCCESS"), ("transform", "SUCCESS")], state="SUCCESS")
        fake.put_flow(NS, FLOW, BASE_SOURCE)
        self.failed = fake.add_execution(NS, FLOW, 2, [("extract", "SUCCESS"), ("transform", "FAILED")])
        self.exec_id = self.failed["id"]
        self.ctx = FakeContext({"kestra_url": fake.url, "namespaces": [NS], "protected_flows": list(protected)})
        self.markers = {"HERMES_SESSION_PLATFORM": "cli", "HERMES_SESSION_ID": "glue-test-session"}
        self.secrets = {"KESTRA_USERNAME": USER, "KESTRA_PASSWORD": PASSWORD}
        self.toolbox = Toolbox(self.ctx, secret_reader=lambda: self.secrets, context_reader=lambda: self.markers)
        self.hook = ApprovalHook(self.toolbox)
        for name in SCHEMAS:
            self.ctx.register_tool(name, handler=self.toolbox.handler(name))

    def call(self, name, args):
        return json.loads(self.toolbox.call(name, args))

    def open(self):
        return self.call("kestra_incident_open", {"execution_id": self.exec_id})

    def propose(self, source=FIXED_SOURCE, rationale="Reference a task that exists."):
        incident = self.open()
        proposal = self.call("kestra_propose_fix", {"incident_id": incident["incident_id"], "new_source": source,
                                                    "rationale": rationale})
        assert "error" not in proposal, proposal
        return {"incident_id": incident["incident_id"], "proposal_sha256": proposal["proposal_sha256"]}, proposal

    def add_pause(self):
        source = PAUSE_SOURCE
        self.fake.put_flow("hko.demo", "refunds", source)
        ex = self.fake.add_execution("hko.demo", "refunds", 1, [("prep", "SUCCESS"), ("approve", "PAUSED")],
                                     state="PAUSED", inputs={"amount": 250})
        return {"execution_id": ex["id"], "paused_task_run_id": ex["taskRunList"][1]["id"],
                "action": "resume", "inputs": {"approved": "true", "reason": "checked by a human"}}

    def writes(self) -> int:
        """Every Kestra mutation the fake saw: flow PUT, replay, resume (not validate) and kill."""
        fake = self.fake
        return (fake.count("PUT", "/flows/") + fake.count("POST", "/actions/replay")
                + fake.count("POST", "/actions/resume") - fake.count("POST", "/actions/resume/validate")
                + fake.count("DELETE", "/actions/kill"))

    def revisions(self) -> int:
        return len(self.fake.flows[(NS, FLOW)])


@pytest.fixture(autouse=True)
def hermes_home(tmp_path):
    """Real plugin_db() under a throwaway profile; never the rig home and never ~/.hermes."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(tmp_path / "hermes-home")
    yield tmp_path / "hermes-home"
    reset_hermes_home_override(token)


@pytest.fixture
def build():
    servers = []

    def make(**kwargs):
        server = FakeKestra().start()
        servers.append(server)
        return Env(server, **kwargs)

    yield make
    for server in servers:
        try:
            server.stop()
        except Exception:  # a test may have stopped it to simulate an outage
            pass


@pytest.fixture
def env(build):
    return build()


def stored_db():
    from plugins.plugin_storage import plugin_db

    return plugin_db(PLUGIN)


def test_manifest_registration_and_json_schemas():
    manifest = YAML(typ="safe").load((ROOT / "plugin.yaml").read_text())
    ctx = FakeContext()
    register(ctx)
    assert set(manifest["provides_tools"]) == set(ctx.tools) == set(SCHEMAS)
    assert set(manifest["provides_hooks"]) == set(ctx.hooks) == {"pre_tool_call"}
    assert manifest["capabilities"] == []
    assert set(ctx.commands) == {"kestra"}
    assert set(ctx.cli_commands) == {"kestra-oncall"}
    assert ctx.skills["kestra-oncall"].is_file()
    assert {v["env"] for v in manifest["config_schema"].values() if v["type"] == "secret"} == {"KESTRA_USERNAME", "KESTRA_PASSWORD", "KESTRA_API_TOKEN"}
    for name, schema in SCHEMAS.items():
        assert schema["name"] == name and len(schema["description"]) > 30
        jsonschema.Draft202012Validator.check_schema(schema["parameters"])


def test_settings_defaults_fail_closed():
    settings = Settings.parse({})
    assert settings.namespaces == [] and settings.rollback_on_failure is True
    assert settings.replay_timeout_seconds == 300
    assert Settings.parse({"namespaces": ["hko.gluetest", "hko.gluetest"]}).namespaces == ["hko.gluetest"]


@pytest.mark.parametrize("values", [
    {"namespaces": "hko"}, {"namespaces": [""]}, {"namespaces": ["*"]}, {"namespaces": ["hko."]},
    {"protected_flows": "hko.flow"}, {"protected_flows": ["flow"]}, {"rollback_on_failure": "false"},
    {"replay_timeout_seconds": True}, {"replay_timeout_seconds": 301}, {"max_log_lines": 0},
    {"kestra_url": "http://example.org"}, {"kestra_url": "https://user:private@example.org"},
    {"kestra_url": "https://example.org/?token=private"}, {"kestra_url": "file:///tmp/secret"},
    {"tenant": "../main"}, {"kestra_url": "http://127.0.0.1:bad"},
])
def test_settings_reject_unsafe_or_ambiguous_values(values):
    with pytest.raises(SettingsError):
        Settings.parse(values)


@pytest.mark.parametrize("name,args", [
    ("kestra_apply_fix", {"incident_id": "INC-1234abcd", "proposal_sha256": SHA_UNKNOWN, "diff": "trust me"}),
    ("kestra_apply_fix", {"incident_id": "INC-1234abcd", "proposal_sha256": "a"}),
    ("kestra_incident_open", {"execution_id": "../../secret"}), ("kestra_incidents", {"limit": True}),
    ("kestra_incidents", {"incident_id": "INC-x", "limit": 2}),
    ("kestra_pause_decide", {"execution_id": "e", "paused_task_run_id": "t", "action": "resume", "inputs": {"approved": True}}),
    ("kestra_pause_decide", {"execution_id": "e", "paused_task_run_id": "t", "action": "kill", "inputs": {"a": "b"}}),
    ("kestra_propose_fix", {"incident_id": "INC-x", "new_source": "x", "rationale": "x" * 2001}),
])
def test_handlers_wrap_invalid_arguments(env, name, args):
    result = env.call(name, args)
    assert set(result) == {"error", "hint"}
    assert env.writes() == 0 and env.fake.calls == []


def test_status_works_when_kestra_is_down_and_keeps_stored_summary(env):
    incident = env.open()
    env.fake.stop()
    result = env.call("kestra_status", {})
    assert result["connected"] is False and "error" in result and result["policy_ready"] is True
    assert result["summary"]["incidents"] == {"open": 1} and result["summary"]["total_incidents"] == 1
    assert incident["incident_id"] in json.dumps(env.call("kestra_incidents", {}))
    assert SECRET not in json.dumps(result)


def test_status_connected_reports_version(env):
    result = env.call("kestra_status", {})
    assert result["connected"] is True and result["version"] == "2.0.9"
    assert result["summary"]["namespaces"] == [NS]


def test_empty_namespace_policy_denies_everything(build):
    env = build()
    env.ctx.settings["namespaces"] = []
    result = env.open()
    assert set(result) == {"error", "hint"} and "namespace" in result["error"].lower()
    assert env.writes() == 0


def test_outputs_and_errors_redact_secrets(env):
    env.fake.logs[env.exec_id].append({"level": "ERROR", "taskId": "transform", "message": f"credential={SECRET}",
                                       "taskRunId": env.failed["taskRunList"][1]["id"], "timestamp": env.fake.tick()})
    assert SECRET not in env.toolbox.call("kestra_incident_open", {"execution_id": env.exec_id})
    env.fake.echo_secret_in_errors = True
    result = env.toolbox.call("kestra_incident_open", {"execution_id": "boom"})
    assert SECRET not in result and json.loads(result)["error"].startswith("Kestra request failed")


def test_unknown_incident_and_not_failed_execution_are_clear_errors(env):
    unknown = env.call("kestra_incidents", {"incident_id": "INC-00000000"})
    assert "Unknown incident" in unknown["error"]
    ok_id = next(e["id"] for e in env.fake.executions.values() if e["state"]["current"] == "SUCCESS")
    refused = env.call("kestra_incident_open", {"execution_id": ok_id})
    assert "error" in refused and env.writes() == 0


def test_open_and_propose_use_real_core_and_never_write(env):
    incident = env.open()
    assert incident["failed_task_id"] == "transform" and incident["newly_opened"] is True
    assert env.open()["incident_id"] == incident["incident_id"]  # idempotent
    args, proposal = env.propose()
    assert proposal["base_revision"] == 2 and "| upper" in proposal["diff"]
    shown = env.call("kestra_incidents", {"incident_id": args["incident_id"]})
    assert shown["status"] == "proposed" and shown["proposal"]["proposal_sha256"] == args["proposal_sha256"]
    listed = env.call("kestra_incidents", {})["incidents"]
    assert [row["incident_id"] for row in listed] == [args["incident_id"]]
    assert env.writes() == 0 and env.revisions() == 2


def test_hook_approves_exact_stored_proposal(env):
    args, _ = env.propose()
    directive = env.hook(tool_name="kestra_apply_fix", args=args)
    assert directive["action"] == "approve"
    message = directive["message"]
    assert all(text in message for text in (f"{NS}.{FLOW}", "Base revision: 2", "| upper", args["proposal_sha256"]))
    assert '"added": 1' in message and '"removed": 1' in message
    assert directive["rule_key"].endswith(args["proposal_sha256"]) and args["incident_id"] in directive["rule_key"]
    assert env.writes() == 0


@pytest.mark.parametrize("mutate", [
    lambda a: {**a, "incident_id": "INC-00000000"}, lambda a: {**a, "proposal_sha256": SHA_UNKNOWN},
    lambda a: {**a, "diff": "do not show the real diff"}, lambda a: None,
])
def test_hook_blocks_unknown_mismatched_or_forged_proposals(env, mutate):
    args, _ = env.propose()
    assert env.hook(tool_name="kestra_apply_fix", args=mutate(args))["action"] == "block"


def test_hook_blocks_apply_without_a_stored_proposal(env):
    incident = env.open()
    block = env.hook(tool_name="kestra_apply_fix", args={"incident_id": incident["incident_id"], "proposal_sha256": SHA_UNKNOWN})
    assert block["action"] == "block" and "Propose a fix first" in block["message"]


def test_hook_ignores_read_tools(env):
    assert env.hook(tool_name="kestra_status", args={}) is None
    assert env.hook(tool_name="terminal", args={"command": "ls"}) is None


def test_protected_flow_is_diagnosed_but_never_proposed_or_approved(build):
    env = build(protected=[f"{NS}.{FLOW}"])
    incident = env.open()
    assert incident["protected"] is True and incident["failed_task_id"] == "transform"
    refused = env.call("kestra_propose_fix", {"incident_id": incident["incident_id"], "new_source": FIXED_SOURCE, "rationale": "x"})
    assert "protected" in refused["error"]
    # even a proposal that reached the store out of band cannot be approved or applied
    from kestra_oncall.store import Store
    from kestra_oncall.incidents import proposal_commitment
    conn = stored_db()
    sha = proposal_commitment(incident_id=incident["incident_id"], execution_id=env.exec_id, failed_task_id="transform",
                              base_revision=2, base_source_sha256="0" * 64, new_source=FIXED_SOURCE)
    Store(conn).save_proposal(incident["incident_id"], proposal_sha256=sha, base_revision=2, base_source_sha256="0" * 64,
                              base_source=BASE_SOURCE, new_source=FIXED_SOURCE, rationale="forged", diff="-a\n+b\n",
                              diff_stat={"added": 1, "removed": 1}, validation=[])
    conn.close()
    args = {"incident_id": incident["incident_id"], "proposal_sha256": sha}
    assert env.hook(tool_name="kestra_apply_fix", args=args)["action"] == "block"
    assert "protected" in env.call("kestra_apply_fix", args)["error"]
    assert env.writes() == 0 and env.revisions() == 2


@pytest.mark.parametrize("markers", [{"HERMES_SESSION_PLATFORM": "webhook"},
                                     {"HERMES_CRON_SESSION": "true", "HERMES_SESSION_PLATFORM": "telegram"},
                                     {"HERMES_SINGLE_QUERY_SESSION": "1"}])
def test_unattended_writes_block_even_before_host_gate(env, markers):
    apply_args, _ = env.propose()
    pause_args = env.add_pause()
    env.call("kestra_pauses", {})
    env.markers.update(markers)
    for name, args in (("kestra_apply_fix", apply_args), ("kestra_pause_decide", pause_args)):
        assert env.hook(tool_name=name, args=args)["action"] == "block"
        assert "Unattended writes" in env.toolbox.call(name, args)
    assert env.writes() == 0 and env.revisions() == 2


def test_apply_end_to_end_through_the_tool_is_verified_and_idempotent(env):
    args, _ = env.propose()
    result = env.call("kestra_apply_fix", args)
    assert result["status"] == "verified" and result["fixed_revision"] == 3
    assert result["actor"] == "hermes:cli:glue-test-session"
    assert env.fake.current(NS, FLOW)["source"] == FIXED_SOURCE and len(env.fake.replays_of(env.exec_id)) == 1
    again = env.call("kestra_apply_fix", args)
    assert again["noop"] is True and env.revisions() == 3 and len(env.fake.replays_of(env.exec_id)) == 1


def test_apply_after_out_of_band_edit_is_stale_and_writes_nothing(env):
    args, _ = env.propose()
    assert env.hook(tool_name="kestra_apply_fix", args=args)["action"] == "approve"   # the human saw revision 2
    env.fake.put_flow(NS, FLOW, BASE_SOURCE + "# edited by someone else\n")
    before = env.writes()
    result = env.call("kestra_apply_fix", args)
    assert result["status"] == "stale" and env.writes() == before and env.revisions() == 3
    assert not env.fake.replays_of(env.exec_id)


def test_failed_replay_rolls_back_to_the_base_source(env):
    args, _ = env.propose()
    env.fake.replay_state = "FAILED"
    result = env.call("kestra_apply_fix", args)
    assert result["status"] == "rolled_back"
    assert env.fake.current(NS, FLOW)["source"] == BASE_SOURCE and env.revisions() == 4


def test_new_base_revision_gets_a_new_rule_key(env):
    args, _ = env.propose()
    first = env.hook(tool_name="kestra_apply_fix", args=args)
    env.fake.put_flow(NS, FLOW, BASE_SOURCE + "# note\n")
    new_args, _ = env.propose(source=FIXED_SOURCE + "# note\n")
    second = env.hook(tool_name="kestra_apply_fix", args=new_args)
    assert new_args["proposal_sha256"] != args["proposal_sha256"]
    assert first["rule_key"] != second["rule_key"] and "Base revision: 3" in second["message"]
    stale_old = env.hook(tool_name="kestra_apply_fix", args=args)
    assert stale_old["action"] == "block"  # the older approval hash no longer matches the latest proposal


def test_pause_requires_snapshot_and_uses_frozen_inputs(env):
    pause = env.add_pause()
    assert env.hook(tool_name="kestra_pause_decide", args=pause)["action"] == "block"
    listed = env.call("kestra_pauses", {})["pauses"]
    assert [row["execution_id"] for row in listed] == [pause["execution_id"]]
    directive = env.hook(tool_name="kestra_pause_decide", args=pause)
    assert directive["action"] == "approve"
    assert "Approve a refund of 250 EUR?" in directive["message"] and '"approved": "true"' in directive["message"]
    mutated = {**pause, "inputs": {"approved": "false"}}
    assert "No current stored pause decision" in env.toolbox.call("kestra_pause_decide", mutated)
    result = env.call("kestra_pause_decide", pause)
    assert result["status"] == "resumed"
    assert env.fake.resumed_with == pause["inputs"]
    assert env.call("kestra_pause_decide", pause)["noop"] is True


def test_pause_resumed_elsewhere_after_approval_is_stale_and_writes_nothing(env):
    pause = env.add_pause()
    env.call("kestra_pauses", {})
    assert env.hook(tool_name="kestra_pause_decide", args=pause)["action"] == "approve"
    env.fake.executions[pause["execution_id"]]["state"]["current"] = "SUCCESS"
    result = env.call("kestra_pause_decide", pause)
    assert "error" in result and env.writes() == 0


def test_pause_wrong_task_run_corrupt_hash_and_kill(env):
    pause = env.add_pause()
    env.call("kestra_pauses", {})
    stale = {**pause, "paused_task_run_id": "different-task-run"}
    assert env.hook(tool_name="kestra_pause_decide", args=stale)["action"] == "block"
    env.hook(tool_name="kestra_pause_decide", args=pause)
    conn = stored_db()
    body = json.loads(conn.execute("SELECT body FROM glue_pause_requests").fetchone()[0])
    body["inputs"]["approved"] = "false"
    conn.execute("UPDATE glue_pause_requests SET body=?", (json.dumps(body),))
    conn.commit()
    conn.close()
    assert "hash mismatch" in env.toolbox.call("kestra_pause_decide", pause)
    assert env.writes() == 0
    kill = {"execution_id": pause["execution_id"], "paused_task_run_id": pause["paused_task_run_id"], "action": "kill"}
    assert env.hook(tool_name="kestra_pause_decide", args=kill)["action"] == "approve"
    assert env.call("kestra_pause_decide", kill)["status"] == "killed"


def test_pause_in_protected_flow_is_never_decided(build):
    env = build(protected=["hko.demo.refunds"])
    pause = env.add_pause()
    env.call("kestra_pauses", {})
    assert env.hook(tool_name="kestra_pause_decide", args=pause)["action"] == "block"
    assert env.writes() == 0


def test_command_parsing():
    assert parse_command("") == {"verb": "status"}
    assert parse_command('resume exec reason="human approved"') == {"verb": "resume", "id": "exec", "inputs": {"reason": "human approved"}}
    for raw in ("apply", "apply a b", "kill a b", "resume e a=b a=c", "resume e not-a-pair", "resume e 'unfinished"):
        with pytest.raises(ValueError):
            parse_command(raw)


def test_command_writes_only_after_explicit_gate_and_via_dispatch(env):
    args, _ = env.propose()
    pause = env.add_pause()
    commands = Commands(env.ctx, env.toolbox, env.hook, approval=lambda *_: {"approved": False})
    assert "Not approved" in commands("apply " + args["incident_id"])
    assert env.writes() == 0 and not any(n == "kestra_apply_fix" for n, _ in env.ctx.dispatched)
    commands.approval = lambda *_: {"approved": True}
    assert "verified" in commands("apply " + args["incident_id"])
    assert env.ctx.dispatched[-1] == ("kestra_apply_fix", args)
    assert env.revisions() == 3 and len(env.fake.replays_of(env.exec_id)) == 1
    assert "noop" in commands("apply " + args["incident_id"]).lower() or "verified" in commands("apply " + args["incident_id"])
    assert env.revisions() == 3 and len(env.fake.replays_of(env.exec_id)) == 1
    assert "hko.demo.refunds" in commands("pauses")
    reply = commands(f"resume {pause['execution_id']} approved=true reason=\"checked by a human\"")
    assert "resumed" in reply and env.ctx.dispatched[-1][0] == "kestra_pause_decide"
    assert env.fake.resumed_with == pause["inputs"]


def test_command_read_replies_are_short_and_use_real_records(env):
    args, _ = env.propose()
    commands = Commands(env.ctx, env.toolbox, env.hook, approval=lambda *_: {"approved": False})
    status = commands("status")
    assert "Kestra 2.0.9: connected" in status and "policy ready" in status and "1 proposed" in status
    listing = commands("incidents")
    assert args["incident_id"] in listing and "proposed" in listing and f"{NS}.{FLOW}" in listing
    shown = commands("show " + args["incident_id"])
    assert args["proposal_sha256"] in shown and f"/kestra apply {args['incident_id']}" in shown and "| upper" in shown
    assert commands("pauses") == "No paused executions."
    assert "No unique current pause" in commands("resume nosuchexecution")
    assert "Unknown incident" in commands("show INC-00000000")
    assert env.writes() == 0


def test_command_show_reports_protected_flow_and_outcome(build):
    env = build(protected=[f"{NS}.{FLOW}"])
    incident = env.open()
    commands = Commands(env.ctx, env.toolbox, env.hook, approval=lambda *_: {"approved": True})
    assert "Protected flow" in commands("show " + incident["incident_id"])
    assert "No stored proposal" in commands("apply " + incident["incident_id"])
    assert env.writes() == 0


def test_command_gate_failure_never_dispatches(env):
    args, _ = env.propose()

    def unavailable(*_):
        raise RuntimeError(SECRET)

    commands = Commands(env.ctx, env.toolbox, env.hook, approval=unavailable)
    result = commands("apply " + args["incident_id"])
    assert SECRET not in result and "could not complete" in result
    assert env.writes() == 0


def test_setup_prints_valid_route_and_never_writes_without_apply(env, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "install_route", lambda route: calls.append(route) or {"installed": True})
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    result = cli.run(parser.parse_args(["setup", "--deliver", "telegram", "--chat-id", "123"]), env.toolbox)
    assert result == {"installed": False, "preview": True} and calls == []
    printed = capsys.readouterr().out
    assert SECRET not in printed and "X-Gitlab-Token" in printed
    route = YAML(typ="safe").load(printed.split("\nKestra forwarder setup:")[0])["platforms"]["webhook"]["extra"]["routes"]["kestra-oncall"]
    assert route["coalesce"]["key"] == "execution_id" and route["mirror_to_session"] is True
    assert route["skills"] == ["kestra-oncall:kestra-oncall"] and route["toolsets"] == ["kestra_oncall"]
    assert "secret" not in route and "Never apply" in route["prompt"] and "is_replay_of_incident" in route["prompt"]
    cli.run(parser.parse_args(["setup", "--apply"]), env.toolbox)
    assert len(calls) == 1


def test_doctor_against_fake_kestra_reports_each_failure_without_exposing_values(env):
    env.secrets = {}
    result = cli.doctor(env.toolbox)
    assert result["ok"] is False and result["checks"]["authentication_configured"] is False
    env.secrets = {"KESTRA_USERNAME": USER, "KESTRA_PASSWORD": "wrong-" + SECRET}
    result = cli.doctor(env.toolbox)
    assert result["ok"] is False and result["checks"]["authentication_accepted"] is False
    assert SECRET not in json.dumps(result)
    env.secrets = {"KESTRA_USERNAME": USER, "KESTRA_PASSWORD": SECRET}
    result = cli.doctor(env.toolbox)
    assert result["ok"] is True and all(result["checks"].values()) and SECRET not in json.dumps(result)


def test_doctor_without_namespaces_is_not_ok(env):
    env.ctx.settings["namespaces"] = []
    result = cli.doctor(env.toolbox)
    assert result["ok"] is False and result["checks"]["allowed_namespaces"] is False


@pytest.fixture
def hermes_host(monkeypatch):
    """Match console-script import precedence, avoiding this repo's tools.py as a top-level package."""
    import hermes_cli
    monkeypatch.syspath_prepend(str(Path(hermes_cli.__file__).resolve().parent.parent))


@pytest.mark.parametrize("answer,allowed", [("once", True), ("deny", False)])
def test_real_hermes_gate_displays_stored_diff(env, monkeypatch, hermes_host, answer, allowed):
    """Run the host's real approval resolver and decision logic, only fake the human UI."""
    import hermes_cli.plugins as plugins
    from tools import approval, approval_context

    args, _ = env.propose()
    prompts = []
    monkeypatch.setattr(plugins, "invoke_hook", lambda name, **kwargs: [env.hook(**kwargs)])
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "is_approved", lambda *_: False)

    def human(command, description, **kwargs):
        prompts.append((command, description))
        return answer

    monkeypatch.setattr(approval, "_presence", lambda *_: (human, True, False, False))
    block = plugins.resolve_pre_tool_block("kestra_apply_fix", args)
    assert (block is None) is allowed
    assert len(prompts) == 1
    assert "Base revision: 2" in prompts[0][1] and "| upper" in prompts[0][1] and args["proposal_sha256"] in prompts[0][1]
    assert env.writes() == 0   # approval alone never writes; the handler does


def test_real_hermes_unattended_default_has_no_pending_prompt(env, monkeypatch, hermes_host):
    from tools import approval, approval_context
    from kestra_oncall.commands import request_approval

    args, _ = env.propose()
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "is_approved", lambda *_: False)
    monkeypatch.setattr(approval, "_presence", lambda *_: (None, False, False, False))
    monkeypatch.setattr(approval, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: True)
    monkeypatch.setattr(approval_context, "_get_cron_approval_mode", lambda: "deny")
    directive = env.hook(tool_name="kestra_apply_fix", args=args)
    result = request_approval("kestra_apply_fix", directive)
    assert result["approved"] is False and "cron" in result["message"]
    assert "pending_approval" not in result


def test_real_hermes_command_registry_keeps_approval_gate(env, monkeypatch, hermes_host):
    """The real ctx registry path must not execute a denied slash-command write."""
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from tools import approval, approval_context
    from kestra_oncall import tools as plugin_tools

    args, _ = env.propose()
    manager = PluginManager(scope_key="glue-command-test")
    ctx = PluginContext(PluginManifest(name="kestra-oncall", path=str(ROOT), source="user"), manager)
    monkeypatch.setattr(plugin_tools, "Toolbox", lambda _ctx: env.toolbox)
    register(ctx)
    command = manager._plugin_commands["kestra"]["handler"]
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "is_approved", lambda *_: False)
    prompts = []
    answer = ["deny"]

    def human(command, description, **kwargs):
        prompts.append(description)
        return answer[0]

    monkeypatch.setattr(approval, "_presence", lambda *_: (human, True, False, False))
    assert "Not approved" in command("apply " + args["incident_id"])
    assert env.writes() == 0 and env.revisions() == 2
    answer[0] = "once"
    assert "verified" in command("apply " + args["incident_id"])
    assert env.revisions() == 3 and len(env.fake.replays_of(env.exec_id)) == 1 and len(prompts) == 2
    assert all(args["proposal_sha256"] in prompt and "| upper" in prompt for prompt in prompts)


def test_real_hermes_setup_writer_is_idempotent_and_preserves_other_routes(monkeypatch, hermes_host, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import load_config_readonly, set_config_value

    token = set_hermes_home_override(tmp_path)
    try:
        set_config_value("platforms.webhook.extra.routes.other", '{"prompt":"keep this"}')
        route = cli.route_config("telegram", "123")
        assert cli.install_route(route) == {"installed": True, "changed": True}
        assert cli.install_route(route) == {"installed": True, "changed": False}
        saved = load_config_readonly()["platforms"]["webhook"]["extra"]["routes"]
        assert saved["other"] == {"prompt": "keep this"}
        assert saved["kestra-oncall"] == route
        with pytest.raises(ValueError, match="different"):
            cli.install_route(cli.route_config("discord"))
        assert SECRET not in (tmp_path / "config.yaml").read_text()
    finally:
        reset_hermes_home_override(token)
