# Plan 01 — `db` subsystem (Plan A, subsystem 1 of 9)

**Module owned:** `sluice/core/db.py` (single file)
**Build order:** `db` → gate (+ mock gate) → events → context → profile → reconcile → audit → control → daemon
**Status:** revision 2, addressing `plan-reviewer` REVISE (2 blockers, 3 majors, 3 minors, 2 nits).
F1/F2/F5 confirmed unchanged; **F3 resolved by the human** (migration 005 moves to subsystem 2);
F4 rationale rewritten.

Ground truth: design of record §5 (state model), §2 (spine), §7 (loop);
errata A1/A2/A3/A4; subsystem map (PP = python-poller `mizzou_fastq_automated_download`).

---

## 1. Scope & non-goals

### Owned

| Thing | Public surface |
|---|---|
| Connection factory | `get_connection(db_path)` |
| Clock helper | `now_iso()` |
| Short-transaction helper | `write_txn(db_path)` (context manager) |
| Forward-only migrations | `MIGRATIONS`, `schema_version(conn)`, `apply_migrations(conn)`, `initialize_database(db_path)` |
| Schema | `schema_migrations`, `runs`, `poller_metadata`, `control_audit`, `idx_runs_eligible` |
| Row primitives | `insert_run`, `get_run`, `update_run`, `select_active_runs` |
| Metadata primitives | `get_metadata`, `set_metadata`, `all_metadata` |
| Errors | `SchemaVersionError`, `UnknownColumnError` |

`sluice/core/db.py` declares an explicit `__all__` naming exactly the surface above. Several
scope guards in §5/§6 assert against `__all__` rather than `dir()`.

### Explicitly NOT owned (and where it belongs)

- The `Gate` contract, `GateResult`, `GateOutcome`, `Gate.order` — **subsystem 2 (`gate`)**.
- **The reference/mock gates *and their satisfaction columns*** — **subsystem 2 (`gate`)**, which
  ships its own `MIGRATIONS` entry 005 adding them. `db` deliberately contains no gate names;
  design §4 is explicit that the daemon (and by extension core) "knows the contract, not the
  gates." What `db` owes subsystem 2 is the migration *framework* (unit 4) and the proof that an
  additive migration works on a populated database (unit 8).
- Wide events, `Notifier` Protocol, B8/B9 — **subsystem 3 (`events`)**.
- `RunContext` / `resolve_context` — **subsystem 4 (`context`)**.
- Profiles, applicability, `pipeline_version` *semantics* — **subsystem 5 (`profile`)**.
  `db` only stores the string.
- `next_gate`, "is this run complete", stamping `completed_at` — **subsystem 6 (`reconcile`)**.
- *Writing* `control_audit` rows — **subsystem 7 (`audit`)**. `db` creates the table and nothing else.
- `pause`/`hold`/`skip`/`set_gate`/`reset_gate`/`lock`, `ProtectedError`, actor policy —
  **subsystem 8 (`control`)**.
- Detection, `fcntl.flock` single-instance, disk floor, the loop — **subsystem 9 (`daemon`)**.
- Config loading, directory provisioning — **`sluice/config`**.
  Note PP `poller_state_db.py:78` does `os.makedirs()` inside DB init; sluice does **not**
  (unit 2 asserts the failure instead).
- Production gate columns (`download_complete`, `checksum_ok`, `merged`, `nvd_done`,
  `transferred`, `registered`, …) and the 6-vs-8-gate question — **Plan B / errata B1**.
  Added by *migration*, never by editing an existing one.

---

## 2. Errata in scope — decisions taken

### A1 — `busy_timeout` is already 5000; `synchronous` is the real change. RESOLVED.

`get_connection` sets `PRAGMA journal_mode=WAL` **and `PRAGMA synchronous=NORMAL`**.
`busy_timeout` is *not* set by a pragma statement; instead `sqlite3.connect(..., timeout=5.0)`
is passed **explicitly** so the value is legible in the source rather than inherited from a
stdlib default.

**Wording that must appear in the docstring and must NOT be exceeded:** the busy timeout is
made explicit *for legibility*. It is **not** a bug fix and buys **no** lock-contention
improvement — PP already measures `PRAGMA busy_timeout = 5000` (errata A1). Any docstring,
comment, or test name implying otherwise is a review defect.

The genuinely-unset pragma is `synchronous` (PP measures `2`/FULL; there is exactly one PRAGMA
statement in the whole PP codebase, `poller_state_db.py:62-65`). `NORMAL` (=1) is safe under WAL.

### A2 — `lifecycle` is 100% greenfield. RESOLVED.

There are no `held_*`/`skipped_*` columns in PP to collapse; the design's word "collapses" is
wrong. Budget is for building it from nothing:
`lifecycle TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active','held','skipped'))`
plus `lifecycle_by`, `lifecycle_at`, `lifecycle_reason`, plus the eligibility predicate.

PP's only eligibility filter is `errored_at IS NULL` (`poller_state_db.py:247`). Sluice's is
**`lifecycle = 'active' AND errored_at IS NULL`** — see unit 12 and fork F4.

### A3 — operator protection is new; the per-run mutex is dropped. RESOLVED.

- `protected_at TEXT`, `protected_by TEXT` are **net-new**, and are what `lock`/`unlock` write.
- **`locked_at` is not ported at all.** In PP it is the daemon's own advisory mutex
  (`acquire_folder_lock`, `poller_state_db.py:591-615`, taken at `poller.py:1662`/`:1736`).
  Single-daemon `fcntl.flock` (design §5) makes it redundant, and PP has **no stale-lock
  reaper** — a SIGKILL wedges a run forever. Reusing it for operator protection would let an
  operator `lock` collide with in-flight processing.
- Unit 5 asserts `locked_at` is **absent** from the schema, so a well-meaning port cannot
  reintroduce it.

### A4 — `globus_task_id` column exists from day one. RESOLVED.

`globus_task_id TEXT` is created in migration 001 even though **nothing in Plan A writes it**.
It does not exist in PP (parsed at `globus_helpers.py:72-75`, logged at `poller.py:1265-1267`,
then discarded), and its absence means a restart re-submits a duplicate multi-TB transfer.
Plan B populates it *immediately after submit, before the wait*. `slack_thread_ts TEXT`
likewise (non-recomputable, errata A5).

### Errata deliberately NOT resolved here

- **B1** (6 vs 8 gates), **B2** (`experiment_id` chicken-and-egg), **B3** (`Gate.order` from true
  execution order) — Plan B / subsystem 2. This plan's only obligation is that the schema is
  **additive**: `experiment_id INTEGER` is nullable, and every gate column arrives by
  `ALTER TABLE ... ADD COLUMN` in a new migration.
- **B4** (retry/quarantine policy). The *schema* consequence — an `attempts` column — is
  deferred to whichever migration the `gate`/`reconcile` plan adds, because a column no code
  reads is worse than a later additive migration. **Recommendation to carry forward:** add
  `retryable: bool` + `max_attempts` to `GateResult`/`Gate` (PP has live policy:
  `TRANSIENT_FAILURE_PREFIXES=("File download",)`, `poller.py:1288`; `max_download_retries=3`,
  `poller.py:124,1362`). If subsystem 2 instead accepts all-failures-quarantine, it must delete
  `reset_attempts` from the op list and no `attempts` column is ever added. `db` is neutral.
- **B5/B6/B8/B9** — daemon / gate / events.

---

## 3. Forks

**F1 — run identity: `name TEXT PRIMARY KEY`, no surrogate integer id.** *(Confirmed by review.)*
Design §5 says "Identity: `id`/`name`" without deciding. PP keys on `folder_name`. Control ops
target runs by the human-typed name (`sluice hold RUN`), the API path segment is that name, and
`control_audit.target_run` is that name. A surrogate id would require every op to resolve
name→id first for zero benefit. SQLite's implicit `rowid` remains for ordering.

**F2 — connections are autocommit (`isolation_level=None`); all in-process write *sequences* go
through `write_txn`, which issues explicit `BEGIN IMMEDIATE` / `COMMIT`.** *(Confirmed by review.)*
Python's default `isolation_level=""` opens implicit transactions on DML and leaves them open
until commit — the exact failure mode design §5's "short transactions only" forbids.
`BEGIN IMMEDIATE` takes the write lock at `BEGIN` rather than at first write, so a busy-timeout
wait happens once, up front.

> **Nesting rule (do not get this wrong).** `initialize_database` opens its connection with
> `get_connection` and **must NOT wrap it in `write_txn`**: `apply_migrations` issues its own
> `BEGIN IMMEDIATE` / `COMMIT` around *each* migration, and nesting the two raises
> `sqlite3.OperationalError: cannot start a transaction within a transaction`. Likewise the row
> primitives (units 9–13) take a caller-supplied connection and issue single autocommit
> statements; the *caller* decides whether to batch them inside a `write_txn`.

**F3 — reference/mock gate columns are subsystem 2's migration 005, not `db`'s.**
**RESOLVED BY THE HUMAN; revision 1's framing was wrong.** The fork is not "core migration vs
test-only" — test-only is correctly dead, because integration tests must hit the real schema.
The real question is *which subsystem owns it*, and the answer is `gate`: putting the literal
strings `mock_alpha_done` / `mock_beta_done` in `sluice/core/db.py` would have core naming
specific gates, contradicting design §4 and this plan's own §1. `db` therefore ships
`MIGRATIONS` **001–004** and stops; subsystem 2 appends 005 using the framework from unit 4 and
the populated-database guarantee from unit 8.

**F4 — the eligibility predicate is exactly two clauses: `lifecycle='active' AND errored_at IS
NULL`.** It does **not** filter `completed_at IS NULL`.

*Rationale (rewritten — revision 1's reason was false).* `completed_at` is not unknowable to
SQL: it is reconcile's own recorded conclusion, already computed against the run's frozen
`pipeline_version` and materialized into the row, so filtering on it needs no profile knowledge.
The honest reasons are:
1. **One canonical eligibility predicate.** `ACTIVE_RUNS_WHERE` means "this run is
   operator-eligible and not quarantined" — one sentence, one constant, quoted identically by
   daemon, control and status. Folding a progress-derived clause into it makes the constant mean
   two things.
2. **Eligibility does not trust a derived cache.** `completed_at` is a *cached conclusion*.
   Reconcile re-derives completeness from the gate columns every look (design §2.4, stateless
   between looks); if the cache and the columns ever disagree — a stale stamp, a `reset_gate` on
   a completed run — the SQL filter would hide the run from the very loop whose job is to notice.

*The cost, stated plainly:* `select_active_runs` returns **every completed run forever**, so
per-cycle work grows unbounded with the lifetime row count. Design §7's loop text says "and not
complete", and this predicate does not deliver that clause. **Accepted mitigation:** reconcile's
skip is a pure function call on an already-fetched row — cheap at the tens-to-hundreds of runs
sluice actually sees (PP: 69 rows). **A `completed_at IS NULL` pre-filter is a legitimate future
optimization and is forbidden by no invariant**; it should be added as a separate scoped query
(e.g. `select_reconcilable_runs`) rather than by widening `ACTIVE_RUNS_WHERE`.

*Precedence question* raised by the subsystem map (line 590) is hereby answered: **`lifecycle`
and `errored_at` are orthogonal AND-ed filters.** A run that is both held and errored needs
*both* `unhold` and `clear_error` to become eligible; `clear_error` on a held run does **not**
make it eligible.

**F5 — `detected` does not survive as a column.** *(Confirmed by review.)* Row existence implies
detection (PP inserts `detected=1` unconditionally, `poller_state_db.py:428-429`, so no row ever
has `0`). Only `detected_at TEXT NOT NULL` is kept — design §5's "detection timestamp".

---

## 4. The schema (authoritative)

The `CREATE TABLE` must be **complete and authoritative** — unlike PP, where `slack_thread_ts`
exists only via an ALTER (`poller_state_db.py:144`) and the CREATE is a lie.

```sql
-- 000_bootstrap  (created by apply_migrations itself, before any MIGRATIONS entry runs;
--                 it is NOT a MIGRATIONS entry, it is the ledger the ladder reads)
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

-- 001_runs
CREATE TABLE runs (
    name              TEXT    PRIMARY KEY,
    experiment_id     INTEGER,                      -- nullable: B2 unresolved, Plan B owns it
    size_tb           REAL,
    detected_at       TEXT    NOT NULL,
    pipeline_version  TEXT    NOT NULL,             -- profile id, stamped at detection, never changes
    completed_at      TEXT,

    lifecycle         TEXT    NOT NULL DEFAULT 'active',
    lifecycle_by      TEXT,
    lifecycle_at      TEXT,
    lifecycle_reason  TEXT,

    error_reason      TEXT,
    errored_at        TEXT,

    run_from_step     TEXT,                         -- one-shot control targeting
    run_from_fresh    TEXT,

    protected_at      TEXT,                         -- A3: operator protection, NOT a mutex
    protected_by      TEXT,

    globus_task_id    TEXT,                         -- A4: resume token, written by Plan B
    slack_thread_ts   TEXT,

    CHECK (lifecycle IN ('active', 'held', 'skipped')),
    CHECK ((error_reason IS NULL) = (errored_at IS NULL)),
    CHECK ((protected_at IS NULL) = (protected_by IS NULL))
);

-- 002_poller_metadata
CREATE TABLE poller_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 003_control_audit  (append-only; db creates it, subsystem 7 writes it)
CREATE TABLE control_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_run  TEXT,                 -- NULL for global ops (pause/resume)
    args_json   TEXT,
    result      TEXT
);

-- 004_indices
CREATE INDEX idx_runs_eligible ON runs (lifecycle, errored_at);

-- 005 and beyond are NOT ours: subsystem 2 adds the reference-gate columns,
-- Plan B adds the production gate columns. Never edit a shipped migration.
```

Notes the engineer must not trip on:
- SQLite requires **table constraints after all column definitions** — the three `CHECK`s go
  last, not inline.
- `(x IS NULL) = (y IS NULL)` never yields NULL, so the CHECK is total.
- `ALTER TABLE ADD COLUMN` accepts `NOT NULL DEFAULT <constant>`; that is why gate columns are
  addable forever by later subsystems.
- No index on `detected_at`. PP's `idx_folders_detected` indexes a column that is `1` for every
  row — useless (subsystem map line 571). Do not copy it.
- No `FOREIGN KEY`s, no `PRAGMA foreign_keys=ON`: `control_audit.target_run` deliberately does
  not reference `runs`, so the audit trail survives a row being deleted.

---

## 5. Units, in dependency order (13 units, `MIGRATIONS` = 001–004)

Every unit is one RED → GREEN → REFACTOR cycle. Tests live in `tests/test_db.py` unless named
otherwise. All tests use `tmp_path` — **never `:memory:`**, because `PRAGMA journal_mode=WAL`
silently returns `'memory'` on an in-memory DB and the pragma assertions would be vacuous.

---

### Unit 1 — `now_iso()`

```python
def now_iso() -> str: ...
```
Returns the current UTC instant as an ISO-8601 string with an explicit offset, e.g.
`"2026-07-20T14:03:11.482913+00:00"` (`datetime.now(timezone.utc).isoformat()`).
Every timestamp column in this subsystem is written with it.

**Tests**
- `test_now_iso_is_parseable_utc` — `datetime.fromisoformat(now_iso())` succeeds and the result
  has `.tzinfo is not None` and `.utcoffset() == timedelta(0)`.
- `test_now_iso_is_monotonic_string_sortable` — collecting three calls into a list, the list
  equals its own `sorted()` (string comparison of ISO-8601 UTC is chronological).

---

### Unit 2 — `get_connection(db_path)` + module hygiene

```python
def get_connection(db_path: str | os.PathLike[str]) -> sqlite3.Connection: ...
```
Behavior:
1. `sqlite3.connect(db_path, timeout=5.0, isolation_level=None)` — timeout explicit for
   legibility (A1), `isolation_level=None` per F2.
2. `conn.row_factory = sqlite3.Row`.
3. `conn.execute("PRAGMA journal_mode=WAL")`.
4. `conn.execute("PRAGMA synchronous=NORMAL")`.
5. Returns the connection. It does **not** create directories and does **not** create schema.

**Tests**
- `test_get_connection_reports_wal_journal_mode` — `conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"`.
  (Assert the *queried* value, not that the statement ran.)
- `test_get_connection_reports_synchronous_normal` — `conn.execute("PRAGMA synchronous").fetchone()[0] == 1`.
- `test_get_connection_busy_timeout_is_5000` — `conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000`.
  Docstring on this test must state it documents a value that was **already** 5000 by default
  (A1) and is asserted only to pin the explicit `timeout=5.0`.
- `test_get_connection_rows_are_mappings` — after `CREATE TABLE t(a,b)` / insert `(1,2)`,
  `row["a"] == 1` and `dict(row) == {"a": 1, "b": 2}`.
- `test_get_connection_is_autocommit` — `conn.isolation_level is None` and after a bare
  `conn.execute("INSERT ...")` (no explicit BEGIN) `conn.in_transaction is False`.
- `test_get_connection_does_not_create_parent_directory` — for `tmp_path / "nope" / "s.db"`,
  `sqlite3.OperationalError` is raised and `(tmp_path / "nope").exists() is False`.
  (Deliberate divergence from PP `poller_state_db.py:78`; provisioning is config/bootstrap's.)
- `test_get_connection_returns_a_fresh_connection_each_call` — three assertions, because the
  first two are each individually insufficient:
  1. two calls on the same path return objects for which `a is not b`;
  2. `type(a) is sqlite3.Connection` — an **exact type check, not `isinstance`**, because a proxy
     subclassing `Connection` satisfies `isinstance` while still sharing one underlying handle;
  3. no module-level cached connection, scanning **recursively**: collect every module-level value
     *plus* the members of any module-level tuple/list/set/frozenset and the keys and values of any
     module-level dict, and assert none is a `sqlite3.Connection`.

  > **The recursion is load-bearing — do not simplify it back.** Revision 2 specified the flat
  > `[v for v in vars(db).values() if isinstance(v, sqlite3.Connection)] == []`, and `code-reviewer`
  > demonstrated that form passing against a real defect: a module-level `_POOL: dict[str,
  > Connection]` returning proxies. A flat `vars()` scan only sees a connection bound *directly* to
  > a global, so it kills the naive `_CONN = None` singleton and misses the keyed-cache form —
  > which is the more likely one, since "just memoize per path" is the natural optimization once
  > daemon, CLI and API each call `get_connection` every look. Same technique as unit 13's
  > `test_db_defines_no_metadata_key_values`.

  **Guards "the DB is the single source of truth" — no in-process authoritative handle. Per section
  6 this is the *only* test guarding that invariant, which is why it carries three assertions.**
- `test_db_imports_only_stdlib` — parse `sluice/core/db.py` with `ast`; for every `Import` /
  `ImportFrom`, the root module name is in `sys.stdlib_module_names`. **Guards `gates → core`
  ahead of the broader `tests/test_dependency_arrows.py`.**

---

### Unit 3 — `write_txn(db_path)`

```python
@contextlib.contextmanager
def write_txn(db_path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]: ...
```
Opens a connection via `get_connection`, issues `BEGIN IMMEDIATE`, yields the connection,
`COMMIT`s on clean exit, `ROLLBACK`s on exception (re-raising), and **always closes** the
connection in `finally`. This is the sanctioned way to batch several writes; design §5 forbids
holding a write transaction across a stage or subprocess.

> Because the connection is closed in `finally`, **do not** assert `conn.in_transaction` after
> the block — CPython guards that getter with a live-connection check and raises
> `sqlite3.ProgrammingError` instead of asserting anything. The lock-release evidence is
> `test_write_txn_releases_the_write_lock` below; the "forgot to COMMIT" evidence is
> `test_write_txn_commits_on_clean_exit`. The open-transaction risk that *is* real lives in the
> caller-supplied-connection primitives (units 9, 11, 13), where it is guarded.

**Tests**
- `test_write_txn_commits_on_clean_exit` — insert inside the block; a *new* connection opened
  after the block sees the row.
- `test_write_txn_rolls_back_on_exception` — insert then `raise RuntimeError("boom")` inside the
  block; `pytest.raises(RuntimeError)` around it, and a new connection sees `count == 0`.
- `test_write_txn_closes_the_connection` — capture the yielded `conn`; after the block,
  `conn.execute("SELECT 1")` raises `sqlite3.ProgrammingError`.
- `test_write_txn_releases_the_write_lock` — after the block, a fresh `get_connection` can
  `execute("BEGIN IMMEDIATE")` without raising `sqlite3.OperationalError` ("database is locked").
- `test_write_txn_does_not_block_concurrent_readers` — with the `write_txn` block open and an
  insert already issued, a second connection opened *inside* the block can still
  `SELECT COUNT(*)` without raising (WAL readers are not blocked by a writer) and sees the
  pre-transaction count.

---

### Unit 4 — migration framework

```python
class SchemaVersionError(RuntimeError): ...

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]
MIGRATIONS: tuple[Migration, ...]          # ordered, contiguous 1..4

def schema_version(conn: sqlite3.Connection) -> int: ...        # 0 on a virgin DB
def apply_migrations(conn: sqlite3.Connection) -> list[int]: ...# versions newly applied
def initialize_database(db_path) -> list[int]: ...              # open + apply + close
```
`apply_migrations` creates the `schema_migrations` ledger (§4, `000_bootstrap`) if absent, then
applies every `MIGRATIONS` entry whose version exceeds `schema_version(conn)`, **in ascending
order, each inside its own `BEGIN IMMEDIATE` … `COMMIT`**, recording a `schema_migrations` row
per migration. Forward-only: if `schema_version(conn)` exceeds `max(MIGRATIONS)`, raise
`SchemaVersionError` naming both versions (the DB was written by a newer sluice; downgrading
silently would corrupt state). There is no `downgrade`.

`initialize_database` obtains its connection from `get_connection` and passes it straight to
`apply_migrations` — **never wrapped in `write_txn`** (see the F2 nesting rule).

This deliberately replaces PP's four repeated `try/except sqlite3.OperationalError: pass`
ALTER ladders (`poller_state_db.py:126-146`), which swallow *every* OperationalError — a locked
DB or a full disk mid-ALTER is discarded and the process continues on a half-migrated schema.

**Tests** (in `tests/test_db_migrations.py`)
- `test_schema_version_is_zero_on_virgin_database` — fresh file, `schema_version(conn) == 0`.
- `test_initialize_database_applies_all_migrations` — returned list `== [m[0] for m in MIGRATIONS]`
  and afterwards `schema_version(conn) == max(...)`.
- `test_initialize_database_is_idempotent` — call twice; the second call returns `[]`, and
  `SELECT COUNT(*) FROM schema_migrations` equals `len(MIGRATIONS)` both times.
- `test_migration_versions_are_contiguous_and_unique` — `[v for v, _, _ in MIGRATIONS] ==
  list(range(1, len(MIGRATIONS) + 1))`. (Stops two later branches both claiming version 005.)
- `test_apply_migrations_refuses_a_newer_schema` — manually insert
  `schema_migrations(version=999, ...)`; `pytest.raises(SchemaVersionError)` and the message
  contains `"999"`.
- `test_failed_migration_does_not_record_its_version` — monkeypatch `MIGRATIONS` to append a
  migration that raises; `pytest.raises`, then `schema_version(conn)` equals the last *good*
  version and no row for the failing version exists.
- `test_initialize_database_does_not_nest_transactions` — `initialize_database` on a fresh path
  completes without raising `sqlite3.OperationalError`. **Encodes the F2 nesting rule.** If anyone
  writes `with write_txn(path) as conn: apply_migrations(conn)`, the inner `BEGIN IMMEDIATE` raises
  `cannot start a transaction within a transaction` and this catches it directly.

  > **Do NOT also assert a spy on `db.write_txn` records zero calls.** That pins today's call graph
  > rather than the behaviour, and would fail on a valid refactor: if `write_txn` were later made
  > re-entrant (SAVEPOINT when `conn.in_transaction`, or accepting an existing connection) and
  > `initialize_database` routed through it for uniformity, that is correct code — and the spy fails
  > anyway. It adds no safety the no-raise assertion does not already provide. Assert behaviour, not
  > issuance.
- `test_db_defines_no_default_database_path` — no module-level string constant in
  `sluice.core.db` ends in `.db` or `.sqlite`. **Guardrail §8: no production defaults in code;
  a missing path is a caller error, not a silent fallback.**

---

### Unit 5 — migration 001: `runs`

DDL exactly as §4. **Tests** (in `tests/test_db_schema.py`)

Presence is asserted as a **subset**, never as set equality: later subsystems add columns by
migration, and an equality assertion would force an edit to this test every time — directly
against this plan's "never edit a shipped migration" discipline, and it would couple this unit
to units that do not exist yet. Meaning is carried by the *absence* tests, which do not churn.

- `test_runs_table_has_the_001_columns` — with
  `actual = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}`,
  assert `RUNS_001_COLUMNS <= actual` where `RUNS_001_COLUMNS` is the literal set from §4.
- `test_runs_table_has_no_locked_at_column` — `"locked_at" not in actual`. **Errata A3 guard:**
  the per-run processing mutex is deliberately not ported.
- `test_no_production_gate_columns_yet` — `actual` is disjoint from
  `{"download_complete","checksum_ok","merged","nvd_done","transferred","registered"}`.
  **Errata B1 guard:** the 6-vs-8-gate decision is Plan B's, and this fails loudly if someone
  pre-empts it here.
- `test_no_mock_gate_columns_in_db_subsystem` — no name in `actual` contains `"mock"`.
  **F3 guard:** reference-gate columns belong to subsystem 2's migration 005; core must not name
  gates. *(Subsystem 2 will need to relax this test when it lands 005 — that is the correct
  moment for it to change hands, and it should be moved into subsystem 2's test file then.)*
- `test_runs_name_is_primary_key` — `PRAGMA table_info(runs)` shows `pk == 1` for `name`; a second
  insert of the same name raises `sqlite3.IntegrityError`.
- `test_lifecycle_defaults_to_active` — insert with only `name`, `detected_at`,
  `pipeline_version`; read back `row["lifecycle"] == "active"`.
- `test_lifecycle_rejects_an_unknown_value` — `UPDATE runs SET lifecycle='paused'` raises
  `sqlite3.IntegrityError`.
- `test_error_columns_must_be_set_together` — setting `errored_at` alone raises
  `sqlite3.IntegrityError`; setting both succeeds; clearing both succeeds.
  (Encodes design §5's "quarantined ⇔ error set".)
- `test_protected_columns_must_be_set_together` — same shape for `protected_at`/`protected_by`.
- `test_optional_identity_and_resume_columns_default_null` — a minimal insert yields
  `experiment_id`, `size_tb`, `completed_at`, `globus_task_id`, `slack_thread_ts`,
  `run_from_step`, `run_from_fresh`, `protected_at`, `protected_by` all `None`.

---

### Unit 6 — migration 002: `poller_metadata`

**Tests**
- `test_poller_metadata_is_a_key_value_table` — `PRAGMA table_info` column names `== {"key","value"}`,
  and `key` has `pk == 1`.
- `test_poller_metadata_upsert_replaces_value` — two `INSERT ... ON CONFLICT(key) DO UPDATE`
  statements leave exactly one row with the second value.

---

### Unit 7 — migration 003: `control_audit`

**Tests**
- `test_control_audit_has_the_audit_columns` — column-name set equals
  `{"id","ts","actor","action","target_run","args_json","result"}`.
  (Equality is safe here: no subsystem adds audit columns; if one ever does, it is a real
  design change that should surface.)
- `test_control_audit_requires_ts_actor_action` — inserting with `actor=NULL` raises
  `sqlite3.IntegrityError`; `target_run=NULL` is accepted (global ops such as `pause`).
- `test_db_exports_no_audit_writer` — **assert against the declared public surface, not `dir()`**
  (migration callables are named e.g. `_migration_003_control_audit` and would trip a `dir()`
  grep on a perfectly correct implementation):
  `assert not [n for n in sluice.core.db.__all__ if re.match(r"^(insert|write|log|record|append)_.*audit", n)]`
  and `assert not hasattr(sluice.core.db, "insert_control_audit")`.
  **Scope guard:** writing audit rows is subsystem 7.
- `test_db_exports_exactly_the_documented_surface` — `set(sluice.core.db.__all__) ==
  DOCUMENTED_SURFACE`, where `DOCUMENTED_SURFACE` is the literal set named in §1. This makes §1's
  "`__all__` names exactly the surface above" executable rather than aspirational, and **subsumes
  the regex above**: it catches any future scope creep — an audit writer, a control op, a gate
  helper — not only names matching one verb pattern. Keep both; the regex documents the specific
  hazard, this catches the general one.

  > `__all__` covers the **Python exports only**. §1's Schema row lists `idx_runs_eligible`, which
  > is a SQL object and correctly not an export.

---

### Unit 8 — migration 004: `idx_runs_eligible`

**Tests**
- `test_eligibility_index_covers_lifecycle_and_errored_at` — assert the *indexed columns*, not the
  name: `[r["name"] for r in conn.execute("PRAGMA index_info(idx_runs_eligible)")] ==
  ["lifecycle", "errored_at"]`.
- `test_no_index_covers_detected_at` — for every index in `PRAGMA index_list(runs)`,
  `"detected_at"` is not among its `PRAGMA index_info` column names. (Name-based checking would
  miss `CREATE INDEX idx_whatever ON runs(detected_at)`. PP's `idx_folders_detected` indexes a
  column that is constant across all rows; subsystem map line 571.)
- `test_index_migration_applies_to_a_pre_existing_database` — initialize with `MIGRATIONS`
  truncated to versions 1–3, insert a run, then run the full `MIGRATIONS`; the index exists and
  the pre-existing row is unchanged. **This is the "additive migration on live data" proof that
  subsystem 2's migration 005 and Plan B's gate columns depend on.**

---

### Unit 9 — `insert_run`

```python
def insert_run(
    conn: sqlite3.Connection,
    name: str,
    *,
    pipeline_version: str,
    detected_at: str | None = None,
    experiment_id: int | None = None,
    size_tb: float | None = None,
) -> bool: ...
```
`INSERT INTO runs (...) VALUES (...) ON CONFLICT(name) DO NOTHING`, with `detected_at`
defaulting to `now_iso()`. Returns `True` if a row was inserted, `False` if the run already
existed (`cursor.rowcount > 0`).

**Not `INSERT OR IGNORE`** — deliberately. `OR IGNORE` also swallows CHECK and NOT NULL
violations, conflating "already exists" with "invalid row"; that is a smaller cousin of the
blanket-suppression failure this plan condemns in PP's ALTER ladder (unit 4).
`ON CONFLICT(name) DO NOTHING` suppresses exactly the primary-key conflict and lets a bad row
raise.

Detection is "stamp once, never change" — mirrors PP `mark_folder_detected`
(`poller_state_db.py:427-431`), which is already idempotent and state-only, plus the
`pipeline_version` stamp PP has no analogue for (subsystem map line 397). The caller supplies the
connection so the daemon can batch a detection sweep inside one `write_txn`.

**Tests**
- `test_insert_run_creates_an_active_row` — returns `True`; the row has `lifecycle == "active"`,
  `pipeline_version == "reference-v1"`, `detected_at` non-null.
- `test_insert_run_is_idempotent` — second call with the *same* name returns `False` and the row
  count is still 1.
- `test_insert_run_never_restamps_pipeline_version` — insert with `"v1"`, then call again with
  `"v2"`; the stored `pipeline_version` is still `"v1"`. **Freezes design §8's "stamped at
  detection and never changes".**
- `test_insert_run_requires_pipeline_version` — it is keyword-only and has no default;
  `pytest.raises(TypeError)` when omitted. (No production default baked in — guardrail §8.)
- `test_insert_run_propagates_a_constraint_violation` — call with a value that violates a
  NOT NULL/CHECK (e.g. `detected_at=""` combined with a monkeypatched column, or simply
  `pipeline_version=None`); `sqlite3.IntegrityError` is raised rather than the call silently
  returning `False`. **Proves the divergence from `INSERT OR IGNORE` is real, not cosmetic.**
- `test_row_primitives_leave_no_open_transaction[insert_run]` — call `insert_run` on a
  caller-supplied `get_connection` handle; afterwards `conn.in_transaction is False` and the row
  is visible from a *second* connection (i.e. it was committed, not sitting in an open txn).
  **This is the short-transaction guard.**

---

### Unit 10 — `get_run`

```python
def get_run(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None: ...
```
**Tests**
- `test_get_run_returns_a_mapping_row` — `row["name"]` and `row["lifecycle"]` both readable.
- `test_get_run_returns_none_for_unknown_name` — `get_run(conn, "nope") is None`.

---

### Unit 11 — `update_run`

```python
class UnknownColumnError(KeyError): ...

def update_run(conn, name: str, fields: Mapping[str, object]) -> bool: ...
```
Builds `UPDATE runs SET <col> = ? ... WHERE name = ?` with **column names validated against
`PRAGMA table_info(runs)`** and values bound as parameters. Raises `UnknownColumnError` for any
unknown column (no string interpolation of caller data reaches SQL). Returns `True` if a row was
updated. Empty `fields` is a no-op returning `False`.

Deliberate design point: `update_run` is a *primitive*, not a policy. It has no opinion about
which columns an operator may touch — `set_gate`'s refusal to fabricate `experiment_id` (errata
B2, design §9) lives in subsystem 8, not here.

**Tests**
- `test_update_run_writes_named_columns` — set `experiment_id=1234` and `size_tb=2.5`; read back.
- `test_update_run_rejects_an_unknown_column` — `pytest.raises(UnknownColumnError)` for
  `{"drop_table": 1}`, and the message contains `"drop_table"`.
- `test_update_run_rejects_sql_in_a_column_name` — `{"name = 'x'; DROP TABLE runs; --": 1}`
  raises `UnknownColumnError` and `runs` still exists.
- `test_update_run_returns_false_for_unknown_run` — no matching row → `False`.
- `test_update_run_honours_check_constraints` — `{"errored_at": now_iso()}` alone raises
  `sqlite3.IntegrityError`; `{"errored_at": ..., "error_reason": "boom"}` succeeds.
- `test_row_primitives_leave_no_open_transaction[update_run]` — after `update_run` on a
  caller-supplied `get_connection` handle, `conn.in_transaction is False` and a second connection
  sees the new value.

---

### Unit 12 — `select_active_runs` (the eligibility predicate)

```python
ACTIVE_RUNS_WHERE: str = "lifecycle = 'active' AND errored_at IS NULL"

def select_active_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]: ...
```
`SELECT * FROM runs WHERE {ACTIVE_RUNS_WHERE} ORDER BY detected_at, name`. Deterministic
ordering so daemon behavior is reproducible in tests. Per F4 it does **not** filter
`completed_at`.

**Tests** — each exclusion is its own test.
- `test_active_runs_includes_a_plain_active_run`.
- `test_active_runs_excludes_held` — a run with `lifecycle='held'` is absent while an active
  sibling is present.
- `test_active_runs_excludes_skipped` — same shape for `'skipped'`.
- `test_active_runs_excludes_quarantined` — `lifecycle='active'` but `errored_at`/`error_reason`
  set → absent.
- `test_clear_error_alone_does_not_make_a_held_run_eligible` — start from a run that is *both*
  held and errored; clear only the error columns; it is still absent.
- `test_unhold_alone_does_not_make_a_quarantined_run_eligible` — the mirror scenario: same
  starting run, set `lifecycle='active'` while leaving the error columns set; still absent.
  (Together these two encode the F4 precedence answer: the filters are orthogonal and AND-ed.)
- `test_active_runs_is_ordered_by_detection_then_name` — three runs inserted out of order come
  back sorted.
- `test_active_runs_still_includes_a_completed_run` — a run with `completed_at` set is present.
  *Docstring:* this is fork F4. Eligibility is deliberately one canonical predicate that does not
  consult the derived `completed_at` cache, at the accepted cost of returning completed runs
  forever; reconcile skips them with a pure function call. A `completed_at IS NULL` pre-filter is
  a legitimate future optimization, but it belongs in a *separate* query
  (e.g. `select_reconcilable_runs`), not folded into `ACTIVE_RUNS_WHERE`.

---

### Unit 13 — metadata accessors

```python
def get_metadata(conn, key: str, default: str | None = None) -> str | None: ...
def set_metadata(conn, key: str, value: str) -> None: ...
def all_metadata(conn) -> dict[str, str]: ...
```
Key-agnostic primitives over `poller_metadata`. `set_metadata` is an upsert
(`ON CONFLICT(key) DO UPDATE SET value = excluded.value`). `db` knows **no key names** — the
meaning of `paused`, `pipeline_profile`, the disk-halt flag and single-instance info belongs to
`control`/`daemon`. PP writes exactly one key ever, `last_poll` (`poller_state_db.py:842`); there
is no global pause today (subsystem map lines 370/622).

**Tests**
- `test_set_then_get_metadata_round_trips`.
- `test_set_metadata_overwrites_an_existing_key` — two sets, one row, second value.
- `test_get_metadata_returns_the_default_for_a_missing_key` — `get_metadata(conn, "nope") is None`
  and `get_metadata(conn, "nope", "x") == "x"`.
- `test_all_metadata_returns_every_key` — `all_metadata(conn) == {"paused": "0", "pipeline_profile": "reference-v1"}`.
- `test_db_defines_no_metadata_key_values` — collect every module-level **string value** in
  `sluice.core.db` (every `str` in `vars(db).values()`, plus the string members of any
  module-level tuple/list/frozenset/set and the keys and values of any module-level dict) and
  assert that collection is disjoint from `{"paused", "pipeline_profile", "disk_halted"}`.
  Matching *values* rather than attribute names is what stops `KEY_1 = "paused"` slipping
  through. **Scope guard: no policy in db.**
- `test_row_primitives_leave_no_open_transaction[set_metadata]` — after `set_metadata` on a
  caller-supplied `get_connection` handle, `conn.in_transaction is False` and a second connection
  sees the value.

---

## 6. Invariant checks

| Invariant (from the spine) | How `db` could violate it | Guarding test |
|---|---|---|
| **DB is the single source of truth** | a module-level cache / singleton connection | `test_get_connection_returns_a_fresh_connection_each_call` (unit 2) |
| **`gates → core`, never the reverse** | importing a gate id list or exception; naming a gate column in core | the pre-existing `tests/test_dependency_arrows.py` (stays green), plus `test_db_imports_only_stdlib` (unit 2) and `test_no_mock_gate_columns_in_db_subsystem` (unit 5) |
| **Short transactions only** | a primitive that leaves a transaction open on a caller's connection | `test_row_primitives_leave_no_open_transaction` on `insert_run` (unit 9), `update_run` (unit 11) and `set_metadata` (unit 13); plus `test_write_txn_releases_the_write_lock` (unit 3) and `test_get_connection_is_autocommit` (unit 2) |
| **Control ops never start/stop work** | db exposing a policy or side-effecting helper | `test_db_exports_no_audit_writer` (unit 7, via `__all__`) and `test_db_defines_no_metadata_key_values` (unit 13) |
| **No secrets / no production defaults** | a default `pipeline_version`, a default db path | `test_insert_run_requires_pipeline_version` (unit 9) and `test_db_defines_no_default_database_path` (unit 4) |
| **Forward-only, additive schema** | editing an already-shipped migration | `test_migration_versions_are_contiguous_and_unique`, `test_apply_migrations_refuses_a_newer_schema` (unit 4), `test_index_migration_applies_to_a_pre_existing_database` (unit 8), and the subset-not-equality form of `test_runs_table_has_the_001_columns` (unit 5) |
| **Transactions are never nested** | wrapping `apply_migrations` in `write_txn` | `test_initialize_database_does_not_nest_transactions` (unit 4) — the no-raise assertion, not a call-graph spy |
| **A1 not miscast as a bug fix** | a docstring/test claiming `busy_timeout` fixes locking | `test_get_connection_busy_timeout_is_5000` carries the corrective docstring; `code-reviewer` checks the wording |
| **A3 mutex not resurrected** | porting `acquire_folder_lock` | `test_runs_table_has_no_locked_at_column` (unit 5) |
| **B1 not pre-empted** | adding production gate columns here | `test_no_production_gate_columns_yet` (unit 5) |

---

## 7. Done criteria

1. `pixi run test` is green, including the two pre-existing tests
   (`tests/test_dependency_arrows.py`, `tests/test_guard_hook.py` — 55 baseline cases) plus the
   new `tests/test_db.py`, `tests/test_db_schema.py`, `tests/test_db_migrations.py`.
2. `sluice/core/db.py` imports cleanly with `sluice.gates`, `sluice.api`, and `sluice.cli`
   absent, and imports **nothing outside the standard library**.
3. `initialize_database(tmp/state.db)` on a fresh path produces a DB where
   `PRAGMA journal_mode` = `wal`, `PRAGMA synchronous` = `1`, `schema_version(conn) == 4`, and
   `.tables` = `control_audit · poller_metadata · runs · schema_migrations`.
4. Running `initialize_database` a second time applies nothing and changes no row.
5. `select_active_runs` on a DB holding one active, one held, one skipped, one quarantined and
   one completed run returns exactly the active one **and** the completed one (F4), in
   detection order.
6. `sluice/core/db.py` contains no `locked_at`, no gate column name of any kind (production or
   mock), no metadata key string, no default DB path, and no audit-writing export.
7. Subsystem 2 (`gate`) can start against a stable foundation: `MIGRATIONS` ends at 004,
   `apply_migrations` is proven idempotent and forward-only, and unit 8 proves an additive
   migration lands correctly on a populated database — exactly what subsystem 2's migration 005
   (the reference-gate satisfaction columns) needs.
