# Plan 14 — predicate deepening (locality refactor)

Status: approved by Will 2026-08-01, not yet built.

Source: the architecture review of 2026-07-31 (`$TMPDIR/architecture-review-20260731-201213.html`),
findings #1 and #2. The review's one-line diagnosis: sluice's modules are genuinely deep — the
friction is a cluster of **locality failures**, where a load-bearing rule has one home and is then
re-implemented inline at N sites.

This is a **behavior-preserving refactor**. No new capability, no ADR risk, no schema change, no
migration. The 493 existing tests are the safety net: they must stay green with only the two
surface-test edits named in §3. Baseline verified 2026-08-01 at HEAD `6652c8b` — `pixi run test`
reports **493 passed in 3.90s**.

## 1. Scope & non-goals

**In scope — two predicates, each collapsed to one definition:**

1. The **daemon-eligibility** rule (`lifecycle = 'active' AND errored_at IS NULL`) — three copies
   today, two after (the SQL twin is irreducible), with the surviving seam test-guarded.
2. The **safe-path-component** rule (a run name that is not `""`/`"."`/`".."` and contains no `/`
   or `\`) — two copies today, **one** after. Nothing left to drift.

**Explicit non-goals** (each is a deliberate exclusion, not an oversight):

- The three standalone `errored_at` reads — `events.py:141`, `control.py:635` (`clear_error`),
  `control.py:1667`. They ask a *different* question ("is this run quarantined?"), one clause
  rather than the AND of two. Wrapping a single comparison would be a shallow pass-through.
- Review finding #3, the five inline `_conclude` twins in `control.py`. Biggest consistency win
  available, but it touches the 1,745-line control module and deserves its own pass.
- Review findings #4 and #5. Deferred.
- Review finding #6 (the "world adapter" shape across `gates/`) is an **anti-recommendation**: do
  not extract a base class. It would be a shallow pass-through. Document it in a future
  `CONTEXT.md` instead.
- The drift-hunter agent (see §6). Will's idea, sequenced after this refactor.

## 2. Decisions taken (Will, 2026-08-01)

| # | Decision | Rationale |
|---|---|---|
| D1 | `is_daemon_eligible` lives in **`db.py`**, directly beside `ACTIVE_RUNS_WHERE` | The failure being prevented is the two versions drifting apart; adjacency is what prevents it. `db` also owns the `runs` vocabulary. |
| D2 | Both twins carry a docstring line naming the other | A signal to the next reader/agent that the pair moves together. |
| D3 | The twin seam gets an **executable equivalence test**, not just docstrings | A comment cannot fail. A test can. |
| D4 | That test is wired into the existing **`arch_guard.py`** PostToolUse hook | Same instant feedback the dependency arrow already gets, for ~one line of hook change. |
| D5 | `is_safe_path_component` lives in **`context.py`** | `context` owns the identity→path boundary. It is a leaf module (no sluice imports), and `daemon` already imports `resolve_context` from it, so no new dependency arrow. |

**Import-graph check (ADR-0003 layering rule, unaffected):** `db.py` and `context.py` both import
zero sluice modules and stay that way. `daemon.py` already imports from both; `control.py` already
imports from `db`. Every call site added below uses an import that exists today. The
`gates -> core` arrow is untouched.

## 3. Unit list (dependency order)

Each unit is independently startable from a failing test and independently green.

### Unit 1 — export `is_safe_path_component` from `context.py`

The simpler of the two, and the only one that fully collapses.

**RED:** a test asserting `sluice.core.context.is_safe_path_component` exists and is callable,
covering the five rejection cases (`""`, `"."`, `".."`, a name containing `/`, a name containing
`\`) and at least one accepted name.

**GREEN:**
- Add `def is_safe_path_component(run_name: str) -> bool` to `context.py`, returning
  `not ("/" in run_name or "\\" in run_name or run_name in ("", ".", ".."))`.
- `resolve_context` (`context.py:97`) calls it instead of inlining the condition. The raised
  `ValueError` message is unchanged — it still carries the raw offending name, because the
  daemon's quarantine-grade log depends on that text.
- Extend `context.__all__` to `["RunContext", "is_safe_path_component", "resolve_context"]`.
- **Delete** `daemon._is_safe_path_component` (`daemon.py:786`) outright. Its two callers
  (`daemon.py:282` detection, `daemon.py:834` the per-run jsonl sink) call the imported one.
  Add `is_safe_path_component` to the existing `from sluice.core.context import ...` line.
- `daemon.__all__` is unchanged (the deleted helper was private).

**Test edit required:** `tests/test_context.py:602` asserts `context.__all__` is exactly
`{RunContext, resolve_context}`. It gains `is_safe_path_component`. This is a deliberate contract
change, which is exactly what that test is there to force.

**Docstring note:** `daemon.py:789-793`'s comment says the rule is "re-implemented here … there is
no exported helper to import". That sentence becomes false and must go.

### Unit 2 — export `is_daemon_eligible` from `db.py`

**RED:** a test asserting `db.is_daemon_eligible` returns `True` only for a row that is both
`lifecycle == "active"` and `errored_at is None`, across all four combinations.

**GREEN:**
- Add, immediately below `ACTIVE_RUNS_WHERE` (`db.py:508`):

  ```python
  def is_daemon_eligible(run_row) -> bool:
      return run_row["lifecycle"] == "active" and run_row["errored_at"] is None
  ```

  Duck-typed on `run_row` (subscript only), so a `sqlite3.Row` and a plain mapping both work —
  matching `resolve_context`'s existing style.
- **D2 docstrings, both directions.** `is_daemon_eligible`'s docstring names `ACTIVE_RUNS_WHERE`
  as its SQL twin and states that SQLite cannot call Python, which is why the pair is irreducible
  and why `tests/test_predicate_twins.py` exists. The `ACTIVE_RUNS_WHERE` comment block
  (`db.py:498-507`) gains the reciprocal line. Neither is edited alone.
- Add `"is_daemon_eligible"` to `db.__all__`.

**Call sites collapse:**
- `daemon.py:422` → `if not is_daemon_eligible(run_row): return "ineligible"`. The comment at
  `daemon.py:420-421` that flags the smell ("re-implemented inline") is deleted — it describes a
  condition that no longer holds. Add the name to the existing `from sluice.core.db import (...)`.
- `control.py:923` (`_quiescence_refusal_why`) → `if is_daemon_eligible(run_row):`. The refusal
  string is unchanged. Add the name to the existing `from sluice.core.db import (...)`.

**Test edit required:** `tests/test_db.py:1826` (`test_db_exports_exactly_the_documented_surface`)
gains `"is_daemon_eligible"`, and the comment above it at `:1813` — which currently says the
surface "excludes `ACTIVE_RUNS_WHERE` (an internal constant)" — is amended to say the *constant*
stays internal while its Python twin is exported.

### Unit 3 — the twin equivalence test (`tests/test_predicate_twins.py`, new file)

The mechanism that makes D3 real. New file rather than an addition to `test_db.py`, so the hook in
unit 4 can run it in isolation and stay fast.

**The test:** into a real initialized database, insert runs spanning the full cross-product —
`lifecycle` in `{active, held, skipped}` × `errored_at` in `{NULL, set}` — plus at least one
`completed_at`-stamped active row (F4: completed runs are deliberately still eligible, and this
row is what catches a future attempt to fold `completed_at IS NULL` into either twin).

Then assert the two paths agree exactly:

```python
every_row = conn.execute("SELECT * FROM runs").fetchall()   # no WHERE — the Python path filters
sql_names = {row["name"] for row in select_active_runs(conn)}
python_names = {row["name"] for row in every_row if is_daemon_eligible(row)}
assert sql_names == python_names
```

Both greps in §5 return exactly **2** against HEAD `6652c8b` today and must return exactly **1**
after — verified 2026-08-01, so those criteria are measurements, not aspirations.

Its docstring states plainly what it is for: the SQL and Python twins cannot be merged (SQLite
cannot call a Python function), so this is the only thing standing between them and silent drift.
Editing one copy alone turns it red.

### Unit 4 — wire the twin test into `arch_guard.py`

`arch_guard.py` currently runs a single `GUARD_TEST` and prints one hardcoded failure headline.
Generalize to a small table of `(test_file, failure_headline)` pairs, run each on any edit under
`sluice/core/`, and report the headline belonging to whichever failed — so the dependency-arrow
message stays exactly as precise as it is today and the twin failure gets its own.

The existing behaviors are preserved unchanged: silence when `pixi` is absent (`FileNotFoundError`
→ exit 0, never cry wolf), exit 2 on failure, exit 0 when the edited path is outside `sluice/core/`.

**Note:** `tests/test_guard_hook.py` covers `guard_production_and_secrets.py`, not this hook, so it
needs no edit.

**Tracking:** `.claude/hooks/*.py` is allowlisted in `.gitignore:62`, so this edit is a tracked
change and syncs to the CHTC copy.

## 4. Invariant checks

- **Behavior preservation.** Every one of the 493 existing tests stays green. Exactly two are
  edited, both surface tests, both because the module contract deliberately changed
  (`test_context.py:602`, `test_db.py:1826`). **If any other test needs editing, stop** — that is
  evidence the change was not behavior-preserving.
- **ADR-0003 layering.** `tests/test_dependency_arrows.py` green. No new sluice import in `db.py`
  or `context.py`; both stay leaves.
- **No schema change.** No migration, no `MIGRATIONS` entry, no `schema_version` bump.
- **Error text unchanged.** `resolve_context`'s `ValueError` message and
  `_quiescence_refusal_why`'s refusal string are byte-identical after the refactor — both are
  operator-facing and asserted by existing tests.
- **`ACTIVE_RUNS_WHERE` stays internal.** It is not added to `db.__all__`. Only its Python twin is
  exported.
- **Copy count, the actual deliverable.** Eligibility: 3 → 2 (SQL + Python, test-guarded).
  Safe-path: 2 → 1.

## 5. Done criteria (observable, green)

1. `grep -rn 'lifecycle.*==.*"active"' sluice/` returns exactly **one** Python site (`db.py`).
2. `grep -rn 'in ("", ".", "..")' sluice/` returns exactly **one** site (`context.py`).
3. `pixi run test` — 493 + the new twin/unit tests, all passing.
4. `pixi run lint` clean.
5. `pytest tests/test_dependency_arrows.py` green.
6. Hook proof: edit a file under `sluice/core/`, confirm the hook runs both guard tests; break one
   twin on purpose and confirm it reports the twin headline, then revert.

## 6. Carry-forwards (not this plan)

- **The drift-hunter agent — proposed, then DROPPED 2026-08-01. Do not rebuild it.**
  Will's idea, and a good one, but investigating it found the job already done by the
  `/improve-codebase-architecture` skill (mattpocock plugin) — which is in fact what *produced* the
  2026-07-31 review this plan came from. A custom agent would have duplicated a proven tool.

  The one gap that seemed to justify building our own — "it needs a standing exclusion list or it
  will nag about settled decisions forever" — **does not exist**. That skill already reads
  `docs/adr/` as decisions not to re-litigate, and offers to record an ADR when a candidate is
  rejected for a load-bearing reason, precisely so future sweeps stop re-suggesting it. Finding #6
  (the `gates/` world-adapter anti-recommendation) is exactly that shape and now lives in
  `CONTEXT.md`.

  Two things follow, both since done:
  1. **`CONTEXT.md` now exists.** The skill reads it and is instructed to use its vocabulary; the
     project's own `.claude/agents/domain.md` already named it as the domain-doc home.
  2. **`docs/adr/` is now tracked** (`.gitignore` §7). The "do not re-litigate" mechanism runs on
     those files; while they were gitignored it worked only on this one laptop and would have
     silently stopped working the moment a second copy existed.

  What stays true regardless of who does the hunting: it is a **periodic whole-repo sweep**, never a
  per-increment diff review — the two copies of the eligibility rule were written seventeen commits
  apart in different subsystems, so a diff-scoped reviewer is structurally the wrong window. And the
  division of labour holds: **the sweep discovers, a test enforces** — every pair it finds should end
  as either a collapsed duplicate or a twin test, and then leave its attention permanently.

  Note the skill is marked `disable-model-invocation: true`, so it cannot be auto-invoked and is not
  something to wire into the build loop — Will runs it, and it ends in a grilling loop that needs
  him. Cadence: after a subsystem lands, or every few increments.
- Review findings #3, #4, #5 — each its own pass.
- Finding #6 → a `CONTEXT.md` entry, not code.
- Plan B increment 4 (the download gate) resumes after this.
