#!/usr/bin/env python3
"""Type slash commands into the REAL Hermes terminal UI inside a pseudo-terminal and print what it showed.

Needs `pexpect` (the rig venv has none; the system python3 does). Usage:

    HERMES_HOME=... python3 tests/e2e/pty_cli.py "/kestra status|Kestra 2.0.9: connected" "/kestra show INC-x|To apply exactly this"

Each argument is `<text to type>|<substring that must appear in the answer>`. Output: one JSON line
{"results": [{"typed", "expect", "found", "seconds", "screen"}...], "alive_at_end": bool}. The model is
never asked anything: slash commands run in the CLI's own command dispatcher, which is the path a
person uses.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

import pexpect

HERMES = os.environ.get('HKO_HERMES_DIR', '/home/asuran/hko-rig/hermes-agent')
ANSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07')


def drain(child, seconds: float) -> str:
    out, end = '', time.time() + seconds
    while time.time() < end:
        try:
            out += child.read_nonblocking(65536, timeout=0.5)
        except pexpect.TIMEOUT:
            pass
        except pexpect.EOF:
            break
    return out


def main() -> int:
    env = dict(os.environ, TERM='xterm-256color')
    child = pexpect.spawn(f'{HERMES}/.venv/bin/hermes', ['chat', '--cli'], env=env, encoding='utf-8',
                          timeout=60, dimensions=(60, 200), cwd=os.getcwd())
    screen = ''
    deadline = time.time() + 90
    while time.time() < deadline and 'Type your message' not in screen:
        screen += drain(child, 1)
    screen += drain(child, 12)  # the banner appears before the input line accepts keys; a first command typed too early is lost
    results = []
    for spec in sys.argv[1:]:
        typed, _, expect = spec.partition('|')
        child.send(typed + '\r')
        start, seen = time.time(), ''
        seen += drain(child, 4)
        if typed not in ANSI.sub('', seen):      # not even echoed: the UI was not ready, type it again
            child.send(typed + '\r')
        while time.time() - start < 150:
            seen += drain(child, 1)
            if expect in ANSI.sub('', seen):
                seen += drain(child, 2)
                break
        text = ANSI.sub('', seen)
        results.append({'typed': typed, 'expect': expect, 'found': expect in text,
                        'seconds': round(time.time() - start, 1), 'screen': text[-2500:]})
    alive = child.isalive()
    child.send('/exit\r')
    drain(child, 3)
    child.terminate(force=True)
    print(json.dumps({'results': results, 'alive_at_end': alive}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
