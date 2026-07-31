"""The ``experiment`` gate — Plan B gate 0, the first production gate (plan 13 Unit 3).

The FIRST gate that touches an external service (LabKey), the FIRST that persists a
load-bearing user-facing value (``experiment_id``, Will's reference key), and a
**protected / non-resettable** gate. It lives under ``sluice.gates`` (the world side
of the ``gates -> core`` arrow) because it may import the ``labkey`` package; it
imports ``sluice.core`` for the ``Gate``/``GateResult`` contract only.

The central hazard is errata C1: LabKey experiment creation is non-idempotent, so a
naive retry mints a duplicate real experiment. This gate closes that three ways
(§2 C1): resume-first from state (no network when ``experiment_id`` is already set),
reject "most-recent-by-user" (identity is the ``Description`` the gate wrote), and
**lookup-before-create** (find first; create only on a ``None`` find — closing the
"created in LabKey, daemon died before persist" crash window). Every ``LabKeyError``
becomes a quarantine, fail-closed: an erroring or ambiguous lookup **never** falls
through to create (ADR-0002).

The gate only RETURNS ``experiment_id`` in ``GateResult.metadata``; the daemon
persists it atomically with ``labkey_registered`` (``persists`` / ADR-0006). No run
state is written here (ADR-0002 decision 2), and no file is ever deleted (ADR-0005).
"""

import functools
from typing import Any

from sluice.core.gate import Gate, GateOutcome, GateResult
from sluice.gates.labkey import LabKeyError, make_experiment_client

__all__ = ["EXPERIMENT", "run_experiment_gate"]


def run_experiment_gate(context: Any, make_client) -> GateResult:
    """Resolve (or create) this run's LabKey experiment, idempotently, fail-closed.

    ``make_client`` is a ``Callable[[], ExperimentClient]`` — the injectable seam
    (the detection ``lister`` precedent, a plain callable, not a Protocol). The
    orchestration, in order:

    1. **Resume-first (C1.1, no network).** If ``context.run_row["experiment_id"]``
       is already set, return SUCCESS reusing it with **zero** LabKey contact —
       ``make_client`` is never called, so a resume succeeds even if LabKey is
       down or its config is absent.
    2. **Lookup-before-create (C1.3, ⚑1 — the crash-window guard).** Otherwise
       build the client and ``find_experiment_by_run_name`` first; only a ``None``
       find leads to ``create_experiment``. A restart after "created in LabKey but
       daemon died before persist" thus *adopts* the existing experiment instead
       of minting a duplicate.
    3. **Fail closed (C5).** Any ``LabKeyError`` from find OR create → a FAILURE
       result naming the run (the daemon quarantines). An erroring or ambiguous
       lookup never falls through to create.

    Only ``LabKeyError`` is caught — the adapter wraps every world failure in it,
    so a non-``LabKeyError`` is a bug and must propagate (the daemon's own
    call-site guard turns even that into a quarantine, but it is not swallowed
    here). The metadata key ``"experiment_id"`` matches ``EXPERIMENT.persists`` so
    the daemon can persist it; a mismatch would quarantine via the substrate's
    ADR-0006 guard.

    Precondition (F5): reads ``context.run_row["experiment_id"]`` and ``["name"]``
    by subscript — both are real ``runs`` columns present in every snapshot
    ``resolve_context`` builds (``experiment_id`` since migration 001; ``name`` is
    the primary key). A direct-construction test MUST populate BOTH keys; a bare
    ``KeyError`` from an under-populated test ``run_row`` is a test-setup miss, not
    a gate bug.
    """
    existing = context.run_row["experiment_id"]
    if existing is not None:
        # Resume path: the id is already persisted — reuse it, touch nothing.
        return GateResult(GateOutcome.SUCCESS, metadata={"experiment_id": existing})

    run_name = context.run_row["name"]
    client = make_client()  # only now — construct the adapter
    try:
        found = client.find_experiment_by_run_name(run_name)  # ⚑1 crash-window guard
        experiment_id = (
            found if found is not None else client.create_experiment(run_name)
        )
    except LabKeyError as labkey_error:
        # Fail closed: quarantine, never a fall-through to a duplicate create.
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"experiment gate: LabKey resolution failed for run "
                f"{run_name!r}: {labkey_error}"
            ),
        )
    return GateResult(GateOutcome.SUCCESS, metadata={"experiment_id": experiment_id})


def _run_experiment(context: Any) -> GateResult:
    """Production wiring: build the adapter from injected config, then orchestrate.

    The gate builds its own adapter here (no daemon seam, unlike detection —
    ``sluice.gates`` may import the ``labkey`` package). ``functools.partial``
    defers ``make_experiment_client(context.config)`` behind the zero-arg
    ``make_client`` seam ``run_experiment_gate`` calls, so on the resume path the
    partial is never invoked and no config or secret is touched.
    """
    return run_experiment_gate(
        context, functools.partial(make_experiment_client, context.config)
    )


def _invalidate_experiment(context: Any) -> None:
    """A deliberate NO-OP (⚑C / ADR-0005) — it must NOT clear ``experiment_id``.

    Production ``invalidate`` is never invoked automatically (ADR-0005 d.3); it
    remains a dormant hook. This is the one gate where ``produces`` names a value
    ``invalidate`` must **never** touch: clearing ``experiment_id`` would orphan a
    real LabKey experiment and its on-disk artifacts (B2). Structural safety backs
    this — rewind is column-clear only and never touches ``experiment_id``
    (ADR-0005 d.2), so a reset of ``labkey_registered`` preserves the id and the
    resume-first gate reuses it. Returns ``None``, touches no client, raises
    nothing.
    """
    return None


EXPERIMENT = Gate(
    id="experiment",
    name="LabKey experiment creation",
    satisfied_column="labkey_registered",  # migration 007 (plan 13 Unit 1)
    order=0,  # TRUE execution order — first
    needs=(),  # order 0, depends on nothing
    produces=(
        "experiment_id",
    ),  # NON-EMPTY on purpose: arms control B2 (control.py:1044)
    run=_run_experiment,
    invalidate=_invalidate_experiment,  # deliberate no-op (⚑C / ADR-0005)
    terminate=None,  # not interruptible (⚑E — later)
    persists={"experiment_id": "experiment_id"},  # ⚑B / ADR-0006 d.1
)
