"""SQLite state store primitives for sluice.

The database is the single source of truth for run state (architecture design of
record, section 5). This module owns the low-level primitives that every other
subsystem builds on; it deliberately knows nothing about gates, profiles, or the
daemon loop.

Dependency direction is one-way: ``gates -> core``. Nothing under ``sluice/core``
may import ``sluice.gates``, ``sluice.api``, or ``sluice.cli``, so this module
imports only from the standard library. ``tests/test_dependency_arrows.py``
enforces that on every module it discovers under ``sluice.core``.

``__all__`` is the module's declared public surface. Later units assert against
it rather than against ``dir()``, so a name is public here only if it is listed
there on purpose.
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

__all__ = ["get_connection", "now_iso", "write_txn"]


def now_iso() -> str:
    """Return the current instant as an ISO-8601 string with an explicit UTC offset.

    Every timestamp column in this subsystem is written with this function, which
    gives those columns two properties the rest of the system relies on:

    1. The string round-trips through ``datetime.fromisoformat`` as an
       offset-aware instant. Writing a naive local timestamp instead would parse
       back fine and then raise ``TypeError`` the first time it was compared
       against an offset-aware value.
    2. Plain string comparison of the output is chronological. SQLite orders
       these columns lexically (``ORDER BY detected_at``), and UTC ISO-8601 is
       big-endian — year, then month, then day, on down to microseconds — so
       lexical order matches chronological order. The one width variation is
       that ``isoformat`` omits the fractional part entirely on an exact second,
       and even that orders correctly: two stamps can only differ in width after
       an identical seconds field, where ``"+"`` (of the offset) sorts before
       ``"."`` (of the fraction), placing the exact second first, which is
       where it belongs.

    Returns:
        e.g. ``"2026-07-20T14:03:11.482913+00:00"``.
    """
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a new SQLite connection to ``db_path``, configured for this system.

    A *fresh* connection every call, deliberately: the database is the single
    source of truth, so there is no module-level handle to cache. A connection
    carries transaction state, which means a shared one would let an open
    transaction in one caller change what another caller sees, making an
    in-process object authoritative over the file.

    The configuration applied here:

    * ``isolation_level=None`` — autocommit. Python's default (``""``) opens an
      implicit transaction on the first DML statement and holds the write lock
      until something commits, which is precisely the long-lived write
      transaction design of record section 5 forbids. Batching is opt-in, via
      ``write_txn``'s explicit ``BEGIN IMMEDIATE`` layered on top of this.
    * ``row_factory = sqlite3.Row`` — rows are read by column name throughout
      this subsystem. Positional reads would keep "working" after a migration
      appends a column, silently returning the wrong one.
    * ``journal_mode=WAL`` — readers do not block on a writer, which is what
      lets the daemon poll while a gate writes.
    * ``synchronous=NORMAL`` — the safe setting under WAL, and the one
      *durability* pragma python-poller leaves unset: SQLite's default is ``2``
      (FULL), so without this statement the connection pays an fsync per commit.
      Both pragmas do real work here — dropping either one is observable
      (``journal_mode`` falls back to ``delete``, ``synchronous`` to ``2``).

    On ``timeout=5.0`` (errata A1): passing it is **not** a bug fix and buys no
    lock-contention improvement whatsoever. ``sqlite3.connect`` already defaults
    to ``timeout=5.0``, so ``busy_timeout`` is 5000 ms either way — a version of
    this function that omits the argument entirely behaves identically. It is
    written out only so the number is legible in the source rather than
    inherited invisibly from a stdlib default.

    This function does not create the parent directory and does not create
    schema. Provisioning directories belongs to config/bootstrap: a connection
    factory that calls ``makedirs`` stands up a brand-new empty state database
    next to a typo'd path, and the daemon then reports zero known runs instead
    of refusing to start.

    Args:
        db_path: Path to the SQLite database file. Required — there is no
            default, because a missing path is a caller error rather than
            grounds for silently falling back to some production location.

    Returns:
        An open connection. The caller owns it and is responsible for closing it.

    Raises:
        sqlite3.DatabaseError: The covering case — catch this if you want to
            catch everything this function can raise. Note the two failures
            below are *siblings*, not one subclass of the other, so
            ``except sqlite3.OperationalError`` alone will not catch a corrupt
            file.
        sqlite3.OperationalError: If the database file cannot be opened, which
            includes the case of a parent directory that does not exist.
            Raised by ``connect`` itself, before anything is constructed.
        sqlite3.DatabaseError: If ``db_path`` names an existing file that is not
            a SQLite database ("file is not a database"). ``connect`` succeeds
            on such a file and the first pragma is what fails.
    """
    connection = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        # A pragma can fail after connect() has already handed us a live
        # handle: connect() opens lazily, so a corrupt or non-SQLite file at
        # db_path connects fine and then raises DatabaseError here. Without
        # this, the raising frame stays alive in the traceback holding the
        # connection, and a daemon that logs exc_info and retries each cycle
        # leaks an fd per attempt.
        #
        # Intentionally untested. The only ways to observe it are a spy on
        # sqlite3.connect, which pins the call graph (the anti-pattern the plan
        # rules out at 01-db-plan.md:410-416), or an fd count under /proc, which
        # is Linux-only and so would not run on the macOS half of this
        # two-environment project. Please do not "fix" that with a spy.
        connection.close()
        raise
    return connection


@contextmanager
def write_txn(db_path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    """Run a block inside one short, explicit write transaction, then tear it down.

    This helper is how design of record section 5's "short transactions only"
    rule is actually enforced. Unit 2's connection runs in autocommit
    (``isolation_level=None``), so nothing ever opens an implicit transaction on
    its own; ``write_txn`` is the *only* thing that opens one, and it always
    closes it. A write transaction that outlived its block would hold the write
    lock and starve every other writer — the daemon and every CLI control
    operation — which is precisely the failure mode that rule exists to prevent.

    The transaction is opened with ``BEGIN IMMEDIATE`` rather than a plain
    (deferred) ``BEGIN``. ``IMMEDIATE`` takes the database write lock up front,
    so the busy-timeout wait — if there is contention — happens once, at a single
    predictable point, rather than mid-transaction when a deferred transaction
    tries to upgrade a read lock to a write lock and can instead fail outright
    with ``SQLITE_BUSY``. A read-then-write caller is exactly where a deferred
    transaction bites; taking the lock eagerly removes that class of failure.

    On a clean exit the block's work is committed. If the block raises, the
    transaction is explicitly rolled back and the exception is re-raised
    unchanged. The explicit ``ROLLBACK`` is deliberate: closing the connection
    would also discard an open transaction, but relying on that would make
    correctness depend on a ``sqlite3_close`` implementation detail, and leaves
    the rollback invisible to a reader of this code. Either way, the connection
    is closed in ``finally`` — ``write_txn`` owns the connection it opens, so a
    leak here is a leaked file descriptor on every daemon cycle.

    Args:
        db_path: Path to the SQLite database file, forwarded to
            ``get_connection``. Required — there is no default path.

    Yields:
        The open connection, for the caller to execute statements on. Do not
        close it or issue ``COMMIT``/``ROLLBACK`` inside the block; that is this
        helper's job.

    Raises:
        Whatever the block raises (re-raised after ``ROLLBACK``), plus anything
        ``get_connection`` or the transaction statements themselves raise.
    """
    connection = get_connection(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
    finally:
        connection.close()
