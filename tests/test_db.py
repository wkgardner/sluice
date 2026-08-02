"""Behavioral tests for ``sluice.core.db`` — the SQLite state store primitives.

The database is the single source of truth for run state (design of record §5),
so each primitive is pinned by a test asserting observable behavior, not shape.
"""

import ast
import contextlib
import inspect
import os
import pathlib
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

import sluice.core.db
from sluice.core.db import now_iso

# Generous bound to kill frozen-constant / cached-at-import regressions, not to
# measure scheduler latency.
MAXIMUM_CLOCK_SKEW = timedelta(seconds=5)


@contextlib.contextmanager
def host_timezone(zone_name: str):
    """Run the block as though the host's local time zone were ``zone_name``.

    ``time.tzset()`` is required for ``datetime.now().astimezone()`` to pick up
    the changed ``TZ``; setting the env var alone is not enough. Unix only.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = zone_name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_now_iso_returns_a_live_utc_iso_string():
    """``now_iso()`` returns an aware UTC instant that tracks the clock.

    The only sluice *decision* it encodes is UTC-not-local; everything else is a
    stdlib/ISO-8601 property. Asserts the delegation is wired — a frozen-constant
    or local-time regression both fail.
    """
    observed = datetime.fromisoformat(now_iso())

    assert observed.utcoffset() == timedelta(0), "now_iso() must be UTC, not local"
    assert abs(observed - datetime.now(timezone.utc)) < MAXIMUM_CLOCK_SKEW


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="TZ manipulation is Unix only")
@pytest.mark.parametrize(
    "zone_name",
    ["UTC", "America/Chicago", "Asia/Kolkata"],
)
def test_now_iso_is_utc_regardless_of_host_timezone(zone_name):
    """The offset is ``+00:00`` even when the host's local zone is not UTC.

    Varying the host zone is what separates explicit-UTC from local-aware: on a
    UTC host, ``datetime.now().astimezone()`` renders ``+00:00`` too, so a
    same-zone check would pass a local-time implementation. ``Asia/Kolkata`` also
    pins the half-hour offset (``+05:30``).
    """
    with host_timezone(zone_name):
        stamp = now_iso()

    assert stamp.endswith("+00:00"), f"local zone {zone_name} leaked into the timestamp"
    assert datetime.fromisoformat(stamp).utcoffset() == timedelta(0)


# --------------------------------------------------------------------------
# Unit 2 — get_connection(db_path) + module hygiene
#
# Every test opens a *real file* under ``tmp_path``, never ``:memory:``: SQLite
# silently refuses WAL on an in-memory database (``PRAGMA journal_mode`` reports
# ``memory``), which would make the pragma assertions vacuous.
# --------------------------------------------------------------------------


@contextlib.contextmanager
def opened_connection(db_path):
    """Yield ``get_connection(db_path)`` and close it, whatever the test does.

    Resolved through the module object, not a top-level import, so an unwritten
    function surfaces as ``AttributeError`` in the test rather than an import-time
    failure that reddens unrelated passing tests.
    """
    connection = sluice.core.db.get_connection(db_path)
    try:
        yield connection
    finally:
        connection.close()


def test_get_connection_reports_wal_journal_mode(tmp_path):
    """The opened database is actually *in* WAL mode, as queried back from SQLite.

    Queried via ``PRAGMA journal_mode`` (not by observing the statement was
    issued), since SQLite may accept ``journal_mode=WAL`` and decline to act on
    it, returning the mode it actually chose. WAL is what lets the daemon's
    readers run without blocking on a writer (design of record §5).
    """
    with opened_connection(tmp_path / "state.db") as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert journal_mode.lower() == "wal"


def test_get_connection_reports_synchronous_normal(tmp_path):
    """``PRAGMA synchronous`` reads back as ``1`` (NORMAL), not SQLite's default ``2``.

    The one durability pragma this unit genuinely changes: NORMAL is the safe
    setting under WAL. Because the default is ``2`` (FULL), this fails loudly if
    the statement is dropped or only ``journal_mode`` is set (errata A1).
    """
    with opened_connection(tmp_path / "state.db") as connection:
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert synchronous == 1


# NOTE: no `test_..._busy_timeout_is_5000` deliberately. busy_timeout is already
# 5000 ms via Python's `sqlite3.connect(timeout=5.0)` default (errata A1), so a
# test would pass against an empty implementation — it tests the stdlib default,
# not a sluice decision. The genuinely-set pragma is `synchronous=NORMAL`, above.


def test_get_connection_rows_are_mappings(tmp_path):
    """Rows come back keyed by column name, not as bare positional tuples.

    Consumers read ``row["lifecycle"]`` by name; the default tuple factory makes
    those raise ``TypeError``, and positional reads silently read the wrong column
    once a later migration appends one.
    """
    with opened_connection(tmp_path / "state.db") as connection:
        connection.execute("CREATE TABLE t (a, b)")
        connection.execute("INSERT INTO t (a, b) VALUES (1, 2)")
        row = connection.execute("SELECT a, b FROM t").fetchone()

        assert row["a"] == 1
        assert dict(row) == {"a": 1, "b": 2}


def test_get_connection_is_autocommit(tmp_path):
    """A bare statement commits itself and leaves no transaction open (fork F2).

    Python's default ``isolation_level=""`` opens an implicit transaction on the
    first DML and holds the write lock until commit — the long-lived write
    transaction design of record §5 forbids. ``isolation_level=None`` makes each
    statement stand alone; batching is opt-in via ``write_txn``'s ``BEGIN
    IMMEDIATE``. The insert makes ``in_transaction is False`` load-bearing (it is
    ``False`` before any DML too); ``isolation_level is None`` pins the setting.
    """
    with opened_connection(tmp_path / "state.db") as connection:
        connection.execute("CREATE TABLE t (a)")
        connection.execute("INSERT INTO t (a) VALUES (1)")

        assert connection.isolation_level is None
        assert connection.in_transaction is False


def test_get_connection_does_not_create_parent_directory(tmp_path):
    """A missing parent directory is an error, not something to helpfully create.

    Diverges from python-poller's ``os.makedirs()`` (``poller_state_db.py:78``):
    a factory that creates dirs silently stands up an empty state database next to
    a typo'd path, and the daemon then reports zero runs instead of refusing to
    start. Provisioning is config/bootstrap's job.
    """
    missing_parent = tmp_path / "nope"

    with pytest.raises(sqlite3.OperationalError):
        sluice.core.db.get_connection(missing_parent / "state.db")

    assert missing_parent.exists() is False


def module_level_values(module):
    """Yield every module-level value, recursing into plain containers.

    A flat ``vars(module).values()`` scan misses a cache held one level down
    (``_POOL: dict[str, Connection]``), so recursion makes the scan mean "the
    module holds a connection anywhere". Recurses into tuple/list/set/frozenset
    members and both keys and values of dicts (as unit 13 also relies on).
    """
    seen_ids = set()

    def walk(value):
        # Guard against a self-referential container; also keeps the walk linear.
        if id(value) in seen_ids:
            return
        seen_ids.add(id(value))

        yield value

        if isinstance(value, (tuple, list, set, frozenset)):
            for member in value:
                yield from walk(member)
        elif isinstance(value, dict):
            for key, member in value.items():
                yield from walk(key)
                yield from walk(member)

    for value in vars(module).values():
        yield from walk(value)


def test_get_connection_returns_a_fresh_connection_each_call(tmp_path):
    """Each call yields a real, distinct connection, and the module caches none.

    Guards "the database is the single source of truth" (plan section 6): a
    module-level cached handle would make an in-process object authoritative.
    Three assertions, each closing a more sophisticated defect than the last:

    1. ``first is not second`` — but this alone passes for fresh *wrapper* objects
       over one shared handle.
    2. ``type(first) is sqlite3.Connection`` (exact, not ``isinstance``) rejects a
       proxy interposed to share an underlying handle.
    3. the recursive module scan catches a keyed cache (``_POOL[path] = conn``),
       the natural-looking "just memoize per path" optimization, as well as a bare
       ``_CONN`` global.
    """
    db_path = tmp_path / "state.db"

    with opened_connection(db_path) as first, opened_connection(db_path) as second:
        assert first is not second
        assert type(first) is sqlite3.Connection, (
            "get_connection must return a real sqlite3.Connection, not a wrapper "
            "or proxy that could be sharing one underlying handle"
        )

    cached_connections = [
        value
        for value in module_level_values(sluice.core.db)
        if isinstance(value, sqlite3.Connection)
    ]

    assert cached_connections == [], (
        "sluice.core.db holds a connection at module level; the database, not an "
        "in-process handle, is the single source of truth"
    )


def test_db_imports_only_stdlib():
    """``sluice/core/db.py`` imports nothing outside the standard library.

    Core is the layer everything else may depend on, so it must depend on nothing
    of ours. Parsed with ``ast``, not inspected at runtime, because a conditional
    or function-local import — precisely how a dependency inversion gets smuggled
    in — would not show up in the module namespace.
    """
    source_path = inspect.getsourcefile(sluice.core.db)
    tree = ast.parse(pathlib.Path(source_path).read_text(encoding="utf-8"))

    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) is by definition inside sluice.
            if node.level:
                imported_roots.add("sluice")
            elif node.module:
                imported_roots.add(node.module.split(".")[0])

    non_stdlib = sorted(imported_roots - sys.stdlib_module_names)

    assert non_stdlib == [], f"{source_path} imports non-stdlib modules: {non_stdlib}"


# --------------------------------------------------------------------------
# Unit 3 — write_txn(db_path)
#
# ``write_txn`` enforces design of record §5's "short transactions only": it
# layers an explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` on unit 2's autocommit
# connection (fork F2), rolls back and re-raises on exception, and always closes
# in ``finally``.
#
# Because of that close, no test may read ``conn.in_transaction`` after the block
# (CPython raises ``ProgrammingError`` on a closed connection's getter). Lock
# release is evidenced by a fresh connection taking the write lock; the
# forgot-to-COMMIT case by ``test_write_txn_commits_on_clean_exit``. The testable
# open-transaction risk lives in the caller-supplied-connection primitives (units
# 9, 11, 13), where the handle is still open.
#
# DEFERRED FOR integration-tester (two writers): ``BEGIN IMMEDIATE`` vs plain
# ``BEGIN`` is indistinguishable single-process — a deferred txn still takes the
# write lock at its first write, so a plain-``BEGIN`` mutant survives all five
# unit tests. The property is only observable under concurrency: a deferred
# read-then-write can fail ``SQLITE_BUSY`` on lock upgrade (busy-timeout is not
# honored on the deadlock-avoidance upgrade path) while ``IMMEDIATE`` takes the
# lock up front. Until the integration test exists, ``BEGIN IMMEDIATE`` is
# enforced by review + docstring only.
#
# Real file under ``tmp_path`` every time, never ``:memory:``.
# --------------------------------------------------------------------------


def create_counter_table(db_path):
    """Create table ``t`` at ``db_path`` and commit it, before any test's block.

    Every ``write_txn`` test needs a durable pre-existing table so it observes the
    transaction's behavior, not the DDL's. ``get_connection``'s autocommit makes
    the ``CREATE TABLE`` durable at once.
    """
    with opened_connection(db_path) as connection:
        connection.execute("CREATE TABLE t (a)")


def count_rows(db_path) -> int:
    """Return ``SELECT COUNT(*) FROM t`` as seen by a brand-new connection.

    A new connection holds none of the writer's transaction state, so it reports
    what the file actually committed.
    """
    with opened_connection(db_path) as connection:
        return connection.execute("SELECT COUNT(*) FROM t").fetchone()[0]


def test_write_txn_commits_on_clean_exit(tmp_path):
    """Work done inside the block is durable once the block exits normally.

    Observed from a separate connection, so it reflects the file, not the writer's
    handle. Fails if the implementation opens ``BEGIN IMMEDIATE`` and never issues
    ``COMMIT``: the ``finally: close()`` then rolls back and the row vanishes.
    """
    db_path = tmp_path / "state.db"
    create_counter_table(db_path)

    with sluice.core.db.write_txn(db_path) as connection:
        connection.execute("INSERT INTO t (a) VALUES (1)")

    assert count_rows(db_path) == 1


def test_write_txn_rolls_back_on_exception(tmp_path):
    """An exception inside the block discards the writes *and* propagates.

    ``pytest.raises`` catches an implementation that swallows the exception; the
    count catches one that commits anyway — and the degenerate case that never
    opens a transaction (under autocommit an un-wrapped insert is already durable
    by the time the ``raise`` runs).
    """
    db_path = tmp_path / "state.db"
    create_counter_table(db_path)

    with pytest.raises(RuntimeError, match="boom"):
        with sluice.core.db.write_txn(db_path) as connection:
            connection.execute("INSERT INTO t (a) VALUES (1)")
            raise RuntimeError("boom")

    assert count_rows(db_path) == 0


def test_write_txn_closes_the_connection(tmp_path):
    """The yielded connection is closed by the time the block has exited.

    ``write_txn`` owns the connection, so leaking it leaks an fd per call (the
    daemon calls this every cycle). A closed connection raises ``ProgrammingError``
    on use — the observable difference between "closed" and "merely committed".
    """
    db_path = tmp_path / "state.db"
    create_counter_table(db_path)

    with sluice.core.db.write_txn(db_path) as connection:
        connection.execute("INSERT INTO t (a) VALUES (1)")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_write_txn_releases_the_write_lock(tmp_path):
    """Another writer can take the write lock immediately after the block.

    A write transaction that outlives its block starves every other writer (design
    of record §5). The probe sets ``busy_timeout=0`` so a still-held lock surfaces
    as an immediate ``OperationalError`` rather than a 5-second stall.
    """
    db_path = tmp_path / "state.db"
    create_counter_table(db_path)

    with sluice.core.db.write_txn(db_path) as connection:
        connection.execute("INSERT INTO t (a) VALUES (1)")

    with opened_connection(db_path) as probe:
        probe.execute("PRAGMA busy_timeout=0")
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")


def test_write_txn_does_not_block_concurrent_readers(tmp_path):
    """A reader opened while the block is mid-write is served, and sees the old value.

    The WAL guarantee the daemon depends on (design of record §5): it polls run
    state while a gate writes. The two halves fail differently — a non-WAL database
    makes the ``SELECT`` *raise* "database is locked", while a missing isolation
    boundary would let the reader see the writer's uncommitted row.
    """
    db_path = tmp_path / "state.db"
    create_counter_table(db_path)
    with opened_connection(db_path) as setup:
        setup.execute("INSERT INTO t (a) VALUES (0)")

    with sluice.core.db.write_txn(db_path) as connection:
        connection.execute("INSERT INTO t (a) VALUES (1)")

        with opened_connection(db_path) as reader:
            reader_count = reader.execute("SELECT COUNT(*) FROM t").fetchone()[0]

    assert reader_count == 1, "the reader must not observe the writer's uncommitted row"
    assert count_rows(db_path) == 2


# --------------------------------------------------------------------------
# Unit 4 — the migration framework.
#
# ``MIGRATIONS``, ``schema_version``, ``apply_migrations``,
# ``initialize_database``, ``SchemaVersionError`` (plan 01, unit 4). Symbols are
# reached through the module object so an unwritten name surfaces as
# ``AttributeError`` in the test, not an import-time failure.
#
# The framework is tested independently of the real migrations 001-004 (units
# 5-8): behaviours that need "a migration" use synthetic ones, so unit 4 never
# reaches forward. Real files under ``tmp_path``, never ``:memory:``.
# --------------------------------------------------------------------------


def test_schema_version_is_zero_on_virgin_database(tmp_path):
    """A freshly-opened database with no ledger reports schema version ``0``.

    A virgin file has no ``schema_migrations`` table, so ``schema_version`` must
    tolerate the missing ledger and report ``0``, not raise ``no such table``:
    ``apply_migrations`` asks this before it has created the ledger, on the exact
    database it is meant to bootstrap.
    """
    with opened_connection(tmp_path / "state.db") as connection:
        assert sluice.core.db.schema_version(connection) == 0


def _synthetic_migrations(count, *, table_prefix):
    """Build ``count`` contiguous synthetic migrations (versions ``1..count``).

    Each creates its own throwaway table, giving the engine real DDL to run
    without touching the production schema, so the engine tests exercise the
    engine rather than whatever ``MIGRATIONS`` holds today. Unit-4 tests
    monkeypatch these over ``sluice.core.db.MIGRATIONS``.
    """

    def _make(version):
        def _apply(connection):
            connection.execute(f"CREATE TABLE {table_prefix}_{version} (x INTEGER)")

        return (version, f"{table_prefix}_{version}", _apply)

    return tuple(_make(version) for version in range(1, count + 1))


def test_initialize_database_applies_all_migrations(tmp_path, monkeypatch):
    """``initialize_database`` runs every ``MIGRATIONS`` entry and lands at the top.

    Tests the engine via a monkeypatched synthetic ``MIGRATIONS``. Two halves: the
    return value names the versions newly applied, in order, and a *fresh*
    connection reads the ledger back at the highest version — reading from a new
    connection proves the work was committed, not staged in an open transaction.
    """
    synthetic = _synthetic_migrations(3, table_prefix="applies")
    monkeypatch.setattr(sluice.core.db, "MIGRATIONS", synthetic)
    db_path = tmp_path / "state.db"

    applied = sluice.core.db.initialize_database(db_path)

    expected_versions = [version for version, _name, _fn in synthetic]
    assert applied == expected_versions
    with opened_connection(db_path) as connection:
        assert sluice.core.db.schema_version(connection) == max(expected_versions)


def test_initialize_database_is_idempotent(tmp_path, monkeypatch):
    """A second ``initialize_database`` on the same path applies nothing.

    Tests the engine via a monkeypatched synthetic ``MIGRATIONS``. Run twice: the
    first call returns every version, the second returns ``[]``, and
    ``schema_migrations`` holds exactly ``len(MIGRATIONS)`` rows after each — so a
    runner that re-applies on the second pass is caught by the count even if it
    returned ``[]``.
    """
    synthetic = _synthetic_migrations(3, table_prefix="idem")
    monkeypatch.setattr(sluice.core.db, "MIGRATIONS", synthetic)
    db_path = tmp_path / "state.db"

    first = sluice.core.db.initialize_database(db_path)
    with opened_connection(db_path) as connection:
        count_after_first = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

    second = sluice.core.db.initialize_database(db_path)
    with opened_connection(db_path) as connection:
        count_after_second = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

    migration_count = len(synthetic)
    assert first == [version for version, _name, _fn in synthetic]
    assert second == [], "the second call must apply nothing"
    assert count_after_first == migration_count
    assert count_after_second == migration_count


def test_migration_versions_are_contiguous_and_unique(tmp_path):
    """``MIGRATIONS`` versions are exactly ``1..N`` with no gap or repeat.

    A structural guard on the tuple, stopping two branches both shipping "version
    005" (or one skipping a number): the ledger keys on ``version``, so a duplicate
    fails to insert and a gap breaks the contiguity the runner relies on.
    ``tmp_path`` is unused but kept to match its siblings' signatures.
    """
    versions = [version for version, _name, _fn in sluice.core.db.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))


def test_apply_migrations_refuses_a_newer_schema(tmp_path):
    """A database written by a newer sluice is refused, never silently downgraded.

    A hand-inserted ledger row for version ``999`` simulates a DB a future sluice
    already migrated. ``apply_migrations`` must raise ``SchemaVersionError`` naming
    the version, not treat the unknown future version as "nothing to do" and run
    today's code against a schema it does not understand.
    """
    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    with opened_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (999, "written_by_a_newer_sluice", now_iso()),
        )
        with pytest.raises(sluice.core.db.SchemaVersionError) as excinfo:
            sluice.core.db.apply_migrations(connection)

    assert "999" in str(excinfo.value)


def test_failed_migration_does_not_record_its_version(tmp_path, monkeypatch):
    """A migration that raises records nothing; earlier successes survive.

    Monkeypatched ladder: two good fixture migrations then one whose body raises.
    ``apply_migrations`` must propagate the failure — the point of this framework
    over python-poller's blanket ``except OperationalError: pass`` — and, read
    back from a fresh connection, the ledger shows the last good version.

    That fresh read is load-bearing twice: it proves the good migrations were
    committed per-migration (a single wrapping transaction would roll them back to
    ``0``) and that the failing version was never recorded (catching a runner that
    stamps the ledger before running the body). ``SchemaVersionError`` subclasses
    ``RuntimeError``, so asserting ``RuntimeError`` accepts a raw or wrapped raise.
    """

    class _MigrationBoom(RuntimeError):
        """Distinctive failure raised by the synthetic migration under test."""

    def _explode(_connection):
        raise _MigrationBoom("synthetic migration failure")

    good = _synthetic_migrations(2, table_prefix="failboom")
    last_good_version = max(version for version, _name, _fn in good)
    failing_version = last_good_version + 1
    monkeypatch.setattr(
        sluice.core.db,
        "MIGRATIONS",
        good + ((failing_version, "explode", _explode),),
    )

    db_path = tmp_path / "state.db"
    with opened_connection(db_path) as connection:
        with pytest.raises(RuntimeError):
            sluice.core.db.apply_migrations(connection)

    with opened_connection(db_path) as verifier:
        assert sluice.core.db.schema_version(verifier) == last_good_version
        rows_for_failed = verifier.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (failing_version,),
        ).fetchone()[0]
    assert rows_for_failed == 0


def test_initialize_database_does_not_nest_transactions(tmp_path):
    """``initialize_database`` on a fresh path completes without an OperationalError.

    Encodes the fork F2 nesting rule (plan 01, section 3): ``apply_migrations``
    opens its own ``BEGIN IMMEDIATE`` per migration, so wrapping it in ``write_txn``
    would make the inner ``BEGIN`` raise ``cannot start a transaction within a
    transaction``. Asserts only no-raise — not ``write_txn`` call counts, which
    would fail a valid re-entrant refactor the no-raise assertion already admits.
    """
    db_path = tmp_path / "state.db"
    try:
        sluice.core.db.initialize_database(db_path)
    except sqlite3.OperationalError as exc:  # pragma: no cover - failure path
        pytest.fail(f"initialize_database nested a transaction: {exc}")


def test_db_defines_no_default_database_path(tmp_path):
    """No module-level string constant in ``db`` names a ``.db``/``.sqlite`` path.

    Guardrail (plan 01, section 8): a missing database path is a caller error, not
    grounds for a baked-in production fallback. Dunders are excluded (``__name__``
    is ``"sluice.core.db"``), so this catches only a real constant such as
    ``DEFAULT_DB_PATH = "/var/lib/sluice/state.db"``.
    """
    offenders = {
        name: value
        for name, value in vars(sluice.core.db).items()
        if not name.startswith("__")
        and isinstance(value, str)
        and (value.endswith(".db") or value.endswith(".sqlite"))
    }
    assert offenders == {}


# --------------------------------------------------------------------------
# Units 5-8 — migrations 001-004: the real schema.
#
# These tests reach the schema the only sanctioned way: ``initialize_database``
# (which applies ``MIGRATIONS``) against a real file under ``tmp_path``, never
# ``:memory:`` (WAL is silently not applied in memory). Schema symbols are
# reached through the module object so a missing table/column surfaces in the
# test, not at import time.
#
# Presence is asserted as a SUBSET (``expected <= actual``), never set equality:
# later subsystems add columns by migration (005's mock-gate columns, Plan B's
# gate columns), and equality would force edits to shipped-migration tests --
# against the "never edit a shipped migration" discipline. Meaning is carried by
# the ABSENCE tests, which do not churn. (``control_audit`` is the exception: no
# subsystem adds audit columns, so equality there is a deliberate guard, unit 7.)
# --------------------------------------------------------------------------


# The column set migration 001 must create (plan 01, section 4). Asserted as a
# subset of the live schema, so it never churns when a later migration appends.
RUNS_001_COLUMNS = {
    "name",
    "experiment_id",
    "size_tb",
    "detected_at",
    "pipeline_version",
    "completed_at",
    "lifecycle",
    "lifecycle_by",
    "lifecycle_at",
    "lifecycle_reason",
    "error_reason",
    "errored_at",
    "run_from_step",
    "run_from_fresh",
    "protected_at",
    "protected_by",
    "globus_task_id",
    "slack_thread_ts",
}

# The six production gate columns (6-vs-8 question belongs to Plan B / errata B1),
# which must NOT appear in ``runs`` until a later migration adds them.
PRODUCTION_GATE_COLUMNS = {
    "download_complete",
    "checksum_ok",
    "merged",
    "nvd_done",
    "transferred",
    "registered",
}


@contextlib.contextmanager
def initialized_connection(db_path):
    """Apply ``MIGRATIONS`` to a fresh ``db_path`` and yield an open connection.

    Reaches the schema through ``initialize_database``, not test DDL, so what is
    asserted is the schema the migrations build. The read connection is a separate
    handle, so nothing is observed through a still-open transaction.
    """
    sluice.core.db.initialize_database(db_path)
    with opened_connection(db_path) as connection:
        yield connection


def runs_columns(connection):
    """Return the set of column names on the ``runs`` table.

    ``PRAGMA table_info`` returns zero rows (not an error) for a missing table, so
    presence tests built on this fail cleanly while the table is absent.
    """
    return {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}


def insert_minimal_run(connection, name="r1", *, pipeline_version="reference-v1"):
    """Insert the minimal valid ``runs`` row: the two NOT NULL columns plus name.

    ``detected_at`` and ``pipeline_version`` are the only NOT NULL columns without
    a default, so this is the smallest row the schema accepts.
    """
    connection.execute(
        "INSERT INTO runs (name, detected_at, pipeline_version) VALUES (?, ?, ?)",
        (name, now_iso(), pipeline_version),
    )


# ---- Unit 5 — migration 001: runs -----------------------------------------


def test_runs_table_has_the_001_columns(tmp_path):
    """Every column named in section 4's ``runs`` DDL is present.

    Asserted as a subset, not set equality: migration 005 and Plan B's gate columns
    each ``ADD COLUMN`` more, and equality would force an edit to this
    shipped-migration test on every additive migration.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        actual = runs_columns(connection)

    missing = RUNS_001_COLUMNS - actual
    assert missing == set(), f"migration 001 is missing columns: {sorted(missing)}"


def test_runs_table_has_no_locked_at_column(tmp_path):
    """``runs`` has no ``locked_at`` column (errata A3).

    python-poller's ``locked_at`` per-run mutex is deliberately not ported:
    single-daemon ``fcntl.flock`` makes it redundant, it has no stale-lock reaper
    (a SIGKILL wedges a run forever), and reusing it for operator protection would
    collide with in-flight processing. Protection is the net-new
    ``protected_at``/``protected_by`` instead. Kills a well-meaning re-port.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        actual = runs_columns(connection)

    assert "name" in actual, "runs table must exist for this guard to be meaningful"
    assert "locked_at" not in actual
    assert {"protected_at", "protected_by"} <= actual, (
        "operator protection columns must be present in their place"
    )


def test_no_production_gate_columns_yet(tmp_path):
    """``runs`` carries none of the six production gate columns yet (errata B1).

    The 6-vs-8-gate decision is Plan B's; those columns arrive by a later additive
    migration, never by editing 001. Fails loudly if someone pre-empts it here.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        actual = runs_columns(connection)

    assert "name" in actual, "runs table must exist for this guard to be meaningful"
    leaked = actual & PRODUCTION_GATE_COLUMNS
    assert leaked == set(), (
        f"production gate columns pre-empted Plan B: {sorted(leaked)}"
    )


def test_runs_name_is_primary_key(tmp_path):
    """``name`` is the primary key: it is flagged ``pk`` and rejects a duplicate.

    Fork F1 — run identity is the human-typed name, no surrogate integer id.
    Asserted two ways: ``PRAGMA table_info`` shows the declared key, and a
    duplicate insert raising ``IntegrityError`` proves uniqueness is enforced.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        pk_flag = {
            row["name"]: row["pk"]
            for row in connection.execute("PRAGMA table_info(runs)")
        }
        assert pk_flag["name"] == 1

        insert_minimal_run(connection, "dup")
        with pytest.raises(sqlite3.IntegrityError):
            insert_minimal_run(connection, "dup")


def test_lifecycle_defaults_to_active(tmp_path):
    """A run inserted without a ``lifecycle`` reads back as ``'active'``.

    ``lifecycle`` is greenfield (errata A2): ``NOT NULL DEFAULT 'active'``. A run is
    operator-eligible when active, so the default starts a freshly-detected run
    eligible without the caller stating it.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_minimal_run(connection, "fresh")
        row = connection.execute(
            "SELECT lifecycle FROM runs WHERE name = ?", ("fresh",)
        ).fetchone()

    assert row["lifecycle"] == "active"


def test_lifecycle_rejects_an_unknown_value(tmp_path):
    """Inserting a ``lifecycle`` outside the allowed set raises ``IntegrityError``.

    ``CHECK (lifecycle IN ('active', 'held', 'skipped'))`` (errata A2). Without it,
    a stale writer could park a run in a lifecycle the eligibility predicate and
    control ops never named, making it invisible to both. Kills a drop-CHECK mutant.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO runs (name, detected_at, pipeline_version, lifecycle) "
                "VALUES (?, ?, ?, ?)",
                ("bogus", now_iso(), "reference-v1", "paused"),
            )


def test_error_columns_must_be_set_together(tmp_path):
    """Quarantine is all-or-nothing: ``error_reason`` and ``errored_at`` move together.

    ``CHECK ((error_reason IS NULL) = (errored_at IS NULL))`` encodes design section
    5's "quarantined iff error set". Setting one alone must raise; setting or
    clearing both must succeed — a half-written quarantine would hide or surface a
    run inconsistently.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_minimal_run(connection, "q")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE runs SET errored_at = ? WHERE name = ?", (now_iso(), "q")
            )

        connection.execute(
            "UPDATE runs SET errored_at = ?, error_reason = ? WHERE name = ?",
            (now_iso(), "boom", "q"),
        )
        connection.execute(
            "UPDATE runs SET errored_at = NULL, error_reason = NULL WHERE name = ?",
            ("q",),
        )


def test_protected_columns_must_be_set_together(tmp_path):
    """Operator protection is all-or-nothing: ``protected_at``/``protected_by`` pair.

    ``CHECK ((protected_at IS NULL) = (protected_by IS NULL))``. The pairing makes
    ``lock``/``unlock`` (subsystem 8) write a coherent record; either column alone
    would be an audit hole.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_minimal_run(connection, "p")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE runs SET protected_at = ? WHERE name = ?", (now_iso(), "p")
            )

        connection.execute(
            "UPDATE runs SET protected_at = ?, protected_by = ? WHERE name = ?",
            (now_iso(), "operator", "p"),
        )
        connection.execute(
            "UPDATE runs SET protected_at = NULL, protected_by = NULL WHERE name = ?",
            ("p",),
        )


def test_optional_identity_and_resume_columns_default_null(tmp_path):
    """A minimal insert leaves every optional column NULL, resume tokens included.

    Covers B2 (``experiment_id`` nullable — the chicken-and-egg is Plan B's) and A4
    (``globus_task_id``/``slack_thread_ts`` exist from day one though Plan A never
    writes them: their absence is what makes a restart re-submit a duplicate
    multi-TB transfer, so they must be present and default NULL for Plan B).
    """
    optional_columns = [
        "experiment_id",
        "size_tb",
        "completed_at",
        "globus_task_id",
        "slack_thread_ts",
        "run_from_step",
        "run_from_fresh",
        "protected_at",
        "protected_by",
    ]
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_minimal_run(connection, "minimal")
        row = connection.execute(
            "SELECT * FROM runs WHERE name = ?", ("minimal",)
        ).fetchone()

    for column in optional_columns:
        assert row[column] is None, f"{column} must default to NULL"


# ---- Unit 6 — migration 002: poller_metadata ------------------------------


def test_poller_metadata_is_a_key_value_table(tmp_path):
    """``poller_metadata`` is exactly ``key`` (primary key) and ``value``.

    A key/value store for global daemon state. ``db`` creates only the shape; it
    knows no key names (that policy is control/daemon's).
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        info = list(connection.execute("PRAGMA table_info(poller_metadata)"))

    names = {row["name"] for row in info}
    pk_flag = {row["name"]: row["pk"] for row in info}
    assert names == {"key", "value"}
    assert pk_flag["key"] == 1


def test_poller_metadata_upsert_replaces_value(tmp_path):
    """``key`` being the primary key lets an upsert replace a value in place.

    Two ``INSERT ... ON CONFLICT(key) DO UPDATE`` on one key leave exactly one row
    holding the second value — the behaviour ``set_metadata`` (unit 13) is built
    on. A non-PK ``key`` would let the second insert duplicate instead.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        for value in ("0", "1"):
            connection.execute(
                "INSERT INTO poller_metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("paused", value),
            )
        rows = list(connection.execute("SELECT key, value FROM poller_metadata"))

    assert len(rows) == 1
    assert rows[0]["value"] == "1"


# ---- Unit 7 — migration 003: control_audit --------------------------------


def test_control_audit_has_the_audit_columns(tmp_path):
    """``control_audit`` has exactly the seven audit columns.

    Set equality is deliberate here (unlike ``runs``): no subsystem adds audit
    columns, so a change to this set is a real design change that should surface.
    ``db`` creates the table; writing rows is subsystem 7.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        names = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(control_audit)")
        }

    assert names == {"id", "ts", "actor", "action", "target_run", "args_json", "result"}


def test_control_audit_requires_ts_actor_action_and_allows_null_target(tmp_path):
    """``ts``/``actor``/``action`` are NOT NULL; ``target_run`` is nullable.

    Every audit row records when, who and what. ``target_run`` is nullable because
    global ops like ``pause``/``resume`` have no target run; forcing one would
    fabricate a target or block auditing the op.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        for null_column in ("ts", "actor", "action"):
            values = {"ts": now_iso(), "actor": "operator", "action": "pause"}
            values[null_column] = None
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO control_audit (ts, actor, action) "
                    "VALUES (:ts, :actor, :action)",
                    values,
                )

        # A global op with no target run is a valid, auditable row.
        connection.execute(
            "INSERT INTO control_audit (ts, actor, action, target_run) "
            "VALUES (?, ?, ?, NULL)",
            (now_iso(), "operator", "pause"),
        )


def test_control_audit_id_autoincrements_without_reusing_deleted_ids(tmp_path):
    """``id`` is monotonic and never reuses a deleted row's value (AUTOINCREMENT).

    ``INTEGER PRIMARY KEY AUTOINCREMENT`` (design section 4) so audit ids strictly
    increase and are never recycled — ordering the trail cannot rely on wall-clock
    ``ts`` (an NTP step moves it backwards). A plain ``INTEGER PRIMARY KEY`` mutant
    reuses a deleted tail row's id, so delete-highest-then-insert must still yield
    a larger id.
    """
    with initialized_connection(tmp_path / "state.db") as connection:

        def append_audit_row():
            cursor = connection.execute(
                "INSERT INTO control_audit (ts, actor, action) VALUES (?, ?, ?)",
                (now_iso(), "operator", "pause"),
            )
            return cursor.lastrowid

        first = append_audit_row()
        second = append_audit_row()
        assert second > first

        connection.execute("DELETE FROM control_audit WHERE id = ?", (second,))
        third = append_audit_row()

    assert third > second, "AUTOINCREMENT must not reuse the id of a deleted row"


# ---- Unit 8 — migration 004: idx_runs_eligible ----------------------------


def test_eligibility_index_covers_lifecycle_and_errored_at(tmp_path):
    """The eligibility index covers ``(lifecycle, errored_at)``, in that order.

    Asserts the indexed columns, not the index name, so a wrong column, wrong
    order, or missing index is caught. Supports the two-clause eligibility
    predicate ``lifecycle = 'active' AND errored_at IS NULL`` (fork F4).
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        indexed_columns = [
            row["name"]
            for row in connection.execute("PRAGMA index_info(idx_runs_eligible)")
        ]

    assert indexed_columns == ["lifecycle", "errored_at"]


def test_no_index_covers_detected_at(tmp_path):
    """No index on ``runs`` covers ``detected_at`` (subsystem map line 571).

    python-poller's ``idx_folders_detected`` indexes a column that is ``1`` for
    every row — useless. Checked by each index's columns via ``PRAGMA index_info``,
    not by name, so ``CREATE INDEX idx_whatever ON runs(detected_at)`` cannot slip
    through under an innocent name.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        indexes = [row["name"] for row in connection.execute("PRAGMA index_list(runs)")]
        assert indexes, "runs must exist and carry at least its primary-key index"
        for index_name in indexes:
            covered = [
                row["name"]
                for row in connection.execute(f"PRAGMA index_info({index_name})")
            ]
            assert "detected_at" not in covered, (
                f"index {index_name} covers detected_at, a constant-value column"
            )


def test_index_migration_applies_to_a_pre_existing_database(tmp_path, monkeypatch):
    """Migration 004 lands its index on a database already holding rows.

    The "additive migration on live data" proof that migration 005 and Plan B's
    gate columns depend on. Initialise with ``MIGRATIONS`` truncated to 001-003,
    insert a run, apply the full ladder: the index must exist and the pre-existing
    row be untouched.
    """
    full_migrations = sluice.core.db.MIGRATIONS
    db_path = tmp_path / "state.db"

    monkeypatch.setattr(sluice.core.db, "MIGRATIONS", full_migrations[:3])
    sluice.core.db.initialize_database(db_path)
    with opened_connection(db_path) as connection:
        insert_minimal_run(connection, "preexisting", pipeline_version="reference-v1")

    monkeypatch.setattr(sluice.core.db, "MIGRATIONS", full_migrations)
    sluice.core.db.initialize_database(db_path)

    with opened_connection(db_path) as connection:
        indexes = {row["name"] for row in connection.execute("PRAGMA index_list(runs)")}
        assert "idx_runs_eligible" in indexes
        row = connection.execute(
            "SELECT pipeline_version FROM runs WHERE name = ?", ("preexisting",)
        ).fetchone()
        assert row["pipeline_version"] == "reference-v1"


def test_all_migrations_yield_the_top_version_and_every_object(tmp_path):
    """The full ladder's end state: head version, all tables and the index present.

    Migration 005 (mock gate columns) is subsystem 2's, appended per plan 02
    §3.5; 006 (``excluded_gates``) is subsystem 8's, appended per plan 08
    unit 9 (⚑2); 007 (``labkey_registered``) is Plan B's ``experiment`` gate,
    appended per plan 13 Unit 1 (⚑3 — incremental, one satisfaction column per
    gate). The end-state literal stays hardcoded — generalizing to
    ``max(MIGRATIONS)`` would pin nothing; this test is edited consciously each
    time the ladder grows. ``initialize_database`` on a fresh path reaches
    ``schema_version`` at head with ``schema_migrations``, ``runs``,
    ``poller_metadata``, ``control_audit`` and ``idx_runs_eligible`` all created.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        assert sluice.core.db.schema_version(connection) == len(
            sluice.core.db.MIGRATIONS
        )

        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "schema_migrations",
            "runs",
            "poller_metadata",
            "control_audit",
        } <= tables

        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_runs_eligible" in indexes


# The production satisfaction columns that have NOT shipped yet (⚑3): each
# arrives with its own gate in its own migration, never bundled ahead of it. This
# set is the incremental-migration guard, and it SHRINKS by one entry each time a
# gate lands — a name still listed here must not exist in the schema. Names track
# plan 13 §6's carry-forward, distinct from the older placeholder set in
# ``PRODUCTION_GATE_COLUMNS`` above.
#
# Shipped so far: ``labkey_registered`` (007, experiment gate), ``downloaded``
# (008, download gate).
UNSHIPPED_PRODUCTION_SATISFACTION_COLUMNS = {
    "checksummed",
    "samplesheet_created",
    "authority_registered",
    "nvd_completed",
    "globus_submitted",
    "clumped_data_backed_up",
}


def test_migration_007_adds_labkey_registered_column(tmp_path):
    """Migration 007 adds ONLY ``labkey_registered`` as ``INTEGER NOT NULL DEFAULT 0``.

    Plan 13 Unit 1 (⚑3 — incremental): the ``experiment`` gate's satisfaction
    column lands with its gate, and the other production satisfaction columns do
    NOT — they each ship with their own gate/migration (008+). ``experiment_id``
    is not asserted here: it already exists from migration 001 (ADR-0006). The
    ``NOT NULL DEFAULT 0`` shape keeps reconcile's ``WHERE col = 0`` out of
    three-valued logic and the ALTER legal on a populated table.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        info = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(runs)")
        }

    assert "labkey_registered" in info, "migration 007 did not add the column"
    column = info["labkey_registered"]
    assert column["type"] == "INTEGER"
    assert column["notnull"] == 1
    assert column["dflt_value"] == "0"

    # The incremental-migration guard: no gate column has landed ahead of the
    # gate that owns it.
    leaked = set(info) & UNSHIPPED_PRODUCTION_SATISFACTION_COLUMNS
    assert leaked == set(), (
        "a production satisfaction column landed ahead of its gate; each ships "
        f"with its own migration (⚑3). Leaked: {sorted(leaked)}"
    )


# --------------------------------------------------------------------------
# Units 9-11 — the ``runs`` row primitives: insert_run, get_run, update_run.
#
# Each primitive takes a caller-supplied connection and issues single autocommit
# statements; the caller decides whether to batch several inside one
# ``write_txn``. Symbols reached through the module object; real files under
# ``tmp_path``, never ``:memory:``.
# --------------------------------------------------------------------------


# ---- Unit 9 — insert_run --------------------------------------------------


def test_insert_run_creates_an_active_row(tmp_path):
    """A first ``insert_run`` returns ``True`` and lands an active, stamped row.

    Detection is "stamp once": the row starts ``lifecycle = 'active'``, carries the
    caller's ``pipeline_version`` verbatim, and has a non-null ``detected_at``
    (filled from ``now_iso()`` when omitted). ``True`` is the "a row was inserted"
    signal distinguishing a new detection from a re-detection.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        inserted = sluice.core.db.insert_run(
            connection, "run_alpha", pipeline_version="reference-v1"
        )

        assert inserted is True
        row = connection.execute(
            "SELECT lifecycle, pipeline_version, detected_at FROM runs WHERE name = ?",
            ("run_alpha",),
        ).fetchone()

    assert row["lifecycle"] == "active"
    assert row["pipeline_version"] == "reference-v1"
    assert row["detected_at"] is not None


def test_insert_run_is_idempotent(tmp_path):
    """A second ``insert_run`` of the same name returns ``False`` and adds no row.

    Detection re-runs every cycle, so re-inserting a known run must be a quiet
    no-op. ``False`` is the "already existed" signal (``ON CONFLICT(name) DO
    NOTHING`` left ``rowcount`` at zero); the count staying at 1 proves nothing was
    duplicated.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        first = sluice.core.db.insert_run(
            connection, "run_beta", pipeline_version="reference-v1"
        )
        second = sluice.core.db.insert_run(
            connection, "run_beta", pipeline_version="reference-v1"
        )
        count = connection.execute(
            "SELECT COUNT(*) FROM runs WHERE name = ?", ("run_beta",)
        ).fetchone()[0]

    assert first is True
    assert second is False, "a re-detection must report the run already existed"
    assert count == 1


def test_insert_run_never_restamps_pipeline_version(tmp_path):
    """Re-detecting a run does not overwrite its frozen ``pipeline_version``.

    Freezes design section 8's "stamped at detection and never changes": a run is
    reconciled against the version it was born with. ``ON CONFLICT DO NOTHING``
    delivers this (the second insert touches no columns); a ``DO UPDATE`` upsert
    would silently re-stamp, which this catches.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        sluice.core.db.insert_run(connection, "run_gamma", pipeline_version="v1")
        sluice.core.db.insert_run(connection, "run_gamma", pipeline_version="v2")
        row = connection.execute(
            "SELECT pipeline_version FROM runs WHERE name = ?", ("run_gamma",)
        ).fetchone()

    assert row["pipeline_version"] == "v1"


def test_insert_run_requires_pipeline_version(tmp_path):
    """``pipeline_version`` is keyword-only with no default; omitting it is a TypeError.

    Guardrail (plan 01, section 8): no baked-in production default for the profile
    id. It must be supplied explicitly at detection, so omitting it is a
    programming error that must fail loudly rather than stamp some default.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        with pytest.raises(TypeError):
            sluice.core.db.insert_run(connection, "run_delta")


def test_insert_run_propagates_a_constraint_violation(tmp_path):
    """A row that violates a constraint raises; it is not swallowed as "exists".

    Why ``insert_run`` uses ``ON CONFLICT(name) DO NOTHING`` not ``INSERT OR
    IGNORE``: ``OR IGNORE`` suppresses *every* constraint, so a NULL
    ``pipeline_version`` would be discarded and return the "already exists" signal,
    conflating invalid with duplicate. ``DO NOTHING`` suppresses only the PK
    conflict and lets the NOT NULL violation raise. Kills the ``OR IGNORE`` mutant.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        with pytest.raises(sqlite3.IntegrityError):
            sluice.core.db.insert_run(connection, "run_epsilon", pipeline_version=None)


# ---- Unit 10 — get_run ----------------------------------------------------


def test_get_run_returns_a_mapping_row(tmp_path):
    """``get_run`` returns the run as a column-keyed row.

    Consumers read by column name, so the row must be the ``sqlite3.Row`` mapping,
    not a bare tuple. Seeded with the raw helper, not ``insert_run``, so it fails
    only for ``get_run`` reasons.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_minimal_run(connection, "run_zeta")
        row = sluice.core.db.get_run(connection, "run_zeta")

        assert row["name"] == "run_zeta"
        assert row["lifecycle"] == "active"


def test_get_run_returns_none_for_unknown_name(tmp_path):
    """A miss returns ``None`` — not ``{}``, not an empty row, not an exception.

    Callers branch on ``is None``. An empty container or a raise would break the
    natural ``if get_run(...) is None:`` guard. Pins the miss contract.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        result = sluice.core.db.get_run(connection, "no_such_run")

    assert result is None


# ---- Unit 11 — update_run + UnknownColumnError -----------------------------


def test_update_run_writes_named_columns(tmp_path):
    """``update_run`` sets the named columns and returns ``True`` on a real update.

    The happy path: a mapping of column → value for an existing run lands, readable
    back by name. ``True`` is the "a row was updated" signal. This does not prove
    values are bound rather than string-formatted (both ``1234`` and ``2.5`` format
    into valid SQL) — that is
    ``test_update_run_stores_a_value_containing_sql_literally``'s job.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_minimal_run(connection, "run_eta")

        updated = sluice.core.db.update_run(
            connection, "run_eta", {"experiment_id": 1234, "size_tb": 2.5}
        )

        assert updated is True
        row = connection.execute(
            "SELECT experiment_id, size_tb FROM runs WHERE name = ?", ("run_eta",)
        ).fetchone()

    assert row["experiment_id"] == 1234
    assert row["size_tb"] == 2.5


def test_update_run_rejects_an_unknown_column(tmp_path):
    """An unwhitelisted column name raises ``UnknownColumnError``, naming the column.

    The critical security property: column identifiers cannot be bound as SQL
    parameters, so an unvalidated column name is an injection vector.
    ``update_run`` validates every key against the live ``runs`` columns and refuses
    anything else with ``UnknownColumnError`` — not a silent no-op, not a leaked
    ``OperationalError``. The message must carry the offending name.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_minimal_run(connection, "run_theta")

        with pytest.raises(sluice.core.db.UnknownColumnError) as excinfo:
            sluice.core.db.update_run(connection, "run_theta", {"drop_table": 1})

    assert "drop_table" in str(excinfo.value)


def test_update_run_rejects_sql_in_a_column_name(tmp_path):
    """A column name carrying SQL is rejected by the whitelist; ``runs`` survives.

    The injection made concrete: a column key of ``"name = 'x'; DROP TABLE runs;
    --"`` must be refused with ``UnknownColumnError``, and ``runs`` must still
    exist. An implementation that interpolates the column name would execute the
    piggybacked ``DROP TABLE``.
    """
    malicious_column = "name = 'x'; DROP TABLE runs; --"
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_minimal_run(connection, "run_iota")

        with pytest.raises(sluice.core.db.UnknownColumnError):
            sluice.core.db.update_run(connection, "run_iota", {malicious_column: 1})

        runs_still_present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchone()

    assert runs_still_present is not None, "the runs table must not have been dropped"


def test_update_run_stores_a_value_containing_sql_literally(tmp_path):
    """A value containing SQL syntax is stored verbatim, proving values are bound.

    The column is whitelisted, but the *value* must reach SQLite as a bound
    parameter. Setting ``lifecycle_reason`` (plain TEXT) to a string that is itself
    valid SQL and reading it back byte-for-byte proves binding: a mutant that
    interpolates the value would raise on the unbalanced quote or execute the
    injection, and ``runs`` would not survive. The round-trip plus the surviving
    table kill that value-interpolation mutant.
    """
    injection = "'; DROP TABLE runs; --"
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_minimal_run(connection, "run_kappa")

        sluice.core.db.update_run(
            connection, "run_kappa", {"lifecycle_reason": injection}
        )

        row = connection.execute(
            "SELECT lifecycle_reason FROM runs WHERE name = ?", ("run_kappa",)
        ).fetchone()
        runs_still_present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchone()

    assert runs_still_present is not None, "the runs table must not have been dropped"
    assert row["lifecycle_reason"] == injection


def test_update_run_returns_false_for_unknown_run(tmp_path):
    """Updating a run that does not exist changes nothing and returns ``False``.

    When no row matches, the ``UPDATE`` affects zero rows and the primitive reports
    ``False`` rather than raising, letting callers tell "updated" from "no such run"
    without a prior existence probe.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        updated = sluice.core.db.update_run(
            connection, "ghost_run", {"experiment_id": 1}
        )

    assert updated is False


def test_update_run_honours_check_constraints(tmp_path):
    """``update_run`` is a primitive, not a policy: the schema's CHECKs still bite.

    ``update_run`` just binds and writes, so the ``runs`` CHECK ``((error_reason IS
    NULL) = (errored_at IS NULL))`` enforces coherent quarantine. Setting
    ``errored_at`` alone must raise ``IntegrityError`` (the update reaches the
    database, not filtered by the primitive); setting both in one call must succeed.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_minimal_run(connection, "run_lambda")

        with pytest.raises(sqlite3.IntegrityError):
            sluice.core.db.update_run(
                connection, "run_lambda", {"errored_at": now_iso()}
            )

        updated = sluice.core.db.update_run(
            connection,
            "run_lambda",
            {"errored_at": now_iso(), "error_reason": "boom"},
        )

    assert updated is True


# ---- Shared short-transaction guard (units 9, 11; unit 13 adds set_metadata) ----


def _exercise_insert_run(connection):
    """Perform an ``insert_run`` write; return the coordinates to verify it.

    Returns ``(table, key_column, key_value, value_column, expected)`` — the
    coordinates a second connection uses to confirm the write committed, not left
    in an open transaction on this handle.
    """
    inserted = sluice.core.db.insert_run(
        connection, "txn_probe", pipeline_version="reference-v1"
    )
    assert inserted is True
    return "runs", "name", "txn_probe", "pipeline_version", "reference-v1"


def _exercise_update_run(connection):
    """Perform an ``update_run`` write; return the coordinates to verify it.

    Seeds the row with the raw helper first (committed under autocommit), then
    exercises ``update_run`` — the primitive under test here.
    """
    insert_minimal_run(connection, "txn_probe")
    updated = sluice.core.db.update_run(
        connection, "txn_probe", {"experiment_id": 4321}
    )
    assert updated is True
    return "runs", "name", "txn_probe", "experiment_id", 4321


def _exercise_set_metadata(connection):
    """Perform a ``set_metadata`` write; return the coordinates to verify it.

    The unit-13 addition to the shared guard. ``set_metadata`` writes to
    ``poller_metadata``, not ``runs`` — which is why the guard verifies against a
    caller-supplied coordinate tuple rather than assuming the ``runs`` table.
    """
    sluice.core.db.set_metadata(connection, "txn_probe_key", "txn_probe_value")
    return "poller_metadata", "key", "txn_probe_key", "value", "txn_probe_value"


@pytest.mark.parametrize(
    "exercise",
    [_exercise_insert_run, _exercise_update_run, _exercise_set_metadata],
    ids=["insert_run", "update_run", "set_metadata"],
)
def test_row_primitives_leave_no_open_transaction(tmp_path, exercise):
    """A row primitive commits its write and leaves no open transaction behind.

    The testable short-transaction guard design of record §5 requires (unit 3's
    ``write_txn`` tests deferred it here, where the handle is still open). Each
    primitive runs on an autocommit ``get_connection`` handle, so a correct single
    statement commits itself. Two checks: ``in_transaction is False`` catches a
    left-open transaction, and the second-connection read (committed state only)
    catches a write that never reached the file. Together they kill a "``BEGIN``
    without ``COMMIT``" mutant.
    """
    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    with opened_connection(db_path) as connection:
        table, key_column, key_value, value_column, expected = exercise(connection)
        assert connection.in_transaction is False, (
            "the primitive left a transaction open on the caller's connection"
        )

    with opened_connection(db_path) as verifier:
        row = verifier.execute(
            f"SELECT {value_column} FROM {table} WHERE {key_column} = ?", (key_value,)
        ).fetchone()

    assert row is not None, (
        "the write was not committed — a second connection cannot see it"
    )
    assert row[value_column] == expected


# --------------------------------------------------------------------------
# Unit 12 — select_active_runs + the eligibility predicate (fork F4).
#
# The predicate reconcile depends on: ``lifecycle = 'active' AND errored_at IS
# NULL`` (constant ``ACTIVE_RUNS_WHERE``) — two AND-ed orthogonal clauses that
# deliberately do NOT filter ``completed_at`` (F4). Each exclusion gets its own
# test so a dropped clause is caught, and orthogonality is pinned by the pair of
# "clearing one condition alone is not enough" tests. Real files under
# ``tmp_path``, never ``:memory:``.
# --------------------------------------------------------------------------


def insert_run_row(
    connection,
    name,
    *,
    detected_at=None,
    lifecycle="active",
    errored=False,
    completed=False,
):
    """Insert a ``runs`` row with explicit lifecycle / error / completion state.

    The eligibility tests drive the three dimensions the predicate reads
    (lifecycle, the error pair, and — for F4 — completion) independently, which
    ``insert_run`` does not expose. ``errored`` sets ``errored_at`` *and*
    ``error_reason`` together, as the paired CHECK requires.
    """
    connection.execute(
        "INSERT INTO runs "
        "(name, detected_at, pipeline_version, lifecycle, errored_at, error_reason, "
        " completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            detected_at if detected_at is not None else now_iso(),
            "reference-v1",
            lifecycle,
            now_iso() if errored else None,
            "boom" if errored else None,
            now_iso() if completed else None,
        ),
    )


def active_run_names(connection):
    """Return the names ``select_active_runs`` yields, in the order it returns them.

    Reads by column name, which also pins that ``select_active_runs`` returns
    column-keyed ``sqlite3.Row`` objects — a positional consumer would silently read
    the wrong column once a later migration appends one.
    """
    return [row["name"] for row in sluice.core.db.select_active_runs(connection)]


def test_active_runs_includes_a_plain_active_run(tmp_path):
    """A run that is ``lifecycle='active'`` and not errored is eligible.

    The base case: nothing excludes a plain active run.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_run_row(connection, "plain_active")

        assert active_run_names(connection) == ["plain_active"]


def test_active_runs_excludes_held(tmp_path):
    """A ``lifecycle='held'`` run is absent while an active sibling is present.

    Kills the mutant that drops the ``lifecycle = 'active'`` clause. The active
    sibling proves the predicate is filtering, not returning nothing.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_run_row(connection, "active_one")
        insert_run_row(connection, "held_one", lifecycle="held")

        names = active_run_names(connection)

    assert "active_one" in names
    assert "held_one" not in names


def test_active_runs_excludes_skipped(tmp_path):
    """A ``lifecycle='skipped'`` run is absent while an active sibling is present.

    A predicate like ``lifecycle != 'held'`` would admit a skipped run; this
    catches it. Kept separate from the held case so a predicate excluding only one
    of the two is caught.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_run_row(connection, "active_one")
        insert_run_row(connection, "skipped_one", lifecycle="skipped")

        names = active_run_names(connection)

    assert "active_one" in names
    assert "skipped_one" not in names


def test_active_runs_excludes_quarantined(tmp_path):
    """A quarantined run (``errored_at``/``error_reason`` set) is absent.

    Kills the mutant that drops the ``errored_at IS NULL`` clause. The run is still
    active, so only the error clause can exclude it.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_run_row(connection, "active_one")
        insert_run_row(connection, "errored_one", errored=True)

        names = active_run_names(connection)

    assert "active_one" in names
    assert "errored_one" not in names


def test_clear_error_alone_does_not_make_a_held_run_eligible(tmp_path):
    """Clearing the error on a run that is *both* held and errored leaves it absent.

    Fork F4's orthogonality, half one: the two clauses are AND-ed, so a held-and-
    quarantined run needs both an unhold and a clear-error. Clearing only the error
    columns leaves the held clause failing. Kills a predicate that OR-ed the two,
    or treated "not errored" as sufficient.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_run_row(connection, "both", lifecycle="held", errored=True)
        connection.execute(
            "UPDATE runs SET errored_at = NULL, error_reason = NULL WHERE name = ?",
            ("both",),
        )

        names = active_run_names(connection)

    assert "both" not in names


def test_unhold_alone_does_not_make_a_quarantined_run_eligible(tmp_path):
    """Unholding a run that is *both* held and errored leaves it absent.

    Half two (mirror of the previous test): from the same start, setting
    ``lifecycle='active'`` while leaving the error columns set leaves ``errored_at
    IS NULL`` failing. Together the two tests pin that both clauses must hold.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_run_row(connection, "both", lifecycle="held", errored=True)
        connection.execute(
            "UPDATE runs SET lifecycle = 'active' WHERE name = ?", ("both",)
        )

        names = active_run_names(connection)

    assert "both" not in names


def test_active_runs_is_ordered_by_detection_then_name(tmp_path):
    """Results are ordered by ``detected_at`` then ``name``, not by insertion order.

    Deterministic ordering makes daemon behaviour reproducible. ``zebra`` is
    detected earliest (sorts first despite its name); ``alpha`` and ``mango`` share
    a later ``detected_at`` so the tie breaks on name. Insertion order is ``mango,
    zebra, alpha``, so a missing ``ORDER BY``, an ``ORDER BY name`` only, and an
    ``ORDER BY detected_at`` without the name tiebreak each produce a different
    sequence and are caught.
    """
    early = "2026-07-20T00:00:00+00:00"
    late = "2026-07-20T12:00:00+00:00"
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_run_row(connection, "mango", detected_at=late)
        insert_run_row(connection, "zebra", detected_at=early)
        insert_run_row(connection, "alpha", detected_at=late)

        assert active_run_names(connection) == ["zebra", "alpha", "mango"]


def test_active_runs_still_includes_a_completed_run(tmp_path):
    """A completed run (``completed_at`` set) is still returned — fork F4.

    Eligibility is one canonical predicate, ``lifecycle = 'active' AND errored_at IS
    NULL``, that deliberately does not consult the derived ``completed_at`` cache:
    the constant means exactly one thing everywhere, and eligibility must not trust
    a cached conclusion reconcile re-derives every look (a stale stamp or a
    ``reset_gate`` would otherwise hide the run from the loop meant to notice). The
    accepted cost is that completed runs stay returned forever; reconcile skips
    them itself. A ``completed_at IS NULL`` pre-filter belongs in a separate scoped
    query, never folded into ``ACTIVE_RUNS_WHERE`` — this test rejects that.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_run_row(connection, "in_progress")
        insert_run_row(connection, "done", completed=True)

        names = active_run_names(connection)

    assert set(names) == {"in_progress", "done"}


# --------------------------------------------------------------------------
# Unit 13 — the metadata primitives: get_metadata, set_metadata, all_metadata.
#
# Key-agnostic primitives over ``poller_metadata``. ``set_metadata`` is an upsert
# (``ON CONFLICT(key) DO UPDATE``); ``db`` knows no key names — the meaning of
# ``paused``/``pipeline_profile``/the disk-halt flag belongs to control/daemon.
# Real files under ``tmp_path``. (The short-transaction guard for ``set_metadata``
# lives in ``test_row_primitives_leave_no_open_transaction`` above.)
# --------------------------------------------------------------------------


def test_set_then_get_metadata_round_trips(tmp_path):
    """A value written with ``set_metadata`` reads back with ``get_metadata``.

    The base case of the key/value store: what went in comes out unchanged.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        sluice.core.db.set_metadata(
            connection, "last_poll", "2026-07-20T00:00:00+00:00"
        )

        assert (
            sluice.core.db.get_metadata(connection, "last_poll")
            == "2026-07-20T00:00:00+00:00"
        )


def test_set_metadata_overwrites_an_existing_key(tmp_path):
    """Setting an existing key replaces its value in place — one row, not two.

    ``set_metadata`` is an upsert. Asserted by raw SQL, not ``get_metadata``, to
    isolate the write path: one row survives, holding the second value. Kills the
    plain ``INSERT`` mutant, which would raise ``IntegrityError`` on the second
    call (``key`` is the PK).
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        sluice.core.db.set_metadata(connection, "paused", "first")
        sluice.core.db.set_metadata(connection, "paused", "second")

        rows = list(
            connection.execute(
                "SELECT key, value FROM poller_metadata WHERE key = ?", ("paused",)
            )
        )

    assert len(rows) == 1, "an upsert must replace in place, not duplicate the key"
    assert rows[0]["value"] == "second"


def test_get_metadata_returns_the_default_for_a_missing_key(tmp_path):
    """A missing key returns the default: ``None`` by default, or the value given.

    Pins the miss contract: no ``default`` yields ``None`` (the sentinel callers
    branch on), a supplied ``default`` is returned verbatim — catching a mutant
    that returned ``""``, raised, or ignored ``default``.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        assert sluice.core.db.get_metadata(connection, "nope") is None
        assert sluice.core.db.get_metadata(connection, "nope", "x") == "x"


def test_all_metadata_returns_every_key(tmp_path):
    """``all_metadata`` returns every stored pair as a plain ``dict``.

    Seeded with raw ``INSERT``, not ``set_metadata``, to isolate the read path. The
    whole store comes back as a ``{key: value}`` mapping — the shape control/daemon
    read global state from.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        connection.execute(
            "INSERT INTO poller_metadata (key, value) VALUES (?, ?)", ("paused", "0")
        )
        connection.execute(
            "INSERT INTO poller_metadata (key, value) VALUES (?, ?)",
            ("pipeline_profile", "reference-v1"),
        )

        assert sluice.core.db.all_metadata(connection) == {
            "paused": "0",
            "pipeline_profile": "reference-v1",
        }


def test_db_defines_no_metadata_key_values():
    """``sluice.core.db`` hardcodes none of the daemon/control metadata key strings.

    Scope guard: ``paused``, ``pipeline_profile`` and ``disk_halted`` are
    daemon/control policy, not the key-agnostic db layer's. Every module-level
    string value (recursing via ``module_level_values``) is matched against those
    keys by value, not name, so ``PAUSED_KEY = "paused"`` cannot slip through.
    """
    forbidden_keys = {"paused", "pipeline_profile", "disk_halted"}
    module_strings = {
        value for value in module_level_values(sluice.core.db) if isinstance(value, str)
    }

    leaked = module_strings & forbidden_keys
    assert leaked == set(), (
        f"db hardcodes daemon/control metadata keys: {sorted(leaked)}"
    )


# --------------------------------------------------------------------------
# Subsystem-closing surface test.
#
# With unit 13 landed the public surface is complete, so §1's "``__all__`` names
# exactly the surface" becomes executable. It excludes the Schema-row objects
# (SQL objects, not Python exports) and the ``ACTIVE_RUNS_WHERE`` *constant*,
# which stays internal — its Python twin ``is_daemon_eligible`` is exported
# (plan 14 unit 2), because callers need the rule callable while the SQL string
# stays an implementation detail of ``select_active_runs``.
# --------------------------------------------------------------------------


def test_db_exports_exactly_the_documented_surface():
    """``sluice.core.db.__all__`` names exactly the plan §1 owned surface.

    Makes §1's "``__all__`` names exactly the surface" executable, catching future
    scope creep — an audit writer, a control op, a gate helper. Covers Python
    exports only: the Schema rows (SQL objects) and the internal
    ``ACTIVE_RUNS_WHERE`` constant do not belong here.
    """
    documented_surface = {
        # Connection factory, clock, short-transaction helper.
        "get_connection",
        "now_iso",
        "write_txn",
        # Forward-only migrations.
        "MIGRATIONS",
        "schema_version",
        "apply_migrations",
        "initialize_database",
        # Row primitives.
        "insert_run",
        "get_run",
        "update_run",
        "select_active_runs",
        # The Python twin of the internal ACTIVE_RUNS_WHERE constant (plan 14).
        "is_daemon_eligible",
        # Metadata primitives.
        "get_metadata",
        "set_metadata",
        "all_metadata",
        # Errors.
        "SchemaVersionError",
        "UnknownColumnError",
    }

    assert set(sluice.core.db.__all__) == documented_surface


# ==========================================================================
# SUBSYSTEM-LEVEL INTEGRATION TESTS
#
# Everything above is a unit test. The two properties below are *emergent* — they
# only appear when the primitives compose:
#
#   1. ``write_txn``'s ``BEGIN IMMEDIATE`` (vs plain deferred ``BEGIN``) — invisible
#      with one writer, only shows up when a second connection holds the write
#      lock. The test DEFERRED in the Unit 3 comment block above.
#
#   2. The lifecycle state machine — one run walked through its whole lifecycle via
#      the real primitives, asserting eligibility at every hop.
#
# Single-host invariant (design of record §5): every test is ONE process, MULTIPLE
# connections, ONE file on local disk. Real files under ``tmp_path``, never
# ``:memory:`` (a shared in-memory db has weaker locking, making these vacuous).
# ==========================================================================


# ---- Integration 1 — the two-writer BEGIN IMMEDIATE contention property ----
#
# Deterministic, not racing threads. One connection (A) holds the write lock for
# the whole test via ``BEGIN IMMEDIATE`` and never commits; the contender sets
# ``busy_timeout=0`` so a contended lock surfaces as an immediate
# ``OperationalError``, assertable without sleeps or wall-clock timing.


@contextlib.contextmanager
def connection_holding_the_write_lock(db_path):
    """Yield a connection that holds the database write lock for the whole block.

    Issues ``BEGIN IMMEDIATE`` (takes SQLite's single write lock up front) plus a
    write and does not commit until the block exits, so every other writer is
    contended for the ``with`` body. On exit it commits and closes, releasing the
    lock. The deterministic stand-in for "another writer is busy", no threads.
    """
    holder = sluice.core.db.get_connection(db_path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO runs (name, detected_at, pipeline_version) VALUES (?, ?, ?)",
            ("lock_holder", now_iso(), "reference-v1"),
        )
        yield holder
        holder.execute("COMMIT")
    finally:
        holder.close()


def test_deferred_read_then_write_fails_on_the_lock_upgrade(tmp_path):
    """A deferred txn that reads then writes can raise ``SQLITE_BUSY`` on upgrade.

    Pins the hazard ``write_txn``'s ``BEGIN IMMEDIATE`` exists to avoid. With A
    holding the write lock, B starts a deferred transaction, takes a SHARED read
    lock by reading, then attempts a write: SQLite must upgrade SHARED → write, but
    A holds it, and the busy handler is not honored on this deadlock-avoidance
    upgrade path, so B fails outright. ``busy_timeout=0`` only makes it prompt.

    The point: a deferred read-then-write caller can get *into* a transaction, do
    real work, then fail on the write it already committed to — the mid-transaction
    failure ``BEGIN IMMEDIATE`` removes by taking the lock first.
    """
    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    with connection_holding_the_write_lock(db_path):
        with opened_connection(db_path) as deferred:
            deferred.execute("PRAGMA busy_timeout=0")
            deferred.execute("BEGIN")  # deferred: acquires no lock yet
            # A read acquires a SHARED lock — B is now mid-transaction.
            deferred.execute("SELECT COUNT(*) FROM runs").fetchone()
            # The write must upgrade SHARED -> write lock, which A holds. This is
            # the read-then-write-upgrade failure BEGIN IMMEDIATE is designed out of.
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                deferred.execute(
                    "INSERT INTO runs (name, detected_at, pipeline_version) "
                    "VALUES (?, ?, ?)",
                    ("deferred_writer", now_iso(), "reference-v1"),
                )
            deferred.execute("ROLLBACK")


def test_write_txn_takes_the_write_lock_at_begin_never_mid_transaction(
    tmp_path, monkeypatch
):
    """``write_txn`` fails up front at ``BEGIN IMMEDIATE`` — its block never runs.

    The correctness property ``BEGIN IMMEDIATE`` buys over plain ``BEGIN``. With A
    holding the write lock, ``write_txn`` contends for it at transaction start, so
    under ``busy_timeout=0`` it raises ``SQLITE_BUSY`` before yielding — the body
    never executes. ``get_connection`` is monkeypatched to stamp ``busy_timeout=0``
    on the connection ``write_txn`` opens internally (its only seam), turning the
    5-second wait into an instant failure without changing which ``BEGIN`` it issues.

    Non-vacuity: the discriminating assertion is ``block_body_ran``, not
    ``pytest.raises`` — a plain-``BEGIN`` mutant *also* raises here (its deferred
    read-then-write upgrade fails, per the test above), but only after entering the
    block. Real ``BEGIN IMMEDIATE`` gives ``block_body_ran is False`` (fails at
    BEGIN); the mutant gives ``True`` (fails mid-block). This one flag distinguishes
    them.
    """
    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    real_get_connection = sluice.core.db.get_connection

    def get_connection_with_zero_busy_timeout(path):
        connection = real_get_connection(path)
        connection.execute("PRAGMA busy_timeout=0")
        return connection

    monkeypatch.setattr(
        sluice.core.db, "get_connection", get_connection_with_zero_busy_timeout
    )

    with connection_holding_the_write_lock(db_path):
        block_body_ran = False
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            with sluice.core.db.write_txn(db_path) as connection:
                # A plain-BEGIN mutant reaches here and fails on the write below;
                # real BEGIN IMMEDIATE raises at BEGIN and never reaches this line.
                block_body_ran = True
                connection.execute("SELECT COUNT(*) FROM runs").fetchone()
                connection.execute(
                    "INSERT INTO runs (name, detected_at, pipeline_version) "
                    "VALUES (?, ?, ?)",
                    ("immediate_writer", now_iso(), "reference-v1"),
                )

        assert block_body_ran is False, (
            "write_txn must fail at BEGIN IMMEDIATE, before yielding — a "
            "plain-BEGIN (deferred) write_txn would instead enter the block and "
            "fail mid-transaction on the write-lock upgrade"
        )

    # A has released the lock; a fresh write_txn now succeeds immediately, proving
    # the earlier failure was contention, not breakage.
    with sluice.core.db.write_txn(db_path) as connection:
        connection.execute(
            "INSERT INTO runs (name, detected_at, pipeline_version) VALUES (?, ?, ?)",
            ("after_release", now_iso(), "reference-v1"),
        )

    with opened_connection(db_path) as reader:
        names = {row["name"] for row in reader.execute("SELECT name FROM runs")}
    assert "after_release" in names
    assert "immediate_writer" not in names, (
        "the contended write_txn's block never ran, so its row must not exist"
    )


# ---- Integration 2 — the lifecycle journey (the state machine composing) ----
#
# Walks ONE run through its whole lifecycle via the real primitives
# (``insert_run`` / ``update_run``), asserting eligibility at every transition. A
# second run stays plainly active throughout as a control, so each assertion
# distinguishes "this run's state changed" from "the query returned nothing".


def test_lifecycle_journey_through_the_real_primitives(tmp_path):
    """One run's whole lifecycle, asserting ``select_active_runs`` at every hop.

    Each step driven by ``update_run`` and observed through ``select_active_runs``:

    1. ``insert_run`` → eligible.
    2. hold → gone; un-hold → returns.
    3. quarantine (error columns set together, paired CHECK) → gone; clear → returns.
    4. complete (``completed_at`` set, still active) → **still present** — fork F4:
       eligibility does not consult the completion cache; reconcile skips it.
    5. skip → gone for good.

    ``steady`` is never touched; its continuous presence proves each disappearance
    of ``journey`` is that run leaving the set, not the query going dark.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        # 1. Detection: both runs land active and eligible.
        assert sluice.core.db.insert_run(
            connection, "journey", pipeline_version="reference-v1"
        )
        assert sluice.core.db.insert_run(
            connection, "steady", pipeline_version="reference-v1"
        )
        assert set(active_run_names(connection)) == {"journey", "steady"}

        # 2a. Hold journey — an operator hold removes it from the eligible set.
        sluice.core.db.update_run(connection, "journey", {"lifecycle": "held"})
        assert "journey" not in active_run_names(connection)
        assert "steady" in active_run_names(connection)

        # 2b. Un-hold — it returns.
        sluice.core.db.update_run(connection, "journey", {"lifecycle": "active"})
        assert "journey" in active_run_names(connection)
        assert "steady" in active_run_names(connection)

        # 3a. Quarantine — the error columns move together (paired CHECK), and the
        # run drops out of eligibility on ``errored_at IS NULL``.
        sluice.core.db.update_run(
            connection,
            "journey",
            {"error_reason": "gate raised", "errored_at": now_iso()},
        )
        assert "journey" not in active_run_names(connection)
        assert "steady" in active_run_names(connection)

        # 3b. clear_error — both error columns cleared together; it returns.
        sluice.core.db.update_run(
            connection, "journey", {"error_reason": None, "errored_at": None}
        )
        assert "journey" in active_run_names(connection)
        assert "steady" in active_run_names(connection)

        # 4. Completion — F4: a completed run is STILL eligible. The query does not
        # consult ``completed_at``; reconcile skips completed runs itself.
        sluice.core.db.update_run(connection, "journey", {"completed_at": now_iso()})
        journey_row = sluice.core.db.get_run(connection, "journey")
        assert journey_row["completed_at"] is not None
        assert journey_row["lifecycle"] == "active"
        assert journey_row["errored_at"] is None
        assert "journey" in active_run_names(connection)
        assert "steady" in active_run_names(connection)

        # 5. Skip — a terminal operator decision; gone for good.
        sluice.core.db.update_run(connection, "journey", {"lifecycle": "skipped"})
        assert "journey" not in active_run_names(connection)
        assert "steady" in active_run_names(connection)


# --------------------------------------------------------------------------
# Plan 14 unit 2 — the eligibility rule as a callable Python predicate.
#
# ``ACTIVE_RUNS_WHERE`` is the SQL half and cannot be called from Python;
# daemon and control each hand-transcribed it instead. ``is_daemon_eligible``
# is the one Python definition both now call. The SQL/Python pair is
# irreducible (SQLite cannot call a Python function), so it is guarded
# mechanically by ``tests/test_predicate_twins.py``.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lifecycle", "errored", "eligible"),
    [
        ("active", False, True),
        ("active", True, False),
        ("held", False, False),
        ("held", True, False),
        ("skipped", False, False),
        ("skipped", True, False),
    ],
)
def test_is_daemon_eligible_matches_the_two_and_ed_clauses(
    tmp_path, lifecycle, errored, eligible
):
    """Only ``lifecycle='active'`` AND a clear ``errored_at`` is daemon-eligible.

    The full cross-product, so dropping either clause is caught: a predicate
    reading only ``lifecycle`` would call the errored-active row eligible, and
    one reading only ``errored_at`` would call the clear held row eligible.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_run_row(connection, "subject", lifecycle=lifecycle, errored=errored)
        run_row = sluice.core.db.get_run(connection, "subject")

    assert sluice.core.db.is_daemon_eligible(run_row) is eligible, (
        f"lifecycle={lifecycle!r} errored={errored} must be "
        f"{'eligible' if eligible else 'ineligible'} — the rule is exactly "
        "lifecycle='active' AND errored_at IS NULL (F4)."
    )


def test_is_daemon_eligible_ignores_completion(tmp_path):
    """A completed but still-active run stays eligible (F4).

    ``completed_at`` is a cache reconcile re-derives every look, so filtering on
    it could hide a run (stale stamp, reset_gate) from the loop meant to notice
    it. This pins that F4 carve-out against a future "obvious" tightening.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        insert_run_row(connection, "done_but_active", completed=True)
        run_row = sluice.core.db.get_run(connection, "done_but_active")

    assert sluice.core.db.is_daemon_eligible(run_row) is True, (
        "a completed run that is still active and unquarantined must remain "
        "eligible — completed_at is deliberately not part of the rule (F4)."
    )


def test_is_daemon_eligible_accepts_a_plain_mapping():
    """The predicate is duck-typed on subscript, so a plain dict works.

    Callers hand it a ``sqlite3.Row`` in production, but control and daemon
    tests build plain dicts; requiring a real Row would make the predicate
    awkward to use exactly where it is most useful.
    """
    assert (
        sluice.core.db.is_daemon_eligible({"lifecycle": "active", "errored_at": None})
        is True
    )


# --------------------------------------------------------------------------
# Migration 008 — the download gate's satisfaction column (plan 15 increment 2).
#
# Incremental-migration policy (plan 13 ⚑3): one satisfaction column per gate,
# shipped WITH its gate. 008 owns only `downloaded`.
# --------------------------------------------------------------------------


def test_migration_008_adds_downloaded_not_null_default_zero(tmp_path):
    """``runs.downloaded`` exists as ``INTEGER NOT NULL DEFAULT 0`` at head.

    Never nullable: a nullable satisfaction column gives reconcile three-valued
    logic (``WHERE col = 0`` silently misses NULL), and the constant default
    keeps the ALTER legal on a populated table — pre-existing rows land
    unsatisfied, which is correct because they never ran the gate.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        column = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(runs)")
        }["downloaded"]

    assert column["type"] == "INTEGER"
    assert column["notnull"] == 1, (
        "downloaded must be NOT NULL — a nullable satisfaction column gives "
        "reconcile three-valued logic"
    )
    assert column["dflt_value"] == "0"


def test_migration_ladder_head_is_008(tmp_path):
    """The ladder reaches 008 and records it.

    Pins that the download gate's migration is actually wired into MIGRATIONS
    rather than merely defined — a gate whose column never lands would raise
    KeyError in reconcile on every look.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        assert sluice.core.db.schema_version(connection) == 8
