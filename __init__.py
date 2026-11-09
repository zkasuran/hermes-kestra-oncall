"""Kestra on-call registration. Importing the package has no Hermes side effects."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def register(ctx: Any) -> None:
    """Register every surface even when Kestra is down or no credentials are set."""
    from . import cli
    from .commands import Commands
    from .hooks import ApprovalHook
    from .tools import SCHEMAS, Toolbox

    toolbox = Toolbox(ctx)
    hook = ApprovalHook(toolbox)
    for name, schema in SCHEMAS.items():
        ctx.register_tool(name=name, toolset="kestra_oncall", schema=schema, handler=toolbox.handler(name),
                          description=schema["description"], emoji="🛎")
    ctx.register_hook("pre_tool_call", hook)
    ctx.register_command("kestra", handler=Commands(ctx, toolbox),
                         description="Review Kestra incidents, approve fixes and answer pauses",
                         args_hint="status | incidents | show <id> | apply <id> <hash> | pauses | resume <exec> <taskrun> [k=v ...] | kill <exec> <taskrun>")
    ctx.register_cli_command(name="kestra-oncall", help="Configure and diagnose Kestra on-call",
                             setup_fn=cli.setup_parser, handler_fn=lambda args: cli.run(args, toolbox))
    ctx.register_skill("kestra-oncall", Path(__file__).parent / "skills" / "kestra-oncall" / "SKILL.md",
                       description="Diagnose Kestra failures, propose the smallest fix and request human approval; never write unattended.")
