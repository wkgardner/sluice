"""Wide events — canonical log lines that describe run state, never act on it.

Imports are a closed set: stdlib + ``structlog`` + ``sluice.core.gate``
(plan 03 §1; ADR-0003). ``slack_sdk``, ``sqlite3``, and ``sluice.core.db``
are forbidden by name (errata B8; ADR-0002): builders take a row object,
never a connection, so this module structurally cannot write run state or
reach Slack — ``tests/test_events.py`` enforces it by ast parse.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

import structlog

from sluice.core.gate import Gate, GateResult

# The declared public surface — exactly the ten names plan 03 §1 documents
# (db unit-7 precedent): a name is public only if listed here, and unit 8
# asserts set equality so surface creep in either direction fails a test.
__all__ = [
    "Notifier",
    "SEVERITY_BY_RUN_OUTCOME",
    "build_gate_event",
    "build_run_event",
    "control_event",
    "emit_gate_event",
    "emit_run_event",
    "format_duration",
    "run_outcome",
    "safe_notify",
]

# One module logger for every event this module emits (logging skill: one
# logger; plan 03 §3.1). Configuration happens once at daemon startup
# (subsystem 9) — never here.
log = structlog.get_logger("sluice.events")


@runtime_checkable
class Notifier(Protocol):
    """Milestone notifier the daemon/api inject; core never implements it.

    B8's injection contract (plan 03 §3.1, ⚑4/⚑5): core owns only this
    Protocol — the concrete Slack implementation arrives with Plan B, and the
    daemon is the sole caller (via ``safe_notify``). ``runtime_checkable``
    ``isinstance`` checks method *names* only, never signatures.
    """

    def open_run_thread(
        self,
        run_name: str,
        experiment_id: int | None = None,
        size_tb: float | None = None,
    ) -> str | None:
        """Open the run's notification thread; return its id (the A5 seam) or ``None``."""
        ...

    def update_run_header(self, run_name: str) -> None:
        """Rewrite the run's parent message with late-arriving metadata.

        Added 2026-09-22 (supersedes the plan 03 ⚑4 three-method pin, by
        user decision): the parent posted at detection reads "exp pending"
        until gate 0 stamps the experiment id, and that id is the one value
        everyone scans the channel for.
        """
        ...

    def post_milestone(self, run_name: str, text: str, mention: bool = False) -> None:
        """Post a milestone message to the run's thread."""
        ...

    def finish_run_thread(
        self, run_name: str, outcome: str, reason: str | None = None
    ) -> None:
        """Close the run's thread with its terminal outcome."""
        ...


def safe_notify(
    notifier: Notifier | None, method: str, /, *args: object, **kwargs: object
) -> object | None:
    """Call ``method`` on ``notifier`` and return its value, swallowing notifier failures loudly.

    An unconfigured (``None``) notifier is a normal deployment mode (plan 03
    unit 3): return ``None`` and log nothing. Any ``Exception`` from the call
    itself is swallowed to ``None`` — notification failure never fails work —
    and surfaced as exactly one ``notify.failed`` error line carrying
    ``notify.method`` and ``error.type``, the module's only error-level line.
    On success the return value is propagated because ``open_run_thread``'s
    thread id rides it back to the daemon (the A5 seam).

    Raises:
        AttributeError: ``method`` does not name a method on the notifier —
            resolved outside the swallow guard because a typo is a programmer
            error; swallowing it would disable notifications forever with
            zero signal.
    """
    if notifier is None:
        return None
    # Resolve outside the try: a bad method name must fail loud (unit 3).
    resolved_notifier_method = getattr(notifier, method)
    try:
        return resolved_notifier_method(*args, **kwargs)
    except Exception as notifier_call_error:
        # Swallow, but loudly: a Slack outage must never fail a gate or the
        # daemon (plan 03 §5), yet the lost milestone must leave a trace.
        log.error(
            "notify.failed",
            **{
                "notify.method": method,
                "error.type": type(notifier_call_error).__name__,
            },
        )
        return None


# Severity per run outcome (plan 03 §3.3) — a CLOSED vocabulary keyed by
# exactly the outcomes run_outcome can derive, so build_run_event's lookup can
# never KeyError. No "retrying" (ADR-0002 deleted the retry tier), no "halted"
# (⚑3: B5's disk-halt is subsystem 9's global pause, not a run outcome), no
# "error" (an escaped exception becomes a daemon-persisted FAILURE →
# quarantined). "quarantined" is "critical" (⚑2): every quarantine demands an
# operator clear_error, and D5's future @-mention policy keys on critical.
SEVERITY_BY_RUN_OUTCOME: dict[str, str] = {
    "completed": "info",
    "quarantined": "critical",
    "held": "warning",
    "skipped": "info",
    "active": "info",
}


def run_outcome(run_row) -> str:
    """Classify a ``runs`` row as ``quarantined``, ``completed``, ``held``, ``skipped``, or ``active``.

    Fixed §3.3 precedence over an arbitrary — even inconsistent — row:
    pipeline facts outrank operator dispositions, and the actionable fact
    outranks all, so a non-NULL ``errored_at`` wins over everything (the
    pending operator action is ``clear_error``) and ``completed_at`` over any
    later hold/skip. Total over any schema-shaped row — ``get_status``
    (subsystem 8) reuses it on non-terminal rows — so an inconsistent
    errored+completed row classifies as ``quarantined`` rather than raising.

    Raises:
        KeyError: ``run_row`` is a mapping missing a column read here
            (``errored_at``, ``completed_at``, ``lifecycle``).
        IndexError: ``run_row`` is a ``sqlite3.Row`` missing such a column.
    """
    # Rules 1-2: pipeline facts — the non-NULL terminal timestamps.
    if run_row["errored_at"] is not None:
        return "quarantined"
    if run_row["completed_at"] is not None:
        return "completed"
    # Rules 3-4: operator dispositions recorded on lifecycle.
    if run_row["lifecycle"] == "held":
        return "held"
    if run_row["lifecycle"] == "skipped":
        return "skipped"
    return "active"


def format_duration(ms: int | float) -> str:
    """Render a millisecond duration in the largest unit that keeps it human-scaled.

    A straight port of python-poller ``events.py:88-104``: sub-second stays in
    integer milliseconds; otherwise the value climbs to seconds, minutes, then
    hours, each rendered to one decimal, so a Slack milestone reads at a glance
    rather than in raw milliseconds (plan 03 §3.1, unit 1).
    """
    if ms < 1000:
        return f"{ms} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} min"
    hours = minutes / 60
    return f"{hours:.1f} h"


# The §3.2 conditional half of the run.finished schema: event field → runs
# column, present iff the column is non-NULL. Absent state yields absent
# fields, never defaults (no-fabrication, §9/B2 ethic); columns not in this
# table (protected_*, globus_task_id, slack_thread_ts, run_from_*) are state,
# not event fields, and never leak onto the line.
_CONDITIONAL_RUN_EVENT_FIELD_TO_COLUMN: tuple[tuple[str, str], ...] = (
    ("experiment.id", "experiment_id"),
    ("run.size_tb", "size_tb"),
    ("run.completed_at", "completed_at"),
    ("run.errored_at", "errored_at"),
    ("error.reason", "error_reason"),
    ("lifecycle.by", "lifecycle_by"),
    ("lifecycle.at", "lifecycle_at"),
    ("lifecycle.reason", "lifecycle_reason"),
)

# The terminal outcomes and the row column holding each one's terminal
# timestamp (§3.2): total_duration_ms exists iff the run reached one of these
# — never fabricated for a run still in flight. run_outcome guarantees the
# named column is non-NULL whenever it derives the outcome.
_TERMINAL_TIMESTAMP_COLUMN_BY_OUTCOME: dict[str, str] = {
    "completed": "completed_at",
    "quarantined": "errored_at",
}


def _milliseconds_between_iso_timestamps(start_iso: str, end_iso: str) -> int:
    """Return the whole milliseconds elapsed from ``start_iso`` to ``end_iso``.

    Both values are parsed with ``datetime.fromisoformat`` — the round-trip
    partner of the db's ``now_iso`` timestamp format (plan 03 §3.2).

    Raises:
        ValueError: either timestamp is not ISO-8601.
    """
    elapsed = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
    return int(elapsed.total_seconds() * 1000)


def build_run_event(
    run_row,
    gates: Sequence[Gate] = (),
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the canonical ``run.finished`` field dict purely from a ``runs`` row.

    The field names are a pinned cross-subsystem seam (plan 03 §3.2) and the
    build is a pure function of state (B9): always the identity fields plus
    ``outcome``/``severity``, conditionals iff non-NULL in the row,
    ``total_duration_ms`` iff terminal, and — iff ``gates`` is non-empty — the
    satisfaction partition ``gates.satisfied``/``gates.unsatisfied`` of gate
    ids by ``row[gate.satisfied_column]`` truthiness, caller order preserved.

    Args:
        run_row: schema-shaped row supporting ``row[column]`` — ``dict`` and
            ``sqlite3.Row`` both qualify (§3.1 duck type).
        extra: caller-supplied fields (e.g. ``failed.gate``), merged last;
            keys must not collide with built fields.

    Raises:
        KeyError: ``run_row`` is a mapping missing a column read here; or —
            uniformly for both row shapes, presence pre-checked via
            ``row.keys()`` (§3.1) — a gate's ``satisfied_column`` is absent
            from the row (registry/schema mismatch), named in the message.
        IndexError: ``run_row`` is a ``sqlite3.Row`` missing a non-gate
            column read here.
        ValueError: a terminal timestamp in the row is not ISO-8601 (state
            corruption — the db writes them via ``now_iso``); or an ``extra``
            key collides with a built field, named in the message — no
            silent clobber of the canonical line.
    """
    outcome = run_outcome(run_row)
    fields: dict[str, object] = {
        "run.name": run_row["name"],
        "run.pipeline_version": run_row["pipeline_version"],
        "run.lifecycle": run_row["lifecycle"],
        "run.detected_at": run_row["detected_at"],
        "outcome": outcome,
        "severity": SEVERITY_BY_RUN_OUTCOME[outcome],
    }
    for field_name, column_name in _CONDITIONAL_RUN_EVENT_FIELD_TO_COLUMN:
        if run_row[column_name] is not None:
            fields[field_name] = run_row[column_name]
    terminal_timestamp_column = _TERMINAL_TIMESTAMP_COLUMN_BY_OUTCOME.get(outcome)
    if terminal_timestamp_column is not None:
        fields["total_duration_ms"] = _milliseconds_between_iso_timestamps(
            run_row["detected_at"], run_row[terminal_timestamp_column]
        )
    if gates:
        satisfied_gate_ids: list[str] = []
        unsatisfied_gate_ids: list[str] = []
        for gate in gates:
            # Presence pre-check via keys() (§3.1 trap): sqlite3.Row raises
            # IndexError on a missing subscript and its `in` tests VALUE
            # membership, so only this spelling makes a registry/schema
            # mismatch a uniform KeyError naming the column on both shapes.
            if gate.satisfied_column not in run_row.keys():
                raise KeyError(
                    f"gate {gate.id!r} reads satisfaction column "
                    f"{gate.satisfied_column!r}, which is missing from the "
                    "run row — registry/schema mismatch (a gate registered "
                    "whose migration never ran?)"
                )
            if run_row[gate.satisfied_column]:
                satisfied_gate_ids.append(gate.id)
            else:
                unsatisfied_gate_ids.append(gate.id)
        fields["gates.satisfied"] = satisfied_gate_ids
        fields["gates.unsatisfied"] = unsatisfied_gate_ids
    if extra:
        # Merge last, but never over a built field (§3.2): a caller that
        # could rewrite e.g. "outcome" could lie about state on the one
        # canonical line operators trust (guardrail §8, no silent clobber).
        colliding_extra_keys = sorted(set(extra) & set(fields))
        if colliding_extra_keys:
            raise ValueError(
                f"extra keys {colliding_extra_keys!r} collide with "
                "builder-owned run.finished fields — extras may only add "
                "facts the builder cannot know, never rewrite the canonical "
                "line (plan 03 §3.2)"
            )
        fields.update(extra)
    return fields


def emit_run_event(
    run_row,
    gates: Sequence[Gate] = (),
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Emit the canonical ``run.finished`` line for a run and return its fields.

    Exactly one ``log.info`` call — one rich canonical line per run per look
    (plan 03 §2 B9; logging skill) — always at level info: severity rides as a
    FIELD (§3.3). The built dict is returned so callers (control Results,
    tests) read the fields instead of re-deriving the line (§3.1). The daemon
    owns the call sites and the persist-then-emit rule (§7 carry-forward).

    Raises:
        KeyError: propagated from ``build_run_event`` — a missing row column
            or a gate ``satisfied_column`` absent from the row.
        IndexError: propagated from ``build_run_event`` — ``run_row`` is a
            ``sqlite3.Row`` missing a non-gate column.
        ValueError: propagated from ``build_run_event`` — a non-ISO terminal
            timestamp, or an ``extra`` key colliding with a built field.
    """
    fields = build_run_event(run_row, gates=gates, extra=extra)
    log.info("run.finished", **fields)
    return fields


def build_gate_event(
    run_name: str,
    gate: Gate,
    result: GateResult,
    duration_ms: int | None = None,
) -> dict[str, object]:
    """Build the ``gate.finished`` field dict for one gate result.

    The field names are a pinned seam (plan 03 §3.2): ``run.name`` rides on
    every gate line because correlation with the run's canonical line is by
    field (no contextvars — B9/D6); ``gate.outcome`` is the enum's ``.value``
    string, the persisted/logged vocabulary; ``gate.metadata`` rides NESTED,
    never splatted, so result keys cannot collide with canonical field names.
    Conditionals follow no-fabrication: ``gate.duration_ms`` iff the daemon
    supplied a timing (per-gate timings live here, not on the run row — §2
    B9), ``gate.reason``/``gate.metadata`` iff the result carried them.
    """
    fields: dict[str, object] = {
        "run.name": run_name,
        "gate.id": gate.id,
        "gate.outcome": result.outcome.value,
    }
    if duration_ms is not None:
        fields["gate.duration_ms"] = duration_ms
    if result.reason:
        fields["gate.reason"] = result.reason
    if result.metadata:
        fields["gate.metadata"] = result.metadata
    return fields


def emit_gate_event(
    run_name: str,
    gate: Gate,
    result: GateResult,
    duration_ms: int | None = None,
) -> dict[str, object]:
    """Emit the ``gate.finished`` line for one gate result and return its fields.

    Exactly one ``log.info`` call — one ``gate.finished`` line per gate per
    persist (plan 03 unit 6; logging skill) — always at level info: even a
    FAILURE result rides an info line (severity never rides gate lines, §3.2).
    The built dict is returned so callers read the fields instead of
    re-deriving the line (§3.1). The daemon owns the call sites, times
    ``gate.run``, and passes ``duration_ms`` (§7 carry-forward).
    """
    fields = build_gate_event(run_name, gate, result, duration_ms=duration_ms)
    log.info("gate.finished", **fields)
    return fields


def control_event(
    action: str,
    actor: str,
    target_run: str | None = None,
    result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Emit the ``control.finished`` line for one control op and return its fields.

    The event half of §9's "audit row + wide event" (plan 03 unit 7; the audit
    row is subsystem 7's, the mutate → audit → event ordering is subsystem
    8's). Exactly one ``log.info`` call at level info; ``run.name`` rides iff
    the op targeted a run (global ops like ``pause`` carry none — a fabricated
    ``run.name`` would corrupt subsystem 9's per-run jsonl routing);
    ``result`` rides NESTED under ``control.result``, never splatted. There is
    deliberately no notifier parameter — §11's "control actions never post to
    Slack" is a signature fact.
    """
    fields: dict[str, object] = {
        "control.action": action,
        "control.actor": actor,
    }
    if target_run:
        fields["run.name"] = target_run
    if result is not None:
        fields["control.result"] = result
    log.info("control.finished", **fields)
    return fields
