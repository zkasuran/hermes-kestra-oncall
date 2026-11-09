"""Load the repository root as the package `kestra_oncall` so tests import it like Hermes does."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if "kestra_oncall" not in sys.modules:
    spec = importlib.util.spec_from_file_location("kestra_oncall", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
