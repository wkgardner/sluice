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
    "is_daemon_eligible",
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

    Always a fresh connection — a cached handle's transaction state would make an
    in-process object authoritative over the file, and the DB is the single
    source of truth (§5). Configured autocommit (only ``write_txn`` opens
    transactions), ``sqlite3.Row`` (column-keyed reads survive additive
    migrations), ``journal_mode=WAL`` (readers don't block on a writer), and
    ``synchronous=NORMAL``. Does not create the parent directory or schema: a
    factory that ran ``makedirs`` would stand up an empty DB next to a typo'd
    path.

    Args:
        db_path: Path to the SQLite file. Required — a missing path is a caller
            error, not grounds for a silent production fallback.

    Raises:
        sqlite3.OperationalError: The file cannot be opened (incl. a missing
            parent directory).
        sqlite3.DatabaseError: ``db_path`` is an existing non-SQLite file —
            the covering case (a *sibling* of ``OperationalError``, not a
            superclass), so catch it to catch everything this can raise.
    """
    # timeout=5.0 is already the sqlite3.connect default; passed for legibility
    # only (errata A1).
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

    Enforces §5's "short transactions only": the connection is autocommit, so
    ``write_txn`` is the only thing that opens a transaction and it always closes
    it. ``BEGIN IMMEDIATE`` takes the write lock up front, so contention waits
    once at a predictable point instead of failing ``SQLITE_BUSY`` mid-transaction
    on a lock upgrade. Commits on clean exit; rolls back and re-raises on error;
    closes either way.

    Yields:
        The open connection. Do not close it or issue COMMIT/ROLLBACK inside
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

    One complete authoritative ``CREATE TABLE``, no later ALTERs. Errata baked
    in: A2 ``lifecycle`` greenfield; A3 ``protected_at``/``protected_by`` net-new
    (python-poller's ``locked_at`` mutex deliberately not ported); A4
    ``globus_task_id`` and ``slack_thread_ts`` present from day one. The paired
    ``(x IS NULL) = (y IS NULL)`` CHECKs encode §5's "quarantined ⇔ error set"
    and the same for protection. No gate-satisfaction columns (mock: subsystem
    2's migration 005; production: Plan B) and no FOREIGN KEYs.
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
        "CREATE TABLE poller_metadata (    key   TEXT PRIMARY KEY,    value TEXT)"
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


def _migration_005_mock_gate_columns(conn: sqlite3.Connection) -> None:
    """Add the reference-gate satisfaction columns to ``runs`` (plan 02 §3.5).

    Owned by subsystem 2 (gate), not db — gate-column migration fns live in this
    ladder because ``initialize_database`` must work with ``sluice.gates`` absent
    (plan 02 §2 F3-revision; the precedent 006+ follows). ``INTEGER NOT NULL
    DEFAULT 0``, never nullable: a nullable satisfaction column gives reconcile
    three-valued logic (``WHERE col = 0`` silently misses NULL), and the constant
    default makes the ALTER legal on a populated table — pre-existing rows land
    unsatisfied, which is correct because they never ran the gates.
    """
    conn.execute(
        "ALTER TABLE runs ADD COLUMN mock_alpha_done INTEGER NOT NULL DEFAULT 0",
    )
    conn.execute(
        "ALTER TABLE runs ADD COLUMN mock_beta_done INTEGER NOT NULL DEFAULT 0",
    )


def _migration_006_excluded_gates(conn: sqlite3.Connection) -> None:
    """Add the per-run exclusion overlay column to ``runs`` (plan 08 ⚑2 / ADR-0004 d.3).

    Owned by subsystem 8 (control), living in this ladder per the same
    F3-revision precedent as 005. NULL means "no exclusions" — never ``'[]'``
    (no-fabrication), and pre-existing rows correctly land NULL because
    nothing was ever excluded from them; otherwise a sorted, deduplicated
    JSON array of gate ids written only by ``exclude_gate``/``include_gate``.
    The CHECK admits NULL or valid JSON; the array *shape* is enforced
    fail-loud by ``control.run_exclusions``, the sole parse site.
    """
    conn.execute(
        "ALTER TABLE runs ADD COLUMN excluded_gates TEXT "
        "CHECK (excluded_gates IS NULL OR json_valid(excluded_gates))",
    )


def _migration_007_labkey_registered(conn: sqlite3.Connection) -> None:
    """Add the ``experiment`` gate's satisfaction column to ``runs`` (plan 13 Unit 1).

    Owned by Plan B's first production gate, living in this ladder per the same
    F3-revision precedent as 005/006 (``initialize_database`` must work with
    ``sluice.gates`` absent). ONE column — ``labkey_registered`` — per ⚑3:
    migrations are INCREMENTAL, one satisfaction column per gate shipped with
    its gate, superseding gate-map §6's "one coherent migration 007". The six
    other production satisfaction columns are NOT added here; each arrives with
    its own gate/migration. The gate's ``experiment_id`` OUTPUT needs no column
    — it already exists from migration 001 (``db.py:148``, ADR-0006 consequence).

    ``INTEGER NOT NULL DEFAULT 0``, never nullable: a nullable satisfaction
    column gives reconcile three-valued logic (``WHERE col = 0`` silently misses
    NULL), and the constant default keeps the ALTER legal on a populated table —
    pre-existing rows land unsatisfied, which is correct because they never ran
    the gate.
    """
    conn.execute(
        "ALTER TABLE runs ADD COLUMN labkey_registered INTEGER NOT NULL DEFAULT 0",
    )


def _migration_008_downloaded(conn: sqlite3.Connection) -> None:
    """Add the ``download`` gate's satisfaction column to ``runs`` (plan 15 increment 2).

    ONE column — ``downloaded`` — per the incremental-migration policy (⚑3):
    each production gate ships its own migration alongside it, superseding
    gate-map §6's single coherent migration. The gate's *outputs* need no column:
    it produces ``raw_seqs/`` on disk, not a persisted token, so ``persists`` is
    empty and nothing here mirrors it.

    ``INTEGER NOT NULL DEFAULT 0`` for the same reason as 007: a nullable
    satisfaction column gives reconcile three-valued logic (``WHERE col = 0``
    silently misses NULL), and the constant default keeps the ALTER legal on a
    populated table — pre-existing rows land unsatisfied, which is correct
    because they never ran the gate.
    """
    conn.execute(
        "ALTER TABLE runs ADD COLUMN downloaded INTEGER NOT NULL DEFAULT 0",
    )


def _migration_009_checksummed(conn: sqlite3.Connection) -> None:
    """Add the ``checksum`` gate's satisfaction column to ``runs`` (plan 15 increment 3).

    ONE column — ``checksummed`` — per the incremental-migration policy (⚑3).
    The gate verifies the delivered manifest against the transferred files and
    persists no token: whether the data is intact is re-derivable by re-reading
    it, so ``persists`` is empty and nothing here mirrors an output.

    ``INTEGER NOT NULL DEFAULT 0``, as with 007/008 — a nullable satisfaction
    column gives reconcile three-valued logic, and the constant default keeps the
    ALTER legal on a populated table.
    """
    conn.execute(
        "ALTER TABLE runs ADD COLUMN checksummed INTEGER NOT NULL DEFAULT 0",
    )


def _migration_010_samplesheet_created(conn: sqlite3.Connection) -> None:
    """Add the ``samplesheet`` gate's satisfaction column to ``runs`` (plan 15 increment 6).

    ONE column — ``samplesheet_created`` — per the incremental-migration policy
    (⚑3). The gate writes ``samplesheet.csv`` into the run's staging tree and
    persists no token, so ``persists`` is empty and nothing here mirrors an output.

    ``INTEGER NOT NULL DEFAULT 0``, as with 007-009.
    """
    conn.execute(
        "ALTER TABLE runs ADD COLUMN samplesheet_created INTEGER NOT NULL DEFAULT 0",
    )


def _migration_011_nvd_completed(conn: sqlite3.Connection) -> None:
    """Add the ``nvd`` gate's satisfaction column to ``runs`` (plan 15 increment 7).

    ONE column — ``nvd_completed`` — per the incremental-migration policy (⚑3).

    **Column rename recorded (gate map §3):** production tracks the NVD stage with
    TWO columns, ``pipeline_started`` and ``pipeline_completed`` (PP
    ``poller_state_db.py:39-40``). sluice collapses both onto this single column,
    because a started-but-not-finished NVD run is not a distinct state sluice
    needs: the gate is satisfied or it is not, and an interrupted run resumes from
    Nextflow's own session cache rather than from a database flag. Anything
    grepping production for those two names maps both here.
    """
    conn.execute(
        "ALTER TABLE runs ADD COLUMN nvd_completed INTEGER NOT NULL DEFAULT 0",
    )


def _migration_012_profiling_completed(conn: sqlite3.Connection) -> None:
    """Add the ``profiling`` gate's satisfaction column to ``runs``.

    ONE column per the incremental-migration policy (⚑3). The ``profiling`` gate
    (silly-fast-bfx, order 4) is sluice-only — production has no counterpart
    column, so there is no rename to record. ``INTEGER NOT NULL DEFAULT 0``, as
    with 007-011: pre-existing rows land unsatisfied, which is correct because
    they never ran the gate. (Whether an old row is *asked* to run it is decided
    by its stamped profile, not by this column — see ``sluice/gates/profiles.py``.)
    """
    conn.execute(
        "ALTER TABLE runs ADD COLUMN profiling_completed INTEGER NOT NULL DEFAULT 0",
    )


def _migration_013_authority_registered(conn: sqlite3.Connection) -> None:
    """Add the ``registration`` gate's satisfaction column to ``runs``.

    ONE column per the incremental-migration policy (⚑3). The name is python-poller's
    own (gate map §3), so it keeps its production meaning: this run's samples went
    through authority-list registration. ``INTEGER NOT NULL DEFAULT 0``, as with
    007-012: pre-existing rows land unsatisfied, which is correct because they never
    ran the gate. (Whether an old row is *asked* to run it is decided by its stamped
    profile — only ``production-v3.3`` and later list ``registration``.)
    """
    conn.execute(
        "ALTER TABLE runs ADD COLUMN authority_registered INTEGER NOT NULL DEFAULT 0",
    )


# (version, name, apply_fn) each. The framework (unit 4) runs each apply_fn in its
# own BEGIN IMMEDIATE/COMMIT and records the version only on success, so the
# apply_fns issue plain DDL and touch neither the transaction nor the ledger.
# Later subsystems append 007+ (Plan B's production gate columns — plan 08 §2,
# F-AUD1 superseded); a shipped migration is never edited.
MIGRATIONS: tuple[Migration, ...] = (
    (1, "001_runs", _migration_001_runs),
    (2, "002_poller_metadata", _migration_002_poller_metadata),
    (3, "003_control_audit", _migration_003_control_audit),
    (4, "004_idx_runs_eligible", _migration_004_idx_runs_eligible),
    (5, "005_mock_gate_columns", _migration_005_mock_gate_columns),
    (6, "006_excluded_gates", _migration_006_excluded_gates),
    (7, "007_labkey_registered", _migration_007_labkey_registered),
    (8, "008_downloaded", _migration_008_downloaded),
    (9, "009_checksummed", _migration_009_checksummed),
    (10, "010_samplesheet_created", _migration_010_samplesheet_created),
    (11, "011_nvd_completed", _migration_011_nvd_completed),
    (12, "012_profiling_completed", _migration_012_profiling_completed),
    (13, "013_authority_registered", _migration_013_authority_registered),
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
    """Apply every pending migration in ascending order.

    Creates the ledger (``schema_migrations``, the ``000_bootstrap`` of §4) if
    absent. Each migration runs in its **own** ``BEGIN IMMEDIATE``…``COMMIT``
    with its version recorded only after its body succeeds — a failure records
    nothing and leaves earlier migrations committed. Idempotent: a second run
    applies nothing.

    Returns:
        The versions applied, ``[]`` if none were pending.

    Raises:
        SchemaVersionError: The DB records a version newer than
            ``max(MIGRATIONS)`` (migrations are forward-only), or ``MIGRATIONS``
            itself is non-contiguous.
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
    """Open ``db_path``, apply all pending migrations, and close.

    Passes the connection straight to ``apply_migrations`` — **never** through
    ``write_txn`` (F2 nesting rule), since ``apply_migrations`` owns a
    ``BEGIN IMMEDIATE`` per migration and an outer transaction would raise
    ``cannot start a transaction within a transaction``.

    Returns:
        The versions applied, ``[]`` if none were pending.

    Raises:
        SchemaVersionError: See ``apply_migrations``.
        sqlite3.OperationalError: ``db_path`` cannot be opened (see
            ``get_connection``).
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

    "Stamp once, never change": the first sighting of ``name`` lands an
    ``active`` row with the ``pipeline_version`` it was born with (§8 — frozen at
    detection); every later sighting is a no-op returning ``False``. Uses
    ``ON CONFLICT(name) DO NOTHING``, **not** ``INSERT OR IGNORE`` — the latter
    suppresses every constraint class, silently reporting a bad row as "already
    exists". The caller owns the connection, so a detection sweep can batch
    inside one ``write_txn``.

    Args:
        name: Run identity — the human-typed folder name (F1).
        pipeline_version: Keyword-only, required — no baked-in default.
        detected_at: Defaults to ``now_iso()``.

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
    Security-critical: column identifiers can't be bound as parameters, so every
    key is whitelisted against the live ``runs`` columns *before* any SQL is
    built, and every value is a bound parameter — SQL in a value is stored
    literally, not executed. Empty ``fields`` is a ``False`` no-op. Caller
    hazard: ``UnknownColumnError`` subclasses ``KeyError``, so a bare
    ``except KeyError`` around ``update_run`` would swallow a caller bug.

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

    cursor = conn.execute(f"UPDATE runs SET {assignments} WHERE name = ?", parameters)
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
#
# TWIN (plan 14): ``is_daemon_eligible`` below is the Python half of this exact
# rule. SQLite cannot call a Python function, so the pair is irreducible and the
# two must be edited together — ``tests/test_predicate_twins.py`` fails if one
# changes without the other.
ACTIVE_RUNS_WHERE = "lifecycle = 'active' AND errored_at IS NULL"


def is_daemon_eligible(run_row) -> bool:
    """Return whether the daemon may work on this run — the Python twin of ``ACTIVE_RUNS_WHERE``.

    The single canonical eligibility rule (F4) as a callable: operator-eligible
    (``lifecycle == 'active'``) and not quarantined (``errored_at is None``).
    Held, skipped, and quarantined runs all answer ``False``; a *completed* run
    still answers ``True``, because ``completed_at`` is deliberately no part of
    the rule (see ``ACTIVE_RUNS_WHERE`` above for why).

    TWIN: ``ACTIVE_RUNS_WHERE`` is the SQL half of this same rule, used by
    ``select_active_runs`` to filter inside the database. SQLite cannot call a
    Python function, so the two cannot be merged — they are edited together or
    not at all, and ``tests/test_predicate_twins.py`` runs both over the same
    rows to prove they still agree. Before plan 14 this half was hand-copied
    into ``daemon.process_run`` and ``control._quiescence_refusal_why``; both
    now call here.

    Args:
        run_row: any mapping-like row exposing ``lifecycle`` and ``errored_at``
            by subscript — a ``sqlite3.Row`` in production, a plain dict in
            tests.

    Returns:
        ``True`` iff the run is daemon-eligible.
    """
    return run_row["lifecycle"] == "active" and run_row["errored_at"] is None


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

    One plain upsert statement (insert or replace, no duplicate, no
    ``IntegrityError`` on a repeat) that needs no transaction of its own (F2);
    callers may batch it inside ``write_txn`` — control does, so a metadata
    write and its audit row land atomically (plan 08 F7). Key-agnostic like
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
