"""The gate contract — the task object the whole system hangs off.

Everything here is pure data and vocabulary (design of record §4); behavior
lives in the functions a ``Gate`` references, never in methods. This module is
stdlib-only: ``gates -> core`` is one-way, so nothing here may import
``sluice.gates``/``api``/``cli`` or third-party code
(``tests/test_dependency_arrows.py`` enforces it).
"""

import dataclasses
import enum
from collections.abc import Callable, Iterable
from typing import Any

__all__ = [
    "Gate",
    "GateOutcome",
    "GateResult",
    "validate_gates",
]


class GateOutcome(enum.Enum):
    """Closed vocabulary for what a gate's ``run`` reports back to the loop.

    Exactly these four members, by design: the values are the persisted/logged
    strings (design §4), and the closed set is the surface of ADR-0002
    decision 1 — every FAILURE quarantines, so no ``TRANSIENT``/``RETRY``
    member may be added without a new ADR.
    """

    SUCCESS = "success"  # loop may set satisfied_column = 1
    FAILURE = "failure"  # loop quarantines: errored_at + error_reason (ADR-0002)
    HELD = "held"  # terminated mid-run by a hold; loop writes nothing
    SKIPPED = "skipped"  # gate judged itself inapplicable at runtime


@dataclasses.dataclass(frozen=True)
class GateResult:
    """What a gate's ``run`` returns to the loop — outcome plus optional detail.

    Exactly these three fields (design §4): no ``retryable``, no ``attempts`` —
    ADR-0002 decision 1 quarantines every failure, so retry policy has no
    surface here. Gates only *return* results; the loop persists them
    (ADR-0002 decision 2), including ``metadata`` per its own policy.
    """

    outcome: GateOutcome
    reason: str | None = None
    metadata: dict | None = None

    def __post_init__(self) -> None:
        """Reject a non-``GateOutcome`` outcome and a reasonless FAILURE.

        The loop branches on ``outcome`` with ``is`` against the enum members,
        so a stray string — even a valid enum *value* like ``"success"`` —
        would match no branch and silently mis-branch at persist time; and the
        schema CHECK ``(error_reason IS NULL) = (errored_at IS NULL)`` makes a
        reasonless FAILURE unpersistable at quarantine time (plan 02 §3.1–3.2).
        Both fail here at construction — including a whitespace-only reason,
        which would satisfy the CHECK while telling the operator nothing.

        Raises:
            TypeError: if ``outcome`` is not a ``GateOutcome`` member.
            ValueError: if ``outcome`` is FAILURE and ``reason`` is absent or
                whitespace-only.
        """
        # Type check first: the FAILURE guard below compares with `is`, so a
        # string outcome would silently skip it and construct reasonless.
        if not isinstance(self.outcome, GateOutcome):
            raise TypeError(
                f"GateResult.outcome must be a GateOutcome member, "
                f"got {self.outcome!r} (plan 02 §3.1: the loop branches "
                f"with `is` on the enum, so a bare value mis-branches)"
            )
        if self.outcome is GateOutcome.FAILURE and (
            self.reason is None or not self.reason.strip()
        ):
            raise ValueError(
                "a FAILURE GateResult requires a non-empty reason "
                "(plan 02 §3.2: the quarantine write cannot persist without one)"
            )


@dataclasses.dataclass(frozen=True)
class Gate:
    """The task object — pure data plus function references, never methods.

    Exactly these ten fields (plan 02 §3.1): no retry fields (B4/ADR-0002),
    no applicability (that lives in the profile, subsystem 5), no timeout
    (B6, deferred). ``order`` is TRUE execution order, never display/column
    numbering — transcribing python-poller's ``PIPELINE_GATE_COLUMNS`` into
    ``order`` deadlocks the daemon (errata B3/A6). ``needs`` and ``produces``
    carry no default: a gate author must state dependencies explicitly.
    ``run`` returns a result and never writes run state; the loop is the sole
    writer (ADR-0002 decision 2).
    """

    id: str  # stable key, e.g. "mock_alpha"
    name: str  # human label
    satisfied_column: str  # runs column recording completion (1 = satisfied)
    order: int  # TRUE execution order (B3/A6) — never column order
    needs: tuple[str, ...]  # gate ids that must be satisfied first
    produces: tuple[str, ...]  # artifacts/state created (invalidate + set_gate, B2)
    run: Callable[[Any], GateResult]  # THE work; returns, never writes (ADR-0002)
    invalidate: Callable[[Any], None]  # undo produces on disk; caller clears column
    terminate: Callable[[Any], None] | None = None  # stop in-flight work on hold
    control_point: bool = True  # daemon re-checks control state at checkpoints

    def __post_init__(self) -> None:
        """Coerce ``needs``/``produces`` to tuples and reject a self-need.

        A frozen record is only as immutable as its weakest field (plan 02
        §3.1): a list passed at construction and kept by reference could be
        mutated later, changing this Gate's dependency graph from the outside.
        A gate that needs itself can never run — an undetectable reconcile
        deadlock at daemon runtime — and construction is the only place the
        contradiction is locally checkable (plan 02 unit 3), so it fails here.

        Raises:
            TypeError: if ``needs`` or ``produces`` is not iterable.
            ValueError: if ``needs`` contains this gate's own ``id``.
        """
        # object.__setattr__ is the one sanctioned route past the freeze
        # during construction.
        object.__setattr__(self, "needs", tuple(self.needs))
        object.__setattr__(self, "produces", tuple(self.produces))
        if self.id in self.needs:
            raise ValueError(
                f"gate {self.id!r} lists itself in needs — a self-need can "
                f"never be satisfied (plan 02 unit 3: reconcile deadlock)"
            )


def validate_gates(gates: Iterable[Gate]) -> tuple[Gate, ...]:
    """Validate a gate set as a whole and return it sorted ascending by ``order``.

    The returned tuple is the execution sequence subsystems 5/6 consume, so a
    registry author's listing order never leaks into scheduling — this is the
    mechanical B3 guard (plan 02 §2): transcribing python-poller's
    ``PIPELINE_GATE_COLUMNS`` (``poller_state_db.py:32-42``, download listed
    before the registration that must execute first) fails here instead of
    deadlocking the daemon. There is deliberately no cycle detection: orders
    are unique and every need strictly precedes its needer, so the gate graph
    is acyclic by construction — the strict order is the topological witness.

    Raises:
        ValueError: naming the offending gate id(s), for duplicate ``id``s,
            duplicate ``satisfied_column``s, duplicate ``order`` values, a
            ``need`` naming a gate not in the set, or a ``need`` whose
            ``order`` is not strictly lower than its needer's.
    """
    ordered_gates = tuple(sorted(gates, key=lambda gate: gate.order))

    # Whole-set uniqueness: a duplicate id makes the registry ambiguous, a
    # shared satisfied_column marks two gates done with one write, and equal
    # orders make "lowest order first" (spec §7) nondeterministic.
    for field_name in ("id", "satisfied_column", "order"):
        gate_id_by_value: dict[object, str] = {}
        for gate in ordered_gates:
            value = getattr(gate, field_name)
            if value in gate_id_by_value:
                raise ValueError(
                    f"gates {gate_id_by_value[value]!r} and {gate.id!r} share "
                    f"{field_name}={value!r} — every gate's {field_name} must "
                    f"be unique in the set"
                )
            gate_id_by_value[value] = gate.id

    order_by_gate_id = {gate.id: gate.order for gate in ordered_gates}
    for gate in ordered_gates:
        for needed_gate_id in gate.needs:
            if needed_gate_id not in order_by_gate_id:
                raise ValueError(
                    f"gate {gate.id!r} needs {needed_gate_id!r}, which is not "
                    f"in the gate set"
                )
            if order_by_gate_id[needed_gate_id] >= gate.order:
                raise ValueError(
                    f"gate {gate.id!r} (order {gate.order}) needs "
                    f"{needed_gate_id!r} (order "
                    f"{order_by_gate_id[needed_gate_id]}), which does not "
                    f"strictly precede it — order is TRUE execution order "
                    f"(errata B3), never column-listing order"
                )

    return ordered_gates
