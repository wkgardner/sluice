"""The reconcile loop — the sole actor that composes every core module.

Subsystem 9 of Plan A (design of record §7). The daemon adds almost no logic
of its own: each look reads state, decides via ``reconcile``, runs one gate,
persists the result **directly** (never through control ops — ADR-0002's two
write paths; the daemon writes its own progress and never audits it, plan 07
§6), then emits the canonical events every prior plan deferred here. The ONE
sanctioned daemon→control call is B5 disk-floor ``pause`` (⚑1).

Imports are a closed set (plan 09 §3.11): stdlib + ``structlog`` + the built
``sluice.core`` modules only. ``subprocess``/``shutil``/``slack_sdk`` are
forbidden by name, and nothing here deletes a file or calls ``invalidate``
(ADR-0005) — ``tests/test_daemon.py`` pins both by ast parse. Short
transactions only: a persist ``write_txn`` opens strictly *after* ``gate.run``
returns, and every emit/notify is post-commit.
"""

import fcntl
import json
import logging
import logging.handlers
import pathlib
import signal
import threading
import time
import types
from collections.abc import Callable, Mapping

import structlog

from sluice.core import control, stop
from sluice.core.context import (
    is_safe_path_component,
    resolve_context,
    with_start_announcer,
)
from sluice.core.db import (
    get_connection,
    get_metadata,
    get_run,
    initialize_database,
    insert_run,
    is_daemon_eligible,
    now_iso,
    select_active_runs,
    set_metadata,
    update_run,
    write_txn,
)
from sluice.core.events import (
    emit_gate_event,
    emit_run_event,
    format_duration,
    safe_notify,
)
from sluice.core.gate import GateOutcome, GateResult
from sluice.core.profile import (
    UnknownProfileError,
    apply_exclusions,
    load_profile,
    required_gates,
)
from sluice.core.reconcile import is_complete, next_gate

__all__ = [
    "DaemonHostError",
    "DaemonLockError",
    "DAEMON_LAST_LOOK_KEY",
    "acquire_daemon_lock",
    "assemble_registry",
    "assert_daemon_host",
    "configure_logging",
    "daemon_host_path",
    "daemon_lock_path",
    "detect_new_runs",
    "install_signal_handlers",
    "is_daemon_running",
    "prepare_database",
    "process_run",
    "read_daemon_host",
    "read_last_look",
    "record_daemon_host",
    "run_cycle",
    "run_forever",
    "watch_dir_lister",
]

# One module logger for the daemon's own vocabulary (plan 09 §3.10); the three
# pinned lines (run.finished/gate.finished/control.finished) come only from
# events.py/control.py — never rebuilt here.
log = structlog.get_logger("sluice.daemon")


class DaemonLockError(RuntimeError):
    """A second sluice daemon tried to run against a lock another instance holds.

    Single-instance **on one host** is the ``fcntl.flock`` alone (§5; the A3
    lesson — no per-run ``locked_at`` mutex, no stale-lock reaper). Raised loud
    so a double start dies visibly rather than two daemons racing the same DB.

    It cannot see across machines; ``DaemonHostError`` covers that half.
    """


class DaemonHostError(RuntimeError):
    """The database is claimed by a different machine than this one.

    Design §5's single-host invariant says the DB "is only ever opened from the
    one host the daemon and operators share", because "corruption comes from
    cross-host network-FS locking". On a cluster whose home directories are
    shared, nothing enforced that — a colleague on another login node running
    any sluice command became a second NFS client on a WAL database.

    Raised on both sides of the same rule: an operator command from the wrong
    node, and a *second daemon* on another node — which ``fcntl.flock``
    structurally cannot catch, since each machine has its own lock table.
    """


def daemon_host_path(state_file) -> pathlib.Path:
    """The sibling file recording which machine claims this database.

    Beside the DB, exactly like the lock file, and for the same reason: it must
    be readable **before** anything opens the database. ``poller_metadata`` is
    the obvious home and the wrong one — reading a metadata row means opening
    SQLite, which creates the WAL ``-shm`` segment, which is precisely the
    cross-host act the claim exists to prevent. A plain text file costs one
    ``read_text`` and touches nothing.
    """
    state_file = pathlib.Path(state_file)
    return state_file.with_name(state_file.name + ".host")


def record_daemon_host(state_file, hostname: str) -> None:
    """Claim this database for ``hostname``, replacing any previous claim.

    Written at every daemon start rather than once at install: moving the daemon
    to another node is a legitimate operation, and the next start should re-pin
    by itself rather than depending on someone remembering a file.
    """
    host_path = daemon_host_path(state_file)
    host_path.parent.mkdir(parents=True, exist_ok=True)
    host_path.write_text(hostname, encoding="utf-8")


def read_daemon_host(state_file) -> str | None:
    """The machine currently claiming this database, or ``None`` if unclaimed."""
    try:
        recorded = daemon_host_path(state_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return recorded or None


def assert_daemon_host(state_file, current_host: str) -> None:
    """Refuse to touch ``state_file`` from a machine that does not own it.

    An **unclaimed** database is allowed from anywhere: the claim arrives with
    the daemon, and refusing before then would make a fresh install unusable by
    the person setting it up.

    Args:
        state_file: the configured ``state_file`` path.
        current_host: this machine's name, supplied by the caller. Passed in
            rather than read here so this module needs no ``socket`` import and
            a test can name a host without pretending to be one.

    Raises:
        DaemonHostError: another machine claims this database.
    """
    recorded = read_daemon_host(state_file)
    if recorded is None or recorded == current_host:
        return
    raise DaemonHostError(
        f"this sluice database belongs to {recorded!r} and you are on "
        f"{current_host!r}. Its state file is on shared storage, so opening it "
        f"from a second machine risks corrupting it (design §5). Run this on "
        f"{recorded!r} instead."
    )


DAEMON_LAST_LOOK_KEY = "daemon_last_look"


def daemon_lock_path(state_file) -> pathlib.Path:
    """Where the daemon's lock file lives, beside the database.

    One home for the rule: ``run_forever`` takes this lock and
    ``is_daemon_running`` probes it, and a second copy of the derivation would
    let the holder and the probe disagree about which file they mean — the probe
    would then report "not running" forever, which is worse than not probing.
    """
    return pathlib.Path(str(state_file) + ".lock")


def is_daemon_running(state_file) -> bool:
    """Whether a daemon currently holds the lock on THIS machine.

    A true liveness check rather than a heartbeat: it asks the operating system
    who holds the flock, so a daemon busy inside an hours-long gate reports
    running, exactly as one sitting in its poll sleep does. A heartbeat alone
    cannot tell those apart — NVD has no time bound (⚑F), so a stale timestamp
    is as consistent with healthy work as with a dead process.

    Only meaningful on the host the daemon runs on, which is the only place
    operators are (ADR-0007: everyone SSHes into that one machine, and
    ``assert_daemon_host`` refuses commands from anywhere else). ``flock`` is
    per-machine and cannot see a daemon on another node.

    Read-only by construction: opened ``"rb"``, so a probe never creates the lock
    file and never truncates it — ``acquire_daemon_lock`` opens ``"w"``, which
    would. A missing file means no daemon has ever started here, reported as not
    running rather than as an error.
    """
    lock_file = daemon_lock_path(state_file)
    try:
        handle = lock_file.open("rb")
    except OSError:
        # Absent, or unreadable by this operator. Either way we cannot say a
        # daemon is running, and inventing an answer would be worse than "no".
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        # Someone holds it. On this host that is the daemon, by definition.
        return True
    else:
        # We took it, so nobody had it. Release immediately — this probe must
        # never become the thing that blocks a daemon from starting.
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def read_last_look(state_file) -> str | None:
    """When the daemon last began a look, as an ISO timestamp, or ``None``.

    Complements ``is_daemon_running``: the lock says whether a daemon is there,
    this says whether it is getting round to anything. A live daemon whose last
    look is hours old is inside a long gate; a live daemon that never advances
    it is wedged, which nothing else in the system would surface.
    """
    connection = get_connection(str(state_file))
    try:
        return get_metadata(connection, DAEMON_LAST_LOOK_KEY)
    finally:
        connection.close()


def acquire_daemon_lock(lock_path):
    """Take the single-instance advisory lock, returning the open handle to hold.

    Opens (creating) ``lock_path`` and takes a non-blocking exclusive
    ``fcntl.flock``. The caller keeps the returned handle for the daemon's
    life; closing it (or process death) drops the lock — the kernel reaps it,
    so there is no stale-lock file to clean (§5, the A3 lesson).

    Args:
        lock_path: Filesystem path for the lock file — derived by the daemon as
            ``state_file + ".lock"``, on the same local disk as the DB.

    Returns:
        The open file handle; hold it, and ``close()`` it to release.

    Raises:
        DaemonLockError: another instance already holds the lock (the
            ``BlockingIOError``/``OSError`` from a failed non-blocking flock).
    """
    handle = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as flock_error:
        handle.close()
        raise DaemonLockError(
            f"cannot acquire the daemon lock at {lock_path!r}: another sluice "
            f"daemon holds the lock (single-instance is the flock alone, §5)"
        ) from flock_error
    return handle


def assemble_registry(*registries: Mapping[str, object]) -> Mapping[str, object]:
    """Union several profile registries, raising on any duplicate key.

    The composition-root obligation (plan 05 §7): production registries merge
    with the reference registry here, and a ``pipeline_version`` carried by two
    of them is a mis-assembly that would silently shadow one definition with
    another (``{**a, **b}`` hides it). Fail loud instead, and return a
    read-only proxy so no caller can mutate the assembled surface.

    Args:
        registries: the registries to union, in precedence-free order — no key
            may appear in two of them.

    Returns:
        A read-only ``MappingProxyType`` union of every entry.

    Raises:
        ValueError: a ``pipeline_version`` key appears in more than one
            registry; the message names the colliding key.
    """
    merged: dict[str, object] = {}
    for registry in registries:
        for pipeline_version, profile in registry.items():
            if pipeline_version in merged:
                raise ValueError(
                    f"duplicate pipeline_version {pipeline_version!r} across "
                    f"assembled registries — a merge-time shadow (plan 05 §7); "
                    f"every profile key must be unique in the union"
                )
            merged[pipeline_version] = profile
    return types.MappingProxyType(merged)


def prepare_database(config, *, registry, default_pipeline_version):
    """Migrate the DB to head and seed/validate the active-default profile key.

    Startup obligation (plan 05 §7): apply every pending migration, then in one
    short transaction seed ``metadata.pipeline_profile`` with
    ``default_pipeline_version`` if the key is absent — validating it resolves
    first — or, if an operator already set it, leave that value untouched but
    validate the *stored* value still resolves. An unresolvable active default
    is a single loud startup error, never N per-run quarantines.

    Args:
        config: the loaded config; ``state_file`` is read by subscript.
        registry: the assembled profile registry to validate against.
        default_pipeline_version: the deployment default to seed on a fresh DB.

    Returns:
        The database path.

    Raises:
        UnknownProfileError: the default (fresh DB) or the stored value
            (existing DB) does not resolve against ``registry``.
        ValueError: a stored/default value resolves to a mis-keyed profile
            (propagated from ``load_profile``).
    """
    db_path = config["state_file"]
    initialize_database(db_path)
    with write_txn(db_path) as connection:
        stored = get_metadata(connection, control.PIPELINE_PROFILE_KEY)
        if stored is None:
            # Validate before seeding: an unresolvable default must fail at
            # startup, and the rollback leaves the key unseeded.
            load_profile(default_pipeline_version, registry)
            set_metadata(
                connection, control.PIPELINE_PROFILE_KEY, default_pipeline_version
            )
        else:
            # An operator's set_default_profile outranks the deployment default;
            # still validate the stored value resolves (a bad active default is
            # one startup error, not one quarantine per run).
            load_profile(stored, registry)
    return db_path


def watch_dir_lister(config) -> list[tuple[str, float | None]]:
    """List the immediate subdirectories of ``watch_dir`` as sized candidates.

    The reference/test detection source (plan 11 unit 1): the default lister when
    ``detect_new_runs`` is given none, preserving the Plan-A watch-directory
    sweep byte-for-byte. Files are ignored; each subdirectory becomes a
    ``(name, None)`` tuple (the watch-dir sweep never sizes a run — only the
    remote lister carries a ``size_tb``), sorted by name.

    Args:
        config: the loaded config; ``watch_dir`` by subscript.

    Returns:
        ``(name, None)`` tuples for every immediate subdirectory, sorted by name.

    Raises:
        KeyError: ``config`` lacks ``watch_dir``.
    """
    watch_dir = config["watch_dir"]
    if not str(watch_dir).strip():
        # An empty watch_dir is NOT "scan nothing" — Path("") is the CURRENT
        # DIRECTORY, so this would mint a run for every subdirectory of wherever
        # the daemon happens to have been started. Observed for real: an
        # end-to-end run with watch_dir unset detected `.git`, `docs`, `tests`
        # and quarantined them all. Refuse instead (guardrail §8: no silent
        # fallback).
        raise ValueError(
            "watch_dir is empty — refusing to scan the current directory. Set "
            "watch_dir, or inject the production lister (sluice.gates.detection."
            "list_remote_runs) via the daemon's lister seam"
        )
    return sorted(
        (
            (entry.name, None)
            for entry in pathlib.Path(watch_dir).iterdir()
            if entry.is_dir()
        ),
        key=lambda candidate: candidate[0],
    )


def detect_new_runs(config, *, lister=None, notifier=None) -> list[str]:
    """List detection candidates via ``lister`` and insert each unseen run once.

    Detection (§3.2, plan 11): the injected ``lister`` (default
    ``watch_dir_lister``) yields ``(name, size_tb)`` candidates, called exactly
    once **before and outside any transaction** so a network listing never holds
    a ``write_txn`` (§5). Candidates are sorted by **name only** (never
    whole-tuple — comparing ``None`` with a ``float`` on a duplicate name would
    raise ``TypeError``); any name failing ``is_safe_path_component`` is dropped
    with one ``detection.unsafe_name`` error event so a garbled listing cannot
    mint a run named ``..``. In ONE ``write_txn`` each surviving unseen name is
    inserted with ``pipeline_version`` stamped from ``metadata.pipeline_profile``
    read in that same transaction (§8 freeze) and its ``size_tb`` on the row.
    ``insert_run``'s idempotent bool gates the post-commit ``run.detected`` line
    and ``open_run_thread`` notify, so a re-sweep is silent. A lister that raises
    degrades to zero candidates plus one ``detection.failed`` event (the D4
    fail-open posture) — except a ``KeyError`` (missing config key = deployment
    bug), which fails loud.

    Args:
        config: the loaded config; ``state_file`` by subscript (and ``watch_dir``
            only when the default lister is used).
        lister: optional ``config -> [(name, size_tb), ...]`` detection source;
            ``None`` selects ``watch_dir_lister``.
        notifier: optional milestone notifier; called via ``safe_notify``.

    Returns:
        The genuinely-new run names, sorted.

    Raises:
        KeyError: ``config`` lacks a key the active lister or ``state_file``
            requires — a deployment bug that fails loud (the lister's ``KeyError``
            is deliberately re-raised, never contained).
        RuntimeError: ``metadata.pipeline_profile`` is unseeded — only reachable
            if ``prepare_database`` was skipped; names the missing key.
    """
    db_path = config["state_file"]
    active_lister = lister or watch_dir_lister
    # List before the transaction so the write_txn stays short (§5).
    try:
        candidates = active_lister(config)
    except KeyError:
        # A missing config key is a deployment bug — fail loud, matching this
        # function's existing Raises: KeyError contract (plan 11 unit 2).
        raise
    except Exception as listing_error:  # noqa: BLE001 — fail-open (D4/D1 posture)
        # Any other lister failure degrades to zero candidates plus one event;
        # a missing rsync/sshpass binary (FileNotFoundError) lands here too — an
        # intended fail-open path, NOT loud propagation (plan 11 unit 2 / clar 4).
        log.error(
            "detection.failed",
            **{
                "error.type": type(listing_error).__name__,
                "error.message": str(listing_error),
            },
        )
        return []

    # Sort by name only — never a whole-tuple sort (None vs float raises).
    sorted_candidates = sorted(candidates, key=lambda candidate: candidate[0])
    safe_candidates: list[tuple[str, float | None]] = []
    for name, size_tb in sorted_candidates:
        if is_safe_path_component(name):
            safe_candidates.append((name, size_tb))
        else:
            # A garbled/malicious listing must never mint a run named ``..``.
            log.error("detection.unsafe_name", **{"detection.rejected_name": name})

    newly_detected: list[str] = []
    with write_txn(db_path) as connection:
        default_pipeline_version = get_metadata(
            connection, control.PIPELINE_PROFILE_KEY
        )
        if default_pipeline_version is None:
            raise RuntimeError(
                f"metadata key {control.PIPELINE_PROFILE_KEY!r} is not seeded — "
                f"detection needs the active-default profile; run "
                f"prepare_database first (plan 09 §3.2)"
            )
        for name, size_tb in safe_candidates:
            if insert_run(
                connection,
                name,
                pipeline_version=default_pipeline_version,
                size_tb=size_tb,
            ):
                newly_detected.append(name)
    # Post-commit only (persist-then-emit): the rows exist before any line or
    # notify describes them.
    for name in newly_detected:
        log.info(
            "run.detected",
            **{"run.name": name, "run.pipeline_version": default_pipeline_version},
        )
        # The A5 seam, completed: open_run_thread RETURNS the notifier's thread id
        # and the daemon persists it, so replies find their thread across restarts.
        # The daemon stays the sole writer of run state — a notifier that wrote its
        # own column would be a second authority over the runs table.
        thread_id = safe_notify(notifier, "open_run_thread", name)
        if thread_id:
            # Its own short transaction, outside the detection one: a notifier
            # round-trip must never be inside a write lock (§5), and a failure to
            # record the id must not roll back the detection that already
            # succeeded. Worst case the thread id is lost and the next
            # open_run_thread opens a fresh thread — noisy, never incorrect.
            with write_txn(db_path) as connection:
                update_run(connection, name, {"slack_thread_ts": str(thread_id)})
    return newly_detected


def _get_run_row(db_path, run_name):
    """Fetch one run row on a fresh short-lived read connection, then close it.

    Every drive step re-fetches through this — statelessness is *re-derivation*
    from the committed DB, never a cached row (§3.3). A plain read connection,
    closed immediately (``get_connection``'s context manager would commit, not
    close — the DB is the single authority).

    Args:
        db_path: the state database path.
        run_name: the run to fetch.

    Returns:
        The run row, or ``None`` if no such run exists.
    """
    connection = get_connection(db_path)
    try:
        return get_run(connection, run_name)
    finally:
        connection.close()


def _stamp_quarantine(db_path, run_name, reason: str) -> None:
    """Write the quarantine pair ``errored_at``/``error_reason`` in one short txn.

    The direct progress write (never audited, plan 07 §6): the paired columns
    move together to satisfy the schema CHECK. Opened only after the decision
    to quarantine is made — no gate call is in flight.

    Args:
        db_path: the state database path.
        run_name: the run to quarantine.
        reason: the human-readable ``error_reason`` naming the anomaly.
    """
    with write_txn(db_path) as connection:
        update_run(
            connection, run_name, {"error_reason": reason, "errored_at": now_iso()}
        )


def _quarantine_without_gate(db_path, run_name, reason: str, notifier) -> str:
    """Quarantine a run for a resolution anomaly with no gate to blame (§3.5).

    The per-run isolation contract: a contract/context resolution failure
    quarantines exactly this run and emits ``run.finished`` with no ``gates``
    and no ``failed.gate`` (there is no gate), then notifies — never a loop
    crash.

    Args:
        db_path: the state database path.
        run_name: the run to quarantine.
        reason: the ``error_reason`` naming the resolution anomaly.
        notifier: optional milestone notifier.

    Returns:
        The literal token ``"quarantined"``.
    """
    _stamp_quarantine(db_path, run_name, reason)
    committed_row = _get_run_row(db_path, run_name)
    emit_run_event(committed_row)
    safe_notify(notifier, "finish_run_thread", run_name, "quarantined", reason)
    return "quarantined"


# Gates announced in Slack BEFORE they run: the hours-long ones that would
# otherwise leave the thread silent between the previous milestone and their
# terminal outcome (production request 2026-09-22 — NVD launched with nothing
# in the channel saying so).
GATES_ANNOUNCED_AT_START = ("download",)

# The Tower-enabled Nextflow gates announce their OWN start, through
# ``RunContext.announce_start``, once Nextflow has recorded the run's Seqera
# watch link — so "started" carries the link to monitor it (request
# 2026-11-13). The link exists only seconds after launch, inside gate.run.
GATES_ANNOUNCING_THEIR_OWN_START = ("profiling", "nvd")


def _start_announcer(notifier, run_name, gate):
    """The callback a Nextflow gate calls once its pipeline has started.

    Posts ``<gate name> started``, plus the Seqera link when the gate found one.
    Through ``safe_notify``, so a Slack failure never reaches the gate.
    """

    def announce_start(watch_url):
        text = f"{gate.name} started"
        if watch_url:
            text = f"{text} \u00b7 monitor progress on Seqera: {watch_url}"
        safe_notify(notifier, "post_milestone", run_name, text)

    return announce_start


def _milestone_metrics(metadata) -> str:
    """A compact `` · key value`` suffix from a gate's scalar metadata.

    Ints and floats only (bools excluded — flags are not metrics), so counts
    and sizes reach Slack while paths and other strings stay in the event log
    where they belong (sweep 2026-09-25: bare "<name> completed" carried none
    of the numbers the gate had just computed).
    """
    if not metadata:
        return ""
    scalar_parts = [
        f"{key.replace('_', ' ')} {value}"
        for key, value in sorted(metadata.items())
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return (" \u00b7 " + " \u00b7 ".join(scalar_parts)) if scalar_parts else ""


def _post_success_notice(notifier, run_name, metadata) -> None:
    """Post a SUCCESS result's ``notice`` as its own thread reply, if it has one.

    Gates never hold the notifier (they only return results \u2014 ADR-0002), so a gate
    that has something a human must read beyond counts returns it in its metadata:
    ``notice`` (the text) and ``notice_mention`` (whether to @-mention the on-call).
    The registration gate uses this for its report of skipped samples. A missing or
    empty ``notice`` posts nothing, so every other gate's thread is unchanged.
    """
    if not metadata or not metadata.get("notice"):
        return
    safe_notify(
        notifier,
        "post_milestone",
        run_name,
        str(metadata["notice"]),
        mention=bool(metadata.get("notice_mention", False)),
    )


def process_run(config, run_name, *, registry, notifier=None, should_stop=None) -> str:
    """Drive one run through its gates until it blocks, one gate per look.

    The per-run step (§3.3–3.6), repeated within a cycle (⚑2 drive-until-
    blocked): each iteration re-fetches the row, re-checks eligibility
    (``lifecycle == 'active' and errored_at is None``), re-derives the contract
    (``apply_exclusions(load_profile(...), run_exclusions(row))``), and either
    stamps completion, runs the lowest-order runnable gate, or stops. Every
    step is a full stateless re-derivation, so a hold/skip/quarantine landing
    mid-drive is honored at the next gate boundary. Quarantine-grade
    resolution failures (§3.5) quarantine this ONE run; reconcile's schema-drift
    ``KeyError`` and persist ``OperationalError`` are fatal by design and
    propagate — the catches wrap the specific call sites, never the whole body.

    Args:
        config: the loaded config.
        run_name: the run to drive.
        registry: the assembled profile registry.
        notifier: optional milestone notifier.
        should_stop: optional zero-arg predicate checked between gate steps; a
            truthy return stops the drive (SIGTERM finishes the current gate).

    Returns:
        One token from ``{"missing", "ineligible", "quarantined", "held",
        "completed", "already-complete", "stopped"}``.
    """
    db_path = config["state_file"]
    while True:
        if should_stop is not None and should_stop():
            return "stopped"

        run_row = _get_run_row(db_path, run_name)
        if run_row is None:
            # The missing-row anomaly: loud, never fatal (the row was deleted
            # between selection and this look).
            log.error("run.vanished", **{"run.name": run_name})
            return "missing"

        # §3.3 eligibility, re-checked on the fresh row every step — the one
        # canonical rule, called rather than re-typed (plan 14).
        if not is_daemon_eligible(run_row):
            return "ineligible"

        # Contract derivation — the §3.5 quarantine class (call-site-scoped, so
        # reconcile's fatal KeyError below is never swallowed).
        try:
            base_profile = load_profile(run_row["pipeline_version"], registry)
            excluded_gate_ids = control.run_exclusions(run_row)
            contract = apply_exclusions(base_profile, excluded_gate_ids)
        except (UnknownProfileError, ValueError) as resolution_error:
            reason = (
                f"contract resolution failed for run {run_name!r}: "
                f"{type(resolution_error).__name__}: {resolution_error}"
            )
            return _quarantine_without_gate(db_path, run_name, reason, notifier)

        # Completion look (§3.6): re-derived every look; completed_at is a cache.
        if is_complete(run_row, contract):
            if run_row["completed_at"] is None:
                with write_txn(db_path) as connection:
                    update_run(connection, run_name, {"completed_at": now_iso()})
                committed_row = _get_run_row(db_path, run_name)
                emit_run_event(committed_row, gates=required_gates(contract))
                safe_notify(notifier, "finish_run_thread", run_name, "completed")
                return "completed"
            return "already-complete"

        gate = next_gate(run_row, contract)

        # Context resolution — the second §3.5 quarantine call site.
        try:
            context = resolve_context(run_row, config)
        except (ValueError, KeyError) as context_error:
            reason = (
                f"context resolution failed for run {run_name!r}: "
                f"{type(context_error).__name__}: {context_error}"
            )
            return _quarantine_without_gate(db_path, run_name, reason, notifier)

        # The caller-mkdirs obligation (plan 04 §7): create workdir before run.
        context.workdir.mkdir(parents=True, exist_ok=True)
        if gate.id in GATES_ANNOUNCING_THEIR_OWN_START:
            context = with_start_announcer(
                context, _start_announcer(notifier, run_name, gate)
            )

        # Time gate.run with a monotonic clock (no datetime import); the persist
        # write_txn opens only AFTER this returns (short transactions, §5).
        started = time.monotonic()
        try:
            if gate.id in GATES_ANNOUNCED_AT_START:
                safe_notify(
                    notifier, "post_milestone", run_name, f"{gate.name} started"
                )
            result = gate.run(context)
        except Exception as gate_error:  # noqa: BLE001 — an escape becomes FAILURE
            # §3.4: a raised gate never dies the loop; it becomes a quarantine.
            result = GateResult(
                GateOutcome.FAILURE,
                reason=(
                    f"unhandled {type(gate_error).__name__} in gate "
                    f"{gate.id!r}: {gate_error}"
                ),
            )
        duration_ms = int((time.monotonic() - started) * 1000)

        outcome = result.outcome
        if outcome is GateOutcome.SUCCESS:
            # ADR-0006 d.2: on SUCCESS the daemon writes every declared persists
            # output AND the satisfaction column in ONE update_run / ONE
            # write_txn — output and checkmark can never disagree. Gates only
            # RETURN metadata (ADR-0002); the daemon is the sole writer.
            metadata = result.metadata or {}
            missing = [key for key in gate.persists if key not in metadata]
            if missing:
                # A gate that returned SUCCESS but omitted a declared output
                # cannot have its checkmark set — the output and its column move
                # together or not at all. Quarantine this ONE run (data
                # anomaly), mirroring the SKIPPED/FAILURE tail: a SYNTHETIC
                # FAILURE feeds the gate event so gate.finished reads honestly
                # as a failure, not a misleading success. Nothing is written to
                # the satisfaction column, so the refusal is atomic.
                reason = (
                    f"gate {gate.id!r} returned SUCCESS but omitted declared "
                    f"persists key(s) {sorted(missing)} — the output and its "
                    f"satisfaction column must be written together (ADR-0006 d.2)"
                )
                failure = GateResult(GateOutcome.FAILURE, reason=reason)
                _stamp_quarantine(db_path, run_name, reason)
                emit_gate_event(run_name, gate, failure, duration_ms=duration_ms)
                committed_row = _get_run_row(db_path, run_name)
                emit_run_event(committed_row, extra={"failed.gate": gate.id})
                safe_notify(
                    notifier, "finish_run_thread", run_name, "quarantined", reason
                )
                return "quarantined"

            # Missing = key ABSENT (guarded above), not value-is-None: a present
            # key with a None value is the gate author's choice and persists as
            # NULL. A persists column absent from the runs whitelist raises
            # UnknownColumnError (a KeyError subclass) out of update_run — fatal
            # by design (the catches above are call-site-scoped, so it is not
            # swallowed), and the write_txn rolls back, so neither the output
            # nor the checkmark lands.
            fields = {gate.satisfied_column: 1}
            for metadata_key, column in gate.persists.items():
                fields[column] = metadata[metadata_key]
            with write_txn(db_path) as connection:
                update_run(connection, run_name, fields)
            emit_gate_event(run_name, gate, result, duration_ms=duration_ms)
            if "experiment_id" in fields:
                # The parent message said "exp pending" until now — rewrite it
                # with the id everyone scans the channel for.
                safe_notify(notifier, "update_run_header", run_name)
            safe_notify(
                notifier,
                "post_milestone",
                run_name,
                f"{gate.name} completed in {format_duration(duration_ms)}"
                f"{_milestone_metrics(result.metadata)}",
            )
            _post_success_notice(notifier, run_name, result.metadata)
            continue  # drive the next gate

        if outcome is GateOutcome.HELD:
            # Nothing is persisted (plan 02 §3.2); terminate in-flight work if
            # the gate is interruptible; no run.finished (held is not terminal).
            if gate.terminate is not None:
                gate.terminate(context)
            emit_gate_event(run_name, gate, result, duration_ms=duration_ms)
            return "held"

        # FAILURE or SKIPPED — both quarantine as a failure (ADR-0004 d.4 for
        # SKIPPED: the column is never set; the gate/contract disagreement is a
        # human decision).
        if outcome is GateOutcome.SKIPPED:
            reason = (
                f"gate '{gate.id}' returned SKIPPED but is in this run's "
                f"contract (ADR-0004 d.4): the gate and the contract disagree "
                f"— a human decides which is wrong"
            )
        else:
            reason = result.reason
        _stamp_quarantine(db_path, run_name, reason)
        emit_gate_event(run_name, gate, result, duration_ms=duration_ms)
        committed_row = _get_run_row(db_path, run_name)
        emit_run_event(committed_row, extra={"failed.gate": gate.id})
        safe_notify(notifier, "finish_run_thread", run_name, "quarantined", reason)
        return "quarantined"


def run_cycle(
    config,
    *,
    registry,
    notifier=None,
    disk_floor_check: Callable[[Mapping], str | None] | None = None,
    lister=None,
    should_stop=None,
) -> dict:
    """Run one full look: pause head-check → disk floor → detect → per-run drive.

    Cycle order is spec §7 verbatim (§3.8). The loop-head ``is_paused`` check
    short-circuits *everything* — including detection and the disk check — so a
    disk-tripped pause cannot re-fire ``control.pause`` every cycle (one audit
    row per trip). Below the floor (B5, ⚑1): audited ``control.pause`` with
    actor ``"daemon"`` plus one error-level ``daemon.paused`` line; no
    auto-clear (operator ``resume`` only), no run quarantine (global check, no
    tripping run). A ``disk_floor_check`` that raises is logged and treated as
    healthy (fail-open, D1 posture).

    Args:
        config: the loaded config; ``state_file`` by subscript.
        registry: the assembled profile registry.
        notifier: optional milestone notifier.
        disk_floor_check: optional ``config -> reason|None`` seam; a reason
            string trips the auto-pause.
        lister: optional detection-source seam handed straight to
            ``detect_new_runs`` (``None`` = the watch-dir default).
        should_stop: optional zero-arg predicate checked between runs and passed
            through to each ``process_run``.

    Returns:
        A small plain status dict (never authoritative state):
        ``{"status": ..., "detected": [...], "runs": {name: token}}``.
    """
    db_path = config["state_file"]

    # §3.8 loop-head pause parse — the one B5 read site; a plain read connection,
    # closed immediately.
    pause_connection = get_connection(db_path)
    try:
        currently_paused = control.is_paused(pause_connection)
    finally:
        pause_connection.close()
    if currently_paused:
        log.info("daemon.cycle", **{"cycle.status": "paused"})
        return {"status": "paused", "detected": [], "runs": {}}

    if disk_floor_check is not None:
        try:
            floor_reason = disk_floor_check(config)
        except Exception as disk_check_error:  # noqa: BLE001 — fail-open (D1)
            log.error(
                "disk_floor_check.failed",
                **{"error.type": type(disk_check_error).__name__},
            )
            floor_reason = None
        if floor_reason is not None:
            reason = f"disk floor: {floor_reason}"
            # The ONE sanctioned daemon→control call (⚑1): audited pause, actor
            # "daemon", target_run NULL.
            control.pause(db_path, "daemon", reason=reason)
            log.error("daemon.paused", **{"daemon.reason": reason})
            log.info("daemon.cycle", **{"cycle.status": "disk-paused"})
            return {"status": "disk-paused", "detected": [], "runs": {}}

    detected = detect_new_runs(config, lister=lister, notifier=notifier)

    selection_connection = get_connection(db_path)
    try:
        active_rows = select_active_runs(selection_connection)
        # Fence counts for the cycle summary: how many runs exist but are
        # deliberately out of the daemon's reach. This is what lets an operator
        # tell "healthy but nothing eligible" from "broken and silent" (3ah).
        skipped_count, held_count, quarantined_count = selection_connection.execute(
            "SELECT COALESCE(SUM(lifecycle = 'skipped'), 0),"
            "       COALESCE(SUM(lifecycle = 'held'), 0),"
            "       COALESCE(SUM(errored_at IS NOT NULL), 0)"
            " FROM runs"
        ).fetchone()
    finally:
        selection_connection.close()

    run_tokens: dict[str, str] = {}
    stopped = False
    for run_row in active_rows:
        if should_stop is not None and should_stop():
            stopped = True
            break
        token = process_run(
            config,
            run_row["name"],
            registry=registry,
            notifier=notifier,
            should_stop=should_stop,
        )
        run_tokens[run_row["name"]] = token
        if token == "stopped":
            stopped = True
            break

    cycle_status = "stopped" if stopped else "ok"
    # One summary line per look, always — the canonical answer to "what did
    # the daemon just do, and if nothing, why not" (3ah).
    log.info(
        "daemon.cycle",
        **{
            "cycle.status": cycle_status,
            "cycle.detected": len(detected),
            "cycle.eligible": len(active_rows),
            "cycle.runs": run_tokens,
            "cycle.skipped": skipped_count,
            "cycle.held": held_count,
            "cycle.quarantined": quarantined_count,
        },
    )
    return {
        "status": cycle_status,
        "detected": detected,
        "runs": run_tokens,
    }


def _make_shutdown_handler(shutdown_event: threading.Event):
    """Build a signal handler that records the request and sets the shutdown flag.

    Decision A (§3.7): a signal carries zero control semantics — it emits the
    ``daemon.shutdown_requested`` line and sets a ``threading.Event``, nothing
    else. No signal ever reaches a gate; holds stay poll-only.

    Args:
        shutdown_event: the flag the handler sets.

    Returns:
        A ``(signum, frame)`` handler function.
    """

    def _handle(signum, frame):
        """Record the shutdown request and set the flag; touch nothing else."""
        log.info(
            "daemon.shutdown_requested",
            **{"daemon.signal": signal.Signals(signum).name},
        )
        shutdown_event.set()

    return _handle


def install_signal_handlers(shutdown_event: threading.Event) -> None:
    """Install the SIGTERM/SIGINT handlers that set ``shutdown_event`` (decision A).

    Both signals map to the same flag-only handler: the current cycle finishes
    its gate, then the loop exits cleanly (systemd-ready). Must run on the main
    thread.

    Args:
        shutdown_event: the flag the handlers set on receipt of a signal.
    """
    handler = _make_shutdown_handler(shutdown_event)
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _interruptible_sleep(total_seconds: float, shutdown_event: threading.Event) -> None:
    """Sleep up to ``total_seconds``, waking early once ``shutdown_event`` is set.

    Sliced ~1 s against the flag (§3.7): a flag-only signal handler does not
    interrupt ``time.sleep``, so the sleep is chopped into short naps that
    re-check the event, keeping SIGTERM's shutdown latency near a second.

    Args:
        total_seconds: the poll interval to wait.
        shutdown_event: the flag whose set state ends the wait early.
    """
    deadline = time.monotonic() + total_seconds
    while not shutdown_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def run_forever(
    config,
    *,
    registry,
    default_pipeline_version,
    notifier=None,
    disk_floor_check: Callable[[Mapping], str | None] | None = None,
    lister=None,
    max_cycles: int | None = None,
    hostname: str | None = None,
) -> None:
    """Take the lock, prepare state, then run cycles until shutdown or ``max_cycles``.

    The process entrypoint (§3.7), built systemd-ready. Startup order: read
    ``poll_interval_minutes`` by subscript (fail-loud before any work), acquire
    the single-instance lock (loud refusal of a second instance, before the
    DB), ``prepare_database``, install the flag-only signal handlers, emit
    ``daemon.started``. Each iteration runs one ``run_cycle`` with
    ``should_stop`` bound to the shutdown flag, then an interruptible sleep;
    ``max_cycles`` gives the ``--once`` mode. On the flag or exhaustion:
    ``daemon.stopped`` and a finally-guaranteed lock release.

    Args:
        config: the loaded config; ``poll_interval_minutes`` and ``state_file``
            by subscript.
        registry: the assembled profile registry.
        default_pipeline_version: the deployment default seeded on a fresh DB.
        notifier: optional milestone notifier.
        disk_floor_check: optional disk-floor seam (see ``run_cycle``).
        lister: optional detection-source seam passed through each ``run_cycle``
            (``None`` = the watch-dir default).
        max_cycles: run at most this many cycles then stop (``None`` = forever).
        hostname: this machine's name, used to claim the database. Supplied by
            the caller rather than read here, so this module needs no ``socket``
            import and its closed set (§3.11) stays closed. ``None`` skips both
            the check and the claim — for a caller that has no host to offer.

    Raises:
        KeyError: ``config`` lacks ``poll_interval_minutes`` or ``state_file``
            (fail-loud before the lock is taken).
        DaemonHostError: another machine claims this database (before the lock,
            and before the DB — a second daemon across nodes is the gap the
            flock cannot see).
        DaemonLockError: another instance holds the lock (before the DB).
    """
    # Fail loud on missing config BEFORE any side effect (unit 6).
    poll_interval_minutes = config["poll_interval_minutes"]
    state_file = str(config["state_file"])
    lock_path = daemon_lock_path(state_file)

    # Ordered before the lock deliberately: the flock is per-machine, so on the
    # wrong node it would succeed and tell us nothing. The host claim is the
    # only check that can refuse a second daemon on a second machine.
    if hostname is not None:
        assert_daemon_host(state_file, hostname)

    lock_handle = acquire_daemon_lock(lock_path)
    try:
        # Claimed after the lock, so a refused second instance on THIS host
        # never rewrites a claim it does not hold.
        if hostname is not None:
            record_daemon_host(state_file, hostname)
        prepare_database(
            config,
            registry=registry,
            default_pipeline_version=default_pipeline_version,
        )
        # The module-level cooperative flag (core.stop), not a local Event:
        # long-running gate internals poll the same flag at their submission
        # boundaries, so one signal stops both the cycle loop AND an in-flight
        # download's wave submission (Bug B, HANDOFF 3ac). Cleared at startup
        # so a previous shutdown can never make a fresh daemon refuse work.
        shutdown_event = stop.SHUTDOWN
        shutdown_event.clear()
        install_signal_handlers(shutdown_event)
        log.info(
            "daemon.started", **{"daemon.poll_interval_minutes": poll_interval_minutes}
        )

        cycles_run = 0
        while not shutdown_event.is_set():
            if max_cycles is not None and cycles_run >= max_cycles:
                break
            # Stamped before the look, not after: a gate can run for hours (⚑F),
            # and a timestamp written only on completion would leave an operator
            # unable to tell "started a look and is working" from "died before
            # starting one". Its own transaction, so a later gate failure cannot
            # roll the heartbeat back.
            with write_txn(state_file) as heartbeat_connection:
                set_metadata(heartbeat_connection, DAEMON_LAST_LOOK_KEY, now_iso())
            run_cycle(
                config,
                registry=registry,
                notifier=notifier,
                disk_floor_check=disk_floor_check,
                lister=lister,
                should_stop=shutdown_event.is_set,
            )
            cycles_run += 1
            if max_cycles is not None and cycles_run >= max_cycles:
                break
            _interruptible_sleep(poll_interval_minutes * 60, shutdown_event)

        log.info("daemon.stopped", **{"daemon.cycles_run": cycles_run})
    finally:
        lock_handle.close()


def _make_sink_processor(
    events_logger, console_logger, console_renderer, run_events_dir
):
    """Build the terminal structlog processor that fans one event out to the sinks.

    The routing heart of ``configure_logging`` (§3.11): every event is rendered
    to a JSON line and written to the rotating events sink; an event carrying a
    safe ``"run.name"`` is also appended to ``run_events_dir/<run.name>.jsonl``
    (the literal field plan 03 fixed as load-bearing); an unsafe name goes to
    the main sink only plus one error line. The event is also rendered to the
    console sink. The processor drops the event so no logger factory writes it
    again.

    Args:
        events_logger: the stdlib logger owning the rotating jsonl handler.
        console_logger: the stdlib logger owning the console handler.
        console_renderer: a ``structlog`` console renderer callable.
        run_events_dir: the directory for per-run jsonl files.

    Returns:
        A structlog processor ``(logger, method_name, event_dict)``.
    """

    def _process(logger, method_name, event_dict):
        """Render one event to every sink, then drop it from the chain."""
        json_line = json.dumps(dict(event_dict), default=str, sort_keys=True)
        events_logger.info(json_line)

        run_name = event_dict.get("run.name")
        if run_name is not None:
            if is_safe_path_component(run_name):
                per_run_path = pathlib.Path(run_events_dir) / f"{run_name}.jsonl"
                with per_run_path.open("a", encoding="utf-8") as per_run_file:
                    per_run_file.write(json_line + "\n")
            else:
                # Never turn an unsafe identity into a path (§3.11): main sink
                # only, plus one error line (no run.name, so no recursion here).
                log.error(
                    "run_events.unsafe_name",
                    **{"run_events.rejected_name": run_name},
                )

        console_logger.info(console_renderer(logger, method_name, dict(event_dict)))
        raise structlog.DropEvent

    return _process


def _reset_stdlib_logger(name: str) -> logging.Logger:
    """Return the named stdlib logger with its handlers removed and closed.

    Idempotency support for ``configure_logging`` (unit 7): repeat calls must
    replace, never stack, handlers — so every prior handler on the sink logger
    is detached and closed before a fresh one is attached.

    Args:
        name: the stdlib logger name to reset.

    Returns:
        The reset logger (no handlers, non-propagating, INFO level).
    """
    stdlib_logger = logging.getLogger(name)
    for existing_handler in list(stdlib_logger.handlers):
        stdlib_logger.removeHandler(existing_handler)
        existing_handler.close()
    stdlib_logger.setLevel(logging.INFO)
    stdlib_logger.propagate = False
    return stdlib_logger


def configure_logging(config) -> None:
    """Configure structlog: ISO-UTC lines, a rotating jsonl sink, per-run routing.

    The minimal ``logging_setup`` port (plan 03 §7): every event gets an
    ISO-UTC timestamp and level, is written as a JSON line to
    ``config["events_file"]`` through a ``RotatingFileHandler`` sized by
    ``events_max_mb``/``events_backup_count``, and — iff it carries a safe
    ``"run.name"`` — appended to ``config["run_events_dir"]/<run.name>.jsonl``.
    The console sink renders the same events for humans. Idempotent: repeat
    calls replace, never stack, handlers. Heartbeats and the deployment-context
    fields stay Plan B (D5). Config keys are read by subscript, fail-loud.

    Args:
        config: the loaded config; ``events_file``, ``events_max_mb``,
            ``events_backup_count`` and ``run_events_dir`` by subscript.

    Raises:
        KeyError: ``config`` lacks any required logging key.
    """
    events_file = pathlib.Path(config["events_file"])
    events_max_mb = config["events_max_mb"]
    events_backup_count = config["events_backup_count"]
    run_events_dir = pathlib.Path(config["run_events_dir"])

    events_file.parent.mkdir(parents=True, exist_ok=True)
    run_events_dir.mkdir(parents=True, exist_ok=True)

    events_logger = _reset_stdlib_logger("sluice._events_sink")
    rotating_handler = logging.handlers.RotatingFileHandler(
        events_file,
        maxBytes=events_max_mb * 2**20,
        backupCount=events_backup_count,
        encoding="utf-8",
    )
    rotating_handler.setFormatter(logging.Formatter("%(message)s"))
    events_logger.addHandler(rotating_handler)

    console_logger = _reset_stdlib_logger("sluice._console_sink")
    # No stream argument: logging.StreamHandler defaults to stderr, so the
    # console sink reaches stderr without this module importing sys (§3.11).
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    console_logger.addHandler(console_handler)

    console_renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _make_sink_processor(
                events_logger, console_logger, console_renderer, run_events_dir
            ),
        ],
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )
