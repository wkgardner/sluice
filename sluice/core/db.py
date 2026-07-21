"""SQLite state store primitives for sluice.

The database is the single source of truth for run state (design of record §5).
This module owns the low-level primitives every other subsystem builds on; it
knows nothing about gates, profiles, or the daemon loop.

One-way dependency ``gates -> core``: nothing under ``sluice.core`` imports
``sluice.gates``/``api``/``cli``, so this module is stdlib-only
(``tests/test_dependency_arrows.py`` enforces it). ``__all__`` is the declared
public surface; later units assert against it, so a name is public only if
listed there.
"""

import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone

__all__ = [
    "MIGRATIONS",
    "SchemaVersionError",
    "UnknownColumnError",
    "all_metadata",
    "apply_migrations",
    "get_connection",
    "get_metadata",
    "get_run",
    "initialize_database",
    "insert_run",
    "now_iso",
    "schema_version",
    "select_active_runs",
    "set_metadata",
    "update_run",
    "write_txn",
]


def now_iso() -> str:
    """Current instant as an ISO-8601 string with an explicit UTC offset.

    Every timestamp column is written with this. UTC (not local) is the one
    decision that matters: two hosts write these columns and SQLite orders them
    lexically, so a mixed-offset column would misorder ``ORDER BY detected_at``.
    """
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a fresh SQLite connection to ``db_path``, configured for this system.

    A *fresh* connection every call — no cached module-level handle, since the DB
    is the single source of truth and a shared connection's transaction state
    would make an in-process object authoritative over the file.

    Configuration:

    * ``isolation_level=None`` (autocommit) — Python's default opens an implicit
      transaction on first DML and holds the write lock, the long-lived write
      transaction §5 forbids. Batching is opt-in via ``write_txn``.
    * ``row_factory=sqlite3.Row`` — rows read by column name; positional reads
      would silently return the wrong column after an additive migration.
    * ``journal_mode=WAL`` — readers don't block on a writer.
    * ``synchronous=NORMAL`` — the durability pragma python-poller leaves unset
      (SQLite defaults to ``2``/FULL). Both pragmas do real work; dropping either
      is observable.

    ``timeout=5.0`` is passed for legibility only (errata A1): it is already the
    ``sqlite3.connect`` default, so ``busy_timeout`` is 5000 ms either way and
    omitting it behaves identically — it is not a fix and buys no contention win.

    Does not create the parent directory or schema: a factory that ran
    ``makedirs`` would stand up an empty DB next to a typo'd path.

    Args:
        db_path: Path to the SQLite file. Required — a missing path is a caller
            error, not grounds for a silent production fallback.

    Raises:
        sqlite3.OperationalError: The file cannot be opened (incl. a missing
            parent directory) — raised by ``connect`` before anything is built.
        sqlite3.DatabaseError: ``db_path`` is an existing non-SQLite file;
            ``connect`` succeeds and the first pragma fails. This is the covering
            case (and a *sibling* of ``OperationalError``, not a superclass), so
            catch it if you want to catch everything this can raise.
    """
    connection = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        # A pragma can raise after connect() handed us a live handle (a corrupt
        # file connects fine, then fails here); without closing, a daemon that
        # logs exc_info and retries leaks an fd per cycle. Intentionally untested
        # — the only probes (a connect spy, an fd count under /proc) pin the call
        # graph or don't run on macOS. Do not "fix" that with a spy.
        connection.close()
        raise
    return connection


@contextmanager
def write_txn(db_path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    """Run a block inside one short, explicit write transaction, then tear it down.

    How §5's "short transactions only" is enforced: the connection is autocommit,
    so ``write_txn`` is the only thing that opens a transaction and it always
    closes it. A transaction outliving its block would hold the write lock and
    starve the daemon and every CLI op.

    Uses ``BEGIN IMMEDIATE`` (not deferred ``BEGIN``): it takes the write lock up
    front, so contention waits once at a predictable point instead of a
    read-then-write caller failing ``SQLITE_BUSY`` mid-transaction on a lock
    upgrade. Commits on clean exit; on error rolls back explicitly (not relying on
    close's implicit discard) and re-raises; closes in ``finally`` either way.

    Yields the open connection. Do not close it or issue COMMIT/ROLLBACK inside
    the block.
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


class SchemaVersionError(RuntimeError):
    """Schema and code cannot be reconciled forward-only.

    Raised when the DB records a version higher than any known migration (written
    by a newer sluice — migrations are forward-only, so refuse rather than corrupt
    state), or when ``MIGRATIONS`` itself is non-contiguous/repeated.
    """


# (version, name, apply_fn): an integer version, the name recorded in the ledger,
# and a function that applies the schema change to a connection already in a txn.
Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _migration_001_runs(conn: sqlite3.Connection) -> None:
    """Create ``runs`` — the single source of truth for run state (§4).

    One complete authoritative ``CREATE TABLE`` (no later ALTERs). Errata baked
    in: A2 ``lifecycle`` greenfield (``NOT NULL DEFAULT 'active'`` + CHECK); A3
    ``protected_at``/``protected_by`` net-new and python-poller's ``locked_at``
    mutex deliberately not ported; A4 ``globus_task_id`` present from day one (its
    absence re-submits a duplicate multi-TB transfer), ``slack_thread_ts``
    likewise. The paired ``(x IS NULL) = (y IS NULL)`` CHECKs are total (never
    NULL) and encode §5's "quarantined ⇔ error set" and the same for protection.
    No gate-satisfaction columns (mock: subsystem 2's migration 005; production:
    Plan B) and no FOREIGN KEYs.
    """
    conn.execute(
        "CREATE TABLE runs ("
        "    name              TEXT    PRIMARY KEY,"
        "    experiment_id     INTEGER,"
        "    size_tb           REAL,"
        "    detected_at       TEXT    NOT NULL,"
        "    pipeline_version  TEXT    NOT NULL,"
        "    completed_at      TEXT,"
        "    lifecycle         TEXT    NOT NULL DEFAULT 'active',"
        "    lifecycle_by      TEXT,"
        "    lifecycle_at      TEXT,"
        "    lifecycle_reason  TEXT,"
        "    error_reason      TEXT,"
        "    errored_at        TEXT,"
        "    run_from_step     TEXT,"
        "    run_from_fresh    TEXT,"
        "    protected_at      TEXT,"
        "    protected_by      TEXT,"
        "    globus_task_id    TEXT,"
        "    slack_thread_ts   TEXT,"
        "    CHECK (lifecycle IN ('active', 'held', 'skipped')),"
        "    CHECK ((error_reason IS NULL) = (errored_at IS NULL)),"
        "    CHECK ((protected_at IS NULL) = (protected_by IS NULL))"
        ")"
    )


def _migration_002_poller_metadata(conn: sqlite3.Connection) -> None:
    """Create ``poller_metadata`` — a bare key/value store for global daemon state.

    ``db`` creates only the shape and names no keys (``paused``,
    ``pipeline_profile``, disk-halt flag belong to control/daemon). ``key`` is the
    PK so callers can ``ON CONFLICT(key) DO UPDATE``.
    """
    conn.execute(
        "CREATE TABLE poller_metadata ("
        "    key   TEXT PRIMARY KEY,"
        "    value TEXT"
        ")"
    )


def _migration_003_control_audit(conn: sqlite3.Connection) -> None:
    """Create ``control_audit`` — the append-only audit trail (§4).

    Table only; writing rows is subsystem 7. ``id`` is AUTOINCREMENT so audit ids
    strictly increase and are never recycled (the trail can't order on wall-clock
    ``ts``, which an NTP step can move backwards). ``target_run`` is nullable
    (global ops have no target); no FK to ``runs`` so the trail survives deletes.
    """
    conn.execute(
        "CREATE TABLE control_audit ("
        "    id          INTEGER PRIMARY KEY AUTOINCREMENT,"
        "    ts          TEXT NOT NULL,"
        "    actor       TEXT NOT NULL,"
        "    action      TEXT NOT NULL,"
        "    target_run  TEXT,"
        "    args_json   TEXT,"
        "    result      TEXT"
        ")"
    )


def _migration_004_idx_runs_eligible(conn: sqlite3.Connection) -> None:
    """Index ``runs (lifecycle, errored_at)`` for the eligibility predicate (§4, F4).

    Supports ``lifecycle = 'active' AND errored_at IS NULL``. No ``detected_at``
    index (python-poller's indexes a column constant across every row).
    """
    conn.execute("CREATE INDEX idx_runs_eligible ON runs (lifecycle, errored_at)")


# (version, name, apply_fn) each. The framework (unit 4) runs each apply_fn in its
# own BEGIN IMMEDIATE/COMMIT and records the version only on success, so the
# apply_fns issue plain DDL and touch neither the transaction nor the ledger.
# Later subsystems append 005+; a shipped migration is never edited.
MIGRATIONS: tuple[Migration, ...] = (
    (1, "001_runs", _migration_001_runs),
    (2, "002_poller_metadata", _migration_002_poller_metadata),
    (3, "003_control_audit", _migration_003_control_audit),
    (4, "004_idx_runs_eligible", _migration_004_idx_runs_eligible),
)


def schema_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration version, or ``0`` on a fresh DB.

    Must tolerate a missing ``schema_migrations`` (``apply_migrations`` asks
    before creating the ledger) and an empty ledger — both report ``0`` rather
    than raising ``no such table``.
    """
    ledger_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if ledger_exists is None:
        return 0
    highest = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
    return highest if highest is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration in ascending order; return the versions applied.

    Creates the ledger (``schema_migrations``, the ``000_bootstrap`` of §4) if
    absent. Each migration runs in its **own** ``BEGIN IMMEDIATE``…``COMMIT`` with
    its version recorded only after its body succeeds — so a failure records
    nothing and leaves earlier migrations committed (guarded by
    ``test_failed_migration_does_not_record_its_version``). A version higher than
    ``max(MIGRATIONS)`` raises ``SchemaVersionError`` (forward-only). Returns
    ``[]`` on an idempotent second run.
    """
    # Validate MIGRATIONS at runtime (not only via the structural test), so the
    # tuple is self-validating however assembled — a later subsystem appending
    # 005, or a monkeypatched tuple. Cheap, and closes a gap no behavioural test
    # covers (a sorted-but-non-contiguous MIGRATIONS).
    versions = [version for version, _name, _apply in MIGRATIONS]
    if versions != list(range(1, len(versions) + 1)):
        raise SchemaVersionError(
            f"MIGRATIONS versions must be contiguous 1..N with no repeats; got {versions}"
        )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "    version    INTEGER PRIMARY KEY,"
        "    name       TEXT NOT NULL,"
        "    applied_at TEXT NOT NULL"
        ")"
    )

    current_version = schema_version(conn)
    highest_known = max(versions) if versions else 0
    if current_version > highest_known:
        raise SchemaVersionError(
            f"database schema version {current_version} is newer than this code "
            f"knows about (highest migration is {highest_known}); refusing to run "
            "against a schema written by a newer sluice"
        )

    applied_versions: list[int] = []
    for version, name, apply in MIGRATIONS:
        if version <= current_version:
            continue
        # One migration per transaction: record the ledger row only after the
        # body succeeds; roll back this migration alone on failure.
        conn.execute("BEGIN IMMEDIATE")
        try:
            apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (version, name, now_iso()),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        applied_versions.append(version)

    return applied_versions


def initialize_database(db_path: str | os.PathLike[str]) -> list[int]:
    """Open ``db_path``, apply all pending migrations, close, and return versions.

    Passes the connection straight to ``apply_migrations`` — **never** through
    ``write_txn`` (F2 nesting rule), since ``apply_migrations`` owns a
    ``BEGIN IMMEDIATE`` per migration and an outer transaction would raise
    ``cannot start a transaction within a transaction``.
    """
    connection = get_connection(db_path)
    try:
        return apply_migrations(connection)
    finally:
        connection.close()


class UnknownColumnError(KeyError):
    """``update_run`` was asked to write a column ``runs`` does not have.

    The injection-defense failure signal, not just a typo report: column
    identifiers can't be bound as parameters, so ``update_run`` whitelists keys
    against the live columns and raises this for anything off the list rather than
    interpolating it into the SQL.
    """


def insert_run(
    conn: sqlite3.Connection,
    name: str,
    *,
    pipeline_version: str,
    detected_at: str | None = None,
    experiment_id: int | None = None,
    size_tb: float | None = None,
) -> bool:
    """Insert a newly-detected run once; return whether a row was created.

    "Stamp once, never change": the first sighting of ``name`` lands an ``active``
    row with the ``pipeline_version`` it was born with (§8 — frozen at detection);
    every later sighting is a no-op returning ``False``. The caller owns the
    connection, so a detection sweep can batch inside one ``write_txn``.

    Uses ``ON CONFLICT(name) DO NOTHING``, **not** ``INSERT OR IGNORE``: the latter
    suppresses every constraint class, so a bad row (e.g. NULL ``pipeline_version``)
    would be silently reported as "already exists" — the blanket suppression this
    subsystem condemns in python-poller. ``DO NOTHING`` suppresses only the PK
    conflict and lets genuine violations raise.

    Args:
        conn: Caller-supplied autocommit connection.
        name: Run identity — the human-typed folder name (F1).
        pipeline_version: Keyword-only, required (no baked-in default; omitting is
            a ``TypeError``).
        detected_at: Defaults to ``now_iso()``.
        experiment_id: Optional, nullable (B2 is Plan B's).
        size_tb: Optional.

    Raises:
        sqlite3.IntegrityError: On a NOT NULL/CHECK violation — never swallowed.
    """
    cursor = conn.execute(
        "INSERT INTO runs (name, pipeline_version, detected_at, experiment_id, size_tb) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO NOTHING",
        (
            name,
            pipeline_version,
            detected_at if detected_at is not None else now_iso(),
            experiment_id,
            size_tb,
        ),
    )
    return cursor.rowcount > 0


def get_run(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Return the ``runs`` row for ``name`` (column-keyed), or ``None`` on a miss.

    Consumers branch on ``get_run(...) is None``, so a miss is ``None`` — never an
    empty row, never an exception.
    """
    return conn.execute("SELECT * FROM runs WHERE name = ?", (name,)).fetchone()


def update_run(
    conn: sqlite3.Connection, name: str, fields: Mapping[str, object]
) -> bool:
    """Write named columns of one run; return whether a row matched.

    A primitive, not a policy: which columns may change and which must move
    together is enforced by ``set_gate`` (B2) and the schema's CHECKs, not here.

    Security-critical: identifiers can't be bound, so the ``SET`` clause is built
    from column names. Every key is whitelisted against the live ``runs`` columns
    (``PRAGMA table_info``) and anything off the list raises ``UnknownColumnError``
    *before* any SQL is built; every **value** is a bound parameter, so SQL in a
    value is stored literally, not executed. Empty ``fields`` is a ``False`` no-op.

    Caller hazard: ``UnknownColumnError`` subclasses ``KeyError``, so a bare
    ``except KeyError`` around ``update_run`` (e.g. in subsystem-8 ``set_gate``)
    would swallow it — it signals a caller bug, not a recoverable condition.

    Raises:
        UnknownColumnError: A key is not a live ``runs`` column.
        sqlite3.IntegrityError: The write violates a CHECK (e.g. ``errored_at``
            without ``error_reason``).
    """
    if not fields:
        return False

    valid_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    unknown_columns = [column for column in fields if column not in valid_columns]
    if unknown_columns:
        raise UnknownColumnError(
            f"update_run refuses unknown column(s) {sorted(unknown_columns)}: "
            "not among the runs table's columns"
        )

    # Identifiers double-quoted defense-in-depth (redundant against today's plain
    # columns, but a future column named after a SQLite keyword would else emit a
    # syntax error); the whitelist above is the actual injection defense.
    columns = list(fields)
    assignments = ", ".join(f'"{column}" = ?' for column in columns)
    parameters = [fields[column] for column in columns]
    parameters.append(name)

    cursor = conn.execute(
        f"UPDATE runs SET {assignments} WHERE name = ?", parameters
    )
    return cursor.rowcount > 0


# The single canonical eligibility predicate (F4): operator-eligible
# (``lifecycle = 'active'``) and not quarantined (``errored_at IS NULL``). The two
# clauses are orthogonal and AND-ed, written as bare ``column op literal`` so
# ``idx_runs_eligible`` can serve them. NOT exported (internal constant). It
# deliberately omits ``completed_at IS NULL``: ``completed_at`` is a cache
# reconcile re-derives every look, so filtering on it could hide a run (stale
# stamp, reset_gate) from the loop meant to notice; the accepted cost is that
# completed runs are returned forever and reconcile skips them cheaply. A
# ``completed_at`` pre-filter, if wanted, is a separate query, never a widening
# of this constant.
ACTIVE_RUNS_WHERE = "lifecycle = 'active' AND errored_at IS NULL"


def select_active_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Operator-eligible, non-quarantined runs in ``(detected_at, name)`` order.

    Matches ``ACTIVE_RUNS_WHERE``; the ``name`` tiebreak makes ordering total and
    deterministic when detection timestamps collide. Includes completed runs by
    design (F4 — see ``ACTIVE_RUNS_WHERE``); reconcile skips them with a cheap
    pure call rather than a SQL filter on a derived cache.
    """
    return conn.execute(
        f"SELECT * FROM runs WHERE {ACTIVE_RUNS_WHERE} ORDER BY detected_at, name"
    ).fetchall()


def get_metadata(
    conn: sqlite3.Connection, key: str, default: str | None = None
) -> str | None:
    """Return ``poller_metadata[key]``, or ``default`` on a miss.

    Key-agnostic (``db`` names no keys; their meaning belongs to control/daemon).
    A miss is distinguishable only if the caller supplies a sentinel ``default``.
    """
    row = conn.execute(
        "SELECT value FROM poller_metadata WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row is not None else default


def set_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert ``key`` → ``value`` in ``poller_metadata``.

    One autocommit upsert (insert or replace, no duplicate, no ``IntegrityError``
    on a repeat). The connection is autocommit (F2), so this leaves no open
    transaction — do not wrap it in ``write_txn``. Key-agnostic like
    ``get_metadata``.
    """
    conn.execute(
        "INSERT INTO poller_metadata (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def all_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    """Return the entire ``poller_metadata`` store as a ``{key: value}`` dict."""
    return {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM poller_metadata")
    }
