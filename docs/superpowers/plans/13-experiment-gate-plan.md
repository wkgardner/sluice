# Plan 13 — The `experiment` gate (Plan B, increment 3): gate 0, the first real gate

**Type:** IMPLEMENTATION plan (test-first). **LIGHTWEIGHT MODE** — one agent builds in a
single pass; one review follows. Every unit lands from a named failing test.
**Review verdict: READY TO BUILD. All three ⚑ flags DECIDED (2026-07-31).**

**Milestone.** This is the FIRST production gate, the FIRST that touches an external
service (LabKey), the FIRST that persists a load-bearing user-facing value
(`experiment_id`, Will's reference key), and a **protected / non-resettable** gate. The
central hazard is **errata C1: LabKey experiment creation is non-idempotent — a naive
retry creates a duplicate real experiment.** Care is concentrated on the idempotency /
resume-first orchestration (Unit 3) and the fail-closed adapter (Unit 2).

**Ground truth (read in this order):**
- `docs/superpowers/plans/10-plan-b-gate-map.md` §3 (the gate row), §4 (**C1**, **C2**,
  **B2**, **C5**), §5 (⚑A/⚑B/⚑C), §6 (migration 007 — **note: §6's "one coherent
  migration" is SUPERSEDED here by ⚑3; migrations are incremental, one column per gate**),
  §8 step 4 — **FINAL**.
- `docs/adr/0006-gate-output-persistence.md` (⚑B — `persists` mechanism; this gate is
  its first real user); `docs/adr/0002-gate-failure-and-persistence.md` (gates only
  RETURN; every FAILURE quarantines; the daemon is the sole writer);
  `docs/adr/0005-no-automatic-file-deletion.md` (invalidate never invoked; rewind is
  column-clear only).
- As-built core: `sluice/core/gate.py` (the 11-field `Gate` incl. `persists`, `produces`,
  `control_point`; `validate_gates`), `sluice/core/context.py` (`RunContext.run_row`
  snapshot — the resume read path), `sluice/core/daemon.py:481-525` (the persist
  choreography — **already built** by increment 2, consumes `persists`),
  `sluice/core/control.py:1044` (the B2 `set_gate` fabrication refusal, keyed on
  `gate.produces`), `sluice/core/db.py:214-262` (the migration ladder; `experiment_id`
  INTEGER column since migration 001, `db.py:148`).
- Live poller (**PP**, read-only): `labkey_client.py:117-179` (`create_experiment` — the
  C1-hazardous path), `:182-227` (`get_most_recent_experiment` — the racy
  "most-recent-by-user" query we **reject**), `:54-68` (`format_experiment_description`),
  `:92` (api-key file read), `:102` (the `labkey_project` config key — real **value
  never appears in this doc**, C2), `poller.py:1479-1506` (the caller-side create step),
  `:1481-1485` (the caller-side reuse-existing idempotency we **port into the gate**).
- The detection plan (`plans/11-detection-plan.md` §1) — the world-adapter precedent
  (`sluice/gates/`, injected callable, no-network tests, `KeyError` fails loud).

---

## 1. Scope & non-goals

### Owned (this increment builds)

| Thing | Public surface |
|---|---|
| Migration 007 (core `db.py` ladder) | `_migration_007_labkey_registered` + the `(7, …)` `MIGRATIONS` entry — **⚑3: ONE column, `labkey_registered`** |
| The LabKey world adapter (new, `sluice/gates/labkey.py`) | `make_experiment_client(config, *, api_factory=None) -> ExperimentClient`; `ExperimentClient` (methods `find_experiment_by_run_name(run_name) -> int | None`, `create_experiment(run_name) -> int`); `LabKeyError` |
| The gate (new, `sluice/gates/experiment.py`) | `EXPERIMENT: Gate`; `run_experiment_gate(context, make_client) -> GateResult` (the tested orchestration) |
| Config surface (declared for Plan C's loader; read by subscript, no code default) | `labkey_server`, `labkey_experiments_project` (C2), `labkey_schema`, `labkey_query`, `labkey_api_key_file` (a **path** to a gitignored secret file) |
| Tests | `tests/test_experiment.py` (new — adapter + gate); one migration assertion in `tests/test_db.py` |

### NOT owned (deferred, stated so the engineer does not drift)

- **The daemon persist wiring.** Already built at `daemon.py:481-525` (increment 2): on
  SUCCESS it writes every declared `persists` output **and** the satisfaction column in
  one `update_run`/one `write_txn`, and quarantines a SUCCESS that omitted a declared
  key. This gate only *declares* `persists={"experiment_id": "experiment_id"}`; it writes
  nothing. **Do not touch `daemon.py`.**
- **The `production-v3.1` profile.** It cannot assemble until all eight gates exist
  (`profile.py` validates the full set). This increment ships `EXPERIMENT` as a standalone
  `Gate`, ready to be listed later. See §3 Unit 3 for how the gate is proven **without**
  the full profile.
- **⚑A's `experiment_id`-aware `resolve_context.workdir`** — ⚑2 DEFERS it to the download
  increment. This gate touches no disk (map §3) and runs fine on the name-keyed workdir.
  `context.py` is **unchanged** here.
- **The other six production satisfaction columns** (`downloaded`, `checksummed`,
  `samplesheet_created`, `authority_registered`, `nvd_completed`, `globus_submitted`,
  `clumped_data_backed_up`) — ⚑3: each ships with its own gate in its own migration (§6).
  **Not added here.**
- **The control-layer belt-and-suspenders refusal** of `reset_gate`/`backtrack` on
  `labkey_registered` (map §4 B2) — a Plan C follow-up. This increment supplies the
  structural protection (§2 below); the extra control refusal is a carry-forward.
- **Downloading / paths / the `/staging` symlink** — the `download`/`nvd` gates.
- **The `registration` gate** (a *different* gate, order 4, DISABLED — map §4 B1/E-2).
  "Experiment creation" ≠ "authority-list registration". Not touched.

---

## 2. Errata & decisions in scope — no TBDs

- **C1 (LabKey create is non-idempotent / racy) — THE central hazard.** Resolved three
  ways, all in the gate + adapter:
  1. **Resume-first from state (cheap, no network).** If `context.run_row["experiment_id"]`
     is already set, the gate returns SUCCESS reusing it and **never calls LabKey** (ports
     PP `poller.py:1481-1485` into the gate).
  2. **Reject "most-recent-by-user."** The adapter does **not** port
     `get_most_recent_experiment` (PP `:182-227`) — a concurrent same-user experiment
     poisons it. `labkey_user_id` is **dropped from the config surface.** Identity is
     resolved by the folder-name-bearing `Description` the gate itself wrote (map §4 C1).
  3. **Lookup-before-create (⚑1 APPROVED) closes the crash window.** When `experiment_id`
     is NULL the gate first asks LabKey `find_experiment_by_run_name(run_name)`; only on
     `None` does it `create_experiment`. This is what makes a "created in LabKey, daemon
     died before persist" restart adopt the existing experiment instead of creating a
     **duplicate**.
- **C2 (overloaded `labkey_project`).** This gate uses **`labkey_experiments_project`**
  only (the real value lives at PP `:102`, in gitignored `config.json`, **never here**).
  `labkey_authority_project` is the `registration` gate's key — out of scope.
- **C5 (fail-loud LabKey).** Every load-bearing LabKey failure — unreachable, auth
  failure, insert rejected, id unresolvable, **ambiguous match** — raises `LabKeyError`
  from the adapter → the gate returns `GateResult(FAILURE, …)` → the daemon quarantines
  (ADR-0002). **Fail closed:** an erroring or ambiguous lookup **never falls through to
  create** (that would risk the very duplicate C1 warns about). Never the silent-success
  of PP `labkey_client.py:328-330`.
- **B2 (`experiment_id` is a load-bearing output; the gate is protected).** Concrete
  mechanism, three interlocking parts, **no gate-side enforcement code**:
  - `EXPERIMENT.produces = ("experiment_id",)` — **non-empty on purpose.** This arms the
    already-built control refusal at `control.py:1044`: `set_gate("labkey_registered", 1)`
    without `force` raises `FabricationError` (a state-only op cannot fabricate a real
    LabKey row).
  - `EXPERIMENT.invalidate` is a **deliberate no-op** (⚑C / ADR-0005) that **does not
    clear `experiment_id`.** Clearing it would orphan a real LabKey row and on-disk
    artifacts. This is the one gate where `produces` names a value invalidate must
    **never** touch — documented in the code.
  - Structural safety (already in place): rewind is column-clear only and **never**
    touches `experiment_id` (ADR-0005 d.2), so a reset of `labkey_registered` preserves
    the id; the resume-first gate (part 1 above) then reuses it. **A rewind cannot orphan
    the LabKey row.**
- **⚑B / ADR-0006.** `EXPERIMENT.persists = {"experiment_id": "experiment_id"}`. The gate
  returns `experiment_id` in `GateResult.metadata`; the daemon persists it **atomically
  with `labkey_registered`** (`daemon.py:481-525`). `validate_gates` accepts this because
  the persist target (`experiment_id`) is not the satisfaction column (`labkey_registered`)
  (`gate.py:184-195`).
- **⚑C.** No deletion; `EXPERIMENT.invalidate` is a documented no-op (above).
- **No-default config / no secrets.** The adapter reads its five config keys by
  **subscript** (a missing key is a loud `KeyError`, never a fallback). The LabKey API key
  is a **secret**: config carries only `labkey_api_key_file` (a path); the value is read
  from that gitignored file at first API call and **never** logged or placed in any
  `LabKeyError` message.

### ⚑ Flags — all DECIDED (2026-07-31)

- **⚑1 — APPROVED (require lookup-before-create).** Kept exactly as planned: the gate calls
  `find_experiment_by_run_name` before `create_experiment` whenever `experiment_id` is
  NULL; exact-suffix `"Folder name: <name>"` match; an errored or ambiguous lookup fails
  **closed** (quarantine) and **never** falls through to create. This is the only thing
  that stops a crash-between-create-and-persist from minting a duplicate real experiment —
  a persist to an external service sluice cannot clean up (ADR-0005 spirit). Realized in
  Unit 2 (`find`/`create`) + Unit 3 (orchestration).
- **⚑2 — APPROVED (defer the `experiment_id`-aware `resolve_context.workdir`).** It lands
  with the **download** increment (its first consumer). `context.py` is **unchanged** this
  increment; this gate touches no disk and runs correctly on the name-keyed workdir. The
  carry-forward stays in §6.
- **⚑3 — DECIDED AGAINST "ship whole"; migrations are INCREMENTAL, one per gate.** Per the
  explicit deselection (consistent with the accepted "adding a gate means adding a
  migration" view): **migration 007 adds ONLY `labkey_registered`** — this gate's
  satisfaction column. `experiment_id` already exists (migration 001), so no other column,
  and the other six production satisfaction columns are **not** added here. Each subsequent
  production gate ships its own satisfaction column in its own migration (§6 carry-forward).
  **This supersedes gate map §6's "one coherent migration 007" framing** — flagged in the
  ground-truth note, Unit 1, and §6 so a later reader is not confused.

---

## 3. Unit list (dependency order)

### Unit 1 — Migration 007: the `labkey_registered` satisfaction column (`db.py`)

**Change (⚑3 — INCREMENTAL, ONE column).** Add `_migration_007_labkey_registered(conn)`
mirroring `_migration_005_mock_gate_columns` (`db.py:214-230`): a **single** statement

```
ALTER TABLE runs ADD COLUMN labkey_registered INTEGER NOT NULL DEFAULT 0
```

Append `(7, "007_labkey_registered", _migration_007_labkey_registered)` to `MIGRATIONS`
(`db.py:255-262`). Docstring: this is the `experiment` gate's satisfaction column;
`NOT NULL DEFAULT 0` so reconcile's `WHERE col = 0` never meets three-valued logic and the
ALTER stays legal on a populated table (pre-existing rows land unsatisfied — correct, they
never ran the gate).

**⚑3 supersedes gate map §6.** The map framed migration 007 as "one coherent migration"
carrying all eight production satisfaction columns. That framing is **superseded**:
migrations are incremental — one satisfaction column per gate, shipped with its gate.
Migration 007 adds ONLY `labkey_registered`. **Do NOT add** the other six columns here;
each ships with its own gate/migration (§6). `experiment_id` already exists (migration 001,
`db.py:148`), so there is **no output column** in this migration (ADR-0006 consequence).

**Failing test (`tests/test_db.py`):**
- `test_migration_007_adds_labkey_registered_column`: after `initialize_database`,
  `PRAGMA table_info(runs)` includes `labkey_registered` with `type == "INTEGER"`,
  `notnull == 1`, `dflt_value == "0"`; **and** assert the other six production satisfaction
  columns (`downloaded`, `checksummed`, `samplesheet_created`, `authority_registered`,
  `nvd_completed`, `globus_submitted`, `clumped_data_backed_up`) are **absent** — the
  incremental-migration guard (⚑3): they arrive with their own gates, not here. One test —
  a schema fact, not branching logic. (The ladder-contiguity guard already lives in
  `test_db.py`; no need to re-prove it.)

---

### Unit 2 — The LabKey world adapter (`sluice/gates/labkey.py`, new)

This is where the real C1 logic and fail-closed posture live; it earns an exhaustive
suite. **No network, no `labkey` package in tests:** the labkey library is imported
**lazily** inside the api factory, and every test injects a **fake api** via `api_factory`.

**Module docstring must state:** a world adapter injected at/derived from config; imports
`sluice.core`-nothing and the `labkey` package lazily; it is **not** a `Gate` and appears
in no profile (dependency arrow auto-guarded by `tests/test_dependency_arrows.py`).

**`make_experiment_client(config, *, api_factory=None) -> ExperimentClient`**
- Reads by **subscript** (no `.get` default — no-default invariant): `labkey_server`,
  `labkey_experiments_project`, `labkey_schema`, `labkey_query`, `labkey_api_key_file`.
  A missing key raises `KeyError` (loud, deployment bug).
- **Constructs nothing external:** captures the params (+ `api_factory`, defaulting to a
  private `_default_api_factory` that lazily reads the key file and builds `APIWrapper`).
  **No file read, no network at construction** — so a resuming run (Unit 3) that never
  calls a method never reads the secret.
- Drops `labkey_user_id` entirely (C1).

**`ExperimentClient.find_experiment_by_run_name(run_name) -> int | None`**
- Ports the *resolution* idea from PP `:159-175` but keyed on `Description`, **not**
  "most-recent-by-user" (C1). Builds the api via `self._api_factory()`, `select_rows`
  filtered on `Description` (a server-side CONTAINS on the `"Folder name: " + run_name`
  token; the authoritative match is the exact-suffix check below).
- **Exact-suffix match (F3) / substring-collision guard:** the token is written as the
  **TRAILING** segment of the `Description` (nothing follows it — see `create` below), so
  keep only rows whose `Description` **ends with** exactly `"Folder name: " + run_name`.
  This is an `endswith` check, matching how `create` writes the string, so run `"A"` never
  matches run `"AB"`'s experiment.
- `0` exact-suffix matches → `None`; exactly `1` → `int(ExperimentNumber)`; `>1` → raise
  `LabKeyError` (ambiguous — never guess, the C1 poison). Any api/network exception →
  wrap in `LabKeyError` (fail-closed). The message must **not** contain the api key.

**`ExperimentClient.create_experiment(run_name) -> int`**
- `Description` is a fixed-format string that **ends with** `"Folder name: " + run_name`
  as its **TRAILING token, with nothing after it** (F3) — exactly what `find`'s `endswith`
  matches. **Deliberate simplification of PP `:54-68`:** drop the NVD-version and date
  decorations — pulling the NVD version at *gate 0* would couple it to the `nvd` tool
  (gate 5); nothing downstream parses the `Description` beyond the trailing folder name.
- `insert_rows(schema, query, rows=[{"Description": description}])`; then resolve the id by
  calling `self.find_experiment_by_run_name(run_name)` (the SAME query — insert doesn't
  return the number, PP `:167-175`). If it returns `None` → raise `LabKeyError`
  ("created but ExperimentNumber unresolvable" — fail-loud, mirroring PP `:173-174`, never
  silent). Returns the `int` otherwise. Any api exception → `LabKeyError`.
- **Single-writer resolve note (F2 — put in the docstring):** resolving the just-inserted
  id by `Description` trusts sluice's single-daemon writer (design §2) — one look creates
  one experiment. If a *concurrent* same-folder insert ever existed, the resolve returns
  `>1` and surfaces as an **ambiguous `LabKeyError` (fail-loud, quarantine)** — never a
  silent duplicate. This is a stated trust boundary, not an unhandled race.

**`LabKeyError(Exception)`** — the single fail-closed signal the gate catches; its `str`
never carries the secret.

**Failing tests (`tests/test_experiment.py`, adapter section — fake api):**
- `test_make_experiment_client_missing_required_key_fails_loud`: config lacking
  `labkey_experiments_project` → `KeyError` (no-default). One key pinned; note the others
  are read the same way.
- `test_client_construction_is_network_and_secret_free`: `make_experiment_client` with
  `labkey_api_key_file` pointing at a nonexistent path **succeeds**, and the default api
  factory is never invoked at construction (resume-tolerance at the adapter level).
- `test_find_returns_none_when_no_match`: fake `select_rows` returns `{"rows": []}` →
  `None`.
- `test_find_returns_experiment_number_for_single_exact_match`: one row ending
  `"Folder name: RUN_A"` with `ExperimentNumber=5150`, `run_name="RUN_A"` → `5150`.
- `test_find_disambiguates_substring_run_names`: rows include one ending
  `"Folder name: RUN_AB"`; `run_name="RUN_A"` → `None` (the substring is excluded by the
  `endswith` guard). **A C1 correctness test.**
- `test_find_raises_on_multiple_exact_matches`: two rows both ending
  `"Folder name: RUN_A"` → `LabKeyError` (ambiguous, never guess).
- `test_find_never_queries_by_user_id`: inspect the recorded `filter_array` passed to the
  fake `select_rows` — it filters on `Description`, and there is **no** `modifiedby`/user
  filter (the C1 fix, PP `:212` rejected).
- `test_find_wraps_api_error_as_labkey_error`: fake `select_rows` raises `RuntimeError` →
  `LabKeyError`; assert the message does **not** contain the fake api key.
- `test_create_writes_trailing_folder_name_then_resolves`: `create_experiment("RUN_A")`
  records an `insert_rows` whose `Description` **ends with** `"Folder name: RUN_A"` (F3),
  then a `select_rows` that returns the single match `5150` → returns `5150`; assert insert
  **precedes** the resolve select.
- `test_create_raises_when_id_unresolvable_after_insert`: insert succeeds, resolve
  `select_rows` returns `{"rows": []}` → `LabKeyError` (fail-loud, never returns a fake id).
- `test_create_wraps_insert_error_as_labkey_error`: `insert_rows` raises → `LabKeyError`.

---

### Unit 3 — The `experiment` gate (`sluice/gates/experiment.py`, new)

**The orchestration — `run_experiment_gate(context, make_client) -> GateResult`.**
`make_client: Callable[[], ExperimentClient]` is the injectable seam (the detection
`lister`/`disk_floor_check` precedent — a plain callable, not a Protocol). Logic:

```
existing = context.run_row["experiment_id"]           # snapshot; column present since 001
if existing is not None:
    return GateResult(SUCCESS, metadata={"experiment_id": existing})   # resume; make_client NOT called
run_name = context.run_row["name"]
client = make_client()                                 # only now — construct the adapter
try:
    found = client.find_experiment_by_run_name(run_name)               # ⚑1 crash-window guard, C1
    experiment_id = found if found is not None else client.create_experiment(run_name)
except LabKeyError as err:
    return GateResult(FAILURE, reason=f"experiment gate: LabKey resolution failed for run {run_name!r}: {err}")
return GateResult(SUCCESS, metadata={"experiment_id": experiment_id})
```

Catch **`LabKeyError` only** — a non-`LabKeyError` is a bug and must propagate (the adapter
wraps all world failures). The reason string carries the run name and the (secret-free)
error; the metadata key `"experiment_id"` matches the `persists` mapping so the daemon can
persist it (a mismatch would quarantine via `daemon.py:487-509` — the substrate's guard).

**`run_row` precondition (F5).** `run_experiment_gate` reads `context.run_row["experiment_id"]`
and `["name"]` by **subscript** — both are real `runs` columns present in every snapshot
`resolve_context` builds (`experiment_id` since migration 001; `name` is the primary key).
A future **direct-construction** test MUST populate BOTH keys in its `run_row`; a bare
`KeyError` from an under-populated test `run_row` is a test-setup miss, **not** a gate bug.

**The Gate object + wrappers.**
```
EXPERIMENT = Gate(
    id="experiment",
    name="LabKey experiment creation",
    satisfied_column="labkey_registered",           # map §3; needs migration 007 (Unit 1)
    order=0,                                          # TRUE execution order, first
    needs=(),                                         # order 0
    produces=("experiment_id",),                     # NON-EMPTY: arms control B2 (control.py:1044)
    run=_run_experiment,
    invalidate=_invalidate_experiment,               # deliberate no-op (⚑C/ADR-0005)
    terminate=None,                                   # not interruptible (⚑E — later)
    persists={"experiment_id": "experiment_id"},     # ⚑B/ADR-0006 d.1
)
```
- `_run_experiment(context)` = `run_experiment_gate(context, functools.partial(make_experiment_client, context.config))`.
  Thin production wiring: the gate builds its own adapter from injected config — **no
  daemon seam** (unlike detection, since `sluice/gates/` may import the `labkey` package).
- `_invalidate_experiment(context) -> None`: **documented no-op.** Docstring: production
  invalidate is never invoked automatically (ADR-0005 d.3); it must NOT clear
  `experiment_id` — that would orphan a real LabKey row and on-disk artifacts (B2). This is
  the gate where `produces` names a value invalidate must never touch.

**Proving the gate WITHOUT the `production-v3.1` profile (decision).** The full profile
cannot assemble yet. The daemon persist choreography is **already proven** for any gate
carrying `persists` (increment 2, `test_daemon.py`). So this increment does **not** re-drive
the gate through `process_run`. It proves (a) the decision logic via `run_experiment_gate`
with a **fake whole-client**, and (b) that `EXPERIMENT` carries the exact contract the
already-proven daemon acts on. Composition is then correct by construction.

**Failing tests (`tests/test_experiment.py`, gate section — fake client):**

Orchestration (the risk — exhaustive on the branches):
- `test_resume_reuses_persisted_experiment_id_without_touching_labkey`: `run_row` with
  `experiment_id=4242` (and `name`); a `make_client` whose call is recorded → result is
  `SUCCESS, metadata={"experiment_id": 4242}` **and** `make_client` was called **zero
  times** (no LabKey contact on resume — succeeds even if LabKey is down / config absent).
  **The resume-first milestone test.**
- `test_first_run_finds_none_then_creates`: `experiment_id=None`; fake `find` → `None`,
  `create` → `5150` → `SUCCESS, metadata={"experiment_id": 5150}`; assert `find` was called
  **before** `create`.
- `test_crash_window_adopts_existing_experiment_without_creating`: `experiment_id=None`,
  fake `find` → `5150` → `SUCCESS, metadata={"experiment_id": 5150}` **and** `create` was
  **never called** (⚑1 / C1 — no duplicate on a create-then-crash restart).
- `test_lookup_error_quarantines_and_never_creates`: `experiment_id=None`, fake `find`
  raises `LabKeyError` → `FAILURE` whose reason names the run; assert `create` **never
  called** (fail-closed — a flaky lookup must not fall through to a duplicate create).
- `test_create_error_quarantines`: `experiment_id=None`, `find` → `None`, `create` raises
  `LabKeyError("insert rejected")` → `FAILURE` naming the run (C5). (Covers "id
  unresolvable" too — the adapter surfaces that as `LabKeyError`, Unit 2.)

Contract (light — data pins, not branching):
- `test_experiment_gate_contract`: `EXPERIMENT.id == "experiment"`,
  `satisfied_column == "labkey_registered"`, `order == 0`, `needs == ()`,
  `produces == ("experiment_id",)` (non-empty — the B2 arming), `terminate is None`,
  `persists == {"experiment_id": "experiment_id"}`; and
  `validate_gates((EXPERIMENT,)) == (EXPERIMENT,)` (the persist target differs from the
  satisfaction column, so the `gate.py:184-195` self-clobber guard passes).
- `test_experiment_invalidate_is_noop`: `EXPERIMENT.invalidate(context)` returns `None`,
  raises nothing, and touches no client (⚑C).

Wiring (light — the thin `_run_experiment` wrapper):
- `test_gate_run_resume_path_is_end_to_end_labkey_free`: call `EXPERIMENT.run(context)`
  with a `run_row` carrying `experiment_id=99` (and `name`) and **no labkey config keys** →
  `SUCCESS, metadata={"experiment_id": 99}` (proves the wrapper delegates and that resume
  needs no LabKey config at all).
- `test_gate_run_create_path_delegates_to_client` (monkeypatch
  `sluice.gates.experiment.make_experiment_client` → a fake client): `EXPERIMENT.run` on
  `experiment_id=None` returns the fake's created id — proves the partial wires config into
  `run_experiment_gate`. (Monkeypatch is acceptable for the thin wrapper; the risk logic is
  covered above.)

---

## 4. Invariant checks

| Invariant | Risk here | Guard (test) |
|---|---|---|
| DB is the single source of truth | Gate could cache/mint identity in memory | `experiment_id` is read from `context.run_row` (the sealed snapshot) and written only by the daemon; the gate returns it in metadata. `test_resume_…` (reads snapshot), the whole gate has no DB import |
| State-only control / daemon is the only actor (ADR-0002) | A gate that "creates an experiment" might be read as doing work control should trigger | The gate is daemon-run like any gate; **control** never creates experiments. `EXPERIMENT.run` writes **no** run state; the daemon persists (already-built). No `sqlite3`/`db` import in either new module |
| Gates only RETURN; loop persists (ADR-0002) | `persists` might tempt a gate-side write | `run_experiment_gate` returns a `GateResult`; the daemon writes. Covered by every orchestration test asserting on the returned result, not on DB rows |
| `gates → core`, one-way | New `sluice/gates/*` modules | Both import `sluice.core` (Gate/GateResult/GateOutcome) + the `labkey` package **lazily**; `tests/test_dependency_arrows.py` auto-guards them (they sit on the gates side). Do **not** create a new top-level package |
| Reconcile is a pure function of state | An external-service gate could leak world facts into scheduling | Nothing about LabKey reaches `reconcile`; the gate's only inputs are `context.run_row`/`config`. `reconcile.py` **unchanged** |
| No secrets / no baked defaults | The api key; five config keys | Key read from `labkey_api_key_file` (a path) lazily, never logged, never in `LabKeyError` (`test_find_wraps_api_error…`); all five keys read by subscript (`test_make_experiment_client_missing_required_key_fails_loud`). No value appears in this doc (C2) |
| sluice never deletes files (ADR-0005) | invalidate on a protected gate | `_invalidate_experiment` is a no-op that does not clear `experiment_id` (`test_experiment_invalidate_is_noop`) |
| C1 — no duplicate external experiment | The make-or-break | `test_crash_window_adopts_existing…`, `test_lookup_error_quarantines_and_never_creates`, `test_find_raises_on_multiple_exact_matches`, `test_find_never_queries_by_user_id`, `test_create_raises_when_id_unresolvable_after_insert` |
| Short transactions | None — the gate opens no transaction | The daemon's persist `write_txn` opens only after `run` returns (already-built); this increment adds no txn |

---

## 5. Done criteria (observable, green)

1. Migration 007 applies on a fresh DB; `runs` carries `labkey_registered` (**only**) as
   `INTEGER NOT NULL DEFAULT 0`, and the other six production satisfaction columns are
   **absent** (⚑3 incremental; Unit 1 test green).
2. `sluice/gates/labkey.py` provides `make_experiment_client`/`ExperimentClient`/
   `LabKeyError`; construction is network- and secret-free; `find` resolves by
   `Description` trailing-token `endswith` (never by user), disambiguates substrings, and
   fails **closed** on ambiguity/error; `create` writes the trailing token then
   resolves-or-fails-loud (all Unit 2 tests green, no network, no `labkey` package needed
   to run the suite).
3. `sluice/gates/experiment.py` exports `EXPERIMENT` (order 0, `satisfied_column=
   "labkey_registered"`, `produces=("experiment_id",)`, `persists={"experiment_id":
   "experiment_id"}`, no-op invalidate) and `run_experiment_gate`, which: reuses a
   persisted id with **zero** LabKey contact; on first run does lookup-before-create; adopts
   an existing experiment on the crash window without a duplicate; quarantines every LabKey
   failure fail-closed (all Unit 3 tests green).
4. `validate_gates((EXPERIMENT,))` accepts the gate; B2 protection is live (non-empty
   `produces` arms `control.py:1044`); the gate is ready to be listed in `production-v3.1`.
5. `daemon.py`, `context.py`, `reconcile.py`, `events.py`, `control.py`, `mock_gates.py`,
   the `reference-v1` profiles are **unchanged**; the full existing suite (dependency-arrow,
   migration ladder, daemon persist choreography) stays green.

---

## 6. Carry-forwards (next increments)

- **⚑A `experiment_id`-aware `resolve_context.workdir` (⚑2 — deferred here).** Lands with
  the **download** increment (its first consumer): `workdir = {local_staging_dir}/{experiment_id}`
  when set, name-keyed fallback while NULL, plus the `/staging` symlink (`.absolute()`, gate
  map §5 ⚑A). `download` reads `experiment_id` off the snapshot this gate now persists.
- **Incremental production-column migrations (⚑3).** Each subsequent production gate ships
  its own satisfaction column in its own migration, with its gate: `download` → **008**
  `downloaded`; `checksum` → **009** `checksummed`; `samplesheet` → `samplesheet_created`;
  `registration` → `authority_registered`; `nvd` → `nvd_completed`; `globus_submit` →
  `globus_submitted`; `globus_verify` → `clumped_data_backed_up`. **This supersedes gate
  map §6's "one coherent migration 007"** — recorded here so a later reader is not confused.
- **Control belt-and-suspenders (Plan C)** — refuse `reset_gate`/`backtrack` that would
  clear `labkey_registered` (map §4 B2), on top of the structural protection already in
  place.
- **`production-v3.1` profile (last gate increment)** — lists `EXPERIMENT` at order 0.
- **`download` gate (next, increment 4)** — `needs=("experiment",)`; consumes the persisted
  `experiment_id` (via ⚑A workdir) for the raw-lanes staging tree.
