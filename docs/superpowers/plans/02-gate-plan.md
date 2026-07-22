# Plan 02 — `gate` subsystem (Plan A, subsystem 2 of 9)

**Modules owned:** `sluice/core/gate.py` (the contract), `sluice/core/mock_gates.py`
(the reference implementations), plus **migration 005** appended to
`sluice/core/db.py`'s `MIGRATIONS`.
**Build order:** db → **gate (+ mock gates)** → events → context → profile → reconcile →
audit → control → daemon
**Tests:** consolidated in `tests/test_gate.py` (db precedent: one file per subsystem).

Ground truth: design of record §4 (gate contract), §5 (state model), §7 (loop), §8
(profiles), §12 (reference/mock gates required); errata B1/B2/B3/B4/B6/A6;
**ADR-0002 (binding: all-failures-quarantine; gates never write run state)**;
ADR-0001 (docstring standard — every unit's code follows it);
`docs/superpowers/plans/01-db-plan.md` (F3, migration framework, unit-8 additive proof);
subsystem map (PP = python-poller).

---

## 1. Scope & non-goals

### Owned

| Thing | Public surface |
|---|---|
| Outcome vocabulary | `GateOutcome` (SUCCESS / FAILURE / HELD / SKIPPED) |
| Result record | `GateResult(outcome, reason, metadata)` — frozen |
| The task object | `Gate(...)` — frozen dataclass of ten fields, **function references, never methods** |
| Gate-set validation | `validate_gates(gates) -> tuple[Gate, ...]` (the mechanical B3 guard) |
| Reference gates | `MOCK_ALPHA`, `MOCK_BETA`, `MOCK_GATES` in `sluice/core/mock_gates.py` |
| Schema | migration `(5, "005_mock_gate_columns", ...)` — `mock_alpha_done`, `mock_beta_done` on `runs` |

`gate.py` declares `__all__ = ["Gate", "GateOutcome", "GateResult", "validate_gates"]`;
`mock_gates.py` declares `__all__ = ["MOCK_ALPHA", "MOCK_BETA", "MOCK_GATES"]`.
Surface tests assert equality against these (db unit-7 precedent).

### Explicitly NOT owned (and where it belongs)

- **Applicability** (`required|optional|disabled`) — **subsystem 5 (`profile`)**. The Gate
  record carries no applicability field; the fields-equality test (unit 3) makes that
  mechanical.
- **`RunContext` / `resolve_context`** — **subsystem 4 (`context`)**. Gate functions here
  take a duck-typed `context` whose required surface is exactly **one attribute,
  `workdir`** (a `pathlib.Path`); see §3.4. Subsystem 4 must provide it and may then
  tighten `run`'s annotation from `Callable[[Any], GateResult]` to
  `Callable[[RunContext], GateResult]` (typing-only change).
- **Wide events, the `Notifier` Protocol, B8/B9** — **subsystem 3 (`events`)** (db plan
  precedent line); nothing in this subsystem emits events — gate outcomes reach
  observability through the loop.
- **`next_gate`, completeness, the SKIPPED-persistence policy** — **subsystem 6
  (`reconcile`)**. See the carry-forward in §3.2.
- **Writing satisfaction columns, error columns, or any `runs` state** — **the loop**
  (control ops, subsystem 8; daemon persist step, subsystem 9). ADR-0002 decision 2. No
  unit in this plan routes a `runs` write through gate code; unit 9's integration test
  plays the loop's role explicitly.
- **`set_gate`'s refusal to fabricate load-bearing outputs, `reset_gate`/`backtrack`
  protection of the experiment gate (B2/§9)** — **subsystem 8 (`control`)**, keyed on the
  `produces` field this plan defines. See §2 B2.
- **Timeout enforcement (B6)** — **subsystem 9 / Plan B**. See §2 B6.
- **Production gates and their columns** (`download_complete`, `checksum_ok`, `merged`,
  `nvd_done`, `transferred`, `registered`), the 6-vs-8-gate question — **Plan B / errata
  B1**. `tests/test_db.py::test_no_production_gate_columns_yet` forbids exactly those six
  names and **must stay green and untouched**; the mock columns are named
  `mock_alpha_done` / `mock_beta_done` precisely so they are disjoint from that set.

---

## 2. Errata & ADR resolutions in scope — decisions taken, no TBDs

### B4 — retry/quarantine policy. RESOLVED by ADR-0002 decision 1 (human-decided).

**Every gate failure quarantines the run.** `FAILURE` → the loop sets `errored_at` +
`error_reason`; recovery is only an explicit operator `clear_error`. The Gate contract
carries **no `retryable` and no `max_attempts` fields in Plan A** — the fields-equality
tests (units 2–3) make adding one a visible contract change, and unit 9's choreography
test shows the quarantine leg end-to-end. This **supersedes** the db plan §2's
carried-forward recommendation ("add `retryable` + `max_attempts`"); that recommendation
is dead. Consequences carried forward by name:
- **Subsystem 8 must delete `reset_attempts` from the §9 op list** — no policy reads a
  counter, so no counter exists and no `attempts` column is ever added (exactly the db
  plan's "a column no code reads is worse" logic, now permanent).
- Subsystems 6 and 9 cite ADR-0002 instead of reopening B4. If retry is ever wanted, it
  arrives as a new ADR, never a quiet field.

### ADR-0002 decision 2 vs spec §4's `invalidate` sentence. RESOLVED.

Spec §4 says `invalidate` "clears the gate's `satisfied_column` and any `produces`."
ADR-0002 (later, binding) says no gate function touches any `runs` column. Read the spec
sentence as describing the **combined effect of the reset choreography**, not the gate's
own writes: `invalidate(context)` undoes **on-disk/external artifacts only** (the
`produces`); the **caller** — `reset_gate` (subsystem 8) or the daemon honoring
`run_from_fresh` (subsystem 9) — clears the satisfaction column in its own short
transaction. `invalidate`'s docstring must state this (ADR-0001 why-paragraph); unit 9's
choreography test demonstrates it. Mock gates are tested by asserting on the returned
`GateResult` and the filesystem, never on DB side effects.

### B3 — `Gate.order` is TRUE execution order, made mechanical. RESOLVED.

PP's `PIPELINE_GATE_COLUMNS` (`poller_state_db.py:32-42`) lists `downloaded` before
`labkey_registered`, but registration executes **first** and download needs the
`experiment_id` it produces (true order: `poller.py:1468` → `:1556` → `:1585` → `:1157` →
`:1176` → `:1192` → `:1231` → `:1264`). Transcribing the column list into `order`
produces a profile that deadlocks. Resolution has three parts:
1. `Gate.order`'s docstring is the canonical statement: *order is true execution order,
   never display/column numbering* — this is also the content of the dangling "§0 note"
   (errata A6, resolved editorially here).
2. `validate_gates` (unit 4) **rejects any gate set where a `need` does not have a
   strictly lower `order` than its needer** — the naive PP transcription now fails a test
   instead of deadlocking a daemon. Plan B's registry must pass through it.
3. Because orders are unique and every need strictly precedes its needer, the gate graph
   is acyclic by construction — **do not add a DFS cycle check**; the strict order is the
   topological witness.

### B2 — `produces` semantics (the gate-contract half). RESOLVED here; policy half carried to subsystem 8.

`produces: tuple[str, ...]` is the declared list of **artifacts/state this gate creates**
— opaque string identifiers, honest and exhaustive (everything `invalidate` will remove,
plus any load-bearing state like `experiment_id` in Plan B). Its two consumers:
`invalidate` (this plan — the mocks' `produces` name exactly the files their
`invalidate` deletes) and `set_gate`/`reset_gate` policy (subsystem 8 — `set_gate` must
refuse to mark satisfied a gate whose `produces` it cannot fabricate unless `force`;
`reset_gate`/`backtrack` on an experiment-producing gate must be refused or protected,
because clearing `experiment_id` orphans on-disk artifacts and a real LabKey row).
**Carry-forward, named:** the refusal tests cannot exist until subsystem 8 (no
experiment-producing gate exists in Plan A); subsystem 8's plan must specify them keyed
on `produces` and cite this section.

### B6 — per-gate timeout. NOT a Plan A field; deferral ⚑ APPROVED (2026-07-21, decided by the user).

The contract's field list is closed at the ten fields in §3.1 (task constraint; unit 3
enforces by equality). A `timeout_seconds` field with no enforcer would be a lie — same
logic as B4's dead counter. **Recommendation (deferral APPROVED 2026-07-21, decided by
the user; executed at subsystem 9 / Plan B planning):** add
`timeout_seconds: float | None = None` *in the same change* that builds enforcement
(daemon-side, expiry → `GateOutcome.FAILURE`, which quarantines per ADR-0002 — the
wedged-merge incident, PP `poller.py:988,1003`, then produces an operator-visible
quarantine instead of a blocked poller). The fields-equality test is edited consciously
at that moment.

### F3 (db plan fork) — where migration 005's function lives. REVISED; ⚑ APPROVED (2026-07-21, decided by the user).

The db plan's F3 resolution gave migration 005 to this subsystem (unchanged here) but its
rationale sentence said the literal strings `mock_alpha_done`/`mock_beta_done` must not
appear in `sluice/core/db.py`. That sentence cannot survive contact with the mechanics,
and this plan revises it: **`_migration_005_mock_gate_columns` is defined in
`sluice/core/db.py` and appended to `MIGRATIONS` there.** Why the alternatives lose:
- *Define the fn in `mock_gates.py`, import it from `db.py`:* breaks the shipped guard
  `test_db_imports_only_stdlib` (ast-level, catches function-body imports too), and makes
  the foundation module's importability hostage to a leaf.
- *Self-registration (mock_gates appends to `db.MIGRATIONS` at import time):* the ladder
  applied by `initialize_database` would depend on **import order** — a nondeterministic
  schema. Forbidden outright; unit 8's truncated-ladder test would catch the symptom.
- *The decisive argument:* Plan B's production-gate migrations face the same question,
  and for them core **cannot** import the fn from `sluice/gates` — `initialize_database`
  must work with `sluice.gates` absent (design §2.5, `tests/test_dependency_arrows.py`).
  So migration fns for gate columns **must** live in core regardless; `db.py`'s ladder is
  the one home, and 005 sets the precedent 006+ will follow.

What F3 actually protects survives intact: db's **machinery and exports** (primitives,
predicates, `__all__`) name no gates — only the append-only schema changelog does, and
the schema itself names gate columns by design (§5). The testable content is preserved by
relocating the guard: migrations **001–004 alone** yield no mock columns (unit 8).
The db subsystem shipped 001–004 and stopped; no shipped migration is edited; ownership
of 005 stays with this subsystem. **Placement revision APPROVED 2026-07-21 (decided by
the user).**

### B1 — deliberately NOT pre-empted.

No production gate name, column, or ordering decision appears anywhere in this subsystem.
`test_no_production_gate_columns_yet` stays green untouched (§1).

### C4 — deliberately NOT resolved here; deferred by name to subsystem 6 + Plan B (user-approved 2026-07-21).

Never stamp completion over a gate that was skipped rather than satisfied — PP stamps
`completed_at` unconditionally outside any Globus guard (`poller.py:1614`; the NVD path
has the guard, `poller.py:1127-1134`). Adjacent to §3.2's SKIPPED gap: subsystem 6 must
resolve its SKIPPED-persistence choice and its completion predicate **together**, so
"skipped" never masquerades as "satisfied" when completion is evaluated, and Plan B's
globus gate inherits the guard.

---

## 3. The contract, authoritative

### 3.1 Records

```python
# sluice/core/gate.py — stdlib imports only (enum, dataclasses, typing/collections.abc)

class GateOutcome(enum.Enum):
    SUCCESS = "success"   # loop may set satisfied_column = 1
    FAILURE = "failure"   # loop sets errored_at + error_reason  (ADR-0002: quarantine, always)
    HELD    = "held"      # terminated mid-run by a hold; loop writes NO satisfaction/error state
    SKIPPED = "skipped"   # gate judged itself inapplicable at runtime

@dataclasses.dataclass(frozen=True)
class GateResult:
    outcome: GateOutcome
    reason: str | None = None
    metadata: dict | None = None      # loop persists (e.g. globus_task_id, Plan B)
    # __post_init__: outcome must be a GateOutcome (TypeError otherwise);
    # FAILURE requires a non-empty reason (ValueError otherwise) — see unit 2.

@dataclasses.dataclass(frozen=True)
class Gate:
    id: str                                   # stable key, e.g. "mock_alpha"
    name: str                                 # human label
    satisfied_column: str                     # runs column recording completion (1 = satisfied)
    order: int                                # TRUE execution order (B3/A6) — never column order
    needs: tuple[str, ...]                    # gate ids that must be satisfied first (no default: state your deps)
    produces: tuple[str, ...]                 # artifacts/state created (invalidate + set_gate safety, B2)
    run: Callable[[Any], GateResult]          # THE work; returns, never writes run state (ADR-0002)
    invalidate: Callable[[Any], None]         # undo produces on disk; caller clears the column (ADR-0002)
    terminate: Callable[[Any], None] | None = None   # stop in-flight work on hold; None = not interruptible
    control_point: bool = True                # daemon re-checks control state at internal checkpoints
    # __post_init__: needs/produces coerced to tuple; ValueError if id in needs — see unit 3.

def validate_gates(gates: Iterable[Gate]) -> tuple[Gate, ...]: ...   # see unit 4
```

A4 caveat on the `metadata` comment: `metadata` is **not** a license for a single-gate
submit-and-wait — a resumable handle like `globus_task_id` must reach the DB immediately
after submit, **before** any wait (errata A4; Plan B owns the mechanics), or a daemon
restart re-submits a duplicate multi-TB transfer.

Deliberate non-checks, stated so nobody "fixes" them: `Gate` does **not** validate that
`satisfied_column` is non-empty or schema-live — `update_run`'s whitelist
(`UnknownColumnError`) is the injection/typo defense, and the shipped registry is bound
to the schema by unit 8's cross-binding test. `Gate` does not validate `order >= 0`.

### 3.2 Outcome → loop-write choreography (who writes what; gates write nothing)

| Outcome | The loop (subsystem 9) writes, in its own short txn |
|---|---|
| SUCCESS | `satisfied_column = 1`, persists `metadata` per its policy |
| FAILURE | `error_reason = result.reason`, `errored_at = now_iso()` — run leaves eligibility (`ACTIVE_RUNS_WHERE`) |
| HELD | nothing — partial state stays as-is; `lifecycle='held'` was already the operator's write |
| SKIPPED | **decided by subsystem 6/9.** Recommendation carried forward: treat as satisfied-for-progress (`satisfied_column = 1`), matching §8's "disabled is treated as already-satisfied"; persisting nothing would make `next_gate` return the same gate forever (livelock). Subsystem 6's plan must resolve this, citing here. |

The schema CHECK `(error_reason IS NULL) = (errored_at IS NULL)` is why `GateResult`
refuses a reasonless FAILURE (unit 2): otherwise the loop must fabricate a reason or
violate the CHECK.

### 3.3 Mock gates (`sluice/core/mock_gates.py`) — behavior spec

Both are pure functions of `(context.workdir, filesystem)` returning `GateResult`; no DB,
no network, no sleep. Imports: stdlib + `sluice.core.gate` only; **`sqlite3` and
`sluice.core.db` are forbidden imports** (structural ADR-0002 guard, unit 7).

**`MOCK_ALPHA`** — the touch-file reference gate (design §12), not interruptible.
`id="mock_alpha"`, `satisfied_column="mock_alpha_done"`, `order=1`, `needs=()`,
`produces=("mock_alpha.marker",)`, `terminate=None`.
- `run(ctx)`: writes `ctx.workdir / "mock_alpha.marker"` (idempotent — overwrite is
  fine), returns `GateResult(SUCCESS, metadata={"marker": str(path)})`.
- `invalidate(ctx)`: deletes the marker, `missing_ok` (a `reset_gate` on a never-run gate
  must not crash).

**`MOCK_BETA`** — the scriptable, interruptible gate.
`id="mock_beta"`, `satisfied_column="mock_beta_done"`, `order=2`,
`needs=("mock_alpha",)`, `produces=("mock_beta.marker", "mock_beta.terminated")`.
- `run(ctx)`: reads `ctx.workdir / "mock_beta.instruction"` and dispatches on its
  stripped content:
  - file **absent** or `"success"` → writes `mock_beta.marker`, returns SUCCESS;
  - `"failure"` → FAILURE, reason names the scripted failure; **no marker**;
  - `"held"` → HELD; no marker (partial state as-is);
  - `"skipped"` → SKIPPED; no marker;
  - anything else (including an empty file) → **FAILURE** with the literal bad
    instruction in `reason` — fail-loud, never a silent fallback (guardrail §8's
    no-silent-defaults stance, applied to the reference implementation).
- `terminate(ctx)`: writes `mock_beta.terminated` and returns `None` — a visible trace so
  daemon tests can assert termination happened by reading state, and **no `GateResult`**
  (terminate never reports an outcome; the loop already knows it held).
- `invalidate(ctx)`: deletes `mock_beta.marker` **and** `mock_beta.terminated`,
  `missing_ok` (a stale terminated flag would pollute a re-run's forensics).

`MOCK_GATES = (MOCK_ALPHA, MOCK_BETA)` — in execution order, valid under
`validate_gates`. The instruction-file protocol is what lets daemon/control integration
tests (subsystems 8–9) script outcomes through the filesystem while asserting only on
state — no monkeypatching a running daemon.

### 3.4 The duck-typed context contract

Until subsystem 4 exists, `context` is any object with a **`workdir: pathlib.Path`**
attribute — nothing else. Tests use `types.SimpleNamespace(workdir=tmp_path)`. Module
docstring of `mock_gates.py` states this as the contract subsystem 4 inherits: RunContext
must provide `workdir`. Do not grow the mocks' context surface.

### 3.5 Migration 005 (appended to `MIGRATIONS` in `sluice/core/db.py`)

```sql
-- 005_mock_gate_columns   (fn: _migration_005_mock_gate_columns; see §2 F3-revision)
ALTER TABLE runs ADD COLUMN mock_alpha_done INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN mock_beta_done  INTEGER NOT NULL DEFAULT 0;
```

`INTEGER NOT NULL DEFAULT 0`, never nullable: a nullable satisfaction column gives
reconcile three-valued logic (`WHERE col = 0` silently misses NULL). `DEFAULT 0` is a
constant, so the ALTER is legal on a populated table (db plan §4 note), and pre-existing
rows land as **unsatisfied** — correct, they never ran the gates. Tuple entry:
`(5, "005_mock_gate_columns", _migration_005_mock_gate_columns)` — the existing
contiguity validation (runtime check in `apply_migrations` +
`test_migration_versions_are_contiguous_and_unique`) accepts 5 with zero edits; the
generic db tests (apply-all, idempotency, truncated-ladder) absorb it unchanged, with
**one named exception**:
`test_db.py::test_all_four_migrations_yield_version_four_and_every_object` initializes
over the full ladder and hardcodes the version-4 end state, so it goes red when entry 5
lands and is updated to the version-5 end state in unit 8. **A shipped migration
(001–004) is never edited.**

---

## 4. Units, in dependency order (9 units)

Every unit is one RED → GREEN → REFACTOR cycle starting from the named failing test —
**except unit 9**, which adds no production code and is green-on-arrival by design:
integration/characterization tests that pin the composed behavior of units 1–8 (parallel
to the relocated guard landing green in unit 8 step (c)).
Tests live in `tests/test_gate.py`. A small local factory
`make_gate(**overrides) -> Gate` (defaults: a no-op `run` returning
`GateResult(GateOutcome.SUCCESS)`, no-op `invalidate`, `needs=()`, `produces=()`) keeps
ad-hoc gate construction one line; define it at the top of the test file in unit 3.

---

### Unit 1 — `GateOutcome` + module bootstrap

RED: `tests/test_gate.py` fails at `import sluice.core.gate` (module absent).

**Tests**
- `test_gate_outcome_serializes_to_the_spec_strings` —
  `{o.name: o.value for o in GateOutcome} == {"SUCCESS": "success", "FAILURE":
  "failure", "HELD": "held", "SKIPPED": "skipped"}`. Equality, not subset: the four
  values are the persisted/logged vocabulary (§4) and the closed outcome set is itself
  the B4 resolution's surface — no `TRANSIENT`, no `RETRY` member may creep in.
- `test_gate_imports_only_stdlib` — ast-parse `sluice/core/gate.py`; every
  `Import`/`ImportFrom` root is in `sys.stdlib_module_names` (same technique as
  `test_db.py::test_db_imports_only_stdlib`). Guards `gates → core` at the file level,
  ahead of `tests/test_dependency_arrows.py` (which auto-discovers new core modules via
  `pkgutil.walk_packages` — no edit needed there).

---

### Unit 2 — `GateResult`

**Tests**
- `test_gate_result_fields_are_exactly_outcome_reason_metadata` —
  `[f.name for f in dataclasses.fields(GateResult)] == ["outcome", "reason",
  "metadata"]`, and `reason`/`metadata` defaults are `None`. Equality is the point: no
  `retryable`, no `attempts` (B4/ADR-0002).
- `test_gate_result_failure_requires_a_reason` — `GateResult(GateOutcome.FAILURE)` and
  `GateResult(GateOutcome.FAILURE, reason="  ")` both raise `ValueError` whose message
  contains `"reason"`; `GateResult(GateOutcome.FAILURE, reason="boom")` constructs.
  Docstring cites §3.2: the schema CHECK makes a reasonless FAILURE unpersistable.
- `test_gate_result_rejects_a_non_outcome` — `GateResult("success")` raises `TypeError`.
  The loop branches with `is` on the enum; a stray string would silently mis-branch at
  persist time — fail at construction instead.

---

### Unit 3 — `Gate`

**Tests**
- `test_gate_contract_fields_are_exactly_the_spec_ten` —
  `[f.name for f in dataclasses.fields(Gate)] == ["id", "name", "satisfied_column",
  "order", "needs", "produces", "run", "invalidate", "terminate", "control_point"]`;
  `terminate` default `is None`; `control_point` default `is True`; `needs` and
  `produces` defaults are `dataclasses.MISSING` (a gate author must state dependencies
  explicitly — forgetting `needs` is the B3 deadlock's cousin). **This one test carries
  three errata:** no `retryable`/`max_attempts` (B4), no applicability field (§4 — policy
  lives in the profile), no `timeout_seconds` (B6 deferred). Editing it is a conscious
  contract change requiring an ADR or plan revision.
- `test_gate_records_are_frozen` — assigning to `Gate.order` and `GateResult.outcome` on
  instances raises `dataclasses.FrozenInstanceError`. Pins the spec §4 `frozen=True`
  choice (someone could remove the flag).
- `test_gate_coerces_needs_and_produces_to_tuples` — construct with `needs=["a"]`,
  `produces=["p"]`; both stored values are `tuple`. A shared list mutated later would
  change a "frozen" record from the outside.
- `test_gate_rejects_a_self_need` — `make_gate(id="x", needs=("x",))` raises `ValueError`
  naming `"x"`. A self-need can never be satisfied — an undetectable reconcile deadlock;
  fail at construction, the only place it is locally checkable.

---

### Unit 4 — `validate_gates` (B3 made mechanical)

```python
def validate_gates(gates: Iterable[Gate]) -> tuple[Gate, ...]: ...
```
Validates a gate set as a whole and returns it **sorted ascending by `order`** (the
execution order subsystems 5/6 consume). Raises `ValueError` naming the offending
gate id(s) for: duplicate `id`s; duplicate `satisfied_column`s; duplicate `order`
values; a `need` naming a gate not in the set; a `need` whose `order` is not strictly
lower than its needer's. No cycle detection — see §2 B3 point 3; the docstring says so.

**Tests**
- `test_validate_gates_returns_execution_order` — pass `[beta_like, alpha_like]`
  (orders 2 and 1); returns them sorted `(alpha_like, beta_like)`.
- `test_validate_gates_rejects_duplicate_ids` — two gates with `id="dup"` →
  `ValueError`, message contains `"dup"`.
- `test_validate_gates_rejects_duplicate_satisfied_columns` — two gates sharing
  `satisfied_column="done"` → `ValueError`. One gate's success would silently mark the
  other satisfied — ambiguous completion.
- `test_validate_gates_rejects_duplicate_orders` — two gates with `order=1` →
  `ValueError`. Equal orders make "lowest order first" (§7) nondeterministic.
- `test_validate_gates_rejects_an_unknown_need` — `needs=("ghost",)` → `ValueError`
  containing both `"ghost"` and the needer's id.
- `test_validate_gates_rejects_a_need_that_does_not_precede` — **the B3 test.**
  Reconstruct the PP trap shape: `download` at `order=1` with
  `needs=("registration",)`, `registration` at `order=2` → `ValueError` naming both.
  Docstring cites errata B3 (`PIPELINE_GATE_COLUMNS`, PP `poller_state_db.py:32-42`,
  lists download before the registration that must execute first — the naive
  transcription deadlocks; this test makes it fail at registry construction instead).
- `test_gate_exports_exactly_the_documented_surface` —
  `set(sluice.core.gate.__all__) == {"Gate", "GateResult", "GateOutcome",
  "validate_gates"}` (scope guard, db unit-7 precedent).

---

### Unit 5 — `MOCK_ALPHA`

RED: `import sluice.core.mock_gates` fails. Behavior per §3.3.

**Tests** (all use `ctx = types.SimpleNamespace(workdir=tmp_path)` — the whole context
contract, §3.4; all assert on the **returned `GateResult` and the filesystem only**,
never the DB — ADR-0002)
- `test_mock_alpha_run_touches_its_marker_and_reports_success` —
  `result = MOCK_ALPHA.run(ctx)`; `result.outcome is GateOutcome.SUCCESS`;
  `(tmp_path / "mock_alpha.marker").exists()`;
  `result.metadata["marker"] == str(tmp_path / "mock_alpha.marker")`.
- `test_mock_alpha_run_is_reentrant` — call `run` twice; the second call also returns
  SUCCESS and does not raise. Encodes §4's "idempotent enough to re-enter" on the
  reference implementation.
- `test_mock_alpha_invalidate_is_the_inverse_and_idempotent` — after a successful run,
  `MOCK_ALPHA.invalidate(ctx)` removes the marker; calling `invalidate` again on the
  clean directory does not raise.
- `test_mock_alpha_record` — `satisfied_column == "mock_alpha_done"`; `needs == ()`;
  `terminate is None` (the not-interruptible branch of the contract);
  `produces == ("mock_alpha.marker",)` (produces names exactly what invalidate clears —
  B2 honesty).

---

### Unit 6 — `MOCK_BETA`

**Tests**
- `test_mock_beta_follows_its_instruction_file` — parametrized over
  `(instruction, expected_outcome, marker_expected)`:
  `(<absent>, SUCCESS, True)`, `("success", SUCCESS, True)`, `("failure", FAILURE,
  False)`, `("held", HELD, False)`, `("skipped", SKIPPED, False)`. Asserts
  `result.outcome is expected`, marker existence matches, and for FAILURE that
  `result.reason` is a non-empty string.
- `test_mock_beta_fails_loud_on_an_unknown_instruction` — instruction `"retry"` →
  outcome is FAILURE and `"retry"` appears in `result.reason`. An unrecognized script is
  an error, not a silent success (guardrail §8 posture; also what keeps a typo'd daemon
  integration test from passing vacuously).
- `test_mock_beta_terminate_leaves_a_visible_trace` — `MOCK_BETA.terminate(ctx)` returns
  `None`; `(tmp_path / "mock_beta.terminated").exists()`; no `mock_beta.marker` was
  created. Terminate stops work and reports nothing — the loop records the hold, not the
  gate (§4, decision A).
- `test_mock_beta_invalidate_removes_all_its_artifacts` — after a successful run and a
  terminate call, `invalidate` removes both `mock_beta.marker` and
  `mock_beta.terminated`; a second call does not raise.
- `test_mock_beta_record` — `needs == ("mock_alpha",)`;
  `satisfied_column == "mock_beta_done"`; `terminate is not None`;
  `produces == ("mock_beta.marker", "mock_beta.terminated")`.

---

### Unit 7 — `MOCK_GATES` registry + mock-module hygiene

**Tests**
- `test_mock_gates_is_a_valid_ordered_registry` —
  `validate_gates(MOCK_GATES) == MOCK_GATES` (already in execution order and passes
  every rule, including B3's needs-precede) and
  `[g.id for g in MOCK_GATES] == ["mock_alpha", "mock_beta"]`.
- `test_mock_gates_import_only_gate_and_stdlib` — ast-parse
  `sluice/core/mock_gates.py`: every import root is in `sys.stdlib_module_names` or is
  `sluice`, every `sluice.*` import is exactly `sluice.core.gate`, and — asserted by
  name so the message teaches — neither `sqlite3` nor `sluice.core.db` appears.
  **The structural ADR-0002 guard:** a gate module that cannot reach a SQL client or the
  db primitives cannot write run state. Also forecloses a `db ↔ mock_gates` import cycle.
- `test_mock_gates_exports_exactly_the_documented_surface` —
  `set(sluice.core.mock_gates.__all__) == {"MOCK_ALPHA", "MOCK_BETA", "MOCK_GATES"}`.

---

### Unit 8 — migration 005 + F3 guard relocation

The only unit that edits `sluice/core/db.py` (appends the fn + tuple entry per §3.5) and
`tests/test_db.py` (**two pre-authorized edits, named below, and no others** — the first
pre-authorized by db plan unit 5's relocation note, the second by §3.5's named
exception). Cycle discipline, in order: (a) write the three new tests below — RED;
(b) implement 005 — they go GREEN, and two `test_db.py` tests go RED **by design**:
`test_no_mock_gate_columns_in_db_subsystem`, and
`test_all_four_migrations_yield_version_four_and_every_object` (it initializes over the
full ladder and hardcodes `schema_version == 4`); (c) in the same cycle, delete the
first and land its successor `test_mock_columns_arrive_only_in_migration_005` here —
never weaken it, relocate it — and update the second **in place** to the version-5 end
state: rename it `test_all_five_migrations_yield_version_five_and_every_object`, assert
`schema_version(connection) == 5`, docstring updated to note migration 005 is subsystem
2's (this plan, §3.5). Keep its end-state literal hardcoded — do **not** generalize it
to `max(MIGRATIONS)`, which would pin nothing; it is edited consciously each time the
ladder grows (the fields-equality discipline, applied to schema). Do not touch
`test_no_production_gate_columns_yet` (§1) or any other db test — the remaining ladder
tests are generic over `MIGRATIONS` and absorb entry 5 unchanged (verify: full suite
green at the end of the cycle with exactly those two `test_db.py` edits).

**Tests**
- `test_migration_005_adds_the_mock_satisfaction_columns` — `initialize_database` on a
  fresh path, then `insert_run` a minimal row: `{"mock_alpha_done", "mock_beta_done"}`
  is a subset of the `runs` columns, and the new row reads back `0` (exactly, not
  `None`) for both — new runs start unsatisfied, and satisfaction is two-valued (§3.5).
- `test_migration_005_lands_on_a_populated_database` — monkeypatch `MIGRATIONS`
  truncated to 001–004 (the exact technique of
  `test_db.py::test_index_migration_applies_to_a_pre_existing_database`), initialize,
  insert a run; restore the full ladder, `apply_migrations`; the pre-existing row
  survives with both mock columns `== 0`. This is the greenfield-drain story: rows that
  predate 005 are unsatisfied, never NULL, never spuriously satisfied.
- `test_mock_columns_arrive_only_in_migration_005` — with `MIGRATIONS` truncated to
  001–004, no `runs` column name contains `"mock"`. **The relocated F3 guard**: the db
  subsystem's own migrations name no gates; the columns exist only because 005 adds them.
- `test_every_mock_gate_column_is_live_in_the_schema` — after full initialization,
  `{g.satisfied_column for g in MOCK_GATES} <= runs_columns`. **The cross-binding
  test** for the one seam this plan creates (column names appear in both `db.py`'s
  ladder and `mock_gates.py`'s records): a rename on either side fails here, not at
  daemon runtime as `UnknownColumnError`.

---

### Unit 9 — subsystem integration: the ADR-0002 write model, end-to-end

Two tests that compose the gate records with the real db substrate, with **the test
playing the loop** — the executable statement of who writes what, which subsystems 6, 8
and 9 will implement and cite.

**Tests**
- `test_gate_functions_leave_the_database_untouched` — initialize (through 005), insert
  run `"r1"`, snapshot `dict(get_run(conn, "r1"))` and `all_metadata(conn)`; call
  `MOCK_ALPHA.run(ctx)`, `MOCK_BETA.run(ctx)` scripted to `"failure"`,
  `MOCK_BETA.terminate(ctx)`, `MOCK_ALPHA.invalidate(ctx)` — the context carries only
  `workdir`, no handle to the DB at all; re-read: the row dict and metadata are
  identical, `schema_version` still `5`. ADR-0002 decision 2, proven on every gate
  function class (run/terminate/invalidate) at once.
- `test_loop_choreography_run_persist_quarantine_reset` — one flowing test (db plan's
  lifecycle-journey precedent), each step commented with the subsystem that will own the
  write:
  1. `MOCK_ALPHA.run(ctx)` → SUCCESS; *as the daemon (9):*
     `update_run(conn, "r1", {"mock_alpha_done": 1})`; `get_run` shows `1`.
  2. Script beta `"failure"`; `MOCK_BETA.run(ctx)` → FAILURE with reason; *as the
     daemon:* `update_run(..., {"error_reason": result.reason, "errored_at":
     now_iso()})`; `"r1"` is **absent** from `select_active_runs` — the quarantine leg
     of ADR-0002 decision 1, observable: every failure quarantines, no retry.
  3. *As `clear_error` (8):* clear both error columns together; `"r1"` is eligible
     again — the **only** recovery path (no automatic retry exists to test).
  4. *As `reset_gate` (8):* `MOCK_ALPHA.invalidate(ctx)` (marker gone) **then**
     `update_run(..., {"mock_alpha_done": 0})` in the caller's write — the §2
     invalidate-choreography split; column reads `0`, gate is re-runnable.

---

## 5. Invariant checks

| Invariant | How this subsystem could violate it | Guarding test |
|---|---|---|
| **Gates never write run state; the loop is the sole writer** (ADR-0002 d.2) | a mock gate opening the DB; `invalidate` clearing its column | `test_mock_gates_import_only_gate_and_stdlib` (unit 7, structural) + `test_gate_functions_leave_the_database_untouched` (unit 9, behavioral) |
| **All failures quarantine; no retry fields** (ADR-0002 d.1 / B4) | `retryable`/`max_attempts`/`attempts` creeping into the records | fields-equality tests (units 2–3) + the quarantine leg of `test_loop_choreography_run_persist_quarantine_reset` (unit 9) |
| **`gates → core`, never the reverse; core imports with gates absent** | gate/mock modules importing `sluice.gates`/api/cli or third-party code | `test_gate_imports_only_stdlib` (unit 1), `test_mock_gates_import_only_gate_and_stdlib` (unit 7); `tests/test_dependency_arrows.py` auto-covers both new modules |
| **Reconcile is a pure function of state** | order/needs contradictions that make "next runnable gate" ambiguous or deadlocked | `validate_gates` unit 4 tests — dup orders, unknown need, B3 needs-precede — + `test_mock_gates_is_a_valid_ordered_registry` (unit 7) |
| **Applicability lives in the profile, not the gate** | an applicability/required field on `Gate` | `test_gate_contract_fields_are_exactly_the_spec_ten` (unit 3) |
| **Forward-only, additive schema; never edit a shipped migration** | editing 001–004; mock columns leaking into them; nullable satisfaction columns | `test_mock_columns_arrive_only_in_migration_005`, `test_migration_005_lands_on_a_populated_database`, `test_migration_005_adds_the_mock_satisfaction_columns` (unit 8); existing contiguity tests stay green unedited |
| **B1 not pre-empted** | naming a mock column after a production gate | existing `test_db.py::test_no_production_gate_columns_yet` (untouched) — mock names are disjoint by §1 |
| **No silent fallbacks** | mock beta defaulting an unknown instruction to success | `test_mock_beta_fails_loud_on_an_unknown_instruction` (unit 6) |
| **Short transactions only** | (nothing here opens transactions; gates hold no connection) | inherited — db unit 9/11 guards; unit 9's choreography uses autocommit primitives only |

---

## 6. Done criteria

1. `pixi run test` fully green: all pre-existing tests (including
   `test_no_production_gate_columns_yet` and `test_dependency_arrows.py`) plus
   `tests/test_gate.py`; `test_no_mock_gate_columns_in_db_subsystem` no longer exists,
   superseded by `test_mock_columns_arrive_only_in_migration_005`; the db end-state test
   is renamed `test_all_five_migrations_yield_version_five_and_every_object` and asserts
   version 5 (unit 8's second pre-authorized edit).
2. `sluice/core/gate.py` imports only stdlib; `sluice/core/mock_gates.py` imports only
   stdlib + `sluice.core.gate` — neither names `sqlite3` nor `sluice.core.db`.
3. `initialize_database` on a fresh path yields `schema_version == 5` and a `runs` table
   carrying `mock_alpha_done`/`mock_beta_done` as `INTEGER NOT NULL DEFAULT 0`; the same
   ladder applied over a populated 004-level DB preserves rows with both columns `0`.
4. `dataclasses.fields` show `Gate` at exactly the ten contract fields and `GateResult`
   at exactly three — no retry, no applicability, no timeout field.
5. `validate_gates` mechanically rejects the PP column-order transcription (B3) and
   returns execution order; `MOCK_GATES` passes it.
6. All four `GateOutcome`s, `terminate`, and `invalidate` are demonstrated by the mock
   gates with assertions on `GateResult` + filesystem only; unit 9 proves gate functions
   leave the DB byte-identical while the test-as-loop persists every state change.
7. Downstream subsystems can start: 4 (`context`) owes exactly `workdir`; 5 (`profile`)
   consumes `validate_gates`; 6 (`reconcile`) gets pure gate data + the SKIPPED
   carry-forward; 8 (`control`) gets `produces`-keyed set_gate/reset_gate policy and the
   **delete-`reset_attempts`** instruction; 9 (`daemon`) gets the §3.2 choreography and
   the B6 decision point.

## 7. Carry-forwards (named, so no plan rediscovers them)

- **Subsystem 4 (context):** provide `RunContext.workdir`; optionally tighten `run`'s
  annotation to `RunContext` (typing-only). RunContext will also carry a DB path (§6) —
  ADR-0002 still forbids gate-side run-state writes; reachable ≠ permitted.
- **Subsystem 6 (reconcile):** resolve SKIPPED persistence (§3.2, recommendation:
  satisfied-for-progress); cite ADR-0002 rather than reopening B4. Additional owed
  items (user-approved 2026-07-21):
  - the four gate-selection conditions — needs satisfied, `satisfied_column` false,
    required-or-due-optional in the run's profile, lowest `order` first (spec §7);
  - completion is evaluated against the run's stamped `pipeline_version`, never the
    current default profile (spec §5/§8);
  - the remaining completion-definition elements beyond the SKIPPED note — a quantified
    predicate over applicable gates, `next_gate is None` ⇔ complete, optional/disabled
    handling (spec §7/§8);
  - `reconcile.next_gate` is the most-tested unit in the system and owes the full
    selection test matrix (spec §7).
- **Subsystems 6/8:** `run_from_step`/`run_from_fresh` are ONE-SHOT pointers — cleared
  in the same transaction that honors them; honor-without-clear is an infinite-redo bug
  (spec §5/§9).
- **Subsystem 6 + Plan B:** errata C4 — never stamp completion over a gate that was
  skipped rather than satisfied (deferred by name, §2 C4).
- **Subsystem 8 (control):** `set_gate` refusal + `reset_gate`/`backtrack` protection
  keyed on `produces` (§2 B2); **delete `reset_attempts` from the op list** (§2 B4);
  `clear_error` is the sole quarantine recovery; reset choreography = `invalidate` then
  clear the column in the op's own transaction.
- **Subsystem 9 (daemon):** implement the §3.2 outcome table verbatim; call `terminate`
  only on hold; decide B6 (`timeout_seconds` field + enforcement together — deferral
  approved, §2 B6).
- **Plan B:** production gate columns arrive as migration 006+ with fns in core `db.py`
  (the §2 F3-revision precedent); the production registry must pass `validate_gates`,
  which makes a B3 order transcription error a test failure, not a deadlock; resolve
  errata A4 — persist `globus_task_id` immediately after submit, **before** the wait
  (§3.1 A4 caveat).

## ⚑ Human sign-off — both APPROVED (2026-07-21, decided by the user)

1. **§2 F3-revision — APPROVED.** Migration 005's function lives in
   `sluice/core/db.py`'s ladder (ownership and sequencing unchanged; only the db plan's
   file-placement sentence is revised, with the Plan-B-forces-it argument).
2. **§2 B6 — APPROVED.** `timeout_seconds` deferred to the subsystem-9/Plan-B change
   that builds its enforcement (contract field list stays closed at ten for Plan A).
