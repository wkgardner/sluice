"""Behavioral tests for ``sluice.core.daemon`` — the reconcile loop (plan 09).

The daemon is the sole actor (design §2/§7): it composes every built module,
reads state, decides via ``reconcile``, runs one gate per look, persists the
result directly (never through control ops, never audited — plan 07 §6), and
emits the canonical events. These tests assert on state (DB + filesystem +
captured events) only — never by monkeypatching a running loop — and drive the
capstones through the real ``run_cycle`` plus pure control ops.
"""

import ast
import inspect
import json
import logging
import pathlib
import signal

import pytest
import structlog
import structlog.testing

from sluice.core import control, daemon
from sluice.core.daemon import (
    DaemonLockError,
    acquire_daemon_lock,
    assemble_registry,
    configure_logging,
    detect_new_runs,
    install_signal_handlers,
    prepare_database,
    process_run,
    run_cycle,
    DaemonHostError,
    assert_daemon_host,
    daemon_host_path,
    record_daemon_host,
    run_forever,
)
from sluice.core.db import (
    UnknownColumnError,
    get_connection,
    get_metadata,
    get_run,
    insert_run,
    set_metadata,
    update_run,
    write_txn,
)
from sluice.core.gate import Gate, GateOutcome, GateResult
from sluice.core.mock_gates import MOCK_ALPHA
from sluice.core.profile import (
    REFERENCE_PROFILE_ALPHA_DISABLED,
    REFERENCE_PROFILES,
    Applicability,
    build_profile,
)

# --------------------------------------------------------------------------
# Shared substrate.
# --------------------------------------------------------------------------

DEFAULT_VERSION = "reference-v1"
DISABLED_VERSION = "reference-v1-alpha-disabled"


@pytest.fixture(autouse=True)
def _isolate_process_state():
    """Restore signal handlers and structlog/logging config after every test.

    The daemon installs SIGTERM/SIGINT handlers and ``configure_logging``
    rewires the global structlog config plus stdlib sink loggers; without a
    reset a configure-logging test could leave handlers pointed at a deleted
    ``tmp_path`` file that a later unrelated emit would try to write.
    """
    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_sigint = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGTERM, original_sigterm)
    signal.signal(signal.SIGINT, original_sigint)
    structlog.reset_defaults()
    for sink_name in ("sluice._events_sink", "sluice._console_sink"):
        sink_logger = logging.getLogger(sink_name)
        for handler in list(sink_logger.handlers):
            sink_logger.removeHandler(handler)
            handler.close()


@pytest.fixture
def config(tmp_path):
    """A fail-loud config dict over ``tmp_path`` with the watch directory created."""
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    return {
        "state_file": str(tmp_path / "state.db"),
        "watch_dir": str(watch_dir),
        "local_staging_dir": str(tmp_path / "staging"),
        "events_file": str(tmp_path / "logs" / "events.jsonl"),
        "run_events_dir": str(tmp_path / "logs" / "runs"),
        "events_max_mb": 100,
        "events_backup_count": 5,
        "poll_interval_minutes": 10,
    }


@pytest.fixture
def prepared(config):
    """A config whose database is migrated and seeded with ``reference-v1``."""
    prepare_database(
        config, registry=REFERENCE_PROFILES, default_pipeline_version=DEFAULT_VERSION
    )
    return config


class RecordingNotifier:
    """A 3-method notifier that records every call for assertion (plan 09 §4)."""

    def __init__(self):
        self.calls = []

    def open_run_thread(self, run_name, experiment_id=None, size_tb=None):
        """Record an open-thread call and return no thread id (A5 is Plan B)."""
        self.calls.append(("open_run_thread", run_name))
        return None

    def update_run_header(self, run_name):
        """Record a parent-header rewrite."""
        self.calls.append(("update_run_header", run_name))

    def post_milestone(self, run_name, text, mention=False):
        """Record a milestone post."""
        self.calls.append(("post_milestone", run_name, text))

    def finish_run_thread(self, run_name, outcome, reason=None):
        """Record a finish-thread call with its terminal outcome."""
        self.calls.append(("finish_run_thread", run_name, outcome, reason))


class RecordingDiskCheck:
    """A recording disk-floor seam returning a fixed reason (or ``None`` = healthy)."""

    def __init__(self, reason=None):
        self.reason = reason
        self.calls = 0

    def __call__(self, config):
        """Record the call and return the configured floor reason."""
        self.calls += 1
        return self.reason


def _make_folder(config, name):
    """Create a watch-directory subfolder for ``name``."""
    (pathlib.Path(config["watch_dir"]) / name).mkdir(parents=True, exist_ok=True)


def _detect(config, name, notifier=None):
    """Create ``name``'s watch folder and run one detection sweep."""
    _make_folder(config, name)
    return detect_new_runs(config, notifier=notifier)


def _set_columns(config, name, **columns):
    """Directly write run columns (test scaffolding for pre-satisfied state)."""
    with write_txn(config["state_file"]) as connection:
        update_run(connection, name, columns)


def _write_beta_instruction(config, name, text):
    """Script ``mock_beta``'s outcome by writing its instruction file."""
    run_dir = pathlib.Path(config["local_staging_dir"]) / name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "mock_beta.instruction").write_text(text, encoding="utf-8")


def _run_row(config, name):
    """Fetch a run row on a throwaway connection."""
    connection = get_connection(config["state_file"])
    try:
        return get_run(connection, name)
    finally:
        connection.close()


def _audit_rows(config):
    """Return every ``control_audit`` row ordered by id."""
    connection = get_connection(config["state_file"])
    try:
        return connection.execute("SELECT * FROM control_audit ORDER BY id").fetchall()
    finally:
        connection.close()


def _events_named(captured, event_name):
    """Filter captured structlog entries to those whose event is ``event_name``."""
    return [entry for entry in captured if entry["event"] == event_name]


class StopAfter:
    """A ``should_stop`` that returns False for the first ``n`` calls, then True."""

    def __init__(self, n):
        self.n = n
        self.calls = 0

    def __call__(self):
        """Increment the call count and report whether the budget is spent."""
        self.calls += 1
        return self.calls > self.n


# --------------------------------------------------------------------------
# Unit 1 — import hygiene + single-instance lock.
# --------------------------------------------------------------------------

ALLOWED_DAEMON_IMPORTS = {
    "fcntl",
    "json",
    "logging",
    "logging.handlers",
    "os",
    "pathlib",
    "signal",
    "threading",
    "time",
    "types",
    "collections.abc",
    "structlog",
    "sluice.core.db",
    "sluice.core.gate",
    "sluice.core.events",
    "sluice.core.context",
    "sluice.core.profile",
    "sluice.core.reconcile",
    "sluice.core.control",
    # The cooperative shutdown flag (Bug B, HANDOFF 3ac): signal handlers set
    # it, long-running gate internals poll it at submission boundaries.
    "sluice.core.stop",
}

FORBIDDEN_ATTRIBUTE_NAMES = {"invalidate", "unlink", "rmtree", "remove"}


def test_daemon_import_surface_is_closed():
    """``daemon.py`` imports only the §3.11 set and never deletes or invalidates.

    The ADR-0005 structural pin plus the closed import surface: parsed with
    ``ast`` (function-local smuggling is covered), imports must be a subset of
    the plan's closed set with ``subprocess``/``shutil``/``slack_sdk`` absent by
    name, and no attribute reference named ``invalidate``/``unlink``/``rmtree``/
    ``remove`` may appear anywhere — the daemon starts work only through gate
    references and deletes nothing.
    """
    source_path = inspect.getsourcefile(daemon)
    tree = ast.parse(pathlib.Path(source_path).read_text(encoding="utf-8"))

    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = "sluice.core"
                resolved = f"{base}.{node.module}" if node.module else base
            else:
                resolved = node.module
            if resolved is None:
                continue
            if resolved in ("sluice", "sluice.core"):
                imported_modules.update(
                    f"{resolved}.{alias.name}" for alias in node.names
                )
            else:
                imported_modules.add(resolved)

    def is_allowed(module_name):
        return any(
            module_name == allowed or module_name.startswith(f"{allowed}.")
            for allowed in ALLOWED_DAEMON_IMPORTS
        )

    outside = sorted(m for m in imported_modules if not is_allowed(m))
    assert outside == [], f"daemon imports outside the closed set: {outside}"

    for forbidden in ("subprocess", "shutil", "slack_sdk"):
        assert forbidden not in imported_modules, (
            f"{forbidden!r} must be absent — the daemon spawns nothing and "
            "reaches no Slack SDK directly (ADR-0005 / plan 09 §3.11)"
        )

    offending_attributes = sorted(
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTE_NAMES
    )
    assert offending_attributes == [], (
        "the daemon must never reference invalidate/unlink/rmtree/remove — it "
        f"deletes nothing (ADR-0005); found {offending_attributes}"
    )


def test_second_lock_acquisition_refuses_loud(tmp_path):
    """A second lock acquisition raises ``DaemonLockError``; release re-enables it.

    Single-instance is the ``fcntl.flock`` alone (§5): while a handle holds the
    lock, a second acquisition on the same path is refused loud; closing the
    first handle drops the lock (no reaper), so a third acquisition succeeds.
    """
    lock_path = str(tmp_path / "state.db.lock")

    first_handle = acquire_daemon_lock(lock_path)
    with pytest.raises(DaemonLockError):
        acquire_daemon_lock(lock_path)

    first_handle.close()

    third_handle = acquire_daemon_lock(lock_path)
    third_handle.close()


# --------------------------------------------------------------------------
# Unit 2 — registry assembly + startup.
# --------------------------------------------------------------------------


def test_assemble_registry_raises_on_key_collision_and_is_read_only():
    """``assemble_registry`` refuses a duplicate key and returns a read-only union.

    Plan 05 §7's collision guard: a second registry re-keying ``reference-v1``
    raises ``ValueError`` naming the key (never a silent ``{**a, **b}`` shadow);
    a clean union carries both sides and refuses item assignment.
    """
    shadow_registry = {"reference-v1": REFERENCE_PROFILE_ALPHA_DISABLED}
    with pytest.raises(ValueError, match="reference-v1"):
        assemble_registry(REFERENCE_PROFILES, shadow_registry)

    # A clean union of two disjoint single-entry registries.
    union = assemble_registry(
        {"reference-v1": REFERENCE_PROFILES["reference-v1"]},
        {DISABLED_VERSION: REFERENCE_PROFILE_ALPHA_DISABLED},
    )
    assert set(union) == {"reference-v1", DISABLED_VERSION}
    with pytest.raises(TypeError):
        union["reference-v1"] = REFERENCE_PROFILE_ALPHA_DISABLED


def test_prepare_database_migrates_and_seeds_once(config):
    """``prepare_database`` migrates to head and seeds the default once.

    A fresh path lands the schema at head and seeds
    ``metadata.pipeline_profile`` with the default; a second call with a
    *different* default leaves the stored value untouched (seed-once — an
    operator's value survives restarts).
    """
    db_path = prepare_database(
        config, registry=REFERENCE_PROFILES, default_pipeline_version=DEFAULT_VERSION
    )
    connection = get_connection(db_path)
    try:
        assert get_metadata(connection, control.PIPELINE_PROFILE_KEY) == "reference-v1"
    finally:
        connection.close()

    prepare_database(
        config, registry=REFERENCE_PROFILES, default_pipeline_version=DISABLED_VERSION
    )
    connection = get_connection(db_path)
    try:
        assert get_metadata(connection, control.PIPELINE_PROFILE_KEY) == "reference-v1"
    finally:
        connection.close()


def test_prepare_database_fails_loud_on_unresolvable_default(config):
    """An unresolvable default (fresh or stored) is a loud startup error.

    A default not in the registry raises and seeds nothing; and a hand-written
    garbage stored value is caught by the stored-value validation on a later
    call.
    """
    from sluice.core.profile import UnknownProfileError

    with pytest.raises(UnknownProfileError):
        prepare_database(
            config, registry=REFERENCE_PROFILES, default_pipeline_version="ghost-v9"
        )
    connection = get_connection(config["state_file"])
    try:
        assert get_metadata(connection, control.PIPELINE_PROFILE_KEY) is None
    finally:
        connection.close()

    prepare_database(
        config, registry=REFERENCE_PROFILES, default_pipeline_version=DEFAULT_VERSION
    )
    with write_txn(config["state_file"]) as connection:
        set_metadata(connection, control.PIPELINE_PROFILE_KEY, "garbage-vX")
    with pytest.raises(UnknownProfileError):
        prepare_database(
            config,
            registry=REFERENCE_PROFILES,
            default_pipeline_version=DEFAULT_VERSION,
        )


# --------------------------------------------------------------------------
# Unit 3 — detection.
# --------------------------------------------------------------------------


def test_detection_inserts_active_rows_stamped_with_the_default(prepared):
    """Detection lists subdirectories only and stamps the metadata default.

    Two folders plus one stray *file* under ``watch_dir`` yield exactly two
    active rows stamped ``reference-v1``; the file is not a run.
    """
    _make_folder(prepared, "run_a")
    _make_folder(prepared, "run_b")
    (pathlib.Path(prepared["watch_dir"]) / "stray.txt").write_text(
        "x", encoding="utf-8"
    )

    detected = detect_new_runs(prepared)
    assert detected == ["run_a", "run_b"]
    for name in ("run_a", "run_b"):
        row = _run_row(prepared, name)
        assert row["lifecycle"] == "active"
        assert row["pipeline_version"] == "reference-v1"
    assert _run_row(prepared, "stray.txt") is None


def test_detection_is_idempotent_and_emits_once(prepared):
    """A second sweep inserts nothing and emits/notifies exactly once per run.

    ``insert_run``'s idempotent bool gates the ``run.detected`` line and the
    ``open_run_thread`` notify, so a re-sweep is silent.
    """
    notifier = RecordingNotifier()
    _make_folder(prepared, "run_a")

    with structlog.testing.capture_logs() as captured:
        first = detect_new_runs(prepared, notifier=notifier)
        second = detect_new_runs(prepared, notifier=notifier)

    assert first == ["run_a"]
    assert second == []
    assert len(_events_named(captured, "run.detected")) == 1
    assert notifier.calls.count(("open_run_thread", "run_a")) == 1


def test_pipeline_version_default_is_read_only_at_detection(prepared):
    """The default is read only at detection — an existing run is never restamped.

    Detect run1 under ``reference-v1``; switch the active default; add run2 and
    detect — run1 keeps ``reference-v1`` (never restamped), run2 takes the new
    default. The §8 freeze mechanism.
    """
    _make_folder(prepared, "run1")
    detect_new_runs(prepared)

    control.set_default_profile(
        prepared["state_file"], "will", DISABLED_VERSION, registry=REFERENCE_PROFILES
    )
    _make_folder(prepared, "run2")
    detect_new_runs(prepared)

    assert _run_row(prepared, "run1")["pipeline_version"] == "reference-v1"
    assert _run_row(prepared, "run2")["pipeline_version"] == DISABLED_VERSION


def test_detection_fails_loud_without_watch_dir_or_seed(config, tmp_path):
    """Detection fails loud on a missing ``watch_dir`` key or an unseeded default.

    A config lacking ``watch_dir`` raises ``KeyError``; a seeded DB whose
    ``pipeline_profile`` key was deleted raises ``RuntimeError`` naming it.
    """
    from sluice.core.db import initialize_database

    initialize_database(config["state_file"])
    no_watch = dict(config)
    del no_watch["watch_dir"]
    with pytest.raises(KeyError):
        detect_new_runs(no_watch)

    # Seed then delete the key to reach the unseeded-at-detection guard.
    with write_txn(config["state_file"]) as connection:
        set_metadata(connection, control.PIPELINE_PROFILE_KEY, "reference-v1")
    with write_txn(config["state_file"]) as connection:
        connection.execute(
            "DELETE FROM poller_metadata WHERE key = ?",
            (control.PIPELINE_PROFILE_KEY,),
        )
    with pytest.raises(RuntimeError, match=control.PIPELINE_PROFILE_KEY):
        detect_new_runs(config)


# --------------------------------------------------------------------------
# Unit 4 — the per-run step (process_run), exhaustively.
# --------------------------------------------------------------------------


def test_success_persists_column_and_emits_gate_event(prepared):
    """A SUCCESS persists the column, mkdirs the workdir, emits, and notifies.

    Drive run1 at gate alpha with a ``should_stop`` that stops after one gate:
    ``mock_alpha_done == 1``, the marker exists under the daemon-created
    workdir, the ``gate.finished`` line carries the gate id/outcome/an integer
    duration and nested metadata, no other column moved, and ``post_milestone``
    is called once with the gate's name.
    """
    notifier = RecordingNotifier()
    _detect(prepared, "run1", notifier=notifier)

    with structlog.testing.capture_logs() as captured:
        token = process_run(
            prepared,
            "run1",
            registry=REFERENCE_PROFILES,
            notifier=notifier,
            should_stop=StopAfter(1),
        )

    assert token == "stopped"
    row = _run_row(prepared, "run1")
    assert row["mock_alpha_done"] == 1
    assert row["mock_beta_done"] == 0
    assert row["completed_at"] is None
    assert row["errored_at"] is None

    marker = pathlib.Path(prepared["local_staging_dir"]) / "run1" / "mock_alpha.marker"
    assert marker.exists()

    gate_lines = _events_named(captured, "gate.finished")
    assert len(gate_lines) == 1
    line = gate_lines[0]
    assert line["gate.id"] == "mock_alpha"
    assert line["gate.outcome"] == "success"
    assert isinstance(line["gate.duration_ms"], int)
    assert line["gate.metadata"]["marker"] == str(marker)

    assert (
        notifier.calls.count(("post_milestone", "run1", f"{MOCK_ALPHA.name} completed"))
        == 1
    )


def test_drive_until_blocked_completes_a_run_in_one_call(prepared):
    """One ``process_run`` drives a fresh run to completion (⚑2 drive-until-blocked).

    No ``should_stop``: a single call returns ``"completed"``, both columns are
    1, ``completed_at`` is stamped, exactly one ``run.finished`` with
    ``outcome == "completed"`` and no unsatisfied gates, and
    ``finish_run_thread`` is called.
    """
    notifier = RecordingNotifier()
    _detect(prepared, "run1")

    with structlog.testing.capture_logs() as captured:
        token = process_run(
            prepared, "run1", registry=REFERENCE_PROFILES, notifier=notifier
        )

    assert token == "completed"
    row = _run_row(prepared, "run1")
    assert row["mock_alpha_done"] == 1
    assert row["mock_beta_done"] == 1
    assert row["completed_at"] is not None

    run_lines = _events_named(captured, "run.finished")
    assert len(run_lines) == 1
    assert run_lines[0]["outcome"] == "completed"
    assert run_lines[0]["gates.unsatisfied"] == []
    assert ("finish_run_thread", "run1", "completed", None) in notifier.calls


def test_already_complete_is_silent(prepared):
    """A second call on a completed run is a silent no-op (B9 transition-only).

    Returns ``"already-complete"``, emits zero events, and leaves the row
    byte-identical.
    """
    _detect(prepared, "run1")
    process_run(prepared, "run1", registry=REFERENCE_PROFILES)
    before = dict(_run_row(prepared, "run1"))

    with structlog.testing.capture_logs() as captured:
        token = process_run(prepared, "run1", registry=REFERENCE_PROFILES)

    assert token == "already-complete"
    assert captured == []
    assert dict(_run_row(prepared, "run1")) == before


def test_failure_quarantines_and_emits_both_lines_from_committed_state(prepared):
    """A FAILURE quarantines and emits gate-then-run lines from committed state.

    Beta scripted ``"failure"`` with alpha pre-satisfied: ``error_reason`` is
    the gate's reason, ``errored_at`` is set, ``mock_beta_done`` stays 0; the
    events are ``gate.finished`` (failure) then ``run.finished`` with
    ``outcome == "quarantined"``, ``severity == "critical"``, ``failed.gate``,
    and the committed error fields — provable only if built after the persist.
    """
    notifier = RecordingNotifier()
    _detect(prepared, "run1")
    _set_columns(prepared, "run1", mock_alpha_done=1)
    _write_beta_instruction(prepared, "run1", "failure")

    with structlog.testing.capture_logs() as captured:
        token = process_run(
            prepared, "run1", registry=REFERENCE_PROFILES, notifier=notifier
        )

    assert token == "quarantined"
    row = _run_row(prepared, "run1")
    assert row["errored_at"] is not None
    assert (
        row["error_reason"] == "scripted failure ('failure' in mock_beta.instruction)"
    )
    assert row["mock_beta_done"] == 0

    ordered = [
        e["event"] for e in captured if e["event"] in ("gate.finished", "run.finished")
    ]
    assert ordered == ["gate.finished", "run.finished"]
    gate_line = _events_named(captured, "gate.finished")[0]
    assert gate_line["gate.outcome"] == "failure"
    run_line = _events_named(captured, "run.finished")[0]
    assert run_line["outcome"] == "quarantined"
    assert run_line["severity"] == "critical"
    assert run_line["failed.gate"] == "mock_beta"
    assert run_line["run.errored_at"] == row["errored_at"]
    assert run_line["error.reason"] == row["error_reason"]

    assert notifier.calls[-1][:3] == ("finish_run_thread", "run1", "quarantined")


def test_skipped_in_contract_quarantines_and_never_sets_the_column(prepared):
    """An in-contract SKIPPED quarantines and never sets the column (ADR-0004 d.4).

    Beta scripted ``"skipped"`` → quarantined with an ``error_reason`` naming
    SKIPPED, contract, and the gate id; ``mock_beta_done`` stays 0; the
    ``gate.finished`` line carries ``gate.outcome == "skipped"``.
    """
    _detect(prepared, "run1")
    _set_columns(prepared, "run1", mock_alpha_done=1)
    _write_beta_instruction(prepared, "run1", "skipped")

    with structlog.testing.capture_logs() as captured:
        token = process_run(prepared, "run1", registry=REFERENCE_PROFILES)

    assert token == "quarantined"
    row = _run_row(prepared, "run1")
    assert row["mock_beta_done"] == 0
    assert "SKIPPED" in row["error_reason"]
    assert "contract" in row["error_reason"]
    assert "mock_beta" in row["error_reason"]
    assert _events_named(captured, "gate.finished")[0]["gate.outcome"] == "skipped"


def test_held_result_writes_nothing_and_calls_terminate(prepared):
    """A HELD result writes nothing and calls terminate; no ``run.finished``.

    Beta scripted ``"held"``: the row is unchanged (no column, error, or
    completion), the terminated trace exists, the marker is absent, the
    ``gate.finished`` outcome is ``held``, ``process_run`` returns ``"held"``,
    and no ``run.finished`` is emitted (held is not a terminal look).
    """
    _detect(prepared, "run1")
    _set_columns(prepared, "run1", mock_alpha_done=1)
    _write_beta_instruction(prepared, "run1", "held")
    before = dict(_run_row(prepared, "run1"))

    with structlog.testing.capture_logs() as captured:
        token = process_run(prepared, "run1", registry=REFERENCE_PROFILES)

    assert token == "held"
    assert dict(_run_row(prepared, "run1")) == before
    run_dir = pathlib.Path(prepared["local_staging_dir"]) / "run1"
    assert (run_dir / "mock_beta.terminated").exists()
    assert not (run_dir / "mock_beta.marker").exists()
    assert _events_named(captured, "gate.finished")[0]["gate.outcome"] == "held"
    assert _events_named(captured, "run.finished") == []


def test_escaped_gate_exception_becomes_failure_quarantine(prepared):
    """A gate ``run`` that raises becomes a FAILURE quarantine, never a loop crash.

    A probe gate whose ``run`` raises ``RuntimeError('boom')`` quarantines the
    run with an ``error_reason`` naming ``RuntimeError`` and the gate id; no
    exception escapes ``process_run``.
    """

    def _boom(context):
        raise RuntimeError("boom")

    probe_gate = Gate(
        id="boom_gate",
        name="Boom probe",
        satisfied_column="mock_alpha_done",
        order=1,
        needs=(),
        produces=(),
        run=_boom,
        invalidate=lambda context: None,
    )
    probe_profile = build_profile("probe-v1", [(probe_gate, Applicability.REQUIRED)])
    probe_registry = {"probe-v1": probe_profile}

    with write_txn(prepared["state_file"]) as connection:
        insert_run(connection, "probe_run", pipeline_version="probe-v1")

    token = process_run(prepared, "probe_run", registry=probe_registry)
    assert token == "quarantined"
    row = _run_row(prepared, "probe_run")
    assert "RuntimeError" in row["error_reason"]
    assert "boom_gate" in row["error_reason"]


def test_checkpoint_honors_a_hold_landed_after_selection(prepared):
    """A hold landed after selection makes the run ineligible before any gate runs.

    Alpha satisfied, then the run held: ``process_run`` returns ``"ineligible"``
    and beta never runs — the fresh re-fetch, not the caller's snapshot, governs.
    """
    _detect(prepared, "run1")
    _set_columns(prepared, "run1", mock_alpha_done=1)
    control.hold(prepared["state_file"], "will", "run1")

    with structlog.testing.capture_logs() as captured:
        token = process_run(prepared, "run1", registry=REFERENCE_PROFILES)

    assert token == "ineligible"
    assert _run_row(prepared, "run1")["mock_beta_done"] == 0
    assert not (
        pathlib.Path(prepared["local_staging_dir"]) / "run1" / "mock_beta.marker"
    ).exists()
    assert captured == []


def test_checkpoint_honors_a_hold_landed_mid_drive(prepared):
    """A hold written mid-drive stops the drive at the next gate boundary.

    Gate 1's ``run`` stands in for a concurrent operator: it holds the run then
    returns SUCCESS. ``process_run`` persists gate 1, then the next step's
    re-fetch sees ``held`` and stops — gate 2 never runs, and the token is
    ``"ineligible"``.
    """

    def _hold_then_succeed(context):
        control.hold(context.db_path, "will", context.workdir.name)
        return GateResult(GateOutcome.SUCCESS)

    gate_one = Gate(
        id="probe_one",
        name="Probe one (holds mid-drive)",
        satisfied_column="mock_alpha_done",
        order=1,
        needs=(),
        produces=(),
        run=_hold_then_succeed,
        invalidate=lambda context: None,
    )
    gate_two = Gate(
        id="probe_two",
        name="Probe two",
        satisfied_column="mock_beta_done",
        order=2,
        needs=("probe_one",),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS),
        invalidate=lambda context: None,
    )
    probe_profile = build_profile(
        "probe-hold-v1",
        [(gate_one, Applicability.REQUIRED), (gate_two, Applicability.REQUIRED)],
    )
    probe_registry = {"probe-hold-v1": probe_profile}

    with write_txn(prepared["state_file"]) as connection:
        insert_run(connection, "held_run", pipeline_version="probe-hold-v1")

    token = process_run(prepared, "held_run", registry=probe_registry)

    assert token == "ineligible"
    row = _run_row(prepared, "held_run")
    assert row["mock_alpha_done"] == 1
    assert row["mock_beta_done"] == 0
    assert row["lifecycle"] == "held"


def test_missing_row_is_loud_not_fatal(prepared):
    """``process_run`` on a never-inserted name is loud, not fatal.

    Returns ``"missing"`` with one error-level ``run.vanished`` line and no
    exception.
    """
    with structlog.testing.capture_logs() as captured:
        token = process_run(prepared, "ghost", registry=REFERENCE_PROFILES)

    assert token == "missing"
    vanished = _events_named(captured, "run.vanished")
    assert len(vanished) == 1
    assert vanished[0]["log_level"] == "error"


@pytest.mark.parametrize(
    "kind",
    ["ghost_version", "corrupt_exclusions", "missing_staging_key"],
)
def test_resolution_failures_quarantine_the_one_run(prepared, kind):
    """Each §3.5 resolution anomaly quarantines exactly the one run, no exception.

    (a) a ghost ``pipeline_version``; (b) corrupt ``excluded_gates`` JSON; (c) a
    config lacking ``local_staging_dir`` (the ``resolve_context`` ``KeyError``
    path). Each quarantines with a naming ``error_reason`` and a
    ``run.finished`` quarantined line without ``failed.gate``.
    """
    _detect(prepared, "run1")
    run_config = prepared

    if kind == "ghost_version":
        _set_columns(prepared, "run1", pipeline_version="ghost-v9")
    elif kind == "corrupt_exclusions":
        with write_txn(prepared["state_file"]) as connection:
            connection.execute(
                "UPDATE runs SET excluded_gates = ? WHERE name = ?",
                ('{"not": "a list"}', "run1"),
            )
    else:  # missing_staging_key
        run_config = dict(prepared)
        del run_config["local_staging_dir"]

    with structlog.testing.capture_logs() as captured:
        token = process_run(run_config, "run1", registry=REFERENCE_PROFILES)

    assert token == "quarantined"
    row = _run_row(prepared, "run1")
    assert row["errored_at"] is not None
    assert row["error_reason"]
    run_line = _events_named(captured, "run.finished")[0]
    assert run_line["outcome"] == "quarantined"
    assert "failed.gate" not in run_line


def test_exclusion_overlay_is_honored_at_resolution(prepared):
    """The daemon resolves against the exclusion overlay, not the bare profile.

    Alpha satisfied, then a scripted beta failure quarantines the run; after
    ``exclude_gate('mock_beta')`` + ``clear_error``, ``process_run`` returns
    ``"completed"`` with ``mock_beta_done == 0`` — beta is out of the contract.
    """
    _detect(prepared, "run1")
    _set_columns(prepared, "run1", mock_alpha_done=1)
    _write_beta_instruction(prepared, "run1", "failure")
    assert process_run(prepared, "run1", registry=REFERENCE_PROFILES) == "quarantined"

    control.exclude_gate(
        prepared["state_file"], "will", "run1", "mock_beta", registry=REFERENCE_PROFILES
    )
    control.clear_error(prepared["state_file"], "will", "run1")

    token = process_run(prepared, "run1", registry=REFERENCE_PROFILES)
    assert token == "completed"
    assert _run_row(prepared, "run1")["mock_beta_done"] == 0


def test_gate_run_executes_outside_any_transaction(prepared):
    """A gate that opens its own ``write_txn`` succeeds — no txn is held across it.

    The short-transactions invariant: a probe gate whose ``run`` opens
    ``write_txn(ctx.db_path)`` and writes a scratch metadata key succeeds
    promptly, impossible if the daemon held a write transaction across
    ``gate.run``.
    """

    def _writes_own_txn(context):
        with write_txn(context.db_path) as connection:
            set_metadata(connection, "scratch", "written")
        return GateResult(GateOutcome.SUCCESS)

    probe_gate = Gate(
        id="txn_probe",
        name="Txn probe",
        satisfied_column="mock_alpha_done",
        order=1,
        needs=(),
        produces=(),
        run=_writes_own_txn,
        invalidate=lambda context: None,
    )
    probe_profile = build_profile(
        "probe-txn-v1", [(probe_gate, Applicability.REQUIRED)]
    )
    probe_registry = {"probe-txn-v1": probe_profile}

    with write_txn(prepared["state_file"]) as connection:
        insert_run(connection, "txn_run", pipeline_version="probe-txn-v1")

    token = process_run(prepared, "txn_run", registry=probe_registry)
    assert token == "completed"

    connection = get_connection(prepared["state_file"])
    try:
        assert get_metadata(connection, "scratch") == "written"
    finally:
        connection.close()
    assert _run_row(prepared, "txn_run")["mock_alpha_done"] == 1


def test_progress_writes_are_never_audited(prepared):
    """A full drive leaves zero ``control_audit`` rows (plan 07 §6).

    Persists, completion stamps, and emissions are direct writes — never
    audited; only control ops write the trail, and this test issues none.
    """
    _detect(prepared, "run1")
    assert process_run(prepared, "run1", registry=REFERENCE_PROFILES) == "completed"
    assert _audit_rows(prepared) == []


# --------------------------------------------------------------------------
# Unit 4 (ADR-0006) — the persist choreography. Proven with TEST-LOCAL gate
# fixtures that declare ``persists`` onto the already-existing whitelisted
# columns ``experiment_id``/``globus_task_id`` (real since migration 001): no
# migration, no reference-profile change. The shipped mock_gates/reference-v1
# stay untouched (Plan A frozen); the real experiment gate is a later increment.
# --------------------------------------------------------------------------


def _single_gate_registry(gate, version="persist-v1"):
    """Wrap one gate in a one-gate REQUIRED profile keyed by ``version``."""
    return version, {version: build_profile(version, [(gate, Applicability.REQUIRED)])}


def test_success_persists_declared_output_atomically_with_satisfaction(prepared):
    """A declared persists output and its satisfaction column land in one txn (ADR-0006 d.2).

    A test-local gate with ``persists={"exp": "experiment_id"}`` whose ``run``
    returns ``SUCCESS`` with ``metadata={"exp": 4242}``. After ``process_run``
    the committed row carries BOTH ``experiment_id == 4242`` and the gate's
    ``satisfied_column == 1`` — output and checkmark written together, never
    apart. ADR-0002 is intact: the gate only RETURNED metadata; the daemon wrote.
    """
    persist_gate = Gate(
        id="persist_gate",
        name="Persist probe",
        satisfied_column="mock_alpha_done",
        order=1,
        needs=(),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS, metadata={"exp": 4242}),
        invalidate=lambda context: None,
        persists={"exp": "experiment_id"},
    )
    version, registry = _single_gate_registry(persist_gate)
    with write_txn(prepared["state_file"]) as connection:
        insert_run(connection, "persist_run", pipeline_version=version)

    token = process_run(prepared, "persist_run", registry=registry)

    assert token == "completed"
    row = _run_row(prepared, "persist_run")
    assert row["experiment_id"] == 4242
    assert row["mock_alpha_done"] == 1
    assert row["errored_at"] is None


def test_success_missing_declared_persist_key_quarantines(prepared):
    """SUCCESS omitting a declared persists key quarantines with nothing partially written.

    Same gate, but ``run`` returns ``SUCCESS`` with ``metadata=None`` (no
    ``exp`` key). ADR-0006 d.2 fail-loud: ``process_run`` returns
    ``"quarantined"``, the row's ``errored_at`` is set and ``error_reason``
    names the missing key ``"exp"``, ``experiment_id`` stays NULL and the
    satisfaction column stays 0 (the refusal is atomic — output and checkmark
    move together or not at all), and ``run.finished`` carries ``failed.gate``.
    """
    persist_gate = Gate(
        id="persist_gate",
        name="Persist probe",
        satisfied_column="mock_alpha_done",
        order=1,
        needs=(),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS, metadata=None),
        invalidate=lambda context: None,
        persists={"exp": "experiment_id"},
    )
    version, registry = _single_gate_registry(persist_gate)
    with write_txn(prepared["state_file"]) as connection:
        insert_run(connection, "persist_run", pipeline_version=version)

    with structlog.testing.capture_logs() as captured:
        token = process_run(prepared, "persist_run", registry=registry)

    assert token == "quarantined"
    row = _run_row(prepared, "persist_run")
    assert row["errored_at"] is not None
    assert "exp" in row["error_reason"]
    # Atomic refusal: neither the output nor the checkmark was written.
    assert row["experiment_id"] is None
    assert row["mock_alpha_done"] == 0

    gate_line = _events_named(captured, "gate.finished")[0]
    assert gate_line["gate.outcome"] == "failure"
    run_line = _events_named(captured, "run.finished")[0]
    assert run_line["failed.gate"] == "persist_gate"


def test_success_persist_to_unknown_column_fails_loud(prepared):
    """A persists column absent from the runs whitelist raises UnknownColumnError, fatal.

    A gate mapping onto a non-existent column returns ``SUCCESS`` with the key
    present, so it clears the missing-key guard and reaches ``update_run`` —
    whose live whitelist raises ``UnknownColumnError`` (a ``KeyError`` subclass,
    the injection-defense signal). It PROPAGATES out of ``process_run`` (the
    catches are call-site-scoped, never around the persist write); the
    ``write_txn`` rolls back, so neither the satisfaction column nor any partial
    write lands — a gate-author/deployment bug surfaced honestly (ADR-0006 d.2).
    """
    bad_gate = Gate(
        id="bad_gate",
        name="Bad-column probe",
        satisfied_column="mock_alpha_done",
        order=1,
        needs=(),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS, metadata={"x": 1}),
        invalidate=lambda context: None,
        persists={"x": "not_a_runs_column"},
    )
    version, registry = _single_gate_registry(bad_gate)
    with write_txn(prepared["state_file"]) as connection:
        insert_run(connection, "bad_run", pipeline_version=version)

    with pytest.raises(UnknownColumnError):
        process_run(prepared, "bad_run", registry=registry)

    # The txn rolled back: the satisfaction column never landed.
    row = _run_row(prepared, "bad_run")
    assert row["mock_alpha_done"] == 0
    assert row["errored_at"] is None


def test_gate_without_persists_writes_only_satisfaction(prepared):
    """A default-empty-``persists`` gate writes only its checkmark — the untouched path.

    The regression guard for the shipped mock_gates/reference-v1 path (which
    declare no ``persists``): a SUCCESS with the default-empty map writes ONLY
    the satisfaction column — no token column moves. ``experiment_id`` and
    ``globus_task_id`` stay NULL, exactly as before ADR-0006.
    """
    plain_gate = Gate(
        id="plain_gate",
        name="No-persists probe",
        satisfied_column="mock_alpha_done",
        order=1,
        needs=(),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS),
        invalidate=lambda context: None,
    )
    version, registry = _single_gate_registry(plain_gate)
    with write_txn(prepared["state_file"]) as connection:
        insert_run(connection, "plain_run", pipeline_version=version)

    token = process_run(prepared, "plain_run", registry=registry)

    assert token == "completed"
    row = _run_row(prepared, "plain_run")
    assert row["mock_alpha_done"] == 1
    assert row["experiment_id"] is None
    assert row["globus_task_id"] is None


# --------------------------------------------------------------------------
# Unit 5 — the cycle (run_cycle). B5 lives here.
# --------------------------------------------------------------------------


def test_paused_cycle_does_nothing_at_all(prepared):
    """A paused cycle short-circuits detection, the disk check, and every run.

    With the pipeline paused and a fresh watch folder plus a recording disk
    check: the cycle returns ``{"status": "paused"}``, inserts no row, runs no
    gate, and never calls the disk check (pause short-circuits it).
    """
    control.pause(prepared["state_file"], "will")
    _make_folder(prepared, "run1")
    disk_check = RecordingDiskCheck(reason=None)

    result = run_cycle(
        prepared,
        registry=REFERENCE_PROFILES,
        disk_floor_check=disk_check,
    )

    assert result["status"] == "paused"
    assert _run_row(prepared, "run1") is None
    assert disk_check.calls == 0


def test_disk_floor_trips_auto_pause_with_daemon_actor(prepared):
    """Below the floor auto-pauses (actor ``daemon``); operator resume is the only clear.

    A disk check returning a reason trips ``{"status": "disk-paused"}``,
    ``is_paused`` becomes True, exactly one audit row (actor ``daemon``, action
    ``pause``, ``target_run`` NULL, reason in args), one error ``daemon.paused``
    line, and a second cycle returns ``"paused"`` without re-calling the check.
    Then ``resume`` + a healthy check lets the next cycle detect and drive.
    """
    _make_folder(prepared, "run1")
    tripping_check = RecordingDiskCheck(reason="quota 95% consumed")

    with structlog.testing.capture_logs() as captured:
        result = run_cycle(
            prepared, registry=REFERENCE_PROFILES, disk_floor_check=tripping_check
        )

    assert result["status"] == "disk-paused"
    connection = get_connection(prepared["state_file"])
    try:
        assert control.is_paused(connection) is True
    finally:
        connection.close()

    audit = _audit_rows(prepared)
    assert len(audit) == 1
    assert audit[0]["actor"] == "daemon"
    assert audit[0]["action"] == "pause"
    assert audit[0]["target_run"] is None
    assert "quota 95% consumed" in audit[0]["args_json"]

    paused_lines = _events_named(captured, "daemon.paused")
    assert len(paused_lines) == 1
    assert paused_lines[0]["log_level"] == "error"

    # Second cycle: paused short-circuits, the check is not called again.
    second_check = RecordingDiskCheck(reason="still bad")
    second = run_cycle(
        prepared, registry=REFERENCE_PROFILES, disk_floor_check=second_check
    )
    assert second["status"] == "paused"
    assert second_check.calls == 0
    assert len(_audit_rows(prepared)) == 1  # one audit row per trip

    # Operator resume is the only clear; then the daemon runs again.
    control.resume(prepared["state_file"], "will")
    healthy_check = RecordingDiskCheck(reason=None)
    third = run_cycle(
        prepared, registry=REFERENCE_PROFILES, disk_floor_check=healthy_check
    )
    assert third["status"] == "ok"
    assert _run_row(prepared, "run1") is not None
    assert _run_row(prepared, "run1")["completed_at"] is not None


def test_cycle_detects_and_drives_in_one_look(prepared):
    """One cycle detects a fresh folder and drives it to completion.

    Detection precedes selection, and drive-until-blocked completes the run in
    the same look.
    """
    _make_folder(prepared, "run1")
    result = run_cycle(prepared, registry=REFERENCE_PROFILES)

    assert result["detected"] == ["run1"]
    assert result["runs"]["run1"] == "completed"
    assert _run_row(prepared, "run1")["completed_at"] is not None


def test_should_stop_finishes_current_gate_then_stops(prepared):
    """``should_stop`` finishes the in-flight gate, then stops before the next run.

    Two fresh runs and a ``should_stop`` that trips once run_a's first gate has
    persisted: run_a has exactly one column set (never killed mid-gate), the
    cycle status is ``"stopped"``, and run_b is untouched.
    """
    _make_folder(prepared, "run_a")
    _make_folder(prepared, "run_b")

    def should_stop():
        return bool(
            _run_row(prepared, "run_a")
            and _run_row(prepared, "run_a")["mock_alpha_done"]
        )

    result = run_cycle(prepared, registry=REFERENCE_PROFILES, should_stop=should_stop)

    assert result["status"] == "stopped"
    row_a = _run_row(prepared, "run_a")
    assert row_a["mock_alpha_done"] == 1
    assert row_a["mock_beta_done"] == 0
    row_b = _run_row(prepared, "run_b")
    assert row_b["mock_alpha_done"] == 0
    assert row_b["mock_beta_done"] == 0


def test_failing_disk_check_fails_open(prepared):
    """A disk check that raises is logged and treated as healthy (D1 fail-open).

    One error line, and the cycle proceeds to detect and drive.
    """
    _make_folder(prepared, "run1")

    def raising_check(config):
        raise RuntimeError("df blew up")

    with structlog.testing.capture_logs() as captured:
        result = run_cycle(
            prepared, registry=REFERENCE_PROFILES, disk_floor_check=raising_check
        )

    assert result["status"] == "ok"
    assert _run_row(prepared, "run1")["completed_at"] is not None
    error_lines = [e for e in captured if e.get("log_level") == "error"]
    assert any(e["event"] == "disk_floor_check.failed" for e in error_lines)


# --------------------------------------------------------------------------
# Unit 6 — run_forever + signals.
# --------------------------------------------------------------------------


def test_run_forever_refuses_a_second_instance(config):
    """``run_forever`` refuses a second instance before touching the DB.

    With the lock held manually, ``run_forever(max_cycles=1)`` raises
    ``DaemonLockError`` before any migration — no rows are created.
    """
    lock_handle = acquire_daemon_lock(str(config["state_file"]) + ".lock")
    try:
        with pytest.raises(DaemonLockError):
            run_forever(
                config,
                registry=REFERENCE_PROFILES,
                default_pipeline_version=DEFAULT_VERSION,
                max_cycles=1,
            )
    finally:
        lock_handle.close()

    # No DB was created/migrated by the refused start: the lock acquisition
    # precedes prepare_database, so the state file must not exist at all.
    assert not pathlib.Path(config["state_file"]).exists()


def test_run_forever_single_cycle_end_to_end(config):
    """``run_forever(max_cycles=1)`` runs one cycle, releases the lock, and logs.

    One watch folder completes; the lock is re-acquirable afterward;
    ``daemon.started`` and ``daemon.stopped`` are both emitted; and a missing
    ``poll_interval_minutes`` raises ``KeyError`` before the lock is taken.
    """
    _make_folder(config, "run1")

    with structlog.testing.capture_logs() as captured:
        run_forever(
            config,
            registry=REFERENCE_PROFILES,
            default_pipeline_version=DEFAULT_VERSION,
            max_cycles=1,
        )

    assert _run_row(config, "run1")["completed_at"] is not None
    assert _events_named(captured, "daemon.started")
    assert _events_named(captured, "daemon.stopped")

    # Lock released: re-acquirable.
    handle = acquire_daemon_lock(str(config["state_file"]) + ".lock")
    handle.close()

    # Missing poll_interval_minutes fails loud before the lock.
    no_poll = dict(config)
    del no_poll["poll_interval_minutes"]
    with pytest.raises(KeyError):
        run_forever(
            no_poll,
            registry=REFERENCE_PROFILES,
            default_pipeline_version=DEFAULT_VERSION,
            max_cycles=1,
        )


def test_sigterm_sets_only_the_shutdown_flag(prepared):
    """SIGTERM sets the shutdown flag and emits; no DB or file state changes.

    Installing the handlers and raising SIGTERM sets the flag and emits
    ``daemon.shutdown_requested``; the process stays alive and every run row is
    byte-identical — decision A intact: signals carry zero control semantics.
    """
    import threading

    _detect(prepared, "run1")
    row_before = dict(_run_row(prepared, "run1"))

    shutdown_event = threading.Event()
    install_signal_handlers(shutdown_event)

    with structlog.testing.capture_logs() as captured:
        signal.raise_signal(signal.SIGTERM)

    assert shutdown_event.is_set()
    assert _events_named(captured, "daemon.shutdown_requested")
    # No DB/file state changed: the handler touched nothing but the flag.
    assert dict(_run_row(prepared, "run1")) == row_before
    assert _audit_rows(prepared) == []
    assert not (pathlib.Path(prepared["local_staging_dir"]) / "run1").exists()


# --------------------------------------------------------------------------
# Unit 7 — configure_logging.
# --------------------------------------------------------------------------


def test_configure_logging_routes_dual_and_per_run(config):
    """Events go to the main sink; those with a safe ``run.name`` also route per-run.

    An event with ``run.name`` lands in the events file and in
    ``run_events_dir/<name>.jsonl``; an event without ``run.name`` lands in the
    events file only, with no per-run file created for it.
    """
    configure_logging(config)
    logger = structlog.get_logger("sluice.daemon")
    logger.info("run.detected", **{"run.name": "run1"})
    logger.info("daemon.started")

    events_path = pathlib.Path(config["events_file"])
    lines = [json.loads(line) for line in events_path.read_text().splitlines() if line]
    events = [line["event"] for line in lines]
    assert "run.detected" in events
    assert "daemon.started" in events

    per_run_path = pathlib.Path(config["run_events_dir"]) / "run1.jsonl"
    per_run_lines = [
        json.loads(line) for line in per_run_path.read_text().splitlines() if line
    ]
    assert [line["event"] for line in per_run_lines] == ["run.detected"]

    # No per-run file for the nameless event.
    assert not (
        pathlib.Path(config["run_events_dir"]) / "daemon.started.jsonl"
    ).exists()


def test_configure_logging_is_idempotent(config):
    """Configuring twice then emitting once writes exactly one events line.

    Repeat calls replace, never stack, handlers.
    """
    configure_logging(config)
    configure_logging(config)
    structlog.get_logger("sluice.daemon").info("daemon.started")

    events_path = pathlib.Path(config["events_file"])
    lines = [line for line in events_path.read_text().splitlines() if line]
    assert len(lines) == 1


def test_unsafe_run_name_never_becomes_a_path(config):
    """An unsafe ``run.name`` never becomes a per-run path; one error line is logged.

    An event with ``run.name="../evil"`` creates no file outside
    ``run_events_dir``, stays in the main sink, and produces one error line.
    """
    configure_logging(config)
    structlog.get_logger("sluice.daemon").info(
        "run.detected", **{"run.name": "../evil"}
    )

    run_events_dir = pathlib.Path(config["run_events_dir"])
    escaped = run_events_dir.parent / "evil.jsonl"
    assert not escaped.exists()
    assert not (run_events_dir / "../evil.jsonl").exists()

    events_path = pathlib.Path(config["events_file"])
    lines = [json.loads(line) for line in events_path.read_text().splitlines() if line]
    events = [line["event"] for line in lines]
    assert "run.detected" in events
    assert events.count("run_events.unsafe_name") == 1


# --------------------------------------------------------------------------
# Unit 8 — Plan A capstones: state-only control proven through the real loop.
# --------------------------------------------------------------------------


def test_lifecycle_journey_reference_profile(prepared):
    """Detect → complete → idle: one canonical line ever, then silent cycles.

    Cycle 1 completes run1 (drive-until-blocked) with exactly one
    ``run.finished``; later cycles report ``"already-complete"`` with zero new
    events; ``control_audit`` stays empty (only progress happened).
    """
    _make_folder(prepared, "run1")

    with structlog.testing.capture_logs() as first_cycle:
        first = run_cycle(prepared, registry=REFERENCE_PROFILES)
    assert first["runs"]["run1"] == "completed"
    assert len(_events_named(first_cycle, "run.finished")) == 1

    with structlog.testing.capture_logs() as later_cycle:
        second = run_cycle(prepared, registry=REFERENCE_PROFILES)
    assert second["runs"]["run1"] == "already-complete"
    assert _events_named(later_cycle, "run.finished") == []
    assert _audit_rows(prepared) == []


def test_disabled_variant_completes_without_alpha(config):
    """A ``reference-v1-alpha-disabled`` run completes with alpha never run.

    Plan 05 §7's owed proof: needs-unblock (alpha's column stays 0 yet beta
    runs) AND completion-exclusion, both through the real loop.
    """
    prepare_database(
        config, registry=REFERENCE_PROFILES, default_pipeline_version=DISABLED_VERSION
    )
    _make_folder(config, "run1")

    result = run_cycle(config, registry=REFERENCE_PROFILES)
    assert result["runs"]["run1"] == "completed"
    row = _run_row(config, "run1")
    assert row["pipeline_version"] == DISABLED_VERSION
    assert row["mock_alpha_done"] == 0
    assert row["mock_beta_done"] == 1
    assert row["completed_at"] is not None


def test_quarantine_clear_error_retry_completes(prepared):
    """Quarantine → fix → clear_error → complete emits two canonical lines over a life.

    A scripted beta failure quarantines (later cycles run nothing for it); after
    fixing the instruction and ``clear_error``, a later cycle completes it —
    exactly TWO ``run.finished`` lines across its life (quarantined then
    completed).
    """
    _make_folder(prepared, "run1")
    _write_beta_instruction(prepared, "run1", "failure")

    with structlog.testing.capture_logs() as cycle_one:
        run_cycle(prepared, registry=REFERENCE_PROFILES)
    quarantine_lines = _events_named(cycle_one, "run.finished")
    assert len(quarantine_lines) == 1
    assert quarantine_lines[0]["outcome"] == "quarantined"

    # Quarantined: it drops out of selection, so a later cycle never touches it.
    with structlog.testing.capture_logs() as cycle_two:
        result_two = run_cycle(prepared, registry=REFERENCE_PROFILES)
    assert "run1" not in result_two["runs"]
    assert _events_named(cycle_two, "run.finished") == []

    # Fix + clear, then complete.
    _write_beta_instruction(prepared, "run1", "success")
    control.clear_error(prepared["state_file"], "will", "run1")
    with structlog.testing.capture_logs() as cycle_three:
        result_three = run_cycle(prepared, registry=REFERENCE_PROFILES)
    assert result_three["runs"]["run1"] == "completed"
    completed_lines = _events_named(cycle_three, "run.finished")
    assert len(completed_lines) == 1
    assert completed_lines[0]["outcome"] == "completed"


def test_hold_unhold_resumes_where_it_left_off(prepared):
    """Hold pauses a run's progress; unhold resumes it — state-only, no signal.

    After alpha: ``hold`` makes cycles do nothing for it; ``unhold`` lets beta
    run and the run complete. Control end to end via columns alone.
    """
    # A first cycle would complete the run outright (drive-until-blocked); to
    # hold after alpha, satisfy alpha directly then hold before any drive.
    _make_folder(prepared, "run1")
    detect_new_runs(prepared)
    _set_columns(prepared, "run1", mock_alpha_done=1)
    control.hold(prepared["state_file"], "will", "run1")

    held_result = run_cycle(prepared, registry=REFERENCE_PROFILES)
    assert "run1" not in held_result["runs"]
    assert _run_row(prepared, "run1")["mock_beta_done"] == 0

    control.unhold(prepared["state_file"], "will", "run1")
    resumed = run_cycle(prepared, registry=REFERENCE_PROFILES)
    assert resumed["runs"]["run1"] == "completed"
    assert _run_row(prepared, "run1")["mock_beta_done"] == 1


def test_exclusion_remediates_a_stuck_run(prepared):
    """Exclusion remediates a stuck run through the loop (ADR-0004 d.3).

    Beta quarantines; ``exclude_gate('mock_beta')`` + ``clear_error`` lets the
    next cycle complete past the missing step with ``mock_beta_done == 0``.
    """
    _make_folder(prepared, "run1")
    _write_beta_instruction(prepared, "run1", "failure")
    run_cycle(prepared, registry=REFERENCE_PROFILES)
    assert _run_row(prepared, "run1")["errored_at"] is not None

    control.exclude_gate(
        prepared["state_file"], "will", "run1", "mock_beta", registry=REFERENCE_PROFILES
    )
    control.clear_error(prepared["state_file"], "will", "run1")

    result = run_cycle(prepared, registry=REFERENCE_PROFILES)
    assert result["runs"]["run1"] == "completed"
    assert _run_row(prepared, "run1")["mock_beta_done"] == 0


# --------------------------------------------------------------------------
# Plan 11 — the detection-source seam (injected lister + failure containment).
# --------------------------------------------------------------------------


def _fixed_lister(candidates):
    """A lister returning a fixed candidate list, ignoring config."""

    def _lister(config):
        return list(candidates)

    return _lister


def test_detect_new_runs_uses_injected_lister_and_stamps_size_tb(prepared):
    """An injected lister supplies ``(name, size_tb)`` and needs no ``watch_dir``.

    A config copy WITHOUT ``watch_dir`` plus a lister returning a duplicate pair
    inserts one row per distinct name, stamps ``size_tb`` from the lister and the
    active-default ``pipeline_version`` from metadata (read back, not a literal),
    and emits exactly one ``run.detected`` per name (the duplicate is silenced by
    ``insert_run``'s idempotent bool).
    """
    no_watch = dict(prepared)
    del no_watch["watch_dir"]
    lister = _fixed_lister([("runB", 1.25), ("runA", None), ("runB", 1.25)])

    with structlog.testing.capture_logs() as captured:
        detected = detect_new_runs(no_watch, lister=lister)

    assert detected == ["runA", "runB"]
    stamp = None
    connection = get_connection(no_watch["state_file"])
    try:
        stamp = get_metadata(connection, control.PIPELINE_PROFILE_KEY)
    finally:
        connection.close()
    assert _run_row(no_watch, "runB")["size_tb"] == 1.25
    assert _run_row(no_watch, "runA")["size_tb"] is None
    assert _run_row(no_watch, "runA")["pipeline_version"] == stamp
    assert _run_row(no_watch, "runB")["pipeline_version"] == stamp

    detected_lines = _events_named(captured, "run.detected")
    assert sorted(line["run.name"] for line in detected_lines) == ["runA", "runB"]


def test_detect_new_runs_rejects_unsafe_candidate_names(prepared):
    """An unsafe candidate name is dropped with one error event; no row is minted.

    A lister emitting ``../evil`` never mints a run named ``..`` — it is skipped
    with one ``detection.unsafe_name`` event carrying the rejected name, and only
    the safe candidate lands.
    """
    lister = _fixed_lister([("../evil", None), ("ok_run", None)])

    with structlog.testing.capture_logs() as captured:
        detected = detect_new_runs(prepared, lister=lister)

    assert detected == ["ok_run"]
    unsafe = _events_named(captured, "detection.unsafe_name")
    assert len(unsafe) == 1
    assert unsafe[0]["detection.rejected_name"] == "../evil"

    connection = get_connection(prepared["state_file"])
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        connection.close()


def test_detect_new_runs_calls_lister_outside_any_write_transaction(prepared):
    """The lister runs before the insert txn opens — it may itself write the DB.

    A lister that opens ``write_txn`` on the same DB and writes a probe key
    succeeds and that key reads back; a ``detect_new_runs`` holding its write
    transaction across the lister call would deadlock here (the
    short-transaction invariant, made executable).
    """

    def probing_lister(config):
        with write_txn(config["state_file"]) as connection:
            set_metadata(connection, "probe", "x")
        return []

    detected = detect_new_runs(prepared, lister=probing_lister)

    assert detected == []
    connection = get_connection(prepared["state_file"])
    try:
        assert get_metadata(connection, "probe") == "x"
    finally:
        connection.close()


def test_detection_failure_degrades_to_zero_candidates_and_one_event(prepared):
    """A raising lister degrades to zero candidates plus one ``detection.failed``.

    The D4 fail-open posture: an unreachable remote returns ``[]`` with exactly
    one error event naming the exception type, and inserts no rows.
    """

    def raising_lister(config):
        raise RuntimeError("connection closed by remote")

    with structlog.testing.capture_logs() as captured:
        detected = detect_new_runs(prepared, lister=raising_lister)

    assert detected == []
    failed = _events_named(captured, "detection.failed")
    assert len(failed) == 1
    assert failed[0]["error.type"] == "RuntimeError"

    connection = get_connection(prepared["state_file"])
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        connection.close()


def test_detection_failure_does_not_stop_the_cycle(prepared):
    """A detection failure never kills the cycle — pre-existing runs still drive.

    Seed a run via the watch-dir path, then run a cycle with a raising lister:
    ``detected`` is empty but the pre-existing run still appears in ``runs``.
    """
    _detect(prepared, "existing")

    def raising_lister(config):
        raise RuntimeError("df blew up on the remote")

    result = run_cycle(prepared, registry=REFERENCE_PROFILES, lister=raising_lister)

    assert result["detected"] == []
    assert "existing" in result["runs"]


def test_detection_config_keyerror_still_fails_loud(prepared):
    """A lister ``KeyError`` (missing config key) propagates — fail loud, not open."""

    def keyerror_lister(config):
        raise KeyError("password_file")

    with pytest.raises(KeyError, match="password_file"):
        detect_new_runs(prepared, lister=keyerror_lister)


def test_run_cycle_passes_lister_through(prepared):
    """``run_cycle`` hands its ``lister`` to ``detect_new_runs`` unchanged."""
    lister = _fixed_lister([("cycle_run", 3.0)])
    result = run_cycle(prepared, registry=REFERENCE_PROFILES, lister=lister)

    assert result["detected"] == ["cycle_run"]
    assert _run_row(prepared, "cycle_run") is not None
    assert _run_row(prepared, "cycle_run")["size_tb"] == 3.0


def test_run_forever_passes_lister_through(config):
    """``run_forever(max_cycles=1, lister=...)`` detects the injected candidate."""
    lister = _fixed_lister([("forever_run", 2.5)])
    run_forever(
        config,
        registry=REFERENCE_PROFILES,
        default_pipeline_version=DEFAULT_VERSION,
        max_cycles=1,
        lister=lister,
    )

    row = _run_row(config, "forever_run")
    assert row is not None
    assert row["size_tb"] == 2.5
    assert row["pipeline_version"] == DEFAULT_VERSION


def test_production_stamp_requires_registry_entry_at_startup(config):
    """``production-v3.1`` cannot be seeded until a registry defines it (the interlock).

    Green against the built ``prepare_database``: pointing the default at
    ``production-v3.1`` with the reference registry dies as ONE loud startup
    ``UnknownProfileError`` — nothing is seeded and no run row exists — so the
    composition root cannot wire production detection wrongly until a later
    increment adds the profile entry (gate-map caveat 2).
    """
    from sluice.core.profile import UnknownProfileError

    with pytest.raises(UnknownProfileError):
        prepare_database(
            config,
            registry=REFERENCE_PROFILES,
            default_pipeline_version="production-v3.1",
        )

    connection = get_connection(config["state_file"])
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert get_metadata(connection, control.PIPELINE_PROFILE_KEY) is None
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# The A5 seam, completed (plan 15 increment 5): the daemon persists the thread
# id its notifier returns, so replies find their thread across restarts.
# ---------------------------------------------------------------------------


def test_detection_persists_the_thread_id_the_notifier_returns(tmp_path):
    """``open_run_thread``'s return value lands in ``runs.slack_thread_ts``.

    The seam ``safe_notify``'s docstring always described — the thread id "rides
    back to the daemon" — but which nothing acted on: the column existed from
    migration 001 and no code ever wrote it, so every notification would have
    posted un-threaded.

    The daemon does the write, not the notifier, which keeps it the single writer
    of run state.
    """
    import sqlite3

    from sluice.core.daemon import detect_new_runs, prepare_database
    from sluice.core.profile import REFERENCE_PROFILES

    config = {
        "state_file": str(tmp_path / "state.db"),
        "watch_dir": str(tmp_path / "watch"),
    }
    prepare_database(
        config, registry=REFERENCE_PROFILES, default_pipeline_version="reference-v1"
    )

    class ThreadOpeningNotifier:
        def open_run_thread(self, run_name, experiment_id=None, size_tb=None):
            return "1722600000.000100"

    detected = detect_new_runs(
        config,
        lister=lambda _config: [("SEQ_A", 1.0)],
        notifier=ThreadOpeningNotifier(),
    )

    assert detected == ["SEQ_A"]
    connection = sqlite3.connect(config["state_file"])
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT slack_thread_ts FROM runs WHERE name = 'SEQ_A'"
        ).fetchone()
    finally:
        connection.close()
    assert row["slack_thread_ts"] == "1722600000.000100"


def test_detection_survives_a_notifier_that_returns_no_thread_id(tmp_path):
    """A notifier returning ``None`` leaves the column NULL and detection succeeds.

    The unconfigured case (``safe_notify(None, ...)`` returns ``None``) and the
    already-threaded case both land here. Neither is an error, and neither may
    roll back a detection that already committed.
    """
    import sqlite3

    from sluice.core.daemon import detect_new_runs, prepare_database
    from sluice.core.profile import REFERENCE_PROFILES

    config = {
        "state_file": str(tmp_path / "state.db"),
        "watch_dir": str(tmp_path / "watch"),
    }
    prepare_database(
        config, registry=REFERENCE_PROFILES, default_pipeline_version="reference-v1"
    )

    detected = detect_new_runs(
        config, lister=lambda _config: [("SEQ_B", 1.0)], notifier=None
    )

    assert detected == ["SEQ_B"]
    connection = sqlite3.connect(config["state_file"])
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT slack_thread_ts FROM runs WHERE name = 'SEQ_B'"
        ).fetchone()
    finally:
        connection.close()
    assert row["slack_thread_ts"] is None


def test_an_empty_watch_dir_is_refused_rather_than_scanning_the_cwd():
    """``watch_dir=""`` raises instead of sweeping the current directory.

    ``pathlib.Path("")`` is the CURRENT DIRECTORY, so an unset watch_dir would
    mint a run for every subdirectory of wherever the daemon was started. This is
    not hypothetical: the first end-to-end dry run had watch_dir unset and
    detected ``.git``, ``.claude``, ``docs``, ``sluice`` and ``tests`` as runs,
    then quarantined all five.

    Guardrail §8 — no silent fallback. An empty value is a configuration mistake,
    not an instruction to scan everything in reach.
    """
    from sluice.core.daemon import watch_dir_lister

    with pytest.raises(ValueError, match="refusing to scan the current directory"):
        watch_dir_lister({"watch_dir": "  "})


# ---------------------------------------------------------------------------
# The cross-host guard (shared-operations increment).
# ---------------------------------------------------------------------------


def test_the_recorded_host_lives_beside_the_db_not_inside_it(config):
    """The daemon's host is written to a sibling file, never to poller_metadata.

    The guard's whole job is to stop a second machine opening this database, so
    reading it must not require opening the database. ``poller_metadata`` was the
    obvious home and is the wrong one: a metadata read on the wrong host creates
    the WAL ``-shm`` segment, which is the exact act being prevented.
    """
    state_file = pathlib.Path(config["state_file"])

    record_daemon_host(str(state_file), "submit1")

    assert daemon_host_path(str(state_file)) == state_file.with_name(
        state_file.name + ".host"
    )
    assert daemon_host_path(str(state_file)).read_text(encoding="utf-8") == "submit1"


def test_the_guard_passes_on_the_recorded_host(config):
    """The ordinary case: the operator is where the daemon is, so nothing happens."""
    record_daemon_host(str(config["state_file"]), "submit1")

    assert_daemon_host(str(config["state_file"]), "submit1")


def test_the_guard_refuses_from_another_host_and_names_the_right_one(config):
    """A command from the wrong node is refused with the host to go to.

    THE failure this exists for. The DB is on a networked home, so a second
    machine opening it is cross-host network-FS locking — silent corruption that
    takes the run history with it (design §5). An error naming ``submit1`` turns
    that into an instruction.
    """
    record_daemon_host(str(config["state_file"]), "submit1")

    with pytest.raises(DaemonHostError) as raised:
        assert_daemon_host(str(config["state_file"]), "submit2")

    assert "submit1" in str(raised.value)
    assert "submit2" in str(raised.value)


def test_the_guard_allows_a_db_no_daemon_has_claimed(config):
    """No recorded host means no pin yet — setup and first use must still work.

    The pin arrives with the daemon. Refusing before then would make a fresh
    install unusable by the person setting it up.
    """
    assert_daemon_host(str(config["state_file"]), "anywhere")


def test_the_guard_reads_no_sqlite_at_all(config):
    """The refusal happens even when the state file is not a database.

    Pins the property the design depends on: the guard must run *before*
    anything touches the DB, so it cannot be reading the DB to decide. A state
    file full of garbage still produces a clean host refusal rather than a
    SQLite error.
    """
    state_file = pathlib.Path(config["state_file"])
    state_file.write_text("not a database", encoding="utf-8")
    record_daemon_host(str(state_file), "submit1")

    with pytest.raises(DaemonHostError):
        assert_daemon_host(str(state_file), "submit2")


def test_run_forever_claims_the_host_it_starts_on(config):
    """Starting the daemon pins the DB to this machine.

    Written at startup rather than once at install: moving the daemon to another
    node is a legitimate operation, and the next start should re-pin rather than
    require someone to remember a file.
    """
    _make_folder(config, "run1")

    run_forever(
        config,
        registry=REFERENCE_PROFILES,
        default_pipeline_version=DEFAULT_VERSION,
        max_cycles=1,
        hostname="submit1",
    )

    assert daemon_host_path(str(config["state_file"])).read_text() == "submit1"


def test_a_second_daemon_on_another_host_is_refused(config):
    """Two daemons on two nodes is the gap ``fcntl.flock`` structurally cannot close.

    flock is per-machine: a second daemon on another node takes its own lock and
    both run against one database. The host claim is what actually enforces
    single-instance on a cluster, so a daemon starting on the wrong node refuses.
    """
    record_daemon_host(str(config["state_file"]), "submit1")

    with pytest.raises(DaemonHostError):
        run_forever(
            config,
            registry=REFERENCE_PROFILES,
            default_pipeline_version=DEFAULT_VERSION,
            max_cycles=1,
            hostname="submit2",
        )


def test_a_cycle_with_nothing_to_do_still_logs_a_summary(prepared):
    """An idle cycle emits one ``daemon.cycle`` line — silence never means idle.

    The operator question this exists to answer (3ah): "is there nothing to
    poll, or is something broken and just not logging?" A healthy-but-idle
    daemon used to emit nothing per cycle, indistinguishable on stdout from a
    wedged one. Now every cycle says what it saw and why nothing moved.
    """
    with structlog.testing.capture_logs() as captured:
        result = run_cycle(
            prepared, registry=REFERENCE_PROFILES, lister=_fixed_lister([])
        )

    assert result["status"] == "ok"
    summaries = _events_named(captured, "daemon.cycle")
    assert len(summaries) == 1
    line = summaries[0]
    assert line["cycle.status"] == "ok"
    assert line["cycle.detected"] == 0
    assert line["cycle.eligible"] == 0
    assert line["cycle.skipped"] == 0
    assert line["cycle.held"] == 0
    assert line["cycle.quarantined"] == 0


def test_persisting_an_experiment_id_updates_the_thread_header(prepared):
    """A gate persist that lands ``experiment_id`` refreshes the Slack parent.

    The daemon, as the sole notifier caller, follows the ADR-0006 persist with
    ``update_run_header`` so the thread parent stops reading "exp pending".
    """
    persist_gate = Gate(
        id="persist_gate",
        name="Persist probe",
        satisfied_column="mock_alpha_done",
        order=1,
        needs=(),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS, metadata={"exp": 4242}),
        invalidate=lambda context: None,
        persists={"exp": "experiment_id"},
    )
    version, registry = _single_gate_registry(persist_gate)
    with write_txn(prepared["state_file"]) as connection:
        insert_run(connection, "header_run", pipeline_version=version)
    notifier = RecordingNotifier()

    process_run(prepared, "header_run", registry=registry, notifier=notifier)

    assert ("update_run_header", "header_run") in notifier.calls


def test_announced_gates_post_a_started_milestone(prepared, monkeypatch):
    """Gates in ``GATES_ANNOUNCED_AT_START`` post "<name> started" before running.

    Production request 2026-09-22: NVD runs for hours, and the Slack thread
    jumped from "samplesheet completed" straight to the terminal outcome with
    nothing saying the pipeline had launched.
    """
    import sluice.core.daemon as daemon_module

    monkeypatch.setattr(daemon_module, "GATES_ANNOUNCED_AT_START", ("mock_alpha",))
    notifier = RecordingNotifier()
    _detect(prepared, "announce_run", notifier=notifier)

    process_run(
        prepared, "announce_run", registry=REFERENCE_PROFILES, notifier=notifier
    )

    started = [
        c for c in notifier.calls if c[0] == "post_milestone" and "started" in c[2]
    ]
    assert len(started) == 1
    assert started[0][1] == "announce_run"
    assert started[0][2] == f"{MOCK_ALPHA.name} started"


def test_both_nextflow_gates_are_announced_at_start():
    """``profiling`` and ``nvd`` both post "started" — each launches a Nextflow run.

    Profiling is quicker than NVD, but on CHTC it still waits in the HTCondor
    queue, and without the announcement the thread would jump from "samplesheet
    completed" to "NVD started" with the profiling run invisible in between.
    """
    from sluice.core.daemon import GATES_ANNOUNCED_AT_START

    assert set(GATES_ANNOUNCED_AT_START) == {"profiling", "nvd"}
