# Plan 09 — daemon (`sluice/core/daemon.py`): the reconcile loop, the sole actor

**Subsystem 9 of 9 (Plan A, control core).** Order: db → gate → events → context → profile →
reconcile → audit → control → **daemon**. This is the LAST Plan A subsystem: it composes all
eight built modules and adds almost no logic of its own — the loop reads state, decides via
`reconcile`, runs gates, persists results directly, and emits the events whose call sites
every prior plan deferred here.

**Build mode: lightweight.** One implementer, one pass, one review. Tests and code are
written together per unit; every named test below is the unit's definition of done.
**Reviewed 2026-07-29: READY TO BUILD; both ⚑ flags APPROVED by the user (see the sign-off
section).**

Ground truth: design of record §7 (THE loop; poll-not-signal decision A; disk-halt →
auto-pause), §5 (single-daemon `fcntl.flock`; single-host DB; short transactions), §2 (the
daemon writes its own progress directly, never via the API); errata B5/B9/B6/F-ADR4;
ADR-0002 (all failures quarantine; the loop is the sole run-state writer), ADR-0004 (binary
contracts; d.4 SKIPPED-quarantines), ADR-0005 (sluice never deletes files; `invalidate` is a
dormant hook nothing invokes); the §6/§7 carry-forward sections of plans 02–08 (collected in
§1.1 below — the daemon is everyone's consumer).

---

## 1. Scope & non-goals

**Owns** — everything in `sluice/core/daemon.py` + `tests/test_daemon.py`:

- `DaemonLockError` + `acquire_daemon_lock(lock_path)` — single-instance `fcntl.flock`.
- `assemble_registry(*registries)` — the collision-detecting profile-registry union
  (plan 05 §7 obligation).
- `prepare_database(config, *, registry, default_pipeline_version)` — migrations +
  seed/validate `metadata.pipeline_profile` (plan 05 §7).
- `detect_new_runs(config, *, notifier=None)` — the watch-directory sweep; stamps
  `pipeline_version` from the metadata default at detection (the ONLY moment the default is
  read for a run).
- `process_run(config, run_name, *, registry, notifier=None, should_stop=None)` — the
  per-run step: fresh re-fetch → derive contract (`apply_exclusions(load_profile(...),
  run_exclusions(row))`) → complete? stamp/skip → `next_gate` → `resolve_context` + mkdir →
  `gate.run` timed → persist per outcome → emit. Drives gates until the run blocks (⚑2).
- `run_cycle(config, *, registry, notifier=None, disk_floor_check=None, should_stop=None)`
  — one full look: `is_paused` head-check → disk floor (B5) → detect → per-run steps.
- `run_forever(config, *, registry, notifier=None, disk_floor_check=None, max_cycles=None)`
  — lock, startup, signal handlers, cycle + sleep loop, clean shutdown.
- `configure_logging(config)` — the minimal `logging_setup` port (plan 03 §7): dual
  console + rotating-jsonl sink, per-run `logs/runs/<run>.jsonl` routing keyed on the
  literal `"run.name"` field.
- The pre-authorized `gate.py` SKIPPED-comment sweep (errata F-ADR4) — unit 0.
- Config-schema edits, both pre-authorized/routine: rename `folder_events_dir` →
  `run_events_dir` (plan 03 §7, "read by nothing" today); add `watch_dir` (placeholder
  `"<path/to/watch_dir>"`). `poll_interval_minutes` and `state_file`/`local_staging_dir`/
  `events_file`/`events_max_mb`/`events_backup_count` are reused, not duplicated.

**Does NOT touch** (defer by name):

- **Production gates, remote detection, CephFS disk floor** — Plan B. Plan A detection is a
  local watch-directory listing; the disk floor is an *injected seam*
  (`disk_floor_check: Callable[[Mapping], str | None] | None`, default `None` = no check).
  D1's xattr strategy plugs into that seam in Plan B.
- **`GateResult.metadata` → column persistence** — Plan A persists NO metadata to columns
  (metadata rides `gate.finished` only). Errata A4's `globus_task_id` must be persisted
  *mid-gate, before the wait* — it cannot ride a returned result at all; Plan B designs that
  write with the globus gate.
- **In-gate control checkpoints** (each download wave / NVD stdout line / Globus increment —
  spec §7) — Plan B, designed with those gates. Plan A's checkpoint set is the daemon's own:
  loop-head pause check, per-gate-step fresh re-fetch, `should_stop` between gate steps.
  Constraint carried: gates may *read* state via `ctx.db_path` at checkpoints; ADR-0002
  forbids writes.
- **B6 `timeout_seconds` enforcement** — Plan B (plan 02 ⚑2 approved the deferral "to the
  subsystem-9/Plan-B change that builds its enforcement"; a synchronous in-process gate call
  has no supervisor to enforce one — the subprocess machinery arrives with the merge gate).
- **Heartbeats + the 7 deployment-context fields (D5), concrete Slack `Notifier`,
  `slack_thread_ts` persistence (A5)** — Plan B (plan 03 §7; user-decided: nothing in
  Plan A reads or writes `slack_thread_ts`).
- **Calling control ops for progress** — never. The daemon's persists are direct
  `update_run`/`write_txn` writes, never audited (plan 07 §6). The ONE sanctioned
  daemon→control call is B5 auto-pause via `control.pause` (plans 07/08 carry-forwards).
- **`Gate.invalidate`** — never called, by anything (ADR-0005 d.3; plan 08 §6 binding:
  spec §7's `run_from_fresh: gate.invalidate(ctx)` pseudocode line is dead).
- **Pointer columns `run_from_step`/`run_from_fresh`** — never read (plan 08 ⚑1: dormant;
  the routed honor-or-replace question is closed — pointerless rewind shipped in control).
- **Supervision (systemd vs screen, E1), `sluice doctor` (E2), the CLI entrypoint** —
  Plan C. `run_forever` is built systemd-ready (SIGTERM clean shutdown, flock refusal,
  `max_cycles` for a `--once` mode).

### 1.1 Collected obligations (who imposed what — all land here)

| From | Obligation |
|---|---|
| plan 02 §3.2/§7 | Outcome→persist table verbatim; `terminate` only on hold; SKIPPED row resolved by ADR-0004 d.4 (quarantine) |
| plan 03 §7 ⚑1/⚑5 | Persist-then-emit; `emit_gate_event` at every persist with daemon-timed `duration_ms`; `emit_run_event` at BOTH terminal looks (completion stamp + quarantine, `extra={"failed.gate": ...}`); all notifier calls via `safe_notify` at detection/persist/terminal; design the B5 pause alert; own the `configure_logging` port keyed on `"run.name"` |
| plan 04 §7 | Fetch the row, fail-loud on `None`, `resolve_context(row, config)`, **`ctx.workdir.mkdir(parents=True, exist_ok=True)`** before `gate.run`; `resolve_context` escapes are quarantine-grade for that run, never a loop crash |
| plan 05 §7 | Stamp `pipeline_version` from `metadata.pipeline_profile` at detection; seed the key at startup; assemble the registry with a collision-raising union; `UnknownProfileError` = per-run quarantine; the disabled-variant full-loop proof consumes `REFERENCE_PROFILE_ALPHA_DISABLED` |
| plan 06 §7 | `next_gate is None` ⟹ stamp `completed_at` then emit (B9); the SKIPPED→quarantine persist (column never set); never call `next_gate` on an ineligible run; completion re-derived, `completed_at` is a cache |
| plan 07 §6 | Progress writes never audited; B5 auto-pause is an audited control action with actor `"daemon"` (final string decided here — ⚑1, approved) |
| plan 08 §6 | Contract resolution is ALWAYS `apply_exclusions(load_profile(run["pipeline_version"], registry), run_exclusions(row))`; loop head reads `control.is_paused` (the one B5 parse site); hold reaction = `terminate` + record HELD; no auto-clear of a disk-halt pause (operator `resume` only); owes the exclusion loop-integration mirror of control's unit-11 miniature |

---

## 2. Errata in scope

- **B5 (daemon half) — RESOLVED HERE (⚑1 APPROVED 2026-07-29).** Disk below floor →
  `control.pause(db_path, "daemon", reason="disk floor: <why>")` — the same audited op,
  `target_run` NULL, actor literal `"daemon"` — plus ONE error-level `daemon.paused` wide
  event carrying the reason (the ⚑3-of-plan-03 obligated alert; no core halt exception, no
  new run-outcome vocabulary — B5 is metadata state). **No auto-clear**: operator `resume`
  is the only clear (already control's shipped stance — the silent-overnight-stall risk is
  accepted over flapping). **No run quarantine**: the Plan A check runs at the loop head,
  global, before any run is selected — there is no "tripping run"; Plan B revisits if it
  adds per-gate pre-flight checks. The spec's pause-not-halt reversal of the 2026-06-22
  HALT decision is confirmed as intended (it is the state-only-consistent choice and
  is already baked into shipped, user-approved control code).
- **B9 — CLOSED HERE (wiring; design closed by plan 03 ⚑1).** The canonical per-run
  `run.finished` line is emitted at exactly two moments, both after their persist commits:
  (a) the look where the contract is complete and `completed_at` transitions NULL→stamped;
  (b) the quarantine persist (FAILURE/SKIPPED/resolution failure). Never on subsequent
  already-complete looks — the emission condition is the *transition*, so `select_active_runs`
  returning completed runs forever (db F4) causes no spam. A run that quarantines, is
  cleared, and completes emits two canonical lines over its life — the honest reading of
  statelessness.
- **F-ADR4 sweep — pre-authorized, unit 0.** `sluice/core/gate.py`'s
  `SKIPPED = "skipped"` inline comment predates ADR-0004 and must name the quarantine
  response.
- **B6 — routed to Plan B** under the already-approved plan 02 ⚑2 deferral (see §1
  non-goals). Not a new fork.
- **A1/A3 hygiene (inherited, verified by review not tests):** the daemon adds no pragmas
  (`get_connection` owns WAL + `synchronous=NORMAL`); there is no per-run `locked_at`
  mutex anywhere — single-instance is the `flock` alone.
- **E1 noted, not resolved:** who starts/restarts the daemon is Plan C; this plan only
  guarantees the process is supervisable (clean SIGTERM, loud second-instance refusal).

---

## 3. Fixed design decisions (the implementer builds exactly this)

1. **`db_path = config["state_file"]`, always by subscript** — the same source
   `resolve_context` uses; one authority, fail-loud `KeyError` on a missing key. The lock
   file is `str(config["state_file"]) + ".lock"` — derived, not a new key, and therefore on
   the same local disk as the DB (single-host invariant §5).
2. **Detection** lists the immediate *subdirectories* of `config["watch_dir"]` (files
   ignored), sorted by name; each unseen name is `insert_run(conn, name,
   pipeline_version=<metadata default>)` — all inserts batched in ONE `write_txn`, the
   default read inside that same txn. `insert_run`'s `ON CONFLICT DO NOTHING` + returned
   bool make the sweep idempotent and give the "genuinely new" signal that gates the
   `run.detected` event and the `open_run_thread` notify. A missing
   `metadata.pipeline_profile` at detection raises `RuntimeError` naming the unseeded key
   (only reachable if `prepare_database` was skipped).
3. **Per-run pacing (⚑2, approved): drive until blocked.** Within one cycle, `process_run`
   repeats the full stateless look — fresh `get_run`, fresh contract derivation,
   `is_complete`, `next_gate` — running one gate per look, until the run blocks: completion
   stamped, a non-SUCCESS outcome, ineligibility observed at the re-fetch, or
   `should_stop()`. Every iteration is re-derived from the DB (statelessness is
   *re-derivation*, not one-gate pacing), so a hold written mid-drive is honored at the
   very next gate boundary. This matches python-poller's per-folder behavior (`poller.py`
   runs a folder through all stages in one pass) and means a run completes — and emits its
   canonical line — in the cycle its last gate succeeds, instead of leaking one
   `poll_interval_minutes` per gate. **Eligibility, re-checked on the fresh row at every
   step, is exactly `row["lifecycle"] == "active" and row["errored_at"] is None`** — the
   same predicate as db's `ACTIVE_RUNS_WHERE`, which is a non-exported internal constant;
   the daemon re-implements it inline on the fetched row rather than importing it.
4. **The outcome→persist table (plan 02 §3.2, SKIPPED resolved by ADR-0004 d.4).** Each
   persist is its own short `write_txn`, opened only *after* `gate.run` returns:

   | `result.outcome` | daemon writes (direct, never audited) | then |
   |---|---|---|
   | SUCCESS | `{satisfied_column: 1}` (metadata NOT persisted — §1) | `emit_gate_event`; `safe_notify(notifier, "post_milestone", name, f"{gate.name} completed")` — that literal text; continue the drive |
   | FAILURE | `{error_reason: result.reason, errored_at: now_iso()}` | `emit_gate_event`; re-fetch; `emit_run_event(extra={"failed.gate": gate.id})`; `safe_notify finish_run_thread(..., "quarantined", ...)`; stop the drive |
   | SKIPPED | quarantine as FAILURE, `error_reason = "gate '<id>' returned SKIPPED but is in this run's contract (ADR-0004 d.4): the gate and the contract disagree — a human decides which is wrong"`; **the column is never set** | same as FAILURE (gate line carries `outcome="skipped"`) |
   | HELD | **nothing** | call `gate.terminate(ctx)` if not `None`; `emit_gate_event`; stop the drive |

   An `Exception` escaping `gate.run` is converted to
   `GateResult(FAILURE, reason=f"unhandled {type(exc).__name__} in gate '{gate.id}': {exc}")`
   and takes the FAILURE row — the loop never dies for one run (plan 03 §3.3: no `error`
   outcome exists; an escape *becomes* a quarantine).
5. **Per-run isolation contract (the try boundary — the rule, stated once).**
   `process_run` catches exactly the **quarantine class** and quarantines the ONE run
   (`error_reason` naming the anomaly, `errored_at`; emit `run.finished` with no `gates`
   and no `failed.gate` — there is no gate; notify finish; return `"quarantined"`; never a
   loop crash — plans 04/05 §7):
   - `UnknownProfileError` and `ValueError` from `load_profile` (unregistered version /
     key-mismatch);
   - `ValueError` from `run_exclusions` (corrupt `excluded_gates` JSON);
   - `ValueError`/`KeyError` from `resolve_context` (traversal name / missing config key);
   - any `Exception` escaping `gate.run` (converted per §3.4).

   Everything else is **fatal by design and propagates** — `run_cycle` adds no catch:
   reconcile's missing-gate-column `KeyError` (`_require_gate_columns_present`) and
   persist-time `sqlite3.OperationalError`/`UnknownColumnError` are the schema-drift
   class, where every run is equally broken; quarantining runs one by one would flood the
   fleet with noise that a single crash states once. Consequence for the implementation:
   the catches wrap the **specific call sites** (contract derivation, `resolve_context`,
   `gate.run`) — never one blanket `try` around the body, because a blanket
   `except KeyError` would swallow reconcile's fatal `KeyError` along with
   `resolve_context`'s quarantine-grade one.
6. **Completion look:** contract complete and `completed_at` IS NULL → stamp
   `completed_at = now_iso()` in a short txn, re-fetch, `emit_run_event(row,
   gates=required_gates(contract))` (partition reads all-satisfied), `safe_notify
   finish_run_thread(name, "completed")`. Complete and already stamped → return
   `"already-complete"` silently (F4's accepted cost, no event). `completed_at` is never an
   *input* — `is_complete` re-derives every look.
7. **Signals are process lifecycle ONLY — decision A stands.** SIGTERM and SIGINT handlers
   set a shutdown flag (a `threading.Event`); nothing else. `should_stop` (the flag's
   `.is_set`) is checked between gate steps and between runs, so SIGTERM finishes the
   current gate then exits. Holds remain poll-only: no signal reaches any gate, ever.
8. **Cycle order (spec §7 verbatim):** `is_paused` → disk floor → detect → per-run drive.
   The loop-head check opens a plain read connection — `is_paused(get_connection(db_path))`,
   since `is_paused` takes a `conn`, not a path — closed immediately after. Paused
   short-circuits *everything* including detection and the disk check (so a disk-tripped
   pause cannot re-fire `control.pause` every cycle — one audit row per trip). A
   `disk_floor_check` that itself raises is logged at error level and treated as healthy
   (fail-open — D1's posture; the strategy owns its internal semantics).
9. **`run_cycle` returns a small plain dict** (`{"status": "paused" | "disk-paused" |
   "stopped" | "ok", "detected": [...], "runs": {name: token}}`) purely for tests/logging —
   never authoritative state. `process_run` returns one token from the closed set
   `{"missing", "ineligible", "quarantined", "held", "completed", "already-complete",
   "stopped"}`.
10. **Daemon-owned event vocabulary** (logger `structlog.get_logger("sluice.daemon")`):
    `daemon.started`, `daemon.stopped`, `daemon.shutdown_requested` (info);
    `run.detected` (info, carries `run.name`); `daemon.paused` (error, B5),
    `run.vanished` (error, the missing-row anomaly). The pinned three
    (`run.finished`/`gate.finished`/`control.finished`) come only from `events.py`/
    `control.py` — the daemon never rebuilds them.
11. **Import surface (closed set):** stdlib (`fcntl`, `os`, `pathlib`, `signal`, `time`,
    `threading`, `types`, `logging`, `logging.handlers`, `json`, `collections.abc`) +
    `structlog` + `sluice.core.{db, gate, events, context, profile, reconcile, control}`.
    Forbidden by name: `sluice.gates/api/cli` (arch guard), `slack_sdk`, `subprocess`,
    `shutil` — the daemon starts work only through gate function references and deletes
    nothing (ADR-0005).

---

## 4. Units, in dependency order

All tests in `tests/test_daemon.py` unless noted. House test substrate: `tmp_path` DB via
`prepare_database`, watch/staging dirs under `tmp_path`, `REFERENCE_PROFILES` as the
registry, mock-beta instruction files to script outcomes (plan 02 §3.3 — assert on state,
never monkeypatch a running daemon), `structlog.testing.capture_logs` for events, a
3-method recording fake for the notifier.

### Unit 0 — the F-ADR4 sweep (pre-authorized, no test)

Edit `sluice/core/gate.py` line 35 only:
`SKIPPED = "skipped"  # gate judged itself inapplicable at runtime` →
`SKIPPED = "skipped"  # in-contract SKIPPED = gate/contract contradiction: the daemon quarantines (ADR-0004 d.4)`.
Comment-only; the suite staying green is the guard. No other gate.py hunk is authorized.

### Unit 1 — module skeleton, import hygiene, single-instance lock

`DaemonLockError(RuntimeError)`; `acquire_daemon_lock(lock_path) -> IO` — open/create the
file, `fcntl.flock(fd, LOCK_EX | LOCK_NB)`; on `BlockingIOError`/`OSError` close and raise
`DaemonLockError` naming the path and the "another sluice daemon holds the lock" diagnosis.
Release = close the returned handle (kernel drops the flock; also on process death — no
stale-lock reaper needed, the A3 lesson).

- `test_daemon_import_surface_is_closed` — ast-parse `daemon.py`: imports ⊆ the §3.11 set;
  `subprocess`/`shutil`/`slack_sdk` absent by name; **no attribute reference named
  `invalidate`, `unlink`, `rmtree`, or `remove` anywhere in the module** (ADR-0005
  structural pin — the daemon deletes nothing and never invalidates).
- `test_second_lock_acquisition_refuses_loud` — acquire; second `acquire_daemon_lock` on
  the same path raises `DaemonLockError`; close the first handle; a third acquisition
  succeeds (release works, no reaper).

### Unit 2 — registry assembly + startup (`assemble_registry`, `prepare_database`)

`assemble_registry(*registries) -> Mapping[str, Profile]` — explicit union; a duplicate
`pipeline_version` key raises `ValueError` naming it (never a silent `{**a, **b}` shadow —
plan 05 §7); returns a read-only `MappingProxyType`.

`prepare_database(config, *, registry, default_pipeline_version) -> pathlib.Path` —
`initialize_database(config["state_file"])`; then in one `write_txn`: if
`PIPELINE_PROFILE_KEY` is absent, validate `default_pipeline_version` via
`load_profile(..., registry)` and seed it; if present, leave it untouched (an operator's
`set_default_profile` outranks the deployment default) but validate *the stored value*
resolves — an unresolvable active default is a startup error, not N per-run quarantines.
Returns the db path.

- `test_assemble_registry_raises_on_key_collision_and_is_read_only` — union of
  `REFERENCE_PROFILES` + a dict re-keying `"reference-v1"` raises `ValueError` naming the
  key; a clean union contains both sides and refuses item assignment (`TypeError`).
- `test_prepare_database_migrates_and_seeds_once` — fresh path: schema at head,
  `get_metadata(conn, PIPELINE_PROFILE_KEY) == "reference-v1"`; call again with default
  `"reference-v1-alpha-disabled"` → key still `"reference-v1"` (seed-once; operator value
  survives restarts).
- `test_prepare_database_fails_loud_on_unresolvable_default` — default not in registry →
  `UnknownProfileError`, key not seeded; then seed a good default, hand-write garbage into
  the key via `set_metadata`, call again → raises (the stored-value validation).

### Unit 3 — detection (`detect_new_runs`)

Per §3.2. After the txn commits, per genuinely-new name: `log.info("run.detected",
**{"run.name": name, "run.pipeline_version": stamped})` and
`safe_notify(notifier, "open_run_thread", name)` (return value discarded — A5 persistence
is Plan B's). Returns the new names, sorted.

- `test_detection_inserts_active_rows_stamped_with_the_default` — two folders + one stray
  *file* in `watch_dir` → exactly two rows, `lifecycle='active'`,
  `pipeline_version="reference-v1"`, returns both names; the file is not a run.
- `test_detection_is_idempotent_and_emits_once` — second sweep returns `[]`, row count
  unchanged, `capture_logs` shows exactly one `run.detected` per run across both sweeps;
  fake notifier's `open_run_thread` called exactly once per run.
- `test_pipeline_version_default_is_read_only_at_detection` — the freeze mechanism: detect
  run1; `control.set_default_profile(..., "reference-v1-alpha-disabled", registry=...)`;
  add folder run2; detect → run1 still `"reference-v1"` (never restamped), run2
  `"reference-v1-alpha-disabled"`.
- `test_detection_fails_loud_without_watch_dir_or_seed` — config missing `watch_dir` →
  `KeyError`; seeded config but `PIPELINE_PROFILE_KEY` deleted → `RuntimeError` naming the
  unseeded key.

### Unit 4 — the per-run step (`process_run`) — the core, tested exhaustively

Per §3.3–3.6. The fresh re-fetch at each gate step is THE control checkpoint: a hold/skip/
quarantine/deletion landing after `select_active_runs` (or after the previous gate of this
drive) is honored before any further work. The eligibility predicate re-checked on each
fresh row is the §3.3 inline rule: `lifecycle == 'active' and errored_at is None`.

- `test_success_persists_column_and_emits_gate_event` — run1 at gate alpha, drive with a
  `should_stop` that stops after one gate: `mock_alpha_done == 1`, marker file exists under
  `local_staging_dir/run1` (the caller-mkdirs obligation — no pre-created dir),
  `gate.finished` line has `gate.id=="mock_alpha"`, `gate.outcome=="success"`, an integer
  `gate.duration_ms`, and nested `gate.metadata`; no other run column changed; notifier
  `post_milestone` called once with text `f"{MOCK_ALPHA.name} completed"`.
- `test_drive_until_blocked_completes_a_run_in_one_call` (⚑2's behavior) — fresh eligible
  run, no `should_stop`: ONE `process_run` returns `"completed"`; both columns 1,
  `completed_at` stamped, exactly one `run.finished` with `outcome=="completed"` and
  `gates.unsatisfied == []`; `finish_run_thread(name, "completed")` called.
- `test_already_complete_is_silent` — call again on the completed run: returns
  `"already-complete"`, zero new events (B9: emission only at the transition), row
  byte-identical.
- `test_failure_quarantines_and_emits_both_lines_from_committed_state` — script beta
  `"failure"` (alpha pre-satisfied): `error_reason == result.reason`, `errored_at` set,
  `mock_beta_done == 0`; event order is `gate.finished` (outcome `failure`) then
  `run.finished` with `outcome=="quarantined"`, `severity=="critical"`,
  `extra` field `failed.gate=="mock_beta"`, and `run.errored_at`/`error.reason` present —
  provable only if built from the re-fetched committed row (persist-then-emit witnessed);
  `finish_run_thread(..., "quarantined", ...)` called; returns `"quarantined"`.
- `test_skipped_in_contract_quarantines_and_never_sets_the_column` — ADR-0004 d.4: script
  beta `"skipped"` → quarantined, `error_reason` contains `"SKIPPED"`, `"contract"`, and
  the gate id; `mock_beta_done == 0` forever; `gate.finished` carries
  `gate.outcome=="skipped"`.
- `test_held_result_writes_nothing_and_calls_terminate` — script beta `"held"`: run row
  unchanged (no column, no error, no completion), `mock_beta.terminated` trace file exists
  (terminate invoked), `mock_beta.marker` absent, `gate.finished` outcome `held`, returns
  `"held"`; NO `run.finished` (held is not a terminal look — the daemon simply stops
  driving; plan 03 §2).
- `test_escaped_gate_exception_becomes_failure_quarantine` — a local probe `Gate` (in a
  throwaway registry/profile over the mock columns) whose `run` raises `RuntimeError("boom")`:
  no raise out of `process_run`; quarantined with `error_reason` naming `RuntimeError` and
  the gate id.
- `test_checkpoint_honors_a_hold_landed_after_selection` — alpha satisfied, then
  `control.hold(...)` the run, call `process_run` with the stale expectation that beta is
  next: returns `"ineligible"`, beta never ran (no marker, no writes, no events) — the
  fresh re-fetch, not the caller's snapshot, governs.
- `test_checkpoint_honors_a_hold_landed_mid_drive` — a throwaway two-gate profile over the
  mock columns where gate 1's `run` stands in for a concurrent operator: it calls
  `control.hold(ctx.db_path, "will", <name>)` then returns SUCCESS (test scaffolding only —
  shipped gates never touch control). `process_run` persists gate 1's success, then the
  next drive step's re-fetch sees `held` and stops: gate 2 never runs, returns
  `"ineligible"`. The in-flight gate's real work is never discarded; the hold lands at the
  next gate boundary (decision A's "effectively immediate").
- `test_missing_row_is_loud_not_fatal` — `process_run` on a never-inserted name: returns
  `"missing"`, one error-level `run.vanished` line, no exception.
- `test_resolution_failures_quarantine_the_one_run` — the §3.5 quarantine class,
  parametrized three ways: (a) `pipeline_version` hand-updated to `"ghost-v9"`; (b)
  `excluded_gates` hand-written to `'{"not": "a list"}'`; (c) run row's name is fine but
  config lacks `local_staging_dir` (the `resolve_context` `KeyError` path). Each → that
  run quarantined with a naming `error_reason`, `run.finished` quarantined emitted without
  `failed.gate`, no exception. (The fatal class needs no dedicated daemon test: reconcile's
  own suite pins the missing-column `KeyError`; the §3.5 call-site-scoped catches are what
  the reviewer verifies — no blanket `try` exists to swallow it.)
- `test_exclusion_overlay_is_honored_at_resolution` — plan 08 §6's owed mirror:
  `reference-v1` run, alpha satisfied, quarantine it (scripted beta failure), then
  `control.exclude_gate(..., "mock_beta", ...)` + `control.clear_error(...)`:
  `process_run` returns `"completed"` with `mock_beta_done == 0` — the daemon resolved
  `apply_exclusions(load_profile(...), run_exclusions(row))`, not the bare profile.
- `test_gate_run_executes_outside_any_transaction` — short-transactions invariant: a probe
  gate whose `run` opens its own `write_txn(ctx.db_path)` (BEGIN IMMEDIATE) and writes a
  scratch metadata key, then returns SUCCESS. It succeeds promptly — impossible if the
  daemon held a write txn across `gate.run`.
- `test_progress_writes_are_never_audited` — after a full drive to completion:
  `SELECT count(*) FROM control_audit` is exactly the number of control ops the test
  itself issued (zero) — persists, completion stamps, and emissions leave no audit rows.

### Unit 5 — the cycle (`run_cycle`)

Per §3.8–3.9. B5 lives here.

- `test_paused_cycle_does_nothing_at_all` — `control.pause(...)` first; a cycle with a
  fresh watch folder and a recording `disk_floor_check`: returns `{"status": "paused"}`, no
  row inserted, no gate ran, **the disk check was never called** (pause short-circuits it —
  the no-refire guarantee).
- `test_disk_floor_trips_auto_pause_with_daemon_actor` — `disk_floor_check` returning
  `"quota 95% consumed"`: cycle returns `{"status": "disk-paused"}`; `is_paused` now True;
  exactly one `control_audit` row with `actor=="daemon"`, `action=="pause"`,
  `target_run IS NULL`, args containing the reason; one error-level `daemon.paused` line;
  a second cycle returns `"paused"` without calling the check again (one audit row per
  trip). Then `control.resume(db_path, "will")` + a healthy check → the next cycle detects
  and runs gates (operator resume is the only clear — B5 end to end).
- `test_cycle_detects_and_drives_in_one_look` — fresh folder, one cycle: row exists AND the
  run is `"completed"` (detection precedes selection; drive-until-blocked).
- `test_should_stop_finishes_current_gate_then_stops` — two fresh runs, a `should_stop`
  flipping True after the first gate persist: first run has exactly one column set (the
  in-flight gate finished — never killed mid-gate), cycle status `"stopped"`, second run
  untouched.
- `test_failing_disk_check_fails_open` — a check that raises: one error line, cycle
  proceeds to detect/drive (D1 posture).

### Unit 6 — `run_forever` + signals

Per §3.7. Startup order: read `poll_interval_minutes` by subscript (fail-loud before any
work), `acquire_daemon_lock`, `prepare_database`, install handlers, `daemon.started`;
loop `run_cycle(..., should_stop=flag.is_set)` then interruptible sleep
(`poll_interval_minutes * 60`, sliced ~1 s against the flag — a flag-only handler does not
interrupt `time.sleep`); on flag or `max_cycles` exhaustion: `daemon.stopped`, close the
lock handle (finally-guaranteed).

- `test_run_forever_refuses_a_second_instance` — hold the lock manually;
  `run_forever(config, registry=..., max_cycles=1)` raises `DaemonLockError` before
  touching the DB (no new rows, no events).
- `test_run_forever_single_cycle_end_to_end` — `max_cycles=1` with one watch folder: run
  completed, lock released afterward (re-acquirable), `daemon.started` and `daemon.stopped`
  both emitted; missing `poll_interval_minutes` → `KeyError` before the lock is taken.
- `test_sigterm_sets_only_the_shutdown_flag` — install the handlers,
  `signal.raise_signal(signal.SIGTERM)`: the flag is set, `daemon.shutdown_requested`
  emitted, the process (pytest) is alive, and no DB/file state changed — decision A intact:
  signals carry zero control semantics.

### Unit 7 — `configure_logging`

Minimal port of PP `logging_setup.py` (plan 03 §7): structlog with ISO-UTC timestamper +
level, console renderer to stderr, JSON-lines to `config["events_file"]` via
`logging.handlers.RotatingFileHandler(maxBytes=events_max_mb*2**20,
backupCount=events_backup_count)`, and a per-run processor appending any event dict
carrying `"run.name"` to `config["run_events_dir"]/<run.name>.jsonl` — the literal field
plan 03 fixed as load-bearing. A `run.name` that is not a single safe path component is
never turned into a path: main-sink only + one error line. **The safe-component rule is
re-implemented here** — contains `/` or `\`, or equals `""`/`"."`/`".."` — because
`resolve_context` keeps it inline at its identity→path boundary (`context.py` ~74–79);
there is no exported helper to import. Deployment-context fields and heartbeats stay
Plan B (D5/D6). Idempotent: repeat calls replace, never stack, handlers. Config keys by
subscript, fail-loud.

- `test_configure_logging_routes_dual_and_per_run` — configure into `tmp_path`; emit one
  event with `run.name` and one without: events file has both as parseable JSON lines;
  `run_events_dir/<name>.jsonl` has exactly the first; no file for the nameless event.
- `test_configure_logging_is_idempotent` — configure twice, emit once → exactly one line in
  the events file.
- `test_unsafe_run_name_never_becomes_a_path` — event with `run.name="../evil"`: no file
  created outside `run_events_dir`, event still in the main sink, one error line.

### Unit 8 — Plan A capstone integrations (state-only control proven through the real loop)

All via repeated `run_cycle` (never `run_forever`, no sleeps), asserting on DB + filesystem
+ captured events only.

- `test_lifecycle_journey_reference_profile` — detect → complete → idle: cycle 1 completes
  run1 (drive-until-blocked); exactly ONE `run.finished` ever; later cycles report
  `"already-complete"` with zero new events; `control_audit` has zero rows (nothing but
  progress happened).
- `test_disabled_variant_completes_without_alpha` — plan 05 §7's owed proof: run stamped
  `"reference-v1-alpha-disabled"` → completes with `mock_alpha_done == 0` (satisfied by
  absence: needs-unblock AND completion-exclusion through the real loop).
- `test_quarantine_clear_error_retry_completes` — scripted beta failure → quarantined
  (cycle N runs nothing for it — left eligibility); fix the instruction file +
  `control.clear_error` → later cycles complete it; exactly TWO canonical `run.finished`
  lines over its life (quarantined then completed — plan 03 ⚑1's statelessness).
- `test_hold_unhold_resumes_where_it_left_off` — after alpha: `hold` → cycles do nothing
  for it; `unhold` → beta runs, completes. State-only control end to end: no signal, no
  daemon poke, just columns.
- `test_exclusion_remediates_a_stuck_run` — ADR-0004 d.3 through the loop: beta quarantines
  → `exclude_gate("mock_beta")` + `clear_error` → next cycle completes past the missing
  step with `mock_beta_done == 0`.

---

## 5. Invariant checks

| Invariant | How this subsystem could violate it | Guarding test |
|---|---|---|
| DB is the single source of truth; stateless between looks | the drive loop caching a plan/row across gate steps | `test_checkpoint_honors_a_hold_landed_after_selection` + `..._mid_drive`; every drive step re-fetches (unit 4) |
| State-only control / daemon is the only actor | control ops leaking into progress; daemon auditing its persists | `test_progress_writes_are_never_audited`; the one sanctioned exception pinned by `test_disk_floor_trips_auto_pause_with_daemon_actor` |
| Two write paths — daemon writes direct | persists routed through control/API | same audit-count tests; import set has no `api` (unit 1 ast + arch guard) |
| `gates → core`, never reverse | daemon importing `sluice.gates` | `tests/test_dependency_arrows.py` (auto-covers `core.daemon`) + `test_daemon_import_surface_is_closed` |
| Short transactions only | holding a txn across `gate.run` | `test_gate_run_executes_outside_any_transaction` |
| Persist-then-emit (B9) | emitting from pre-commit state | committed-row field assertions in `test_failure_quarantines_...`; transition-only emission in `test_already_complete_is_silent` |
| Poll-not-signal for holds (decision A) | signal machinery growing control semantics | `test_sigterm_sets_only_the_shutdown_flag` |
| ADR-0005 — sluice deletes nothing; `invalidate` dormant | any unlink/rmtree/invalidate call | the ast pin in `test_daemon_import_surface_is_closed` |
| ADR-0004 d.4 — SKIPPED quarantines, column never set | treating SKIPPED as satisfied (the old plan-02 recommendation) | `test_skipped_in_contract_quarantines_and_never_sets_the_column` |
| §8 freeze — default read only at detection | restamping or resolving against the live default | `test_pipeline_version_default_is_read_only_at_detection`; contract always from `run["pipeline_version"]` (unit 4) |
| No production defaults / fail-loud config | `.get(...)` fallbacks for `watch_dir`/`state_file`/paths | `test_detection_fails_loud_without_watch_dir_or_seed`; the `KeyError` arms in units 4/6/7 |
| Single instance (flock), single host | two daemons; a per-run mutex creeping back (A3) | `test_second_lock_acquisition_refuses_loud`, `test_run_forever_refuses_a_second_instance`; review: no `locked_at` anywhere |

---

## 6. Done criteria

1. `pixi run test` green (321 existing + ~35 new) and `pixi run lint` clean; the arch guard
   and hook pass on every `daemon.py` edit.
2. `sluice/core/daemon.py` ships the §1 surface; `gate.py`'s SKIPPED comment names the
   quarantine response (unit 0); `config.example.json` gained `watch_dir` and renamed
   `folder_events_dir` → `run_events_dir` — placeholders only.
3. **Plan A is complete**: with nothing but a config dict, `REFERENCE_PROFILES`, and an
   empty watch directory, `run_forever(config, registry=REFERENCE_PROFILES,
   default_pipeline_version="reference-v1", max_cycles=N)` detects folders, drives the
   reference pipeline to completion, and is fully steerable by the pure control ops —
   pause/resume, hold/unhold, quarantine/clear, exclude — with zero external services, a
   complete ordered audit trail of every control action, no audit rows for progress, and
   the canonical B9 lines at exactly the terminal looks.
4. Errata B5 (daemon half) and B9 are stamped resolved; F-ADR4's sweep note is discharged;
   B6/D1/D5/A4/A5/E1/E2 are carried forward by name (§7), not silently dropped.

## 7. Carry-forwards (named, so no plan rediscovers them)

- **Plan B (production gates):** in-gate control checkpoints (download waves / NVD stdout /
  Globus increments) — gates read state via `ctx.db_path` at checkpoints and return HELD;
  ADR-0002 still forbids gate-side writes; D6 warns checkpoints must not live in
  `ThreadPoolExecutor` workers relying on contextvars. `disk_floor_check` gets the D1
  CephFS xattr strategy (quota root, bytes math, fail-open, never `df`), config-selected,
  plugged into the existing seam — and Plan B decides whether a per-gate pre-flight check
  changes the ⚑1 no-run-quarantine answer. B6 `timeout_seconds` + enforcement arrive
  together with the subprocess-running gates (expiry → FAILURE → quarantine). A4:
  `globus_task_id` persisted mid-gate immediately after submit — a designed exception to
  "gates never write run state" or a daemon-provided callback; decided with the gate,
  citing ADR-0002. Metadata→column persistence policy defined per gate, explicitly.
  Concrete Slack `Notifier`; persist `open_run_thread`'s ts to `slack_thread_ts` (A5);
  heartbeats + the 7 deployment-context fields into `configure_logging` (D5). Detection's
  remote (SSH) listing replaces/augments the local `watch_dir` sweep; B1/B2 (where
  `experiment_id` is born) may move work into the detection step — designed there.
  Production registry injected at the composition root via `assemble_registry`.
- **Plan C (API + CLI):** the daemon entrypoint (`sluice daemon`) calls
  `configure_logging(config)` then `run_forever`; `--once` = `max_cycles=1`. E1: a systemd
  unit is now sufficient — SIGTERM is a clean finish-current-gate shutdown and a double
  start dies loudly on the flock. E2: `sluice doctor` preflights. The audit reader orders
  by `id` (plan 07). `run_cycle`'s return dict and `process_run`'s token vocabulary are a
  pinned seam if the CLI surfaces them.

---

## ⚑ Human sign-off — both APPROVED (2026-07-29, decided by the user)

1. **⚑1 — B5 closure trio — APPROVED (2026-07-29, decided by the user, as recommended).**
   (a) Disk floor → auto-**pause** (not halt) is confirmed as the intended reversal of the
   2026-06-22 HALT decision; (b) **no auto-clear** — operator `resume` only, accepting the
   silent-overnight-stall risk over flapping (the pause is loud: critical-severity
   `daemon.paused` line + audit row); (c) **no run quarantine** at the global loop-head
   check (there is no tripping run at that point), with audit actor literal `"daemon"`.
   Plan B revisits (c) only if it adds per-gate pre-flight disk checks (§7).
2. **⚑2 — per-run pacing: drive-until-blocked — APPROVED (2026-07-29, decided by the
   user, as recommended).** Spec §7's pseudocode reads one-gate-per-run-per-cycle; this
   plan drives each run gate-by-gate within the cycle, each step a full stateless
   re-derivation with a checkpoint between gates. Rationale: statelessness is
   re-derivation, not pacing; python-poller behaves this way per folder; the alternative
   leaks one `poll_interval_minutes` (10 min) of dead time per gate and delays the
   completion stamp/canonical line by a full cycle. Accepted cost: within a cycle, run A
   finishes its gates before run B starts (the same serialization PP has).
