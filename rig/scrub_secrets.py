#!/usr/bin/env python3
"""Replace the rig's real credential VALUES with [REDACTED] in files you name (agent transcripts, logs).

    python rig/scrub_secrets.py .work/rig/kiro-run-1.jsonl .work/docs/kiro-run-1.jsonl [--check]

Values come from /home/asuran/hko-rig/secrets/kestra.env, the rig HERMES_HOME .env and the opencode LLM key
(keys containing KEY, TOKEN, PASSWORD or SECRET, 8+ characters) plus KESTRA_USERNAME. Nothing is printed except
per-file counts. `--check` only counts. Run it after the agents have stopped writing: a live transcript is
appended to while it runs. Rotate the Kestra password if a transcript ever left this machine.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

from common import HOME, SECRETS, env_file

OPENCODE = Path.home() / '.config/opencode/opencode.json'


def secret_values() -> list[str]:
    values: set[str] = set()
    for path in (SECRETS, HOME / '.env'):
        if path.exists():
            for key, value in env_file(path).items():
                if len(value) >= 8 and any(t in key for t in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET')):
                    values.add(value)
                if key == 'KESTRA_USERNAME' and len(value) >= 8:
                    values.add(value)
    try:
        values.add(json.loads(OPENCODE.read_text())['provider']['agentrouter']['options']['apiKey'])
    except (OSError, KeyError, ValueError):
        pass
    return sorted(values, key=len, reverse=True)


def main() -> int:
    check = '--check' in sys.argv
    files = [Path(a) for a in sys.argv[1:] if not a.startswith('--')]
    values = secret_values()
    total = 0
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(errors='replace')
        count = sum(text.count(v) for v in values)
        total += count
        print(f'{path}: {count} occurrence(s)' + ('' if check or not count else ' -> redacted'))
        if count and not check:
            for v in values:
                text = text.replace(v, '[REDACTED]')
            path.write_text(text)
    return 1 if (check and total) else 0


if __name__ == '__main__':
    raise SystemExit(main())
