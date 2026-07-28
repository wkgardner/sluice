"""The pure decision engine — next gate and completion from ``(run_row, profile)``.

Reconcile decides, the daemon persists (ADR-0002; design §7): everything here
is a pure function of the state row and the resolved ``Profile`` it is handed.
This module's imports are a closed set — stdlib plus ``sluice.core.gate``/
``sluice.core.profile`` only (plan 06 §1) — so it structurally cannot reach the
DB, a clock, entropy, or the network: purity is a property of the import
surface, enforced by ``tests/test_reconcile.py`` and
``tests/test_dependency_arrows.py``, not a promise.
"""

from sluice.core.gate import Gate
from sluice.core.profile import Applicability, Profile, required_gates

__all__ = ["is_complete", "next_gate"]


def _require_gate_columns_present(run_row, profile: Profile) -> None:
    """Raise ``KeyError`` naming every profile-gate ``satisfied_column`` absent from ``run_row``.

    The up-front fail-loud pre-check (plan 06 §3.2, option (a)): both decision
    functions call this before reading any column, because ``is_complete``'s
    ``all()`` short-circuits — a drifted column after an unsatisfied gate would
    otherwise go unread and the registry/schema drift would pass silently as
    "not complete" (guardrail §8: no silent fallback). Presence goes through
    ``run_row.keys()``, never ``column in run_row`` — on a ``sqlite3.Row``,
    ``in`` tests *value* membership, and indexing a missing column raises
    ``IndexError``, so this pre-check is also what makes ``dict`` and
    ``sqlite3.Row`` raise the same ``KeyError`` (§3.1 trap).

    Raises:
        KeyError: one or more of the profile's gates' ``satisfied_column``s is
            not a column of ``run_row``; the message names all missing columns.
    """
    missing_columns = [
        gate.satisfied_column
        for gate in profile.gates
        if gate.satisfied_column not in run_row.keys()
    ]
    if missing_columns:
        raise KeyError(
            f"run row is missing gate column(s) {missing_columns} required by "
            f"profile {profile.pipeline_version!r} — a registry/schema drift"
        )


def is_complete(run_row, profile: Profile) -> bool:
    """Return whether every contract gate's ``satisfied_column`` is truthy in ``run_row``.

    The completion predicate (design §7 read through ADR-0004): complete means
    every gate of the run's own contract — ``required_gates(profile)``, the
    non-disabled set — has a truthy column, so one unsatisfied contract gate
    blocks completion (the reconcile half of errata C4) and a disabled gate
    never does (satisfied by absence, ADR-0004 d.2). An all-disabled profile is
    vacuously complete (``all([])``). Completion is re-derived from columns on
    every call — ``completed_at`` is a derived cache, never an input (§5) —
    and a pure function of ``(run_row, profile)`` only (plan 06 fact 1).

    Raises:
        KeyError: propagated from the up-front presence pre-check — a gate
            column named by ``profile`` is absent from ``run_row``.
    """
    _require_gate_columns_present(run_row, profile)
    # Truthy read (§3.2): columns are INTEGER NOT NULL DEFAULT 0, and bool()
    # also treats a defensive NULL as unsatisfied.
    return all(bool(run_row[gate.satisfied_column]) for gate in required_gates(profile))


def next_gate(run_row, profile: Profile) -> Gate | None:
    """Return the lowest-``order`` runnable contract gate, or ``None`` when complete.

    The selection rule (design §7 read through ADR-0004; plan 06 §3.2): the
    first gate of ``profile.gates`` — already ascending by ``Gate.order``, the
    sole ordering authority (B3) — that is in the contract (not ``DISABLED``),
    has a falsy ``satisfied_column``, and has every need satisfied. A need is
    satisfied iff its gate is ``DISABLED`` (satisfied by absence, ADR-0004 d.2
    — its column stays ``0``) or its column is truthy (§3.3). ``None`` is the
    completion signal: ``next_gate is None ⟺ is_complete`` (the §3.3 theorem),
    so the daemon stamps ``completed_at`` on it. Pure function of
    ``(run_row, profile)`` only — no pointer, eligibility, or clock input
    (plan 06 facts 1 and 5; §2 pointer decision).

    Raises:
        KeyError: propagated from the up-front presence pre-check — a gate
            column named by ``profile`` is absent from ``run_row``.
    """
    _require_gate_columns_present(run_row, profile)
    # Need columns are looked up via the need gate's satisfied_column;
    # validate_gates guarantees every need id resolves to a gate in the set.
    gates_by_id = {gate.id: gate for gate in profile.gates}
    for gate in profile.gates:
        if profile.applicability[gate.id] is Applicability.DISABLED:
            continue  # (a) out of the contract — never selected (fact 4)
        if bool(run_row[gate.satisfied_column]):
            continue  # (b) already satisfied
        every_need_satisfied = all(
            profile.applicability[need_id] is Applicability.DISABLED
            or bool(run_row[gates_by_id[need_id].satisfied_column])
            for need_id in gate.needs
        )
        if not every_need_satisfied:
            continue  # (c) blocked — a contract need is still unsatisfied
        return gate
    return None
