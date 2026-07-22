"""Reference (mock) gates — pure functions of ``(context.workdir, filesystem)``.

The duck-typed context contract these gates rely on — and the contract
subsystem 4's ``RunContext`` inherits — is exactly one attribute, ``workdir``
(a ``pathlib.Path``); plan 02 §3.4. Gates never write run state (ADR-0002
decision 2): everything here works on the filesystem and *returns* results,
so ``sqlite3`` and ``sluice.core.db`` are forbidden imports here.
"""

from typing import Any

from sluice.core.gate import Gate, GateOutcome, GateResult

# The documented public surface (plan 02 §1, unit 7) — exactly these three;
# the ``_run_*``/``_invalidate_*`` helpers stay private so the Gate records
# remain the only supported handles.
__all__ = ["MOCK_ALPHA", "MOCK_BETA", "MOCK_GATES"]

# One name for the marker so ``produces`` and ``invalidate`` cannot drift
# apart (errata B2: produces names exactly what invalidate clears).
_MOCK_ALPHA_MARKER_FILENAME = "mock_alpha.marker"


def _run_mock_alpha(context: Any) -> GateResult:
    """Touch the alpha marker under ``context.workdir`` and return SUCCESS.

    Idempotent by design (design §4, "idempotent enough to re-enter"): an
    existing marker is overwritten, never an error. The marker path travels in
    ``metadata`` so the loop's persist step has something real to record —
    this function never writes run state itself (ADR-0002 decision 2).

    Raises:
        OSError: if the marker cannot be written under ``context.workdir``.
    """
    marker_path = context.workdir / _MOCK_ALPHA_MARKER_FILENAME
    marker_path.touch()
    return GateResult(GateOutcome.SUCCESS, metadata={"marker": str(marker_path)})


def _invalidate_mock_alpha(context: Any) -> None:
    """Delete the alpha marker; a missing marker is not an error.

    ``missing_ok`` because a ``reset_gate`` on a never-run gate must not crash
    (plan 02 §3.3); the caller — never this function — clears the satisfaction
    column in its own transaction (ADR-0002 decision 2).

    Raises:
        OSError: if the marker exists but cannot be removed.
    """
    (context.workdir / _MOCK_ALPHA_MARKER_FILENAME).unlink(missing_ok=True)


MOCK_ALPHA = Gate(
    id="mock_alpha",
    name="Mock alpha (touch-file reference gate)",
    satisfied_column="mock_alpha_done",
    order=1,
    needs=(),
    produces=(_MOCK_ALPHA_MARKER_FILENAME,),
    run=_run_mock_alpha,
    invalidate=_invalidate_mock_alpha,
    terminate=None,  # not interruptible (plan 02 §3.3)
)

# Beta's file names, single-sourced for the same B2 reason as alpha's.
_MOCK_BETA_INSTRUCTION_FILENAME = "mock_beta.instruction"
_MOCK_BETA_MARKER_FILENAME = "mock_beta.marker"
_MOCK_BETA_TERMINATED_FILENAME = "mock_beta.terminated"


def _run_mock_beta(context: Any) -> GateResult:
    """Dispatch on the beta instruction file's stripped content (plan 02 §3.3).

    An absent instruction file means plain SUCCESS — the daemon's ordinary,
    unscripted path. Only SUCCESS writes ``mock_beta.marker``: a marker left by
    a scripted non-success would let a later invalidate-then-rerun cycle
    mistake it for completed work. An unrecognized instruction (including an
    empty file) is FAILURE carrying the literal bad text — fail-loud, never a
    silent fallback (guardrail §8). Never writes run state (ADR-0002).

    Raises:
        OSError: if the instruction file cannot be read or the marker cannot
            be written under ``context.workdir``.
    """
    instruction_path = context.workdir / _MOCK_BETA_INSTRUCTION_FILENAME
    try:
        instruction = instruction_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        # Nobody scripted an outcome; the reference gate simply succeeds.
        instruction = "success"

    if instruction == "success":
        (context.workdir / _MOCK_BETA_MARKER_FILENAME).touch()
        return GateResult(GateOutcome.SUCCESS)
    if instruction == "failure":
        return GateResult(
            GateOutcome.FAILURE,
            reason="scripted failure ('failure' in mock_beta.instruction)",
        )
    if instruction == "held":
        return GateResult(GateOutcome.HELD)
    if instruction == "skipped":
        return GateResult(GateOutcome.SKIPPED)
    return GateResult(
        GateOutcome.FAILURE,
        reason=f"unrecognized mock_beta.instruction content: {instruction!r}",
    )


def _terminate_mock_beta(context: Any) -> None:
    """Touch the terminated trace file; report nothing (plan 02 §3.3).

    Terminate never returns a ``GateResult`` (spec §4, decision A): the loop
    already recorded the hold, so a reported outcome would invite a second,
    conflicting persist. The trace file — never ``mock_beta.marker``, which
    would let a rerun mistake interruption for completed work — is what lets
    daemon/control tests assert termination happened by reading state alone.

    Raises:
        OSError: if the trace file cannot be written under ``context.workdir``.
    """
    (context.workdir / _MOCK_BETA_TERMINATED_FILENAME).touch()


def _invalidate_mock_beta(context: Any) -> None:
    """Delete the beta marker and the terminated trace; missing files are fine.

    Both of beta's ``produces`` go (plan 02 §3.3): a stale terminated trace
    surviving a reset would pollute a re-run's forensics — state readers would
    conclude the fresh run was interrupted when it never was. ``missing_ok``
    because a ``reset_gate`` on a never-run gate must not crash, and the caller
    — never this function — clears the satisfaction column (ADR-0002
    decision 2).

    Raises:
        OSError: if an artifact exists but cannot be removed.
    """
    (context.workdir / _MOCK_BETA_MARKER_FILENAME).unlink(missing_ok=True)
    (context.workdir / _MOCK_BETA_TERMINATED_FILENAME).unlink(missing_ok=True)


MOCK_BETA = Gate(
    id="mock_beta",
    name="Mock beta (scriptable reference gate)",
    satisfied_column="mock_beta_done",
    order=2,
    needs=("mock_alpha",),
    produces=(_MOCK_BETA_MARKER_FILENAME, _MOCK_BETA_TERMINATED_FILENAME),
    run=_run_mock_beta,
    invalidate=_invalidate_mock_beta,
    terminate=_terminate_mock_beta,  # interruptible (plan 02 §3.3)
)

# The reference registry downstream subsystems consume (profiles, reconcile).
# Listed in TRUE execution order (errata B3) so ``validate_gates`` returns it
# unchanged — the pattern Plan B's production registry must follow (plan 02
# §3.3, unit 7).
MOCK_GATES = (MOCK_ALPHA, MOCK_BETA)
