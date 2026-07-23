# Plan 04 — `context` subsystem (Plan A, subsystem 4 of 9)

**Module owned:** `sluice/core/context.py` (single file) — the frozen `RunContext`
record and the pure `resolve_context` function.
**Build order:** db → gate (+ mock gates) → events → **context** → profile →
reconcile → audit → control → daemon
**Tests:** consolidated in `tests/test_context.py` (one file per subsystem; db/gate/events precedent).

Ground truth: design of record **§6** (RunContext & resume model), §4 (how gates
receive context), §5/§7/§8 (what derivation may read); errata **A5, A7, D2**
(in scope), **B2** (context path-half), A4 (touched, not owned); the
**spec §6 revision — daemon-only notifier model — events plan ⚑5 APPROVED
2026-07-22**; ADR-0001 (docstring standard, binds every unit's code); ADR-0002
(gates never write run state — RunContext may *carry* a db path but reachable ≠
permitted); ADR-0003 (layering yes, stdlib-only no); plan 02 §3.4 + §7
(RunContext must satisfy the mocks' `workdir`-only contract) and plan 03 §7
(no notifier field); subsystem map (PP = python-poller).

---

## 1. Scope & non-goals

### Owned

| Thing | Public surface |
|---|---|
| The frozen context record | `RunContext(workdir, db_path, config)` — three fields, frozen, read-only config |
| The pure derivation | `resolve_context(run_row, config) -> RunContext` |

`context.py`'s final surface is `__all__ = ["RunContext", "resolve_context"]`,
grown as the names land (unit 1 declares `__all__ = ["RunContext"]`; unit 2
extends it with `"resolve_context"`); unit 3 asserts equality (db unit-7
precedent).

**Allowed imports (closed set): stdlib only.** `dataclasses`, `pathlib`,
`types`, `collections.abc` (typing). Context derives paths and holds config; it
never reaches the database, a notifier, or a gate module, so it imports none of
them. `sqlite3`, `sluice.core.db`, `slack_sdk`, and any `sluice.gates`/`api`/`cli`
are forbidden **by name** (unit 1). The structural consequence is fact 3's teeth:
a module that cannot import a SQL client cannot open a connection or query — so
"resolve_context does no DB work" is guaranteed by construction, not by promise.

### Explicitly NOT owned (and where it belongs)

- **The notifier.** RunContext carries **no notifier field** — the daemon-only
  caller model is the accepted revision of spec §6's letter (events plan ⚑5,
  APPROVED 2026-07-22). The daemon calls the `Notifier` Protocol via
  `safe_notify` at its own moments; gates return `GateResult`s and never notify
  (plan 03 §2 B8, §7). The fields-equality test (unit 1) makes re-adding a
  notifier a visible contract change.
- **Destination / remote / NVD path fields** (`raw_seqs_dir`, `merged_seqs_dir`,
  `nvd_work_dir`, `nvd_launch_dir`, `globus_source_path`, `globus_dest_path`, …)
  — **Plan B** (production gates + its `resolve_context` extension). These are
  the "destination/remote path fields" the user named as deferrals when deciding
  Plan A's LEAN context (2026-07-22). They are experiment_id-keyed and cannot
  land until B2 (§2) is resolved with a real experiment gate. Each such field
  edits the unit-1 fields-equality test consciously — a contract change, never a
  quiet field.
- **Config loading / schema validation.** Like the row, config arrives
  **already loaded** — resolve_context consumes a `Mapping`, it does not read a
  file. `resolve_context` reads the two keys it needs by subscript (fail-loud on
  absence, §3.3), but comprehensive required-key validation belongs to the
  config loader / daemon startup (Plan C / subsystem 9). Named carry-forward, §7.
- **Missing-row handling.** `resolve_context` assumes a valid, already-fetched
  row (spec §6's letter: it *receives* the row). The daemon fetches the row and
  branches on `get_run(...) is None` **before** calling resolve_context — that
  branch is the caller's, subsystem 9 (§7).
- **Creating directories / any filesystem mutation.** resolve_context **computes
  paths, never `mkdir`s** (fact 3). The daemon creates `ctx.workdir` before
  running a gate (§7). Unit 2 pins the no-mutation guarantee.
- **`gate.py`.** This plan does **not** edit the gate module. Plan 02 §1 offered
  an optional tightening of `run`'s annotation from `Callable[[Any], GateResult]`
  to `Callable[[RunContext], GateResult]`; this plan **declines** it — see §2
  "The declined annotation tightening."
- **Persisting the resume tokens** `globus_task_id` / `slack_thread_ts` — **Plan
  B**. They are non-recomputable persisted state (A5), read straight from the run
  row by Plan B's globus gate / notifier wiring; resolve_context neither carries
  nor recomputes them (§2 A5).

---

## 2. Errata & decided-facts resolutions — decisions taken, no TBDs

The four user-decided facts (2026-07-22) are **locked** and encoded below; none
is reopened.

### Fact 1 — RunContext carries NO notifier field. ENCODED.

Spec §6 lists "notifier" among what `resolve_context` derives; the events plan
⚑5 (APPROVED) supersedes that seam with the daemon-only-caller model. RunContext
has three fields — `workdir`, `db_path`, `config` — and none is a notifier. The
unit-1 fields-equality test pins the **closed** field set so any later addition
(a notifier, or a Plan B path field) is a mechanical, visible contract change.
Structural backstop: unit 1 forbids `slack_sdk` by name, so a notifier could not
even be constructed inside this module.

### Fact 2 — Plan A's RunContext is LEAN: workdir + DB path + config only. ENCODED.

The production destination/remote path fields are **named deferrals to Plan B**
(§1, §7), guarded by the same fields-equality test. Plan A computes exactly one
path — `workdir` — plus carries the config-sourced `db_path` and the config
itself. Everything a Plan B production gate needs to build an experiment-keyed
staging path is deferred to Plan B's resolve_context extension.

### Fact 3 — resolve_context receives the already-fetched row; it is a PURE derivation. ENCODED.

`resolve_context(run_row, config)` takes the row and the config — **no db path
parameter, no connection parameter** (unit 2's signature test). It performs no
DB queries, opens no connection, writes nothing, and mutates no filesystem
(computes paths, never `mkdir`s — unit 2). The db path is not passed in; it
**travels as a RunContext field sourced from `config["state_file"]`** (fact 3's
last clause). Purity is enforced three ways: the signature gives it no handle to
query with; import hygiene (unit 1) denies it `sqlite3`/`sluice.core.db`; and the
no-mutation + determinism tests (unit 2) pin the behavior.

### Fact 4 — the non-recomputable persisted set is {globus_task_id, slack_thread_ts}; resolve_context never fabricates or recomputes them. ENCODED (A5, A7, D2).

Spec §6 called `globus_task_id` "the only" non-recomputable value; errata **A5**
corrects it — `slack_thread_ts` is a second Slack-assigned handle that is equally
non-recomputable (PP `poller_notifications.py:110-111`) and equally persisted.
Resolution in context scope: **RunContext carries neither**, and resolve_context
reads neither from the row. They remain columns on `runs` (present since db
migration 001), read directly by Plan B where resume/monitoring needs them; a
context recomputation of either is definitionally impossible (they are externally
assigned). Unit 2's `test_resolve_context_ignores_resume_tokens` proves a row with
both set resolves identically to one with both NULL — they do not participate.

**A7 + D2 — the NVD launch-dir trap, encoded as a rule, deferred as a field.**
Errata A7: PP derives the NVD *launch* dir from `Path(__file__).resolve().parent`
(`nvd_helpers.py:186,199`), not from config — an install-location coupling that
D2 shows is dangerous (a launch dir silently relocated onto CephFS makes
`--resume` a no-op, forcing a multi-hour re-run). Only the NVD *work* dir is
config-derivable (`nvd_helpers.py:66-68`). Context-scope resolution:
1. **No RunContext path may ever be derived from `__file__`.** Unit 1's
   source-scan test asserts `__file__` does not appear in `context.py`, and
   unit 2 proves `workdir` is rooted under the config staging dir, not under the
   module's location. The trap cannot enter Plan A.
2. The NVD launch dir itself is a **Plan B** field, sourced from an **explicit
   config key**, never `__file__`; and config load must **validate at load** that
   the launch root is local POSIX and not under the staging mount (D2). Both are
   named carry-forwards (§7) — Plan A owns neither the field nor the validator,
   but it fixes the rule they must obey.

### B2 — the experiment_id chicken-and-egg (context's path half). RESOLVED for Plan A by keying `workdir` on `name` — ⚑1 APPROVED (2026-07-23, decided by the user); production experiment-keyed paths deferred.

Errata B2: spec §6 derives *all* paths from `experiment_id`, but a gate
*produces* `experiment_id` (PP `poller.py:1490`) and it names the whole staging
tree (`{staging}/{experiment_id}/...`, `poller.py:1502-1504`) — so a context
built before the experiment gate has run has no `experiment_id` to key on.
Plan A has **no experiment gate** (the mock gates need only a working
directory), and `experiment_id` is nullable in the schema. Resolution: **Plan
A's single path, `workdir`, is keyed on `run_row["name"]`, not `experiment_id`**
— `workdir = Path(config["local_staging_dir"]) / name`. This is deliberate and
sidesteps B2 entirely for Plan A:
- `name` is the `runs` **immutable primary key, always stamped by `insert_run`**
  and never changed ("stamp once, never change"), so `workdir` is always
  resolvable and stable across looks — a restarted daemon rebuilds the identical
  path from the row alone (spec §6's resume guarantee), and `reset_gate` (which
  can clear other columns) cannot move it (a `reset` never changes `name`).
- keying on `experiment_id` would require a silent NULL fallback — forbidden
  (guardrail §8, no silent fallbacks) — and would move `workdir` mid-run when the
  experiment gate later assigns `experiment_id`, orphaning any markers already
  written under the old path.

`workdir` is therefore a **generic per-run working directory**, not the
production experiment-staging tree. Plan B's production gates read their
experiment_id-keyed destination fields (deferred, §1); `workdir` stays name-keyed
permanently. **⚑1 APPROVED (2026-07-23, decided by the user)** — with the Plan B
note that `experiment_id` keys the production destination/pipeline-facing paths
and leads user-facing surfaces (Slack threads, CLI listings) once it exists.
Deliberately NOT copied: PP's `base_local_dir / str(experiment_id)`
(`poller.py:1502-1504`) — that is Plan B's experiment-keyed derivation, not
Plan A's `workdir`.

### A4 — touched, not owned.

`globus_task_id` must be persisted immediately after Globus submit, **before**
the wait, or a restart re-submits a duplicate multi-TB transfer. That is Plan B's
mechanic (the globus gate writing `metadata`, the daemon persisting it). Context
only guarantees it never recomputes the token (fact 4). Named carry-forward, §7.

### The declined annotation tightening. DECIDED (not a fork).

Plan 02 §1 offered subsystem 4 the option to retype `gate.run` from
`Callable[[Any], GateResult]` to `Callable[[RunContext], GateResult]`. This plan
**declines** it and does not touch `gate.py`. Reasons: (a) it is typing-only, zero
behavior; (b) `gate.py` uses no `from __future__ import annotations`, so the
annotation is evaluated at class-definition time — importing `RunContext` there
would create a real **`gate → context`** runtime import, coupling the
foundational contract module (built first) to a later module for no gain; (c) the
duck-typed contract is deliberately *looser* than RunContext — gates need only
`.workdir` (plan 02 §3.4), and mock-gate tests pass a `SimpleNamespace`; keeping
`Any` preserves that intentional looseness. RunContext simply *satisfies* the
existing `.workdir` contract (unit 3 proves it), which is all the coupling the
system needs.

---

## 3. The contract, authoritative

### 3.1 Surface

```python
# sluice/core/context.py — imports: stdlib ONLY

@dataclasses.dataclass(frozen=True)
class RunContext:
    """Everything a gate needs to do its work, derived fresh from state (§6)."""
    workdir: pathlib.Path          # per-run working directory: {staging}/{name}
    db_path: pathlib.Path          # the state DB path, sourced from config (fact 3)
    config: Mapping[str, object]   # the loaded config, held read-only (see __post_init__)
    # __post_init__: wrap `config` in types.MappingProxyType(dict(config)) so a
    # frozen record cannot leak a mutable, shared config to a gate (see unit 1).

def resolve_context(run_row, config: Mapping[str, object]) -> RunContext: ...
```

Functional shaping (project preference): a **frozen dataclass** plus a
**module-level function**, no methods, no builder class. `resolve_context` is a
pure `(row, config) -> RunContext` — the same functional shape as the control ops
and `reconcile.next_gate`.

**`run_row` duck type:** any object supporting `row["name"]` — both `dict` and
`sqlite3.Row` qualify (unit 3 exercises a real `sqlite3.Row` from `get_run`).
Plan A reads exactly one column, `name`; it never reads `experiment_id` (fact 2 /
B2). The daemon guarantees the row is non-`None` (§1, missing-row = caller's).

### 3.2 What resolve_context computes

| RunContext field | Derivation | Source |
|---|---|---|
| `workdir` | `pathlib.Path(config["local_staging_dir"]) / run_row["name"]` | config key + row name (B2: name-keyed, ⚑1 approved) |
| `db_path` | `pathlib.Path(config["state_file"])` | config key (fact 3 — DB path travels as a field) |
| `config`  | the passed mapping, snapshot-wrapped read-only | passed through |

Pure path arithmetic only: **no `.resolve()`** (it touches the filesystem to walk
symlinks) and **no `mkdir`** — resolve_context must not read or write the disk
(fact 3). Whether the staging dir or db path is absolute is a config concern
(deferred to the loader, §7); resolve_context joins strings into `Path`s and
stops.

### 3.3 Fail-loud and injection-safe reads

- **Required config keys by subscript, never `.get(..., default)`.** A missing
  `local_staging_dir` or `state_file` raises `KeyError` naming the key — a
  load-time error, not a silent production fallback (guardrail §8). Unit 2 pins it.
- **The workdir root comes EXCLUSIVELY from `config["local_staging_dir"]`** —
  never a default of `/scratch` or any assumed system scratch/temp location
  (user-added constraint, recorded with the ⚑1 stamp): the deployment cluster
  (CHTC) has **no** `/scratch` filesystem, so a pattern-matched scratch default
  would be not just a forbidden silent fallback but a broken path at runtime. A
  missing `local_staging_dir` fails loud at config load (loader, §7), with the
  subscript `KeyError` above as resolve_context's own backstop.
- **`run_row["name"]` is validated as a single safe path component before it
  becomes a path.** resolve_context is the boundary where a stored string identity
  turns into a filesystem path, so it is the right place to refuse a traversing
  name: a `name` containing `/` or `\`, or equal to `""`, `"."`, or `".."`,
  raises `ValueError` — otherwise `Path("/staging") / "../../etc"` escapes the
  staging root. Sequencing folder names are always single components in
  production, so a legitimate name never trips this. Unit 2 pins it (the
  injection-defense test).

### 3.4 The duck-typed context contract this satisfies (from plan 02 §3.4)

The mock gates (and the whole gate contract) require exactly one attribute:
`workdir: pathlib.Path`. RunContext provides it. Unit 3 proves a resolved context
drives `MOCK_ALPHA.run(ctx)` — the load-bearing cross-subsystem proof that
subsystem 4 delivered what subsystem 2 was owed (plan 02 §7). `db_path`'s Plan A
consumers are the **daemon's** direct progress writes (spec §2.6) and the
checkpoint control-polling of spec §7 decision A — control state is re-read at
gate checkpoints through `db.get_connection(ctx.db_path)`, so WAL + busy_timeout
apply. Carrying `db_path` does **not** license gate-side run-state writes —
ADR-0002 stands ("reachable ≠ permitted", plan 02 §7). Note the *structural*
guard (the `sqlite3`/`sluice.core.db` import ban, plan 02 unit 7) covers the
**mock gates only**: Plan B production gates may legitimately import what their
work needs, so for them ADR-0002 rests on review and the loop-persists
choreography (plan 02 §3.2), not on structure — carried forward by name in §7.

---

## 4. Units, in dependency order (3 units)

Every unit is one RED → GREEN → REFACTOR cycle starting from the named failing
test — **except unit 3**, which adds **no production code** and is
**green-on-arrival by design**: integration tests that pin the composed behavior
of units 1–2 (the plan-02 unit-9 carve-out convention). `__all__` grows as names
land — unit 1 declares `__all__ = ["RunContext"]`, unit 2 extends it with
`"resolve_context"` — so unit 3's export test arrives green and pins the closed
surface. Tests live in `tests/test_context.py`. A shared helper defined at the
top of the file in unit 2:
`make_config(**overrides) -> dict` (defaults: `local_staging_dir="/staging"`,
`state_file="data/sluice_state.db"`, plus any extra placeholder keys a test names)
— so each test names only the keys its behavior is about. Row doubles are plain
`dict`s except unit 3, which uses a real `sqlite3.Row`.

Every unit's code follows ADR-0001: Google-style docstrings, one why-paragraph
citing the §/errata, constraints not justifications.

---

### Unit 1 — `RunContext` record + module bootstrap + import/`__file__` hygiene

RED: `tests/test_context.py` fails at `import sluice.core.context` (module absent).

**Tests**
- `test_context_is_stdlib_only_and_db_free` — ast-parse `sluice/core/context.py`:
  every `Import`/`ImportFrom` root is in `sys.stdlib_module_names` (or is a
  relative import inside `sluice`, of which there are none); and — asserted **by
  name** so the failure message teaches — none of `sqlite3`, `sluice.core.db`,
  `slack_sdk`, `sluice.gates`, `sluice.api`, `sluice.cli` appears. (Technique of
  `test_db_imports_only_stdlib` / `test_mock_gates_import_only_gate_and_stdlib` /
  `test_events_is_core_clean_by_import`.) This one test carries three invariants:
  the `gates → core` arrow at file level, fact 3's structural "no DB work"
  guarantee (no SQL client to query with), and fact 1's structural "no notifier"
  (no `slack_sdk` to build one). `tests/test_dependency_arrows.py` auto-discovers
  the new module via `pkgutil.walk_packages` — no edit there.
- `test_context_derives_no_path_from_dunder_file` — the raw source of
  `context.py` does not contain the token `__file__` (substring check on the
  read text, message citing A7/D2). Encodes the highest-risk NVD trap as a
  standing structural guard: no install-location path coupling can ever enter the
  context module, so a future path field cannot be `__file__`-derived the way PP's
  launch dir was (`nvd_helpers.py:186,199`).
- `test_run_context_fields_are_exactly_workdir_db_path_config` —
  `[f.name for f in dataclasses.fields(RunContext)] == ["workdir", "db_path",
  "config"]`, all three required (no defaults). **This one test carries facts 1
  and 2:** no notifier field, no destination/remote path fields — a LEAN, closed
  set. Editing it is a conscious contract change (a Plan B path field, or a
  human-approved notifier reversal), requiring a plan revision or ADR.
- `test_run_context_is_frozen` — assigning to `RunContext.workdir` on an instance
  raises `dataclasses.FrozenInstanceError`. Pins the spec §6 frozen choice
  (someone could drop `frozen=True`); the DB is the single source of truth, so a
  context object must not become a second mutable authority mid-run.
- `test_run_context_config_is_read_only` — construct a `RunContext`; mutating
  `ctx.config["k"] = "v"` (and `del ctx.config["k"]`) raises `TypeError`; and a
  later mutation of the *original* dict passed in does not change `ctx.config`
  (the `dict(config)` snapshot). A frozen record whose config were a live shared
  dict would let a gate rewrite config for every other gate — a mutable in-memory
  authority the spine forbids.

---

### Unit 2 — `resolve_context` (the pure derivation)

RED: the first behavioral test fails at `AttributeError`/`ImportError` on
`resolve_context`. Behavior per §3.2–3.3. Extends `__all__` with
`"resolve_context"` (§4 intro).

**Tests**
- `test_resolve_context_derives_workdir_db_path_and_config` — with
  `config = make_config(local_staging_dir="/staging",
  state_file="data/x.db")` and `row = {"name": "run_A"}`:
  `ctx.workdir == pathlib.Path("/staging/run_A")`,
  `ctx.db_path == pathlib.Path("data/x.db")`, and `ctx.config["local_staging_dir"]
  == "/staging"` (the config is carried, read-only). The happy path pins all
  three §3.2 derivations at once, including the B2 name-keyed workdir (⚑1,
  approved) — and that the workdir root is the config value, no other root
  (§3.3: never `/scratch`/system-temp).
- `test_resolve_context_creates_no_filesystem_state` — call resolve_context with
  `local_staging_dir = str(tmp_path / "staging")` (which does **not** exist) and a
  `state_file` under a non-existent dir: returns normally; afterward
  `not ctx.workdir.exists()`, the staging parent was not created, and no db file
  was created. Fact 3's "computes paths, never `mkdir`s" — the daemon, not
  resolve_context, will create `ctx.workdir` (§7).
- `test_resolve_context_signature_is_row_and_config_only` —
  `list(inspect.signature(resolve_context).parameters) == ["run_row", "config"]`.
  Fact 3 as a signature fact: resolve_context is handed **no** connection and
  **no** db path, so it structurally cannot query the database — the db path is an
  output field, never an input. Editing this signature is a conscious purity
  change.
- `test_resolve_context_is_deterministic` — two calls with the same `row` and
  `config` return **equal** `RunContext`s (`==`, via the frozen dataclass's
  generated `__eq__`; `MappingProxyType` compares by underlying mapping). A pure
  function of state has no hidden input; a regression that read the clock, the
  filesystem, or a module global would diverge here.
- `test_resolve_context_fails_loud_on_a_missing_config_key` — parametrized:
  `make_config()` minus `local_staging_dir` → `KeyError` whose message names
  `"local_staging_dir"`; minus `state_file` → `KeyError` naming `"state_file"`.
  Required keys are read by subscript, never `.get` with a default — no silent
  production fallback (guardrail §8), and in particular no `/scratch`/system-temp
  workdir default (§3.3: CHTC has no `/scratch`).
- `test_resolve_context_rejects_a_traversing_run_name` — parametrized over
  names `"../evil"`, `"a/b"`, `"..\\evil"`, `".."`, `"."`, `""` → each raises
  `ValueError` naming the offending value; a plain name `"run_A"` resolves
  normally. The injection defense at the string-identity→path boundary (§3.3):
  without it, `Path(staging) / "../../etc"` escapes the staging root.
- `test_resolve_context_ignores_resume_tokens` — a row with
  `globus_task_id="task-xyz"` and `slack_thread_ts="1721.001"` set resolves to a
  `RunContext` **equal** to the one from the same row with both keys absent/NULL:
  neither token appears anywhere in the context, and the derivation reads neither.
  Fact 4 / A5 — resolve_context never recomputes or carries the non-recomputable
  persisted values; they stay in the row for Plan B.

---

### Unit 3 — surface + real-`sqlite3.Row` integration + the mock-gate `workdir` contract

The only unit that touches the real db + gate substrate. Adds no production
code; **green-on-arrival by design** (§4 intro — the plan-02 unit-9 carve-out
convention), no RED is claimed: these are integration tests pinning the composed
behavior of units 1–2.

**Tests**
- `test_context_exports_exactly_the_documented_surface` —
  `set(sluice.core.context.__all__) == {"RunContext", "resolve_context"}`
  (scope guard, db unit-7 precedent).
- `test_resolve_context_reads_a_real_sqlite_row` — `initialize_database(tmp)`;
  `insert_run(conn, "run_A", pipeline_version="v1")`; `row = get_run(conn,
  "run_A")` (a real `sqlite3.Row`); with a config whose `local_staging_dir` is
  `str(tmp_path / "staging")`: `ctx = resolve_context(row, config)` yields
  `ctx.workdir == tmp_path / "staging" / "run_A"` and `ctx.db_path ==
  pathlib.Path(config["state_file"])`. **The load-bearing part:** the whole
  derivation runs off `row["name"]` on a real `sqlite3.Row`, against the real
  schema's column name — a drift in either surfaces here, not in the daemon. It
  also proves `resolve_context` never needed `experiment_id` (the row's is NULL
  and untouched — B2, fact 2).
- `test_resolved_context_satisfies_the_mock_gate_workdir_contract` — from the
  resolved `ctx` above: `ctx.workdir.mkdir(parents=True)` (the caller/daemon's
  job — resolve_context created nothing), then `result = MOCK_ALPHA.run(ctx)`
  returns `GateOutcome.SUCCESS` and `(ctx.workdir / "mock_alpha.marker").exists()`.
  Proves RunContext delivers exactly the `workdir` attribute the gate contract
  requires (plan 02 §3.4/§7) and demonstrates the compute-paths-vs-mkdir split:
  resolve_context computed the path, the caller created it, the gate wrote under
  it — the daemon's choreography in miniature.

---

## 5. Invariant checks

| Invariant | How this subsystem could violate it | Guarding test |
|---|---|---|
| **DB is the single source of truth; resolve_context never queries or writes** (state-only spine; fact 3) | a builder taking a connection/db_path param; importing `sqlite3`/`db` | `test_context_is_stdlib_only_and_db_free` (unit 1, structural) + `test_resolve_context_signature_is_row_and_config_only` (unit 2) |
| **resolve_context is a PURE function of state** (spec §6; parallels reconcile purity) | reading the clock/filesystem/module global; mkdir-ing | `test_resolve_context_is_deterministic` + `test_resolve_context_creates_no_filesystem_state` (unit 2) |
| **RunContext carries no notifier** (spec §6 revision / events ⚑5, fact 1) | a notifier field; importing `slack_sdk` | `test_run_context_fields_are_exactly_workdir_db_path_config` + the `slack_sdk`-by-name half of `test_context_is_stdlib_only_and_db_free` (unit 1) |
| **LEAN field set — no early destination/remote path fields** (fact 2) | a Plan B path field leaking into Plan A | `test_run_context_fields_are_exactly_workdir_db_path_config` (unit 1) |
| **No path derived from `__file__`** (A7/D2 trap) | copying PP's launch-dir install-location coupling | `test_context_derives_no_path_from_dunder_file` (unit 1) + `workdir` under config staging in `test_resolve_context_derives_workdir_db_path_and_config` (unit 2) |
| **Workdir root exclusively from `config["local_staging_dir"]` — no `/scratch`/system-temp default; CHTC has no `/scratch`** (⚑1 stamp note, §3.3) | a hardcoded scratch/temp fallback when the key is missing, or as the path root | `test_resolve_context_fails_loud_on_a_missing_config_key` + the workdir-root assertion of `test_resolve_context_derives_workdir_db_path_and_config` (unit 2) |
| **`gates → core`; core imports with gates absent** | importing a gates/api/cli module | `test_context_is_stdlib_only_and_db_free` (unit 1); `tests/test_dependency_arrows.py` auto-covers the new module |
| **No silent config fallback** (guardrail §8) | `.get(key, default)` instead of `config[key]` | `test_resolve_context_fails_loud_on_a_missing_config_key` (unit 2) |
| **Injection defense at the identity→path boundary** | joining a raw run name into a path | `test_resolve_context_rejects_a_traversing_run_name` (unit 2) |
| **No mutable in-memory authority; frozen context** (spine) | `frozen=False`; a live shared config dict | `test_run_context_is_frozen`, `test_run_context_config_is_read_only` (unit 1) |
| **Resume tokens never recomputed/fabricated** (A5, fact 4) | reading/deriving `globus_task_id`/`slack_thread_ts` | `test_resolve_context_ignores_resume_tokens` (unit 2) |
| **RunContext satisfies the mocks' `workdir`-only contract** (plan 02 §3.4/§7) | RunContext missing/renaming `workdir` | `test_resolved_context_satisfies_the_mock_gate_workdir_contract` (unit 3) |
| **Gates reachable ≠ permitted (db_path carried, writes still forbidden)** (ADR-0002) | — (context only carries the path) | inherited: plan 02 unit 7/9 (mock gates cannot import `sqlite3`/`db`); mock-only — production gates covered by review + choreography, §3.4/§7 |

---

## 6. Done criteria

1. `pixi run test` fully green: every pre-existing test (dependency arrows now
   covering `sluice.core.context`) plus `tests/test_context.py`.
2. `sluice/core/context.py` imports only stdlib; `sqlite3`, `sluice.core.db`,
   `slack_sdk`, and any `sluice.gates`/`api`/`cli` appear nowhere in it, and the
   token `__file__` appears nowhere in it.
3. `dataclasses.fields(RunContext)` is exactly `["workdir", "db_path", "config"]`
   — no notifier, no destination/remote path fields; the record is frozen and its
   `config` is read-only.
4. `resolve_context(run_row, config)` takes exactly `(run_row, config)`, derives
   `workdir = {config[local_staging_dir]}/{row[name]}` (no other root — never a
   `/scratch`/system-temp default, §3.3) and `db_path = {config[state_file]}`,
   carries config read-only, creates nothing on disk, is deterministic, fails
   loud on a missing required key, refuses a traversing name, and ignores
   `globus_task_id`/`slack_thread_ts`.
5. A resolved context built from a real `sqlite3.Row` drives `MOCK_ALPHA.run(ctx)`
   after the caller mkdirs `ctx.workdir` — RunContext satisfies the gate
   contract's `workdir` requirement end-to-end.
6. Downstream can start: subsystem 5 (`profile`) needs nothing from context;
   subsystem 6 (`reconcile`) is pure over state and independent of context;
   subsystem 9 (`daemon`) gets `resolve_context` to call after fetching a row and
   before running a gate, plus the mkdir-the-workdir obligation; Plan B gets the
   resolve_context extension points and the A4/A5/A7/D2/B2 carry-forwards below.

---

## 7. Carry-forwards (named, so no plan rediscovers them)

- **Subsystem 9 (daemon):** the caller owns missing-row handling — fetch the row,
  branch on `get_run(...) is None` **before** `resolve_context`, and that branch
  is **fail-loud**: a fetched-`None` row is a state anomaly to surface (log and
  handle per its policy), never a silent skip. `resolve_context` itself can raise
  **inside the loop**: `ValueError` on a traversing name (§3.3) and `KeyError` on
  a missing required config key (unreachable once the loader validates the full
  required surface at startup — the intended design, next bullet — but the daemon
  must still not die: treat an escape as a quarantine-grade failure for that run,
  never a loop crash). Then `ctx = resolve_context(row, config)`,
  **`ctx.workdir.mkdir(parents=True, exist_ok=True)`** (resolve_context creates
  nothing), then `gate.run(ctx)`. `invalidate` and `terminate` receive **the same
  context, resolved fresh by their caller** — the daemon on `run_from_fresh`/hold,
  and `reset_gate`'s choreography routed through subsystem 8 (plan 02 §2) — never
  a second context type. The daemon's own progress writes go direct to
  `ctx.db_path` (spec §2.6); ADR-0002 keeps gate-side run-state writes out —
  context carrying `db_path` does not change that.
- **Config loader (Plan C / subsystem 9):** comprehensive required-key validation
  at load — including `local_staging_dir`, whose absence must fail there, never
  fall back to `/scratch` or any system temp (§3.3: CHTC has no `/scratch`).
  resolve_context fails loud per-key as a backstop (§3.3), but the loader
  should validate the whole required surface once at startup — and, per **D2**,
  validate that the NVD launch-dir config key resolves to a **local POSIX** path
  **not under the staging mount**. A missing required key is a load-time error,
  never a silent fallback (guardrail §8).
- **Plan B (production gates + resolve_context extension):**
  - Add the experiment_id-keyed **destination/remote path fields**
    (`raw_seqs_dir`, `merged_seqs_dir`, `nvd_work_dir`, `nvd_launch_dir`,
    `globus_source_path`, `globus_dest_path`) — the fields deferred by fact 2.
    Each new field **edits the unit-1 fields-equality test consciously** (a
    contract change). resolve_context reads `run_row["experiment_id"]` to build
    them, and must **resolve B2** there: either the experiment gate produces
    `experiment_id` and resolve_context returns a *partial* context whose path
    fields are unavailable until it is satisfied, or `experiment_id` is stamped at
    detection (making LabKey part of detection, not a gate). Per the ⚑1 stamp:
    `experiment_id` keys the production destination/pipeline-facing paths and
    leads user-facing surfaces (Slack threads, CLI listings) once it exists;
    Plan A's name-keyed `workdir` (⚑1, approved) is unaffected — it stays
    generic and name-keyed.
  - **ADR-0002's structural guarantee does not extend to production gates.** The
    `sqlite3`/`sluice.core.db` import ban covers the mock gates only (plan 02
    unit 7); Plan B gates may legitimately import what their work needs, so
    "gates never write run state" is enforced for them by review (a named
    reviewer dimension) and the loop-persists choreography (plan 02 §3.2), not
    by import bans.
  - **NVD work dir** is config-derivable: `f"{nvd_results_base}/{experiment_id}/
    nvd-work"` (PP `nvd_helpers.py:66-68`, confirmed by A7). **NVD launch dir**
    comes from an **explicit config key, never `__file__`** (A7), validated local
    POSIX + not-under-staging at load (D2) — the highest-risk single item in the
    NVD port.
  - **Resume tokens:** `globus_task_id` and `slack_thread_ts` stay in the run row
    and are read directly (never on RunContext, never recomputed — A5, fact 4).
    Persist `globus_task_id` **immediately after Globus submit, before the wait**
    (A4), or a restart re-submits a duplicate multi-TB transfer.
- **`gate.py`:** left unchanged by this plan (the declined annotation tightening,
  §2). If Plan B ever wants `run`'s parameter retyped to `RunContext`, do it
  under `from __future__ import annotations` in `gate.py` so the tightening stays
  typing-only and creates no runtime `gate → context` import.

---

## ⚑ Human sign-off — ⚑1 APPROVED (2026-07-23, decided by the user)

1. **§2 B2 — `workdir` keyed on `run_row["name"]`, not `experiment_id` —
   APPROVED (2026-07-23, decided by the user).** Plan A's single computed path
   is `Path(config["local_staging_dir"]) / name`, making `workdir` a generic,
   always-resolvable, stable-across-looks per-run working directory and
   sidestepping the experiment_id chicken-and-egg (B2) entirely for Plan A.
   Rationale: `name` is the immutable primary key, always stamped by
   `insert_run` and never changed; keying on `experiment_id` (nullable in
   Plan A) would require a forbidden silent NULL fallback and would move
   `workdir` mid-run when the experiment gate later assigns `experiment_id`.
   The precedent stands: `workdir` stays name-keyed permanently, while — per
   the user's Plan B note recorded with the stamp — **`experiment_id` keys
   Plan B's production destination/pipeline-facing paths and leads user-facing
   surfaces (Slack threads, CLI listings) once it exists**. User-added
   constraint encoded alongside the approval: the deployment cluster (CHTC)
   has **no** `/scratch` filesystem — the workdir root comes exclusively from
   `config["local_staging_dir"]`, never a `/scratch`/system-temp default, and
   a missing key fails loud at config load (§3.3, §5, §7).
