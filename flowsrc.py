"""Flow source helpers: parse, hash, diff.

Kestra stores flow source verbatim (comments, trailing spaces and CRLF round-trip byte-equal, see
.work/core/FACTS.md section 3), so every equality check that guards a write compares exact bytes via
`sha256_text`. `normalise_source` exists only for human-facing comparisons and diffs.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import re
from dataclasses import dataclass, field
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# Top-level keys whose values contain tasks. Triggers, inputs and labels also carry `id`/`type` keys
# but are not executable task runs, so they are deliberately left out.
_TASK_ROOTS = ("tasks", "errors", "finally", "afterExecution")
PAUSE_TYPE_SUFFIX = ".flow.Pause"


class FlowSourceError(ValueError):
    """The flow source cannot be parsed or lacks the minimum identity. The message says what to fix."""


@dataclass
class PauseTask:
    id: str
    description: str = ""
    on_resume: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "description": self.description, "on_resume": self.on_resume}


@dataclass
class FlowInfo:
    namespace: str
    flow_id: str
    task_ids: list[str]
    pause_tasks: dict[str, PauseTask]

    @property
    def qualified_id(self) -> str:
        return f"{self.namespace}.{self.flow_id}"


def sha256_text(text: str) -> str:
    """Hash of the exact UTF-8 bytes. Never normalise before hashing a value that guards a write."""
    return sha256_bytes(text.encode("utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalise_source(source: str) -> str:
    """Line endings, trailing spaces and the final newline, for display and lenient comparison only."""
    lines = [line.rstrip() for line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip("\n") + "\n"


def equivalent(a: str, b: str) -> bool:
    return normalise_source(a) == normalise_source(b)


def load_flow(source: str) -> dict[str, Any]:
    """Parse the YAML into plain dicts. Duplicate keys and non-mapping roots are errors."""
    if not isinstance(source, str) or not source.strip():
        raise FlowSourceError("Flow source is empty. Provide the complete YAML of the flow.")
    yaml = YAML(typ="safe", pure=True)
    yaml.allow_duplicate_keys = False
    try:
        data = yaml.load(io.StringIO(source))
    except YAMLError as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid YAML"
        raise FlowSourceError(f"Flow source is not valid YAML: {first_line[:200]}") from None
    if not isinstance(data, dict):
        raise FlowSourceError("Flow source must be a YAML mapping with id, namespace and tasks.")
    return data


def _is_task(node: Any) -> bool:
    # Task types are plugin class names (contain a dot); input types are plain enums like STRING or BOOL.
    return (isinstance(node, dict) and isinstance(node.get("id"), str)
            and isinstance(node.get("type"), str) and "." in node["type"])


def _walk_tasks(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_walk_tasks(item))
    elif isinstance(node, dict):
        if _is_task(node):
            found.append(node)
        for value in node.values():
            found.extend(_walk_tasks(value))
    return found


def _pause_from(task: dict[str, Any]) -> PauseTask:
    declared = []
    for item in task.get("onResume") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            declared.append({
                "id": item["id"],
                "type": str(item.get("type", "STRING")),
                "required": bool(item.get("required", True)),
                "defaults": None if item.get("defaults") is None else str(item.get("defaults")),
                "description": str(item.get("description", "")),
            })
    return PauseTask(id=task["id"], description=str(task.get("description") or ""), on_resume=declared)


def parse_flow(source: str) -> FlowInfo:
    data = load_flow(source)
    namespace, flow_id = data.get("namespace"), data.get("id")
    if not isinstance(namespace, str) or not isinstance(flow_id, str) or not namespace or not flow_id:
        raise FlowSourceError("Flow source needs string `id` and `namespace` keys.")
    tasks: list[dict[str, Any]] = []
    for key in _TASK_ROOTS:
        tasks.extend(_walk_tasks(data.get(key)))
    ids, seen = [], set()
    for task in tasks:
        if task["id"] not in seen:
            seen.add(task["id"])
            ids.append(task["id"])
    pauses = {t["id"]: _pause_from(t) for t in tasks if str(t["type"]).endswith(PAUSE_TYPE_SUFFIX)}
    return FlowInfo(namespace=namespace, flow_id=flow_id, task_ids=ids, pause_tasks=pauses)


def unified_diff(old: str, new: str, *, fromfile: str = "base", tofile: str = "proposed") -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=fromfile, tofile=tofile, n=3))


def diff_stat(old: str, new: str) -> dict[str, int]:
    added = removed = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed}


_INPUT_REF = re.compile(r"\{\{\s*inputs\.([A-Za-z0-9_]+)\s*\}\}")


def render_inputs(template: str, inputs: dict[str, Any] | None) -> str:
    """Best-effort rendering of `{{ inputs.name }}` so a human sees the real question. Other
    expressions stay as written. This is display text only and never drives a decision."""
    values = inputs or {}
    return _INPUT_REF.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)
