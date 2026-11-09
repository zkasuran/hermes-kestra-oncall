"""flowsrc: parsing, hashing and diffs."""

import pytest

from kestra_oncall import flowsrc

NESTED = """id: nested
namespace: hko.demo
inputs:
  - id: amount
    type: INT
triggers:
  - id: nightly
    type: io.kestra.plugin.core.trigger.Schedule
    cron: "0 3 * * *"
tasks:
  - id: first
    type: io.kestra.plugin.core.debug.Return
    format: x
  - id: group
    type: io.kestra.plugin.core.flow.Sequential
    tasks:
      - id: inner
        type: io.kestra.plugin.core.log.Log
        message: hi
  - id: approve
    type: io.kestra.plugin.core.flow.Pause
    description: "OK to pay {{ inputs.amount }}?"
    onResume:
      - id: approved
        type: BOOL
        defaults: false
      - id: why
        type: STRING
        required: false
errors:
  - id: alert
    type: io.kestra.plugin.core.log.Log
    message: failed
"""


def test_parse_finds_nested_tasks_pauses_and_skips_inputs_and_triggers():
    info = flowsrc.parse_flow(NESTED)
    assert (info.namespace, info.flow_id) == ("hko.demo", "nested")
    assert info.task_ids == ["first", "group", "inner", "approve", "alert"]
    assert "amount" not in info.task_ids and "nightly" not in info.task_ids and "approved" not in info.task_ids
    pause = info.pause_tasks["approve"]
    assert pause.description == "OK to pay {{ inputs.amount }}?"
    assert [(i["id"], i["type"], i["required"]) for i in pause.on_resume] == [("approved", "BOOL", True), ("why", "STRING", False)]
    assert pause.on_resume[0]["defaults"] == "False"


@pytest.mark.parametrize("source,fragment", [
    ("", "empty"), ("a: [b: c", "not valid YAML"), ("- just\n- a list\n", "mapping"),
    ("id: x\ntasks: []\n", "`id` and `namespace`"), ("id: a\nid: b\nnamespace: n\n", "not valid YAML"),
])
def test_parse_errors_say_what_to_fix(source, fragment):
    with pytest.raises(flowsrc.FlowSourceError, match=fragment):
        flowsrc.parse_flow(source)


def test_hash_is_over_exact_bytes_but_equivalent_ignores_whitespace():
    a, b = "id: a\nnamespace: n\n", "id: a\r\nnamespace: n  \r\n"
    assert flowsrc.sha256_text(a) != flowsrc.sha256_text(b)
    assert flowsrc.equivalent(a, b)
    assert flowsrc.sha256_text("é") == flowsrc.sha256_bytes("é".encode())


def test_diff_and_stat():
    old, new = "a\nb\nc\n", "a\nB\nc\nd\n"
    assert flowsrc.diff_stat(old, new) == {"added": 2, "removed": 1}
    diff = flowsrc.unified_diff(old, new, fromfile="old", tofile="new")
    assert "-b" in diff and "+B" in diff and "+d" in diff and diff.startswith("--- old")
    assert flowsrc.diff_stat(old, old) == {"added": 0, "removed": 0}


def test_render_inputs_only_touches_known_simple_references():
    text = "Pay {{ inputs.amount }} to {{ inputs.who }} on {{ now() }}"
    assert flowsrc.render_inputs(text, {"amount": 250}) == "Pay 250 to {{ inputs.who }} on {{ now() }}"
    assert flowsrc.render_inputs(text, None) == text
