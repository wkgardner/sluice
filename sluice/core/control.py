"""Operator control ops — pure functions that mutate state, audit, emit, and return.

Control is state-only (design §2/§9): every op is
``op(db_path, actor, ...) -> ControlResult``, touching exactly one thing —
the database — in one short transaction, with its audit row committed
atomically and its event emitted after. Imports are a closed set (plan 08
§1): stdlib ``dataclasses``/``json``/``types``/``sqlite3``/``collections.abc``
plus the built core modules control composes (``db``, ``audit``, ``events``,
``profile``, ``reconcile``, ``gate``). With ``os``/``shutil``/``pathlib``/
``sluice.core.context`` unimportable no op has a route to a file (ADR-0005
made structural), and with ``subprocess``/``signal``/``socket`` unimportable
no op can start or stop work — only the daemon acts.
"""

import dataclasses
import json
import sqlite3
import types
from collections.abc import Mapping

from sluice.core.audit import record_control_action
from sluice.core.db import (
    get_connection,
    get_metadata,
    get_run,
    is_daemon_eligible,
    now_iso,
    set_metadata,
    update_run,
    write_txn,
)
from sluice.core.events import control_event, run_outcome
from sluice.core.profile import (
    Applicability,
    UnknownProfileError,
    apply_exclusions,
    load_profile,
    required_gates,
)
from sluice.core.reconcile import is_complete

__all__: list[str] = [
    "ActiveRunError",
    "ControlError",
    "ControlResult",
    "FabricationError",
    "LifecycleError",
    "PAUSED_KEY",
    "PIPELINE_PROFILE_KEY",
    "ProtectedError",
    "UnknownGateError",
    "UnknownRunError",
    "backtrack",
    "clear_error",
    "exclude_gate",
    "get_status",
    "hold",
    "include_gate",
    "is_paused",
    "pause",
    "protect",
    "reset",
    "reset_gate",
    "resume",
    "run_exclusions",
    "set_default_profile",
    "set_gate",
    "skip",
    "unhold",
    "unprotect",
    "unskip",
]

# The B5 metadata vocabulary control owns (plan 08 §2): the literal key the
# daemon's loop-head check reads; values "1" (paused) / "0" (not paused).
PAUSED_KEY = "paused"

# Spec §5's metadata key for the active-default profile id — DB metadata,
# never config (plan 08 §2). Named here so the vocabulary has one home.
PIPELINE_PROFILE_KEY = "pipeline_profile"


def is_paused(conn: sqlite3.Connection) -> bool:
    """Return True iff ``poller_metadata['paused']`` is the literal ``"1"``.

    The single canonical parse of the B5 value grammar (plan 08 §2): key
    absent and ``"0"`` both mean not paused, and no other value is granted
    pause power — a garbled metadata write is ignored, never a fleet pause.
    The daemon's loop head (subsystem 9) reuses exactly this function; a
    second parse site could diverge.
    """
    return get_metadata(conn, PAUSED_KEY) == "1"


class ControlError(RuntimeError):
    """Root of the audited-refusal family: a policy refusal of a real action.

    The exception's family is the audited/not-audited line (plan 08 §2): every
    op audits (``result="refused: <why>"``) and emits a ``ControlError`` before
    raising it, because a refused attempt on real state is exactly what the
    trail is for (spec §5). Plan C maps the whole family to one conflict
    status.
    """


class ProtectedError(ControlError):
    """Refusal of a progress-destroying op on a protected run without ``force`` (spec §9, errata A3)."""


class FabricationError(ControlError):
    """Refusal of ``set_gate`` on a gate with non-empty ``produces`` without ``force`` — a state-only op can fabricate no artifact (errata B2)."""


class ActiveRunError(ControlError):
    """Refusal of a rewind op while the run is daemon-eligible — an in-flight persist would immediately re-stamp the cleared column (plan 08 §2 residual posture)."""


class LifecycleError(ControlError):
    """Refusal of a cross-disposition lifecycle write that would silently undo another operator's disposition (plan 08 §2 matrix)."""


class UnknownRunError(KeyError):
    """Validation failure: the named run does not exist.

    Deliberately outside the ``ControlError`` family (plan 08 §2): a typo'd
    name identifies no real state to act on, so it is never audited and never
    emitted — auditing it would seed the trail with junk ``target_run``
    values.
    """


class UnknownGateError(KeyError):
    """Validation failure: the named gate is absent from the run's profile.

    Deliberately outside the ``ControlError`` family (plan 08 §2): a typo'd
    name identifies no real state to act on, so it is never audited and never
    emitted.
    """


@dataclasses.dataclass(frozen=True)
class ControlResult:
    """Frozen record of one committed control action (plan 08 §2, refining spec §9).

    The record *reports* what was committed; it is never authoritative state
    (design §2 — the database is). ``changes`` is snapshotted and sealed at
    construction (``MappingProxyType`` over a private ``dict`` copy, the
    RunContext/Profile house pattern), so neither a later item-write nor
    mutation of the constructing mapping can rewrite the report.

    Args:
        target_run: ``None`` for global ops (pause/resume/set_default_profile).
        status: ``"ok"`` or ``"noop"`` on any returned result — refusals
            raise, so a ``"refused"`` record rides only the event payload.
        changes: column/key -> value written; empty on noop.
        audit_id: the ``control_audit`` row recording this action.
    """

    action: str
    actor: str
    target_run: str | None
    status: str
    message: str | None
    changes: Mapping[str, object]
    audit_id: int

    def __post_init__(self) -> None:
        """Seal ``changes`` as a read-only snapshot detached from the caller's mapping."""
        object.__setattr__(self, "changes", types.MappingProxyType(dict(self.changes)))

    def to_fields(self) -> dict[str, object]:
        """Return the result as a plain JSON-shaped dict with exactly the seven pinned keys.

        The payload for ``control_event(result=...)`` and Plan C's response
        body (plan 08 §2) — key drift here is API drift. ``changes`` comes
        back as a plain ``dict`` because the sealed proxy is not JSON
        serializable.
        """
        return {
            "action": self.action,
            "actor": self.actor,
            "target_run": self.target_run,
            "status": self.status,
            "message": self.message,
            "changes": dict(self.changes),
            "audit_id": self.audit_id,
        }


def _write_paused_flag(
    db_path,
    actor,
    *,
    action: str,
    desired_value: str,
    noop_why: str,
    reason: str | None,
) -> ControlResult:
    """Run the §2 binding choreography for one paused-flag write and return the committed record.

    The pattern every mutating op repeats: decide (ok | noop) and mutate
    inside ONE ``write_txn`` sharing ONE ``now_iso`` instant with the audit
    row — state + audit land atomically or not at all — then emit the single
    ``control.finished`` event only after commit (persist-then-emit, plan 03
    §7). The noop half of the decision step skips the mutate entirely (a
    redundant write would turn every noop into a phantom ok) but still audits
    ``"noop: <why>"`` and still emits — §5's "every control action" includes
    redundant attempts.

    Raises:
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    shared_audit_instant = now_iso()
    with write_txn(db_path) as connection:
        # is_paused is the ONE parse site of the B5 grammar, so a resume on a
        # never-paused database (key absent) is a noop that upserts nothing —
        # not a write of "0".
        currently_paused = is_paused(connection)
        desired_paused = desired_value == "1"
        state_already_matches = currently_paused == desired_paused
        if state_already_matches:
            status = "noop"
            message = noop_why
            changes: dict[str, object] = {}
            audit_result = f"noop: {noop_why}"
        else:
            set_metadata(connection, PAUSED_KEY, desired_value)
            status = "ok"
            message = None
            changes = {PAUSED_KEY: desired_value}
            audit_result = "ok"
        audit_id = record_control_action(
            connection,
            actor=actor,
            action=action,
            target_run=None,
            args={"reason": reason},
            result=audit_result,
            ts=shared_audit_instant,
        )
    # Committed: state + audit landed together. Only now may an event
    # describe the write (plan 03 §7 — no event for an uncommitted state).
    result = ControlResult(
        action=action,
        actor=actor,
        target_run=None,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
    )
    control_event(action, actor, target_run=None, result=result.to_fields())
    return result


def pause(db_path, actor, *, reason: str | None = None) -> ControlResult:
    """Pause the whole pipeline by upserting ``poller_metadata['paused'] = "1"``, audited, and return the committed record.

    A global op: the audit row's ``target_run`` is SQL NULL and the event
    carries no ``run.name`` (a fabricated one would corrupt per-run jsonl
    routing). Already paused → an audited ``"noop: already paused"``, metadata
    untouched. State-only (B5's operator half): the daemon reads the flag at
    its loop head — nothing here stops in-flight work.

    Raises:
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    return _write_paused_flag(
        db_path,
        actor,
        action="pause",
        desired_value="1",
        noop_why="already paused",
        reason=reason,
    )


def resume(db_path, actor, *, reason: str | None = None) -> ControlResult:
    """Resume a paused pipeline by upserting ``poller_metadata['paused'] = "0"``, audited, and return the committed record.

    The mirror of ``pause``, same global-op choreography. Not currently paused
    (key absent or ``"0"``) → an audited ``"noop: not paused"`` that upserts
    nothing — the mutate step is skipped, not repeated with an equivalent
    value. Operator ``resume`` is the ONLY clear for a B5 disk-halt auto-pause
    (plan 08 §2 — the daemon never auto-clears).

    Raises:
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    return _write_paused_flag(
        db_path,
        actor,
        action="resume",
        desired_value="0",
        noop_why="not paused",
        reason=reason,
    )


def _transition_lifecycle(
    db_path,
    actor,
    run_name,
    *,
    action: str,
    target_lifecycle: str,
    source_lifecycle: str,
    noop_why: str,
    cross_disposition_why: str,
    reason: str | None,
    force: bool | None = None,
) -> ControlResult:
    """Run the §2 choreography for one disposition write under the plan 08 §2 lifecycle matrix and return (or raise) the committed record.

    ``lifecycle`` is ONE field, so the matrix exists to stop an op silently
    undoing another operator's disposition: already at ``target_lifecycle`` →
    an audited ``"noop: <why>"`` with the mutate skipped (the stored trio's
    provenance survives — a re-stamp would credit an actor who changed
    nothing); at ``source_lifecycle`` → the legitimate transition, writing
    the FULL trio (``lifecycle_by``/``lifecycle_at``/``lifecycle_reason``)
    at the shared audit instant; any other disposition → the audit row
    ``"refused: <why>"`` commits alone, the event (result status
    ``"refused"``) is emitted, THEN ``LifecycleError`` is raised — trail and
    stream record the blocked attempt while the stored disposition and its
    provenance survive untouched. Protection (A3) is checked first: on a
    protected run without ``force`` the same refusal choreography raises
    ``ProtectedError`` naming ``protected_by``; ``force=True`` bypasses that
    ONE refusal and never clears ``protected_*`` — ``unprotect`` is the sole
    clear path.

    Args:
        force: ``None`` marks an op OUTSIDE the A3 protected set (hold/
            unhold): no protection check runs and no ``force`` key rides the
            audit args — recording a parameter the op does not take would
            fabricate trail evidence. A bool marks a protected-set op.

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited —
            a typo identifies no real state to act on).
        ProtectedError: the run is protected and ``force`` is False (raised
            after its refusal audit row commits and its event is emitted).
        LifecycleError: the run holds a cross-disposition lifecycle (raised
            after its refusal audit row commits and its event is emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    shared_audit_instant = now_iso()
    refusal: ControlError | None = None
    changes: dict[str, object] = {}
    audit_args: dict[str, object] = {"reason": reason}
    if force is not None:
        audit_args["force"] = force
    with write_txn(db_path) as connection:
        run_row = get_run(connection, run_name)
        if run_row is None:
            # Raising inside write_txn rolls back: nothing written, no event.
            raise UnknownRunError(f"{action}: no run named {run_name!r}")
        current_lifecycle = run_row["lifecycle"]
        run_is_protected = run_row["protected_at"] is not None
        if force is not None and run_is_protected and not force:
            # Protection outranks the matrix — the same-state noop included
            # (review-pinned): "hands off" is informative where a noop is not.
            # The refusal names the protector because the operator's next move
            # is to talk to whoever guarded the run, not to retry.
            protection_why = (
                f"run is protected by {run_row['protected_by']} — "
                "unprotect it or pass force=True"
            )
            status = "refused"
            message = protection_why
            audit_result = f"refused: {protection_why}"
            refusal = ProtectedError(f"{action}: {protection_why}")
        elif current_lifecycle == target_lifecycle:
            status = "noop"
            message = noop_why
            audit_result = f"noop: {noop_why}"
        elif current_lifecycle != source_lifecycle:
            status = "refused"
            message = cross_disposition_why
            audit_result = f"refused: {cross_disposition_why}"
            refusal = LifecycleError(f"{action}: {cross_disposition_why}")
        else:
            changes = {
                "lifecycle": target_lifecycle,
                "lifecycle_by": actor,
                "lifecycle_at": shared_audit_instant,
                "lifecycle_reason": reason,
            }
            update_run(connection, run_name, changes)
            status = "ok"
            message = None
            audit_result = "ok"
        audit_id = record_control_action(
            connection,
            actor=actor,
            action=action,
            target_run=run_name,
            args=audit_args,
            result=audit_result,
            ts=shared_audit_instant,
        )
    # Committed: state + audit landed together. Only now may an event
    # describe the write (plan 03 §7 — no event for an uncommitted state).
    # Refusals emit too — the stream and the trail agree on every attempt —
    # but their record is raised, never returned (plan 08 §2).
    result = ControlResult(
        action=action,
        actor=actor,
        target_run=run_name,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
    )
    control_event(action, actor, target_run=run_name, result=result.to_fields())
    if refusal is not None:
        raise refusal
    return result


def hold(db_path, actor, run_name, *, reason: str | None = None) -> ControlResult:
    """Hold a run by writing ``lifecycle='held'`` with the full provenance trio, audited, and return the committed record.

    State-only (§7 decision A): holding is nothing but making the run fail
    ``ACTIVE_RUNS_WHERE`` — the daemon reacts at its next checkpoint (calling
    ``terminate`` is its job, never this op's). Every lifecycle transition
    writes the TRIO — ``lifecycle_by=actor``, ``lifecycle_at`` = the SAME
    ``now_iso()`` instant as the audit row's ``ts``, ``lifecycle_reason`` —
    never a bare flip, so the row always names its disposition's provenance;
    the §2 matrix governs the rest: already held → audited noop, skipped →
    audited-and-emitted refusal (unskip first). An unknown run rolls back
    having written nothing and emits nothing (validation, not refusal).

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited —
            a typo identifies no real state to act on).
        LifecycleError: the run is skipped — holding it would silently undo
            another operator's skip (raised after its refusal audit row
            commits and its event is emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    return _transition_lifecycle(
        db_path,
        actor,
        run_name,
        action="hold",
        target_lifecycle="held",
        source_lifecycle="active",
        noop_why="already held",
        cross_disposition_why="run is skipped — unskip first",
        reason=reason,
    )


def unhold(db_path, actor, run_name, *, reason: str | None = None) -> ControlResult:
    """Release a held run by writing ``lifecycle='active'`` with the full provenance trio, audited, and return the committed record.

    The mirror of ``hold`` under the §2 matrix: held → active re-stamps the
    FULL trio to the unholding actor (un-ops too, so the row always names
    its latest disposition's provenance — a bare flip would leave the holder
    cited as the author of its activeness); already active → audited noop;
    skipped → audited-and-emitted refusal (unskip first — a skip is revoked
    only by its own un-op). Activeness restores ``ACTIVE_RUNS_WHERE``
    eligibility only jointly with a clean error pair — quarantine is
    orthogonal and needs ``clear_error`` (plan 08 §2, F4).

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited —
            a typo identifies no real state to act on).
        LifecycleError: the run is skipped — unholding it would silently
            undo another operator's skip (raised after its refusal audit row
            commits and its event is emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    return _transition_lifecycle(
        db_path,
        actor,
        run_name,
        action="unhold",
        target_lifecycle="active",
        source_lifecycle="held",
        noop_why="not held",
        cross_disposition_why="run is skipped — unskip first",
        reason=reason,
    )


def skip(
    db_path, actor, run_name, *, reason: str | None = None, force: bool = False
) -> ControlResult:
    """Skip a run by writing ``lifecycle='skipped'`` with the full provenance trio, audited, and return the committed record.

    A skip is a disposition, never a fabricated completion (spec §9):
    ``completed_at`` stays SQL NULL — "we are done driving this run" and "the
    pipeline finished this run" are different facts in different columns, and
    stamping the latter would make ``run_outcome``/``get_status`` render an
    abandonment as completion. The stopping mechanism is state-only, exactly
    ``hold``'s: ``lifecycle='skipped'`` fails ``ACTIVE_RUNS_WHERE``, and the
    daemon simply stops selecting the run. The §2 matrix mirrors ``hold`` with
    roles swapped: already skipped → audited noop, held → audited-and-emitted
    refusal (unhold first). Skip is in the A3 protected set (it removes the
    run from the daemon's reach): a protected run refuses unless ``force``,
    and ``force`` bypasses that one refusal without clearing ``protected_*``.
    An unknown run rolls back having written nothing and emits nothing
    (validation, not refusal).

    Args:
        force: bypass the ``ProtectedError`` refusal on a protected run
            (recorded in the audit args); never clears protection —
            ``unprotect`` is the sole clear path (errata A3).

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited —
            a typo identifies no real state to act on).
        ProtectedError: the run is protected and ``force`` is False (raised
            after its refusal audit row commits and its event is emitted).
        LifecycleError: the run is held — skipping it would silently undo
            another operator's hold (raised after its refusal audit row
            commits and its event is emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    return _transition_lifecycle(
        db_path,
        actor,
        run_name,
        action="skip",
        target_lifecycle="skipped",
        source_lifecycle="active",
        noop_why="already skipped",
        cross_disposition_why="run is held — unhold first",
        reason=reason,
        force=force,
    )


def unskip(db_path, actor, run_name, *, reason: str | None = None) -> ControlResult:
    """Revoke a skip by writing ``lifecycle='active'`` with the full provenance trio, audited, and return the committed record.

    The mirror of ``skip`` under the §2 matrix, and ``unhold``'s counterpart
    with roles swapped: skipped → active re-stamps the FULL trio to the
    unskipping actor (un-ops write the trio too, so the row always names its
    latest disposition's provenance); already active → audited noop; held →
    audited-and-emitted refusal (unhold first — a hold is revoked only by its
    own un-op). Unlike ``skip``, unskip is NOT protection-gated (A3:
    protection blocks progress-destroying ops, and restoring a run to the
    daemon's reach destroys nothing), so there is no ``force`` parameter.
    Activeness restores ``ACTIVE_RUNS_WHERE`` eligibility only jointly with a
    clean error pair — quarantine is orthogonal and needs ``clear_error``
    (plan 08 §2, F4).

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited —
            a typo identifies no real state to act on).
        LifecycleError: the run is held — unskipping it would silently undo
            another operator's hold (raised after its refusal audit row
            commits and its event is emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    return _transition_lifecycle(
        db_path,
        actor,
        run_name,
        action="unskip",
        target_lifecycle="active",
        source_lifecycle="skipped",
        noop_why="not skipped",
        cross_disposition_why="run is held — unhold first",
        reason=reason,
    )


def clear_error(
    db_path, actor, run_name, *, reason: str | None = None
) -> ControlResult:
    """Forgive a run's quarantine by NULLing ``errored_at`` and ``error_reason`` together, audited, and return the committed record.

    ADR-0002's sole sanctioned recovery: quarantine holds until an operator
    runs exactly this op. One ``update_run`` clears the pair (the paired
    CHECK forbids a half-clear), which alone restores ``ACTIVE_RUNS_WHERE``
    eligibility — state-only recovery, the daemon's next stateless look
    re-selects the run, no poke. Eligibility composes (F4): a run both held
    and quarantined also needs ``unhold``. The cleared ``error_reason`` is
    recorded in the audit args (``cleared_error_reason``) because after the
    clear the run row no longer remembers what was forgiven — the trail is
    the only record. Already-clean run → an audited ``"noop: not
    quarantined"`` with the mutate skipped. Not protection-gated (A3:
    protection preserves progress; it never blocks recovery).

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited —
            a typo identifies no real state to act on).
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    shared_audit_instant = now_iso()
    audit_args: dict[str, object] = {"reason": reason}
    with write_txn(db_path) as connection:
        run_row = get_run(connection, run_name)
        if run_row is None:
            # Raising inside write_txn rolls back: nothing written, no event.
            raise UnknownRunError(f"clear_error: no run named {run_name!r}")
        if run_row["errored_at"] is None:
            status = "noop"
            message = "not quarantined"
            changes: dict[str, object] = {}
            audit_result = "noop: not quarantined"
        else:
            changes = {"errored_at": None, "error_reason": None}
            update_run(connection, run_name, changes)
            status = "ok"
            message = None
            audit_result = "ok"
            audit_args["cleared_error_reason"] = run_row["error_reason"]
        audit_id = record_control_action(
            connection,
            actor=actor,
            action="clear_error",
            target_run=run_name,
            args=audit_args,
            result=audit_result,
            ts=shared_audit_instant,
        )
    # Committed: state + audit landed together. Only now may an event
    # describe the write (plan 03 §7 — no event for an uncommitted state).
    result = ControlResult(
        action="clear_error",
        actor=actor,
        target_run=run_name,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
    )
    control_event("clear_error", actor, target_run=run_name, result=result.to_fields())
    return result


def _write_protection_pair(
    db_path,
    actor,
    run_name,
    *,
    action: str,
    desired_protected: bool,
    noop_why: str,
    reason: str | None,
) -> ControlResult:
    """Run the §2 choreography for one write of the paired protection columns and return the committed record.

    The pair moves TOGETHER in one ``update_run`` (migration 001's paired
    CHECK forbids a half-write): stamping sets ``protected_by=actor`` and
    ``protected_at`` = the SAME ``now_iso()`` instant as the audit row's
    ``ts``; clearing NULLs both. A run already in the desired protection
    state is an audited ``"noop: <why>"`` with the mutate SKIPPED — a
    re-stamp on the noop path would silently transfer protection ownership
    to whoever asked last, so the original protector's pair survives
    byte-identical (plan 08 unit 6).

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited —
            a typo identifies no real state to act on).
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    shared_audit_instant = now_iso()
    with write_txn(db_path) as connection:
        run_row = get_run(connection, run_name)
        if run_row is None:
            # Raising inside write_txn rolls back: nothing written, no event.
            raise UnknownRunError(f"{action}: no run named {run_name!r}")
        currently_protected = run_row["protected_at"] is not None
        if currently_protected == desired_protected:
            status = "noop"
            message = noop_why
            changes: dict[str, object] = {}
            audit_result = f"noop: {noop_why}"
        else:
            if desired_protected:
                changes = {
                    "protected_at": shared_audit_instant,
                    "protected_by": actor,
                }
            else:
                changes = {"protected_at": None, "protected_by": None}
            update_run(connection, run_name, changes)
            status = "ok"
            message = None
            audit_result = "ok"
        audit_id = record_control_action(
            connection,
            actor=actor,
            action=action,
            target_run=run_name,
            args={"reason": reason},
            result=audit_result,
            ts=shared_audit_instant,
        )
    # Committed: state + audit landed together. Only now may an event
    # describe the write (plan 03 §7 — no event for an uncommitted state).
    result = ControlResult(
        action=action,
        actor=actor,
        target_run=run_name,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
    )
    control_event(action, actor, target_run=run_name, result=result.to_fields())
    return result


def protect(db_path, actor, run_name, *, reason: str | None = None) -> ControlResult:
    """Guard a run by stamping ``protected_at`` and ``protected_by`` together, audited, and return the committed record.

    Errata A3's operator-owned protection (named ``protect``, never ``lock`` —
    ``locked_at`` is a dead mutex): once stamped, every progress-destroying op
    in the A3 protected set refuses with ``ProtectedError`` naming this
    ``protected_by`` unless forced. The pair is written in ONE ``update_run``
    (migration 001's paired CHECK is satisfied by construction), with
    ``protected_at`` = the SAME ``now_iso()`` instant as the audit row's
    ``ts``. Already protected — by anyone — is an audited ``"noop: already
    protected"`` that leaves the original protector's pair untouched. There
    is deliberately no ``protected_reason`` column (plan 08 unit 6): the
    operator's reason rides the audit args, the trail's sole record of the
    why. Not protection-gated itself and no ``force`` parameter exists —
    protection is operator-owned, and ``unprotect`` is the override.

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited —
            a typo identifies no real state to act on).
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    return _write_protection_pair(
        db_path,
        actor,
        run_name,
        action="protect",
        desired_protected=True,
        noop_why="already protected",
        reason=reason,
    )


def unprotect(db_path, actor, run_name, *, reason: str | None = None) -> ControlResult:
    """Lift a run's protection by NULLing ``protected_at`` and ``protected_by`` together, audited, and return the committed record.

    The mirror of ``protect``: both columns clear in ONE ``update_run``
    (migration 001's paired CHECK forbids a half-clear), restoring the A3
    protected set's ops without ``force``. Not protected → an audited
    ``"noop: not protected"`` with the mutate skipped. No ``force``
    parameter exists on either op — protection is operator-owned and
    unprotect IS the override (plan 08 §2 A3); a force flag would fabricate
    a second, stronger authority.

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited —
            a typo identifies no real state to act on).
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    return _write_protection_pair(
        db_path,
        actor,
        run_name,
        action="unprotect",
        desired_protected=False,
        noop_why="not protected",
        reason=reason,
    )


def set_default_profile(db_path, actor, pipeline_version, *, registry) -> ControlResult:
    """Set the active-default profile by upserting ``poller_metadata['pipeline_profile']``, audited, and return the committed record.

    The active default is DB metadata, never config (plan 05 §7 / plan 08
    §2), and it steers what FUTURE detections stamp — no existing run's
    ``pipeline_version`` is touched (§8's stamp-once freeze). ``load_profile``
    validates the version against the injected registry BEFORE any write, so
    an unresolvable version never becomes the default (fail-loud validation:
    not audited, not emitted — the §2 line). A global op: the audit row's
    ``target_run`` is SQL NULL and the event carries no ``run.name``.

    Args:
        registry: keyword-only and required — no default registry exists
            (guardrail §8; gates reach control only by injection).

    Raises:
        UnknownProfileError: ``pipeline_version`` is not in ``registry``
            (propagated from ``load_profile``; a ``KeyError``, raised before
            anything is written).
        ValueError: ``actor`` is blank (propagated from the audit writer —
            an anonymous audit row is worthless evidence).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``; ``OperationalError`` is a
            subclass).
    """
    # Validate BEFORE opening the transaction: the write is gated on the
    # registry resolving the version (plan 08 §2 — validate-then-upsert).
    load_profile(pipeline_version, registry)
    shared_audit_instant = now_iso()
    with write_txn(db_path) as connection:
        set_metadata(connection, PIPELINE_PROFILE_KEY, pipeline_version)
        audit_id = record_control_action(
            connection,
            actor=actor,
            action="set_default_profile",
            target_run=None,
            args={"pipeline_version": pipeline_version},
            result="ok",
            ts=shared_audit_instant,
        )
    # Committed: state + audit landed together. Only now may an event
    # describe the write (plan 03 §7 — no event for an uncommitted state).
    result = ControlResult(
        action="set_default_profile",
        actor=actor,
        target_run=None,
        status="ok",
        message=None,
        changes={PIPELINE_PROFILE_KEY: pipeline_version},
        audit_id=audit_id,
    )
    control_event(
        "set_default_profile", actor, target_run=None, result=result.to_fields()
    )
    return result


def _resolve_contract_gate(profile, gate_id, *, action: str):
    """Return the ``Gate`` of ``profile`` named ``gate_id``, or fail loud.

    The injection backstop's front door (plan 08 §4): a caller-supplied gate
    id becomes a column name only by resolving to a ``Gate`` record here, so
    a hostile id dies at this lookup before any SQL is built — validation,
    never audited (``update_run``'s whitelist is the second line).

    Raises:
        UnknownGateError: ``gate_id`` names no gate of ``profile``; the
            message names the profile's known gate ids.
    """
    for gate in profile.gates:
        if gate.id == gate_id:
            return gate
    raise UnknownGateError(
        f"{action}: profile {profile.pipeline_version!r} has no gate "
        f"{gate_id!r}; it carries {[gate.id for gate in profile.gates]}"
    )


def _protection_refusal_why(run_row, force: bool) -> str | None:
    """Return the A3 protection-refusal reason for ``run_row``, or ``None``.

    ``None`` when the run is unprotected or ``force`` is passed —
    ``force=True`` bypasses exactly this one refusal and never clears
    ``protected_*`` (errata A3: ``unprotect`` is the sole clear path). The
    reason names ``protected_by`` because the refused operator's next move is
    to talk to whoever guarded the run. Callers consult this BEFORE their
    same-state noop branch (protection-first, review-pinned): a protected run
    answers "hands off", which is informative where a noop is not.
    """
    if run_row["protected_at"] is None or force:
        return None
    return (
        f"run is protected by {run_row['protected_by']} — "
        "unprotect it or pass force=True"
    )


def _quiescence_refusal_why(run_row) -> str | None:
    """Return the rewind-family refusal reason for a daemon-eligible run, or ``None``.

    The §2 residual posture (⚑3 as superseded by ADR-0005) — a
    recommendation-backed guard, not file-safety: the predicate is exactly
    ``ACTIVE_RUNS_WHERE``, called through its Python twin
    ``db.is_daemon_eligible`` (plan 14) rather than re-typed here, so held,
    skipped, and quarantined runs all pass. Only
    ``reset_gate``/``backtrack``/``reset`` consult this; state-only ops carry
    no such precondition.
    """
    if is_daemon_eligible(run_row):
        return (
            "run is daemon-eligible — hold, skip, or wait for quarantine "
            "first: an in-flight gate's persist would immediately re-stamp "
            "the column this op just cleared"
        )
    return None


def _conclude(
    action: str,
    actor: str,
    target_run: str | None,
    *,
    status: str,
    message: str | None,
    changes: Mapping[str, object],
    audit_id: int,
    refusal: ControlError | None,
) -> ControlResult:
    """Build the frozen record, emit the one post-commit event, then raise or return.

    The shared tail of the §2 binding choreography, called only AFTER the
    op's ``write_txn`` committed: state + audit have landed together, so the
    single ``control.finished`` may describe them (persist-then-emit, plan 03
    §7). Refusals emit too — the stream and the trail agree on every attempt
    — but their record is raised, never returned.

    Raises:
        ControlError: ``refusal``, re-raised after the event is emitted.
    """
    result = ControlResult(
        action=action,
        actor=actor,
        target_run=target_run,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
    )
    control_event(action, actor, target_run=target_run, result=result.to_fields())
    if refusal is not None:
        raise refusal
    return result


def set_gate(
    db_path,
    actor,
    run_name,
    gate_id,
    *,
    registry,
    force: bool = False,
    reason: str | None = None,
) -> ControlResult:
    """Mark one contract gate's ``satisfied_column`` 1 without doing its work, audited, and return the committed record.

    Errata B2's no-fabrication mechanism (plan 08 §2/unit 8): a state-only op
    can fabricate NO artifact, so a gate with non-empty ``produces`` refuses
    with ``FabricationError`` naming the unfabricatable outputs unless
    ``force=True`` — the operator vouching for artifacts restored by hand
    (spec §9); a ``produces``-free gate sets freely. The gate resolves from
    the run's OWN stamped profile and the written column name comes from the
    ``Gate`` record, never the caller (injection defense, §4); a gate the
    profile marks ``DISABLED`` is refused out-of-contract, and an
    already-satisfied gate is an audited noop. In the A3 protected set.

    Args:
        registry: keyword-only and required — no default registry exists
            (guardrail §8; gates reach control only by injection).
        force: bypass the ``ProtectedError`` and ``FabricationError``
            refusals (recorded in the audit args); never clears protection.

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited).
        UnknownGateError: ``gate_id`` names no gate of the run's profile
            (never audited — a typo identifies no real state).
        UnknownProfileError: the run's stamped ``pipeline_version`` is not in
            ``registry`` (propagated from ``load_profile``; never audited).
        ProtectedError: the run is protected and ``force`` is False (raised
            after its refusal audit row commits and its event is emitted).
        FabricationError: the gate has non-empty ``produces`` and ``force``
            is False (raised after its refusal audit row commits and its
            event is emitted).
        ControlError: the gate is ``DISABLED`` in the run's profile — out of
            contract (raised after its refusal audit row commits and its
            event is emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``).
    """
    shared_audit_instant = now_iso()
    refusal: ControlError | None = None
    changes: dict[str, object] = {}
    with write_txn(db_path) as connection:
        run_row = get_run(connection, run_name)
        if run_row is None:
            # Raising inside write_txn rolls back: nothing written, no event.
            raise UnknownRunError(f"set_gate: no run named {run_name!r}")
        profile = load_profile(run_row["pipeline_version"], registry)
        gate = _resolve_contract_gate(profile, gate_id, action="set_gate")
        protection_why = _protection_refusal_why(run_row, force)
        if protection_why is not None:
            status = "refused"
            message = protection_why
            audit_result = f"refused: {protection_why}"
            refusal = ProtectedError(f"set_gate: {protection_why}")
        elif profile.applicability[gate.id] is Applicability.DISABLED:
            out_of_contract_why = (
                f"gate {gate_id!r} is out of contract — DISABLED in profile "
                f"{profile.pipeline_version!r}"
            )
            status = "refused"
            message = out_of_contract_why
            audit_result = f"refused: {out_of_contract_why}"
            refusal = ControlError(f"set_gate: {out_of_contract_why}")
        elif run_row[gate.satisfied_column]:
            status = "noop"
            message = "gate already satisfied"
            audit_result = "noop: gate already satisfied"
        elif gate.produces and not force:
            fabrication_why = (
                f"gate {gate_id!r} produces {list(gate.produces)} — a "
                "state-only op cannot fabricate them; pass force=True to "
                "vouch for artifacts restored by hand"
            )
            status = "refused"
            message = fabrication_why
            audit_result = f"refused: {fabrication_why}"
            refusal = FabricationError(f"set_gate: {fabrication_why}")
        else:
            changes = {gate.satisfied_column: 1}
            update_run(connection, run_name, changes)
            status = "ok"
            message = None
            audit_result = "ok"
        audit_id = record_control_action(
            connection,
            actor=actor,
            action="set_gate",
            target_run=run_name,
            args={"gate": gate_id, "reason": reason, "force": force},
            result=audit_result,
            ts=shared_audit_instant,
        )
    return _conclude(
        "set_gate",
        actor,
        run_name,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
        refusal=refusal,
    )


def run_exclusions(run_row) -> tuple[str, ...]:
    """Parse a run row's ``excluded_gates`` column into a tuple of gate ids.

    The single parse site of the ⚑2 overlay column: NULL means no exclusions;
    anything else must be a JSON array of strings. Any other shape fails loud
    (guardrail §8) — a silent ``()`` fallback would silently *unexclude*, and
    the daemon's contract resolution composes on exactly this function
    (plan 08 §6). Only ``get_status`` catches the error, per run.

    Raises:
        ValueError: the stored value is JSON but not an array of strings
            (``json.JSONDecodeError`` for invalid JSON is a subclass); the
            message names the bad value.
    """
    stored_value = run_row["excluded_gates"]
    if stored_value is None:
        return ()
    parsed = json.loads(stored_value)
    if not isinstance(parsed, list) or any(
        not isinstance(gate_id, str) for gate_id in parsed
    ):
        raise ValueError(
            f"excluded_gates must be NULL or a JSON array of gate ids; "
            f"got {stored_value!r} — refusing a silent unexclude (plan 08 "
            "unit 11)"
        )
    return tuple(parsed)


def exclude_gate(
    db_path,
    actor,
    run_name,
    gate_id,
    *,
    registry,
    reason: str | None = None,
    force: bool = False,
) -> ControlResult:
    """Remove one gate from the run's contract via the ``excluded_gates`` overlay, audited, and return the committed record.

    ADR-0004 d.3's remediation op (plan 08 §2 ⚑2): the column stores a
    sorted, deduplicated JSON array the daemon honors as
    ``apply_exclusions(load_profile(...), run_exclusions(row))`` — the
    profile itself is frozen and shared, so the overlay is per-run and the
    stamped ``pipeline_version`` never changes (§8). Reconcile is stateless:
    on the daemon's next look the run resumes past the excluded step with no
    poke. A gate already ``DISABLED`` by the profile is refused (nothing to
    exclude); an already-excluded gate is an audited noop. In the A3
    protected set — a contract edit is destructive.

    Args:
        registry: keyword-only and required — no default registry exists
            (guardrail §8).
        force: bypass the ``ProtectedError`` refusal (recorded in the audit
            args); never clears protection.

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited).
        UnknownGateError: ``gate_id`` names no gate of the run's profile
            (never audited).
        UnknownProfileError: the run's stamped ``pipeline_version`` is not in
            ``registry`` (propagated from ``load_profile``; never audited).
        ProtectedError: the run is protected and ``force`` is False (raised
            after its refusal audit row commits and its event is emitted).
        ControlError: the gate is already ``DISABLED`` in the run's profile
            (raised after its refusal audit row commits and its event is
            emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer), or
            the stored ``excluded_gates`` is corrupt (propagated from
            ``run_exclusions``; rollback, nothing written).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``).
    """
    shared_audit_instant = now_iso()
    refusal: ControlError | None = None
    changes: dict[str, object] = {}
    with write_txn(db_path) as connection:
        run_row = get_run(connection, run_name)
        if run_row is None:
            # Raising inside write_txn rolls back: nothing written, no event.
            raise UnknownRunError(f"exclude_gate: no run named {run_name!r}")
        profile = load_profile(run_row["pipeline_version"], registry)
        gate = _resolve_contract_gate(profile, gate_id, action="exclude_gate")
        current_exclusions = run_exclusions(run_row)
        protection_why = _protection_refusal_why(run_row, force)
        if protection_why is not None:
            status = "refused"
            message = protection_why
            audit_result = f"refused: {protection_why}"
            refusal = ProtectedError(f"exclude_gate: {protection_why}")
        elif profile.applicability[gate.id] is Applicability.DISABLED:
            already_out_why = (
                f"gate {gate_id!r} is already out of contract by profile "
                f"{profile.pipeline_version!r} — nothing to exclude"
            )
            status = "refused"
            message = already_out_why
            audit_result = f"refused: {already_out_why}"
            refusal = ControlError(f"exclude_gate: {already_out_why}")
        elif gate_id in current_exclusions:
            status = "noop"
            message = "gate already excluded"
            audit_result = "noop: gate already excluded"
        else:
            # Sorted + deduplicated: deterministic bytes for the same set of
            # exclusions (the audit-plan ethic — plan 08 unit 11).
            updated_exclusions = sorted({*current_exclusions, gate_id})
            changes = {"excluded_gates": json.dumps(updated_exclusions)}
            update_run(connection, run_name, changes)
            status = "ok"
            message = None
            audit_result = "ok"
        audit_id = record_control_action(
            connection,
            actor=actor,
            action="exclude_gate",
            target_run=run_name,
            args={"gate": gate_id, "reason": reason, "force": force},
            result=audit_result,
            ts=shared_audit_instant,
        )
    return _conclude(
        "exclude_gate",
        actor,
        run_name,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
        refusal=refusal,
    )


def include_gate(
    db_path,
    actor,
    run_name,
    gate_id,
    *,
    registry,
    reason: str | None = None,
    force: bool = False,
) -> ControlResult:
    """Restore one excluded gate to the run's contract, audited, and return the committed record.

    The mirror of ``exclude_gate``: removes ``gate_id`` from the overlay
    (storing SQL NULL, never ``'[]'``, when the last exclusion goes — the
    no-fabrication ethic) so the daemon's next resolution selects the gate
    again. F2, in the SAME transaction: when the recomposed contract is no
    longer complete against the row's columns, a stamped ``completed_at`` is
    now a lie and is cleared — the reset-family mirror; a still-true stamp is
    untouched. A never-excluded gate is an audited noop. In the A3 protected
    set — a contract edit is destructive.

    Args:
        registry: keyword-only and required — no default registry exists
            (guardrail §8).
        force: bypass the ``ProtectedError`` refusal (recorded in the audit
            args); never clears protection.

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited).
        UnknownGateError: ``gate_id`` names no gate of the run's profile
            (never audited).
        UnknownProfileError: the run's stamped ``pipeline_version`` is not in
            ``registry`` (propagated from ``load_profile``; never audited).
        ProtectedError: the run is protected and ``force`` is False (raised
            after its refusal audit row commits and its event is emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer), or
            the stored ``excluded_gates`` is corrupt (propagated from
            ``run_exclusions``; rollback, nothing written).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``).
    """
    shared_audit_instant = now_iso()
    refusal: ControlError | None = None
    changes: dict[str, object] = {}
    with write_txn(db_path) as connection:
        run_row = get_run(connection, run_name)
        if run_row is None:
            # Raising inside write_txn rolls back: nothing written, no event.
            raise UnknownRunError(f"include_gate: no run named {run_name!r}")
        profile = load_profile(run_row["pipeline_version"], registry)
        _resolve_contract_gate(profile, gate_id, action="include_gate")
        current_exclusions = run_exclusions(run_row)
        protection_why = _protection_refusal_why(run_row, force)
        if protection_why is not None:
            status = "refused"
            message = protection_why
            audit_result = f"refused: {protection_why}"
            refusal = ProtectedError(f"include_gate: {protection_why}")
        elif gate_id not in current_exclusions:
            status = "noop"
            message = "gate not excluded"
            audit_result = "noop: gate not excluded"
        else:
            remaining_exclusions = tuple(
                excluded_id
                for excluded_id in current_exclusions
                if excluded_id != gate_id
            )
            changes = {
                "excluded_gates": (
                    json.dumps(sorted(remaining_exclusions))
                    if remaining_exclusions
                    else None
                )
            }
            # F2: re-adding a step the recomposed contract still lacks makes
            # a stamped completed_at a stale cache — clear it in this same
            # transaction (the reset-family mirror). A contract still
            # complete (the re-included gate's column is already 1) keeps a
            # still-true stamp.
            recomposed_contract = apply_exclusions(profile, remaining_exclusions)
            if run_row["completed_at"] is not None and not is_complete(
                run_row, recomposed_contract
            ):
                changes["completed_at"] = None
            update_run(connection, run_name, changes)
            status = "ok"
            message = None
            audit_result = "ok"
        audit_id = record_control_action(
            connection,
            actor=actor,
            action="include_gate",
            target_run=run_name,
            args={"gate": gate_id, "reason": reason, "force": force},
            result=audit_result,
            ts=shared_audit_instant,
        )
    return _conclude(
        "include_gate",
        actor,
        run_name,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
        refusal=refusal,
    )


def reset_gate(
    db_path,
    actor,
    run_name,
    gate_id,
    *,
    registry,
    force: bool = False,
    reason: str | None = None,
) -> ControlResult:
    """Rewind one gate by clearing its ``satisfied_column`` and ``completed_at``, audited, and return the committed record.

    COLUMN-CLEAR ONLY (⚑1/ADR-0005 d.2): one transaction, zero artifact I/O,
    no ``invalidate`` — the daemon's next stateless look re-selects the
    cleared gate and re-runs it, and §4's re-entrancy requirement covers
    whatever is already on disk (deletion is the operator's, manually). The
    gate resolves from the BASE stamped profile (F3 — clearing an excluded
    gate's column is harmless and prevents a later ``include_gate`` finding a
    stale satisfied column). Refuses on a daemon-eligible run (the §2
    residual posture — a surprising re-stamp, not a harm); an already
    unsatisfied gate is an audited noop. In the A3 protected set.

    Args:
        registry: keyword-only and required — no default registry exists
            (guardrail §8).
        force: bypass the ``ProtectedError`` refusal (recorded in the audit
            args); never clears protection and never bypasses
            ``ActiveRunError``.

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited).
        UnknownGateError: ``gate_id`` names no gate of the run's profile
            (never audited).
        UnknownProfileError: the run's stamped ``pipeline_version`` is not in
            ``registry`` (propagated from ``load_profile``; never audited).
        ActiveRunError: the run is daemon-eligible — hold, skip, or wait for
            quarantine first (raised after its refusal audit row commits and
            its event is emitted).
        ProtectedError: the run is protected and ``force`` is False (raised
            after its refusal audit row commits and its event is emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``).
    """
    shared_audit_instant = now_iso()
    refusal: ControlError | None = None
    changes: dict[str, object] = {}
    with write_txn(db_path) as connection:
        run_row = get_run(connection, run_name)
        if run_row is None:
            # Raising inside write_txn rolls back: nothing written, no event.
            raise UnknownRunError(f"reset_gate: no run named {run_name!r}")
        profile = load_profile(run_row["pipeline_version"], registry)
        gate = _resolve_contract_gate(profile, gate_id, action="reset_gate")
        quiescence_why = _quiescence_refusal_why(run_row)
        protection_why = _protection_refusal_why(run_row, force)
        if quiescence_why is not None:
            status = "refused"
            message = quiescence_why
            audit_result = f"refused: {quiescence_why}"
            refusal = ActiveRunError(f"reset_gate: {quiescence_why}")
        elif protection_why is not None:
            status = "refused"
            message = protection_why
            audit_result = f"refused: {protection_why}"
            refusal = ProtectedError(f"reset_gate: {protection_why}")
        elif not run_row[gate.satisfied_column]:
            # Noop keys on the column ALONE — deliberate asymmetry with
            # backtrack's nothing_to_clear: this noop writes nothing, so a
            # still-truthful completed_at (e.g. resetting an out-of-contract
            # gate on a completed run) survives untouched.
            status = "noop"
            message = "gate is not satisfied — nothing to rewind"
            audit_result = "noop: gate is not satisfied — nothing to rewind"
        else:
            changes = {gate.satisfied_column: 0, "completed_at": None}
            update_run(connection, run_name, changes)
            status = "ok"
            message = None
            audit_result = "ok"
        audit_id = record_control_action(
            connection,
            actor=actor,
            action="reset_gate",
            target_run=run_name,
            args={"gate": gate_id, "reason": reason, "force": force},
            result=audit_result,
            ts=shared_audit_instant,
        )
    return _conclude(
        "reset_gate",
        actor,
        run_name,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
        refusal=refusal,
    )


def backtrack(
    db_path,
    actor,
    run_name,
    gate_id,
    *,
    registry,
    force: bool = False,
    reason: str | None = None,
) -> ControlResult:
    """Rewind one gate AND every later gate of the base stamped profile, audited, and return the committed record.

    The pointerless cascade (⚑1/ADR-0005): one ``update_run`` clears the
    targeted gates' ``satisfied_column``s plus ``completed_at`` — no
    ``invalidate``, no artifact I/O, and NO pointer write
    (``run_from_step``/``run_from_fresh`` stay dormant: a pointer-writing op
    with no honoring reader is a defect, plan 06). The cascade iterates the
    BASE profile by ``order`` (F3 — the excluded gates' columns clear too, so
    a later ``include_gate`` never resurrects a stale satisfied column).
    Refuses on a daemon-eligible run (§2 residual posture); nothing to clear
    is an audited noop. In the A3 protected set.

    Args:
        registry: keyword-only and required — no default registry exists
            (guardrail §8).
        force: bypass the ``ProtectedError`` refusal (recorded in the audit
            args); never clears protection and never bypasses
            ``ActiveRunError``.

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited).
        UnknownGateError: ``gate_id`` names no gate of the run's profile
            (never audited).
        UnknownProfileError: the run's stamped ``pipeline_version`` is not in
            ``registry`` (propagated from ``load_profile``; never audited).
        ActiveRunError: the run is daemon-eligible — hold, skip, or wait for
            quarantine first (raised after its refusal audit row commits and
            its event is emitted).
        ProtectedError: the run is protected and ``force`` is False (raised
            after its refusal audit row commits and its event is emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``).
    """
    shared_audit_instant = now_iso()
    refusal: ControlError | None = None
    changes: dict[str, object] = {}
    with write_txn(db_path) as connection:
        run_row = get_run(connection, run_name)
        if run_row is None:
            # Raising inside write_txn rolls back: nothing written, no event.
            raise UnknownRunError(f"backtrack: no run named {run_name!r}")
        profile = load_profile(run_row["pipeline_version"], registry)
        target_gate = _resolve_contract_gate(profile, gate_id, action="backtrack")
        # The base stamped profile's gates at or after the target, by order
        # (profile.gates is already ascending by Gate.order).
        cascade_gates = [
            gate for gate in profile.gates if gate.order >= target_gate.order
        ]
        quiescence_why = _quiescence_refusal_why(run_row)
        protection_why = _protection_refusal_why(run_row, force)
        # completed_at participates here — deliberate asymmetry with
        # reset_gate's column-only noop: with any targeted column OR the stamp
        # set, the ok path clears both, and an over-eager completed_at clear
        # self-heals on the daemon's next look (it re-stamps a truly complete
        # contract), whereas a noop must write nothing at all.
        nothing_to_clear = (
            all(not run_row[gate.satisfied_column] for gate in cascade_gates)
            and run_row["completed_at"] is None
        )
        if quiescence_why is not None:
            status = "refused"
            message = quiescence_why
            audit_result = f"refused: {quiescence_why}"
            refusal = ActiveRunError(f"backtrack: {quiescence_why}")
        elif protection_why is not None:
            status = "refused"
            message = protection_why
            audit_result = f"refused: {protection_why}"
            refusal = ProtectedError(f"backtrack: {protection_why}")
        elif nothing_to_clear:
            status = "noop"
            message = "nothing to rewind"
            audit_result = "noop: nothing to rewind"
        else:
            changes = {gate.satisfied_column: 0 for gate in cascade_gates}
            changes["completed_at"] = None
            update_run(connection, run_name, changes)
            status = "ok"
            message = None
            audit_result = "ok"
        audit_id = record_control_action(
            connection,
            actor=actor,
            action="backtrack",
            target_run=run_name,
            args={"gate": gate_id, "reason": reason, "force": force},
            result=audit_result,
            ts=shared_audit_instant,
        )
    return _conclude(
        "backtrack",
        actor,
        run_name,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
        refusal=refusal,
    )


def reset(
    db_path,
    actor,
    run_name,
    *,
    registry,
    force: bool = False,
    reason: str | None = None,
) -> ControlResult:
    """Restore a run to its freshly-detected state shape, audited, and return the committed record.

    Column-clear only (ADR-0005), one transaction: every base-profile gate
    column → 0, ``completed_at``/``errored_at``/``error_reason``/
    ``excluded_gates`` (fresh = the full stamped contract)/``globus_task_id``
    → NULL, the dormant pointer columns defensively NULL, and
    ``lifecycle='active'`` with the trio naming the resetting actor.
    PRESERVED, never cleared (§2 B2/A5): ``name``, ``experiment_id``,
    ``size_tb``, ``detected_at``, ``pipeline_version``, ``slack_thread_ts``,
    and ``protected_*`` — identity and non-recomputable handles survive; the
    re-running gates' re-entrant ``run``s handle old artifacts, or the
    operator cleans up manually. Refuses on a daemon-eligible run (§2
    residual posture — a quarantined run is already ineligible and resets
    directly). In the A3 protected set.

    Args:
        registry: keyword-only and required — no default registry exists
            (guardrail §8).
        force: bypass the ``ProtectedError`` refusal (recorded in the audit
            args); never clears protection and never bypasses
            ``ActiveRunError``.

    Raises:
        UnknownRunError: no run named ``run_name`` exists (never audited).
        UnknownProfileError: the run's stamped ``pipeline_version`` is not in
            ``registry`` (propagated from ``load_profile``; never audited).
        ActiveRunError: the run is daemon-eligible — hold, skip, or wait for
            quarantine first (raised after its refusal audit row commits and
            its event is emitted).
        ProtectedError: the run is protected and ``force`` is False (raised
            after its refusal audit row commits and its event is emitted).
        ValueError: ``actor`` is blank (propagated from the audit writer).
        sqlite3.DatabaseError: the database cannot be opened or written
            (propagated from ``write_txn``).
    """
    shared_audit_instant = now_iso()
    refusal: ControlError | None = None
    changes: dict[str, object] = {}
    with write_txn(db_path) as connection:
        run_row = get_run(connection, run_name)
        if run_row is None:
            # Raising inside write_txn rolls back: nothing written, no event.
            raise UnknownRunError(f"reset: no run named {run_name!r}")
        profile = load_profile(run_row["pipeline_version"], registry)
        quiescence_why = _quiescence_refusal_why(run_row)
        protection_why = _protection_refusal_why(run_row, force)
        if quiescence_why is not None:
            status = "refused"
            message = quiescence_why
            audit_result = f"refused: {quiescence_why}"
            refusal = ActiveRunError(f"reset: {quiescence_why}")
        elif protection_why is not None:
            status = "refused"
            message = protection_why
            audit_result = f"refused: {protection_why}"
            refusal = ProtectedError(f"reset: {protection_why}")
        else:
            changes = {gate.satisfied_column: 0 for gate in profile.gates}
            changes.update(
                {
                    "completed_at": None,
                    "errored_at": None,
                    "error_reason": None,
                    "excluded_gates": None,
                    "globus_task_id": None,
                    # Defensive: dormant in Plan A (⚑1), cleared so no future
                    # reader can inherit a stale pointer from before a reset.
                    "run_from_step": None,
                    "run_from_fresh": None,
                    "lifecycle": "active",
                    "lifecycle_by": actor,
                    "lifecycle_at": shared_audit_instant,
                    "lifecycle_reason": reason,
                }
            )
            update_run(connection, run_name, changes)
            status = "ok"
            message = None
            audit_result = "ok"
        audit_id = record_control_action(
            connection,
            actor=actor,
            action="reset",
            target_run=run_name,
            args={"reason": reason, "force": force},
            result=audit_result,
            ts=shared_audit_instant,
        )
    return _conclude(
        "reset",
        actor,
        run_name,
        status=status,
        message=message,
        changes=changes,
        audit_id=audit_id,
        refusal=refusal,
    )


def _run_status_entry(
    run_row, registry, *, include_contract_detail: bool
) -> dict[str, object]:
    """Build one run's status entry, total under per-run anomalies.

    ``experiment_id`` is ALWAYS present (``None`` allowed) — the deliberate
    contrast with the event builders' omit-when-NULL: a status table renders
    the blank, and experiment_id is the operator's reference key. A drifted
    profile becomes ``profile_error`` and corrupt exclusions become
    ``exclusions_error`` (F5), each with no fabricated ``complete``/
    ``excluded_gates``/``gates`` fields — one broken run must not hide the
    fleet, and must not be rendered healthy either.
    """
    entry: dict[str, object] = {
        "name": run_row["name"],
        "experiment_id": run_row["experiment_id"],
        "outcome": run_outcome(run_row),
        "lifecycle": run_row["lifecycle"],
        "protected": run_row["protected_at"] is not None,
    }
    if run_row["errored_at"] is not None:
        entry["error_reason"] = run_row["error_reason"]
    if include_contract_detail:
        entry["pipeline_version"] = run_row["pipeline_version"]
        entry["detected_at"] = run_row["detected_at"]
        entry["completed_at"] = run_row["completed_at"]
    try:
        base_profile = load_profile(run_row["pipeline_version"], registry)
    except (UnknownProfileError, ValueError) as profile_error:
        entry["profile_error"] = str(profile_error)
        return entry
    try:
        excluded_gate_ids = run_exclusions(run_row)
        derived_profile = apply_exclusions(base_profile, excluded_gate_ids)
    except ValueError as exclusions_error:
        entry["exclusions_error"] = str(exclusions_error)
        return entry
    entry["excluded_gates"] = list(excluded_gate_ids)
    entry["complete"] = is_complete(run_row, derived_profile)
    if include_contract_detail:
        contract_gates = required_gates(derived_profile)
        entry["gates"] = {
            "satisfied": [
                gate.id for gate in contract_gates if run_row[gate.satisfied_column]
            ],
            "unsatisfied": [
                gate.id for gate in contract_gates if not run_row[gate.satisfied_column]
            ],
        }
    return entry


def get_status(db_path, run_name=None, *, registry) -> dict[str, object]:
    """Report global and per-run state as a plain dict — a pure read.

    No ``actor``, no audit row, no event (plan 08 §2 — there is nothing to
    attribute): §9's "each op writes a row" reads as each *state-writing* op.
    ``complete`` is derived over the DERIVED contract (exclusions applied) via
    ``reconcile.is_complete``; ``outcome`` via ``events.run_outcome``. The
    overview shape is ``{"paused", "pipeline_profile", "runs": [...]}``; the
    single-run shape is one entry plus ``pipeline_version``/``detected_at``/
    ``completed_at`` and the contract partition ``{"gates": {"satisfied",
    "unsatisfied"}}``. Status is TOTAL: a per-run anomaly becomes that run's
    ``profile_error``/``exclusions_error`` field, never a fleet-wide raise.
    The field set is a pinned seam Plan C extends consciously.

    Args:
        run_name: ``None`` for the fleet overview; a run name for the
            single-run detail shape.
        registry: keyword-only and required — no default registry exists
            (guardrail §8).

    Raises:
        UnknownRunError: ``run_name`` was given and no such run exists.
        sqlite3.DatabaseError: the database cannot be opened or read
            (propagated from ``get_connection``).
    """
    connection = get_connection(db_path)
    try:
        if run_name is not None:
            run_row = get_run(connection, run_name)
            if run_row is None:
                raise UnknownRunError(f"get_status: no run named {run_name!r}")
            return _run_status_entry(run_row, registry, include_contract_detail=True)
        # Every run, quarantined and completed included — the whole point of
        # a status board — in select_active_runs' deterministic order.
        all_run_rows = connection.execute(
            "SELECT * FROM runs ORDER BY detected_at, name"
        ).fetchall()
        return {
            "paused": is_paused(connection),
            "pipeline_profile": get_metadata(connection, PIPELINE_PROFILE_KEY),
            "runs": [
                _run_status_entry(run_row, registry, include_contract_detail=False)
                for run_row in all_run_rows
            ],
        }
    finally:
        connection.close()
