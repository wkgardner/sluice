"""Architecture guard: the one-way dependency ``gates -> core``.

The framework core must import with ``sluice.gates``, ``sluice.api``,
``sluice.cli`` and ``sluice.doctor`` absent. If any module under ``sluice.core``
imports one of those, the pipeline is no longer a framework — you could not
delete the production gates and still ship the core.

This test encodes that invariant as an executable guard (architecture design of
record, sections 2.5 and 5). At bootstrap the packages are empty, so it is
trivially green — but it is in place *before* any code can violate it.
"""

import importlib
import pkgutil
import sys

import pytest

# Top-level sluice modules and subpackages that core is forbidden to import.
# `sluice.doctor` is listed in its own right: it imports gates, so a core import
# of it would trip this guard only *transitively* today, and would evaporate the
# moment doctor made that import lazy.
FORBIDDEN = ("sluice.gates", "sluice.api", "sluice.cli", "sluice.doctor")


class _DependencyBlocker:
    """A ``sys.meta_path`` finder that refuses to import forbidden packages.

    Installing this ahead of the normal finders makes any attempt by core code
    to import a forbidden package raise ``ImportError`` at import time, which
    fails the test with a message naming the offending module.
    """

    def __init__(self, forbidden):
        self.forbidden = forbidden

    def find_spec(self, fullname, path, target=None):
        for bad in self.forbidden:
            if fullname == bad or fullname.startswith(bad + "."):
                raise ImportError(
                    f"core code must not import {fullname!r} "
                    f"(one-way dependency: gates -> core, never the reverse)"
                )
        return None


def _drop_sluice_modules():
    """Evict cached ``sluice.*`` modules so (re)imports go through the blocker."""
    for name in list(sys.modules):
        if name == "sluice" or name.startswith("sluice."):
            del sys.modules[name]


def _core_module_names():
    """Every importable module under ``sluice.core`` (including the package)."""
    core = importlib.import_module("sluice.core")
    names = ["sluice.core"]
    for info in pkgutil.walk_packages(core.__path__, prefix="sluice.core."):
        names.append(info.name)
    return names


def test_core_never_imports_gates_api_or_cli():
    """Import every ``sluice.core`` module with the forbidden packages blocked.

    Teardown restores the *original* module objects into ``sys.modules`` rather
    than leaving it stripped: evicting them would make later tests re-import
    fresh copies of ``sluice`` classes, and enum members compared with ``is``
    (the loop's spec'd branch pattern, plan 02 unit 2) would no longer match
    references bound at collection time.
    """
    names = _core_module_names()

    saved_sluice_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "sluice" or name.startswith("sluice.")
    }
    _drop_sluice_modules()
    blocker = _DependencyBlocker(FORBIDDEN)
    sys.meta_path.insert(0, blocker)
    try:
        for name in names:
            sys.modules.pop(name, None)
            try:
                importlib.import_module(name)
            except ImportError as exc:  # only the blocker raises the marker text
                pytest.fail(str(exc))
    finally:
        sys.meta_path.remove(blocker)
        _drop_sluice_modules()
        sys.modules.update(saved_sluice_modules)
