"""Framework core — pipeline-agnostic machinery.

This package must import with ``sluice.gates``, ``sluice.api``, and
``sluice.cli`` absent from ``sys.modules``. If you delete every production gate,
core must still import, run, and pass its tests. See the architecture design of
record, sections 2.5 and 5, and tests/test_dependency_arrows.py.

Plan A populates this package: db, gate, context, profile, reconcile, daemon,
control, audit, events.
"""
