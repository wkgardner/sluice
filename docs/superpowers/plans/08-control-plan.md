# Plan 08 — `control` subsystem (Plan A, subsystem 8 of 9)

**Modules owned:** `sluice/core/control.py` (new — the operator control ops, pure functions
of shape `(db_path, actor, *args) -> ControlResult`), **migration 006** (`excluded_gates`
column, appended to core `db.py`'s ladder per the plan-02 F3-revision precedent — ⚑2
approved), and one **amendment to `sluice/core/profile.py`** (`apply_exclusions`, the pure
per-run contract overlay — ⚑2 approved, unit-0-style with pre-authorized test edits).
**Build order:** db → gate (+ mock gates) → events → context → profile → reconcile →
audit → **control** → daemon
**Tests:** consolidated in `tests/test_control.py`. **Pre-authorized edits elsewhere,
enumerated exactly** (details in the named units): `tests/test_gate.py` —
`test_gate_functions_leave_the_database_untouched` (line ~1012, `schema_version == 5`;
unit 9); `tests/test_db.py` — `test_all_five_migrations_yield_version_five_and_every_object`
(line ~1080: the `== 5` literal **and** the test name/docstring's "five"/"version 5";
unit 9); `tests/test_profile.py` — the surface-equality set 9 → 10 **and** its docstring's
"nine-name surface" (line ~840), plus additive `apply_exclusions` tests (unit 10);
`sluice/core/db.py` — one docstring line in `set_metadata` (unit 2, F7). **No other file
changes.**

Ground truth: design of record **§9** (the op table; each op validates → mutates in a
short txn → audit row → wide event → structured Result; none start or stop work;
`ProtectedError` unless `force`), **§2** (state-only control; two write paths; the daemon
is the only actor), **§5** (lifecycle, protection, quarantine, `poller_metadata`,
`control_audit`), **§7** (holds are poll-only — decision A: the *daemon* calls
`terminate`, never an op), **§4** (gate re-entrancy: `run` "must be idempotent enough to
re-enter" — the property ADR-0005's column-clear-only rewind stands on); errata **A3**
(protection is net-new `protected_at`/`protected_by`; the `locked_at` mutex is dead),
**B2** (`set_gate` no-fabrication; experiment-gate reset hazard), **B5** (pause semantics
half), **F-AUD1** (superseded in part — see §2), **§F/ADR-0004 d.3** (the exclusion op);
plans 01 (F1 `target_run` = run *name*; `update_run` whitelist; `ACTIVE_RUNS_WHERE`),
02 §2/§7 (B2 keyed on `produces`; **`reset_attempts` deleted**; one-shot pointer rule;
its "invalidate then clear" reset choreography is **superseded by ADR-0005** — see §2),
03 §7 (persist-then-emit; every op calls `control_event` exactly once, after its audit
row; ops never see a `Notifier`), 05 §7 (`poller_metadata.pipeline_profile` is owned by
subsystem 8 `set` / 9 seed), 06 §2/§7 (⚑3 as decided: the routed backtrack/fastforward
question, with "a pointer-writing op with no honoring reader is a defect" binding; the
exclusion-op obligation; `get_status` may reuse `is_complete`), 07 §2/§6 (the binding
choreography; `result` vocabulary `"ok" | "noop: <why>" | "refused: <why>"`; the two
routed audit questions); **ADR-0001/0002/0003/0004/0005**; `sluice/core/{db,gate,events,
context,profile,reconcile,audit}.py` as built.

---

## 1. Scope & non-goals

### Owned

| Group | Ops (public surface) |
|---|---|
| Global | `pause`, `resume`, `set_default_profile`; reader `is_paused`; constants `PAUSED_KEY`, `PIPELINE_PROFILE_KEY` |
| Disposition | `hold`, `unhold`, `skip`, `unskip` |
| Recovery | `clear_error` |
| Protection | `protect`, `unprotect` (spec §9's `lock`/`unlock`, renamed per A3 — see §2) |
| Gate state | `set_gate`, `reset_gate`, `backtrack` (pointerless, **column-clear only** — ⚑1/ADR-0005), `exclude_gate`, `include_gate` (⚑2); reader `run_exclusions` |
| Whole-run | `reset` |
| Read-only | `get_status` |
| Types | `ControlResult` (frozen dataclass), `ControlError` + `ProtectedError`, `FabricationError`, `ActiveRunError`, `LifecycleError`; `UnknownRunError`, `UnknownGateError` |

Every mutating op is pure-shaped: `op(db_path, actor, ...) -> ControlResult` — all inputs
explicit (`registry` is a keyword-only **required** parameter where needed, no default,
per guardrail §8), so the API layer (Plan C) wraps each verbatim. Ops are callable and
unit-tested directly (§9).

**Struck from the §9 op table, permanently:** `reset_attempts` (ADR-0002 / plan 02 §7 —
no policy reads a counter, no counter exists, no such audit action string ever);
`fastforward` and the pointer mechanics of `backtrack` (⚑1 — see §2).

**Control performs no filesystem I/O of any kind (ADR-0005).** No op reads, writes, or
deletes a file; no op calls **any** gate function — not `run`, not `terminate`, and not
`invalidate` (ADR-0005 d.2/d.3: rewind is column-clear only; `Gate.invalidate` is a
dormant hook nothing invokes — its fate is Plan B's, §6). Ops touch exactly one thing:
the database.

**Allowed imports (closed set):** stdlib `dataclasses`, `json`, `types`, `sqlite3`,
`collections.abc`; plus `sluice.core.db` (`write_txn`, `get_connection`, `get_run`,
`update_run`, `select_active_runs`, `get_metadata`, `set_metadata`, `all_metadata`,
`now_iso`), `sluice.core.audit` (`record_control_action`), `sluice.core.events`
(`control_event`, `run_outcome`), `sluice.core.profile` (`load_profile`,
`required_gates`, `apply_exclusions`, `Applicability`, `Profile`,
`UnknownProfileError`), `sluice.core.reconcile` (`is_complete`, `next_gate`),
`sluice.core.gate` (`Gate`, typing only). **Forbidden by name** (unit 1, ast-parse, incl.
function-local smuggling): `subprocess`, `os`, `shutil`, `pathlib`, `signal`, `socket`,
`urllib`, `http`, `threading`, `time`, `datetime` (the clock is `db.now_iso`, one
spelling), `structlog` (control's only emission is `events.control_event` — an op module
that could open its own log stream could bypass the canonical line), `slack_sdk`,
`sluice.core.context` (an op with no `resolve_context` has no path to a workdir —
ADR-0005 made structural), `sluice.core.mock_gates` (gates reach ops **only** via the
injected `registry` — control is gate-agnostic), `sluice.gates`/`api`/`cli`. With
`subprocess`/`signal`/`socket`/`os`/`shutil`/`pathlib` unreachable, "control never
starts or stops work — and never touches a file" is a **compile surface**, not a
promise.

### Explicitly NOT owned (and where it belongs)

- **Calling any gate function** — `run`/`terminate` are the daemon's (subsystem 9; §7
  decision A: `hold` writes `lifecycle='held'` and returns; §9's "via terminate"
  describes the *daemon's reaction* at its next checkpoint). `invalidate` is invoked by
  **nothing** (ADR-0005 d.3); whether Plan B removes it, repurposes it, or gives it an
  explicit operator-invoked path is a Plan B decision that must cite ADR-0005 (§6).
- **Deleting artifacts** — the operator's, manually (ADR-0005 d.1). Rewind clears state;
  the daemon re-runs the cleared step and the gate contract's re-entrancy requirement
  (§4: "idempotent enough to re-enter" — the same property crash-retry already demands)
  covers pre-existing/partial artifacts.
- **Honoring exclusions at resolve time** — the daemon (subsystem 9), bound by name in §6:
  `apply_exclusions(load_profile(run["pipeline_version"], registry), run_exclusions(row))`
  is the per-run contract. The op writes; the derivation function ships here; the daemon
  must call it (the no-writer-without-reader constraint, closed within Plan A).
- **B5 disk-halt auto-pause and what clears it** — subsystem 9 (recommended there: the
  daemon calls `control.pause(db_path, "daemon", reason=...)` — same op, same
  choreography; clearing is **operator `resume` only**, no auto-clear/flapping).
- **Seeding `metadata.pipeline_profile` at startup** — subsystem 9 (plan 05 §7); this plan
  ships only the operator `set_default_profile`.
- **The audit writer, `control_event`, `run_outcome`, `is_complete`** — built (plans
  07/03/06); composed here, never reimplemented.
- **HTTP wrappers, authn, exception→status mapping, rendering** — Plan C.
- **Production-gate specifics of B2** (the experiment gate itself, backtrack's
  `checksum|nvd|globus` scope) — Plan B, via the `produces`/re-entrancy mechanisms fixed
  here (§2, §6).

---

## 2. Errata & carried-forward obligations in scope — decisions taken, no TBDs

### ⚑1 — backtrack/fastforward (plan 06 ⚑3's routed question). APPROVED AS AMENDED BY ADR-0005 (2026-07-29, decided by the user).

**Pointerless rewind ships in Plan A, and rewind is COLUMN-CLEAR ONLY.** `reset_gate`
(one gate) and `backtrack` (that gate **and every later gate of the base stamped
profile**) clear the satisfaction columns + `completed_at` in **one transaction** — the
standard single-transaction choreography below, nothing more. They **never call
`invalidate`**; no artifact I/O exists anywhere in `control.py` (ADR-0005 d.2).
Reconcile is stateless, so on the daemon's next look it re-selects the cleared gate and
re-runs it; **pre-existing artifacts are covered by the gate contract's existing
re-entrancy requirement** (design §4: `run` "must be idempotent enough to re-enter" —
the same property a crash-retry already demands; ADR-0005 d.2 cites it as what makes
column-clear-only sound). This supersedes spec §9's `reset_gate` row ("`invalidate` a
gate…"), spec §4's `invalidate`-is-used-by-reset sentence, plan 02 §7's
"invalidate-then-clear" reset choreography, and plan 04 §7's reset-context routing — all
for *automatic* paths, per ADR-0005. `Gate.invalidate` remains in the committed contract
as a **dormant hook nothing invokes** (ADR-0005 d.3); its fate — remove, repurpose, or
an explicit operator-invoked path — is routed to **Plan B by name** (§6).

`fastforward` is not shipped: its one distinct meaning (`run_from_fresh`:
discard-partial-state-before-rerun) is dissolved by the same re-entrancy requirement —
the daemon simply re-runs and the gate tolerates partial artifacts. Therefore **no op
writes `run_from_step`/`run_from_fresh`; nothing reads them; the columns stay dormant**
(schema unchanged; Plan B may revisit only by shipping writer + daemon-side reader
together, per the binding constraint). Unit 13 makes the constraint executable: after
`backtrack`, both pointer columns are asserted NULL. A pointer-writing op with no
honoring reader is a defect — this resolution ships neither.

**F3 (review, confirmed):** the rewind ops iterate the **base stamped profile**
(`load_profile(row["pipeline_version"], registry)`), **not** the exclusion-derived
contract — clearing an excluded gate's column is harmless (the gate is out of the
contract; its column is ignored by `is_complete`/`next_gate`), and cascading over the
base set means a later `include_gate` never resurrects a stale satisfied column. One
test pins it (unit 13).

### ⚑2 — the per-run exclusion op (ADR-0004 d.3). APPROVED (2026-07-29, decided by the user).

Profiles are frozen per `pipeline_version` and shared, so a per-run exclusion cannot edit
a profile. **The design: a per-run overlay column consumed at profile-resolution time.**

1. **Migration 006** (this plan's, in core `db.py`'s ladder): `ALTER TABLE runs ADD
   COLUMN excluded_gates TEXT CHECK (excluded_gates IS NULL OR json_valid(excluded_gates))`
   — NULL = no exclusions; else a sorted, deduplicated JSON array of gate ids
   (`'["mock_beta"]'`). NULL, not `'[]'`, for the empty case (no-fabrication ethic).
   **This supersedes errata F-AUD1's ladder note** ("the migration ladder stays at 005
   and Plan B's gate columns take 006+"): with ⚑2 approved, **006 = `excluded_gates`
   (control) and Plan B's gate columns take 007+**. F-AUD1's substance (append-only as a
   module-surface promise; reader deferred to Plan C ordering by `id`) is untouched.
2. **`profile.apply_exclusions(profile, excluded_gate_ids) -> Profile`** (pure, in
   `profile.py` — profile algebra belongs with `Profile`): returns a Profile with the
   **same gates tuple** (needs-closure intact — a disabled gate satisfies dependents'
   needs by absence, ADR-0004 d.2) and the named ids' applicability flipped to
   `DISABLED`; same `pipeline_version` (the exclusion is an overlay on the run's stamped
   contract, never a new version — §8's freeze is untouched). Unknown id → `ValueError`.
   Empty ids → returns `profile` unchanged.
3. **Ops `exclude_gate` / `include_gate`**: validate against the run's own profile,
   rewrite the column (include-to-empty stores NULL), protection-gated (`force`) — a
   contract edit is destructive. **F2 (review, confirmed): `include_gate` clears
   `completed_at` in the same transaction whenever the recomposed contract is no longer
   complete** — re-adding a step to a completed run's contract makes its `completed_at`
   a lie, exactly the stale cache the reset family clears; the mirror behavior, one
   test (unit 11). **Reader `run_exclusions(run_row) -> tuple[str, ...]`** parses the
   column, fail-loud on any non-array shape.
4. **The daemon (subsystem 9) is bound** to resolve every run's contract as
   `apply_exclusions(load_profile(...), run_exclusions(row))` — named carry-forward, §6,
   with the same binding force as the pointer rule. Reconcile needs zero changes (plan
   06: it consumes whatever Profile it is handed); unit 11 proves the resumption loop
   end-to-end *within this subsystem*.

Rejected alternatives, for the record: rewriting `runs.pipeline_version` to a variant id
(violates stamp-once, §8); DB-stored profiles (profiles are code); a per-run Profile
registry entry (a second authority over contracts).

### ⚑3 — quiescence precondition. SUPERSEDED BY ADR-0005; residual posture recorded, no flag.

The original file-safety rationale (an op deleting artifacts under the daemon's feet) is
**void** — rewind ops touch no files. The residual posture, stated as this plan's
**recommendation, not a safety requirement**: `reset_gate`/`backtrack`/`reset` still
refuse with `ActiveRunError` while the run is daemon-eligible (`lifecycle='active' AND
errored_at IS NULL`, exactly `ACTIVE_RUNS_WHERE`). The honest, weaker rationale: the
daemon may be mid-gate on that run, and its persist would immediately re-stamp the very
column the op just cleared — a **surprising no-op, not a harmful one**. Refusing keeps
the op's effect predictable (rewind a run the daemon cannot be driving: held, skipped,
or quarantined). No new flag — the user has seen this reasoning with ADR-0005. All
state-only ops besides the rewind family carry no such precondition.

### The Result type — spec §9's "a dict" refined to a frozen dataclass. RESOLVED.

```python
@dataclasses.dataclass(frozen=True)
class ControlResult:
    action: str
    actor: str
    target_run: str | None      # None for global ops
    status: str                 # "ok" | "noop"  (refusals raise, they never return)
    message: str | None         # e.g. "already paused"
    changes: Mapping[str, object]  # column/key -> value written; sealed; empty on noop
    audit_id: int               # the control_audit row recording this action
```

`changes` is sealed `MappingProxyType(dict(...))` in `__post_init__` (house pattern —
RunContext/Profile). `to_fields()` returns a plain JSON-shaped dict — the payload for
`control_event(result=...)` and Plan C's response body. The frozen record honors "no
authoritative in-memory state": it *reports* what was committed, it is not the state.

### Refusals vs validation — the exception families and the two routed audit questions. RESOLVED (adopting plan 07 §6's recommendations).

- **`ControlError(RuntimeError)` = policy refusal of a real action → AUDITED and
  EMITTED, then raised.** Subclasses: `ProtectedError` (spec-named), `FabricationError`
  (B2), `ActiveRunError` (residual posture, above), `LifecycleError`
  (cross-disposition, below). §5's "every control action" includes refused attempts — a
  refused `reset_gate` on a protected run is exactly what a trail is for. Audit
  `result` = `"refused: <why>"`.
- **`UnknownRunError(KeyError)` / `UnknownGateError(KeyError)` / propagated
  `UnknownProfileError` / `ValueError` = validation failure → NOT audited, NOT emitted.**
  A typo'd run name identifies no real state to act on; auditing it would seed the trail
  with junk `target_run` values. The line is a *type* fact (ControlError-family ⇒
  audited), pinned in unit 1.
- **`get_status` writes no audit row and emits no event** (plan 07 §6 adopted): it
  mutates nothing; §9's "each op writes a row" reads as each *state-writing* op. It
  consequently takes **no `actor`** — there is nothing to attribute (Plan C still
  authenticates at the HTTP layer).

### The binding choreography (plan 07 §2, implemented in every mutating op — the rewind family included). RESOLVED.

```python
shared_now = now_iso()                      # one instant: state stamps == audit ts
refusal: ControlError | None = None
with write_txn(db_path) as conn:            # ONE short transaction
    row = get_run(conn, run_name)
    if row is None:
        raise UnknownRunError(...)          # rollback: nothing written, no event
    # decide: ok | noop | refusal (protection, lifecycle, quiescence, fabrication)
    if ok:
        update_run(conn, run_name, changes) # / set_metadata for global ops
    audit_id = record_control_action(
        conn, actor=actor, action=action, target_run=run_name,
        args=scalar_params, result="ok" | f"noop: {why}" | f"refused: {why}",
        ts=shared_now,
    )
# committed: state + audit landed atomically, or neither did
control_event(action, actor, target_run=run_name, result=result.to_fields())
if refusal is not None:
    raise refusal
return result
```

There is **no multi-phase variant**: with ADR-0005 the rewind ops perform no disk work,
so every op — `reset` included — is this one pattern (validation, decision, mutation,
audit in a single `write_txn`; the entire artifact-deletion crash-window failure class
is gone, ADR-0005 consequences).

Refinement of plan 07 §6's refusal note, deliberate: the refusal path **exits the same
`write_txn` cleanly having written only the audit row** (mutate skipped), commits, emits,
then raises — one connection, one path, no fresh-connection special case. Both routed
consequences hold: no state change lands unaudited, and no event describes an
uncommitted write (persist-then-emit, plan 03 §7). `args` records the op's **scalar**
parameters only (`gate`, `reason`, `force`); `registry` is machinery, never serialized.
Refusals emit `control.finished` too (result carries `"status": "refused"`) — the event
stream and the trail agree on every attempt.

**The audit `action` vocabulary (D-11):** the `action` string recorded for every audit
row and event **is the op's public function name** (`"pause"`, `"reset_gate"`,
`"exclude_gate"`, …); the closed action set is exactly the mutating ops of the §1 table.
No aliases, no free-form strings.

**F7 — `set_metadata` inside `write_txn` is sanctioned.** `db.set_metadata`'s docstring
currently says "do not wrap it in `write_txn`" — advice for *standalone* callers that is
**superseded for the control choreography**: atomicity with the audit row *requires* the
upsert inside the op's transaction, and it is harmless there — `set_metadata` issues one
plain statement and opens no transaction of its own, so db's actual F2 nesting rule
(never wrap something that opens its *own* transaction, e.g. `apply_migrations`) is not
violated. **Pre-authorized edit (unit 2): the one docstring line in
`sluice/core/db.py::set_metadata`** is reworded to the caller-owns-the-connection
framing (`insert_run`/`update_run` precedent: "needs no transaction of its own; callers
may batch it inside `write_txn`"), removing the contradiction.

### A3 — `protect`/`unprotect`, not `lock`/`unlock`. RESOLVED.

Operator protection is the net-new `protected_at`/`protected_by` pair (paired CHECK,
migration 001). The ops are named **`protect`/`unprotect`** so no one reads them as the
dead `locked_at` processing mutex (A3: dropped, no revival). **The protected set** —
ops that destroy recorded progress or edit the contract, refused with `ProtectedError`
unless `force=True`: `skip`, `set_gate`, `reset_gate`, `backtrack`, `reset`,
`exclude_gate`, `include_gate`. Not gated (protection preserves state; it does not block
recovery or stopping): `pause`, `resume`, `hold`, `unhold`, `unskip`, `clear_error`,
`protect`, `unprotect`, `set_default_profile`, `get_status`. `force=True` never *clears*
protection — it bypasses one refusal and `protected_*` survive (unit 14).

### Lifecycle transitions — explicit, never silent. RESOLVED.

`lifecycle` is one field; cross-disposition writes would silently undo another
operator's disposition. Matrix (same-state = noop, cross-state = `LifecycleError`):
`hold`: active→held ok; held→noop; skipped→refused ("unskip first"). `unhold`:
held→active ok; active→noop; skipped→refused. `skip`/`unskip` mirror with roles
swapped. Every transition writes the trio (`lifecycle_by=actor`, `lifecycle_at=
shared_now`, `lifecycle_reason=reason`) — including un-ops, so the row always names its
latest disposition's provenance. Quarantine is orthogonal (F4's two AND-ed clauses): a
quarantined run can be held/skipped, `hold` never touches `errored_at` — and recovery
composes: a run that is both held **and** quarantined needs **both** `unhold` and
`clear_error` before it re-enters eligibility (D-06/E-12; tested, unit 5).

### B2 — `set_gate` no-fabrication + the experiment-gate rewind hazard. RESOLVED (mechanism here, content Plan B) — cites plan 02 §2 B2 as required.

- **`set_gate` refuses, keyed on `produces`** (plan 02's carry-forward): a state-only op
  can fabricate *no* artifact, so any gate with non-empty `produces` →
  `FabricationError` naming the unfabricatable outputs, unless `force=True` (spec §9:
  the operator takes responsibility — e.g. artifacts restored by hand). A gate with
  empty `produces` sets freely (unit 8 proves refusal is keyed, not blanket). In Plan B
  the experiment gate's `produces` includes `experiment_id` — the refusal is already
  mechanical; no new code will be needed.
- **The rewind half of B2 is structurally defused by ADR-0005.** Rewind clears
  *satisfaction columns* only — never `produces` state, never identity columns — so
  `experiment_id` (and every on-disk artifact) survives any `reset_gate`/`backtrack`/
  `reset`; the orphaning hazard B2 warned about cannot arise from control. What remains
  is a **Plan B re-entrancy obligation** (§6): the experiment gate's `run`, re-entered
  with `experiment_id` already non-NULL on the row, must **reuse** it — never create a
  duplicate LabKey experiment (errata C1's non-idempotent create is the trap).
- `reset` **never clears identity columns** (`experiment_id`, `size_tb`, `detected_at`,
  `pipeline_version`, `slack_thread_ts` — A5: Slack handles are not recomputable).

### B5 (operator half) + metadata vocabulary. RESOLVED here; daemon half carried.

Control owns the key names and value grammar: `PAUSED_KEY = "paused"`, values `"1"`
(paused) / `"0"`; `is_paused(conn)` is the single canonical parse (only `"1"` is
paused). `PIPELINE_PROFILE_KEY = "pipeline_profile"` (spec §5's name). Global ops audit
with `target_run=None` (F1) and emit `control.finished` with no `run.name` (plan 03 —
a fabricated run name would corrupt per-run jsonl routing). Whether the daemon
auto-clears a disk-halt pause is subsystem 9's decision (recommendation: never — operator
`resume` only); the run-that-tripped-the-floor question (errata B5) travels with it.

### `set_default_profile` — plan 05 §7's "subsystem 8 `set`". RESOLVED.

Not in spec §9's table but named by plan 05: the active-default id is DB metadata, never
config. `set_default_profile(db_path, actor, pipeline_version, *, registry)` validates
via `load_profile` (fail-loud `UnknownProfileError`, not audited — validation), then
upserts `PIPELINE_PROFILE_KEY`. It changes only what *future* detections stamp — every
in-flight run keeps its frozen version (§8); the docstring says so.

---

## 3. Units, in dependency order

Sixteen units, one RED→GREEN→REFACTOR each. **Green-on-arrival carve-outs, declared:**
unit 16 adds no production code beyond finalizing `__all__` (plan 02 unit-9 precedent).
All DB tests run on real files under `tmp_path` via `initialize_database`; gates reach
ops only through `REFERENCE_PROFILES` or test-local registries; events are asserted via
`structlog.testing.capture_logs()` (test_events precedent). `__all__` grows as names
land. Docstrings per ADR-0001 (`Raises:` exhaustive on every op — the exception families
are the API contract Plan C maps to HTTP).

### Unit 1 — skeleton: exceptions, `ControlResult`, import hygiene

Signatures: the §2 exception tree; `ControlResult` + `to_fields()` as specified.

- `test_control_import_hygiene` — ast-parse of `control.py`: imports ⊆ the §1 allowed
  set; none of `subprocess`/`os`/`shutil`/`pathlib`/`signal`/`socket`/`urllib`/`http`/
  `threading`/`time`/`datetime`/`structlog`/`slack_sdk`/`sluice.core.context`/
  `sluice.core.mock_gates`/`sluice.gates`/`sluice.api`/`sluice.cli` anywhere, incl.
  function-local imports (the smuggling guard pattern from test_db). This is ADR-0005's
  structural half: no filesystem module is reachable.
- `test_control_result_is_frozen_sealed_and_serializable` — field rebinding raises
  `FrozenInstanceError`; `changes` item-write raises `TypeError` and is detached from the
  constructing dict (mutate the original, proxy unchanged); `json.dumps(r.to_fields())`
  round-trips with the pinned keys `{action, actor, target_run, status, message,
  changes, audit_id}`.
- `test_exception_families_partition_audited_from_validation` — each of
  `ProtectedError`/`FabricationError`/`ActiveRunError`/`LifecycleError` is a
  `ControlError` subclass; `UnknownRunError`/`UnknownGateError` subclass `KeyError` and
  are **not** `ControlError` (the audited/not-audited line as a type fact, §2).

### Unit 2 — `pause` / `resume` / `is_paused`

```python
def pause(db_path, actor, *, reason: str | None = None) -> ControlResult: ...
def resume(db_path, actor, *, reason: str | None = None) -> ControlResult: ...
def is_paused(conn: sqlite3.Connection) -> bool: ...
PAUSED_KEY = "paused"; PIPELINE_PROFILE_KEY = "pipeline_profile"
```

The simplest full choreography — the pattern every later op repeats. **Pre-authorized
edit (F7):** the one `sluice/core/db.py::set_metadata` docstring line ("do not wrap it
in `write_txn`") reworded per §2 — control batches the upsert inside the op's
transaction for state+audit atomicity, which db's F2 nesting rule permits.

- `test_pause_writes_state_audit_and_event_in_the_binding_order` — on an initialized DB
  inside `capture_logs()`: `pause(db, "will", reason="drill")` → fresh connection reads
  `poller_metadata['paused'] == "1"`; exactly one `control_audit` row with
  `action="pause"`, `target_run IS NULL`, `args_json == '{"reason": "drill"}'`,
  `result="ok"`; exactly one captured `control.finished` with `control.actor == "will"`
  and **no `run.name` key**; returned `ControlResult.status == "ok"` and `audit_id` ==
  the row's id.
- `test_pause_and_resume_are_audited_noops_when_state_already_matches` — pause twice:
  second returns `status="noop"`, writes a second audit row `result="noop: already
  paused"`, metadata unchanged; `resume` on a never-paused DB likewise noop.
- `test_is_paused_only_the_literal_1_means_paused` — parametrized: key absent → False;
  `"0"` → False; `"1"` → True (the one canonical parse the daemon reuses).

### Unit 3 — `hold` / `unhold`

```python
def hold(db_path, actor, run_name, *, reason: str | None = None) -> ControlResult: ...
def unhold(db_path, actor, run_name, *, reason: str | None = None) -> ControlResult: ...
```

- `test_hold_sets_lifecycle_trio_and_shares_the_audit_instant` — held run has
  `lifecycle='held'`, `lifecycle_by='will'`, `lifecycle_reason` as given, and
  `lifecycle_at` **string-equal** to the audit row's `ts` (the shared-instant `ts=`
  parameter, plan 07 unit 1, exercised for real); run vanishes from
  `select_active_runs`.
- `test_hold_is_state_only_no_gate_function_runs` — run with a workdir containing beta's
  artifacts; `hold` → no `mock_beta.terminated` trace appears and the workdir file set
  is unchanged (the op never calls `terminate` — that is the daemon's §7 reaction); and
  the signature pin: `inspect.signature(hold)` has no `registry` parameter (state-only
  ops structurally cannot even name a gate).
- `test_hold_unknown_run_fails_loud_with_no_audit_and_no_event` — `UnknownRunError`;
  `control_audit` count unchanged; zero `control.finished` captured (validation is
  silent to the trail; also pins no-event-without-commit).
- `test_hold_and_unhold_lifecycle_matrix` — parametrized over the §2 matrix: held→hold
  noop (audited `"noop: ..."`); skipped→hold `LifecycleError` with audit row
  `result="refused: ..."`, an emitted event whose `control.result` carries
  `"status": "refused"`, and lifecycle unchanged; held→unhold ok (trio re-stamped to the
  unholding actor); active→unhold noop; skipped→unhold refused.
- `test_hold_works_on_a_quarantined_run` — errored run holds fine; `errored_at`
  untouched (quarantine and disposition are orthogonal, F4).

### Unit 4 — `skip` / `unskip` (protection debuts)

```python
def skip(db_path, actor, run_name, *, reason=None, force: bool = False) -> ControlResult: ...
def unskip(db_path, actor, run_name, *, reason=None) -> ControlResult: ...
```

- `test_skip_sets_lifecycle_and_leaves_completed_at_null` — the spec §9 pin: skipped
  run's `completed_at IS NULL`, lifecycle trio written, run out of `select_active_runs`.
- `test_skip_on_protected_run_refuses_unless_force` — protected run: `skip` raises
  `ProtectedError` naming `protected_by`; audit row `result="refused: ..."`; state
  unchanged. `skip(..., force=True)` succeeds, audit `args_json` includes
  `"force": true`, and `protected_*` **survive** (force bypasses, never unprotects).
- `test_skip_and_unskip_lifecycle_matrix` — the mirror of unit 3's matrix (held→skip
  refused, etc.), parametrized.

### Unit 5 — `clear_error`

```python
def clear_error(db_path, actor, run_name, *, reason=None) -> ControlResult: ...
```

- `test_clear_error_clears_the_pair_and_restores_eligibility` — quarantined run
  (`errored_at`+`error_reason` set): after the op both are NULL in one write (the
  paired CHECK makes a half-clear impossible), the run reappears in
  `select_active_runs`, audit records the cleared reason in `args`/result detail (the
  evidence of *what* was forgiven). This is ADR-0002's sole sanctioned recovery.
- `test_clear_error_on_a_clean_run_is_a_noop` — status `"noop"`, audited.
- `test_recovery_from_held_and_quarantined_needs_both_ops` — D-06/E-12: a run both held
  and quarantined; `clear_error` alone → still **absent** from `select_active_runs`
  (the eligibility predicate is AND-ed, F4); then `unhold` → present. Both orders
  asserted symmetric (unhold-first leaves it quarantined-ineligible until
  `clear_error`).

### Unit 6 — `protect` / `unprotect`

```python
def protect(db_path, actor, run_name, *, reason=None) -> ControlResult: ...
def unprotect(db_path, actor, run_name, *, reason=None) -> ControlResult: ...
```

- `test_protect_stamps_the_paired_columns` — `protected_at == audit ts`,
  `protected_by == actor` (the CHECK is satisfied by construction; `reason` rides the
  audit args — there is no `protected_reason` column, deliberately).
- `test_protect_and_unprotect_are_noops_when_already_so` — and unprotect clears **both**
  columns (paired CHECK). No `force` parameter exists on either (protection is
  operator-owned; unprotect *is* the override).

### Unit 7 — `set_default_profile`

```python
def set_default_profile(db_path, actor, pipeline_version, *, registry) -> ControlResult: ...
```

- `test_set_default_profile_validates_then_upserts` — with `REFERENCE_PROFILES`: set to
  `"reference-v1-alpha-disabled"` → `poller_metadata['pipeline_profile']` updated, audit
  ok with `target_run IS NULL`; unknown version → `UnknownProfileError` propagates,
  metadata unchanged, no audit row (validation). One existing run's `pipeline_version`
  asserted untouched — the freeze (§8): the op steers *future* stamps only.

### Unit 8 — `set_gate` (the B2 mechanism)

```python
def set_gate(db_path, actor, run_name, gate_id, *, registry, force: bool = False,
             reason=None) -> ControlResult: ...
```

Resolves the run's own profile (`load_profile(row["pipeline_version"], registry)`), the
gate by id, then: out-of-contract → refuse; satisfied → noop; non-empty `produces`
without `force` → `FabricationError`; else write `satisfied_column = 1` (via
`update_run` — the whitelist is the injection backstop; the column name comes from the
`Gate` record, never from the caller).

- `test_set_gate_refuses_to_fabricate_produces_without_force` — `set_gate(...,
  "mock_alpha")` → `FabricationError` whose message names `mock_alpha.marker` (the
  unfabricatable produces); column stays 0; audit `result="refused: ..."`.
- `test_set_gate_force_sets_the_column_without_doing_the_work` — `force=True` →
  `mock_alpha_done == 1` **and the workdir contains no marker file** (the asymmetric
  mirror, spec §9: satisfied without run); audit args carry `"force": true`.
- `test_set_gate_without_force_succeeds_for_a_produces_free_gate` — test-local registry
  whose gate has `produces=()` → sets without force (refusal is keyed on `produces`,
  not blanket — plan 02 §2 B2 cited).
- `test_set_gate_rejects_unknown_hostile_and_out_of_contract_gates` — parametrized:
  gate id absent from the profile → `UnknownGateError`, no audit; the hostile id
  `"x'; DROP TABLE runs;--"` → `UnknownGateError` and `runs` still exists (caller input
  never reaches SQL — it dies at the registry lookup); a `DISABLED`-in-profile gate →
  `ControlError` refusal `"out of contract"`, audited.
- `test_set_gate_on_a_satisfied_gate_is_a_noop` — audited, column untouched.

### Unit 9 — migration 006: `excluded_gates` (⚑2; db-ladder amendment)

`_migration_006_excluded_gates` appended to `db.MIGRATIONS` (F3-revision precedent —
contract columns live in core's ladder; **F-AUD1 superseded**: Plan B's gate columns
take 007+). **Pre-authorized edits, exactly three sites:**

1. `tests/test_db.py::test_all_five_migrations_yield_version_five_and_every_object`
   (line ~1080) — the `schema_version == 5` literal → `== 6`, **and** the test's name
   and docstring ("all five migrations…version five" → six; its docstring says it is
   edited consciously each time the ladder grows — this is that conscious edit,
   including the residual prose H-16/reviewer-3 named).
2. `tests/test_gate.py::test_gate_functions_leave_the_database_untouched` (line ~1012)
   — **BLOCKER without this edit**: it asserts `schema_version(connection) == 5` and
   goes red under 006. Reword **ladder-agnostic**: capture `schema_version` before the
   gate calls and assert it is unchanged after (the test's actual point — gates change
   no schema — which then never breaks on ladder growth again). Hardcoding `== 6` is
   the fallback if the reviewer prefers literals; the plan recommends the agnostic
   form.
3. `test_no_production_gate_columns_yet` is **untouched** (`excluded_gates` is not a
   gate column) — verified, not edited.

- `test_migration_006_adds_a_nullable_json_checked_excluded_gates` — initialize a
  populated DB (rows inserted at version ≤5 pattern): column present, pre-existing rows
  NULL (no exclusions — correct, none were ever excluded); raw
  `UPDATE runs SET excluded_gates='not json'` → `sqlite3.IntegrityError` (the CHECK);
  `'["mock_beta"]'` accepted.

### Unit 10 — `profile.apply_exclusions` (⚑2; profile amendment)

```python
def apply_exclusions(profile: Profile, excluded_gate_ids: Iterable[str]) -> Profile: ...
```

**Pre-authorized edits:** `tests/test_profile.py` — the surface-equality set in
`test_profile_exports_exactly_the_documented_surface` grows 9 → 10 names **and its
docstring's "nine-name surface" wording follows** (line ~840 — the residual
H-16/reviewer-3 named); new `apply_exclusions` tests are **added** to that file
(behavior lives with its module); nothing else in it changes. `profile.py` gains the
function + one `__all__` entry, no new imports.

- `test_apply_exclusions_overlays_disabled_preserving_the_gate_set` — on
  `REFERENCE_PROFILE`, exclude `"mock_alpha"`: result has the **same** `gates` tuple
  (needs-closure — beta's `needs=("mock_alpha",)` still resolves),
  `applicability["mock_alpha"] is Applicability.DISABLED`,
  `required_gates(result) == (MOCK_BETA,)`, `pipeline_version` unchanged; the input
  profile is unmutated (frozen — re-assert its applicability).
- `test_apply_exclusions_unknown_id_fails_loud_and_empty_is_identity` — unknown id →
  `ValueError` naming it; `apply_exclusions(p, ()) is p`.

### Unit 11 — `exclude_gate` / `include_gate` / `run_exclusions` (⚑2)

```python
def exclude_gate(db_path, actor, run_name, gate_id, *, registry, reason=None,
                 force: bool = False) -> ControlResult: ...
def include_gate(db_path, actor, run_name, gate_id, *, registry, reason=None,
                 force: bool = False) -> ControlResult: ...
def run_exclusions(run_row) -> tuple[str, ...]: ...
```

- `test_exclude_gate_resumes_a_stuck_run_the_adr_0004_miniature` — the load-bearing
  test: `reference-v1` run with `mock_alpha_done=1`, beta stuck at 0;
  `exclude_gate(..., "mock_beta")` → column stores exactly `'["mock_beta"]'`; then the
  full honoring loop composed **in this test**:
  `derived = apply_exclusions(load_profile(row["pipeline_version"], REFERENCE_PROFILES),
  run_exclusions(row))` → `reconcile.next_gate(row, derived) is None` **and**
  `reconcile.is_complete(row, derived)` — the run resumes past the excluded step with
  zero daemon or reconcile changes (statelessness doing the work; the reader the op's
  write depends on exists and is proven).
- `test_exclude_gate_is_idempotent_sorted_and_deduplicated` — exclude twice → second is
  an audited noop, column unchanged; excluding beta then alpha stores
  `'["mock_alpha", "mock_beta"]'` (sorted — deterministic bytes, the audit-plan ethic).
- `test_exclude_gate_guards` — parametrized: unknown gate → `UnknownGateError`, no
  audit; protected run → `ProtectedError` unless `force`; a gate already `DISABLED` in
  the profile → `ControlError` refusal ("already out of contract by profile"), audited.
- `test_include_gate_restores_and_empty_becomes_null` — include the excluded gate →
  reconcile (recomposed) selects it again; including the **last** excluded gate stores
  SQL NULL, not `'[]'` (`excluded_gates IS NULL` asserted); include of a never-excluded
  gate → noop.
- `test_include_gate_clears_a_now_stale_completed_at` — F2: run completed *under* an
  exclusion (`completed_at` stamped, excluded gate's column 0); `include_gate` of that
  gate → in the **same transaction** `completed_at` is NULL (the recomposed contract is
  incomplete, so the stamp is a lie — the reset-family mirror) and reconcile selects
  the re-included gate; control case: including a gate whose column is already 1 leaves
  a still-true `completed_at` untouched.
- `test_run_exclusions_parses_and_fails_loud_on_shape_drift` — NULL → `()`;
  `'["a","b"]'` → `("a", "b")`; a JSON-valid non-array (`'{"a": 1}'`, planted by raw
  SQL) → `ValueError`, never a silent `()` (guardrail §8 — silent fallback would
  silently *unexclude*).

### Unit 12 — `reset_gate` (column-clear only — ADR-0005)

```python
def reset_gate(db_path, actor, run_name, gate_id, *, registry,
               force: bool = False, reason=None) -> ControlResult: ...
```

Standard single-transaction choreography (§2 — no phases, no `config`, no context, no
gate-function call): resolve the **base** stamped profile, find the gate, guard
(quiescence, protection), then clear `satisfied_column` → 0 and `completed_at` → NULL
in the op's one txn. Artifacts are untouched — the daemon re-runs the gate and §4
re-entrancy covers what is already on disk.

- `test_reset_gate_refuses_on_a_daemon_eligible_run` — the §2 residual posture,
  executable: active, non-quarantined run → `ActiveRunError` ("hold, skip, or wait for
  quarantine — an in-flight gate would immediately re-stamp this column"), audit
  `"refused: ..."`, columns unchanged.
- `test_reset_gate_clears_column_and_stale_completed_at_leaving_artifacts` — held run,
  alpha satisfied, marker on disk, `completed_at` stamped: after the op
  `mock_alpha_done == 0`, `completed_at IS NULL`, audit ok — **and the marker file is
  still present** (the ADR-0005 pin: state cleared, artifacts intact, deletion is the
  operator's); `reconcile.next_gate(fresh_row, profile)` returns `MOCK_ALPHA` again —
  the rewind proven by re-selection, no pointer, no invalidate.
- `test_reset_gate_on_an_unsatisfied_gate_is_an_audited_noop` — column already 0 →
  `status="noop"`, nothing written (with no invalidate there is no partial-state
  discard; §4 re-entrancy at the next `run` is the whole story — the ⚑1 amendment in
  code).
- `test_reset_gate_guards` — protected → `ProtectedError` unless force; unknown gate →
  `UnknownGateError`, no audit.

### Unit 13 — `backtrack` (⚑1; column-clear cascade)

```python
def backtrack(db_path, actor, run_name, gate_id, *, registry,
              force: bool = False, reason=None) -> ControlResult: ...
```

Target = the named gate and **every later gate of the base stamped profile** (by
`order` — F3: the base set, not the exclusion-derived contract); one txn clears all
their columns + `completed_at`. No gate function is called; there is no invalidation
order to sequence — the cascade is a single `update_run`.

- `test_backtrack_rewinds_target_and_everything_after` — held `reference-v1` run, both
  gates satisfied, both artifacts present, `completed_at` set; `backtrack` to
  `mock_alpha` → both columns 0, `completed_at IS NULL`, **both artifact files still on
  disk** (ADR-0005), and `next_gate` re-selects `mock_alpha`.
- `test_backtrack_to_the_last_gate_touches_only_it` — backtrack to `mock_beta`: alpha's
  column (and everything else) intact.
- `test_backtrack_cascades_over_the_base_profile_including_excluded_gates` — F3: run
  with `mock_beta` excluded and both columns set; backtrack to `mock_alpha` clears
  **both** columns (the excluded gate's clear is harmless — out of contract — and a
  later `include_gate` finds no stale satisfied column); derived-contract completion is
  re-derived false, alpha re-selected.
- `test_backtrack_never_writes_the_pointer_columns` — after a successful backtrack,
  `run_from_step IS NULL AND run_from_fresh IS NULL` — the ⚑1 no-pointer resolution and
  plan 06's "defect" constraint, executable.
- `test_backtrack_guards` — `ActiveRunError` on an eligible run; `ProtectedError`
  unless force (both audited refusals).

### Unit 14 — `reset` (column-clear only — ADR-0005)

```python
def reset(db_path, actor, run_name, *, registry, force: bool = False,
          reason=None) -> ControlResult: ...
```

Freshly-detected **state** in one txn: clear all base-profile gate columns,
`completed_at`, `error_reason`/`errored_at`, `excluded_gates` (fresh = the full stamped
contract), `globus_task_id`, `run_from_step`/`run_from_fresh` (defensive — dormant in
Plan A); set `lifecycle='active'` with the trio recording the resetting actor. No file
is touched (ADR-0005 — a reset run's old artifacts await the re-running gates'
re-entrant `run`s, or the operator's manual cleanup). **Preserved:** `name`,
`experiment_id`, `size_tb`, `detected_at`, `pipeline_version`, `slack_thread_ts`,
`protected_*` (§2 B2 — identity and non-recomputable handles are never cleared).

- `test_reset_restores_the_freshly_detected_shape_preserving_identity` — held run with
  columns set, artifacts on disk, `completed_at`, an exclusion, and a planted
  `globus_task_id`/`slack_thread_ts`: after reset, every cleared column is NULL/0,
  lifecycle is `active` with `lifecycle_by=actor`, **the workdir file set is
  unchanged** (ADR-0005); `experiment_id`, `size_tb`, `detected_at`,
  `pipeline_version`, and `slack_thread_ts` are byte-identical to before (the preserve
  list, asserted value-by-value).
- `test_reset_on_a_quarantined_run_needs_no_hold_and_clears_the_error` — quarantined
  (ineligible ⇒ quiescent already): reset succeeds directly, error pair cleared —
  quarantine is one of the residual posture's sanctioned entry states.
- `test_reset_guards_and_force_preserves_protection` — eligible run → `ActiveRunError`;
  protected+held run → `ProtectedError`; with `force=True` it succeeds **and**
  `protected_*` survive the reset (protection is not consumed).

### Unit 15 — `get_status`

```python
def get_status(db_path, run_name: str | None = None, *, registry) -> dict[str, object]: ...
```

Read-only; no actor, no audit, no event (§2). Overview shape (run_name None):
`{"paused": bool, "pipeline_profile": str | None, "runs": [<summary>...]}` where each
summary carries `name`, `experiment_id` (key **always present**, `None` allowed — the
deliberate contrast with the event builders' omit-when-NULL: a status table renders the
blank; and experiment_id is the operator's reference key, surfaced everywhere),
`outcome` (via `events.run_outcome`), `lifecycle`, `complete` (via
`reconcile.is_complete` over the **derived** contract — exclusions applied),
`protected` (bool), `excluded_gates` (list), `error_reason` when quarantined.
Single-run shape adds `pipeline_version`, `detected_at`, `completed_at`, and the
contract partition `{"gates": {"satisfied": [...], "unsatisfied": [...]}}`. Status is
**total**: a per-run anomaly becomes a per-run error field, never a fleet-wide raise.
Fields are a pinned seam; Plan C extends consciously.

- `test_get_status_single_run_reports_the_derived_contract` — run with alpha satisfied,
  beta excluded: `complete` is True, `excluded_gates == ["mock_beta"]`, the partition
  covers only contract gates, `experiment_id` key present with `None`.
- `test_get_status_overview_reports_global_and_per_run_state` — paused DB, two runs
  (one quarantined): `paused is True`, `pipeline_profile` echoed, the quarantined
  entry has `outcome == "quarantined"` and its `error_reason`.
- `test_get_status_is_a_pure_read` — audit count, `runs` rows, and metadata identical
  before/after; zero `control.finished` captured.
- `test_get_status_reports_a_drifted_profile_instead_of_raising` — one run stamped with
  an unregistered version: its entry carries `profile_error` naming the version, no
  fabricated `complete`/gates fields; the other runs are reported normally (status is
  total — it must show the broken run, not hide the fleet).
- `test_get_status_reports_corrupt_exclusions_instead_of_raising` — F5, mirroring the
  drift test: one run's `excluded_gates` planted (raw SQL) as a JSON-valid non-array
  (`'{"a": 1}'`): its entry carries an `exclusions_error` field naming the bad shape,
  no fabricated `complete`/`excluded_gates` fields; the other runs are reported
  normally. (`run_exclusions`' loud `ValueError` is caught per-run here, and only
  here.)
- `test_get_status_unknown_run_fails_loud` — named-run form → `UnknownRunError`.

### Unit 16 — end-to-end journey + surface (green-on-arrival)

No production code beyond finalizing `__all__`.

- `test_operator_journey_end_to_end` — the §12 deliverable ("fully controllable via the
  pure ops") over the real schema, `REFERENCE_PROFILES`, a `tmp_path` staging dir, and a
  minimal config: insert a `reference-v1` run → `pause`/`resume` → play the daemon
  verbatim for one gate (resolve_context, mkdir, `MOCK_ALPHA.run(ctx)`, persist column
  in a short txn — plan 06 §7's shape) → `hold` → `reset_gate` alpha (column 0,
  **marker still on disk**) → `unhold` → re-run alpha over the existing marker
  (SUCCESS — §4 re-entrancy exercised for real, the ADR-0005 soundness argument
  demonstrated) + persist → script beta `failure`, run, persist quarantine →
  `clear_error` → `exclude_gate` beta → derived contract `is_complete` → the audit
  trail read `ORDER BY id` lists exactly the op sequence in action order.
- `test_no_control_op_ever_touches_the_workdir` — with artifacts on disk, each of
  `pause`/`hold`/`skip(force)`/`protect`/`exclude_gate(force)`/`set_gate(force)`/
  `reset_gate(force)`/`backtrack(force)`/`reset(force)` runs (rewinds on a held run);
  after all nine the workdir's file set and contents are unchanged — ADR-0005 total
  over the whole surface, behaviorally (the rewind family included, which the ast test
  alone cannot prove).
- `test_control_public_surface_is_exactly_the_declared_ops` — `__all__` set equality
  against the §1 table's names; plus the shape pin (H-12, corrected to the declared
  signatures): per-op `inspect.signature` check that `db_path` and `actor` are the
  first two positional parameters, any target identifiers (`run_name`, `gate_id`,
  `pipeline_version`) follow positionally exactly as declared in §3, and every option
  (`reason`, `force`, `registry`) is keyword-only; the documented exemptions
  (`is_paused`, `run_exclusions`, `get_status`) asserted to have no `actor` parameter.

---

## 4. Invariant checks

| Invariant | How subsystem 8 could violate it | Guard |
|---|---|---|
| State-only control — never start/stop work | an op running a gate, signaling the daemon, or spawning a process | unit 1 ast (no `subprocess`/`signal`/`socket`); unit 3 (`hold` leaves no terminate trace; no `registry` on state-only ops) |
| **No automatic file deletion / no file I/O (ADR-0005)** | a rewind op invalidating artifacts; any filesystem reach | unit 1 ast (no `os`/`shutil`/`pathlib`/`sluice.core.context`); unit 12/14 artifacts-intact assertions; unit 16 `test_no_control_op_ever_touches_the_workdir` over all nine mutating-op classes |
| The daemon is the only actor | rewinding a run mid-flight → the in-flight persist re-stamps the cleared column (surprising no-op) | the §2 residual posture: `ActiveRunError` on eligible runs, `test_reset_gate_refuses_on_a_daemon_eligible_run` (+ backtrack/reset guards) — a recommendation-backed guard, not file-safety |
| DB is the single source of truth | a second in-memory authority | sealed frozen `ControlResult` (unit 1); no module-level mutable state (review dimension) |
| Short transactions only | an op holding a txn across anything slow | structural: every op is the §2 single-txn choreography and control performs no I/O at all (unit 1 ast) — nothing exists that could span a transaction |
| State + audit atomically; event after commit | splitting mutate/audit; emitting on a failed op | plan 07 unit 3 proved the primitive; here: every op test asserts state+audit together; `test_hold_unknown_run_fails_loud_with_no_audit_and_no_event` pins no-event-without-commit; refusal rows pinned per op |
| No unaudited action, no junk trail | skipping refusal audits; auditing typos | the ControlError-vs-KeyError type partition (unit 1) exercised in units 3/4/8/11–14; the D-11 closed action vocabulary (§2) |
| `gates → core`; core gate-agnostic | control importing `mock_gates` or `sluice.gates` | unit 1 ast; `tests/test_dependency_arrows.py` auto-covers `sluice.core.control` |
| One-shot pointer rule (plan 06 ⚑3) | shipping a pointer write with no reader | ⚑1 ships no pointer write at all; `test_backtrack_never_writes_the_pointer_columns` |
| Exclusion write has its honoring reader | op writes `excluded_gates` nothing consumes | `apply_exclusions`+`run_exclusions` ship in the same plan; the unit-11 miniature composes write→derive→reconcile; §6 binds the daemon |
| Stamp-once `pipeline_version` (§8) | `set_default_profile` or `reset` rewriting a run's version | unit 7 (in-flight run untouched); unit 14 preserve list |
| No secrets, no production defaults | a default registry; env reads | `registry` keyword-only **required** (TypeError when omitted — surface pin, unit 16); `os` unimportable (unit 1) |
| Injection defense | caller `gate_id`/`run_name` reaching SQL | column names come only from `Gate` records via the registry (`UnknownGateError` first — unit 8 hostile-id test); `update_run`'s whitelist is the backstop (plan 01) |

---

## 5. Done criteria

1. `sluice/core/control.py` exists with exactly the §1 surface; `pixi run lint` clean;
   docstrings per ADR-0001 with exhaustive `Raises:` on every op; **zero filesystem
   reach and zero gate-function calls anywhere in the module** (ADR-0005, ast-guarded).
2. `tests/test_control.py` green (≈46 test functions, several parametrized); the suite
   grows from **321** with only the enumerated pre-authorized edits outside it
   (test_gate.py ladder-agnostic reword; test_db.py end-state literal + name/docstring;
   test_profile.py surface set + docstring + additive tests; the one db.py
   `set_metadata` docstring line); dependency-arrow guard green auto-covering
   `sluice.core.control`.
3. Migration 006 applied by `initialize_database` on a fresh **and** a populated DB
   (unit 9); `db.MIGRATIONS` ends at 6; **Plan B's gate columns take 007+** (the F-AUD1
   supersession recorded in §2).
4. The unit-16 journey proves a run is fully controllable — paused, held, rewound
   (column-clear, artifacts intact, re-entrant re-run), quarantined, cleared, excluded,
   completed — through the pure ops alone, with a complete ordered audit trail and zero
   daemon code.
5. Subsystem 9 can start: it inherits `is_paused`/`PAUSED_KEY`, the exclusion-honoring
   resolution recipe, the hold-terminate reaction, and B5's auto-pause via
   `control.pause` — all named in §6 with nothing left to design here.

---

## 6. Carry-forwards (named, so no plan rediscovers them)

- **Subsystem 9 (daemon) — BINDING obligations:**
  - **Contract resolution honors exclusions:** per eligible run,
    `profile = apply_exclusions(load_profile(run["pipeline_version"], registry),
    run_exclusions(run_row))` — always, not conditionally. This is the honoring reader
    the ⚑2 op's write depends on (same force as the pointer rule); the daemon plan owes
    an integration test mirroring unit 11's miniature through the real loop.
  - **Loop head reads `is_paused(conn)`**; gate checkpoints re-read run state and, on
    `lifecycle='held'`, call `gate.terminate(ctx)` if present and record `HELD`
    (decision A — the reaction half of `hold`; `terminate` *stops* work, which ADR-0005
    does not touch — it governs deletion).
  - **B5 auto-pause** = `control.pause(db_path, "daemon", reason="disk floor: ...")` —
    the same op, same choreography, audited with `target_run` NULL (actor string
    `"daemon"` recommended; final call is subsystem 9's). Recommended: **no auto-clear**
    — operator `resume` only (flapping vs silent-stall, errata B5; decide there, with
    the does-the-tripping-run-quarantine question).
  - Seed `PIPELINE_PROFILE_KEY` at startup if absent (plan 05 §7); stamp
    `pipeline_version` at detection from it.
  - Progress writes are never audited (plan 07 §6); the daemon never calls control ops
    for its own persists.
  - Pointer columns stay unread (⚑1); any future pointer feature ships op + reader +
    same-txn clear together (plan 02 §7 one-shot rule).
  - **The daemon never calls `invalidate`** — spec §7's `run_from_fresh` pseudocode line
    is dead under ⚑1 + ADR-0005; there is no automatic invalidation path anywhere.
- **Plan B (production gates):**
  - **`Gate.invalidate`'s fate — remove, repurpose, or an explicit operator-invoked
    path — is Plan B's decision and must cite ADR-0005 (d.3).** If Plan B ever gives
    `invalidate` an operator-invoked path, it must tolerate already-absent artifacts
    (the operator may have deleted them manually first).
  - **Errata B7's automatic `reap` gate is frozen pending Will's explicit sign-off at
    Plan B** (ADR-0005 d.4) — automatic raw-FASTQ deletion contradicts d.1 as a
    default.
  - **The experiment gate's `run` must be re-entrant over an existing
    `experiment_id`** (§2 B2 under ADR-0005): rewinds clear the satisfaction column but
    never `experiment_id`, so a re-run must detect the non-NULL id and **reuse** it —
    never create a duplicate LabKey experiment (errata C1's non-idempotent create is
    the trap). Its `produces` must name `experiment_id` so `set_gate`'s refusal stays
    mechanical.
  - **`globus`'s re-entrancy carries the live-transfer question**: a re-run with
    `globus_task_id` non-NULL resumes/verifies the existing transfer rather than
    resubmitting (errata A4). `reset` clears the column (a full restart is the point);
    partial-rewind semantics for an in-flight transfer are designed with the gate.
  - Backtrack's spec scope (`checksum|nvd|globus`) is revisited with the production
    profiles; with column-clear-only rewind the safety question is per-gate
    re-entrancy, not deletion.
  - Trait-based contract construction (ADR-0004 d.1) composes with `excluded_gates`
    unchanged — exclusions overlay whatever contract the traits produce.
  - Plan B's production gate columns are migrations **007+** (F-AUD1 superseded — 006
    is `excluded_gates`).
- **Plan C (API + CLI):**
  - Endpoints wrap ops verbatim; `ControlResult.to_fields()` is the response body;
    exception mapping: `UnknownRunError`/`UnknownGateError`/`UnknownProfileError` → 404,
    `ValueError` → 422, `ProtectedError`/`FabricationError`/`ActiveRunError`/
    `LifecycleError` → 409 (suggested; decided there). `actor` comes from authenticated
    identity — never defaulted (audit's blank-actor `ValueError` is the backstop).
  - `force=True` must be an explicit flag on the CLI (never default); `get_status`
    rendering surfaces `experiment_id` first — it is the operator's reference key.
  - The audit reader (`sluice audit`) orders by `id`, never `ts` (plan 07 §6) — unit
    16's ordered-trail assertion is the in-repo precedent.

---

## ⚑ Human sign-off — all three decided (2026-07-29, decided by the user)

1. **⚑1 — rewind without pointers — APPROVED AS AMENDED BY ADR-0005.** Pointerless
   rewind ships in Plan A, and the amendment strengthens it: rewind is **column-clear
   only** — `reset_gate`/`backtrack` clear satisfaction columns + `completed_at` in one
   transaction and never call `invalidate`; no artifact I/O exists in `control.py`. The
   daemon re-runs cleared steps; the gate contract's §4 re-entrancy requirement covers
   pre-existing artifacts. No `fastforward`; `run_from_step`/`run_from_fresh` stay
   dormant. `Gate.invalidate` becomes a dormant hook nothing invokes; its fate is
   routed to Plan B citing ADR-0005 d.3 (§6).
2. **⚑2 — per-run exclusions as a run column — APPROVED.** `excluded_gates` column
   (migration 006), `exclude_gate`/`include_gate`, the `apply_exclusions` overlay, and
   the bound daemon-side resolution — with the review's attached fixes integrated: F3
   (rewinds iterate the base stamped profile), F2 (`include_gate` clears a now-stale
   `completed_at` in the same transaction), F5 (`get_status` total under
   exclusion-shape corruption).
3. **⚑3 — hold before rewinding — SUPERSEDED BY ADR-0005.** The file-safety rationale
   is void (nothing deletes files). The plan's residual **recommendation** (not a
   safety requirement): rewind ops still refuse on daemon-eligible runs, because an
   in-flight gate's persist can immediately re-stamp the column the op just cleared —
   surprising, not harmful. Recorded in §2 with the rationale honestly downgraded; no
   new flag.
