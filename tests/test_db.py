"""Behavioral tests for ``sluice.core.db`` — the SQLite state store primitives.

The database is the single source of truth for run state (architecture design of
record, section 5), so every primitive in this module is pinned by a test that
asserts observable behavior rather than implementation shape.

Timestamps: every timestamp column in this subsystem (``detected_at``,
``lifecycle_at``, ``errored_at``, ``applied_at``, ``control_audit.ts``) is
written with ``now_iso()``, so its three load-bearing properties are asserted
separately here — it reads the clock, it emits an explicit ``+00:00`` wire
format, and plain string comparison of that format is chronological.
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

# How far ``now_iso()`` may lag a freshly-read clock before we call it stopped.
# Deliberately generous: this bound exists to kill frozen-constant and
# cached-at-import regressions, not to measure scheduler latency.
MAXIMUM_CLOCK_SKEW = timedelta(seconds=5)


@contextlib.contextmanager
def host_timezone(zone_name: str):
    """Run the block as though the host's local time zone were ``zone_name``.

    ``time.tzset()`` is what makes ``datetime.now().astimezone()`` pick up the
    changed ``TZ``; setting the environment variable alone is not enough. Unix
    only, which covers both deployment environments (CHTC Linux, macOS laptop).
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


def render_timestamp_like_now_iso(instant: datetime) -> str:
    """Render ``instant`` in the wire format ``now_iso()`` is contracted to emit.

    The ordering tests below need *known-distinct* instants: three back-to-back
    real calls are not reliably distinct (on a coarse-clock host they are always
    equal), so pinning lexical ordering against the live clock would leave the
    property untested most runs and untestable on some hosts. Synthesizing the
    instants makes those tests deterministic.

    This helper is only legitimate because
    ``test_now_iso_emits_an_explicit_utc_offset`` pins ``now_iso()``'s own output
    to exactly this rendering via a parse/re-render round trip. If the wire
    format ever changes, that test fails first and this helper is corrected with
    it — the two cannot drift apart silently.
    """
    return instant.isoformat()


def test_now_iso_is_parseable_utc():
    """``now_iso()`` round-trips through ``fromisoformat`` as an aware instant.

    A naive timestamp parses fine but raises TypeError the moment it is compared
    against an offset-aware value, so the offset must be present and be zero.
    """
    parsed = datetime.fromisoformat(now_iso())

    assert parsed.tzinfo is not None, "now_iso() must carry an explicit offset"
    assert parsed.utcoffset() == timedelta(0), "now_iso() must be UTC, not local time"


def test_now_iso_reads_the_clock():
    """``now_iso()`` reports the current instant, not a fixed or cached one.

    Without this, a regression that stopped the clock — a constant, or a value
    computed once at import — would pin every timestamp column in the system to
    a single instant while every other property asserted here still held.
    """
    observed = datetime.fromisoformat(now_iso())

    assert abs(observed - datetime.now(timezone.utc)) < MAXIMUM_CLOCK_SKEW


def test_now_iso_emits_an_explicit_utc_offset():
    """The stored string ends in ``+00:00``, whatever the host time zone is.

    Asserting ``utcoffset() == timedelta(0)`` is vacuous on a UTC host: a
    local-time implementation (``datetime.now().astimezone()``) satisfies it,
    and CHTC runs UTC. But this project is two-environment (HANDOFF section 2) —
    the daemon runs on CHTC while control operations are issued from a macOS
    laptop. If those writers disagree about the offset, one row reads ``+00:00``
    and the next ``-05:00``, lexical order stops matching chronological order,
    and ``ORDER BY detected_at`` misorders silently across writers. The literal
    offset in the stored string is therefore behavior, not an implementation
    detail.

    The round trip additionally pins the rendering itself, which is what lets
    ``render_timestamp_like_now_iso`` stand in for the real thing below.
    """
    stamp = now_iso()

    assert stamp.endswith("+00:00")
    assert datetime.fromisoformat(stamp).isoformat() == stamp


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="TZ manipulation is Unix only")
@pytest.mark.parametrize(
    "zone_name",
    ["UTC", "America/Chicago", "Asia/Kolkata"],
)
def test_now_iso_is_utc_regardless_of_host_timezone(zone_name):
    """The offset is ``+00:00`` even when the host's local zone is not UTC.

    This is the assertion that actually separates explicit-UTC from local-aware.
    Checking ``endswith("+00:00")`` on a UTC host cannot: ``datetime.now()
    .astimezone()`` resolves to UTC there and renders ``+00:00`` too, so on CHTC
    — the very host the two-environment concern is about — that check passes for
    a local-time implementation. Varying the host zone is what makes the
    property observable, and ``Asia/Kolkata`` additionally pins the half-hour
    offset case (``+05:30``) that a naive "strip the sign and digits" fix would
    let through.
    """
    with host_timezone(zone_name):
        stamp = now_iso()

    assert stamp.endswith("+00:00"), f"local zone {zone_name} leaked into the timestamp"
    assert datetime.fromisoformat(stamp).utcoffset() == timedelta(0)


def test_now_iso_sorts_lexically_in_chronological_order():
    """Lexical order of the wire format matches chronological order.

    SQLite orders these columns with string comparison (``ORDER BY
    detected_at``), so this equivalence is what makes that ordering correct.

    Note the property is lexical-order-equals-chronological-order, *not*
    monotonic issuance: ``datetime.now`` is wall clock, so an NTP step backwards
    can yield a decreasing pair of successive calls. Nothing may rely on strict
    monotonicity of a ``ts`` column alone — ``control_audit.id`` is INTEGER
    PRIMARY KEY AUTOINCREMENT for exactly that reason (design section 4).
    """
    base = datetime(2026, 7, 20, 14, 3, 11, 482913, tzinfo=timezone.utc)
    chronological = [
        base,
        base + timedelta(microseconds=1),
        base + timedelta(seconds=1),
        base + timedelta(days=1),
        base + timedelta(days=365),
    ]

    rendered = [render_timestamp_like_now_iso(instant) for instant in chronological]

    assert len(set(rendered)) == len(rendered), "synthesized instants must be distinct"
    assert rendered == sorted(rendered)


def test_now_iso_sorts_lexically_across_fractional_second_widths():
    """Ordering survives the width change at an exact second.

    ``isoformat()`` omits the fractional part entirely when microseconds are
    zero, so ``...T14:03:11+00:00`` is shorter than ``...T14:03:11.000001+00:00``
    yet must still sort first. It does, because ``"+"`` (0x2B) precedes ``"."``
    (0x2E) — but that case arises about once in a million calls, so the live
    clock will never sample it, and the reasoning is subtle enough that a future
    "normalize the timestamp format" refactor could break lexical ordering
    invisibly. Pinned directly.
    """
    exact_second = datetime(2026, 7, 20, 14, 3, 11, 0, tzinfo=timezone.utc)
    one_microsecond_later = exact_second + timedelta(microseconds=1)

    earlier = render_timestamp_like_now_iso(exact_second)
    later = render_timestamp_like_now_iso(one_microsecond_later)

    assert len(earlier) < len(later), "the exact second must be the shorter string"
    assert earlier < later


# --------------------------------------------------------------------------
# Unit 2 — get_connection(db_path) + module hygiene
#
# Every connection test below opens a *real file* under ``tmp_path``. Never
# ``:memory:``: SQLite silently refuses WAL on an in-memory database and
# ``PRAGMA journal_mode`` reports ``memory`` there, which would make the pragma
# assertions vacuous — they would fail against a correct implementation and pass
# against nothing at all.
# --------------------------------------------------------------------------


@contextlib.contextmanager
def opened_connection(db_path):
    """Yield ``get_connection(db_path)`` and close it, whatever the test does.

    Resolved through the module object rather than a module-level ``from ...
    import get_connection`` on purpose: while the function is unwritten, an
    import-time failure would take the whole file down with it and turn unit 1's
    passing tests red for a reason that has nothing to do with them.
    """
    connection = sluice.core.db.get_connection(db_path)
    try:
        yield connection
    finally:
        connection.close()


def test_get_connection_reports_wal_journal_mode(tmp_path):
    """The opened database is actually *in* WAL mode, as queried back from SQLite.

    Asserted by querying ``PRAGMA journal_mode`` rather than by observing that a
    ``PRAGMA journal_mode=WAL`` statement was issued: SQLite accepts that
    statement and declines to act on it in several situations (an in-memory
    database, a database already held open in another mode), returning the mode
    it actually chose. Only the queried value distinguishes "we asked" from "it
    happened", and WAL is what lets the daemon's readers run without blocking on
    a writer (design of record, section 5).
    """
    with opened_connection(tmp_path / "state.db") as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert journal_mode.lower() == "wal"


def test_get_connection_reports_synchronous_normal(tmp_path):
    """``PRAGMA synchronous`` reads back as ``1`` (NORMAL), not SQLite's default ``2``.

    This is the one durability pragma this unit genuinely changes. python-poller
    measures ``synchronous = 2`` (FULL) — it never sets the pragma, so it
    inherits the default and pays an fsync per commit. NORMAL is the safe setting
    under WAL, and because the default is ``2`` this assertion fails loudly if the
    statement is dropped or if only ``journal_mode`` is set (errata A1).
    """
    with opened_connection(tmp_path / "state.db") as connection:
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert synchronous == 1


def test_get_connection_busy_timeout_is_5000(tmp_path):
    """The busy timeout is 5000 ms — a value that was **already** 5000 by default.

    This test documents a value, it does not verify a fix. Python's
    ``sqlite3.connect`` defaults to ``timeout=5.0``, so 5000 ms is what
    python-poller already measures (errata A1) despite setting no such pragma.
    Passing ``timeout=5.0`` explicitly buys **no** lock-contention improvement
    whatsoever; it is done purely so the number is legible in the source instead
    of inherited invisibly from a stdlib default. Any docstring, comment or test
    name framing this as a bug fix or a locking improvement is a review defect.

    Accordingly this assertion has no mutation-killing power against the pragma
    work — a ``get_connection`` that sets nothing at all still passes it. It is
    here to pin the number against a future stdlib default change or an
    inadvertent ``timeout=`` edit.
    """
    with opened_connection(tmp_path / "state.db") as connection:
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert busy_timeout == 5000


def test_get_connection_rows_are_mappings(tmp_path):
    """Rows come back keyed by column name, not as bare positional tuples.

    Every consumer in this subsystem reads ``row["lifecycle"]`` /
    ``row["pipeline_version"]`` by name. With the default tuple row factory those
    lookups raise ``TypeError``, and — worse — positional reads keep working
    against a schema that later migrations append columns to, so an index-based
    consumer silently reads the wrong column instead of failing.
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
    first DML statement and holds it — and therefore the write lock — until
    someone commits. That is exactly the long-lived write transaction design of
    record section 5 forbids: a gate that runs a multi-hour subprocess between
    two writes would wedge every other writer. ``isolation_level=None`` makes
    each statement stand alone; batching is opt-in, via ``write_txn``'s explicit
    ``BEGIN IMMEDIATE``.

    Both halves matter. ``in_transaction`` is the behavioural evidence, but it is
    also ``False`` before any DML has run, so the insert is what makes it
    load-bearing; ``isolation_level is None`` pins the setting that produces it.
    """
    with opened_connection(tmp_path / "state.db") as connection:
        connection.execute("CREATE TABLE t (a)")
        connection.execute("INSERT INTO t (a) VALUES (1)")

        assert connection.isolation_level is None
        assert connection.in_transaction is False


def test_get_connection_does_not_create_parent_directory(tmp_path):
    """A missing parent directory is an error, not something to helpfully create.

    Deliberate divergence from python-poller, which calls ``os.makedirs()`` inside
    its DB initialization (``poller_state_db.py:78``). Provisioning directories is
    config/bootstrap's job; a connection factory that creates them will silently
    stand up a brand-new empty state database next to a typo'd path, and the
    daemon then reports zero known runs instead of refusing to start.
    """
    missing_parent = tmp_path / "nope"

    with pytest.raises(sqlite3.OperationalError):
        sluice.core.db.get_connection(missing_parent / "state.db")

    assert missing_parent.exists() is False


def module_level_values(module):
    """Yield every module-level value, recursing into plain containers.

    A flat ``vars(module).values()`` scan only sees objects bound *directly* to a
    module global, so a cache held one level down — ``_POOL: dict[str,
    Connection]``, or a list of handles — is invisible to it. Recursing is what
    makes the scan describe "the module holds a connection anywhere" instead of
    "the module holds a connection in a bare global".

    Mirrors the technique unit 13 specifies for
    ``test_db_defines_no_metadata_key_values``: module-level tuple/list/set/
    frozenset members, plus both the keys and the values of module-level dicts.
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

    Guards the spine's top invariant — "the database is the single source of
    truth" — for which this is the only test in the subsystem (plan section 6). A
    module-level cached handle would make an in-process object authoritative:
    connections carry transaction state, so a shared one lets an open transaction
    in one caller silently change what another caller sees, and whatever it holds
    survives a schema change or an external write the database has already
    accepted.

    Three assertions, because the plausible defect got more sophisticated at each
    step and the first two are each individually insufficient:

    1. ``first is not second`` alone passes for a factory that hands out fresh
       *wrapper* objects over one shared handle.
    2. ``type(first) is sqlite3.Connection`` closes that: an exact type check, not
       ``isinstance``, since a proxy subclassing ``Connection`` would satisfy
       ``isinstance``. Anything sharing an underlying handle has to interpose
       *some* object to do the sharing.
    3. The module-level scan recurses into containers, so a keyed cache
       (``_POOL[path] = conn``) is caught as well as a bare ``_CONN`` global.
       This is the form worth worrying about: once the daemon, CLI and API are
       each calling ``get_connection`` every look, "just memoize it per path" is
       the natural-looking optimization, and it is exactly the one that quietly
       reintroduces an authoritative in-process handle.
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

    Guards the ``gates -> core`` arrow ahead of the broader
    ``tests/test_dependency_arrows.py``: core is the layer everything else is
    allowed to depend on, so it must depend on nothing of ours in return. Parsed
    with ``ast`` rather than inspected at runtime because a conditional or
    function-local import would not show up in the imported module's namespace,
    and that is precisely how a dependency inversion gets smuggled in.
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
# ``write_txn`` is how design of record section 5's "short transactions only"
# rule is actually enforced. It layers an explicit ``BEGIN IMMEDIATE`` /
# ``COMMIT`` on top of unit 2's autocommit connection (fork F2), rolls back and
# re-raises on an exception, and always closes the connection in ``finally``.
#
# Because the connection is closed in ``finally``, none of these tests may read
# ``conn.in_transaction`` after the block: CPython guards that getter with a
# live-connection check and raises ``sqlite3.ProgrammingError`` rather than
# returning a value, so such an assertion is unimplementable alongside the close
# test. Lock release is evidenced by a *fresh* connection taking the write lock
# (``test_write_txn_releases_the_write_lock``); the forgot-to-COMMIT case is
# evidenced by ``test_write_txn_commits_on_clean_exit``. The genuinely testable
# open-transaction risk lives in the caller-supplied-connection primitives
# (units 9, 11, 13), where the handle is still open.
#
# DEFERRED FOR integration-tester (subsystem-level, two writers): ``write_txn``
# uses ``BEGIN IMMEDIATE`` rather than plain ``BEGIN``. No single-process test in
# this file discriminates the two — a deferred transaction still takes the write
# lock at its first write, and every test body writes immediately, so a plain
# ``BEGIN`` mutant survives all five unit tests. The property ``IMMEDIATE`` buys
# is only observable with concurrency: a *deferred* read-then-write transaction
# can fail ``SQLITE_BUSY`` on lock upgrade (busy-timeout is not honored on the
# deadlock-avoidance upgrade path) while ``IMMEDIATE`` takes the lock up front.
# Integration test to write: a second connection holds the write lock; a caller
# doing read-then-write in a deferred txn raises ``SQLITE_BUSY``, whereas the same
# caller under ``write_txn`` blocks then succeeds. Until that exists, ``BEGIN
# IMMEDIATE`` is enforced by review + docstring only.
#
# As above: a real file under ``tmp_path`` every time, never ``:memory:``.
# --------------------------------------------------------------------------


def create_counter_table(db_path):
    """Create table ``t`` at ``db_path`` and commit it, before any test's block.

    Every ``write_txn`` test needs a table that already exists and is durable, so
    that what the test observes afterwards is the transaction's behavior and not
    the DDL's. Written through ``get_connection``, whose autocommit mode makes
    the ``CREATE TABLE`` durable the moment it runs.
    """
    with opened_connection(db_path) as connection:
        connection.execute("CREATE TABLE t (a)")


def count_rows(db_path) -> int:
    """Return ``SELECT COUNT(*) FROM t`` as seen by a brand-new connection.

    A *new* connection is the point: it holds none of the writer's transaction
    state, so what it reports is what the database file actually committed.
    """
    with opened_connection(db_path) as connection:
        return connection.execute("SELECT COUNT(*) FROM t").fetchone()[0]


def test_write_txn_commits_on_clean_exit(tmp_path):
    """Work done inside the block is durable once the block exits normally.

    The observation is made from a separate connection opened after the fact, so
    it reflects the database file rather than any state cached on the writer's
    handle. This is what fails if the implementation opens ``BEGIN IMMEDIATE``
    and never issues ``COMMIT``: the ``finally: close()`` then rolls the
    transaction back and the row silently vanishes.
    """
    db_path = tmp_path / "state.db"
    create_counter_table(db_path)

    with sluice.core.db.write_txn(db_path) as connection:
        connection.execute("INSERT INTO t (a) VALUES (1)")

    assert count_rows(db_path) == 1


def test_write_txn_rolls_back_on_exception(tmp_path):
    """An exception inside the block discards the writes *and* propagates.

    Both halves are load-bearing and each catches a different defect. The
    ``pytest.raises`` catches an implementation that swallows the exception,
    which would turn a failed batch of writes into a silently successful call.
    The count catches one that commits regardless of how the block exited — and
    it also catches the degenerate implementation that never opens a transaction
    at all, since under unit 2's autocommit connection an un-wrapped insert is
    already durable by the time the ``raise`` runs.
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

    ``write_txn`` owns the connection it opens, so leaking it leaks a file
    descriptor per call — and the daemon calls this on every cycle. A closed
    ``sqlite3.Connection`` raises ``ProgrammingError`` on use, which is the
    observable difference between "closed" and "merely committed".
    """
    db_path = tmp_path / "state.db"
    create_counter_table(db_path)

    with sluice.core.db.write_txn(db_path) as connection:
        connection.execute("INSERT INTO t (a) VALUES (1)")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_write_txn_releases_the_write_lock(tmp_path):
    """Another writer can take the write lock immediately after the block.

    This is the concrete failure mode design of record section 5 exists to
    prevent: a write transaction that outlives its block starves every other
    writer — the daemon, and every CLI control operation — until it is torn down.

    The probe sets ``busy_timeout=0`` so a still-held lock surfaces as an
    immediate ``OperationalError`` rather than as a five-second stall followed by
    one; without that the test would still be correct but would pay unit 2's
    5000 ms timeout on every failure.
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

    This is the WAL guarantee the daemon depends on (design of record section 5):
    it must be able to poll run state while a gate is writing. Both halves are
    asserted because they fail differently — a non-WAL database makes the
    ``SELECT`` *raise* "database is locked", while a leaked or missing isolation
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
