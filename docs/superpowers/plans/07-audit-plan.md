# Plan 07 — `audit` subsystem (Plan A, subsystem 7 of 9)

**Modules owned:** `sluice/core/audit.py` (new; the writer for the append-only
`control_audit` trail — the table has existed since db migration 003; this subsystem
builds the function that writes it). **No schema change:** both ⚑ flags were decided
against extension (see the sign-off section) — no migration 006, no reader; the module
is the writer alone.
**Build order:** db → gate (+ mock gates) → events → context → profile → reconcile →
**audit** → control → daemon
**Tests:** consolidated in `tests/test_audit.py` (one file per subsystem). No edits to
any other test file.

Ground truth: design of record **§5** (`control_audit`: append-only log of *every*
control action, `(ts, actor, action, target_run, args_json, result)`; control actions
never post to Slack), **§9** (each op: validate → mutate state in a short transaction →
**write a `control_audit` row** → emit a wide event → return a Result), **§2**
(state-only control; the DB is the single source of truth), **§11** (audit row + wide
event, not Slack); plan 01 (migration 003 built the table and **deferred the writer
here by name** — §1 "Writing `control_audit` rows — subsystem 7"; F1 `target_run` is
the run *name*; F2 autocommit connections, callers batch via `write_txn`; the
AUTOINCREMENT-id-not-wall-clock ordering rationale in `_migration_003_control_audit`'s
docstring, made executable by
`tests/test_db.py::test_control_audit_id_autoincrements_without_reusing_deleted_ids`);
plan 03 §7 (**mutate → audit row → `control_event`, in that order**; persist-then-emit
is binding); ADR-0001 (docstrings), ADR-0002 (no retry tier — no `reset_attempts`
action will ever exist), ADR-0003 (layering; service SDKs never in core), ADR-0004
(remediation is exclusion — the subsystem-8 exclusion op will write its audit rows
through this surface); `sluice/core/db.py` and `sluice/core/events.py` as built.

---

## 1. Scope & non-goals

### Owned

| Thing | Public surface |
|---|---|
| The audit writer (the §9 middle step's primitive) | `record_control_action(conn, *, actor, action, target_run=None, args=None, result=None, ts=None) -> int` |

`audit.py` declares `__all__ = ["record_control_action"]`; the final unit asserts set
equality (db unit-7 precedent). One plain module-level function — functional shaping,
no class, no state.

**Allowed imports (closed set): `json`, `sqlite3`, `collections.abc`, and
`sluice.core.db` — from which exactly `{"now_iso"}` is imported.** Forbidden by name
(unit 1, ast-parse): `structlog` and `slack_sdk` (the audit row is state, never a log
line or a Slack post — §11's "control actions never post to Slack" and plan 03's
row-vs-event split become structural), `sluice.gates`/`api`/`cli` (layering),
`datetime`/`time` (every timestamp routes through `db.now_iso` — one clock spelling,
one format), `os`/`subprocess`/`socket`/`urllib`/`http` (a state-only writer starts
nothing and reaches nothing). The only-`now_iso` pin is load-bearing: with
`get_connection`/`write_txn` unreachable, audit **structurally cannot** open its own
connection or transaction — the caller-supplies-the-connection contract is a compile
surface, not a promise.

**Inherited facts (locked, not reopened):**

1. **`target_run` is the run *name*** (plan 01 F1) — and it is **never validated
   against `runs`**: no FK by design, the trail must survive run deletion, and
   existence checking is the calling op's validation step, not the primitive's.
2. **The writer issues exactly one static `INSERT` on the caller's connection** and
   never opens, commits, or rolls back a transaction (plan 01 F2 — same composition
   contract as `insert_run`/`update_run`; the caller decides whether to batch inside
   `write_txn`).
3. **Ordering is by `id` (AUTOINCREMENT), never `ts`** — an NTP step can move
   wall-clock backwards; ids strictly increase and are never recycled (plan 01,
   migration-003 docstring; the no-reuse half is already executable at
   `tests/test_db.py::test_control_audit_id_autoincrements_without_reusing_deleted_ids`).
   This plan pins the trail-side consequence (unit 2).
4. **The action vocabulary is not audit's.** The writer is action-agnostic (like db's
   key-agnostic metadata primitives); which actions exist, and what `result` strings
   mean, is subsystem 8 policy.

### Explicitly NOT owned (and where it belongs)

- **The control ops themselves** — validate/mutate/refuse policy, the `ProtectedError`
  path, `set_gate`'s B2 fabrication refusal, `reset_gate`'s experiment-gate protection,
  the ADR-0004 d.3 exclusion op — **subsystem 8**. Audit hands it the middle step.
- **`control_event`** — the event half of §9's "audit row + wide event" is built
  (plan 03, `events.control_event`); the op calls it **after** its transaction commits.
  Audit neither emits nor imports structlog.
- **When the daemon writes audit** (B5 auto-pause as a daemon-performed control action)
  — **subsystem 9**; the primitive is actor-agnostic so it needs nothing new.
- **Connection/pragma work** — done in db (A1: WAL + `synchronous=NORMAL` live in
  `get_connection`; `busy_timeout` is already 5000 via Python's default and is **not**
  re-added anywhere). Audit adds no pragmas and no connection factory.
- **Any audit reader** — deferred whole to **Plan C** with its first caller
  (`sluice audit`), per ⚑2 as decided; the ordering rule travels as a named
  carry-forward (§6).
- **DB-level append-only enforcement (triggers)** — declined per ⚑1 as decided;
  append-only is a module-surface promise (§2), guarded by unit 3.

---

## 2. Errata & obligations in scope — decisions taken, no TBDs

No lettered erratum names `audit` directly; its binding obligations come from spec
§5/§9/§11, the plan-01 deferral, and plan 03 §7. Each is resolved here:

**Plan-01 deferral — the writer.** Executed by unit 1. The db-side scope guard,
`tests/test_db.py::test_db_exports_exactly_the_documented_surface` (test_db.py:1770 —
its docstring names "an audit writer" as the scope creep it catches), stays untouched
and green: the writer lives in `audit.py`, and db still exports none.

**A1 (inherited, closed).** Audit composes on caller connections opened by
`db.get_connection`/`write_txn`, where WAL + `synchronous=NORMAL` already live. The
only-`now_iso` import pin makes a second connection path structurally impossible — no
redundant `busy_timeout`, no new pragma site.

**§9 atomicity choreography (the load-bearing decision).** A control op's state write
and its audit row land in **one transaction** — the op's own `write_txn`:

```python
with write_txn(db_path) as conn:
    # 1. validate (reads via conn; refusal policy is the op's)
    # 2. mutate   (update_run / set_metadata — the state-only write)
    audit_id = record_control_action(          # 3. the audit row, same txn
        conn, actor=actor, action="hold", target_run=name,
        args={"reason": reason}, result="ok",
    )
# txn committed — state + audit landed atomically, or neither did
control_event("hold", actor, target_run=name, result=result_dict)   # 4. after commit
```

Consequences, both deliberate and both tested (unit 3): if the audit write (or its
serialization) fails, the transaction rolls back and **the state change never lands
unaudited**; if the mutation fails, no orphan audit row lands either. Mutate-then-audit
within the transaction, so `result` can record what the mutation found; the event
strictly after commit (plan 03 §7 persist-then-emit — an event describing an
uncommitted write is a lie after a crash). A single `record_control_action` call on a
plain autocommit connection is also legal (one INSERT autocommits) — the shape
subsystem 8 may want for auditing *refused* ops, where there is no mutation to batch
with.

**`args_json` — exactly what is serialized, and failures fail loud.**
`args: Mapping[str, object] | None` — the op's own parameters beyond
`(db_path, actor)`, JSON-shaped primitives by construction (Plan C hands them over as
request JSON anyway). Serialization is pinned to:

```python
None if args is None else json.dumps(dict(args), sort_keys=True, ensure_ascii=False, allow_nan=False)
```

- `None` → SQL NULL (absent, not `'null'`, not `'{}'` — the no-fabrication ethic).
- `sort_keys=True` → deterministic: the same call always stores the same bytes.
- `ensure_ascii=False` → UTF-8 text stored readably (SQLite TEXT is UTF-8 native).
- `allow_nan=False` → NaN/Infinity raise `ValueError` instead of emitting non-JSON.
- **No `default=` hook** → a non-serializable value raises `TypeError`. A coercing
  hook (`default=repr`) would silently record a lossy representation — fabricated
  evidence hiding a caller bug. Refusal is the contract.
- **Non-`str` mapping keys raise `TypeError` — at every depth.** `json.dumps` silently
  coerces `{1: "x"}` → `{"1": "x"}` and `{True: "x"}` → `{"true": "x"}`, a type
  rewrite; a top-level-only check would leave the same hole one level down
  (`{"a": {1: "x"}}` coerces just as silently). Before dumping, the writer **walks the
  args structure recursively** — every mapping encountered at any depth, including
  mappings nested inside lists/tuples, must have only `str` keys (`str`/`bytes`
  values are leaves, never descended into); any `int`/`float`/`bool`/`None` key raises
  `TypeError`. Nothing in `args` is ever silently rewritten — the rule is total, not
  top-level.
- **No size cap, no truncation, ever.** Args are op parameters — small by
  construction; a cap would silently destroy evidence.
- Validation and serialization run **before** the INSERT, so a refused call never
  touches the connection; inside an op's `write_txn` the raise rolls the whole op back.

**`result` is opaque TEXT, `str | None` — not a second JSON contract.** The schema
types the column `TEXT` with no `_json` suffix (the deliberate contrast with
`args_json`), and the structured Result dict already rides the wide event nested under
`control.result` (plan 03). The writer takes a caller-chosen summary string
(recommended vocabulary — subsystem 8's to own: `"ok"`, `"refused: <why>"`,
`"noop: <why>"`). A non-str non-None `result` fails loud as
`sqlite3.ProgrammingError` (sqlite3's own unsupported-type refusal; documented, not
re-tested — that would re-prove the library).

**Blank identity refused.** `actor` and `action` empty or whitespace-only raise
`ValueError` naming the field. The schema's NOT NULL admits `''`, and an anonymous
audit row is worthless evidence. This is record integrity (like `update_run`'s
whitelist), not control policy.

**Append-only — a module-surface promise (⚑1 decided: code promise only).** The module
exposes no update/delete surface, ever — pinned by the `__all__`/no-mutation-surface
guard in unit 3, and by review discipline on every future touch of this file. DB-level
trigger enforcement was **declined** (see the ⚑1 stamp — it would also have collided
with the db suite's executable AUTOINCREMENT proof); the append-only invariant lives in
this module's surface, not in the schema.

**B2/B4 traps, named so nobody wanders:** `set_gate`'s refuse-to-fabricate and
`reset_gate`'s experiment-gate protection are **subsystem 8** policy — their refusals
will be *recorded* through this writer, not decided by it. Per ADR-0002 and plan 02 §7,
`reset_attempts` is deleted from the op list — **no `reset_attempts` action string will
ever appear in the trail**; do not reserve or special-case one. B9 is reconcile/daemon
wiring — cited, not reopened.

---

## 3. Units, in dependency order

Three units. **Unit 1 is the RED → GREEN → REFACTOR cycle; units 2 and 3 are
green-on-arrival by design** (the plan-02 unit-9 carve-out): they add no production
code beyond finalizing `__all__` (unit 3) and pin the composed/contractual behavior of
a *correct* unit 1 — unit 2's `never_commits` probe is the assertion that catches the
plausible-wrong unit-1 implementation (a writer that opens its own
`BEGIN`/`COMMIT` passes every unit-1 test, all of which commit via `write_txn`'s clean
exit, and fails only the mid-transaction rollback probe). `__all__` grows as names
land. All DB tests run against real files under `tmp_path`, schema built by
`initialize_database` (never `:memory:`, never hand-rolled DDL).

### Unit 1 — `record_control_action` (the writer)

```python
def record_control_action(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    target_run: str | None = None,
    args: Mapping[str, object] | None = None,
    result: str | None = None,
    ts: str | None = None,
) -> int:
```

Behavior: validate `actor`/`action` non-blank → recursive-key-walk + serialize `args`
per §2 → one static `INSERT INTO control_audit (ts, actor, action, target_run,
args_json, result) VALUES (?, ?, ?, ?, ?, ?)` with every value bound → return
`cursor.lastrowid` (the new row's `id`, so an op can ride it back on its Result).
`conn` is positional-first (db primitive convention); **everything else keyword-only**
— `actor` and `action` are both strings and a transposed pair would be recorded
silently forever. `ts` defaults to `now_iso()`; a caller may pass one so a state write
and its audit row can share the same instant (e.g. `hold` stamping `lifecycle_at` and
the audit `ts` identically). Docstring per ADR-0001: `Raises:` exhaustive
(`ValueError`, `TypeError`, `sqlite3.IntegrityError` propagated,
`sqlite3.ProgrammingError` on a non-str `result`); one why-paragraph citing §9 and this
plan.

Failing tests that define done:

- `test_audit_import_hygiene` — ast-parse of `audit.py`: imports are a subset of
  `{json, sqlite3, collections.abc, sluice.core.db}`; the names imported from
  `sluice.core.db` equal exactly `{"now_iso"}`; none of
  `structlog`/`slack_sdk`/`sluice.gates`/`sluice.api`/`sluice.cli`/`datetime`/`time`/
  `os`/`subprocess`/`socket`/`urllib`/`http` appear anywhere (incl. function-local
  imports — the smuggling pattern test_db.py already guards against).
- `test_record_control_action_appends_one_row_and_returns_its_id` — inside a caller's
  `write_txn` on an initialized DB: one call with all fields supplied (ts injected);
  after commit a fresh connection reads back exactly the six values; the returned int
  equals the row's `id`. The args payload includes a hostile value
  (`{"reason": "x'); DROP TABLE runs;--"}`) asserted stored **literally** with `runs`
  still present — the statement is static and every value is bound (no dynamic
  identifiers exist here, unlike `update_run`; this rides the happy path rather than
  earning its own injection suite).
- `test_record_control_action_defaults_ts_to_now_iso` — call without `ts`; the stored
  value parses via `datetime.fromisoformat` and carries a UTC offset. (One behavioral
  test for a delegation to `db.now_iso`; the format itself is db's tested decision.)
- `test_record_control_action_serializes_args_deterministically_and_none_as_null` —
  two calls whose args carry the same mapping with scrambled insertion order both store
  the byte-identical literal
  `'{"force": true, "gate": "checksum", "note": "café", "waves": [1, 2]}'` (pins
  `sort_keys`, `ensure_ascii=False`, and the no-hook contract against a literal, not a
  tautological re-dump); `args=None` stores SQL NULL — asserted with
  `args_json IS NULL`, distinguishing it from `'null'` and `'{}'`.
- `test_record_control_action_fails_loud_on_unserializable_args` — parametrized:
  `{"p": object()}` → `TypeError`; `{"n": float("nan")}` → `ValueError`;
  `{1: "x"}` → `TypeError`; **`{"a": {1: "x"}}` → `TypeError`** and
  **`{"a": [{True: "x"}]}` → `TypeError`** (the nested and nested-in-sequence cases —
  the recursive walk, not a top-level check; `json.dumps` would coerce both silently).
  After each refusal, `SELECT COUNT(*) FROM control_audit` is unchanged — the
  connection was never touched.
- `test_record_control_action_refuses_blank_actor_or_action` — parametrized over
  `("", "   ")` × both fields: `ValueError` naming the offending field; no row lands.

### Unit 2 — the trail orders by `id`, never `ts`; the writer never manages the transaction (green-on-arrival)

No new production code — two contract pins on inherited facts 2 and 3, raw SQL,
green against a correct unit 1 (the carve-out; the second test is the one that fails a
plausible-wrong unit-1 writer that commits or opens its own transaction):

- `test_audit_trail_orders_by_id_not_ts` — write row A with `ts=T`, then row B with
  `ts` = T **minus one hour** (the NTP step-back, via the `ts` parameter); assert
  `id_B > id_A`; `ORDER BY id` returns (A, B) — true action order — while `ORDER BY ts`
  returns (B, A): the wall-clock lie demonstrated in the same test. Any future consumer
  ordering this table on `ts` has this test to answer to (the Plan C reader
  carry-forward, §6, cites it by name).
- `test_record_control_action_never_commits_the_callers_transaction` — inside
  `write_txn`, write an audit row, then raise before the block exits; a fresh
  connection sees **zero** audit rows. Proves the writer issued no COMMIT and opened no
  transaction of its own (F2 composition) — the property the §9 atomicity choreography
  stands on.

### Unit 3 — choreography integration + surface (green-on-arrival)

No new production code except finalizing `__all__`. Tests pin the composed behavior of
units 1–2 over the real schema:

- `test_control_op_choreography_state_plus_audit_land_atomically` — a fake `hold` op,
  the §9 shape in miniature: `write_txn` wrapping `update_run(conn, "r1",
  {"lifecycle": "held", ...})` + `record_control_action(..., action="hold",
  target_run="r1", args={"reason": "drill"}, result="ok")`. Fresh connection: the run
  is held **and** exactly one audit row with those fields. Then the failure direction:
  the same shape with an unserializable `args` value — the op raises, and a fresh
  connection shows lifecycle **unchanged** and **zero** new audit rows. Both directions
  of "no state change without its audit row; no audit row without its state change" in
  one transaction. Finally: `DELETE FROM runs` (raw SQL — no public surface deletes
  runs) and the audit row still reads back — the trail survives run deletion (no FK,
  plan 01).
- `test_audit_public_surface_is_exactly_the_declared_writer` — `__all__` set equality
  == `{"record_control_action"}`, and no public name matches
  `^(update|delete|purge|edit|amend|redact)_` — the db unit-7 scope-guard precedent
  (`test_db_exports_exactly_the_documented_surface`) pointed at audit itself. With ⚑1
  decided as code-promise-only, **this test is the append-only enforcement**: the
  module has no mutation surface, ever.

---

## 4. Invariant checks

| Invariant | How subsystem 7 could violate it | Guard |
|---|---|---|
| DB is the single source of truth; short transactions only | the writer opening/holding its own transaction, or committing the caller's mid-op | `test_record_control_action_never_commits_the_callers_transaction` (unit 2); structurally: only `now_iso` is importable from db, so `write_txn`/`get_connection` are unreachable |
| State-only control — ops write state + audit + event, never act | audit starting work, reaching the network, or notifying | `test_audit_import_hygiene` (unit 1): no `subprocess`/`socket`/`os`/`urllib`/`http`/`structlog`/`slack_sdk` |
| `gates → core`, never the reverse | audit importing `sluice.gates`/`api`/`cli` | `tests/test_dependency_arrows.py` auto-covers `sluice.core.audit` (it imports every core module); unit 1's ast test double-covers |
| Two write paths, callers own their connections | audit becoming a second connection factory or a policy layer | conn-first signature (no `db_path` anywhere) + the only-`now_iso` import pin |
| Append-only trail; survives run deletion | any update/delete surface; an in-place edit; an FK | the module-surface promise (⚑1 decided): `test_audit_public_surface_is_exactly_the_declared_writer` (unit 3, `__all__` equality + mutation-name regex); deletion-survival assertion (unit 3). No DB-level trigger — declined, see ⚑1 stamp |
| §9 atomicity — no unaudited state change | an op splitting mutate and audit across transactions | `test_control_op_choreography_state_plus_audit_land_atomically` (unit 3), both directions — and the choreography in §2 is the binding contract subsystem 8 implements |
| Ordering by `id`, never `ts` | a consumer ordering on wall-clock | unit 2's raw-SQL pin here; the Plan C reader carry-forward (§6) makes it binding on the first consumer |
| No secrets, no production defaults | — | structural: no config parameter on any signature, no `os`/environ importable; a required value missing (blank actor) is a loud `ValueError`, never a fallback |

---

## 5. Done criteria

1. `sluice/core/audit.py` exists; `__all__ == ["record_control_action"]`;
   `pixi run lint` clean; docstrings per ADR-0001 (`Raises:` exhaustive).
2. `tests/test_audit.py` green (10 test functions, several parametrized); the full
   suite grows from 304 with **no other file changed** — no migration, no edits to
   `tests/test_db.py` or `tests/test_gate.py`; the dependency-arrow guard is green
   auto-covering `sluice.core.audit`.
3. The choreography test proves, over the real schema, that a control op's state write
   and audit row land atomically — and roll back atomically — in one `write_txn`.
4. Subsystem 8 can start: every §9 op is implementable as
   validate → mutate → `record_control_action` → commit → `control_event` with zero
   audit-side changes;
   `tests/test_db.py::test_db_exports_exactly_the_documented_surface` (the db-side
   scope guard, test_db.py:1770) is untouched and still green.

---

## 6. Carry-forwards (named, so no plan rediscovers them)

- **Subsystem 8 (control) — the primary caller.**
  - **The binding op shape** (§2): one `write_txn` per op; mutate → audit → commit →
    `control_event` → return Result. The `audit_id` return may ride the op's Result.
    `args` = the op's own parameters beyond `(db_path, actor)` — JSON-shaped
    primitives; the writer's fail-loud `TypeError` is the backstop, not the op's
    validation.
  - **The `result` vocabulary is subsystem 8's** (recommendation: `"ok"`,
    `"refused: <why>"`, `"noop: <why>"`). The structured Result dict rides the wide
    event (`control.result`), not this column.
  - **Refused ops are audited — recommendation, decide there.** §5 says "every control
    action"; an attempt *is* an action, and a refused `reset_gate` on a protected run
    is exactly what a trail is for. The raise path can't use the op's rolled-back
    transaction — write the refusal row as a single autocommit
    `record_control_action` on a fresh connection before re-raising (the primitive
    supports this today; unit 2 proves the single-INSERT shape).
  - **`get_status` writes no audit row — recommendation, decide there.** It mutates
    nothing; auditing every status poll floods the trail with noise. (Spec §9's "each
    op writes a row" read as: each *state-writing* op.)
  - No `reset_attempts` action, ever (ADR-0002 / plan 02 §7). The ADR-0004 d.3
    exclusion op and any `backtrack`/`fastforward` pointer-writes (the routed plan-06
    question) all audit through this writer like every other op.
- **Subsystem 9 (daemon).**
  - **B5 auto-pause is a control action performed by the daemon:** set
    `metadata.paused` + `record_control_action` (actor — recommendation: `"daemon"`;
    final string is subsystem 9's, with `args` naming the disk floor/trigger) + event,
    same choreography, same atomicity.
  - **Gate-progress persists are NOT control actions — no audit rows.** The trail is
    control-only (§5); do not "helpfully" audit gate completions (that is what
    `gate.finished` events are for). Honoring/clearing a one-shot pointer is a progress
    write; the audit row was written when the op *set* the pointer.
- **Plan C (API + CLI).**
  - **The audit reader arrives with its first caller (`sluice audit [RUN]`) — ⚑2 as
    decided.** It consumes the writer's table via its own query, and it **MUST order by
    `id`, never `ts`** (an NTP step moves wall-clock backwards; ids strictly increase —
    plan 01 migration-003 rationale). The rule is prose here and executable there:
    Plan C's reader unit owes a consumer-side ordering test mirroring
    `test_audit_trail_orders_by_id_not_ts` (unit 2), which any `ts`-ordered
    implementation must answer to. `limit`/filtering/pagination are designed there,
    with their caller.
  - `actor` comes from authenticated identity — the API never defaults or invents one
    (the blank-actor `ValueError` is the backstop, not the design).
- **Plan B.** Nothing — audit is gate-agnostic by construction. (With no migration 006
  here, Plan B's production gate columns take **006+** in the ladder.)

---

## ⚑ Human sign-off — both DECIDED (2026-07-28, decided by the user; both against the plan's recommendation)

1. **⚑1 — DB-level append-only triggers (migration 006) — DECIDED: code promise
   only.** No migration 006, no triggers; the trigger unit is deleted from this plan.
   Append-only is a **module-surface promise**: no update/delete function ever, pinned
   by the `__all__`/no-mutation-surface guard
   (`test_audit_public_surface_is_exactly_the_declared_writer`, unit 3). Recorded so
   the reasoning survives: this also moots the review's blocker — a `BEFORE DELETE`
   trigger would have broken
   `tests/test_db.py::test_control_audit_id_autoincrements_without_reusing_deleted_ids`
   (test_db.py:984), which proves AUTOINCREMENT never reuses ids **by
   delete-highest-then-insert**; DB-level no-delete enforcement and that executable
   proof cannot coexist without reworking the shipped db suite. The ladder stays at
   005; Plan B's gate columns take 006+.
2. **⚑2 — the minimal `read_audit` reader — DECIDED: defer to Plan C.** The reader
   unit is deleted; `__all__` is the writer alone. The reader arrives with its first
   caller (`sluice audit`), consumes the writer's table via its own query, and MUST
   order by `id` never `ts` — the ordering rule stays prose in this plan (§6
   carry-forward) and becomes executable in Plan C's reader unit, citing unit 2's
   `test_audit_trail_orders_by_id_not_ts`.
