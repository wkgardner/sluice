# Plan 06 — `reconcile` subsystem (Plan A, subsystem 6 of 9)

**Modules owned:** `sluice/core/reconcile.py` (new; the pure decision engine: given a
run's state and its resolved `Profile`, compute the next gate to run and derive
completion) **plus the unit-0 amendment to `sluice/core/profile.py`** (remove
`Applicability.OPTIONAL` per **ADR-0004** decision 2, with the pre-authorized
`tests/test_profile.py` edits — five structural plus named docstring-only residuals —
and one `tests/test_gate.py` docstring line, all named in unit 0).
**Build order:** db → gate (+ mock gates) → events → context → profile → **reconcile** →
audit → control → daemon
**Tests:** consolidated in `tests/test_reconcile.py` (one file per subsystem); unit 0's
tests live in `tests/test_profile.py` (it amends that subsystem).

Ground truth: **ADR-0004 (binary contracts — read first: a run's contract is binary,
every contract gate mandatory, excluded gates absent; no runtime optional tier; a
contract gate returning SKIPPED quarantines; remediation is exclusion)**; design of
record **§7** (the reconcile loop's pure core; "complete = every required gate of the
run's own `pipeline_version`"; lowest `order` first — read through ADR-0004: required =
in the contract), **§4** (gate selection inputs — `needs`/`satisfied_column`/`order`),
**§5** (state; `completed_at` is a derived cache), **§8** (profiles;
disabled-is-satisfied — the surviving half of the applicability model); errata **C4**
(central — completion never stamped over a skipped gate), **B1** (the
`nvd_auto_run=false` loop-forever bug the disabled/completion split fixes), **B3**
(order is the sole ordering authority), B4/B9 (cited, not reopened); **ADR-0002** (all
failures quarantine; gates never write state — reconcile decides, the daemon persists;
**reconcile itself writes NOTHING**); ADR-0001 (docstrings); ADR-0003 (layering yes,
stdlib-only no); the **DECIDED FACTS** below; the §7 carry-forwards of plans 02, 03, 05
collected in §2 (plan 05 ⚑1's deferral is **discharged as moot** by ADR-0004 — recorded
in §2); `sluice/core/{gate,mock_gates,db,profile}.py` as built.

---

## 1. Scope & non-goals

### Owned

| Thing | Public surface |
|---|---|
| The ADR-0004 vocabulary amendment (unit 0) | `sluice/core/profile.py`: `Applicability` shrinks to `REQUIRED \| DISABLED` |
| The completion predicate (pure) | `is_complete(run_row, profile) -> bool` |
| The next-gate decision (pure) | `next_gate(run_row, profile) -> Gate \| None` |

`reconcile.py` declares `__all__ = ["is_complete", "next_gate"]`; unit 3 asserts set
equality (db unit-7 precedent). Both are **pure module-level functions** — no class, no
record, no state; functional shaping per project preference. Neither takes a connection or
a db path: reconcile reads only what is handed to it, `(run_row, profile)`.

**Allowed imports (closed set): stdlib + `sluice.core.gate` + `sluice.core.profile`.**
`gate` supplies the `Gate` return type; `profile` supplies `Applicability`,
`required_gates`, and (in tests) the shipped reference profiles. **Forbidden by name**
(unit 1): `sqlite3`, `sluice.core.db`, `structlog`, `slack_sdk`, `sluice.gates`/`api`/`cli`,
**and every I/O/clock/entropy module — `time`, `datetime`, `random`, `os`, `socket`,
`urllib`, `http`, `subprocess`.** The structural consequence is the whole point: a module
that cannot name `sqlite3`/`db` cannot read or write the DB, and one that cannot name
`time`/`datetime`/`random`/`socket` cannot read a clock or the network — so "`next_gate` is
a PURE function of state, no hidden read, no write" is guaranteed by what the module can
even name, not by promise. The DECIDED FACT "a hidden read is a defect" becomes a compile
surface, not a review note.

### The DECIDED FACTS (user-decided; locked, not reopened)

1. **`reconcile.next_gate` is PURE — state in, decision out.** No I/O, no DB writes, no
   clock, no network. A hidden read is a defect (enforced structurally, above, and by the
   two signature tests). Reconcile itself writes **nothing** (ADR-0002: gates never write
   state; reconcile decides, the daemon persists).
2. **Gate selection** = the first **contract** gate (not `DISABLED` in the run's profile)
   whose `needs` are satisfied and whose `satisfied_column` is false, **lowest
   `Gate.order` first**. (ADR-0004 supersedes the earlier "required-or-due-optional"
   phrasing: every contract gate is mandatory; there is no optional tier.) `Gate.order` is
   the sole ordering authority; `profile.gates` is already ascending by `order`
   (`validate_gates` in `Profile.__post_init__`), so reconcile iterates it as-is.
3. **Completion is keyed to the run's stamped `pipeline_version`, never the current
   default.** The daemon resolves `load_profile(run["pipeline_version"], registry)` and
   passes that `Profile` in; reconcile consumes the profile it is given and never reads
   `metadata.pipeline_profile` (it structurally cannot — §1 import hygiene). The freeze
   (plan 05 fact 1) is inherited, not re-implemented.
4. **Disabled gates are out of the contract — treated as satisfied by absence** — never
   selected, never blocking completion, and satisfying the `needs` of their dependents
   **by absence** (their column stays `0`; needs-closure unchanged, ADR-0004 decision 2).
5. **The eligibility predicate (`lifecycle='active' AND errored_at IS NULL`) already lives
   in `db.select_active_runs` (`ACTIVE_RUNS_WHERE`).** Reconcile does **not** re-implement
   it and does **not** read `lifecycle`/`errored_at`; the daemon filters, then calls
   reconcile only on eligible runs.
6. **All failures quarantine** (ADR-0002) — **and a contract gate returning SKIPPED
   quarantines too** (ADR-0004 decision 4, extending ADR-0002). No retry, no attempts, no
   `retryable`. Cited, never reopened.

### Explicitly NOT owned (and where it belongs)

- **Persisting anything** — the loop's `persist(result)` step (**subsystem 9, daemon**).
  Reconcile returns a decision; the daemon writes `satisfied_column`/`completed_at`/error
  state in its own short transaction (ADR-0002 d.2; plan 02 §3.2 outcome table, with the
  SKIPPED row resolved by ADR-0004 d.4 — §2).
- **Emitting the wide event** — **subsystem 9** (plan 03 §2 B9 / §7). Reconcile supplies
  the `next_gate is None` completion signal; `emit_run_event(...)` at that point is the
  daemon's, and it fires **after** the `completed_at` write commits (persist-then-emit,
  plan 03 §7). Reconcile cannot emit (no `structlog` import).
- **The per-run exclusion op (remediation-is-exclusion, ADR-0004 decision 3)** —
  **subsystem 8 (control)**; **trait-based contract construction** — **Plan B**.
  Reconcile *enables* exclusion-remediation (stateless: it re-reads state every look, so
  a run resumes past a newly-excluded step automatically) but owns neither the op nor how
  a per-run contract is represented; it consumes whatever `Profile` the daemon resolves.
- **Resolving/assembling the profile registry, stamping `pipeline_version` at detection,
  seeding `metadata.pipeline_profile`** — **subsystem 9** (plan 05 §7). Reconcile is
  handed the already-resolved `Profile`.
- **`resolve_context`, the workdir, running/invalidating/terminating gates** — the daemon
  (subsystem 9) and the gate functions (plan 02/04). Reconcile picks *which* gate; it never
  calls `gate.run`.
- **The eligibility filter and the loop** — the daemon (subsystem 9). Reconcile is one pure
  call inside it.
- **`run_from_step` / `run_from_fresh` targeting, `backtrack`/`fastforward`** — routed to
  the subsystem-8/9 plans with an explicit open question (§2; decided 2026-07-27 as
  reworded). Plan A's `next_gate` does **not** read the pointers; its signature stays
  exactly `(run_row, profile)`.
- **Production gates/profiles, the 6-vs-8-gate/experiment-gate questions (B1/B2)** — Plan B.
  Reconcile is gate-agnostic; the mechanism proven here carries them unchanged.

---

## 2. Errata & carried-forward obligations in scope — decisions taken, no TBDs

Collected from plans 02 §7, 03 §7, 05 §7 (every obligation those plans assigned to
reconcile by name) plus the errata binding reconcile and **ADR-0004**, which decides what
were this plan's three ⚑ flags (all stamped 2026-07-27 — see the sign-off section).

### "A due optional" + the required-needs-optional deadlock rule — **DISCHARGED AS MOOT** (ADR-0004, 2026-07-27, decided by the user).

Plan 05 ⚑1 (APPROVED 2026-07-23) deferred two obligations here, to be decided jointly:
the definition of "a due optional" and whether a REQUIRED gate may depend on an OPTIONAL
gate. **ADR-0004 dissolves both questions**: there is no runtime optional tier — a run's
contract is binary, every gate in it mandatory, excluded gates absent (decision 1); no
task is scheduled-then-skipped. With no optional gates there is nothing to be "due" and
no required-needs-optional shape to deadlock. **Plan 05 ⚑1's deferral is hereby closed —
discharged as moot, not decided** (ADR-0004 consequences, first bullet). The selection
rule is fact 2; completion is `is_complete` = every contract gate's column truthy. This
paragraph is the visible record of the discharge; no unit tests an optional tier, and
unit 0 removes the vocabulary member so one cannot silently return.

### C4 — completion is never stamped over a *skipped* gate. RESOLVED (central; ADR-0004 d.4).

PP stamps `completed_at` unconditionally at the end (`poller.py:1614`, outside any Globus
guard) while the NVD path already guards against exactly this (`poller.py:1127-1134`,
2026-06-22 incident). Sluice's split resolution, entirely within the pure model:

1. **Reconcile's completion reads gate-satisfaction *columns* only:**
   `is_complete = all(<column truthy> for g in required_gates(profile))` — every contract
   gate. A contract gate whose column is `0` **blocks** completion — `is_complete` is
   `False` and `next_gate` keeps returning a runnable gate. So completion can never be
   *derived* over a contract gate that has not genuinely succeeded. This is the reconcile
   half of C4, testable purely (`test_is_complete_false_while_any_contract_column_is_false`,
   unit 1).
2. **A contract gate's column is set truthy ONLY by genuine SUCCESS** (the daemon's
   persist policy, ADR-0004 d.4): the daemon marks `satisfied_column = 1` on
   `GateOutcome.SUCCESS` only. A contract gate returning `SKIPPED` is a contradiction —
   the contract declares the task mandatory for this `pipeline_version`, the gate says it
   does not apply — so the daemon **quarantines** the run (`errored_at`/`error_reason`),
   never `satisfied_column = 1`; a human decides which is wrong. So a skip never
   satisfies a column, and `is_complete` (which reads columns) can never be reached over a
   skip. Together (1)+(2) = C4.

### SKIPPED-persistence policy (inherited from plan 02 §3.2 + plan 03 "gap events"). RESOLVED by ADR-0004 decision 4.

Plan 02 §3.2 left the SKIPPED persist choice to "subsystem 6/9" and warned that persisting
nothing livelocks (`next_gate` returns the same gate forever). Resolution: **ANY contract
gate returning SKIPPED → the daemon quarantines the run** (`error_reason` naming the
gate/contract contradiction + `errored_at`; the column is never set). There is no
optional-skip branch because there are no optional gates. No livelock: the quarantine
write removes the run from eligibility (`ACTIVE_RUNS_WHERE`), so `next_gate` is never
asked again until an operator acts. Recovery is `clear_error` — typically after the
operator resolves the contradiction, either by fixing the gate's inputs or by **excluding
the step from the run's contract** (ADR-0004 decision 3; the exclusion op is subsystem
8's), after which reconcile's statelessness resumes the run past the missing step
automatically. A `DISABLED` gate is never selected, so its `run` is never called — the
skip-from-outside-the-contract case is unreachable. **Persist-then-emit** (plan 03 §7,
binding): the quarantine write commits before `emit_gate_event`/`emit_run_event`; the
daemon emits `gate.finished` with `gate.outcome="skipped"` (plan 03 unit 6 — the "gap
events surfaced"), so the contradiction is observable in the event stream, not just the
error columns.

### B1 — the disabled treatment reconcile *implements* (plan 05 §7; ADR-0004 d.2). RESOLVED.

The profile *records* `required｜disabled` (plan 05 as amended by unit 0); reconcile
*implements* disabled-is-satisfied-by-absence three ways — never selected (§3.2 step a),
never in the completion predicate (excluded from `required_gates`, so never blocks), and
**satisfying the `needs` of dependents by absence** (its column stays `0`; §3.3;
needs-closure unchanged — a disabled gate stays in `profile.gates` so `validate_gates`
still resolves its dependents' `needs`). This is the fix for PP's `nvd_auto_run=false`
loop-forever bug (`poller.py:1229-1237`; subsystem map:477/634): an excluded gate cannot
leave the run incomplete forever. The shipped `REFERENCE_PROFILE_ALPHA_DISABLED` (plan 05
⚑2) is the ready-made substrate: beta in-contract, its needed alpha excluded.
**Vacuous completion** (plan 05 unit 4, carried here): an all-disabled profile has
`required_gates(profile) == ()` → `is_complete` is `all([]) == True` → complete at
detection, nothing runs. A real, legitimate edge reconcile must handle (unit 1/2 tests).

### `pipeline_version` freeze (plan 05 fact 1) + "complete = every required gate of the run's OWN version" (§7). RESOLVED structurally.

Reconcile never reads `run["pipeline_version"]` or `metadata.pipeline_profile`; it decides
against the `Profile` the daemon resolved from the *stamped* version. Import hygiene (§1)
makes reaching the active default impossible. `is_folder_completed`'s all-gates AND-query
(PP `poller_state_db.py:167-184`) is reframed as `required_gates` of the run's own profile
— the run's **contract** — not "all 9 columns" (the §7 reframing, read through ADR-0004).

### `completed_at` is a derived cache, never an input (§5; db.py `ACTIVE_RUNS_WHERE` note). RESOLVED.

Reconcile **re-derives** completion every look from the contract columns; it never reads
the `completed_at` column. A stale `completed_at` (e.g., a contract column cleared by
`reset_gate` after completion) is correctly re-derived as *not complete* — the run
re-enters the pipeline. Pinned by `test_reconcile_re_derives_completion_ignoring_a_stale_completed_at`
(unit 3). This is why `select_active_runs` deliberately omits `completed_at IS NULL`
(db.py): reconcile owns completion, cheaply, from state.

### B3 / B4 / B9 — cited, not reopened.

- **B3** (order is true execution order): enforced upstream by `validate_gates` in
  `Profile.__post_init__` (plan 05 unit 2). Reconcile trusts `profile.gates` is ascending
  by `order` and needs-closed; it adds no cycle check and no re-validation.
- **B4** (ADR-0002 all-failures-quarantine, extended by ADR-0004 d.4 to contract skips):
  reconcile carries no retry surface; a quarantined run has already left eligibility
  (`select_active_runs`) so reconcile is never even called on it.
- **B9** (the canonical per-run wide event): reconcile provides the `next_gate is None`
  signal; the emit is the daemon's (plan 03 §7).

### `run_from_step` / `run_from_fresh` (plan 02 §7 named subsystem 6 co-owner). DECIDED AS REWORDED (2026-07-27, decided by the user).

**Plan A `next_gate` does not read `run_from_step`/`run_from_fresh`; the signature stays
`(run_row, profile)`** — the pointers never enter the pure decision. `backtrack`/
`fastforward` are spec-§9 control ops and §9 places them **in Plan A's control subsystem**
(this plan does *not* assert they are Plan B's); their production scope
(`checksum｜nvd｜globus`) names gates that do not exist in Plan A, so whether Plan A builds
them is a live question this plan routes, explicitly, to the subsystem-8 and subsystem-9
plans: **"does Plan A build `backtrack`/`fastforward`, and if so, who honors the
pointer?"** Binding constraint on that answer: **a pointer-writing op with no honoring
reader is a defect** — subsystem 8 must not ship an op that writes `run_from_step`/
`run_from_fresh` unless the same plan set names the daemon-side reader that honors it
(and clears it in the same transaction — the one-shot rule, plan 02 §7). Recommended
eventual mechanics: implement `backtrack` as `invalidate` + clear the gate's column in
the op's own transaction, so reconcile **re-selects** the rolled-back gate naturally with
zero biasing — the most state-faithful reading of §7 and of "the DB is the plan." If
explicit `next_gate` biasing is ever wanted instead, it is a consciously-tested signature
change, never a quiet read.

---

## 3. The contract, authoritative

### 3.0 Unit 0's amendment (the ADR-0004 vocabulary)

After unit 0, `sluice/core/profile.py` reads:

```python
class Applicability(enum.Enum):
    REQUIRED = "required"   # in the contract — mandatory (ADR-0004 d.1)
    DISABLED = "disabled"   # out of the contract — satisfied by absence (ADR-0004 d.2)
```

`OPTIONAL` is gone; `"disabled"` stays as the word for "out of the contract".
`required_gates` keeps its filter (`is Applicability.REQUIRED`) — with two members that is
now exactly "every contract gate" — and its docstring is rewritten to say so (the
optional-tier sentence deleted). Both shipped reference profiles already use only
`REQUIRED`/`DISABLED` and survive **unchanged** (ADR-0004 consequences). Everything else
in `profile.py` (`Profile`, `build_profile`, `load_profile`, coverage/type checks, the
freeze) is untouched.

### 3.1 Surface (`sluice/core/reconcile.py`)

```python
# sluice/core/reconcile.py — imports: stdlib + sluice.core.gate + sluice.core.profile ONLY

def is_complete(run_row, profile: Profile) -> bool: ...
def next_gate(run_row, profile: Profile) -> Gate | None: ...
```

**`run_row` duck type:** any object supporting `row[column]` **and** `row.keys()` — both
`dict` and `sqlite3.Row` qualify. Because reconcile never touches the DB, tests pass plain
`dict` rows carrying only the gate columns under test; the real `sqlite3.Row` is exercised
once end-to-end (unit 3). **Implementer trap (from plan 03 §3.1, applies verbatim):**
presence checks go through `column in row.keys()`, **never** `column in row` — on a
`sqlite3.Row`, `in` tests *value* membership, not column presence, so the naive spelling
type-checks and lies; and a missing column must be pre-checked so both row types raise the
same `KeyError` (`sqlite3.Row[missing]` raises `IndexError`, not `KeyError`).

### 3.2 `next_gate` — the algorithm (authoritative)

```
def next_gate(run_row, profile):
    _require_gate_columns_present(run_row, profile)    # fail loud FIRST — see below
    for gate in profile.gates:                         # ascending Gate.order (validate_gates)
        (a) if profile.applicability[gate.id] is DISABLED: continue  # out of the contract (fact 4)
        (b) if <gate.satisfied_column truthy in row>: continue       # already satisfied
        (c) if not <every need satisfied>: continue                  # blocked (§3.3)
        return gate                                     # the contract's next task — lowest order wins
    return None                                         # nothing runnable ⇔ complete (theorem, §3.3)
```

No optional branch exists — `next_gate` no longer consults `is_complete` internally (the
old "optional not due" step died with the optional tier, ADR-0004).

**The up-front presence pre-check (DECIDED: option (a) — validate every profile-gate column
present before deciding).** Both `next_gate` and `is_complete` first call a shared
`_require_gate_columns_present(run_row, profile)` that checks **every** `g.satisfied_column`
for `g in profile.gates` is in `row.keys()` and raises `KeyError` naming **all** the missing
columns before any decision is made. *Why option (a), not deferred detection:* `is_complete`
is `all(<col truthy> for g in required_gates(...))`, which **short-circuits** — for a row
`[R1=0, R2's column missing]` the `all()` returns `False` after `R1` and never reads `R2`,
so a drift on `R2` would go undetected on that pass. That would make "fails loud at the
decision boundary" a lie the moment a contract gate before the drifted one is unsatisfied.
The up-front check makes the guarantee real: a registry/schema drift on *any* profile gate
raises immediately, regardless of `all()` short-circuit and regardless of which branch
(selection column vs need column) would otherwise first have read it. Reading columns after
the pre-check: a gate's `satisfied_column` value is read as **truthy/falsy** (the columns are
`INTEGER NOT NULL DEFAULT 0`, so `0`→false, `1`→true; `bool(row[col])` also treats a
defensive `NULL` as false). This truthy read is the same satisfaction rule
`events.build_run_event` applies for its `gates.satisfied`/`gates.unsatisfied` partition
(plan 03 §3.2) — both implement design §4's single completion signal ("a gate is satisfied
iff its `satisfied_column` is truthy in the DB"), so the decision engine and the event
stream can never disagree about which gates are done.

### 3.3 `is_complete` and needs-satisfaction

```
def is_complete(run_row, profile):
    _require_gate_columns_present(run_row, profile)    # same up-front fail-loud as next_gate (§3.2)
    return all(<g.satisfied_column truthy in row> for g in required_gates(profile))
```

`required_gates(profile)` (plan 05, as amended by unit 0) returns the **contract** — the
non-disabled gates — in execution order. A contract gate is satisfied **only** via its
column. All-disabled → `required_gates == ()` → `all([]) == True` (vacuous complete). The
pre-check runs first so the short-circuit above can never hide a drift on a later contract
gate (§3.2).

**Need-satisfaction** (used by step (c)): a need `n` of gate `g` is satisfied iff
`profile.applicability[n] is DISABLED` **OR** `<n's satisfied_column truthy in row>`. The
disabled arm is satisfied-**by-absence** (fact 4 / ADR-0004 d.2): a disabled gate stays in
`profile.gates` with column `0` (needs-closure unchanged), so the need is satisfied from
applicability, not the column. (`validate_gates` guarantees every `n` is a gate in the set
with an applicability entry, so the lookup never misses — plan 05.)

**The central theorem (pinned by unit 3): `next_gate(row, p) is None ⟺ is_complete(row, p)`.**
Proof of "not complete ⟹ `next_gate` returns non-`None`" (no false-complete, no deadlock,
so the daemon's `if gate is None: mark complete` is exactly "every contract gate
satisfied"): not complete ⟹ some contract gate has a false column; let `g*` be the
lowest-`order` such gate. Every gate before `g*` is disabled or satisfied (by `g*`'s
minimality), and every need `n` of `g*` has `order(n) < order(g*)` (validate_gates) — so
each `n` is either DISABLED (satisfied by absence) or a satisfied contract gate ⟹ all of
`g*`'s needs are satisfied ⟹ the loop skips everything before `g*` at (a)/(b) and returns
`g*`. The converse (complete ⟹ every gate skipped at (a) or (b) ⟹ `None`) is immediate. ∎
**Corollary:** `next_gate` returns exactly the lowest-order unsatisfied contract gate, and
that gate's needs are always already satisfied — under `validate_gates`' invariants the
needs guard (c) never rejects the minimal candidate. It stays in the algorithm because it
is the spec's stated selection rule (§7) and because it is where the **disabled-need arm
lives**: a wrong, column-only needs check would block every dependent of an excluded gate
(returning `None` while incomplete — a deadlock the disabled-variant tests and the theorem
both catch). The theorem is the reason `is_complete` is exposed as its own tested function
rather than folded away: the equivalence becomes a *property* the suite verifies, not an
implicit coupling.

### 3.4 What reconcile reads, exhaustively

From `run_row`: **only** the `satisfied_column` of each gate in `profile.gates` — their
*presence* (the up-front pre-check, §3.2) and their *truthiness* (via steps (b), (c), and
`required_gates`). Never `lifecycle`, `errored_at`, `completed_at`, `pipeline_version`,
`run_from_step`, `run_from_fresh`, or any other column. From `profile`: `gates` (ordered),
`applicability`, and `required_gates(profile)`. Nothing else — no clock, no config, no DB.
Pinned by `test_next_gate_reads_only_the_profile_gate_columns` and the two signature tests.

---

## 4. Units, in dependency order (4 units: 0–3)

Every unit is one RED → GREEN → REFACTOR cycle from the named failing test — **except unit
3**, which adds no production code and is **green-on-arrival by design** (the plan-02 unit-9
carve-out): the exhaustive equivalence theorem + real-`sqlite3.Row` integration pin the
composed behavior of units 0–2; the RED start is the `__all__` equality test. `__all__` of
`reconcile.py` grows as names land (unit 1 declares `["is_complete"]`, unit 2 extends with
`"next_gate"`). Units 1–3's tests live in `tests/test_reconcile.py`; unit 0's in
`tests/test_profile.py`. Helpers at the top of `tests/test_reconcile.py` (defined as units
need them):
- `make_gate(id, order, *, needs=(), satisfied_column=None) -> Gate` — a no-op `run`
  returning `GateResult(SUCCESS)`, no-op `invalidate`, `satisfied_column` defaulting to
  `f"{id}_done"`. Lets tests build arbitrary DAGs whose columns need **not** exist in any
  schema (reconcile reads them off the `dict` row — a benefit of its purity).
- `make_profile(entries) -> Profile` — thin `build_profile("test-v", entries)` over
  `(gate, Applicability)` pairs.
- Rows are plain `dict`s except unit 3, which uses a real `sqlite3.Row` from `get_run`.

Every unit's code follows ADR-0001 (Google docstrings, one why-paragraph citing §/errata,
constraints not justifications).

---

### Unit 0 — remove `Applicability.OPTIONAL` from the committed profile subsystem (ADR-0004 d.2)

The only unit that edits `sluice/core/profile.py`, `tests/test_profile.py`, and one
docstring line of `tests/test_gate.py` — **all edits pre-authorized and named below, and
no others** (the plan-02 unit-8 precedent for amending a shipped subsystem). Cycle
discipline, in order: (a) rewrite the vocabulary test to the binary equality (below) —
**RED** against the shipped three-member enum; (b) remove the `OPTIONAL` member from
`Applicability` and sweep for optional-tier references — **grep for
`optional|three|fourth|tier` across BOTH `sluice/core/profile.py` and
`tests/test_profile.py`** (a grep for `optional` alone misses the *counting* references —
"closed at three", "no fourth tier"); the vocabulary test goes GREEN, and the four other
named tests go **RED by design** (each constructs a profile with `Applicability.OPTIONAL`
— now an `AttributeError`, or a `KeyError` for the name-indexed `Applicability[...]`
parametrize cases, edits 3–4); (c) in the same cycle, update those four in place per the
notes below and apply the named docstring-only residual touches. Full suite green at the
end of the cycle with exactly the five structural `tests/test_profile.py` edits, the six
named docstring-only touches, and zero edits anywhere else — `mock_gates.py`, `db.py`,
both `REFERENCE_PROFILE`s, and every other test are untouched (the shipped profiles
already use only `REQUIRED`/`DISABLED`, ADR-0004 consequences). `tests/test_gate.py` is
untouched **except** the single docstring line named below.

**The five pre-authorized structural edits (`tests/test_profile.py`)**
1. `test_applicability_vocabulary_is_the_spec_three` → renamed
   **`test_applicability_vocabulary_is_the_binary_two`**:
   `{a.name: a.value for a in Applicability} == {"REQUIRED": "required", "DISABLED":
   "disabled"}`. Equality, not subset — the closed two-member set **is** ADR-0004
   decision 2's surface; a third tier (optional, retry, anything) can only return via a
   conscious edit of this test plus a new ADR. Docstring cites ADR-0004.
2. `test_build_profile_splits_pairs_into_gates_and_applicability` — the
   `(MOCK_BETA, Applicability.OPTIONAL)` pair becomes `(MOCK_BETA, Applicability.DISABLED)`
   (preserving the two-distinct-values shape the test exists for); the asserted dict
   updates to match.
3. `test_required_gates_returns_only_required_in_order` — the `"optional-excluded"`
   parameter case is **deleted**; the remaining cases (both `REQUIRED`; alpha `DISABLED` /
   beta `REQUIRED`) already pin "required_gates = the contract, disabled excluded".
4. `test_required_gates_is_empty_for_an_all_optional_profile` → renamed
   **`test_required_gates_is_empty_for_an_all_disabled_profile`**: the `"all-optional"`
   parameter is deleted; the all-`DISABLED` leg (the vacuous-completion edge) survives
   as the single case.
5. `test_load_profile_resolves_by_version_string` — the `other-v1` registry entry,
   currently built with two `OPTIONAL` entries, is rebuilt with
   `[(MOCK_ALPHA, Applicability.DISABLED), (MOCK_BETA, Applicability.REQUIRED)]` (any
   valid two-member shape works; the test is about version resolution, not applicability).

**The six pre-authorized docstring-only residual touches** — each survives the structural
edits above but becomes **false** once `OPTIONAL` is removed, and the zero-other-edits
rule would otherwise forbid fixing it mid-build:
- `tests/test_profile.py:6` (module docstring) — "vocabulary is deliberately closed at
  three … no fourth applicability tier" → closed at **two** / no **third** tier, citing
  ADR-0004 (a *counting* reference; a grep for `optional` misses it).
- `tests/test_profile.py:513` (`test_required_gates_returns_only_required_in_order`
  docstring) — describes the now-impossible OPTIONAL-inclusion regression ("an
  implementation that includes OPTIONAL gates …"); the sentence dies with the tier.
- `tests/test_profile.py:544-556` (the edit-4 test's body docstring) — edit 4 renames the
  test, but its docstring still preaches "a deliberately all-optional profile"; rewritten
  to the all-disabled/empty-contract framing.
- `tests/test_profile.py:743` (comment in
  `test_reference_profile_covers_the_two_mock_gates_both_required`) — "an
  optional/disabled slip here" → "a disabled slip here".
- `tests/test_profile.py:762` (`test_reference_disabled_variant_keeps_the_disabled_gate_in_the_set`
  docstring) — the "not OPTIONAL (which would not prove …)" aside; rewritten without the
  dead member.
- `tests/test_gate.py:155` (`test_gate_contract_fields_are_exactly_the_spec_ten`
  docstring) — "required/optional/disabled lives in the profile" → "required/disabled
  lives in the profile (ADR-0004)". **Updated, not left historical**: a comment lying
  about the live vocabulary is exactly the doc-drift this project kills; this is the only
  `tests/test_gate.py` touch, docstring-only, no assertion changes.

**Production edits:** delete the `OPTIONAL` member (and its comment line); rewrite the
`Applicability` docstring ("exactly these two members … ADR-0004: a run's contract is
binary — in and mandatory, or out and absent"); rewrite `required_gates`' docstring (the
"OPTIONAL runs when due but never blocks" sentence dies; the function now returns "the
contract — every non-disabled gate, in execution order"); no signature or filter-logic
change is required (`is Applicability.REQUIRED` is now exactly "in the contract").

---

### Unit 1 — module bootstrap, import hygiene, `is_complete`

RED: `tests/test_reconcile.py` fails at `import sluice.core.reconcile` (module absent).
Declares `__all__ = ["is_complete"]`.

**Tests**
- `test_reconcile_is_pure_by_import` — ast-parse `sluice/core/reconcile.py`: every
  `Import`/`ImportFrom` root is in `sys.stdlib_module_names` or is `sluice`; every
  `sluice.*` import is exactly `sluice.core.gate` or `sluice.core.profile`; and — asserted
  **by name** so the message teaches — **none** of `sqlite3`, `sluice.core.db`, `structlog`,
  `slack_sdk`, `time`, `datetime`, `random`, `os`, `socket`, `urllib`, `http`, `subprocess`,
  `sluice.gates`, `sluice.api`, `sluice.cli` appears. (Technique of
  `test_profile_is_core_clean_by_import`; `tests/test_dependency_arrows.py` auto-discovers
  the new module — no edit there.) **This one test carries DECIDED FACT 1 structurally:** a
  module that cannot name a SQL client, a clock, or a socket cannot do a hidden read, a write,
  or reach the network — purity is a property of the import surface, not a promise.
- `test_is_complete_signature_is_row_and_profile_only` —
  `list(inspect.signature(is_complete).parameters) == ["run_row", "profile"]`. Purity as a
  signature fact: no connection, no db path, no clock parameter — reconcile is a pure
  function of `(run_row, profile)` (fact 1). Editing this signature is a conscious purity
  change.
- `test_is_complete_true_iff_all_contract_columns_truthy` — parametrized over
  `REFERENCE_PROFILE` (both in-contract): `{alpha 0, beta 0}`→`False`; `{alpha 1, beta 0}`→
  `False`; `{alpha 0, beta 1}`→`False` (order-inconsistent but state-legal — total function,
  never raises); `{alpha 1, beta 1}`→`True`. The §7/`is_folder_completed` reframing: complete
  ⟺ every contract column truthy.
- `test_is_complete_false_while_any_contract_column_is_false` — the **C4 reconcile half**:
  `REFERENCE_PROFILE`, `{alpha 1, beta 0}` → `False`; even one unsatisfied contract column
  blocks completion, so completion can never be *derived* over a not-genuinely-done contract
  gate (docstring cites C4 / ADR-0004 d.4 / PP `poller.py:1614` vs the guard at `:1127-1134`).
- `test_is_complete_ignores_disabled_gates` — the **excluded-never-blocks** test.
  `REFERENCE_PROFILE_ALPHA_DISABLED` with `{mock_alpha_done 0, mock_beta_done 1}` → `True`:
  alpha is out of the contract (column `0`, absent from `required_gates`), so it does not
  block; only beta counts. With `{mock_alpha_done 0, mock_beta_done 0}` → `False` (beta,
  the contract, is unsatisfied).
- `test_is_complete_is_vacuously_true_for_an_all_disabled_profile` —
  `make_profile([(A, DISABLED), (B, DISABLED)])` with `{a 0, b 0}` → `True`.
  `required_gates == ()` → `all([]) == True` → an empty contract is complete at detection
  (plan 05 unit 4 edge as amended by unit 0, carried here as a fact reconcile must handle).
- `test_is_complete_reads_only_contract_gate_columns_and_ignores_completed_at` —
  `REFERENCE_PROFILE` over a **minimal** row `{"mock_alpha_done": 1, "mock_beta_done": 1}`
  (no `lifecycle`/`errored_at`/`completed_at`/`pipeline_version`) → `True`, proving it reads
  only gate columns; and a row `{alpha 1, beta 0, "completed_at": "…stale…"}` → `False`,
  proving completion is **re-derived** from columns and the `completed_at` cache is ignored
  (§5; db.py `ACTIVE_RUNS_WHERE` note; the `reset_gate`-after-complete scenario).
- `test_is_complete_fails_loud_on_a_missing_gate_column` — parametrized over a `dict` row
  and a `sqlite3.Row`, and over **two profile shapes so the `all()` short-circuit cannot
  hide the drift** (option (a), §3.2):
  1. **single contract gate** — `make_profile([(make_gate("g", 1, satisfied_column="ghost_done"),
     REQUIRED)])`, row lacking `ghost_done`;
  2. **the short-circuit trap** — `make_profile([(R1, REQUIRED), (make_gate("r2", 2,
     satisfied_column="ghost_done"), REQUIRED)])` with a row `{R1_col: 0}` (R1 unsatisfied,
     `ghost_done` **absent**). A naive `all(...)` would return `False` after R1 and never read
     `r2`, silently missing the drift; the up-front `_require_gate_columns_present` makes it
     raise anyway.

  All cases raise `KeyError` naming the missing column (`"ghost_done"`), **uniform** across
  `dict` and `sqlite3.Row` (the §3.1 trap). A registry/schema mismatch fails loud at the
  decision boundary, never a silent "not complete" (guardrail §8: no silent fallback).

---

### Unit 2 — `next_gate` (the selection engine — the most-tested unit)

Extends `__all__` with `"next_gate"`. Behavior per §3.2–3.3. This is spec §7's "most-tested
unit"; the matrix below exercises every branch (a)–(c), ordering, needs-blocking, the
disabled treatment, selection identity, the vacuous edge, purity, scope, and fail-loud.

**Tests**
- `test_next_gate_signature_is_row_and_profile_only` —
  `list(inspect.signature(next_gate).parameters) == ["run_row", "profile"]` (purity as a
  signature fact; no `run_from_step`/`run_from_fresh` read — §2 pointer decision; fact 1).
- `test_next_gate_returns_lowest_order_runnable_contract_gate` — `REFERENCE_PROFILE`,
  `{alpha 0, beta 0}` → `is MOCK_ALPHA` **and `is not MOCK_BETA`** though beta's column is
  also false — beta is not returned (its need on alpha is unmet); the two assertions
  together pin need-blocking and lowest-order-first.
- `test_next_gate_advances_when_the_predecessor_is_satisfied` — `REFERENCE_PROFILE`,
  `{alpha 1, beta 0}` → `is MOCK_BETA` (need alpha now truthy). Combined with the previous
  test, proves beta was held **by state**, not position: the same profile yields a
  different gate purely because the row changed.
- `test_next_gate_returns_none_when_all_contract_gates_satisfied` — `REFERENCE_PROFILE`,
  `{alpha 1, beta 1}` → `None`. The completion signal the daemon stamps on.
- `test_next_gate_selection_identity_lowest_wins_and_need_blocked_not_returned` — **the
  selection-*identity* test the None-only theorem cannot catch (a wrong-but-non-`None`
  choice would pass the theorem).** One 3-gate contract, all `REQUIRED`:
  `A(order 1, needs=())`, `B(order 2, needs=(A,))`, `C(order 3, needs=())`. Two sub-cases
  pinning *which* gate is returned:
  1. row all `0` → `next_gate(...) is A` and `is not C`: **two** gates are runnable (A and
     C both have empty needs and false columns) and the lowest-`order` one wins — an
     implementation returning any runnable gate (or the last one) fails here;
  2. row `{a 1, b 0, c 0}` → `next_gate(...) is B` and `is not C`: B's need is now
     satisfied and B (order 2) beats the also-runnable C (order 3) — the need-blocked gate
     from sub-case 1 is returned the moment its need clears, at its rightful position.
- `test_next_gate_treats_a_disabled_gate_as_out_of_the_contract` — **the
  satisfied-by-absence test (fact 4; ADR-0004 d.2; plan 05 ⚑2 exemplar).**
  `REFERENCE_PROFILE_ALPHA_DISABLED`, `{mock_alpha_done 0, mock_beta_done 0}` →
  `is MOCK_BETA`: alpha is **never selected** though its column is `0` (step (a)), and
  beta is **unblocked by alpha's absence** though alpha's column stays `0` (§3.3
  disabled-need arm — this is the test that catches a wrong, column-only needs check).
  Second assertion: `{mock_alpha_done 0, mock_beta_done 1}` → `None` (the contract — beta —
  is satisfied; alpha excluded and never selected even as the last column-false gate).
  This is the fix for PP's `nvd_auto_run=false` loop-forever (`poller.py:1229-1237`).
- `test_next_gate_is_none_for_an_all_disabled_profile` — `make_profile([(A, DISABLED),
  (B, DISABLED)])`, `{a 0, b 0}` → `None`. An empty contract has nothing to run and is
  complete at detection (documented edge, matches unit 1's vacuous case).
- `test_next_gate_reads_only_the_profile_gate_columns` — **the purity/scope guard.**
  `REFERENCE_PROFILE` over a minimal row `{"mock_alpha_done": 0, "mock_beta_done": 0}` with
  **no other keys** → `is MOCK_ALPHA`. Proves `next_gate` touches only the gates'
  satisfaction columns — no hidden read of `lifecycle`/`errored_at`/`completed_at`/
  `pipeline_version`/`run_from_step` (§3.4; fact 1).
- `test_next_gate_ignores_lifecycle_and_error_state` — **eligibility-not-reimplemented
  (fact 5).** `REFERENCE_PROFILE`, `{alpha 0, beta 0, "lifecycle": "held", "errored_at":
  "…", "error_reason": "x", "completed_at": "…"}` → `is MOCK_ALPHA`, identical to the active
  row. Reconcile does not re-implement `ACTIVE_RUNS_WHERE`; the daemon filters via
  `select_active_runs` and calls reconcile only on eligible runs. Guards against a
  "helpful" eligibility check creeping into the pure decision.
- `test_next_gate_fails_loud_on_a_missing_gate_column` — parametrized over `dict` and
  `sqlite3.Row`, and over the same two profile shapes as unit 1 (single contract gate; the
  `[R1=0, R2 column missing]` short-circuit trap), plus a case where the absent column is a
  *need*'s column (a selectable gate whose need names a drifted column). Each raises
  `KeyError` naming the missing column, uniformly — the up-front pre-check (§3.2) covers every
  read path (selection column, need column) in one place. No silent skip.

---

### Unit 3 — the equivalence theorem + real-`sqlite3.Row` integration + surface (green-on-arrival)

Adds no production code except the final `__all__`; the RED start is the surface equality
test (units 1–2 grew `__all__` as names landed). These pin the composed behavior of units
0–2 against the real db + profile substrate.

**Tests**
- `test_reconcile_exports_exactly_the_documented_surface` —
  `set(sluice.core.reconcile.__all__) == {"is_complete", "next_gate"}` (scope guard).
- `test_next_gate_none_iff_is_complete_across_the_state_space` — **the central theorem
  (§3.3), exhaustive.** Parametrized over **every** column assignment in `{0,1}^n` for a
  set of profiles that exercise all contract shapes: `REFERENCE_PROFILE` (two-gate
  contract), `REFERENCE_PROFILE_ALPHA_DISABLED` (excluded need), a 3-gate contract with a
  **disabled middle gate** (`make_profile([(A, REQUIRED), (M, DISABLED), (Z, REQUIRED)])`
  with `Z.needs=(A.id, M.id)` — absence-satisfaction mid-DAG), and an all-disabled
  profile — assert `(next_gate(row, p) is None) == is_complete(row, p)` for every
  `(profile, row)`. One property proves, together: C4 (never `None` while a contract
  column is false), excluded-never-blocks, vacuous completion, and **no deadlock / no
  false-complete** — including order-inconsistent rows (robustness). (It pins only
  None-ness; *which* non-`None` gate is returned is pinned by the identity tests in
  unit 2.)
- `test_reconcile_drives_the_reference_profile_to_completion_off_real_rows` — **the
  load-bearing seam.** `initialize_database(tmp)`; `insert_run(conn, "r1",
  pipeline_version="reference-v1")`; `p = load_profile(get_run(conn, "r1")["pipeline_version"],
  REFERENCE_PROFILES)` — `is REFERENCE_PROFILE`. Loop, playing the daemon's persist:
  `next_gate(get_run(...), p) is MOCK_ALPHA` → `update_run(conn, "r1", {"mock_alpha_done": 1})`;
  `next_gate(...) is MOCK_BETA` → `update_run(..., {"mock_beta_done": 1})`;
  `next_gate(...) is None` and `is_complete(get_run(...), p)`. The whole decision runs off a
  real `sqlite3.Row` against the real schema's `mock_alpha_done`/`mock_beta_done` (migration
  005) — a drift between reconcile's column reads and the schema fails here, not in the daemon.
- `test_reconcile_disabled_variant_completes_without_running_the_excluded_gate` — **the
  full-loop satisfied-by-absence proof off a shipped profile.** `insert_run(conn, "r2",
  pipeline_version="reference-v1-alpha-disabled")`; resolve → `REFERENCE_PROFILE_ALPHA_DISABLED`;
  `next_gate(...) is MOCK_BETA` (alpha out of the contract, never selected; beta unblocked
  by absence); `update_run(..., {"mock_beta_done": 1})`; `next_gate(...) is None`,
  `is_complete(...)` — with `mock_alpha_done` **still `0`** on the final row. The run
  completes with the excluded gate never run and its column never set — this is also the
  pure-decision half of **remediation-is-exclusion** (ADR-0004 d.3): a run whose contract
  no longer contains a step resumes past it with no daemon restart and no special-casing
  (the daemon plan reuses this).
- `test_reconcile_re_derives_completion_ignoring_a_stale_completed_at` — **the C4-adjacent
  reset scenario.** Real row driven to `{alpha 1, beta 1, completed_at set}` → `is_complete`,
  `next_gate is None`. Then `update_run(conn, "r1", {"mock_alpha_done": 0})` (simulating
  `reset_gate`) **without** clearing `completed_at`; re-`get_run`: `is_complete` is `False`
  and `next_gate is MOCK_ALPHA` — reconcile re-derives from columns and ignores the stale
  cache, so the run correctly re-enters the pipeline (§5; db.py cache note). (The test imports
  `sluice.core.db`; the module under test still does not — unit 1 stands.)

---

## 5. Invariant checks

| Invariant | How this subsystem could violate it | Guarding test |
|---|---|---|
| **`next_gate`/`is_complete` are PURE functions of `(run_row, profile)`; no I/O, no DB, no clock/network — a hidden read is a defect** (fact 1) | reading a clock/socket/DB; a connection/db-path param | `test_reconcile_is_pure_by_import` (unit 1, structural — no `sqlite3`/`db`/`time`/`datetime`/`random`/`socket`…) + the two signature tests + `test_next_gate_reads_only_the_profile_gate_columns` (unit 2) |
| **Reconcile writes NOTHING; the daemon is the sole writer** (ADR-0002) | reconcile mutating the row or opening a write | structural (no `sqlite3`/`db` import, unit 1) + signatures take no connection; reconcile returns a decision only |
| **Contracts are binary — no runtime optional tier can return** (ADR-0004 d.1/2) | a third `Applicability` member creeping back; an optional-style branch in `next_gate` | `test_applicability_vocabulary_is_the_binary_two` (unit 0, closed equality) — reinstating a tier requires editing it plus a new ADR |
| **Completion never stamped over a *skipped* gate** (C4; ADR-0004 d.4) | `is_complete` counting a not-satisfied contract gate; a skip setting the column | `test_is_complete_false_while_any_contract_column_is_false` (unit 1) + the equivalence theorem (unit 3) + the SKIPPED→quarantine persist policy carried to subsystem 9 (§2) |
| **Disabled = out of the contract — never selected, never blocks, satisfies dependents' needs by absence** (fact 4; ADR-0004 d.2; B1 loop-forever fix) | selecting a disabled gate; a column-only needs check deadlocking dependents of an excluded gate | `test_next_gate_treats_a_disabled_gate_as_out_of_the_contract` (unit 2), `test_reconcile_disabled_variant_completes_without_running_the_excluded_gate` (unit 3), the disabled-middle-gate profile in the theorem (unit 3) |
| **`next_gate is None ⟺ complete` (no false-complete, no deadlock); and the right gate is chosen** (§7) | a blocked contract gate returning `None`; a wrong-but-non-`None` selection | `test_next_gate_none_iff_is_complete_across_the_state_space` (unit 3) + `test_next_gate_selection_identity_lowest_wins_and_need_blocked_not_returned` (unit 2) |
| **`Gate.order` is the sole ordering authority; lowest first** (fact 2/B3) | selecting by list/column position | `test_next_gate_returns_lowest_order_runnable_contract_gate` + the identity test (unit 2); relies on `validate_gates` sorting (plan 05 unit 2) |
| **Completion keyed to the run's OWN stamped `pipeline_version`, never the default** (fact 3; freeze) | reading `metadata.pipeline_profile` or `run["pipeline_version"]` | structural (no `db` import, unit 1) + `test_reconcile_drives_the_reference_profile_to_completion_off_real_rows` (unit 3, daemon resolves from the stamped version) |
| **`completed_at` is a derived cache, never an input** (§5) | reading `completed_at` instead of re-deriving | `test_is_complete_…_ignores_completed_at` (unit 1), `test_reconcile_re_derives_completion_ignoring_a_stale_completed_at` (unit 3) |
| **Eligibility (`lifecycle='active' AND errored_at IS NULL`) NOT re-implemented** (fact 5) | reconcile reading `lifecycle`/`errored_at` | `test_next_gate_ignores_lifecycle_and_error_state` (unit 2) |
| **No silent fallback; fail loud at the decision boundary on a registry/schema drift** (guardrail §8; option (a)) | a missing column read as "not satisfied"; an `all()` short-circuit hiding a later drift | `test_is_complete_fails_loud_on_a_missing_gate_column` (unit 1, incl. the multi-gate short-circuit trap), `test_next_gate_fails_loud_on_a_missing_gate_column` (unit 2) |
| **All failures quarantine; contract skips quarantine; no retry surface** (B4/ADR-0002/ADR-0004 d.4) | a retry/attempts read; an optional-skip branch | inherited: reconcile is never called on a quarantined run (`select_active_runs`); no retry field exists (plan 02/05); no optional tier exists (unit 0) |
| **`gates → core`; core imports with gates absent** | importing a gates/api/cli module | `test_reconcile_is_pure_by_import` (unit 1); `tests/test_dependency_arrows.py` auto-covers the new module |
| **Amending a shipped subsystem is surgical — and leaves no lying docstring behind** (plan-02 unit-8 precedent; ADR-0004) | unit 0 editing beyond the named edits / touching the shipped profiles / leaving a docstring that misstates the live vocabulary | the unit-0 choreography: full suite green with exactly the five named structural `tests/test_profile.py` edits **plus the six named docstring-only residual touches** (incl. `tests/test_gate.py:155`) and nothing else; the `optional\|three\|fourth\|tier` sweep; both `REFERENCE_PROFILE`s byte-identical |

---

## 6. Done criteria

1. `pixi run test` fully green: every pre-existing test (`tests/test_dependency_arrows.py`
   now auto-covering `sluice.core.reconcile`) plus the new `tests/test_reconcile.py`, with
   exactly the pre-authorized unit-0 edits (five structural `tests/test_profile.py` edits +
   six named docstring-only touches, incl. the single `tests/test_gate.py:155` line) and no
   others.
2. **ADR-0004 executed in code:** `Applicability` is exactly `{REQUIRED: "required",
   DISABLED: "disabled"}`; `OPTIONAL` and every optional-tier reference — including the
   counting ones ("three", "fourth tier") — are gone from `sluice/core/profile.py` and
   `tests/test_profile.py` (the `optional|three|fourth|tier` sweep); both shipped
   reference profiles are unchanged; `required_gates` returns the contract (every
   non-disabled gate) in execution order.
3. `sluice/core/reconcile.py` imports only stdlib + `sluice.core.gate` +
   `sluice.core.profile`; `sqlite3`, `sluice.core.db`, `structlog`, `slack_sdk`, `time`,
   `datetime`, `random`, `os`, `socket`, and any `sluice.gates`/`api`/`cli` appear nowhere —
   so it structurally cannot write, read a clock, or reach the DB/network (fact 1).
4. `is_complete(run_row, profile)` and `next_gate(run_row, profile)` take exactly
   `(run_row, profile)` — no connection, no db path — and read from the row **only** the
   `satisfied_column` of the profile's gates (§3.4), failing loud up front on any missing
   profile-gate column (option (a); no `all()` short-circuit can hide a drift).
5. `is_complete` = every contract gate's column truthy (vacuously `True` for an
   all-disabled/empty contract); it ignores disabled gates and the `completed_at` cache,
   re-deriving every look.
6. `next_gate` returns the lowest-`order` contract gate with needs satisfied and column
   false (the right gate, pinned by identity); treats a disabled gate as out of the
   contract (never selected, unblocks its dependents by absence); and `next_gate is None
   ⟺ is_complete` across the state space (the theorem), including order-inconsistent rows.
7. Both shipped profiles drive to completion off real `sqlite3.Row`s against the real
   schema (`reference-v1` runs both gates; `reference-v1-alpha-disabled` completes with
   the excluded gate never run, `mock_alpha_done` still `0`); a stale `completed_at` is
   correctly re-derived as not-complete.
8. Downstream can start: subsystem 9 (daemon) gets `next_gate`/`is_complete`, the
   outcome-persist policy (SKIPPED → quarantine, ADR-0004 d.4), the `next_gate is None`
   completion-stamp + persist-then-emit + B9 emission point, and the routed
   `backtrack`/`fastforward` question (§2); subsystem 8 (control) gets the exclusion-op
   obligation (ADR-0004 d.3) and the same routed question; Plan B gets trait-based
   contract construction and the gate-agnostic mechanism unchanged.

---

## 7. Carry-forwards (named, so no plan rediscovers them)

- **Subsystem 9 (daemon) — the main consumer.** Per eligible run (already filtered by
  `select_active_runs` — fact 5): `p = load_profile(run["pipeline_version"], registry)`
  (the freeze; resolve from the *stamped* version); `gate = reconcile.next_gate(run, p)`.
  - **`gate is None` ⟹ complete:** write `completed_at = now_iso()` in a short transaction,
    then `emit_run_event(...)` outcome `completed` (plan 03 §2 B9 / §7 —
    **persist-then-emit**; the write commits before the emit). Completion is `next_gate is
    None`, which is exactly `is_complete` (the theorem), so C4 holds: a run never gets
    `completed_at` while a contract column is false.
  - **`gate` returned ⟹ run it and persist per the plan-02 §3.2 outcome table**, with the
    SKIPPED row resolved by ADR-0004 d.4: SUCCESS → `satisfied_column = 1`; FAILURE →
    quarantine (`error_reason` = result.reason, `errored_at`); **SKIPPED → quarantine**
    (`error_reason` naming the gate/contract contradiction, `errored_at`; **the column is
    never set**); HELD → nothing. Emit `gate.finished` (incl. `outcome="skipped"`) after
    each persist commits; emit `run.finished` outcome `quarantined` with
    `extra={"failed.gate": gate.id}` at the FAILURE/SKIPPED persist (plan 03 ⚑1).
  - **Never call `next_gate` on an ineligible run** — eligibility is the daemon's filter,
    reconcile ignores `lifecycle`/`errored_at` (fact 5). A run that quarantines has already
    left `select_active_runs`.
  - **Routed question (decided 2026-07-27, as reworded): "does Plan A build
    `backtrack`/`fastforward`, and if so, who honors the pointer?"** Spec §9 places the
    ops in Plan A's control subsystem, but their scope names Plan B gates. The subsystem-9
    plan must answer its half (the honoring reader) jointly with subsystem 8's; **a
    pointer-writing op with no honoring reader is a defect**, and if honored, the pointer
    is cleared in the same transaction (one-shot rule, plan 02 §7). `next_gate` never
    reads the pointers; any biasing would be a consciously-tested signature change.
- **Subsystem 8 (control):**
  - **The per-run exclusion op (ADR-0004 decision 3)** — remediation is exclusion, not
    skipping: the operator removes the problem step from a stuck run's contract, and
    because reconcile is stateless and re-reads state every look, the run resumes past the
    missing step automatically (proven in miniature by unit 3's disabled-variant test).
    How a per-run contract exclusion is *represented* (a per-run profile, a contract
    column, a variant `pipeline_version`) is subsystem 8/Plan B's design question —
    reconcile consumes whatever `Profile` the daemon resolves and needs no change.
  - The same routed `backtrack`/`fastforward` question as subsystem 9, jointly (§2): do
    not ship a pointer-writing op without its honoring reader.
  - `get_status` may reuse `is_complete(run_row, profile)` for a read-only completion
    field in its snapshot (as events' `run_outcome` is reused) — pure, total, no I/O.
  - `reset_gate` clears a satisfied column; reconcile re-derives the run as not-complete
    on the next look even if `completed_at` is stale (unit 3). `set_gate` marking a
    contract gate satisfied is the sanctioned operator override of the completion input,
    with the B2 fabrication guard (plan 02 §2 B2) — out of reconcile's scope.
- **Plan B:**
  - **Trait-based contract construction (ADR-0004 d.1/d.3):** task definitions' *traits*
    are evaluated against the run when the contract is established, yielding exactly the
    tasks that will run — Plan B's profile story. The former "optional gate" use cases
    resolve at contract-construction time: a step either enters a run's contract
    (mandatory) or does not (absent). The B7 `reap` gate follows the same rule — in the
    contract and mandatory (ordered after the last consumer of the raw files), or
    excluded; there is no opportunistic tier.
  - The mechanism is gate-agnostic — production gates and profiles inherit
    `next_gate`/`is_complete` with zero change; the B1 config switches
    (`nvd_auto_run` etc.) are expressed as `DISABLED` in a profile (plan 05 §7),
    i.e., exclusion from the contract.
  - The globus final-gate C4 guard (errata C4; PP `poller.py:1614` vs the NVD guard
    `:1127-1134`) is satisfied structurally: a contract globus gate that skips
    quarantines rather than setting its column (ADR-0004 d.4), so completion is never
    derived over a skipped transfer.

---

## ⚑ Human sign-off — all three decided (2026-07-27, decided by the user)

1. **⚑1 — "a due optional" + the required-needs-optional deadlock rule — SUPERSEDED BY
   ADR-0004.** The user rejected the optional-tier framing outright ("anything that is
   optional is not included in the contract; optional steps are not still run and then
   skipped once all the mandatory completes"). There is no runtime optional tier: a run's
   contract is binary, every contract gate mandatory, excluded gates absent. The "due
   optional" definition and the deadlock rule are **discharged as moot** (§2), which
   visibly closes plan 05 ⚑1's deferral. Unit 0 removes `Applicability.OPTIONAL` from the
   shipped profile subsystem (ADR-0004 decision 2).
2. **⚑2 — SKIPPED persistence — RESOLVED, stronger than proposed (ADR-0004 decision 4).**
   **Any** contract gate returning SKIPPED quarantines the run — there is no optional-skip
   branch because there are no optional gates. "Skip me" from inside the contract
   contradicts the contract; a human decides which is wrong (`clear_error` + optionally
   the subsystem-8 exclusion op, ADR-0004 d.3). The column is never set; C4 closes.
3. **⚑3 — `run_from_step`/`run_from_fresh` — APPROVED AS REWORDED.** `backtrack`/
   `fastforward` are deferred, but **not** on the claim that they are outside Plan A —
   spec §9 places them in Plan A's control subsystem. The explicit question "does Plan A
   build `backtrack`/`fastforward`, and if so, who honors the pointer?" is routed to the
   subsystem-8/9 plans (§2, §7), with the binding constraint that a pointer-writing op
   with no honoring reader is a defect. `next_gate`'s signature stays `(run_row, profile)`
   and never reads the pointers.
