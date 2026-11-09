"""Make aiohttp visible to the gateway in the rig.

Hermes activates a managed dependency environment under HERMES_HOME/installs once a plugin with
python_dependencies is enabled. In this source checkout that environment lacks the `messaging`
extra, so the webhook adapter reports "aiohttp not installed". `hermes pm install --extra messaging`
refuses here (it wants node/ffmpeg on PATH), so this installs the single pinned package the webhook
adapter needs into the rig's own managed environment. Nothing outside HERMES_HOME is touched.
Idempotent.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from common import HERMES, HOME

PIN = 'aiohttp==3.14.5'  # same pin as the checkout's pyproject messaging extra


def managed_python() -> Path | None:
    os.environ['HERMES_HOME'] = str(HOME)
    sys.path.insert(0, str(HERMES))
    from pm.environments import committed_venv, venv_python
    venv = committed_venv(HERMES)
    return venv_python(venv) if venv else None


def main() -> int:
    python = managed_python()
    if python is None:
        print('no managed environment; the main venv is used (aiohttp already present)')
        return 0
    probe = subprocess.run([str(python), '-c', 'import aiohttp'], capture_output=True)
    if probe.returncode == 0:
        print('aiohttp already importable in', python.parent.parent)
        return 0
    result = subprocess.run(['uv', 'pip', 'install', '--python', str(python), PIN],
                            capture_output=True, text=True, timeout=300)
    print(result.stdout[-400:], result.stderr[-400:])
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
