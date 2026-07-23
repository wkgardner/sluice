"""Behavioral tests for ``sluice.core.events`` — the wide-event module (plan 03).

Events *describe* state; they never act on it (ADR-0002, errata B8/B9). The
first guard here is structural: a module that cannot even name a SQL client or
a Slack SDK cannot write run state or post to Slack behind the daemon's back.
"""

import ast
import inspect
import pathlib
import sqlite3
import sys

import pytest
import structlog.testing


# --------------------------------------------------------------------------
# Shared helpers (plan 03 §4). The fake lives in tests, not core — core ships
# contracts, not test doubles.
# --------------------------------------------------------------------------


def make_run_row(**overrides):
    """Schema-shaped ``runs`` row as a plain dict (plan 03 §4 shared helper).

    Defaults mirror a freshly-inserted row under migrations 001+003: required
    columns filled (``name="r1"``, ``pipeline_version="v1"``,
    ``lifecycle="active"``, ``detected_at`` at a fixed instant, both mock gate
    columns 0), every optional column ``None``. A plain dict satisfies the
    §3.1 ``run_row`` duck type (``row[column]`` + ``row.keys()``) exactly as a
    real ``sqlite3.Row`` does; unit 8 replays the same surface against the
    real thing so a drift between this shape and the schema fails there.
    """
    row = {
        "name": "r1",
        "experiment_id": None,
        "size_tb": None,
        "detected_at": "2026-07-21T10:00:00+00:00",
        "pipeline_version": "v1",
        "completed_at": None,
        "lifecycle": "active",
        "lifecycle_by": None,
        "lifecycle_at": None,
        "lifecycle_reason": None,
        "error_reason": None,
        "errored_at": None,
        "run_from_step": None,
        "run_from_fresh": None,
        "protected_at": None,
        "protected_by": None,
        "globus_task_id": None,
        "slack_thread_ts": None,
        "mock_alpha_done": 0,
        "mock_beta_done": 0,
    }
    row.update(overrides)
    return row


class RecordingNotifier:
    """Recording fake satisfying the ``Notifier`` contract (plan 03 §4).

    Records every lifecycle call as a ``(method, args, kwargs)`` tuple on
    ``self.calls`` and is constructor-scriptable: every method returns
    ``return_value``, or raises ``raise_exception`` (an exception instance)
    instead when one is given — the two knobs units 3+ need to exercise
    ``safe_notify``'s pass-through and swallow legs.
    """

    def __init__(self, return_value=None, raise_exception=None):
        self.calls = []
        self._return_value = return_value
        self._raise_exception = raise_exception

    def _record_call(self, method_name, args, kwargs):
        self.calls.append((method_name, args, kwargs))
        if self._raise_exception is not None:
            raise self._raise_exception
        return self._return_value

    def open_run_thread(self, *args, **kwargs):
        return self._record_call("open_run_thread", args, kwargs)

    def post_milestone(self, *args, **kwargs):
        return self._record_call("post_milestone", args, kwargs)

    def finish_run_thread(self, *args, **kwargs):
        return self._record_call("finish_run_thread", args, kwargs)


# --------------------------------------------------------------------------
# Unit 1 — module bootstrap, import hygiene (the B8 structural guard),
# format_duration.
# --------------------------------------------------------------------------


def test_events_is_core_clean_by_import():
    """``events.py`` imports only stdlib + ``structlog`` + ``sluice.core.gate``.

    The B8 structural guard and the state-only guard in one (plan 03 unit 1):
    ``slack_sdk`` must live behind the injected ``Notifier`` Protocol (ADR-0003
    decision 3 — service SDKs never enter core), and ``sqlite3`` /
    ``sluice.core.db`` are forbidden because builders take a *row*, never a
    connection — a module that cannot reach the database cannot write run
    state. Parsed with ``ast``, not inspected at runtime, because a
    function-local import — precisely how a "just this once" write or post
    sneaks in — never shows up in the module namespace (same technique as
    ``test_db_imports_only_stdlib`` / ``test_mock_gates_import_only_gate_and_stdlib``).
    """
    # Local import: events is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    import sluice.core.events

    source_path = inspect.getsourcefile(sluice.core.events)
    tree = ast.parse(pathlib.Path(source_path).read_text(encoding="utf-8"))

    # Collect fully-qualified module names for every import statement. For
    # ``from X import Y`` we record the module X (absolute) or resolve the
    # relative base against this module's package, sluice.core — so
    # ``from .db import connect`` cannot dodge the guard by being relative.
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative_base_parts = "sluice.core".split(".")
                # level=1 is the containing package; each extra level climbs.
                if node.level > 1:
                    relative_base_parts = relative_base_parts[: -(node.level - 1)]
                resolved = ".".join(relative_base_parts)
                if node.module:
                    resolved = f"{resolved}.{node.module}"
                imported_modules.add(resolved)
            elif node.module:
                imported_modules.add(node.module)

    # Forbidden imports asserted BY NAME so the failure message teaches
    # (plan 03 unit 1): each name is a distinct policy with its own reason.
    slack_sdk_imports = sorted(
        module
        for module in imported_modules
        if module == "slack_sdk" or module.startswith("slack_sdk.")
    )
    assert slack_sdk_imports == [], (
        f"{source_path} imports {slack_sdk_imports}: core never imports a "
        "service SDK (B8, ADR-0003 decision 3) — Slack lives behind the "
        "injected Notifier Protocol, implemented outside core in Plan B"
    )
    sql_client_imports = sorted(
        module
        for module in imported_modules
        if module == "sqlite3" or module.startswith("sqlite3.")
    )
    assert sql_client_imports == [], (
        f"{source_path} imports {sql_client_imports}: events must not reach "
        "a SQL client — events describe state, they never write it "
        "(ADR-0002); builders take a row object, never a connection"
    )
    db_module_imports = sorted(
        module
        for module in imported_modules
        if module == "sluice.core.db" or module.startswith("sluice.core.db.")
    )
    assert db_module_imports == [], (
        f"{source_path} imports {db_module_imports}: events must not reach "
        "the db primitives — a module that cannot open a connection cannot "
        "write run state (plan 03 §1, the state-only structural guard)"
    )

    # Every sluice-internal import is exactly the gate contract module.
    sluice_module_imports = sorted(
        module
        for module in imported_modules
        if module == "sluice" or module.startswith("sluice.")
    )
    non_gate_sluice_imports = [
        module for module in sluice_module_imports if module != "sluice.core.gate"
    ]
    assert non_gate_sluice_imports == [], (
        f"{source_path} imports sluice modules beyond the gate contract: "
        f"{non_gate_sluice_imports}"
    )

    # Everything else must be stdlib or structlog — the one third-party
    # dependency the design of record names as events' foundation (ADR-0003).
    non_stdlib_roots = sorted(
        {module.split(".")[0] for module in imported_modules}
        - sys.stdlib_module_names
        - {"sluice", "structlog"}
    )
    assert non_stdlib_roots == [], (
        f"{source_path} imports third-party modules beyond structlog: "
        f"{non_stdlib_roots}"
    )


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (19, "19 ms"),
        (5324, "5.3 s"),
        (1899507, "31.7 min"),
        (9811266, "2.7 h"),
    ],
)
def test_format_duration_adapts_units(milliseconds, expected):
    """Durations render in the unit that keeps them human-scaled (ms/s/min/h).

    A straight port of python-poller ``events.py:88-104``; the cases are PP's
    own documented pivots, so a drift in a unit boundary or in the one-decimal
    rendering of the scaled units fails here by exact string equality.
    """
    # Local import: format_duration is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import format_duration

    assert format_duration(milliseconds) == expected


# --------------------------------------------------------------------------
# Unit 2 — the Notifier Protocol (B8's injection contract).
# --------------------------------------------------------------------------


def test_notifier_protocol_names_the_three_run_lifecycle_methods():
    """``Notifier`` is runtime-checkable over exactly the three ``run_*`` lifecycle methods.

    B8's injection contract (plan 03 unit 2): core owns only the Protocol — a
    concrete Slack implementation arrives with Plan B, and every fake in this
    suite duck-types the contract. ``runtime_checkable`` isinstance checks
    names only (never signatures), and that is precisely what this pins: the
    runtime-checkable choice itself, and the ``run_*`` method names
    (``open_run_thread`` / ``post_milestone`` / ``finish_run_thread``) — a
    drift back to python-poller's ``folder_*`` names would break every fake
    and Plan B's implementation at this seam.
    """
    # Local import: Notifier is under construction; a module-level import
    # would fail collection for every other test in this file.
    from sluice.core.events import Notifier

    class FakeWithAllThreeLifecycleMethods:
        """Minimal structural fake: exactly the three Protocol methods."""

        def open_run_thread(self, run_name, experiment_id=None, size_tb=None):
            return None

        def post_milestone(self, run_name, text, mention=False):
            return None

        def finish_run_thread(self, run_name, outcome, reason=None):
            return None

    class FakeMissingFinishRunThread:
        """Same fake minus ``finish_run_thread`` — must NOT satisfy the Protocol."""

        def open_run_thread(self, run_name, experiment_id=None, size_tb=None):
            return None

        def post_milestone(self, run_name, text, mention=False):
            return None

    assert isinstance(FakeWithAllThreeLifecycleMethods(), Notifier), (
        "an object defining exactly open_run_thread/post_milestone/"
        "finish_run_thread must satisfy the runtime-checkable Notifier "
        "Protocol (plan 03 §3.1)"
    )
    assert not isinstance(FakeMissingFinishRunThread(), Notifier), (
        "an object missing finish_run_thread must NOT satisfy the Notifier "
        "Protocol — the run-thread lifecycle requires all three methods"
    )


# --------------------------------------------------------------------------
# Unit 3 — safe_notify (notification never fails work).
# --------------------------------------------------------------------------


def test_safe_notify_with_no_notifier_is_a_no_op():
    """A ``None`` notifier makes ``safe_notify`` a silent no-op returning ``None``.

    Notifications are optional: the daemon can run with no notifier configured
    at all (plan 03 unit 3). The unconfigured path must be genuinely silent —
    no return value, and no log line either. In particular it must NOT be
    routed through the failure leg: an implementation that reaches
    ``getattr(None, method)`` would either raise ``AttributeError`` into the
    caller or emit a spurious ``notify.failed`` error line on every milestone
    of every notifier-less deployment.
    """
    # Local import: safe_notify is under construction; a module-level import
    # would fail collection for every other test in this file.
    from sluice.core.events import safe_notify

    with structlog.testing.capture_logs() as captured_log_entries:
        returned = safe_notify(None, "post_milestone", "r1", "sequencing done")

    assert returned is None, (
        "safe_notify with no notifier must return None — there is no call "
        "whose value it could propagate"
    )
    assert captured_log_entries == [], (
        "safe_notify with no notifier must log nothing: an unconfigured "
        "notifier is a normal deployment mode, not a failure to report"
    )


def test_safe_notify_passes_through_and_returns_the_value():
    """``safe_notify`` forwards args/kwargs untouched and propagates the return value.

    The return-value leg is the A5 seam (plan 03 §2): ``open_run_thread``
    returns the Slack-assigned thread ts, and only ``safe_notify``'s
    propagation carries it back to the daemon for Plan B to persist to
    ``slack_thread_ts`` — an implementation that calls the method but discards
    its value (as python-poller's fire-and-forget guards did) silently makes
    that column unpersistable forever. The recorded call tuple pins that
    positional and keyword arguments reach the notifier exactly as given.
    """
    # Local import: safe_notify is under construction; a module-level import
    # would fail collection for every other test in this file.
    from sluice.core.events import safe_notify

    fake = RecordingNotifier(return_value="1721.001")

    returned = safe_notify(fake, "open_run_thread", "r1", experiment_id=7)

    assert returned == "1721.001", (
        "safe_notify must propagate the notifier method's return value — the "
        "A5 seam: open_run_thread's thread ts rides this return back to the "
        "daemon, and dropping it makes slack_thread_ts unpersistable"
    )
    assert fake.calls == [("open_run_thread", ("r1",), {"experiment_id": 7})], (
        "safe_notify must forward positional and keyword arguments to the "
        "named notifier method exactly as the caller gave them"
    )


def test_safe_notify_swallows_notifier_failures_loudly():
    """A raising notifier method is swallowed to ``None`` with exactly one ``notify.failed`` error line.

    The invariant this pins (plan 03 unit 3, invariant table §5): notification
    failure never fails work — a Slack outage must not fail a gate or the
    daemon, so the exception is caught, not propagated. But swallowing must be
    LOUD: exactly one ``log.error`` line named ``notify.failed`` carrying the
    method and the exception type — the module's only error-level line
    (severity-as-field, §2 ADR-0002 note) — replacing python-poller's
    copy-pasted silent try/excepts, which lost outages entirely.
    """
    # Local import: the swallow leg is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import safe_notify

    fake = RecordingNotifier(raise_exception=RuntimeError("slack down"))

    with structlog.testing.capture_logs() as captured_log_entries:
        returned = safe_notify(fake, "post_milestone", "r1", "sequencing done")

    assert returned is None, (
        "safe_notify must swallow a notifier exception and return None — a "
        "Slack outage never fails a gate or the daemon (plan 03 unit 3)"
    )
    assert len(captured_log_entries) == 1, (
        "the swallowed failure must surface as exactly one log line — silent "
        "swallowing loses the outage, double emission breaks the "
        "one-canonical-line rule; got "
        f"{captured_log_entries!r}"
    )
    entry = captured_log_entries[0]
    assert entry["event"] == "notify.failed", (
        "the failure line must be named notify.failed — subsystem 9's "
        "processors and operators grep for this literal"
    )
    assert entry["log_level"] == "error", (
        "notify.failed is the module's only error-LEVEL line (plan 03 §2: "
        "severity rides as a field everywhere else, at level info)"
    )
    assert entry["error.type"] == "RuntimeError", (
        "the line must carry the exception type under error.type so an "
        "operator can distinguish an outage from a bug without a traceback"
    )
    assert entry["notify.method"] == "post_milestone", (
        "the line must name the notifier method that failed under "
        "notify.method — which milestone was lost is the actionable fact"
    )


def test_safe_notify_fails_loud_on_a_method_typo():
    """A misspelled method name raises ``AttributeError`` instead of being swallowed.

    The no-silent-fallback posture (guardrail §8) applied to observability
    (plan 03 unit 3): the swallow guard exists for *notifier* failures (a
    Slack outage), not for *programmer* errors. Method resolution happens
    outside the try, so a typo'd name propagates ``AttributeError`` — an
    implementation that swallowed it would disable that notification forever
    with zero signal beyond a misleading ``notify.failed`` line on every call.
    """
    # Local import: the fail-loud leg is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import safe_notify

    fake = RecordingNotifier()

    with pytest.raises(AttributeError):
        safe_notify(fake, "post_milestne", "r1", "hi")


# --------------------------------------------------------------------------
# Unit 4 — run_outcome + SEVERITY_BY_RUN_OUTCOME (the vocabulary, closed).
# --------------------------------------------------------------------------


# The precedence matrix is module-level because two tests share it: the
# parametrized precedence test below, and the vocabulary-closure test, which
# proves every outcome the matrix can derive has a severity in the map.
RUN_OUTCOME_PRECEDENCE_MATRIX = [
    pytest.param({}, "active", id="fresh-row-is-active"),
    pytest.param(
        {"error_reason": "boom", "errored_at": "2026-07-21T10:05:00+00:00"},
        "quarantined",
        id="errored-is-quarantined",
    ),
    pytest.param(
        {"completed_at": "2026-07-21T10:01:30+00:00"},
        "completed",
        id="completed-at-is-completed",
    ),
    pytest.param({"lifecycle": "held"}, "held", id="held-lifecycle-is-held"),
    pytest.param(
        {"lifecycle": "skipped"}, "skipped", id="skipped-lifecycle-is-skipped"
    ),
    pytest.param(
        {
            "error_reason": "boom",
            "errored_at": "2026-07-21T10:05:00+00:00",
            "lifecycle": "held",
        },
        "quarantined",
        id="errored-outranks-a-hold",
    ),
    pytest.param(
        {"completed_at": "2026-07-21T10:01:30+00:00", "lifecycle": "skipped"},
        "completed",
        id="completed-outranks-a-skip",
    ),
    pytest.param(
        {
            "error_reason": "boom",
            "errored_at": "2026-07-21T10:05:00+00:00",
            "completed_at": "2026-07-21T10:01:30+00:00",
        },
        "quarantined",
        id="errored-outranks-completed-on-an-inconsistent-row",
    ),
]


@pytest.mark.parametrize(
    ("row_overrides", "expected_outcome"), RUN_OUTCOME_PRECEDENCE_MATRIX
)
def test_run_outcome_precedence_matrix(row_overrides, expected_outcome):
    """``run_outcome`` is total over any row, by fixed precedence: quarantined > completed > held > skipped > active.

    Pins §3.3's decision that pipeline facts outrank operator dispositions and
    the actionable fact outranks all: a quarantined-and-held row reads
    ``quarantined`` (the pending operator action is ``clear_error``), a
    completed-then-skipped row reads ``completed`` (a later disposition never
    rewrites what the pipeline did), and even a schema-legal-but-inconsistent
    errored+completed row yields ``quarantined`` instead of raising —
    ``get_status`` (subsystem 8) reuses this on arbitrary rows, so totality is
    part of the contract, not a convenience.
    """
    # Local import: run_outcome is under construction; a module-level import
    # would fail collection for every other test in this file.
    from sluice.core.events import run_outcome

    assert run_outcome(make_run_row(**row_overrides)) == expected_outcome


def test_run_outcome_severity_vocabulary_is_closed():
    """``SEVERITY_BY_RUN_OUTCOME`` is exactly the five-entry §3.3 map, covering every derivable outcome.

    Equality is the point (plan 03 unit 4): the vocabulary is CLOSED — no
    ``retrying`` (ADR-0002 deleted the retry tier there is nothing to
    describe), no ``halted`` (⚑3 approved: B5's disk-halt is a global pause
    owned by subsystem 9, not a run outcome), no ``error`` (an escaped
    exception becomes a daemon-persisted FAILURE → ``quarantined``); and
    ``quarantined`` maps to ``"critical"`` (⚑2 approved — python-poller had
    ``"warning"``; under all-failures-quarantine every quarantine demands an
    operator ``clear_error``, and ``critical`` is what D5's future @-mention
    policy keys on). The closure leg proves the map's keys equal the set of
    outcomes ``run_outcome`` can actually derive over the precedence matrix,
    so ``build_run_event``'s severity lookup can never ``KeyError``.
    """
    # Local import: the severity map is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import SEVERITY_BY_RUN_OUTCOME, run_outcome

    assert SEVERITY_BY_RUN_OUTCOME == {
        "completed": "info",
        "quarantined": "critical",
        "held": "warning",
        "skipped": "info",
        "active": "info",
    }, (
        "the severity map must equal the §3.3 dict exactly: a missing entry "
        "breaks build_run_event's lookup, an extra entry (retrying/halted/"
        "error) reopens vocabulary ADR-0002 and ⚑3 closed, and a severity "
        "drift (quarantined must be critical, ⚑2) breaks the future "
        "@-mention-on-actionable-failures policy"
    )

    derivable_outcomes = {
        run_outcome(make_run_row(**matrix_case.values[0]))
        for matrix_case in RUN_OUTCOME_PRECEDENCE_MATRIX
    }
    assert set(SEVERITY_BY_RUN_OUTCOME) == derivable_outcomes, (
        "the map's keys must equal the set of outcomes run_outcome derives "
        "over the precedence matrix — every derivable outcome has a severity "
        "(no KeyError in build_run_event) and no severity exists for an "
        "outcome that cannot occur"
    )


# --------------------------------------------------------------------------
# Unit 5 — build_run_event + emit_run_event (the canonical line, B9's design
# half).
# --------------------------------------------------------------------------


def test_run_event_fields_for_a_completed_run():
    """``build_run_event`` maps a fully-populated completed row to exactly the §3.2 field set.

    The regression this catches is drift in the cross-subsystem field-name
    seam (plan 03 §3.2): subsystem 9's per-run jsonl processor and every
    downstream reader key on these literal dot-namespaced names, so the test
    pins the whole key SET by equality — a renamed field, a dropped
    conditional, or a naive row-dump that leaks non-event columns
    (``protected_at``, ``globus_task_id``, ``slack_thread_ts``, ...) all fail
    here. The row sets every optional column EXCEPT the error pair
    (``error_reason``/``errored_at`` stay NULL — setting them would flip the
    outcome to ``quarantined`` under §3.3 precedence rule 1; the
    quarantined-row path runs against a real ``sqlite3.Row`` in unit 8), so
    every non-error conditional field must be present. ``total_duration_ms``
    is exact — 90 s between the fixed ``detected_at`` and ``completed_at`` —
    because the ISO round-trip through ``now_iso``'s format (parse via
    ``datetime.fromisoformat``) is the sluice decision under test; and both
    satisfied mock gates land in ``gates.satisfied`` in caller order with an
    empty ``gates.unsatisfied`` partition alongside it (never absent, never
    fabricated elsewhere).
    """
    # Local imports: build_run_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import build_run_event
    from sluice.core.mock_gates import MOCK_GATES

    completed_row_with_every_optional_column = make_run_row(
        experiment_id=7,
        size_tb=1.5,
        completed_at="2026-07-21T10:01:30+00:00",  # 90 s after detected_at
        lifecycle_by="will",
        lifecycle_at="2026-07-21T10:02:00+00:00",
        lifecycle_reason="ops note",
        run_from_step="mock_beta",
        run_from_fresh=0,
        protected_at="2026-07-21T10:00:30+00:00",
        protected_by="will",
        globus_task_id="task-123",
        slack_thread_ts="1721.001",
        mock_alpha_done=1,
        mock_beta_done=1,
    )

    fields = build_run_event(completed_row_with_every_optional_column, gates=MOCK_GATES)

    assert set(fields) == {
        # Always present (§3.2).
        "run.name",
        "run.pipeline_version",
        "run.lifecycle",
        "run.detected_at",
        "outcome",
        "severity",
        # Present iff non-NULL in the row — all non-error optionals are set.
        "experiment.id",
        "run.size_tb",
        "run.completed_at",
        "lifecycle.by",
        "lifecycle.at",
        "lifecycle.reason",
        # Present iff terminal (completed here).
        "total_duration_ms",
        # Present iff gates non-empty.
        "gates.satisfied",
        "gates.unsatisfied",
    }, (
        "the run.finished field set is a pinned cross-subsystem seam (plan 03 "
        "§3.2): for a completed row with every non-error optional set, exactly "
        "the always fields + the non-NULL conditionals + total_duration_ms + "
        "the gates partition appear — no run.errored_at/error.reason (NULL "
        "yields absence, never a default), and no leaked row columns "
        "(protected_*/globus_task_id/slack_thread_ts/run_from_* are state, "
        "not event fields)"
    )
    assert fields["outcome"] == "completed", (
        "outcome must come from run_outcome over the row — completed_at set "
        "with the error pair NULL classifies as completed (§3.3 rule 2)"
    )
    assert fields["severity"] == "info", (
        "severity must be SEVERITY_BY_RUN_OUTCOME[outcome] — completed maps "
        "to info (§3.3), riding as a field, not a log level"
    )
    assert fields["total_duration_ms"] == 90000, (
        "total_duration_ms for a completed run must be exactly completed_at "
        "minus detected_at in milliseconds (here 90 s), parsed from the "
        "now_iso ISO-8601 format via datetime.fromisoformat — the round-trip "
        "through the db's timestamp format is the sluice decision under test"
    )
    assert fields["gates.satisfied"] == ["mock_alpha", "mock_beta"], (
        "with both satisfaction columns truthy, both gate ids land in "
        "gates.satisfied in the caller's order (plan 03 §3.2)"
    )
    assert fields["gates.unsatisfied"] == [], (
        "the partition's empty side must still be present as an empty list — "
        "an all-satisfied run has no unsatisfied gates, and readers rely on "
        "both halves existing whenever gates were supplied"
    )


def test_run_event_fabricates_nothing_for_a_fresh_row():
    """A fresh row with every optional column NULL yields exactly the six always-present fields.

    The no-fabrication posture (plan 03 §3.2, the §9/B2 ethic applied to
    observability): absent state yields absent fields, never defaults. The
    regression this catches is an implementation that pads the canonical line
    — a ``None``-valued ``experiment.id``, a ``total_duration_ms`` of 0 for a
    run still in flight, an empty ``gates.satisfied`` when no gates were
    supplied — any of which would make downstream readers unable to tell
    "state says nothing" from "state says zero". Exact key-set equality is the
    assertion, so a fabricated field fails by name.
    """
    # Local import: build_run_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import build_run_event

    fields = build_run_event(make_run_row())

    assert set(fields) == {
        "run.name",
        "run.pipeline_version",
        "run.lifecycle",
        "run.detected_at",
        "outcome",
        "severity",
    }, (
        "a fresh row (every optional column NULL, no gates, no extra) carries "
        "exactly the always-present §3.2 fields — no total_duration_ms (the "
        "run has no terminal timestamp to subtract), no experiment.id/"
        "run.size_tb/error.reason/lifecycle.* (NULL yields absence, never a "
        "None default), and no gates.* (no gates were supplied to partition)"
    )


def test_run_event_partitions_gates_by_satisfaction_in_caller_order():
    """The gates partition preserves the caller's sequence, never a sorted or registry order.

    The regression this catches (plan 03 unit 5, §3.2): an implementation
    that orders the partition by anything OTHER than the supplied sequence —
    alphabetically, by ``Gate.order``, or by registry position. The
    discriminating leg passes the gates REVERSED with both satisfaction
    columns 0, so both ids land in ONE partition (``gates.unsatisfied``) in
    the reversed order — for ``(MOCK_BETA, MOCK_ALPHA)`` every wrong ordering
    happens to coincide with ``["mock_alpha", "mock_beta"]`` and fails. The
    caller's sequence is the seam: later the profile supplies it, and the
    line must reflect what the profile said, not what the builder prefers.
    """
    # Local imports: build_run_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import build_run_event
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA

    # Both columns 0 (the make_run_row default) + the reversed tuple: the
    # single-partition leg where every wrong ordering is distinguishable.
    fields_for_reversed_gates = build_run_event(
        make_run_row(), gates=(MOCK_BETA, MOCK_ALPHA)
    )
    assert fields_for_reversed_gates["gates.satisfied"] == [], (
        "with both satisfaction columns 0, no gate id may land in "
        "gates.satisfied — satisfaction comes from row[gate.satisfied_column] "
        "truthiness, nothing else"
    )
    assert fields_for_reversed_gates["gates.unsatisfied"] == [
        "mock_beta",
        "mock_alpha",
    ], (
        "both unsatisfied gate ids must appear in the CALLER's (reversed) "
        "order — an implementation that sorts alphabetically, by Gate.order, "
        "or by registry position yields ['mock_alpha', 'mock_beta'] and "
        "fails here (plan 03 §3.2: caller order preserved)"
    )

    # Both columns 1 + the forward tuple: the same rule on the other
    # partition, in the order the profile will later supply.
    fields_for_forward_gates = build_run_event(
        make_run_row(mock_alpha_done=1, mock_beta_done=1),
        gates=(MOCK_ALPHA, MOCK_BETA),
    )
    assert fields_for_forward_gates["gates.satisfied"] == [
        "mock_alpha",
        "mock_beta",
    ], (
        "with both columns 1 and the forward tuple, both ids land in "
        "gates.satisfied in the caller's forward order"
    )
    assert fields_for_forward_gates["gates.unsatisfied"] == [], (
        "the empty side of the partition must still be present as an empty "
        "list whenever gates were supplied"
    )


def _materialize_as_sqlite3_row(row_dict):
    """Materialize a dict row as a real ``sqlite3.Row`` with identical columns.

    The two shapes of the §3.1 ``run_row`` duck type must behave identically,
    and only a real ``sqlite3.Row`` exhibits the trap the missing-column test
    below exists for (``row[missing]`` → ``IndexError``, ``in`` → value
    membership) — a dict stand-in would vacuously pass. A ``sqlite3.Row``
    stays readable after its connection closes, so the row is returned bare.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.row_factory = sqlite3.Row
        quoted_column_list = ", ".join(f'"{column}"' for column in row_dict)
        value_placeholders = ", ".join("?" for _ in row_dict)
        connection.execute(f"CREATE TABLE runs ({quoted_column_list})")
        connection.execute(
            f"INSERT INTO runs VALUES ({value_placeholders})",
            tuple(row_dict.values()),
        )
        return connection.execute("SELECT * FROM runs").fetchone()
    finally:
        connection.close()


@pytest.mark.parametrize(
    "materialize_row",
    [lambda row_dict: row_dict, _materialize_as_sqlite3_row],
    ids=["dict-row", "sqlite3-row"],
)
def test_run_event_fails_loud_on_a_missing_satisfaction_column(materialize_row):
    """A gate whose ``satisfied_column`` is absent from the row raises ``KeyError`` naming the column — uniformly for dict and ``sqlite3.Row``.

    The §3.1 implementer trap made executable (plan 03 unit 5): on a
    ``sqlite3.Row``, naive ``row[missing]`` raises ``IndexError`` (not
    ``KeyError``) and ``column in row`` silently tests VALUE membership rather
    than column presence, so the unguarded spelling type-checks, runs, and
    lies — presence must be pre-checked via ``row.keys()``. A registry/schema
    mismatch (a gate registered whose migration never ran) must surface as the
    same ``KeyError`` naming the missing column on BOTH row shapes at build
    time, not as a puzzling ``IndexError`` in the daemon at 2am.
    """
    # Local imports: the missing-column guard is under construction; a
    # module-level events import would fail collection for every other test.
    from sluice.core.events import build_run_event
    from sluice.core.gate import Gate, GateOutcome, GateResult

    # make_gate-style ad-hoc gate (plan 03 unit 5): a frozen Gate record whose
    # satisfaction column exists in no schema — the registry/schema mismatch.
    ghost_gate = Gate(
        id="ghost",
        name="Ghost (satisfaction column missing from the schema)",
        satisfied_column="ghost_done",
        order=99,
        needs=(),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS),
        invalidate=lambda context: None,
    )

    schema_shaped_row = materialize_row(make_run_row())

    with pytest.raises(KeyError) as raised:
        build_run_event(schema_shaped_row, gates=(ghost_gate,))
    assert "ghost_done" in str(raised.value), (
        "the KeyError must NAME the missing satisfaction column so a "
        "registry/schema mismatch is diagnosable from the message alone; got "
        f"{raised.value!r}"
    )


def test_run_event_extra_merges_but_never_clobbers():
    """Caller extras merge into the fields, but a key colliding with a built field raises ``ValueError`` naming it.

    The regression this catches (plan 03 unit 5, §3.2): the ``extra`` seam
    exists so the daemon can pass facts only IT knows (``failed.gate`` at the
    quarantine persist — the builder never infers the failing gate, no
    fabrication), but that seam must not let any caller rewrite the
    builder-owned canonical fields — an implementation that silently
    ``update``s ``extra`` over the built dict would let a caller overwrite
    ``outcome`` and lie about state on the one line operators trust. The
    collision must fail loud with a ``ValueError`` naming the offending key,
    never a silent clobber (guardrail §8, no-silent-fallback).
    """
    # Local import: the extra-merge guard is under construction; a
    # module-level events import would fail collection for every other test.
    from sluice.core.events import build_run_event

    fields = build_run_event(make_run_row(), extra={"failed.gate": "mock_beta"})
    assert fields["failed.gate"] == "mock_beta", (
        "a non-colliding extra must land in the built fields verbatim — the "
        "daemon's failed.gate at the quarantine persist rides this seam "
        "(plan 03 §2 B9: the builder never infers the failing gate)"
    )

    with pytest.raises(ValueError) as raised:
        build_run_event(make_run_row(), extra={"outcome": "completed"})
    assert "outcome" in str(raised.value), (
        "an extra key colliding with a builder-owned field must raise "
        "ValueError NAMING the colliding key — a caller that could rewrite "
        "outcome could lie about state on the canonical line; got "
        f"{raised.value!r}"
    )


def test_emit_run_event_is_one_canonical_info_line():
    """``emit_run_event`` logs exactly one ``run.finished`` info line carrying every built field, and returns the built dict.

    The skill's core rule made executable (plan 03 unit 5, invariant table
    §5): ONE rich canonical line per run per look. The regressions this
    catches: a second ``log.*`` call sneaking into the emitter (scattered
    emits fragment the canonical line into greppable-but-uncorrelatable
    pieces), the event name drifting from the ``run.finished`` literal or the
    level drifting from info (severity rides as a FIELD, §3.3 — the only
    error-level line in the module is ``notify.failed``), the emitter logging
    a subset of what the builder built, and the emitter returning something
    other than the built fields — callers (control Results, tests) rely on
    the return value so they never re-derive the line.
    """
    # Local imports: emit_run_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import build_run_event, emit_run_event
    from sluice.core.mock_gates import MOCK_GATES

    # A completed row with gates makes the field set rich (conditionals,
    # duration, both partition halves) so "every built field appears in the
    # captured entry" is a substantive claim, not a six-key spot check.
    completed_row = make_run_row(
        experiment_id=7,
        size_tb=1.5,
        completed_at="2026-07-21T10:01:30+00:00",
        mock_alpha_done=1,
        mock_beta_done=1,
    )

    with structlog.testing.capture_logs() as captured_log_entries:
        returned_fields = emit_run_event(completed_row, gates=MOCK_GATES)

    assert len(captured_log_entries) == 1, (
        "emit_run_event must emit exactly ONE log line — one canonical event "
        "per run per look (plan 03 §2 B9 / logging skill); got "
        f"{captured_log_entries!r}"
    )
    entry = captured_log_entries[0]
    assert entry["event"] == "run.finished", (
        "the canonical run line must be named run.finished — subsystem 9's "
        "per-run jsonl processor and operators key on this literal"
    )
    assert entry["log_level"] == "info", (
        "every event emits at level info (plan 03 §2: severity is a FIELD on "
        "the line, never a log level — quarantined rides as "
        "severity='critical' at info)"
    )

    # build_run_event is a pure function of (row, gates, extra), so rebuilding
    # with the same arguments yields the fields the emitter must have logged.
    built_fields = build_run_event(completed_row, gates=MOCK_GATES)
    for field_name, field_value in built_fields.items():
        assert entry[field_name] == field_value, (
            f"built field {field_name!r} must appear on the emitted line with "
            "its built value — an emitter that logs a subset (or mutates a "
            "value) breaks the one-rich-line contract"
        )
    assert returned_fields == built_fields, (
        "emit_run_event must return the built field dict — callers (control "
        "Results, tests) read the fields from the return value instead of "
        "re-deriving the canonical line (plan 03 §3.1)"
    )


# --------------------------------------------------------------------------
# Unit 6 — build_gate_event + emit_gate_event (per-gate per-look; where
# timings live).
# --------------------------------------------------------------------------


def test_gate_event_fields_for_a_success_with_metadata():
    """``build_gate_event`` maps a timed SUCCESS with metadata to exactly the five §3.2 ``gate.finished`` fields.

    Three decisions ride on this exact-dict equality (plan 03 unit 6, §3.2):
    ``gate.outcome`` is the enum's ``.value`` STRING (``"success"``, the
    persisted/logged vocabulary — not the ``GateOutcome`` member, whose repr
    would leak into the jsonl); ``result.metadata`` rides NESTED under
    ``gate.metadata`` (splatting its keys into the line could collide with
    the canonical field names — nesting is collision-proof, and
    ``JSONRenderer`` handles it); and ``run.name`` appears on every gate line
    because correlation with the run's canonical line is BY FIELD — the
    contextvar accumulator that once carried run identity implicitly was
    deliberately not ported (B9/D6). ``gate.duration_ms`` carries the
    daemon-measured timing verbatim: per-gate timings live on ``gate.finished``
    lines, not on the run row (§2 B9).
    """
    # Local imports: build_gate_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import build_gate_event
    from sluice.core.gate import GateOutcome, GateResult
    from sluice.core.mock_gates import MOCK_ALPHA

    fields = build_gate_event(
        "r1",
        MOCK_ALPHA,
        GateResult(GateOutcome.SUCCESS, metadata={"marker": "/tmp/x"}),
        duration_ms=42,
    )

    assert fields == {
        "run.name": "r1",
        "gate.id": "mock_alpha",
        "gate.outcome": "success",
        "gate.duration_ms": 42,
        "gate.metadata": {"marker": "/tmp/x"},
    }, (
        "a SUCCESS with metadata and a duration carries exactly these five "
        "fields (plan 03 §3.2): run.name for by-field correlation (no "
        "contextvars), gate.outcome as the enum's .value string, the "
        "daemon-measured duration verbatim, metadata NESTED (never splatted) "
        "— and nothing else: no gate.reason (the result carried none, absence "
        "yields absence) and no severity (run-line only, PP parity)"
    )


def test_gate_event_failure_carries_the_reason_and_omits_absences():
    """A FAILURE with a reason but no metadata and no duration carries exactly four fields, ``gate.reason`` among them.

    The complement of the success test (plan 03 unit 6, §3.2): the reason —
    the one fact an operator greps for at a quarantine — must ride the line
    verbatim under ``gate.reason``, and the absent optionals must be ABSENT,
    not defaulted. The regression this catches is fabrication (the §9/B2
    no-fabrication ethic applied per-gate): a ``gate.duration_ms`` of 0 for a
    gate the daemon never timed, or a ``None``/``{}`` ``gate.metadata``, would
    make downstream readers unable to tell "the daemon measured zero" from
    "nobody measured anything". Exact key-set equality fails a padded field
    by name.
    """
    # Local imports: build_gate_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import build_gate_event
    from sluice.core.gate import GateOutcome, GateResult
    from sluice.core.mock_gates import MOCK_ALPHA

    fields = build_gate_event(
        "r1", MOCK_ALPHA, GateResult(GateOutcome.FAILURE, reason="boom")
    )

    assert set(fields) == {
        "run.name",
        "gate.id",
        "gate.outcome",
        "gate.reason",
    }, (
        "a FAILURE with a reason, no metadata, and no duration carries "
        "exactly the three always fields plus gate.reason (plan 03 §3.2): an "
        "absent duration is absent, never 0, and absent metadata is absent, "
        "never None/{} — no-fabrication, per gate line"
    )
    assert fields["gate.reason"] == "boom", (
        "the result's reason must ride the line verbatim under gate.reason — "
        "it is the fact the daemon persists to error_reason and the operator "
        "greps for at a quarantine"
    )


def test_gate_event_keeps_a_zero_millisecond_duration():
    """A supplied ``duration_ms`` of 0 rides the line as ``gate.duration_ms: 0`` — supplied-but-falsy is not absent.

    No-fabrication (plan 03 unit 6, §9/B2) cuts both ways: an absent duration
    must be absent, but a legitimate daemon measurement of zero milliseconds —
    a gate that finished inside one clock tick — was SUPPLIED and must appear.
    The regression this catches is the truthiness mutant ``if duration_ms:``
    replacing ``if duration_ms is not None:``, which silently drops 0-ms
    timings and makes "the daemon measured zero" indistinguishable from
    "nobody measured anything" downstream.
    """
    # Local imports: build_gate_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import build_gate_event
    from sluice.core.gate import GateOutcome, GateResult
    from sluice.core.mock_gates import MOCK_ALPHA

    fields = build_gate_event(
        "r1", MOCK_ALPHA, GateResult(GateOutcome.SUCCESS), duration_ms=0
    )

    assert fields.get("gate.duration_ms") == 0, (
        "duration_ms=0 is a supplied measurement, not an absence: the "
        "presence check must be `is not None`, never truthiness — a falsy "
        "check would silently drop legitimate 0-ms timings (no-fabrication, "
        "plan 03 §3.2)"
    )


def test_emit_gate_event_is_one_info_line():
    """``emit_gate_event`` logs exactly one ``gate.finished`` info line carrying the built fields, and returns the built dict.

    The per-gate half of the one-line rule (plan 03 unit 6, invariant table
    §5): one ``gate.finished`` line per gate per persist. The regressions this
    catches mirror the run emitter's: a second ``log.*`` call inside the
    emitter, the event name drifting from the ``gate.finished`` literal or the
    level drifting from info (severity never rides gate lines at all, and the
    module's only error-level line is ``notify.failed``), the emitter logging
    a subset of what the builder built, and the emitter returning anything
    other than the built fields — subsystem 9 emits this at every persist and
    reads the fields from the return value instead of re-deriving them (§3.1).
    """
    # Local imports: emit_gate_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import build_gate_event, emit_gate_event
    from sluice.core.gate import GateOutcome, GateResult
    from sluice.core.mock_gates import MOCK_ALPHA

    # A timed SUCCESS with metadata makes the field set rich (all five §3.2
    # fields, one nested) so "every built field appears in the captured
    # entry" is a substantive claim, not a three-key spot check.
    timed_success_result = GateResult(
        GateOutcome.SUCCESS, metadata={"marker": "/tmp/x"}
    )

    with structlog.testing.capture_logs() as captured_log_entries:
        returned_fields = emit_gate_event(
            "r1", MOCK_ALPHA, timed_success_result, duration_ms=42
        )

    assert len(captured_log_entries) == 1, (
        "emit_gate_event must emit exactly ONE log line — one gate.finished "
        "event per gate per persist (plan 03 unit 6 / logging skill); got "
        f"{captured_log_entries!r}"
    )
    entry = captured_log_entries[0]
    assert entry["event"] == "gate.finished", (
        "the per-gate line must be named gate.finished — operators and "
        "subsystem 9's processors correlate it with run.finished by literal "
        "event name plus the run.name field"
    )
    assert entry["log_level"] == "info", (
        "every event emits at level info (plan 03 §2: even a FAILURE result "
        "rides an info-level gate.finished line — the only error-LEVEL line "
        "in the module is notify.failed)"
    )

    # build_gate_event is a pure function of its arguments, so rebuilding
    # with the same arguments yields the fields the emitter must have logged.
    built_fields = build_gate_event(
        "r1", MOCK_ALPHA, timed_success_result, duration_ms=42
    )
    for field_name, field_value in built_fields.items():
        assert entry[field_name] == field_value, (
            f"built field {field_name!r} must appear on the emitted line "
            "with its built value — an emitter that logs a subset (or "
            "mutates a value) breaks the one-rich-line contract"
        )
    assert returned_fields == built_fields, (
        "emit_gate_event must return the built field dict — callers read the "
        "fields from the return value instead of re-deriving the line "
        "(plan 03 §3.1: emitters call log.info once and return the fields)"
    )


# --------------------------------------------------------------------------
# Unit 7 — control_event (§9's wide event; §11's no-Slack, structural).
# --------------------------------------------------------------------------


def test_control_event_emits_one_line_with_the_op_facts():
    """``control_event`` logs exactly one ``control.finished`` info line carrying the op facts, and returns the field dict.

    The event half of §9's "audit row + wide event" per control op (plan 03
    unit 7; the audit row is subsystem 7's, the ordering — mutate → audit →
    event — is subsystem 8's). The regressions this catches: a second
    ``log.*`` call inside the emitter (one canonical line per op), the event
    name drifting from the ``control.finished`` literal or the level drifting
    from info, ``result`` splatted instead of riding NESTED under
    ``control.result`` (splatting could collide with the canonical field
    names), ``run.name`` dropped from a targeted op (subsystem 9's per-run
    jsonl routing keys on that literal), and a return value other than the
    field dict — subsystem 8's ops read the fields from the return value
    instead of re-deriving the line (§3.1).
    """
    # Local import: control_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import control_event

    with structlog.testing.capture_logs() as captured_log_entries:
        returned_fields = control_event(
            "hold", "will", target_run="r1", result={"ok": True}
        )

    assert len(captured_log_entries) == 1, (
        "control_event must emit exactly ONE log line — one control.finished "
        "event per control op (plan 03 unit 7 / logging skill); got "
        f"{captured_log_entries!r}"
    )
    entry = captured_log_entries[0]
    assert entry["event"] == "control.finished", (
        "the control line must be named control.finished — operators and "
        "subsystem 9's processors key on this literal"
    )
    assert entry["log_level"] == "info", (
        "every event emits at level info (plan 03 §2: severity rides as a "
        "field; the module's only error-LEVEL line is notify.failed)"
    )

    expected_fields = {
        "control.action": "hold",
        "control.actor": "will",
        "run.name": "r1",
        "control.result": {"ok": True},
    }
    for field_name, field_value in expected_fields.items():
        assert entry[field_name] == field_value, (
            f"the emitted line must carry {field_name!r} == {field_value!r} "
            "(plan 03 §3.2): the op facts — action, actor, targeted run, and "
            "the result NESTED under control.result, never splatted"
        )
    assert returned_fields == expected_fields, (
        "control_event must return exactly the §3.2 field dict — subsystem "
        "8's ops read the fields from the return value instead of "
        "re-deriving the line (plan 03 §3.1: emitters call log.info once "
        "and return the built fields)"
    )


def test_control_event_global_op_carries_no_run_name():
    """A global op (no ``target_run``, no ``result``) carries neither ``run.name`` nor ``control.result``.

    The no-fabrication posture applied to control ops (plan 03 unit 7, §3.2):
    ``pause``/``resume`` target the world, not a run, so their line has no
    ``run.name`` — and the absence is load-bearing on the LOGGED line, not just
    the return value, because subsystem 9's per-run jsonl processor routes on
    the ``run.name`` literal (§3.2): a fabricated ``run.name`` (even ``None``
    or ``""``) would misfile every global op into some run's event file.
    Likewise an absent ``result`` stays absent, never a ``None``/``{}``
    default — absent state yields absent fields.
    """
    # Local import: control_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import control_event

    with structlog.testing.capture_logs() as captured_log_entries:
        returned_fields = control_event("pause", "will")

    assert len(captured_log_entries) == 1
    emitted_entry = captured_log_entries[0]
    assert "run.name" not in emitted_entry, (
        "a global op's control.finished line must carry NO run.name field — "
        "subsystem 9's per-run jsonl routing keys on that literal, and a "
        "fabricated run.name would misfile the world-targeting op into a "
        f"run's event file; got {emitted_entry!r}"
    )
    assert "control.result" not in emitted_entry, (
        "an op called without a result must carry NO control.result field — "
        "absent state yields absent fields, never a None/{} default; got "
        f"{emitted_entry!r}"
    )
    assert "run.name" not in returned_fields, (
        "the returned field dict must match the emitted absence — callers "
        "(subsystem 8's ops) read these fields from the return value; got "
        f"{returned_fields!r}"
    )
    assert "control.result" not in returned_fields, (
        "the returned field dict must match the emitted absence — callers "
        "(subsystem 8's ops) read these fields from the return value; got "
        f"{returned_fields!r}"
    )


def test_control_event_cannot_reach_slack():
    """``control_event``'s signature is exactly ``(action, actor, target_run, result)`` — no notifier can be passed.

    §11's policy — control actions never post to Slack — made STRUCTURAL
    (plan 03 unit 7, §2): the policy is unviolatable at the signature level
    because there is no notifier parameter to call. The regression this
    catches is a ``notifier`` (or any) parameter creeping into the signature
    — the one way this module could route a control op to Slack — so editing
    the parameter list becomes a conscious, test-breaking policy change, never
    a drive-by convenience. Signature equality by parameter-name list is the
    whole assertion; only pipeline milestones (daemon-called notifier hooks
    via ``safe_notify``) ever reach Slack.
    """
    # Local import: control_event is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.events import control_event

    assert list(inspect.signature(control_event).parameters) == [
        "action",
        "actor",
        "target_run",
        "result",
    ], (
        "control_event must take exactly (action, actor, target_run, result) "
        "— §11: control actions never post to Slack, so no notifier "
        "parameter may exist; changing this signature is a conscious policy "
        "change (plan 03 unit 7)"
    )


# --------------------------------------------------------------------------
# Unit 8 — subsystem-closing surface + real-substrate integration.
#
# With units 1-7 landed the public surface is complete, so plan 03 §1's
# "``__all__`` = the ten documented names" becomes executable (db unit-7
# precedent, ``test_db_exports_exactly_the_documented_surface``).
# --------------------------------------------------------------------------


def test_events_exports_exactly_the_documented_surface():
    """``sluice.core.events.__all__`` names exactly the plan 03 §1 owned surface.

    Makes §1's documented ``__all__`` executable by set equality, catching
    future scope creep in either direction: a missing name breaks a documented
    downstream seam (subsystem 8 imports ``control_event``/``run_outcome``,
    subsystem 9 imports the emitters + ``safe_notify``, Plan B implements
    ``Notifier``), while an extra name is B8 creep — notably, no exception
    type, no notifier implementation, and no gate-outcome vocabulary may ever
    join this surface (§2: no exception enters events; core ships the Protocol
    only; ``GateOutcome`` belongs to ``sluice.core.gate``).
    """
    # Local import: the events surface is under construction; a module-level
    # import would fail collection for every other test in this file.
    import sluice.core.events

    documented_surface = {
        # B8's injection contract + the never-fail call helper.
        "Notifier",
        "safe_notify",
        # The closed run-outcome vocabulary.
        "run_outcome",
        "SEVERITY_BY_RUN_OUTCOME",
        # The canonical per-run wide event (B9, design half).
        "build_run_event",
        "emit_run_event",
        # The per-gate per-look event.
        "build_gate_event",
        "emit_gate_event",
        # The control-op wide event (§9/§11).
        "control_event",
        # Slack milestone text helper.
        "format_duration",
    }

    assert set(sluice.core.events.__all__) == documented_surface, (
        "events.__all__ must equal the ten documented names of plan 03 §1 — "
        "a missing name breaks a downstream import seam (subsystems 8/9, "
        "Plan B), and an extra name is surface creep past what the plan owns"
    )


def test_run_event_reads_a_real_sqlite_row(tmp_path):
    """The whole quarantine pipeline runs off a real ``sqlite3.Row`` from the real schema — and changes no state.

    Every builder test above drives ``build_run_event`` through dict rows
    shaped like the schema; this is the integration that removes the "shaped
    like" caveat (plan 03 unit 8). The load-bearing part is the §3.1
    ``row.keys()`` duck-typing seam exercised where the daemon will exercise
    it: a real ``sqlite3.Row`` from ``get_run`` against the migrated schema's
    actual column names — the row shape ``make_run_row`` merely imitates. A
    drift in either direction (an events implementation that quietly assumes
    dict semantics — on a ``sqlite3.Row``, ``in`` tests VALUE membership, so a
    ``column in row`` presence check would reject columns that exist — or a
    ``make_run_row`` column list drifting from the migrations) fails here, not
    in the daemon at 2am. The closing unchanged-row assertion is the
    behavioral echo of the unit-1 structural guard: building and emitting the
    quarantine line described state and acted on none of it (ADR-0002).
    """
    # Local imports: the events surface is under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        now_iso,
        update_run,
    )
    from sluice.core.events import emit_run_event, run_outcome
    from sluice.core.mock_gates import MOCK_GATES

    database_path = tmp_path / "state.db"
    initialize_database(database_path)
    connection = get_connection(database_path)
    try:
        # A run that satisfies mock_alpha, then quarantines before mock_beta —
        # the daemon passes failed.gate at the FAILURE persist (§2 B9, ⚑1);
        # the builder never infers it. get_connection is autocommit, so each
        # statement commits alone (the write_txn discipline is db's concern).
        insert_run(connection, "r1", pipeline_version="v1")
        update_run(connection, "r1", {"mock_alpha_done": 1})
        update_run(connection, "r1", {"error_reason": "boom", "errored_at": now_iso()})
        real_row = get_run(connection, "r1")

        # Premise guard: the whole point is the sqlite3.Row substrate — if
        # get_run ever returned dicts, this test would silently stop covering
        # the seam it exists for.
        assert isinstance(real_row, sqlite3.Row), (
            "get_run must hand back a real sqlite3.Row — this test's claim "
            "is that events runs off the daemon's actual row type, not a "
            f"dict imitation; got {type(real_row)!r}"
        )

        assert run_outcome(real_row) == "quarantined", (
            "a row with errored_at set must classify as quarantined off the "
            "real sqlite3.Row exactly as off the dict rows of unit 4 — the "
            "§3.1 duck type has two shapes and one behavior"
        )

        with structlog.testing.capture_logs() as captured_log_entries:
            emitted_fields = emit_run_event(
                real_row, MOCK_GATES, extra={"failed.gate": "mock_beta"}
            )

        assert len(captured_log_entries) == 1, (
            "one canonical run.finished line per run per look, off the real "
            f"substrate too; got {captured_log_entries!r}"
        )
        entry = captured_log_entries[0]
        assert entry["event"] == "run.finished"
        assert entry["severity"] == "critical", (
            "a quarantined run's line must carry severity critical (⚑2, "
            "ADR-0002: every quarantine is operator-actionable) — derived "
            "from the real row's errored_at, not a dict stand-in"
        )
        assert entry["gates.satisfied"] == ["mock_alpha"], (
            "the satisfaction partition must read the real schema's "
            "mock_alpha_done column off the sqlite3.Row — the row.keys() "
            "presence seam against real column names (§3.1)"
        )
        assert entry["gates.unsatisfied"] == ["mock_beta"], (
            "mock_beta_done stayed 0, so mock_beta lands unsatisfied — the "
            "partition's other half off the same real row"
        )
        assert entry["error.reason"] == "boom", (
            "the persisted error_reason must ride the line as error.reason — "
            "the operator's grep target at a quarantine"
        )
        assert (
            isinstance(entry["total_duration_ms"], int)
            and entry["total_duration_ms"] >= 0
        ), (
            "a quarantined run is terminal, so total_duration_ms must be a "
            "non-negative int: errored_at minus detected_at, both written by "
            "the db's now_iso and parsed back via fromisoformat — the ISO "
            "round-trip proven against timestamps the db actually wrote; got "
            f"{entry['total_duration_ms']!r}"
        )
        assert entry["failed.gate"] == "mock_beta", (
            "the daemon-supplied extra must ride the line verbatim — the "
            "quarantine emission's failed.gate seam (§2 B9, ⚑1)"
        )
        assert emitted_fields == {
            field_name: entry[field_name] for field_name in emitted_fields
        }, (
            "the returned field dict must match the emitted line — callers "
            "read fields from the return value instead of re-deriving them"
        )

        # Events described state and acted on none of it: the row re-read
        # after building + emitting is identical (the behavioral echo of the
        # unit-1 structural guard — no sqlite3/db import means no write path,
        # and here is the observable consequence).
        assert dict(get_run(connection, "r1")) == dict(real_row), (
            "building and emitting the run event must change no run state — "
            "events describe state, they never act on it (ADR-0002, plan 03 "
            "§5 invariant: DB is the single truth)"
        )
    finally:
        connection.close()
