# Plan 03 — `events` subsystem (Plan A, subsystem 3 of 9)

**Module owned:** `sluice/core/events.py` (single file)
**Build order:** db → gate (+ mock gates) → **events** → context → profile → reconcile →
audit → control → daemon
**Tests:** consolidated in `tests/test_events.py` (one file per subsystem, db/gate precedent).

Ground truth: design of record §2 (spine), §7 (loop — where the wide event must land),
§9 (control ops emit a wide event), §11 (observability; control actions never post to
Slack); errata **B8/B9** (in scope), A5/B5/D5/D6 (touched, not owned); **ADR-0002**
(binding: all failures quarantine; gates never write state — events *describe* state,
they never act); ADR-0001 (docstring standard, binds every unit's code);
`.claude/skills/logging/SKILL.md` (the wide-event / canonical-log-line pattern);
plan 02 §1 (declared B8/B9 events-owned) and §7 (carry-forwards); subsystem map
(PP = python-poller `mizzou_fastq_automated_download`).

---

## 1. Scope & non-goals

### Owned

| Thing | Public surface |
|---|---|
| The injected notifier contract (B8) | `Notifier` Protocol — `open_run_thread`, `post_milestone`, `finish_run_thread` |
| Never-fail notification | `safe_notify(notifier, method, *args, **kwargs)` |
| Run-outcome vocabulary | `run_outcome(run_row)`, `SEVERITY_BY_RUN_OUTCOME` |
| The canonical per-run wide event (B9, design half) | `build_run_event(run_row, gates, extra)`, `emit_run_event(...)` → `run.finished` |
| The per-gate per-look event | `build_gate_event(run_name, gate, result, duration_ms)`, `emit_gate_event(...)` → `gate.finished` |
| The control-op wide event (§9) | `control_event(action, actor, target_run, result)` → `control.finished` |
| Slack milestone text helper | `format_duration(ms)` (ported, PP `events.py:88-104`) |

`events.py` declares `__all__ = ["Notifier", "SEVERITY_BY_RUN_OUTCOME",
"build_gate_event", "build_run_event", "control_event", "emit_gate_event",
"emit_run_event", "format_duration", "run_outcome", "safe_notify"]`; unit 8 asserts
equality (db unit-7 precedent).

**Allowed imports (closed set):** stdlib + `structlog` + `sluice.core.gate`. The design
of record §3 names structlog as events' foundation and it is already a core dependency
(`pixi.toml`); it is the **only** third-party import. `slack_sdk`, `sqlite3`, and
`sluice.core.db` are forbidden by name (unit 1). Builders take a **row object**, never a
connection or a db path — events cannot write state because it structurally cannot reach
the database.

### Explicitly NOT owned (and where it belongs)

- **A Slack (or any) `Notifier` implementation** — **Plan B**. `slack_sdk` arrives with
  Plan B's dependencies; core owns only the Protocol. Tests use a recording fake defined
  in `tests/test_events.py`.
- **Persisting `slack_thread_ts`** (A5) — **Plan B**. `open_run_thread` *returns* the
  thread ts through `safe_notify` (the seam this plan owes A5); Plan B persists it —
  **nothing in Plan A reads or writes that column** (user-decided). Events never writes
  any `runs` column.
- **Emission wiring** — **subsystems 6 and 9**. `reconcile.next_gate` does not exist yet;
  the moment `next_gate is None` (and the quarantine persist) belongs to the loop. This
  plan ships the builder and the emitter; **it does not close B9** (see §2 B9 —
  HANDOFF §9a caveat, verbatim obligation).
- **`configure_logging`** (dual console+jsonl output, rotating handler, per-run
  `logs/runs/<run>.jsonl` sink, deployment-context fields) — **subsystem 9**, which
  configures structlog once at daemon startup (PP `logging_setup.py`). Tests here use
  `structlog.testing.capture_logs`, which needs no configuration.
- **Heartbeats** (D5) — **Plan B / subsystem 9** (HANDOFF §7 assigns D5 to Plan B). See
  §7 for the constraints they must land under (no contextvars — D6).
- **Audit rows** — **subsystem 7**. `control_event` is the *event* half of §9's
  "audit row + wide event"; the audit row is not written here.
- **`get_status`** — **subsystem 8**, which may reuse `run_outcome`/`build_run_event`
  for its snapshot (they are total functions of a row, not terminal-only).
- **PP's context-manager machinery** — **deliberately not ported, deleted**: the
  `_accumulator`/`_heartbeat_state` contextvars (PP `events.py:44-50`), `folder_event`
  (`:135-181`), `stage` (`:184-227`), `_OutcomeHandle`/`_StageHandle` (`:107-133`),
  `enrich` (`:71-75`), `bind_experiment_id` (`:78-81`), `set_heartbeat_interval`
  (`:65-68`). A with-block accumulator cannot span reconcile looks (B9); everything it
  carried is either re-derived from the run row (identity, outcome, totals), carried on
  `gate.finished` (per-gate durations/fields, via `GateResult.metadata`), or passed
  explicitly by the daemon (`extra`). This deletion also defuses D6's
  contextvar-in-threadpool landmine for run-level events — there is no contextvar left
  to lose.

---

## 2. Errata & policy resolutions in scope — decisions taken, no TBDs

### B8 — `events.py` core-clean. RESOLVED here, fully.

Three parts, each with a named guard:

1. **The gate-exception string sniff is not decoupled — it is structurally impossible.**
   PP maps `type(exc).__name__ == "DiskExhaustedError"` → outcome `"halted"`
   (`events.py:157`). In sluice, no exception ever reaches events: gates *return*
   `GateResult` (ADR-0002 d.2), the daemon persists it, and events builds from
   **rows and results — there is no exception parameter anywhere in the surface**
   (§3 signatures; unit 8's surface test pins it). The `"halted"` outcome is dropped
   from the vocabulary (⚑3); if B5's disk-halt wants a core-owned halt exception or a
   `daemon.paused` event, that belongs to subsystem 9, which owns the disk-floor check
   (§7 carry-forward).
2. **The Slack lifecycle is lifted behind an injected `Notifier` Protocol.** PP welds
   thread open/finish into `folder_event` (`events.py:147-151, 173-177`) and milestones
   into `stage`'s finally (`:219-226`). Sluice: core owns the Protocol (unit 2) and the
   never-fail call helper (unit 3); the **daemon** calls them at its own moments
   (detection / persist / terminal — §7 carry-forward, ⚑5, approved). Core never
   imports `slack_sdk` — unit 1's ast guard forbids it by name.
3. **The dead `database_path` param** (`events.py:136`) dies with `folder_event`
   (non-port, §1). No function in this module takes a db path or connection; unit 1's
   guard forbids `sqlite3`/`sluice.core.db` outright.

Evidence the port is small: PP's own tests already inject a `FakeNotifier` and a local
duck-typed exception (`test/test_events.py:37-44, :119`), and pipeline vocabulary appears
in PP's events code only in comments (subsystem map, grep result).

### B9 — the reconcile-loop wide-event model. DESIGN RESOLVED HERE; **wiring is NOT closed** (HANDOFF §9a caveat).

The contextvar accumulator cannot span looks (§2.4: stateless between looks), so the
canonical per-run line is **rebuilt from state**: `build_run_event(run_row, gates)` is a
pure function of the run row (+ the gate records, for the satisfaction snapshot). No
memory, no accumulator — a daemon restarted mid-run emits the identical line.

**Emission points (the decision, executed by subsystems 6/9):** one `run.finished` per
run per **terminal look** —
- `reconcile.next_gate(run, profile) is None` → outcome `completed` (the B9-named point);
- the FAILURE persist → outcome `quarantined`, with the daemon passing
  `extra={"failed.gate": gate.id}` (the builder never *infers* the failing gate —
  no fabrication). **⚑1 — APPROVED (2026-07-21, decided by the user)** — this second
  emission point extends B9's named option.

**Persist-then-emit rule (binding on 6/8/9):** the event *describes* state, so the state
write (and its commit) always precedes the emit — a line describing an uncommitted write
would survive a crash the write didn't. Carried forward by name (§7).

**Per-gate timings** are not in the run row and are therefore **not on the run line**:
they ride on `gate.finished`, emitted at each persist (unit 6). The run line carries what
state carries: identity, lifecycle, outcome, error fields, wall-clock total
(`detected_at` → terminal timestamp), and the per-gate satisfaction partition. This is
the state-faithful reading of B9's "reconstructing from the run row + per-gate timings":
row fields on the run line, timings on the gate lines, correlated by `run.name`.

**What this plan does NOT claim:** B9 is closed. It is not — `next_gate` does not exist
until subsystem 6. The builder, emitter, vocabulary, and emission-point decision land
here; the call sites land with reconcile/daemon (§7). Any status doc marking B9 resolved
at subsystem 3 is wrong.

### ADR-0002 — events describe failure honestly. APPLIED.

- **No retry vocabulary.** PP's `"retrying"` severity tier (`events.py:58`) is deleted —
  no retry exists to describe. The severity map is a closed, tested equality (unit 4).
- **`quarantined` → severity `"critical"`** (PP had `"warning"`, `events.py:59`).
  Deliberate change: under all-failures-quarantine, every quarantine is
  operator-actionable (`clear_error` is the only recovery), and severity `critical` is
  what D5's future @-mention-on-actionable-failures policy will key on.
  **⚑2 — APPROVED (2026-07-21, decided by the user)**.
- Severity stays **a field, not a level** (PP spec decision #6, `events.py:55-62`):
  every event emits at `info`; the only `error`-level line in the module is
  `notify.failed` (unit 3).

### A5 — touched, not owned.

`Notifier.open_run_thread` returns `str | None` (the Slack-assigned ts,
PP `poller_notifications.py:110-111`), and `safe_notify` **propagates the return value**
— that is the entire seam this plan owes A5. Persisting it is **Plan B's obligation**:
nothing in Plan A reads or writes `slack_thread_ts` (user-decided; the column exists
from db migration 001, unread until Plan B).

### D5 / D6 — deferred with constraints, by name.

Heartbeats and deployment context are Plan B / subsystem 9 (HANDOFF §7). Constraints they
inherit from this plan: heartbeat state must be **explicit** (module dict or passed
handle), never a contextvar (D6: contextvars do not propagate into
`ThreadPoolExecutor` workers, and downloads are threaded); deployment context binds once
in `configure_logging` (subsystem 9), not per-event here.

### §11 — control actions never post to Slack. MADE STRUCTURAL.

`control_event` has **no notifier parameter** — the policy is unviolatable at the
signature level, and unit 7 pins the signature. Only pipeline milestones (daemon-called
notifier hooks) reach Slack.

---

## 3. The contract, authoritative

### 3.1 Surface

```python
# sluice/core/events.py — imports: stdlib + structlog + sluice.core.gate ONLY

log = structlog.get_logger("sluice.events")     # module logger (skill: one logger)

@runtime_checkable
class Notifier(Protocol):
    """Milestone notifier the daemon/api inject; core never implements it."""
    def open_run_thread(self, run_name: str, experiment_id: int | None = None,
                        size_tb: float | None = None) -> str | None: ...
    def post_milestone(self, run_name: str, text: str, mention: bool = False) -> None: ...
    def finish_run_thread(self, run_name: str, outcome: str,
                          reason: str | None = None) -> None: ...

def safe_notify(notifier: Notifier | None, method: str, /,
                *args: object, **kwargs: object) -> object | None: ...

def format_duration(ms: int | float) -> str: ...          # ported PP events.py:88-104

SEVERITY_BY_RUN_OUTCOME: dict[str, str]                   # closed vocabulary: see 3.3

def run_outcome(run_row) -> str: ...                       # pure, total (see 3.3)

def build_run_event(run_row, gates: Sequence[Gate] = (),
                    extra: Mapping[str, object] | None = None) -> dict[str, object]: ...
def emit_run_event(run_row, gates: Sequence[Gate] = (),
                   extra: Mapping[str, object] | None = None) -> dict[str, object]: ...

def build_gate_event(run_name: str, gate: Gate, result: GateResult,
                     duration_ms: int | None = None) -> dict[str, object]: ...
def emit_gate_event(run_name: str, gate: Gate, result: GateResult,
                    duration_ms: int | None = None) -> dict[str, object]: ...

def control_event(action: str, actor: str, target_run: str | None = None,
                  result: Mapping[str, object] | None = None) -> dict[str, object]: ...
```

Functional shaping: frozen records already live in `gate.py`; this module is
**module-level functions only** — no handle classes, no context managers (PP's
`_OutcomeHandle`/`_StageHandle` are not ported, §1). Emitters call `log.info` exactly
once and return the built field dict (callers — control Results, tests — get the fields
without re-deriving).

**`run_row` duck type:** any object supporting `row[column]` and `row.keys()` — both
`dict` and `sqlite3.Row` qualify. Implementer trap: presence checks must go through
`column in row.keys()`, never `column in row` — on a `sqlite3.Row`, `in` silently tests
**value** membership, not column presence (`"a" in row` is `False` even when a column
named `a` exists; `1 in row` is `True` whenever any column holds `1`), so the naive
spelling type-checks, runs, and lies. And a missing gate column must be pre-checked so
both row types raise the same `KeyError` (`sqlite3.Row[missing]` raises `IndexError`,
not `KeyError`) — unit 5 tests this.

### 3.2 Event schemas (field names are a pinned cross-subsystem seam)

Naming: lowercase dot-namespaced (logging skill / OTel-ish; PP convention with
`folder.*` → `run.*` per subsystem map). **`"run.name"` is load-bearing**: subsystem 9's
per-run jsonl processor keys on this literal (PP `logging_setup.py:77` keyed on
`"folder.name"`) — the rename is fixed here, by test.

**`run.finished`** — from `build_run_event(run_row, gates, extra)`:

| Field | Presence |
|---|---|
| `run.name`, `run.pipeline_version`, `run.lifecycle`, `run.detected_at` | always |
| `outcome`, `severity` | always (`run_outcome` + map lookup) |
| `experiment.id`, `run.size_tb` | iff non-NULL in the row |
| `run.completed_at`, `run.errored_at`, `error.reason` | iff non-NULL |
| `lifecycle.by`, `lifecycle.at`, `lifecycle.reason` | iff non-NULL |
| `total_duration_ms` | iff terminal: `completed` → `completed_at − detected_at`; `quarantined` → `errored_at − detected_at` (ISO parse via `datetime.fromisoformat`; `now_iso()` output round-trips). Absent otherwise — **never fabricated** |
| `gates.satisfied`, `gates.unsatisfied` | iff `gates` non-empty: partition of `[g.id]` by `row[g.satisfied_column]` truthiness, caller order preserved |
| caller extras (e.g. `failed.gate`) | merged last; **a key colliding with a built field raises `ValueError`** — no silent clobber of the canonical line |

Deliberately absent, stated so nobody ports them: `folder.attempt` (attempts died with
B4/ADR-0002), `failed.stage` inference (daemon passes `failed.gate` explicitly),
`severity` on gate events (run-line only, PP parity), timestamps (added by the configured
structlog `TimeStamper`, subsystem 9 — `capture_logs` output has none; tests must not
assert them).

**`gate.finished`** — from `build_gate_event`: `run.name`, `gate.id`,
`gate.outcome` (= `result.outcome.value`), plus `gate.duration_ms` iff given (the
**daemon** times `gate.run`; events formats), `gate.reason` iff `result.reason`,
`gate.metadata` iff `result.metadata` (nested dict, not splatted — collision-proof;
`JSONRenderer` handles nesting).

**`control.finished`** — from `control_event`: `control.action`, `control.actor`, plus
`run.name` iff `target_run` (global ops like `pause` have none), `control.result` iff
given (nested).

### 3.3 Run-outcome vocabulary (closed) and precedence

`run_outcome(run_row)` returns exactly one of `completed | quarantined | held |
skipped | active`, by this precedence over an arbitrary (even inconsistent) row —
**pipeline facts outrank operator dispositions; the actionable fact outranks all**:

1. `errored_at` non-NULL → `quarantined` (even if also completed/held — the operator
   action pending is `clear_error`)
2. `completed_at` non-NULL → `completed` (a later hold/skip of a finished run doesn't
   rewrite what the pipeline did)
3. `lifecycle = 'held'` → `held`
4. `lifecycle = 'skipped'` → `skipped`
5. else → `active` (total function: `get_status` reuses this for non-terminal rows;
   the *daemon* simply never emits `run.finished` for it)

```python
SEVERITY_BY_RUN_OUTCOME = {
    "completed":   "info",
    "quarantined": "critical",   # ⚑2 (approved) — PP had "warning"; ADR-0002 makes every quarantine actionable
    "held":        "warning",
    "skipped":     "info",
    "active":      "info",
}
```

No `retrying` (ADR-0002), no `halted` (⚑3, approved — B5 → subsystem 9), no `error` (an
escaped exception becomes a daemon-persisted FAILURE → `quarantined`; subsystem 9 owns
that catch).

---

## 4. Units, in dependency order (8 units)

Every unit is one RED → GREEN → REFACTOR cycle starting from the named failing test.
Tests live in `tests/test_events.py`. Shared helpers, defined at the top of the file as
units need them: `make_run_row(**overrides) -> dict` (defaults: `name="r1"`,
`pipeline_version="v1"`, `lifecycle="active"`, `detected_at="2026-07-21T10:00:00+00:00"`,
every optional column `None`, `mock_alpha_done=0`, `mock_beta_done=0`) and
`RecordingNotifier` (records `(method, args, kwargs)` tuples; constructor-scriptable
return value and raise). The fake lives in tests, not core — core ships contracts, not
test doubles.

---

### Unit 1 — module bootstrap, import hygiene (the B8 structural guard), `format_duration`

RED: `tests/test_events.py` fails at `import sluice.core.events` (module absent).

**Tests**
- `test_events_is_core_clean_by_import` — ast-parse `sluice/core/events.py`: every
  `Import`/`ImportFrom` root is in `sys.stdlib_module_names` or is `structlog` or
  `sluice`; every `sluice.*` import is exactly `sluice.core.gate`; and — asserted by
  name so the failure message teaches — none of `slack_sdk`, `sqlite3`,
  `sluice.core.db` appears. (Technique of `test_db_imports_only_stdlib` /
  `test_mock_gates_import_only_gate_and_stdlib`; `tests/test_dependency_arrows.py`
  auto-discovers the new module via `pkgutil.walk_packages` — no edit there.) This one
  test carries B8's core-clean half **and** the state-only structural guard: a module
  that cannot reach a SQL client cannot write run state.
- `test_format_duration_adapts_units` — parametrized over PP's own documented pivots
  (`events.py:94`): `19 → "19 ms"`, `5324 → "5.3 s"`, `1899507 → "31.7 min"`,
  `9811266 → "2.7 h"`. One behavioral test; the function is a straight port and the
  adaptive-unit choice is the only sluice decision in it.

---

### Unit 2 — `Notifier` Protocol (B8's injection contract)

**Tests**
- `test_notifier_protocol_names_the_three_run_lifecycle_methods` — a minimal fake
  defining exactly `open_run_thread`/`post_milestone`/`finish_run_thread` satisfies
  `isinstance(fake, Notifier)`; an object missing `finish_run_thread` does not. Pins the
  `runtime_checkable` choice and the **`run_*` (not `folder_*`) names** — a drift back
  to PP's names would break every fake and Plan B's implementation. (One test; a
  Protocol has no behavior of its own — `runtime_checkable` checks names only, and the
  docstring says so per ADR-0001.)

---

### Unit 3 — `safe_notify` (notification never fails work; centralizes PP's copy-pasted try/excepts)

Behavior: `None` notifier → no-op returning `None`. Otherwise resolve
`getattr(notifier, method)` **outside** the try (a typo'd method name is a programmer
error — `AttributeError` propagates, fail-loud), call it inside the try; on any
`Exception` emit exactly one `log.error("notify.failed", **{"notify.method": method,
"error.type": type(exc).__name__})` and return `None`; otherwise return the call's
return value. This is the one home for PP's scattered guards
(`events.py:150-151, 176-177, 192-194, 225-226`; `poller_notifications.py:85-88` etc.):
a Slack outage must never fail a gate or the daemon.

**Tests**
- `test_safe_notify_with_no_notifier_is_a_no_op` — returns `None`;
  `capture_logs()` captured nothing.
- `test_safe_notify_passes_through_and_returns_the_value` — against
  `RecordingNotifier` scripted to return `"1721.001"`:
  `safe_notify(fake, "open_run_thread", "r1", experiment_id=7)` returns `"1721.001"`
  and the fake recorded `("open_run_thread", ("r1",), {"experiment_id": 7})`. The
  return-value leg is the A5 seam — drop it and `slack_thread_ts` can never be persisted.
- `test_safe_notify_swallows_notifier_failures_loudly` — fake scripted to raise
  `RuntimeError("slack down")`: returns `None`; `capture_logs()` holds exactly one
  entry with `event == "notify.failed"`, `log_level == "error"`,
  `entry["error.type"] == "RuntimeError"`, `entry["notify.method"] == "post_milestone"`.
- `test_safe_notify_fails_loud_on_a_method_typo` —
  `safe_notify(fake, "post_milestne", "r1", "hi")` raises `AttributeError`. A swallowed
  typo would disable notifications forever with zero signal — the no-silent-fallback
  posture (guardrail §8) applied to observability.

---

### Unit 4 — `run_outcome` + `SEVERITY_BY_RUN_OUTCOME` (the vocabulary, closed)

**Tests**
- `test_run_outcome_precedence_matrix` — parametrized over `make_run_row` overrides →
  expected outcome:
  1. fresh row → `"active"`
  2. `errored_at`+`error_reason` set → `"quarantined"`
  3. `completed_at` set → `"completed"`
  4. `lifecycle="held"` → `"held"`
  5. `lifecycle="skipped"` → `"skipped"`
  6. errored **and** `lifecycle="held"` → `"quarantined"` (the actionable fact wins)
  7. completed **and** `lifecycle="skipped"` → `"completed"` (pipeline fact outranks
     disposition)
  8. errored **and** completed (inconsistent but schema-legal) → `"quarantined"`
     (total function; never raises on a weird row)
- `test_run_outcome_severity_vocabulary_is_closed` — `SEVERITY_BY_RUN_OUTCOME ==`
  the exact five-entry dict of §3.3, by equality. Equality is the point: no
  `retrying` (ADR-0002), no `halted` (⚑3), no `error`; and
  `set(map) == {run_outcome(row) for row in <the matrix rows>}` — every derivable
  outcome has a severity, so `build_run_event`'s lookup can never `KeyError`.

---

### Unit 5 — `build_run_event` + `emit_run_event` (the canonical line, B9's design half)

**Tests**
- `test_run_event_fields_for_a_completed_run` — row with every optional column set
  **except the error pair** (`error_reason`/`errored_at` stay `NULL` — setting them
  would force `quarantined` under §3.3 precedence rule 1; the quarantined-row path is
  exercised on a real row in unit 8), `completed_at="2026-07-21T10:01:30+00:00"` (90 s
  after the default `detected_at`), `gates=MOCK_GATES`, both mock columns 1:
  `set(fields)` equals exactly the §3.2 always+conditional set for this row;
  `fields["outcome"] == "completed"`, `fields["severity"] == "info"`,
  `fields["total_duration_ms"] == 90000` (exact — the ISO round-trip through
  `now_iso`'s format is the sluice decision under test),
  `fields["gates.satisfied"] == ["mock_alpha", "mock_beta"]`,
  `fields["gates.unsatisfied"] == []`.
- `test_run_event_fabricates_nothing_for_a_fresh_row` — `build_run_event(make_run_row())`
  (no gates) → keys are exactly `{"run.name", "run.pipeline_version", "run.lifecycle",
  "run.detected_at", "outcome", "severity"}` — no `total_duration_ms`, no
  `experiment.id`, no `error.reason`, no `gates.*`. The no-fabrication posture (§9/B2's
  ethic, applied to observability: absent state yields absent fields, never defaults).
- `test_run_event_partitions_gates_by_satisfaction_in_caller_order` — row with **both**
  mock columns 0 and the gates passed **reversed**, `gates=(MOCK_BETA, MOCK_ALPHA)` →
  `gates.satisfied == []` and `gates.unsatisfied == ["mock_beta", "mock_alpha"]`: both
  ids land in ONE partition in the caller's reversed order, so an implementation that
  sorts (alphabetically, by `order`, or by registry position) fails. With both columns
  1 and the forward tuple, `gates.satisfied == ["mock_alpha", "mock_beta"]`. The
  partition preserves the sequence the caller (later: the profile) supplies.
- `test_run_event_fails_loud_on_a_missing_satisfaction_column` — a `make_gate`-style
  gate with `satisfied_column="ghost_done"` → `KeyError` whose message names
  `"ghost_done"`. Uniform across dict and `sqlite3.Row` (the §3.1 implementer trap): a
  registry/schema mismatch surfaces here, not as a puzzling `IndexError` at 2am.
- `test_run_event_extra_merges_but_never_clobbers` — `extra={"failed.gate": "mock_beta"}`
  lands in the fields; `extra={"outcome": "completed"}` raises `ValueError` naming
  `"outcome"`. The canonical line's builder-owned fields are not overridable — a caller
  that could rewrite `outcome` could lie about state.
- `test_emit_run_event_is_one_canonical_info_line` — under
  `structlog.testing.capture_logs()`: exactly **one** entry; `event == "run.finished"`;
  `log_level == "info"`; every built field appears in the entry with equal value; the
  return value equals `build_run_event`'s dict. One rich event per run per look — the
  skill's core rule, executable.

---

### Unit 6 — `build_gate_event` + `emit_gate_event` (per-gate per-look; where timings live)

**Tests**
- `test_gate_event_fields_for_a_success_with_metadata` —
  `build_gate_event("r1", MOCK_ALPHA, GateResult(GateOutcome.SUCCESS,
  metadata={"marker": "/tmp/x"}), duration_ms=42)` equals exactly
  `{"run.name": "r1", "gate.id": "mock_alpha", "gate.outcome": "success",
  "gate.duration_ms": 42, "gate.metadata": {"marker": "/tmp/x"}}`. Pins: outcome is the
  enum's `.value` string, metadata rides nested (not splatted), and `run.name` is on
  every gate line (no contextvars — correlation is by field, §3.2).
- `test_gate_event_failure_carries_the_reason_and_omits_absences` — FAILURE with
  `reason="boom"`, no metadata, no duration → keys exactly
  `{"run.name", "gate.id", "gate.outcome", "gate.reason"}`, `gate.reason == "boom"`.
  (No-fabrication again: an absent duration is absent, not 0.)
- `test_emit_gate_event_is_one_info_line` — one captured entry,
  `event == "gate.finished"`, `log_level == "info"`, fields match the built dict.

---

### Unit 7 — `control_event` (§9's wide event; §11's no-Slack, structural)

**Tests**
- `test_control_event_emits_one_line_with_the_op_facts` —
  `control_event("hold", "will", target_run="r1", result={"ok": True})` → one captured
  entry: `event == "control.finished"`, `log_level == "info"`,
  `control.action == "hold"`, `control.actor == "will"`, `run.name == "r1"`,
  `control.result == {"ok": True}`; return value is the same dict.
- `test_control_event_global_op_carries_no_run_name` — `control_event("pause", "will")`
  → `"run.name"` and `"control.result"` absent from the fields (pause/resume target the
  world, not a run; a fabricated `run.name` would corrupt the per-run jsonl routing).
- `test_control_event_cannot_reach_slack` —
  `list(inspect.signature(control_event).parameters) == ["action", "actor",
  "target_run", "result"]`. The §11 policy ("control actions never post to Slack") as a
  signature fact: there is no notifier to call. Editing this signature is a conscious
  policy change.

---

### Unit 8 — surface + real-substrate integration

No new production code except the final `__all__`; green-on-arrival where units 1–7 did
their job (gate-plan unit-9 precedent) — the RED start is the `__all__` equality test.

**Tests**
- `test_events_exports_exactly_the_documented_surface` —
  `set(sluice.core.events.__all__)` equals the §1 ten-name set.
- `test_run_event_reads_a_real_sqlite_row` — `initialize_database(tmp)`;
  `insert_run` `"r1"`; `update_run` to `mock_alpha_done=1`, then to
  `error_reason="boom", errored_at=now_iso()`; `row = get_run(conn, "r1")` (a real
  `sqlite3.Row`): `run_outcome(row) == "quarantined"`;
  `fields = emit_run_event(row, MOCK_GATES, extra={"failed.gate": "mock_beta"})` under
  `capture_logs` yields one `run.finished` with `severity == "critical"`,
  `gates.satisfied == ["mock_alpha"]`, `gates.unsatisfied == ["mock_beta"]`,
  `error.reason == "boom"`, `total_duration_ms` an `int >= 0`, and
  `failed.gate == "mock_beta"`. **The load-bearing part:** the whole pipeline runs off
  `sqlite3.Row` (the `row.keys()` duck-typing seam), against the real schema's column
  names — a drift in either fails here, not in the daemon. Afterward,
  `dict(get_run(conn, "r1")) == dict(row)` — building and emitting changed no state
  (behavioral echo of the unit-1 structural guard; events described, never acted).

---

## 5. Invariant checks

| Invariant | How this subsystem could violate it | Guarding test |
|---|---|---|
| **DB is the single truth; events never write state** (state-only spine) | a builder taking a connection; events importing db/sqlite3 | `test_events_is_core_clean_by_import` (unit 1, structural — no `sqlite3`, no `sluice.core.db`) + the unchanged-row assertion in `test_run_event_reads_a_real_sqlite_row` (unit 8) |
| **Core-clean / `gates → core`; no `slack_sdk` in core** (B8) | importing slack_sdk or a gates module | `test_events_is_core_clean_by_import` (unit 1); `tests/test_dependency_arrows.py` auto-covers the new module |
| **One canonical line per run per look** (skill; B9) | scattered emits, a second `log.*` inside an emitter | `test_emit_run_event_is_one_canonical_info_line` (unit 5), single-entry assertions in units 6–7 |
| **Control actions never post to Slack** (§11) | a notifier param creeping into `control_event` | `test_control_event_cannot_reach_slack` (unit 7, signature equality) |
| **Notification failure never fails work** | an unguarded notifier call path | `test_safe_notify_swallows_notifier_failures_loudly` (unit 3) — and `safe_notify` is the only sanctioned call path (§7 carry-forward to 9/Plan B) |
| **No silent fallbacks / no fabrication** (guardrail §8; §9/B2 ethic) | defaulting absent state into fields; swallowing a method typo; extra clobbering `outcome` | `test_run_event_fabricates_nothing_for_a_fresh_row`, `test_run_event_extra_merges_but_never_clobbers` (unit 5), `test_safe_notify_fails_loud_on_a_method_typo` (unit 3), `test_run_event_fails_loud_on_a_missing_satisfaction_column` (unit 5) |
| **ADR-0002 honesty: no retry tier; quarantine is terminal-actionable** | `retrying`/`halted` re-entering the vocabulary | `test_run_outcome_severity_vocabulary_is_closed` + the precedence matrix (unit 4) |
| **Stateless between looks** (§2.4) | any contextvar/module accumulator spanning looks | structural: no such state exists to test — the §1 non-port is the resolution; `test_run_event_reads_a_real_sqlite_row` proves a from-cold build needs nothing but the row |
| **Short transactions** | n/a — events holds no connection | inherited (db guards); nothing here opens one (unit 1 structural) |

---

## 6. Done criteria

1. `pixi run test` fully green: every pre-existing test (dependency arrows now covering
   `sluice.core.events`) plus `tests/test_events.py`.
2. `sluice/core/events.py` imports only stdlib + `structlog` + `sluice.core.gate`;
   `slack_sdk`, `sqlite3`, `sluice.core.db` appear nowhere in it.
3. B8 fully resolved: no exception enters events (no exception parameter exists), the
   Slack lifecycle lives behind the injected `Notifier` Protocol, the dead
   `database_path` param is gone by non-port.
4. The canonical `run.finished` builds purely from a real `sqlite3.Row` (+ gate records)
   with exact, tested field names; `gate.finished` and `control.finished` emit exactly
   one info line each; `severity` rides as a field on the run line only.
5. **B9 is explicitly NOT claimed closed** — the emission-point decision and the
   persist-then-emit rule are recorded here; the call sites are named carry-forwards to
   subsystems 6/9 (§7).
6. Downstream can start: subsystem 8 gets `control_event` + `run_outcome`/
   `build_run_event` for `get_status`; subsystem 9 gets `emit_run_event`/
   `emit_gate_event`/`safe_notify` and the wiring spec; Plan B gets the `Notifier`
   Protocol to implement and the heartbeat constraints.

---

## 7. Carry-forwards (named, so no plan rediscovers them)

- **Persist-then-emit (subsystems 6/8/9, binding):** state write commits **before** the
  event emits; §9's op shape is mutate → audit row (subsystem 7) → `control_event`, in
  that order. An event describing an uncommitted write is a lie after a crash.
- **Subsystem 4 (context):** build on **⚑5 (approved)** as a decided fact — the
  daemon-only-caller model is the accepted spec §6 revision, so Plan A's `RunContext`
  carries **no notifier field** (design §6's "notifier" entry in the `resolve_context`
  list is superseded). If Plan B gates ever need gate-side milestones (PP posts
  directly from NVD/metadata blocks, `poller.py:1209-1214, 1250-1259`), that arrives
  as a conscious contract change, not a silent field.
- **Subsystem 6 (reconcile):** `next_gate(run, profile) is None` is the completion
  signal; the completion write (`completed_at`, per its C4-aware completion predicate)
  precedes `emit_run_event`. Cite this plan §2 B9; do not re-decide the event shape.
- **Subsystem 8 (control):** every op calls `control_event` exactly once, after its
  audit row; ops never construct or receive a `Notifier` (the signature makes it
  impossible anyway); `get_status` may reuse `run_outcome`/`build_run_event` as a
  read-only snapshot.
- **Subsystem 9 (daemon):** owns all emission wiring — `emit_gate_event` at every
  persist (daemon times `gate.run` and passes `duration_ms`); `emit_run_event` at the
  two terminal looks (⚑1, approved), passing `extra={"failed.gate": gate.id}` on
  quarantine; all notifier calls via `safe_notify` at its milestone moments (open at
  detection, milestone at persist, finish at terminal — the daemon is the sole
  notifier caller, ⚑5, approved). The ts `open_run_thread` returns through
  `safe_notify` is the A5 seam, but **persisting it to `slack_thread_ts` is Plan B's
  obligation — nothing in Plan A reads or writes that column** (user-decided). Owns the
  `configure_logging` port (PP `logging_setup.py`): dual console+rotating-jsonl output,
  the per-run `logs/runs/<run>.jsonl` processor **keyed on the literal `"run.name"`
  field fixed here**, deployment-context binding (D5's 7 fields), and the config keys —
  renaming `folder_events_dir` → `run_events_dir` in the config schema while porting
  (the key is currently read by nothing). Owns B5's disk-halt observability — an
  obligation, not an option, now that ⚑3 is approved: subsystem 9's plan must design
  the pause alert (whether a `daemon.paused` event / core-owned halt exception exists —
  the `halted` vocabulary hole left by ⚑3).
- **Plan B:** implement `Notifier` over `slack_sdk` (token from `SLACK_BOT_TOKEN` env
  only, per bootstrap decision; core never sees it); **persist the thread ts returned
  by `open_run_thread` to `slack_thread_ts`** (A5 — Plan A leaves the column untouched);
  if thread-title experiment updates survive, extend the concrete notifier — the core
  Protocol stays at three methods (⚑4, approved); port heartbeats into `events.py` then
  (D5) with **explicit throttle state, never contextvars** (D6: `ThreadPoolExecutor`
  workers don't inherit them and downloads are threaded), priming-without-emitting
  preserved (PP `events.py:229-246`); the @-mention-on-actionable-failures policy keys
  on `post_milestone(mention=)` + severity `critical`.

---

## ⚑ Human sign-off — all five APPROVED (⚑1–⚑4 2026-07-21, ⚑5 2026-07-22; decided by the user)

1. **§2 B9 — quarantine as a second canonical emission point — APPROVED (2026-07-21,
   decided by the user).** B9's named option is only `next_gate == None`; this plan
   adds "and at the FAILURE persist, outcome `quarantined`" so a quarantined run gets
   its canonical line at the moment it leaves eligibility (it may never reach
   `next_gate == None`). A run that quarantines, is cleared, and completes emits two
   canonical lines over its life — one per terminal look, which is the honest reading
   of statelessness.
2. **§3.3 — `quarantined` severity `"critical"` — APPROVED (2026-07-21, decided by the
   user).** (PP had `"warning"`, `events.py:59`.) Rationale: ADR-0002 removed the retry
   tier, so every quarantine now demands an operator `clear_error`; `critical` is what
   the future @-mention policy (D5, Plan B) will key on.
3. **§3.3 — `halted` dropped from the run-outcome vocabulary — APPROVED (2026-07-21,
   decided by the user).** PP's `halted` existed only for the DiskExhaustedError sniff
   (B8); under B5 disk-halt becomes a *global pause*, which is metadata state, not a
   run outcome — its observability (and any core-owned halt exception) moves to
   subsystem 9 with the B5 decision. Subsystem 9's plan is **obligated** to design the
   pause alert (§7 carry-forward); this plan only removes it from the *run* vocabulary.
4. **§3.1 — `Notifier` fixed at three methods — APPROVED (2026-07-21, decided by the
   user).** PP's `update_folder_experiment` (`poller_notifications.py:120`) omitted
   from the core Protocol. Late experiment-id thread-title updates, if kept, are a
   Plan B concrete-notifier concern invoked from Plan B gates/daemon wiring — core
   calls only the three lifecycle methods.
5. **Spec §6 revision — the notifier is daemon-injected, never RunContext-carried —
   APPROVED (2026-07-22, decided by the user).** Design §6 has `resolve_context`
   derive "destination paths, remote paths, config, notifier, DB path" for gates; this
   plan replaces that seam with a **daemon-only-caller** model (§2 B8: the daemon
   calls the Protocol via `safe_notify` at detection / persist / terminal; gates
   return `GateResult`s and never notify). Consequence: Plan A's `RunContext`
   (subsystem 4) carries **no notifier field**, and PP's gate-side direct posts
   (`poller.py:1209-1214, 1250-1259`) are re-homed to the daemon's persist step in
   Plan B. This is the accepted revision of spec §6's letter; the subsystem-4
   carry-forward in §7 encodes it as a decided fact, and subsystem 4's context plan
   proceeds on the daemon-only model.
