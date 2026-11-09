"""Drive the installed plugin through real Hermes code paths, in a process that plays one chat session.

Run with the rig venv python and HERMES_HOME=/home/asuran/hko-rig/hermes-home. It loads the plugin the
way Hermes does (plugin discovery, config settings, .env credentials, the profile's SQLite store) and then
either calls one registered tool through `model_tools.handle_function_call` (the same entry the agent loop
uses: pre_tool_call hook, approval gate, registry) or runs the `/kestra` slash command handler.

The human is a scripted answer to Hermes's own approval prompt (`--answer once|deny`). The prompt text
that Hermes would show a person is captured in the result, so the harness can assert that it carries the
STORED diff and hash. `--platform webhook|cron` plays an unattended run.

Fault injection (S6 only): `--fault crash-before-replay` kills this process the moment the plugin is about
to call Kestra's replay endpoint, i.e. after the flow update landed and was read back.

Output: one JSON object on the last stdout line: {"output": ..., "prompts": [...], "approval_calls": n}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

HERMES = '/home/asuran/hko-rig/hermes-agent'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', default='cli', choices=['cli', 'webhook', 'cron'])
    parser.add_argument('--answer', default='once', choices=['once', 'deny'])
    parser.add_argument('--fault', default='', choices=['', 'crash-before-replay'])
    sub = parser.add_subparsers(dest='mode', required=True)
    command = sub.add_parser('command')
    command.add_argument('text')
    tool = sub.add_parser('tool')
    tool.add_argument('name')
    tool.add_argument('args_json')
    args = parser.parse_args()

    os.environ.setdefault('HERMES_HOME', '/home/asuran/hko-rig/hermes-home')
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.path.insert(0, HERMES)
    # Hermes boot order: activate the managed dependency environment, then load .env, then plugins.
    from pm.environments import activate_dependencies
    from pathlib import Path
    activate_dependencies(Path(HERMES))
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()

    session_id = 'e2e-' + uuid.uuid4().hex[:8]
    if args.platform == 'cli':
        os.environ.update(HERMES_INTERACTIVE='1', HERMES_SESSION_PLATFORM='cli', HERMES_SESSION_ID=session_id)
    elif args.platform == 'webhook':
        os.environ.update(HERMES_SESSION_PLATFORM='webhook', HERMES_SESSION_ID=session_id)
        os.environ.pop('HERMES_INTERACTIVE', None)
    else:
        os.environ.update(HERMES_CRON_SESSION='true', HERMES_SESSION_PLATFORM='cron', HERMES_SESSION_ID=session_id)
        os.environ.pop('HERMES_INTERACTIVE', None)

    prompts: list[dict] = []

    def human(command_label, description, **kwargs):
        """Hermes calls this where a person would see the approval card."""
        prompts.append({'label': command_label, 'description': description})
        return args.answer

    from tools.terminal_tool import set_approval_callback
    set_approval_callback(human)

    from hermes_cli.plugins import discover_plugins, get_plugin_command_handler
    discover_plugins(force=True)

    if args.fault == 'crash-before-replay':
        # The plugin module name is chosen by Hermes at load time; find the loaded client module.
        KestraClient = next(m.KestraClient for n, m in list(sys.modules.items()) if n.endswith('client') and hasattr(m, 'KestraClient'))

        def die(self, *a, **k):
            sys.stdout.flush()
            print(json.dumps({'fault': 'killed before replay', 'prompts': prompts}), flush=True)
            os._exit(137)
        KestraClient.replay = die

    if args.mode == 'command':
        handler = get_plugin_command_handler('kestra')
        if handler is None:
            raise SystemExit('the /kestra command is not registered; is the plugin enabled?')
        output = handler(args.text)
    else:
        import model_tools
        output = model_tools.handle_function_call(args.name, json.loads(args.args_json), session_id=session_id)
    try:
        output = json.loads(output)
    except (TypeError, ValueError):
        pass
    print(json.dumps({'output': output, 'prompts': prompts, 'approval_calls': len(prompts),
                      'platform': args.platform, 'session_id': session_id}, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
