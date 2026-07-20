"""sluice — a state-only pipeline control plane and daemon.

The database is the single source of truth; every operator action only writes
state, and the daemon is the sole actor that starts or stops pipeline work.

Layers (see docs/superpowers/specs/2026-07-16-sluice-architecture-design-of-record.md):

- ``sluice.core``   framework machinery, pipeline-agnostic
- ``sluice.gates``  production gates implementing ``core.gate.Gate``
- ``sluice.api``    the FastAPI control-write path
- ``sluice.cli``    the ``sluice`` command, an HTTP client to the api
- ``sluice.config`` config schema + loader

One-way dependency: ``cli -> api -> core``; ``gates -> core``; ``core`` imports
nothing internal. That arrow is enforced by tests/test_dependency_arrows.py.
"""

__version__ = "0.1.0"
