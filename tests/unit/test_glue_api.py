"""Glue contracts, including adversarial inputs and actual Hermes approval resolution."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
# The core-owned conftest supplies this alias once it lands. This bootstrap also
# lets glue's contract tests run while the independently owned core is absent.
if "kestra_oncall" not in sys.modules:
    spec = importlib.util.spec_from_file_location("kestra_oncall", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)

from kestra_oncall import register
from kestra_oncall import cli
from kestra_oncall.commands import Commands, parse_command
from kestra_oncall.hooks import ApprovalHook, ApprovalStore
from kestra_oncall.settings import Runtime, Settings, SettingsError, require_attended
from kestra_oncall.tools import SCHEMAS, Toolbox, validate_args

SECRET = "glue-test-private-credential-never-display"
SOURCE = "id: orders\nnamespace: hko.gluetest\ntasks: []\n"
SHA = hashlib.sha256(SOURCE.encode()).hexdigest()
ARGS = {"incident_id": "INC-1234abcd", "proposal_sha256": SHA}
PAUSE_ARGS = {"execution_id": "exec-pause", "paused_task_run_id": "tr-pause", "action": "resume", "inputs": {"approved": "true"}}


class FakeContext:
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


class FakeOncall:
    """Temporary contract stand-in, replaced by real core in the integration fixture."""

    def __init__(self):
        self.incident = {"incident_id": ARGS["incident_id"], "namespace": "hko.gluetest", "flow_id": "orders", "status": "proposed", "protected": False,
                         "proposal": {"proposal_sha256": SHA, "new_source": SOURCE, "base_revision": 2,
                                      "diff": "--- old\n+++ new\n-old_column\n+new_column\n", "diff_stat": {"added": 1, "removed": 1}, "rationale": "Column was renamed."}}
        self.writes = []

    def get_incident(self, incident_id):
        if incident_id != ARGS["incident_id"]:
            raise ValueError("Unknown incident. Open the failed execution first.")
        return copy.deepcopy(self.incident)

    def open_incident(self, execution_id):
        return self.get_incident(ARGS["incident_id"])

    def list_incidents(self, **kwargs):
        return [self.get_incident(ARGS["incident_id"])]

    def propose_fix(self, incident_id, source, rationale):
        return self.get_incident(incident_id)["proposal"]

    def apply_fix(self, incident_id, sha, *, actor):
        self.writes.append((incident_id, sha, actor))
        return {"incident_id": incident_id, "status": "verified"}

    def summary(self):
        return {"proposed": 1}


class FakePauseDesk:
    def __init__(self):
        self.rows = [{"execution_id": "exec-pause", "paused_task_run_id": "tr-pause", "paused_task_id": "approval",
                      "namespace": "hko.gluetest", "flow_id": "refund", "description": "Approve this refund?", "inputs": {}}]
        self.writes = []

    def list_pauses(self):
        return copy.deepcopy(self.rows)

    def decide(self, execution_id, task_run_id, action, inputs, *, actor):
        self.writes.append((execution_id, task_run_id, action, inputs, actor))
        return {"execution_id": execution_id, "state": "RUNNING", "action": action}


@pytest.fixture
def env():
    ctx = FakeContext({"namespaces": ["hko.gluetest"]})
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    oncall, pauses = FakeOncall(), FakePauseDesk()
    runtime = Runtime(Settings.from_context(ctx), SimpleNamespace(server_version=lambda: "2.0.9", search_executions=lambda **_: []),
                      SimpleNamespace(get_proposal=lambda _: None), oncall, pauses, conn)
    markers = {"HERMES_SESSION_PLATFORM": "cli", "HERMES_SESSION_ID": "glue-test-session"}

    @contextmanager
    def factory(_ctx, _secrets):
        yield runtime

    toolbox = Toolbox(ctx, runtime_factory=factory, secret_reader=lambda: {"KESTRA_PASSWORD": SECRET}, context_reader=lambda: markers)
    hook = ApprovalHook(toolbox)
    for name in SCHEMAS:
        ctx.register_tool(name, handler=toolbox.handler(name))
    yield SimpleNamespace(ctx=ctx, toolbox=toolbox, hook=hook, runtime=runtime, markers=markers, oncall=oncall, pauses=pauses)
    conn.close()


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
    ("kestra_apply_fix", {**ARGS, "diff": "trust me"}), ("kestra_apply_fix", {**ARGS, "proposal_sha256": "a"}),
    ("kestra_incident_open", {"execution_id": "../../secret"}), ("kestra_incidents", {"limit": True}),
    ("kestra_incidents", {"incident_id": "INC-x", "limit": 2}),
    ("kestra_pause_decide", {**PAUSE_ARGS, "inputs": {"approved": True}}),
    ("kestra_pause_decide", {**PAUSE_ARGS, "action": "kill"}),
    ("kestra_propose_fix", {"incident_id": "INC-x", "new_source": "x", "rationale": "x" * 2001}),
])
def test_handlers_wrap_invalid_arguments(env, name, args):
    result = json.loads(env.toolbox.call(name, args))
    assert set(result) == {"error", "hint"}
    assert env.oncall.writes == [] and env.pauses.writes == []


def test_status_reports_network_failure_and_keeps_stored_summary(env):
    def down():
        raise OSError(SECRET)
    env.runtime.client.server_version = down
    result = json.loads(env.toolbox.call("kestra_status", {}))
    assert result["connected"] is False and result["summary"] == {"proposed": 1}
    assert SECRET not in json.dumps(result)


def test_success_and_error_outputs_redact_secrets(env):
    env.oncall.incident["log"] = f"credential={SECRET}"
    assert SECRET not in env.toolbox.call("kestra_incident_open", {"execution_id": "failed"})
    def fail(*args, **kwargs):
        raise ValueError(f"failure {SECRET}")
    env.oncall.open_incident = fail
    result = env.toolbox.call("kestra_incident_open", {"execution_id": "failed"})
    assert SECRET not in result and "[REDACTED]" in result


def test_hook_approves_exact_stored_proposal(env):
    directive = env.hook(tool_name="kestra_apply_fix", args=ARGS)
    assert directive["action"] == "approve"
    assert all(text in directive["message"] for text in ("hko.gluetest.orders", "Base revision: 2", "-old_column", "+new_column", SHA))
    assert directive["rule_key"].endswith(SHA)
    assert ARGS["incident_id"] in directive["rule_key"]
    assert env.oncall.writes == []


@pytest.mark.parametrize("args", [{**ARGS, "incident_id": "INC-unknown"}, {**ARGS, "proposal_sha256": "f" * 64}, {**ARGS, "diff": "do not show real diff"}, None])
def test_hook_blocks_unknown_mismatched_or_forged_proposals(env, args):
    assert env.hook(tool_name="kestra_apply_fix", args=args)["action"] == "block"


def test_hook_handles_read_tools_and_protected_flow(env):
    assert env.hook(tool_name="kestra_status", args={}) is None
    env.oncall.incident["protected"] = True
    assert env.hook(tool_name="kestra_apply_fix", args=ARGS)["action"] == "block"


@pytest.mark.parametrize("markers", [{"HERMES_SESSION_PLATFORM": "webhook"}, {"HERMES_CRON_SESSION": "true", "HERMES_SESSION_PLATFORM": "telegram"}, {"HERMES_SINGLE_QUERY_SESSION": "1"}])
def test_unattended_writes_block_even_before_host_gate(env, markers):
    env.markers.update(markers)
    for name, args in (("kestra_apply_fix", ARGS), ("kestra_pause_decide", PAUSE_ARGS)):
        assert env.hook(tool_name=name, args=args)["action"] == "block"
        assert "Unattended writes" in env.toolbox.call(name, args)
    assert env.oncall.writes == [] and env.pauses.writes == []


def test_pause_requires_snapshot_and_uses_frozen_inputs(env):
    assert env.hook(tool_name="kestra_pause_decide", args=PAUSE_ARGS)["action"] == "block"
    env.toolbox.call("kestra_pauses", {})
    directive = env.hook(tool_name="kestra_pause_decide", args=PAUSE_ARGS)
    assert directive["action"] == "approve"
    assert "Approve this refund?" in directive["message"] and '"approved": "true"' in directive["message"]
    mutated = {**PAUSE_ARGS, "inputs": {"approved": "false"}}
    assert "No current stored pause decision" in env.toolbox.call("kestra_pause_decide", mutated)
    result = json.loads(env.toolbox.call("kestra_pause_decide", PAUSE_ARGS))
    assert result["state"] == "RUNNING"
    assert env.pauses.writes[0][3] == {"approved": "true"}


def test_pause_corrupt_hash_and_stale_task_are_blocked(env):
    env.toolbox.call("kestra_pauses", {})
    stale = {**PAUSE_ARGS, "paused_task_run_id": "different-task-run"}
    assert env.hook(tool_name="kestra_pause_decide", args=stale)["action"] == "block"
    env.hook(tool_name="kestra_pause_decide", args=PAUSE_ARGS)
    conn = env.runtime.conn
    body = json.loads(conn.execute("SELECT body FROM glue_pause_requests").fetchone()[0])
    body["inputs"]["approved"] = "false"
    conn.execute("UPDATE glue_pause_requests SET body=?", (json.dumps(body),))
    assert "hash mismatch" in env.toolbox.call("kestra_pause_decide", PAUSE_ARGS)
    assert env.pauses.writes == []


def test_command_parsing():
    assert parse_command("") == {"verb": "status"}
    assert parse_command('resume exec reason="human approved"') == {"verb": "resume", "id": "exec", "inputs": {"reason": "human approved"}}
    for raw in ("apply", "apply a b", "kill a b", "resume e a=b a=c", "resume e not-a-pair", "resume e 'unfinished"):
        with pytest.raises(ValueError):
            parse_command(raw)


def test_command_writes_only_after_explicit_gate_and_via_dispatch(env):
    commands = Commands(env.ctx, env.toolbox, env.hook, approval=lambda *_: {"approved": False})
    assert "Not approved" in commands("apply " + ARGS["incident_id"])
    assert not env.oncall.writes and not any(n == "kestra_apply_fix" for n, _ in env.ctx.dispatched)
    commands.approval = lambda *_: {"approved": True}
    assert "verified" in commands("apply " + ARGS["incident_id"])
    assert env.ctx.dispatched[-1] == ("kestra_apply_fix", ARGS)
    assert env.oncall.writes[0][2] == "hermes:cli:glue-test-session"
    assert "RUNNING" in commands("resume exec-pause approved=true")
    assert env.ctx.dispatched[-1][0] == "kestra_pause_decide"


def test_command_gate_failure_never_dispatches(env):
    def unavailable(*_):
        raise RuntimeError(SECRET)
    commands = Commands(env.ctx, env.toolbox, env.hook, approval=unavailable)
    result = commands("apply " + ARGS["incident_id"])
    assert SECRET not in result and "could not complete" in result
    assert env.oncall.writes == []


def test_command_reads_proposal_from_store_when_incident_is_separate(env):
    proposal = env.oncall.incident.pop("proposal")
    env.runtime.store.get_proposal = lambda _: proposal
    commands = Commands(env.ctx, env.toolbox, env.hook, approval=lambda *_: {"approved": True})
    assert "verified" in commands("apply " + ARGS["incident_id"])


def test_rule_cache_does_not_cover_new_base_revision(env):
    first = env.hook(tool_name="kestra_apply_fix", args=ARGS)
    env.oncall.incident["proposal"]["base_revision"] += 1
    second = env.hook(tool_name="kestra_apply_fix", args=ARGS)
    assert first["rule_key"] != second["rule_key"]


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
    assert "secret" not in route and "Never apply" in route["prompt"]
    cli.run(parser.parse_args(["setup", "--apply"]), env.toolbox)
    assert len(calls) == 1


def test_doctor_reports_missing_auth_without_exposing_values(env):
    result = cli.doctor(env.toolbox)
    assert result["ok"] is False and result["checks"]["authentication_configured"] is False
    env.toolbox.secret_reader = lambda: {"KESTRA_USERNAME": "glue-user", "KESTRA_PASSWORD": SECRET}
    result = cli.doctor(env.toolbox)
    assert result["ok"] is True and SECRET not in json.dumps(result)


def test_doctor_probes_authenticated_endpoint_not_only_public_version(env):
    env.toolbox.secret_reader = lambda: {"KESTRA_USERNAME": "glue-user", "KESTRA_PASSWORD": SECRET}
    def refused(**kwargs):
        raise ValueError(f"authentication failed {SECRET}")
    env.runtime.client.search_executions = refused
    result = cli.doctor(env.toolbox)
    assert result["checks"]["connectivity"] is True
    assert result["checks"]["authentication_accepted"] is False and result["ok"] is False
    assert SECRET not in json.dumps(result)


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

    prompts = []
    monkeypatch.setattr(plugins, "invoke_hook", lambda name, **kwargs: [env.hook(**kwargs)])
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "is_approved", lambda *_: False)
    def human(command, description, **kwargs):
        prompts.append((command, description))
        return answer
    monkeypatch.setattr(approval, "_presence", lambda *_: (human, True, False, False))
    block = plugins.resolve_pre_tool_block("kestra_apply_fix", ARGS)
    assert (block is None) is allowed
    assert len(prompts) == 1
    assert "Base revision: 2" in prompts[0][1] and "+new_column" in prompts[0][1] and SHA in prompts[0][1]


def test_real_hermes_unattended_default_has_no_pending_prompt(env, monkeypatch, hermes_host):
    from tools import approval, approval_context
    from kestra_oncall.commands import request_approval

    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "is_approved", lambda *_: False)
    monkeypatch.setattr(approval, "_presence", lambda *_: (None, False, False, False))
    monkeypatch.setattr(approval, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: True)
    monkeypatch.setattr(approval_context, "_get_cron_approval_mode", lambda: "deny")
    directive = env.hook(tool_name="kestra_apply_fix", args=ARGS)
    result = request_approval("kestra_apply_fix", directive)
    assert result["approved"] is False and "cron" in result["message"]
    assert "pending_approval" not in result


def test_real_hermes_command_registry_keeps_approval_gate(env, monkeypatch, hermes_host):
    """The real ctx registry path must not execute a denied slash-command write."""
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from tools import approval, approval_context
    from kestra_oncall import tools as plugin_tools

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
    assert "Not approved" in command("apply " + ARGS["incident_id"])
    assert env.oncall.writes == []
    answer[0] = "once"
    assert "verified" in command("apply " + ARGS["incident_id"])
    assert len(env.oncall.writes) == 1 and len(prompts) == 2
    assert all(SHA in prompt and "+new_column" in prompt for prompt in prompts)


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
