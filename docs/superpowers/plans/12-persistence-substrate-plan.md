# Plan 12 — ADR-0006 persistence substrate (Plan B increment 2)

**Type:** IMPLEMENTATION plan (test-first). LIGHTWEIGHT MODE: one agent builds in a
single pass; one review follows. **No design decisions live here** — ADR-0006's six
decisions ARE the spec; this plan only translates them into units.

**Ground truth (in order):** `docs/adr/0006-gate-output-persistence.md` (law);
`docs/superpowers/plans/10-plan-b-gate-map.md` §3/§6/§8 (⚑B, migration 007, build
sequence); the as-built core (`sluice/core/gate.py`, `context.py`, `daemon.py`,
`db.py`); ADR-0002 (daemon is the sole writer), the dependency-arrow rule, no-default
config discipline.

**Position in the sequence (plan 10 §8):** this is step 3 — it lands **before** the
`experiment` gate (step 4). Gate 0 is the first user of `persists` (writes
`experiment_id`); ⚑A's `experiment_id`-aware `resolve_context` reads the run-row
snapshot this increment adds. The substrate must be provable **now**, without the real
experiment gate — see Unit 3's test-local gate fixtures.

---

## 1. Scope & non-goals

**In scope (this increment owns):**
- `sluice/core/gate.py` — the eleventh `Gate` field `persists: Mapping[str, str]`
  (default empty, sealed), and the ten→eleven field-pin update in
  `tests/test_gate.py:150-188` citing ADR-0006.
- `sluice/core/context.py` — `RunContext` gains a sealed read-only run-row snapshot
  (`run_row`); `resolve_context` captures it (ADR-0006 d.3).
- `sluice/core/daemon.py` — `process_run`'s SUCCESS branch (`daemon.py:481-486`)
  extends to write declared `persists` outputs **and** the satisfaction column in ONE
  `update_run` inside ONE `write_txn`; the missing-declared-key quarantine path; the
  fail-loud-on-bad-column path (free via `update_run`'s whitelist — pinned, not built).

**NOT in scope (deferred, stated so the engineer does not drift):**
- **⚑A's `experiment_id`-aware workdir** — `resolve_context.workdir` stays
  name-keyed here (`context.py:80-83` unchanged). The workdir derivation change lands
  with the **experiment gate** increment (plan 10 §8 step 4), which reads
  `experiment_id` **from** the snapshot this increment adds. This increment adds the
  snapshot only.
- **Migration 007 / any new column.** The substrate needs **zero** new columns:
  `experiment_id` (INTEGER) and `globus_task_id` (TEXT) exist since migration 001
  (`db.py:148,163`). Migration 007 (incl. `globus_submitted`) belongs with the gates
  that need new columns (plan 10 §6) — NOT here. Explicitly not owned.
- **The `globus_submit`/`globus_verify` split** (ADR-0006 d.4) — a later gate
  increment (plan 10 §8 step 7). This increment supplies the mechanism it will use.
- **The parked `gate_outputs` table** (d.6) — not built. Do not create it.
- **`events.py` and `reconcile.py`** — **unchanged** (see §4). No heartbeats (D5, a
  later increment).
- The shipped `mock_gates.py` and the `reference-v1` profiles — **unchanged** (Plan A
  frozen; see Unit 3 for why the substrate is proven with test-local gates instead).

---

## 2. Errata / decisions in scope — all already made in ADR-0006

Every item below is DECIDED by the ADR; recorded here for the engineer, not re-opened.

- **d.1 — eleventh `Gate` field.** `persists: Mapping[str, str]` (GateResult.metadata
  key → `runs` column), default empty. The `tests/test_gate.py` ten-field pin becomes
  eleven, citing ADR-0006 (same governance as ADR-0002/0004/0005). Gates still only
  RETURN; the daemon stays the sole writer (ADR-0002 intact). → Unit 1.
- **d.2 — write choreography.** On SUCCESS the daemon writes declared outputs + the
  satisfaction column in ONE `update_run` in ONE `write_txn`. A declared key missing
  from `result.metadata` on SUCCESS **quarantines** (fail loud). A `persists` column
  name absent from the `runs` whitelist **fails loud via `update_run`** (already free).
  → Unit 3.
- **d.3 — read path.** `RunContext` carries a **sealed read-only snapshot of the run
  row**, so a resuming gate reads its own prior tokens without touching the DB. → Unit 2.
- **d.4 — globus split.** LATER increment (the gate). Migration 007 carries
  `globus_submitted`. **Not here.** Named as carry-forward (§6).
- **d.5 — paths never persisted.** Enforced by convention: no unit persists a path;
  the test fixtures persist identity tokens only (`experiment_id`). Interlocks with ⚑A
  (workdir derived, not stored).
- **d.6 — `gate_outputs` parked.** Not built.

**No ⚑ flags.** ADR-0006 leaves no user decision open for this increment. Every
ADR-silent *mechanic* (snapshot shape, fields-dict construction, where the missing-key
check sits, how to prove it without the real gate) is resolved in §3 as a
translation, not a decision. If the reviewer finds a genuine fork, flag it — none is
expected.

---

## 3. Unit list (dependency order)

### Unit 1 — `Gate.persists` field + field-pin update (`gate.py`)

**Change.** Add an eleventh field, appended last (a defaulted field must follow the
non-defaulted `produces`/`run`/`invalidate`; appending is least disruptive):

```
persists: Mapping[str, str] = types.MappingProxyType({})  # metadata key -> runs column
```

Add `import types` and `Mapping` (from `collections.abc`) — both stdlib, so the
`gates → core` arrow holds. In `__post_init__`, seal it exactly as `RunContext` seals
`config` (the established precedent):

```
object.__setattr__(self, "persists", types.MappingProxyType(dict(self.persists)))
```

This detaches from the author's mapping (a shared dict could later mutate a gate's
persist behavior) and makes item writes raise. The default-empty case survives the
`dict({})` copy as an empty proxy. Update the field docstring/comment to name the
eleventh field and cite ADR-0006 (the "exactly these ten fields" comment becomes
eleven).

**Failing tests (edit `tests/test_gate.py`):**
- `test_gate_contract_fields_are_exactly_the_spec_eleven` (rename/extend the existing
  `..._spec_ten` at line 150). Assert the field-name list ends with `"persists"`:
  `[... "terminate", "control_point", "persists"]`. Update the docstring to cite
  **ADR-0006 d.1** as the authorizing change (mirroring its existing ADR-0002/0004/0005
  citations). Assert `fields_by_name["persists"].default` is an empty mapping
  (`== {}` / `len(...) == 0`).
- `test_persists_is_sealed_read_only`: construct a `Gate(..., persists={"k": "col"})`
  from a plain dict; assert `gate.persists == {"k": "col"}` **and** that
  `gate.persists["k"] = "x"` raises `TypeError` (the MappingProxyType seal), **and**
  that mutating the *source* dict after construction does not change `gate.persists`
  (detachment). This is the one behavioral test — it proves the sluice-specific
  sealing choice; it does NOT re-prove that a mapping stores values.

*(No new test for "default is empty when omitted" beyond the pin assertion — a
defaulted-field re-prove of the language.)*

---

### Unit 2 — `RunContext` sealed run-row snapshot (`context.py`)

**Change.** Add a fourth `RunContext` field after `config`, **with a sealed empty
default**:

```
run_row: Mapping[str, object] = types.MappingProxyType({})
```

**The default is load-bearing, not cosmetic (review finding 1, HIGH).** Two existing
tests construct `RunContext` **directly**, by keyword with no `run_row`:
`tests/test_context.py:214` (`test_run_context_is_frozen`) and `:241`
(`test_run_context_config_is_read_only`) both call
`RunContext(workdir=, db_path=, config=)`. A no-default `run_row` makes both raise
`TypeError` — a red suite that violates this plan's own "existing suite stays green"
criterion. The fix is the **optional-with-empty-default** field, NOT editing those two
tests: both sites then stay green **untouched**, and the production read path
(`resolve_context`) fills `run_row` for the gate-0/experiment path anyway. Keeping the
field optional is also the cleaner contract.

Seal it in `__post_init__` alongside `config`, same precedent:

```
object.__setattr__(self, "run_row", types.MappingProxyType(dict(self.run_row)))
```

**The seal must be `sqlite3.Row`-safe (review finding 2, MEDIUM).** `dict(<sqlite3.Row>)`
RAISES — a `Row` iterates values, not items (the exact bug this plan flags for
`resolve_context`). That is fine because the seal only ever receives a **plain dict**:
- **direct construction** passes a plain dict (or omits it → the empty-proxy default),
  so `dict(self.run_row)` copies an already-dict — safe;
- the **production path** never hands a raw `Row` to the constructor —
  `resolve_context` pre-converts with a keyed comprehension (below) and passes the
  resulting plain dict.

So `__post_init__` never calls `dict()` on a raw `Row`. **The seal test must construct
`RunContext(run_row=<plain dict>)`, NEVER `RunContext(run_row=<real sqlite3.Row>)`** —
any real-`Row` case flows through `resolve_context`.

In `resolve_context`, build a plain-dict snapshot from the `sqlite3.Row` **at resolve
time** and pass it in (key by name explicitly — `dict(row)` is wrong for a `Row`):

```
run_snapshot = {key: run_row[key] for key in run_row.keys()}
...
return RunContext(workdir=..., db_path=..., config=config, run_row=run_snapshot)
```

`workdir` derivation is **unchanged** (name-keyed; ⚑A is the next increment). Update
the `RunContext` docstring: the snapshot is the ADR-0006 d.3 gate read path — a
resuming gate reads its prior tokens (experiment id, globus task id) from
`context.run_row` without a DB touch; `db_path` still does not license a gate write
(ADR-0002).

**Failing tests (`tests/test_context.py`) (review finding 3, LOW — each supplies the
required inputs).** `resolve_context` reads `config["local_staging_dir"]` and
`config["state_file"]` by subscript (no default); a direct `RunContext` needs
`workdir`/`db_path`/`config`. So each test provides a **minimal config dict**
(`{"local_staging_dir": "/staging", "state_file": "data/sluice_state.db"}`) and the
appropriate source row:
- `test_resolve_context_captures_run_row_snapshot`: call
  `resolve_context(run_row, config)` with a **real `sqlite3.Row`** carrying `name` and
  `experiment_id=4242` and the minimal config; assert
  `context.run_row["experiment_id"] == 4242` and `context.run_row["name"] == <name>`.
  This exercises the keyed-comprehension `Row` conversion end-to-end (the only place a
  real `Row` is converted) and proves the sluice-specific choice: the token is readable
  off the context.
- `test_run_row_snapshot_is_sealed_and_detached`: construct `RunContext` **directly
  with a plain dict** `run_row` (never a raw `Row`) plus the minimal `workdir`/
  `db_path`/`config`; assert `context.run_row["experiment_id"] = 9` raises `TypeError`,
  and that mutating the source dict after construction does not change
  `context.run_row` (the same read-only-authority guard the config seal carries — the
  snapshot must not become a second mutable in-memory authority).

*(No test that `dict(sqlite3.Row)` behaves one way or another — a library re-prove. The
behavioral risks are the `Row`→snapshot conversion, tested once via `resolve_context`,
and seal + detachment, tested once on a plain dict.)*

---

### Unit 3 — daemon persist choreography (`daemon.py`, `process_run` SUCCESS branch)

**Change.** Replace the SUCCESS branch at `daemon.py:481-486`. Two guards precede the
write; the happy path is one `update_run` in one `write_txn`.

```
if outcome is GateOutcome.SUCCESS:
    metadata = result.metadata or {}
    missing = [mkey for mkey in gate.persists if mkey not in metadata]
    if missing:
        # d.2 fail-loud: a gate that returned SUCCESS but omitted a declared
        # output cannot have its checkmark set — output and checkmark move
        # together or not at all. Quarantine this ONE run (data anomaly).
        reason = (
            f"gate {gate.id!r} returned SUCCESS but omitted declared persists "
            f"key(s) {sorted(missing)} — the output and its satisfaction column "
            f"must be written together (ADR-0006 d.2)"
        )
        failure = GateResult(GateOutcome.FAILURE, reason=reason)
        _stamp_quarantine(db_path, run_name, reason)
        emit_gate_event(run_name, gate, failure, duration_ms=duration_ms)
        committed_row = _get_run_row(db_path, run_name)
        emit_run_event(committed_row, extra={"failed.gate": gate.id})
        safe_notify(notifier, "finish_run_thread", run_name, "quarantined", reason)
        return "quarantined"

    fields = {gate.satisfied_column: 1}
    for metadata_key, column in gate.persists.items():
        fields[column] = metadata[metadata_key]
    with write_txn(db_path) as connection:
        update_run(connection, run_name, fields)   # bad column -> UnknownColumnError, fatal
    emit_gate_event(run_name, gate, result, duration_ms=duration_ms)
    safe_notify(notifier, "post_milestone", run_name, f"{gate.name} completed")
    continue
```

**Design translation notes (no re-decisions):**
- The missing-key path mirrors the existing SKIPPED-in-contract quarantine tail
  (`daemon.py:499-512`): daemon-authored reason, stamp, gate event, run event with
  `failed.gate`, notify. A **synthetic `GateResult(FAILURE, reason)`** feeds the gate
  event so `gate.finished` reads honestly as a failure (not a misleading success).
- **Missing = key absent** (`mkey not in metadata`), not value-is-None. ADR d.2 guards
  missing KEYS; a present key with a `None` value is the gate author's choice and
  persists as `NULL`.
- **Bad column is FATAL, not a quarantine.** `update_run`'s whitelist raises
  `UnknownColumnError` (a `KeyError` subclass); the existing catches in `process_run`
  are call-site-scoped around `load_profile`/`resolve_context` only, so it propagates
  out — the honest surface for a gate-author/deployment bug (same class as reconcile's
  schema-drift `KeyError`, `process_run` docstring). The `write_txn` rolls back on the
  raise, so neither the output nor the checkmark is written. This is free behavior —
  **pinned by a test, not built.**
- **Atomicity** both ways: on the happy path output+checkmark commit together; on the
  missing-key refusal neither is written (the checkmark never lands without its
  output); on the bad-column raise the txn rolls back.
- Gate authors must not map a `persists` value onto `satisfied_column` itself (it would
  clobber the `1`). Out of ADR scope; noted, no unit — no shipped gate does this.

**Proving it WITHOUT the real experiment gate (decision, stated):** the shipped
`mock_gates.py` and `reference-v1` profiles stay **untouched** (Plan A frozen, plan 10
§1). A mock persisting to a *new* column would need a migration this increment does not
own; a mock persisting into `experiment_id` would pollute reference runs with fake
identity the future experiment gate's idempotent-reuse read would then see. So the
substrate is proven with **test-local `Gate` fixtures** in `tests/test_daemon.py` that
declare `persists` mapping onto the **already-existing** whitelisted columns
`experiment_id`/`globus_task_id` (real columns since migration 001 — no migration, no
reference-profile change). Wire each fixture into a one-gate test profile/registry and
drive it through `process_run`, following the existing `test_daemon.py` custom-gate
pattern.

**Failing tests (`tests/test_daemon.py`):**
- `test_success_persists_declared_output_atomically_with_satisfaction`: a test-local
  gate with `persists={"exp": "experiment_id"}` whose `run` returns
  `GateResult(SUCCESS, metadata={"exp": 4242})`. After `process_run`, the committed row
  has `experiment_id == 4242` **and** the gate's `satisfied_column == 1` (both written,
  one txn). Token is `"completed"` or drives on per the fixture.
- `test_success_missing_declared_persist_key_quarantines`: same gate, `run` returns
  `GateResult(SUCCESS, metadata={})` (or `metadata=None`). `process_run` returns
  `"quarantined"`; the row has `errored_at` set and `error_reason` naming the missing
  key `"exp"`; **`experiment_id` stays NULL and `satisfied_column` stays 0** (the
  refusal is atomic — no partial write); `run.finished` carries `failed.gate` = the
  gate id.
- `test_success_persist_to_unknown_column_fails_loud`: a gate with
  `persists={"x": "not_a_runs_column"}` returning `SUCCESS, metadata={"x": 1}`. Assert
  `process_run` raises `UnknownColumnError` (propagates, fatal), and the row shows
  neither the satisfaction column set nor a partial write (txn rolled back).
- `test_gate_without_persists_writes_only_satisfaction`: a default-empty-`persists`
  gate returning `SUCCESS`. Assert only `satisfied_column == 1` changed and no token
  column moved — the regression guard that the untouched reference path
  (`mock_gates`/`reference-v1`) behaves exactly as before.

*(Four tests: happy-path atomicity, missing-key quarantine + atomic refusal, injection
fail-loud, and the no-persists regression. These are the real branches/invariants of
the choreography — no filler.)*

---

## 4. Invariant checks

| Invariant | Risk in this subsystem | Guard (test) |
|---|---|---|
| DB is the single source of truth; no authoritative in-memory state | The run-row snapshot could become a second mutable authority | Unit 2 `test_run_row_snapshot_is_sealed_and_detached` (sealed + detached); Unit 1 same for `persists` |
| Daemon is the only actor / gates only RETURN (ADR-0002) | `persists` might tempt gate-side writes | Gate contract unchanged: `run` still returns metadata; the **daemon** writes via `update_run`. Unit 3 tests drive all writes through the daemon; existing `test_daemon.py` no-`invalidate`/no-delete ast pins still hold |
| `gates → core` one-way (stdlib-only core surfaces) | New imports in `gate.py`/`context.py` | Both add only stdlib (`types`, `collections.abc.Mapping`). `tests/test_dependency_arrows.py`, `test_gate.py`, and the `test_context.py` ast guard stay green |
| Existing suite stays green | The new `RunContext.run_row` field could break the two direct-construction tests at `test_context.py:214,241` | The **empty-proxy default** (Unit 2) keeps both sites green **untouched** — this is why the field is optional, not required |
| Short transactions | Persist write could widen the txn | The `write_txn` still opens strictly **after** `gate.run` returns; the fields dict and both guards are built outside it; emits/notify stay post-commit. No new gate call inside a txn |
| Reconcile is a pure function of state | A new `Gate` field could perturb scheduling | `reconcile` reads only `needs`/`order`/`satisfied_column`; `persists` is inert. **No `reconcile.py` change.** Existing reconcile tests (which build gates without `persists` → default empty) stay green — the standing guard |
| No secrets / no baked defaults | — | `persists` default empty is a contract default, not a config fallback; no config key added |

**`events.py`:** no change. The daemon reuses `emit_gate_event`/`emit_run_event`; the
missing-key path feeds a synthetic `FAILURE` result and the existing `failed.gate`
extra. The B9 canonical per-run line is already owned by the reconcile/daemon plans.

---

## 5. Done criteria (observable, green)

1. `tests/test_gate.py` field pin asserts **eleven** fields ending in `persists`,
   docstring cites ADR-0006 d.1; `persists` seals + detaches (Unit 1 tests green).
2. `RunContext` carries a sealed, detached `run_row` snapshot **with an empty-proxy
   default** (the two direct-construction tests at `test_context.py:214,241` stay green
   untouched); `resolve_context` populates it via the keyed `Row` comprehension; a gate
   can read `context.run_row["experiment_id"]` with no DB touch (Unit 2 tests green).
   `workdir` derivation unchanged.
3. `process_run` on SUCCESS writes every declared `persists` output **and** the
   satisfaction column in one `update_run` / one `write_txn`; a missing declared key
   quarantines with nothing partially written; a bad column raises `UnknownColumnError`
   and rolls back; a no-`persists` gate writes only its checkmark (all Unit 3 tests
   green).
4. `events.py`, `reconcile.py`, `mock_gates.py`, the `reference-v1` profiles, and the
   migration ladder are **unchanged**; no new `runs` column exists.
5. The full existing suite stays green (dependency-arrow, context ast + the two direct
   `RunContext` constructions, daemon ast-pins, reconcile purity).

---

## 6. Carry-forwards (next increments, not this one)

- **⚑A / experiment gate (plan 10 §8 step 4):** change `resolve_context.workdir` to
  `{local_staging_dir}/{experiment_id}` when `experiment_id` is set (read from the
  snapshot this increment added), name-keyed fallback while NULL. First **real**
  `persists` user: `experiment` gate declares `{"experiment_id": "experiment_id"}`.
- **Migration 007 (plan 10 §6):** adds `globus_submitted` and the seven other
  production satisfaction columns — lands with the gate increments that need them, not
  here.
- **globus_submit / globus_verify (plan 10 §8 step 7):** `globus_submit` persists
  `{"globus_task_id": "globus_task_id"}` atomically with `globus_submitted` (A4 made
  structural); `globus_verify` reads `globus_task_id` back from the run-row snapshot.
  The mechanism they use is exactly what Unit 3 builds and Unit 2 exposes.
