# sluice — Handoff & Compaction Record

**Updated:** 2026-09-04 (CHTC session — FIRST CHTC INSTALL (3y), seed-and-skip backfill (3z), first daemon start + two findings (3aa), delivery-aware detection/download built TDD and adversarially reviewed, 761 green, uncommitted (3ab); runaway second start killed and fenced, two HIGH bugs queued (3ac); see §10 entries 3y–3ac)
**Purpose:** The single file to read when resuming this project — across sessions, across the two
environments, and across context compaction. If a summary ever replaces the live conversation, start
here.

---

## 0. Resume protocol (read in this order)

1. **This file** — state, environments, reconciliations, next actions.
2. [`specs/2026-07-16-sluice-architecture-design-of-record.md`](docs/superpowers/specs/2026-07-16-sluice-architecture-design-of-record.md) — the approved whole-system design. **Preserved verbatim; never edit it** — corrections go in the errata.
3. [`specs/2026-07-17-sluice-transition-bootstrap-design.md`](docs/superpowers/specs/2026-07-17-sluice-transition-bootstrap-design.md) — how python-poller becomes sluice + what the first commit contains.
4. [`specs/sluice-spec-errata.md`](docs/superpowers/specs/sluice-spec-errata.md) — the ~20 places the code contradicts / gaps / drops-knowledge vs the design of record. Each item is tagged with the plan (A/B/C) that resolves it.
5. [`notes/python-poller-subsystem-map.md`](docs/superpowers/notes/python-poller-subsystem-map.md) — the deep code analysis.
6. [`.claude/agents/README.md`](.claude/agents/README.md) — the build-team roster + orchestration loop (§9a).

Then continue at **§10 Next actions.**

---

## 1. TL;DR — where the project stands

`sluice` is a new, greenfield **state-only pipeline control plane + daemon** that generalizes the
in-production `python-poller` orchestrator (download → MD5 → lane-merge → NVD preprocess → notify →
LabKey register). The **architecture is approved** (design of record). The **transition/bootstrap is
designed and — on macOS — now EXECUTED**: the repo skeleton, config schema, CI, lockfile, and the
dependency-arrow guard test all exist and `pixi run test` is green.

**PLAN A IS COMPLETE — all nine subsystems built, committed, and green (33 commits on `main`,
ALL PUSHED to `origin` = `git@github.com:wkgardner/sluice.git`, `main` tracks `origin/main` — verified 2026-08-01; tagged `plan-a-rc1`).** db, gate, events, context, profile,
reconcile, audit, control, daemon are done; `pixi run test` → 435 passed, lint clean. **ADR-0004
(binary contracts) is executed in code:** the profile vocabulary is `required|disabled` — a run's
contract is binary, every contract gate mandatory, excluded gates absent; there is no runtime
optional tier — and control's `exclude_gate` op is the remediation-is-exclusion mechanism (d.3).
**ADR-0005 is executed:** no automatic file deletion — operators own destructive filesystem
actions; rewinds (`reset_gate`/`backtrack`/`reset`) are column-clear only; `invalidate` dormant
(the daemon never calls it, ast-pinned); B7 reap frozen pending sign-off. **B9 is closed:** both
canonical `run.finished` emission points (completion and quarantine) are live in the daemon.

**PLAN B IS UNDERWAY — 3 increments built, committed, and pushed on `main`:** (1) production
detection behind an injected `lister` seam; (2) the ADR-0006 persistence substrate (`Gate.persists`
+ atomic output/checkmark write; the contract is now **eleven fields**); (3) the **`experiment`
gate — gate 0, the first production gate** (real LabKey experiment creation, resume-first +
lookup-before-create closing the C1 duplicate hazard, fail-closed). `pixi run test` → **493 passed**,
lint clean. The **incremental-migration policy** is adopted (⚑3): one satisfaction column per gate,
shipped with its gate — so the ladder is now at **007** (`labkey_registered`) and each later gate
ships its own (download → 008, checksum → 009, …). **Next: the `download` gate** (multi-TB rsync,
NOT Globus). python-poller keeps running untouched throughout; sluice stays observe-only until
Plan A+B reach parity.

**PUBLICATION STATE (verified 2026-09-02).** There IS a git remote: `origin` =
`git@github.com:wkgardner/sluice.git`, and **`main` tracks `origin/main` at `0bc8858`**.
Everything through `0bc8858` is published. **The current branch
`config-toml-and-nvd-upload-targets` is NOT** — `9f91cee`, `90f2887`, `2c7eb58`, `f50fc4e` and
`d832e09` exist only in this working copy. Older entries in this file that say "no git remote
configured" are **stale**; see §4.3 before planning around them.

---

## 2. Two environments (critical context)

There are **two working copies** of the sluice repo plus the live production repo. Know which you're
in before acting.

| Environment | Path | Role |
|---|---|---|
| **macOS (osx-arm64)** | `/Users/willgardner/Bioinformatics/sluice` | ⭐ **Currently the most advanced copy.** Bootstrap executed; 12 agents, 2 guardrail hooks, subsystem map all present. |
| **CHTC (linux-64)** | `/home/wkgardner/sluice` | Fresh clone of published `main` (PR #2 merged) as of 2026-09-04, configured for production — see §10 entry 3y. **Also hosts the live production `python-poller`** at `/home/wkgardner/python-poller`. |
| **Production** | `/home/wkgardner/python-poller` (CHTC) | **LIVE. DO NOT DISTURB.** Last poll 2026-07-09; 69 runs; dirty working tree. sluice is additive; python-poller is never modified as part of this work. |

**Sync direction has flipped.** The 2026-07-17 handoff had CHTC as the working session; as of
2026-07-20 macOS carries the bootstrap, the agent team, and the hooks. **Sync macOS → CHTC**, not
the reverse. Note that `.claude/` (agents, hooks, settings) is part of what must be synced — the
production/secrets hook only protects the copy it is installed in, and `.claude/` is **not** in
the remote, so cloning from `origin` does not carry it.

**The shared remote now exists** (`origin`, §4.3), so CHTC can `git clone`/`git pull` for
everything that is tracked rather than being rsynced. That covers `main` through `0bc8858` only;
the current branch is unpublished, and `notes/`, `specs/`, `docs/dev-cycle/` and `.claude/`
remain untracked by design and still have to move out of band.

---

## 3. Document set (the map)

All under `docs/superpowers/`.

| Doc | Location | macOS | CHTC (as of 07-17) |
|---|---|---|---|
| Architecture design of record | `specs/2026-07-16-sluice-architecture-design-of-record.md` | ✅ | ✅ |
| Transition / bootstrap design | `specs/2026-07-17-sluice-transition-bootstrap-design.md` | ✅ | ✅ |
| Spec errata & open gaps | `specs/sluice-spec-errata.md` | ✅ | ✅ |
| This handoff | `HANDOFF.md` | ✅ | ⚠️ older revision |
| Subsystem map (deep analysis) | `notes/python-poller-subsystem-map.md` | ✅ (1149 lines) | ⛔ missing — sync from macOS |
| Agent team roster + loop | `.claude/agents/README.md` | ✅ | ⛔ missing |
| Subsystem plans | `docs/superpowers/plans/` | ⛔ none yet — Plan A starts here | ⛔ |
| 4 historical specs | (in python-poller) | 🚫 **deliberately NOT ported** | 🚫 same |
| 2 superseded control-core specs | — | 🚫 **do not exist** (subsystem map Appendix B: definitively not found) | 🚫 |

**Change from the 07-17 plan:** porting the 4 historical specs was **dropped by decision on
2026-07-20** — their actionable content is already mined into the errata (B4/D3/C5, A7/D2, B8/B9/
D5/D6, B1/C2/C3), and the originals remain in python-poller if ever needed. The "2 superseded
control-core specs" line is retired entirely: they were never on disk in any form.

---

## 4. Reconciliations & corrections

### 4.1 — 2026-07-17 (CHTC session)

1. **No `new-spec.md` exists in python-poller.** The transition draft claimed the design of record
   was rescued from an untracked `new-spec.md`. A tracked+untracked search finds none. The design of
   record is the architecture spec **authored in the CHTC brainstorming session on 2026-07-16**.
   **Action still open:** confirm the two working copies are byte-identical (§10).
2. **Filename standardized.** CHTC's `2026-07-16-sluice-architecture.md` → renamed to
   `2026-07-16-sluice-architecture-design-of-record.md` so all references resolve in both copies.
3. **Prior-plan count corrected: 5, not 3.** The disk-halt HALT decision lives in
   `2026-06-22-quarantine-disk-halt` (mined into errata B5).
4. **The "4 prior design specs" are named + dispositioned** (transition §5): 2026-06-08 (RESTORE),
   2026-06-22-nvd (RESTORE), 2026-06-24 (CARRY), 2026-06-25 (DECISION).
5. **Errata high-signal factual flips to remember** (don't rebuild on the wrong premise):
   - **`busy_timeout` is already 5000** (Python's `sqlite3.connect(timeout=5.0)` default) — errata
     A1. **The real unmade change is `synchronous=NORMAL`** (currently FULL).
   - **`lifecycle`, `protected_*`, `run_from_step`, `run_from_fresh`, `globus_task_id` are all
     net-new** — there are no "scattered `held_*`/`skipped_*` columns" to collapse (A2), and
     `locked_at` is a *processing mutex*, not operator protection (A3, dropped in favor of the
     single-daemon `flock`).
   - **`globus_task_id` persistence is the single highest-value new column** (A4): without it a
     restart re-submits a duplicate multi-TB transfer.

### 4.2 — 2026-07-20 (macOS session)

6. **Bootstrap executed on macOS** (transition §4/§7). `pixi run test` green on osx-arm64:
   Python 3.14.6, pytest 9.1.1, 1 passed. Details in §9.
7. **`pixi.toml` corrected at birth:** workspace renamed `sluice`, `platforms` now
   `["linux-64", "osx-arm64"]`, author set to an institutional identity. CI pins
   `pixi-version: v0.72.1` to match the lockfile that was actually generated (the carried-over
   value `v0.63.2` would have failed `locked: true`).
8. **Slack token path narrowed to env only.** `slack_bot_token` is deliberately **absent** from
   `config.example.json` so the token cannot land in a config file (transition §6 picked one path;
   this enforces it).
9. **Historical-spec porting dropped** — see §3.
10. **Guardrails §8 are now partly MECHANICAL, not just prose.** Two hooks in
    `.claude/settings.json` (§9b) enforce "never modify python-poller" and "never read the secret
    files" at the tool-call layer. Verified live, including a caught real violation.
11. **Agent roster diverged from §9a as written** — 12 agents on disk vs 9 documented, with three
    genuine conflicts. Resolved/flagged in §9a below.
12. **`agentic-workflow/` is not in the macOS python-poller** (checked). Storing build tooling
    inside the live production repo also sits awkwardly against guardrail §8; the agents now live in
    `sluice/.claude/agents/` where they are registered and dispatchable.

### 4.3 — 2026-09-02 (macOS session)

13. **⛔→✅ THE "NO GIT REMOTE" CLAIM IS FALSE AND HAS BEEN FOR A MONTH.** `origin` =
    `git@github.com:wkgardner/sluice.git` exists, `main` tracks `origin/main`, and **all of
    `main` through `0bc8858` is published**. The remote was added around 2026-08-01 without a
    handoff update; entry 3o noticed it and flagged the earlier entries stale, but §2 and §10
    item 4 were never corrected, so the file kept telling a cold reader that the entire history
    lived on one laptop. **That is the wrong risk model to plan against** — it makes a
    single-machine backup look urgent when it is done, and it makes "rsync macOS → CHTC" look
    like the only sync route when `git clone` now covers everything tracked.
    - **Dated ledger entries are NOT rewritten.** The "no git remote configured" standing item in
      3d, 3e, 3g, 3h, 3i, 3j and 3n — **and every "on `main`, not pushed" in 2 through 3r** — was
      *true on the date each was written*. Editing them would trade one false record for another.
      They are superseded by this entry; read them as history, not as current state.
      **Everything described in entries 2 through 3t is now published.** Only 3u, 3v and 3w
      describe unpushed work.
    - **What is corrected in place** is every statement that a reader would act on *now*: §1
      (publication state), §2 (sync route), the 3j "front line" bullet, and §10 item 4.
14. **The current branch is unpublished, and that is the live gap.**
    `config-toml-and-nvd-upload-targets` carries five commits that exist nowhere else —
    `9f91cee`, `90f2887`, `2c7eb58`, `f50fc4e`, `d832e09`. Two of them (`9f91cee`, `90f2887`)
    contain a production bug fix and a data-corruption guard. **The one-machine risk did not
    disappear; it moved from the whole history to this branch.** No push has been authorized, so
    it stays that way until Will asks for one.
15. **Suite and lint verified independently at this correction:** `pixi run test` → **747
    passed**, `pixi run lint` → clean, both re-run by the steward rather than taken on report.

---

## 5. Locked decisions (condensed)

**Architecture spine** (full detail in the design of record §2):
- DB (SQLite/WAL) is the **single source of truth**; no authoritative in-memory state.
- **State-only control:** every operator action only *writes state* (+ audit + event); it never
  starts/stops work.
- **The daemon is the only actor**; it **reconciles** — recomputes the entire to-do from state each
  look, holding no memory between looks.
- **One-way dependency:** `gates → core`, never the reverse (enforced by
  `tests/test_dependency_arrows.py`, and by a PostToolUse hook on every `sluice/core/` edit).
- **Two write paths:** control writes go through the API (Plan C); the daemon writes its own progress
  **directly** (never depends on the API being up).
- **Holds are poll-only, no signals** (decision A) — tight stage checkpoints make a hold land in
  seconds.
- **Gate = a frozen dataclass of functions** (`run`/`invalidate`/optional `terminate` +
  `needs`/`produces`/`order`/`satisfied_column`); applicability lives in the **profile**, not the gate.

**Transition** (full detail in the transition design §2):
- **Fresh git history**; **private first, public after Plan A**; **greenfield DB + drain** (no
  state-migration code); **keep both repos indefinitely**; **name:** `sluice` (repo == package == CLI).

---

## 6. Roadmap

```
[approved] Architecture design of record
[DONE on macOS] Transition / bootstrap  ──►  repo skeleton, CI, config schema, lockfile, arch guard
                                          │   (initial commit still pending)
                                          ▼
   Plan A — Control core  (built subsystem-by-subsystem via the agent team, §9a)
     db · gate contract + GateResult · reference/mock gates · context/resolve_context ·
     profile · reconcile · audit · events · control ops · daemon
     └─ resolves errata §B gate-model + §A schema facts
                                          │
                                          ▼
   Plan B — Production gates (download·checksum·merge·nvd·globus·registration)
     └─ resolves errata §C correctness hazards + §D dropped production knowledge
                                          │
                                          ▼
   Plan C — API (FastAPI, the control write path) + sluice CLI (Typer/rich HTTP client)
     └─ resolves errata §E operational gaps (supervision, `sluice doctor`)
```

⚠️ **Plan C is SUPERSEDED as drawn — see §10 entry 3v.** Everything it was going to deliver has
landed without it: the CLI was built on Typer/rich but talks to `control` **directly**, not over
HTTP (3p); `sluice doctor` is built (3r, E2); daemon supervision is decided (3v, E1); and
ADR-0007 rejected the API as the route to multi-user access. The only residue is a FastAPI
service for operating sluice from a laptop without SSHing to CHTC, which **nobody has asked
for**. Treat Plan C as **deliberately unbuilt**, not as pending work.

---

## 7. Open decisions, indexed by the plan that resolves them

From the errata. None block the bootstrap.

- **Plan A (control core):** gate model — 6 named vs 8 live stages; is `registration`
  experiment-creation or authority-list (B1); where `experiment_id` is stamped (detection vs gate 0)
  and the chicken-and-egg with `resolve_context` (B2); `Gate.order` from true execution order, NOT
  `PIPELINE_GATE_COLUMNS` (B3, or it deadlocks); retry/quarantine policy — add `retryable` +
  `max_attempts` or accept all-fail-quarantine + delete `reset_attempts` (B4); disk-halt →
  auto-pause reversal + what clears it (B5); per-gate `timeout_seconds` for merge (B6); `events.py`
  decoupled from gate exceptions + Slack before it enters core (B8); reconcile-loop wide-event model
  — one canonical line at `next_gate == None` (B9); `synchronous=NORMAL` (A1).
- **Plan B (production gates):** raw-FASTQ cleanup as its own `reap` gate so `merge.invalidate` stays
  honest (B7); LabKey `create_experiment` idempotency/race (C1); split the overloaded `labkey_project`
  key (C2); `fetch_registered_labels` must fail **closed** (C3); final gate must not stamp complete
  when Globus is skipped (C4); restore fail-loud LabKey semantics (C5); CephFS xattr disk floor
  (D1); NVD launch dir must be local POSIX, validated at load (D2, A7 — highest-risk NVD port item);
  NVD append-mode logs (D3); download ramp (D4); heartbeats + deployment context (D5); threadpool
  contextvar landmine (D6).
- ~~**Plan C (API + CLI):**~~ **BOTH CLOSED, and Plan C itself is deliberately unbuilt (§6, §10
  entry 3v).** Daemon supervision (**E1**) — resolved 2026-09-02 as screen, recorded in ADR-0007
  (3v). `sluice doctor` preflight for Globus token / CephFS xattrs / `nvd` CLI / Slack token
  (**E2**) — resolved in part 2026-08-03 by doctor increment A (3r); the network tier that
  actually reaches those services is increment **B**, still unstarted.

---

## 8. Guardrails (must not violate)

- **python-poller is live production — never modify it** as part of sluice work. Reading it is
  required for the Plan B port and is fine; writing is not. 🔒 *Now hook-enforced (§9b).*
- **Never commit** infra values, secrets, or production state into sluice. Real values live only in
  gitignored `config.json` + untracked secret files / env. `config.example.json` is placeholders
  only. State DB + logs are gitignored (no `chore: update db` pattern).
- **No production defaults baked into code** as fallbacks — a missing required config key is a
  load-time error, not a silent prod fallback.
- **`data/labkey_api.txt` and `data/mizzou.txt`** (in python-poller) are secrets — never read/display.
  🔒 *Now hook-enforced (§9b), including via shell commands that merely name them.*
- **Single-host DB invariant:** the SQLite file is only ever opened from one host; never place it on
  a share other machines mount.
- **`.gitignore` is the user's** — the user owns it going forward. *(Re-verify this line against the
  current file when next editing; it has changed since 07-17.)*
- **Commit only when the user asks.** Enforced by convention in every agent prompt and by
  `git-steward`'s absolute consent rule.

---

## 9. Current status — what physically exists

### macOS (`/Users/willgardner/Bioinformatics/sluice`) — as of 2026-09-02

**STAMP: `pixi run test` → 747 passed, `pixi run lint` → clean, both guards green (2026-09-02,
re-run by the steward, not taken on report).** Branch `config-toml-and-nvd-upload-targets`;
`main` published at `0bc8858`, this branch is not (§4.3). The suite progression below runs
76 → 131 → … → 493 and then continues in the ledger rather than here; the tail is
**493 → 517 → 654 → 656 → 719 → 720 → 730 → 745 → 747** (entries 3n, 3p, 3q, 3r, 3s, 3u, 3v,
3w; 3t is documentation-only and moves nothing). The narrative below this stamp is accurate
except where a later entry supersedes it.

**PLAN A COMPLETE — all nine subsystems (db, gate, events, context, profile, reconcile, audit,
control, daemon) complete, committed, and green. `pixi run test` → 435 passed (Python 3.14.6,
pytest 9.1.1); `pixi run lint` clean (including format); dependency-arrow guard green
(auto-covering `sluice.core.daemon`). The final commit is tagged `plan-a-rc1` (annotated) —
release candidate pending Plan B production gates.**

- `git init` on `main` — ✅ **initial commit exists:** `638b7482` *(bootstrap: repo skeleton,
  config schema, CI, guardrails, db unit work)*, 24 files, on `main`, not pushed. db units 1–3
  present and green at bootstrap.
- ✅ **db subsystem complete and committed** 2026-07-21 on `main`, not pushed. Three commits:
  - `b65fd1d4` *(docs: point db plan tests at the consolidated test_db.py)* — plan reconciled to
    the single `tests/test_db.py` (the per-file `test_db_migrations.py`/`test_db_schema.py` split
    the plan sketched was never adopted).
  - `fdb7471c` *(db: complete the SQLite state store (engine, schema, primitives))* — units 4–13:
    forward-only migration engine (`schema_version`, `apply_migrations`, `initialize_database`,
    `SchemaVersionError`), schema migrations 001–004 (`runs`, `poller_metadata`, `control_audit`,
    `idx_runs_eligible`), row primitives (`insert_run`, `get_run`, `update_run` +
    `UnknownColumnError`), the F4 eligibility predicate (`select_active_runs`,
    `ACTIVE_RUNS_WHERE`), and metadata primitives, plus subsystem integration tests (two-writer
    `BEGIN IMMEDIATE` contention, lifecycle journey). Resolves A1/A2/A3/A4, B1/B2, F1/F2/F4.
    This commit also carried the doc/test-verbosity trim (units 1–3 docstrings compressed;
    over-tested trivial cases dropped — `now_iso` 6→2, the `busy_timeout` documentation-test, the
    `render_timestamp_like_now_iso` ordering helper) and the test-loop recalibration behind it.
    A green code+tests hunk-split was not forced: `git add -p` is unavailable in the build
    environment, the two files' module-header hunks are interleaved, and the test file resolves db
    functions by module attribute (so a partial split would ship a lying `__all__`) — one honest
    coherent commit beat several fragile ones.
  - `1d998b8` *(docs: record db subsystem complete in handoff)*.
- Suite grew 76 → 131 passed with the db subsystem tests.
- ✅ **ADR-0001 (docstring standard) applied + ruff adopted, committed** 2026-07-21 on `main`, not
  pushed. Two commits:
  - `b427104` *(docs(db): docstrings per ADR-0001 — contracts into sections, justifications to the
    plan)* — `sluice/core/db.py` docstrings brought to Google style, contract facts into
    `Args:`/`Returns:`/`Raises:`/`Yields:` sections, justification prose removed (it lives in
    `docs/superpowers/plans/01-db-plan.md`); ~60 lines shorter, no behavior change.
  - `8b508d6` *(build: adopt ruff (lint + format) — defaults + D/google, CI gate)* — `ruff.toml`
    (defaults + `D`, `convention=google`, `D417` ignored per ADR rule 2, `D1` relaxed under
    `tests/`), pixi `lint`/`format` tasks + `ruff >=0.14,<0.15` (lock regenerated), CI lint gate
    before the test step, `.gitignore` allowlist for `/ruff.toml`, one-time format pass on the two
    test files. `pixi run lint` and 131 tests green under the new gate.
- ✅ **gate subsystem (2 of 9) complete and committed** 2026-07-22 on `main`, not pushed
  (plan: `docs/superpowers/plans/02-gate-plan.md` — 9 TDD units, per-unit adversarial review,
  approvals recorded in the plan). One commit:
  - `bb22eec` *(gate: add contract records, validate_gates, mock gates, migration 005)* —
    `sluice/core/gate.py` (`GateOutcome`, `GateResult`, the frozen ten-field `Gate`,
    `validate_gates` returning TRUE execution order — the mechanical B3 guard),
    `sluice/core/mock_gates.py` (`MOCK_ALPHA`, `MOCK_BETA`, `MOCK_GATES`; sqlite3 and
    `sluice.core.db` structurally unreachable per ADR-0002 d.2), migration **005** appended to
    core `db.py` (`mock_alpha_done`/`mock_beta_done`, `INTEGER NOT NULL DEFAULT 0`; F3-revision
    approved — gate-column migration fns live in core's ladder, precedent for 006+),
    `tests/test_gate.py` (~41 tests incl. the two closing review findings), the two
    pre-authorized `tests/test_db.py` edits (relocated F3 guard; end-state test now asserts
    version 5), and a `tests/test_dependency_arrows.py` teardown fix (restores `sys.modules` so
    enum `is` comparisons survive the blocker test). Resolves errata B3/A6, B2 contract half,
    B4 via ADR-0002, B6 deferred by approved flag; B1 not pre-empted. Suite 131 → 171 passed.
- ✅ **events plan (subsystem 3) reviewed, amended, fully approved, committed** — `e59ae20`
  *(docs: add events plan (03) — notifier contract and wide-event model)*. All five sign-off
  flags user-decided (⚑1–⚑4 2026-07-21, ⚑5 2026-07-22): quarantine as second canonical emission
  point; `quarantined` → severity `critical`; `halted` dropped; `Notifier` fixed at three
  methods; **daemon-only notifier model approved (⚑5)** — Plan A's `RunContext` carries no
  notifier field (spec §6 revised). B8 fully resolved by the plan's design; **B9 explicitly not
  closed** — emission wiring lands with subsystems 6/9.
- ✅ **events subsystem (3 of 9) complete and committed** 2026-07-23 on `main`, not pushed
  (plan: `docs/superpowers/plans/03-events-plan.md` — 8 units, reviewed per-unit). One commit:
  - `0db7a27` *(events: notifier contract, safe_notify, wide-event builders/emitters)* —
    `sluice/core/events.py` (`Notifier` Protocol — runtime_checkable, three run-lifecycle
    methods; `safe_notify` — notification failure never fails work, one `notify.failed` error
    line; `run_outcome` + `SEVERITY_BY_RUN_OUTCOME` — closed five-outcome vocabulary,
    quarantined=critical, no halted, no retry tier; `build_run_event`/`emit_run_event` —
    canonical `run.finished` as a pure function of a runs row, two emission points per ⚑1;
    `build_gate_event`/`emit_gate_event`; `control_event` — no notifier parameter, §11
    structural; `format_duration`; structlog per ADR-0003) and `tests/test_events.py`
    (24 test functions / 35 cases, incl. the zero-ms duration pin and mutation-witnessed
    additions; ast-parse import hygiene; real-schema `sqlite3.Row` integration). All five
    plan flags user-approved. Resolves B8; B9 design-resolved, wiring deferred to 6/9.
    Suite 171 → 206 passed.
- ✅ **context plan (subsystem 4) reviewed and committed** — `39597e0` *(docs: add context
  plan (04) — lean RunContext, pure resolve_context)*. Adversarially reviewed (zero confirmed
  rubric findings), four LOW amendments applied. Encodes the four 2026-07-22 user-decided
  facts (no notifier field; LEAN three-field record; pure `resolve_context(row, config)`;
  resume tokens never carried/recomputed — A5/A7/D2, `__file__` ban). ⚑1 (`workdir` keyed
  on `run_row["name"]` rather than `experiment_id`, the B2 sidestep) was subsequently
  **closed — APPROVED 2026-07-23**; see the context subsystem entry below.
- ✅ **context subsystem (4 of 9) complete and committed** 2026-07-23 on `main`, not pushed
  (plan: `docs/superpowers/plans/04-context-plan.md` — 3 units, reviewed per-unit). Two commits:
  - `56bc950` *(context: frozen RunContext and pure resolve_context)* —
    `sluice/core/context.py` (frozen `RunContext(workdir, db_path, config)`, LEAN closed field
    set — no notifier per events ⚑5, no Plan B path fields — with `config` sealed read-only at
    construction via dict-copy + `MappingProxyType`; pure `resolve_context(run_row, config)` —
    name-keyed `workdir` under `config["local_staging_dir"]` **only** (⚑1 approved; CHTC has no
    `/scratch`, so never a scratch/system-temp default), traversal-name `ValueError` defense at
    the identity→path boundary, fail-loud `KeyError` on missing config keys, no
    mkdir/DB/`.resolve()`) and `tests/test_context.py` (15 test functions / 21 cases across the
    3 units: ast-parse import hygiene incl. the `__file__` ban, the closed/frozen/read-only
    record pins, the derivation/purity/determinism pins, and the unit-3 integrations — a real
    `sqlite3.Row` from `get_run` drives the derivation and a resolved context drives
    `MOCK_ALPHA.run(ctx)` to SUCCESS). Resolves errata A5, A7/D2, and B2 for Plan A.
    Suite 206 → 227 passed.
  - `ce4b872` *(docs: stamp context-plan flag 1 approved; record no-/scratch constraint)* —
    the four LOW review amendments plus the ⚑1 stamp (APPROVED 2026-07-23, decided by the user,
    with the Plan B note that `experiment_id` keys production destination paths and leads
    user-facing surfaces once it exists) and the CHTC no-`/scratch` constraint encoded in the
    plan's invariant table, test sketches, and acceptance criteria.
- ✅ **profile subsystem (5 of 9) complete and committed** 2026-07-24 on `main`, not pushed
  (plan: `docs/superpowers/plans/05-profile-plan.md` — 7 units, reviewed per-unit; both human
  sign-off flags stamped 2026-07-23). Two commits (plus this handoff commit):
  - `3ed1f6e` *(profile: applicability, frozen Profile, load_profile freeze)* —
    `sluice/core/profile.py` (`Applicability` closed vocabulary `required|optional|disabled`;
    frozen validated `Profile` — `validate_gates` in `__post_init__`, applicability covers the
    gate ids exactly in both directions, every value an `Applicability` member, `applicability`
    sealed read-only via `MappingProxyType(dict(...))`; `build_profile` pair-splitter;
    `required_gates` completion-predicate seam with optional/disabled excluded; `load_profile` +
    `UnknownProfileError` — the freeze, no default registry / no fallback, plus the per-run
    registry-coherence `key==id` check; the two shipped reference profiles `REFERENCE_PROFILE` +
    `REFERENCE_PROFILE_ALPHA_DISABLED` in `REFERENCE_PROFILES`, ⚑2) and `tests/test_profile.py`
    (7 units — ast-parse import hygiene incl. the `sqlite3`/`db` structural freeze backstop, the
    B3-order / exact-coverage / read-only record pins, the freeze + coherence pins, the
    two-profile registry, and the real-`sqlite3.Row` integration resolving two runs to two shipped
    definitions while a drifted version fails loud). Import set is closed to stdlib +
    `sluice.core.gate` + `sluice.core.mock_gates`, so the module structurally cannot read
    `metadata.pipeline_profile` (the freeze, §8). Resolves B1 (mechanism —
    applicability-models-config-switches; `required_gates` excluding disabled is the
    `nvd_auto_run=false` loop-forever fix); extends the B3/A6 execution-order guard into every
    profile; B4 cited via ADR-0002 (no retry field, closed vocabulary). ⚑1 APPROVED (deadlock
    rule deferred to reconcile with the definition of "due"), ⚑2 DECIDED (ship two profiles).
    Suite 227 → 253 passed.
  - `5a96646` *(docs: add profile plan (05) — reviewed, both flags stamped)* — the reviewed,
    amended plan, tracked via the plans allowlist.
- ✅ **reconcile subsystem (6 of 9) complete and committed** 2026-07-28 on `main`, not pushed
  (plan: `docs/superpowers/plans/06-reconcile-plan.md` — the ADR-0004 rewrite, re-reviewed, all
  three ⚑ flags stamped 2026-07-27; 4 units incl. unit 0, reviewed per-unit). The 2026-07-24 pause
  resolved (§10 item 3f): the user rejected the runtime optional tier outright, recorded as
  **ADR-0004 (binary contracts)** — a run's contract is binary (every contract gate mandatory,
  excluded gates absent; no optional tier); **any** contract gate returning SKIPPED quarantines
  (d.4, closing C4); remediation is exclusion via the subsystem-8 op (d.3); trait-based contract
  construction is Plan B's (d.1/d.3). Three commits (plus this handoff commit):
  - `8127aab` *(profile: remove the OPTIONAL applicability tier (ADR-0004))* — unit 0:
    `Applicability.OPTIONAL` removed from the committed profile subsystem (vocabulary now
    `required|disabled`; `required_gates` = the contract). Exactly the five pre-authorized
    structural `tests/test_profile.py` edits + six named docstring-only touches (incl. the single
    `tests/test_gate.py` line), verified by the unit-0 reviewer — zero findings, every hunk
    matched to an authorization; both shipped reference profiles unchanged.
  - `44b44cf` *(reconcile: pure is_complete and next_gate over binary contracts)* —
    `sluice/core/reconcile.py`: pure `is_complete(run_row, profile)` and
    `next_gate(run_row, profile)`, both behind the shared up-front
    `_require_gate_columns_present` fail-loud pre-check (defeats the `all()` short-circuit;
    uniform `KeyError` across `dict` and `sqlite3.Row`); import set closed to stdlib +
    `core.gate` + `core.profile` with every clock/db/entropy/network module forbidden by name
    (purity structural); no optional/due/retry logic — the binary model. `tests/test_reconcile.py`:
    purity/import hygiene, signature pins, the completion truth table,
    disabled-satisfies-by-absence, selection-identity cases, the exhaustive
    `next_gate is None ⟺ is_complete` theorem over `{0,1}^n` × four contract shapes,
    real-`sqlite3.Row` integrations driving both shipped profiles to completion, and
    stale-`completed_at` re-derivation. Resolves errata C4's reconcile half; discharges plan 05
    ⚑1 as moot. Suite 253 → 304 passed.
  - `a52940f` *(docs: add reconcile plan (06) — ADR-0004 rewrite, all flags stamped)* — the
    reviewed plan, tracked via the plans allowlist.
- ✅ **audit subsystem (7 of 9) complete and committed** 2026-07-28 on `main`, not pushed
  (plan: `docs/superpowers/plans/07-audit-plan.md` — 3 units, reviewed per-unit; both ⚑ flags
  decided by the user 2026-07-28, **both against the plan's recommendation**, reasoning preserved
  in the sign-off stamps). Two commits (plus this handoff commit):
  - `bccd6f8` *(audit: append-only control_audit writer record_control_action)* —
    `sluice/core/audit.py` (`record_control_action(conn, *, actor, action, target_run=None,
    args=None, result=None, ts=None) -> int` — the caller owns the connection and transaction:
    the writer never opens, commits, or rolls one back; imports closed to
    `json`/`sqlite3`/`collections.abc` + exactly `now_iso` from db, so
    `get_connection`/`write_txn` are structurally unreachable; `args_json` via
    `sort_keys=True`/`ensure_ascii=False`/`allow_nan=False`/no `default=` hook behind a
    recursive str-keys-only walk — loud `TypeError`/`ValueError` at any depth, nothing silently
    rewritten or truncated; blank actor/action refused; `ts` defaults to `db.now_iso`) and
    `tests/test_audit.py` (10 test functions across 3 units: the writer contract,
    order-by-`id`-never-`ts` incl. the NTP step-back wall-clock lie, the
    never-commits/rollback probes, the §9 state+audit atomicity choreography both directions
    over the real schema, run-deletion survival — `target_run` is a name, no FK — and the
    `__all__`/no-mutation-surface guard, THE append-only enforcement per ⚑1). Resolves the
    plan-01 §1 deferral ("Writing control_audit rows — subsystem 7"). Suite 304 → 321 passed.
  - `a549269` *(docs: add audit plan (07) — writer only, both flags stamped)* — the reviewed
    plan (three confirmed review findings applied), tracked via the plans allowlist. ⚑1: no
    migration 006, no DB-level append-only triggers — code promise only, the ladder stays at
    005 (a `BEFORE DELETE` trigger could not coexist with the db suite's executable
    AUTOINCREMENT delete-then-insert proof); Plan B's gate columns take 006+. ⚑2: no reader —
    deferred to Plan C with its first caller (`sluice audit`), the ORDER BY `id` rule a named
    carry-forward.
  - Errata note: `specs/sluice-spec-errata.md` (gitignored with the other specs — this working
    copy only) gained **§F** — the F-ADR4 record that ADR-0004 supersedes the affected spec
    passages.
- ✅ **control subsystem (8 of 9) complete and committed** 2026-07-29 on `main`, not pushed
  (plan: `docs/superpowers/plans/08-control-plan.md` — all three ⚑ flags decided by the user
  2026-07-29). **First subsystem built in the NEW LIGHTWEIGHT MODE** (see §9a — one
  implementation pass + one adversarial review sweep, user-decided 2026-07-29 for token
  economy). Two commits (plus this handoff commit):
  - `34a1992` *(control: operator ops, exclusion overlay, migration 006)* —
    `sluice/core/control.py` (29-name surface; all operator ops: pause/resume, hold/unhold,
    skip/unskip, `clear_error`, protect/unprotect, `set_default_profile`, `set_gate` — B2
    refuse-to-fabricate + `force`; `exclude_gate`/`include_gate`/`run_exclusions` — the
    ADR-0004 d.3 remediation-is-exclusion mechanism; `reset_gate`/`backtrack` — ADR-0005
    column-clear only, `ActiveRunError` on eligible runs; `reset`; `get_status` — total under
    corruption, `experiment_id` always surfaced). Single-transaction mutate → audit → commit →
    event choreography throughout; refusals audit-then-raise; zero file I/O (ast-enforced).
    Plus **migration 006** (`runs.excluded_gates`, NULL-or-valid-JSON checked; ladder comment
    now points later subsystems at 007+, superseding F-AUD1), the `set_metadata`
    batch-inside-`write_txn` docstring (plan 08 F7), `profile.apply_exclusions` (the per-run
    contract overlay — ten-name surface now), `tests/test_control.py` (69 tests incl. the
    end-to-end operator journey, the nine-op artifacts-intact sweep — ADR-0005 executable —
    the AND-ed recovery test, and the protection-precedence pin), and exactly the plan's
    pre-authorized edits to `tests/test_db.py`/`test_gate.py`/`test_profile.py` (verified
    hunk-by-hunk by the adversarial review). Resolves errata B2. Suite 321 → 394 passed.
  - `8645689` *(docs: add control plan (08) — lightweight mode, all flags stamped)* — ⚑1
    backtrack/fastforward approved as amended by ADR-0005; ⚑2 exclusion op approved
    (migration 006); ⚑3 quiescence superseded by ADR-0005, residual posture recorded.
- ✅ **daemon subsystem (9 of 9) complete and committed — PLAN A COMPLETE** 2026-07-30 on
  `main`, not pushed (plan: `docs/superpowers/plans/09-daemon-plan.md` — lightweight mode:
  one-pass build + single adversarial review, APPROVE with two test-only fixes applied; both
  ⚑ flags APPROVED by the user 2026-07-29). Two commits (plus this handoff commit, which
  carries the annotated tag `plan-a-rc1`):
  - `e5d4c6b` *(daemon: reconcile loop — detect, drive-until-blocked, cycle, logging)* —
    `sluice/core/daemon.py` (`acquire_daemon_lock` — single-instance is the flock alone, the
    A3 lesson; `assemble_registry` — duplicate-key-loud registry union; `prepare_database` —
    migrate + seed/validate `metadata.pipeline_profile`, a bad active default is one startup
    error; `detect_new_runs` — single-txn detection, the ONLY read site of the active default
    for a run; `process_run` — the full outcome matrix, drive-until-blocked per ⚑2, every
    step a stateless re-derivation, §3.5 call-site-scoped isolation catches (contract/context
    resolution quarantines ONE run; reconcile's schema-drift `KeyError` and persist
    `OperationalError` stay fatal), persist-then-emit, SKIPPED-in-contract quarantines per
    ADR-0004 d.4; `run_cycle` — loop-head pause short-circuit, B5 disk-floor auto-pause trio
    per ⚑1 (audited `control.pause` actor `"daemon"` — the ONE sanctioned daemon→control
    call — operator-resume only, check fail-open); `run_forever` — flag-only signal handlers,
    interruptible sleep, `max_cycles`, systemd-ready; `configure_logging` — ISO-UTC structlog
    jsonl, rotating events sink + per-run `run_events_dir/<run.name>.jsonl` routing behind
    the safe-path-component guard). Imports a closed set pinned by ast parse —
    `subprocess`/`shutil`/`slack_sdk` forbidden by name; no file deletion, no `invalidate`
    (ADR-0005). **Closes B9** — both canonical `run.finished` emission points live. Plus
    `tests/test_daemon.py` (41 tests incl. the five `run_cycle` capstones and the two review
    fixes), the pre-authorized one-line F-ADR4 SKIPPED-comment sweep in `sluice/core/gate.py`,
    and `config.example.json` (`watch_dir` added; `folder_events_dir` → `run_events_dir`).
    Suite 394 → 435 passed.
  - `289f7c6` *(docs: add daemon plan (09) — lightweight mode, both flags stamped)* — the
    reviewed plan, READY TO BUILD, tracked via the plans allowlist.
- ✅ **PLAN B OPENED — increment 1 (production detection) built and committed** 2026-07-31 on
  `main`, not pushed. Groundwork, all user-decided: the **Plan B gate map is FINAL**
  (`docs/superpowers/plans/10-plan-b-gate-map.md`, committed `8cb03ef` after redaction — see
  the note below): EIGHT
  gates in true execution order — experiment → download → checksum → samplesheet → registration
  [DISABLED] → nvd → globus_submit → globus_verify — all six ⚑ flags stamped 2026-07-30/31.
  **ADR-0006 accepted** (gate output persistence: `Gate.persists` becomes the eleventh contract
  field; outputs written atomically with the satisfaction checkmark in ONE
  `update_run`/`write_txn`; the globus submit/verify split makes A4's persist-before-wait
  structural; the generic `gate_outputs` table PARKED with named revisit triggers). Errata gained
  **F-B6** (NO time bounds on any gate — user-decided; the hung-gate-blocks-the-cycle consequence
  consciously owned; revisit on a real observed hang) alongside the earlier F-V31 (merge is a
  v3.1 no-op; raw lanes are the only on-disk copy). Two commits (plus this handoff commit):
  - `e37210b` *(detection: rsync lister adapter behind an injected daemon seam)* —
    `sluice/gates/detection.py` (a world adapter, NOT a Gate: pure `parse_rsync_listing` with
    manifest-AND-`*001.fastq.gz` candidacy, stricter than the poller; `build_listing_command`
    pinning the exact poller `--list-only` argv with the sshpass password in the `SSHPASS` env
    overlay, never argv — the poller's process-table exposure fixed; `list_remote_runs` whose
    `RemoteListingError` carries stderr, never argv/password), the daemon's injected-lister seam
    (`watch_dir_lister` extracted as the default — Plan A behavior byte-identical; keyword-only
    `lister=` on `detect_new_runs`/`run_cycle`/`run_forever`; lister called outside the
    `write_txn`; unsafe names dropped with an event; `KeyError` loud, world failures degrade to
    one `detection.failed` + zero candidates), `config.example.json` `remote_source_path`
    placeholder (C2), and the go-live interlock (stamping `production-v3.1` dies loud at startup
    until the profile registry entry lands). Suite 435 → **461 passed**; lint clean;
    dependency-arrow guard green.
  - `4cd9860` *(docs: add detection plan (11) — lightweight mode, no open flags)* — reviewed
    READY, built same session.
- ✅ **PLAN B — increment 2 (ADR-0006 persistence substrate) built and committed** 2026-07-31 on
  `main`, not pushed (plan: `docs/superpowers/plans/12-persistence-substrate-plan.md` —
  lightweight mode, one adversarial review APPROVE, TDD build). This is the FIRST Gate contract
  change since subsystem 2 — the contract is now **ELEVEN fields** — made under ADR-0006
  governance (same as the ADR-0002/0004/0005 pins). **No migration:** the substrate uses the
  existing whitelisted `experiment_id`/`globus_task_id` columns, so the migration ladder is
  **UNCHANGED (still 006)**. The shipped `mock_gates`/`reference-v1` path is untouched (Plan A
  frozen) — proven with test-local gate fixtures. Two commits (plus this handoff commit):
  - `80025ad` *(gate: add persists field; daemon persists outputs atomically on SUCCESS)* —
    `sluice/core/gate.py` (ADR-0006 d.1: the eleventh field `persists: Mapping[str, str]`,
    default empty, sealed MappingProxyType over a dict copy; a `validate_gates` clobber guard
    rejecting any gate that persists into its own `satisfied_column` — the bad state made
    unrepresentable), `sluice/core/context.py` (ADR-0006 d.3: `RunContext` gains the sealed
    read-only `run_row` snapshot — OPTIONAL with an empty-proxy default so the two direct
    constructions stay green untouched; `resolve_context` captures it via a keyed comprehension,
    never `dict(<Row>)`, so a resuming gate reads its prior tokens with no DB touch —
    carrying `db_path` still licenses no gate write, ADR-0002 intact), and `sluice/core/daemon.py`
    (ADR-0006 d.2: on SUCCESS the daemon writes every declared output AND the satisfaction column
    in ONE `update_run`/ONE `write_txn`; a declared key absent from `result.metadata` quarantines
    the one run via a synthetic FAILURE so `gate.finished` reads honestly; a persists column
    absent from the runs whitelist raises `UnknownColumnError` and fails loud, the txn rolling
    back). Tests: the field-count pins updated 10→11 (`test_gate.py`) and 3→4 fields
    (`test_context.py`, `run_row` added); `test_resolve_context_ignores_resume_tokens` narrowed
    per ADR-0006 d.3 (which deliberately overturns the old "context ignores tokens" assertion) to
    "derivation ignores tokens + snapshot carries them"; plus the new behavior/atomicity/guard
    tests. Suite 461 → **469 passed**; lint clean; dependency-arrow guard green.
  - `dd8e643` *(docs: add persistence substrate plan (12) — lightweight, ADR-0006 as spec)* —
    reviewed, revised, built same session.
- ✅ **PLAN B — increment 3 (the `experiment` gate, gate 0) built, reviewed APPROVE, and
  committed** 2026-07-31 on `main`, not pushed (plan:
  `docs/superpowers/plans/13-experiment-gate-plan.md` — lightweight mode, all three flags
  stamped, one adversarial review APPROVE, TDD build). The **FIRST production gate**: order 0,
  the first to touch an external service (LabKey), the first to persist a load-bearing
  user-facing value (`experiment_id`, Will's reference key). Suite 469 → **493 passed**; lint
  clean; dependency-arrow guard green (auto-covering the new `sluice.gates.labkey`/`.experiment`).
  Two commits (plus this handoff commit):
  - `7461b4f` *(gate: add experiment gate (gate 0) + fail-closed LabKey adapter)* —
    `sluice/gates/labkey.py` (the fail-closed world adapter: `make_experiment_client` /
    `ExperimentClient` / `LabKeyError`; NOT a Gate, on the gates side of the `gates -> core`
    arrow, imports the `labkey` package lazily inside the default api-factory closure so the
    suite runs with `labkey` absent; the api key is a config **path** `labkey_api_key_file`
    read lazily and NEVER logged nor formatted into any message; `LabKeyError` carries only run
    name + exception type; the C2 project value comes from config key
    `labkey_experiments_project`, never hardcoded; `labkey_user_id` dropped from the config
    surface entirely), `sluice/gates/experiment.py` (the `EXPERIMENT` gate + `run_experiment_gate`:
    resume-first — set `experiment_id` → SUCCESS with **zero** client calls; lookup-before-create —
    adopt on find, create only on `None` — the **C1 duplicate-crash-window closure**; fail-closed —
    any `LabKeyError` → quarantine, never a fall-through to create; `produces=("experiment_id",)`
    arms control B2; `persists={"experiment_id":"experiment_id"}` per ADR-0006; `invalidate` a
    deliberate no-op so it can never orphan a real experiment per ADR-0005), migration **007** in
    core `db.py` (single `ALTER TABLE runs ADD COLUMN labkey_registered INTEGER NOT NULL DEFAULT 0`).
    Tests: `tests/test_experiment.py` (24, incl. crash-window / fail-closed / api-key-leak pins,
    all network- and `labkey`-package-free), `tests/test_db.py` (ladder end-state 6→7 + the
    incremental guard: the six OTHER production satisfaction columns ABSENT), `tests/test_control.py`
    (one migration-006 test sliced to `MIGRATIONS[:6]`, semantics-preserving). Resolves errata
    C1, C2, B2; implements ADR-0002/0005/0006.
  - `8f97c12` *(docs: add experiment gate plan (13) — lightweight, all 3 flags stamped)* —
    reviewed READY, built same session; carries NO real C2 project value (config-key pointer only).
  - **⚑3 — INCREMENTAL-MIGRATION POLICY ADOPTED (record for future gate increments):**
    migrations are now **one satisfaction column per gate, shipped with its gate** — 007 adds
    ONLY `labkey_registered`, superseding gate-map §6's single coherent "migration 007". Each
    later gate ships its own: **download → 008, checksum → 009, samplesheet → 010, …** The C1
    duplicate-experiment hazard is now **structurally closed** (lookup-before-create).
  - `8cb03ef` *(docs: add Plan B gate map (10) — FINAL, all six flags stamped, production values
    redacted to config-key pointers)* — the git steward's pre-commit scan initially REFUSED this
    file (it carried real LabKey project paths §4 C2 and the production Globus/CephFS destination
    path §5 ⚑A — the exact leak class that motivated sluice's fresh history). It was then redacted
    at the source: the two LabKey values became `real value at PP <file:line>` pointers and the
    Globus destination became a required config key `globus_destination_root` (no code default,
    same pointer convention). Re-scanned clean and committed. **Lesson recorded:** the fresh-history
    gate works — a document does not enter history until its production values are pointers or
    placeholders.
- ⚠️ **Untracked-by-design, this machine only:** `docs/adr/` — now **SIX** ADRs (the FIVE below
  plus the new **`0006-gate-output-persistence.md`**, accepted 2026-07-31); also `notes/` and
  `specs/` (the persistence research note and the errata's new §F entries — F-V31, F-B6 — exist
  ONLY in this working copy); and the production poller clone now lives at
  `/Users/willgardner/Bioinformatics/mizzou_fastq_automated_download` (read-only ground truth for
  Plan B, hook-protected, refreshed 2026-07-30 — note the path differs from the guard hook's
  legacy `python-poller` alias). Previously recorded FIVE ADRs:
  `0001-docstring-standard.md`, `0002-gate-failure-and-persistence.md` (all failures quarantine;
  gates never write run state), `0003-core-import-policy.md` (structlog allowed in core;
  stdlib-only retired), **and the new `0004-binary-contracts.md`** (a run's contract is binary —
  no runtime optional tier; any contract gate returning SKIPPED quarantines; remediation is
  exclusion via the subsystem-8 op; trait-based contract construction is Plan B's) **and the new
  `0005-no-automatic-file-deletion.md`** (operators own destructive filesystem actions; rewinds
  are column-clear only; `invalidate` dormant; B7 reap frozen pending sign-off) — plus
  `.claude/CLAUDE.md`, the amended `.claude/agents/*.md`
  (implementer/test-author/code-reviewer/docs-builder now enforce ADR-0001), and the
  `docs/dev-cycle/` artifacts (`01-db/` through `07-audit/` — **no `08-control/` artifacts:
  visual docs are optional in the lightweight mode**) are all still behind the deny-all
  allowlist `.gitignore` — the human curates the allowlist. ~~**Standing open item:** no allowlist
  rule for `docs/adr/` yet~~ → **CLOSED 2026-08-01 (§10 entry 3o): `!/docs/adr/*.md` and
  `!/CONTEXT.md` added; all six ADRs are now tracked and in history.** `notes/`, `specs/`, and
  `docs/dev-cycle/` remain untracked by design.
- `pixi.toml` (name `sluice`, `platforms = ["linux-64", "osx-arm64"]`, deps: python 3.14 / pytest 9 /
  structlog) + `pixi.lock` (solved with pixi 0.72.1).
- ~~`config.example.json`~~ → **`config.example.toml` since `9f91cee`** (entry 3u); the JSON file
  is deleted, not merely superseded. **Placeholders only**, `slack_bot_token` deliberately
  omitted, and each key now carries its schema `help` text as a TOML comment.
- `.github/workflows/test.yml` — hermetic `pixi run test`, `locked: true`, pinned `pixi-version: v0.72.1`.
- `tests/test_dependency_arrows.py` — the arch guard; installs a `sys.meta_path` blocker and imports
  every `sluice.core` module with `gates`/`api`/`cli`/**`doctor`** forbidden. **Verified it actually
  fails** on an injected violation. `sluice.doctor` is listed in its own right (2026-08-03): it
  imports gates, so a core import of it would trip the guard only *transitively*, and that
  protection would evaporate the moment doctor made any of those imports lazy.
- `tests/test_guard_hook.py` — 54 cases covering the production/secrets hook on both hosts' paths.
- Package skeleton: `sluice/{__init__,core,api,cli,gates,config}/__init__.py`, `tests/__init__.py`.
- `README.md`, `.gitignore`.
- Docs: design of record, transition, errata, **subsystem map (present here, missing on CHTC)**.
- `.claude/`: 12 agents + README, 2 hooks, `settings.json`, `skills/logging/`.
- ✅ **`sluice/doctor.py` — preflight diagnosis, increment A (local tier)** 2026-08-03, committed
  (§10 entry 3r). 745 lines; `tests/test_doctor.py` (846) + `tests/test_doctor_drift.py` (195).
  Reachable as `sluice doctor`; `--json` supported. **Increments B and C are NOT built** — see 3r.
- ✅ **`install.sh` — `sluice` on PATH** 2026-08-03, committed, allowlisted in `.gitignore`. Writes
  a marked wrapper to `~/.local/bin/sluice`; no sudo, refuses to overwrite a `sluice` it did not
  write, verifies the wrapper runs before claiming success.
- ✅ **`docs/OPERATING.md` — operator guide, now covering how to point sluice at a source**, plus
  `sluice doctor`, `install.sh` and the setup backup. Committed 2026-08-03 (`d194cb0`) **after** the
  steward's scan refused a first draft carrying a real host and account; sanitized and re-scanned
  clean before it entered history. See 3r.

### CHTC (`/home/wkgardner/sluice`) — as of 2026-07-17, unverified since

- `git init` on `main`, no commits. `pixi.toml` ⚠️ `linux-64` only. `.gitignore`, `README.md`.
- Package skeleton present; `notes/` dir exists but **empty**.
- ⛔ Missing: `config.example.json`, CI, `pixi.lock`, the arch-guard test, subsystem map, `.claude/`.

---

## 9a. Build tooling — the agent team

The test-first build team for Plan A lives in **`sluice/.claude/agents/`** and is **registered and
dispatchable** via `subagent_type` from a sluice-rooted session. Roster + orchestration loop:
[`.claude/agents/README.md`](.claude/agents/README.md).

> **Correction to the 07-17 §9a:** the team is no longer a set of drafts in
> `python-poller/agentic-workflow/` (that directory does not exist on macOS, and putting build
> tooling inside the live production repo conflicts with guardrail §8). It is 12 live agents here.

**The canonical roster, model recommendations, subsystem order, and loop live in
[`.claude/README.md`](.claude/README.md)** — deliberately one copy. `.claude/agents/README.md` was a
second copy that drifted within a day; it is now a pointer.

**Roster (10 on disk).** Planning: `plan-author`, `plan-reviewer`. Engineering: `test-author`,
`implementer`. Review: `code-reviewer`, `integration-tester`, `build-verifier`. Support:
`docs-builder`, `teachme`, `git-steward`.

### Resolved 2026-07-20

1. ✅ **`tdd-engineer` retired.** The `test-author` → `implementer` split supersedes it: separating
   who writes the test from who makes it pass removes the grade-your-own-homework bias of one agent
   doing both. The loop never called `tdd-engineer` anyway.
2. ✅ **`core-reviewer` retired, merged into `code-reviewer`.** `code-reviewer` keeps the
   load-bearing independence rule (never feed it the implementer's reasoning) and **absorbed
   core-reviewer's checklist**: the DB-as-only-truth and two-write-paths checks, a concurrency/state
   item (WAL + `synchronous=NORMAL`, `busy_timeout` not miscast as a bug fix per A1, `flock` with no
   leftover `locked_at` mutex per A3), the B4 retry-policy and B8/B9 `events`-core-clean traps, and a
   secrets/production-defaults item. Two reviewers with ~90% overlap meant neither got read
   carefully.

3. ✅ **Subsystem order decided 2026-07-20** (all three conflicts now closed):

   ```
   db → gate (+ one mock gate early) → events → context → profile → reconcile → audit → control → daemon
   ```

   The early mock gate is deliberate: design of record §12 requires reference/mock gates, and they
   are what let the daemon and control ops be tested with no external services. The rejected
   alternative was `db → events → gate → …` (events second), from the retired roster copy.

   **Carry this caveat into the `events` plan (B8/B9).** At position 3, `events` can fully settle
   **B8** — core-clean structlog helpers, an injected `Notifier` Protocol, no gate-exception string
   sniffing — but it **cannot close B9**. The single canonical per-run wide event is emitted at
   `next_gate == None`, which does not exist until `reconcile` is built, so that wiring lands with or
   after `reconcile`. The `events` plan must say so rather than marking B9 resolved.

### The loop, per subsystem

`plan-author → plan-reviewer →` (per unit: `test-author → implementer → code-reviewer →
build-verifier`) `→ integration-tester → docs-builder → teachme →` human-authorized commit via
`git-steward`.

Reviewers are independent: they get the diff + plan + spec, never the author's reasoning.

> **MODE SHIFT — lightweight mode (user-decided 2026-07-29, for token economy; first applied
> to subsystem 8, control).** The per-unit `test-author → implementer → code-reviewer →
> build-verifier` cycle is replaced by **one implementation pass + one adversarial review
> sweep** over the whole subsystem; visual `docs/dev-cycle/` artifacts are optional. Planning
> and human flag sign-off are unchanged; commits still go through `git-steward` with the full
> pre-commit gate. **Future sessions follow this mode** unless the user says otherwise.

## 9b. Guardrail hooks (`.claude/settings.json`)

Two project hooks make §8 mechanical rather than advisory. Both verified live on 2026-07-20.

- **`hooks/guard_production_and_secrets.py`** (PreToolUse, all file + Bash tools) — refuses any
  Write/Edit/NotebookEdit into the live python-poller tree, any mutating Bash command against it
  (`rm`/`mv`/write-redirects/`sed -i`/`git` state changes), and any Read or Bash command that
  touches or even names `labkey_api.txt` / `mizzou.txt`. **Reads of python-poller source are
  deliberately allowed** — the Plan B port depends on them.
- **`hooks/arch_guard.py`** (PostToolUse, Write|Edit) — on any edit under `sluice/core/`, runs
  `tests/test_dependency_arrows.py` (~1.5s) and reports failure straight back. Proven to catch a real
  `import sluice.gates` violation.

**Both hosts are covered.** The guard checks *all* known production roots — the macOS path **and**
`/home/wkgardner/python-poller` (CHTC) — on every machine, because the hook file is synced between
working copies. A root that does not exist locally simply never matches. Extend via
`SLUICE_PROD_REPOS` (`os.pathsep`-separated). The earlier macOS-only constant left CHTC — the host
that actually runs production — unprotected.

**The guard is itself under test:** `tests/test_guard_hook.py` (54 cases) asserts both the deny half
and the allow half against both roots, so CI's linux runner covers the CHTC path. Losing the deny
half would endanger production; losing the allow half would block the Plan B port. Full suite: **55
passed**.

⚠️ Hooks still only run in the copy they are installed in — CHTC needs `.claude/` synced before it is
actually protected.

---

## 10. Next actions (ordered)

1. **Confirm the subsystem order** (§9a — the last open conflict; the other two are resolved).
   Recommendation is to adopt `.claude/README.md` as written, with the B9 caveat noted there.
2. ✅ **Bootstrap committed** 2026-07-20 via `git-steward` — clean initial commit `638b7482` on
   `main`, 24 files, no secrets, no state; pre-commit gate passed, tree green (76 passed). Not pushed.
3. ✅ **`db` subsystem built and committed** 2026-07-21 (see §9: commits `b65fd1d4`, `fdb7471c`,
   and this handoff commit on `main`, not pushed). Connection with WAL + `synchronous=NORMAL`,
   schema + forward-only migrations, greenfield `lifecycle`, the `WHERE lifecycle='active'`
   eligibility predicate — errata A1/A2/A3/A4, B1/B2, forks F1/F2/F4 all resolved; 131 passed.
   **Next subsystem: `gate` (subsystem 2) — the gate contract** (`db → gate (+ one mock gate
   early) → …`, §9a). Subsystem 2 owns migration **005** (gate-satisfaction columns): the `db`
   plan (F3) deliberately deferred it, and migration 005 is where those columns land.
3a. ✅ **ADR-0001 + ruff adoption committed** 2026-07-21 (`b427104`, `8b508d6`, and this handoff
   commit on `main`, not pushed; see §9). **Open item (still standing, now covers FOUR ADRs):**
   decide whether to add `.gitignore` allowlist rules for `docs/adr/` (ADR-0001/0002/0003/0004),
   `.claude/CLAUDE.md`, `.claude/agents/*.md`, and `docs/dev-cycle/` so the ADRs the commits cite
   enter history (human curates the allowlist).
3b. ✅ **gate subsystem committed + events plan approved and committed** 2026-07-22 (`bb22eec`,
   `e59ae20`, and this handoff commit on `main`, not pushed; see §9). Subsystem 2 complete: 9
   units, reviewed, 171 tests green. Subsystem 3 fully planned and decided — all five events-plan
   flags approved, including ⚑5 (daemon-only notifier model; spec §6 revised).
3c. ✅ **events subsystem built and committed + context plan committed** 2026-07-23 (`0db7a27`,
   `39597e0`, and this handoff commit on `main`, not pushed; see §9). Subsystem 3 complete: 8
   units, reviewed per-unit, 171 → 206 tests green, all five plan flags user-approved.
   Subsystem 4 (`context`) fully planned and adversarially reviewed — one flag then open (⚑1,
   closed 2026-07-23; see 3d).
3d. ✅ **⚑1 closed and context subsystem built and committed** 2026-07-23 (`56bc950`, `ce4b872`,
   and this handoff commit on `main`, not pushed; see §9). Subsystem 4 complete: 3 units,
   reviewed per-unit, 206 → 227 tests green; ⚑1 APPROVED (name-keyed `workdir`; CHTC
   no-`/scratch` constraint recorded with the stamp). **All four core-data subsystems — db,
   gate, events, context — are now committed and green.** Standing items unchanged: ADRs
   still gitignored (3a), `docs/dev-cycle/` untracked incl. the new `04-context/` artifacts,
   no git remote configured.
   **Next: plan `profile` (subsystem 5)** via the §9a loop (`… → context → profile →
   reconcile → …`) — applicability lives in the profile, not the gate (§5).
3e. ✅ **profile subsystem built and committed** 2026-07-24 (`3ed1f6e`, `5a96646`, and this
   handoff commit on `main`, not pushed; see §9). Subsystem 5 complete: 7 units, reviewed
   per-unit, 227 → 253 tests green; both flags stamped 2026-07-23 (⚑1 APPROVED — no
   profile-side deadlock ban; ⚑2 DECIDED — two reference profiles ship). **Plan A now 5 of 9
   core subsystems done — db, gate, events, context, profile all committed and green.**
   Standing items unchanged: ADRs 0001-0003 still gitignored (3a), `docs/dev-cycle/` untracked
   incl. the new `05-profile/` artifacts, no git remote configured; CHTC drift since 2026-07-17
   (§2/§3) still unresolved.
   **Next: plan `reconcile` (subsystem 6)** via the §9a loop (`… → profile → reconcile →
   audit → …`). It inherits two named obligations deferred here per ⚑1 — the
   required-needs-optional deadlock rule and the definition of "a due optional" (both decided
   at reconcile, jointly) — plus the disabled-is-satisfied treatment (skip / needs-unblock /
   never-block) and the vacuous-completion edge (`required_gates(profile) == ()` ⇒ complete);
   the ready-made substrate for the disabled path is the shipped `REFERENCE_PROFILE_ALPHA_DISABLED`.
3f. ✅ **reconcile pause resolved — all three sign-off flags decided by the user 2026-07-27**
   (was: PAUSED mid-sign-off 2026-07-24; the review record — APPROVE WITH SHOULD-FIX, core logic
   cleared with no deadlock/equivalence hole, two should-fix amendments applied — stands, its
   conclusions folded into the shipped plan). The decisions, recorded in the plan's sign-off
   section and in **ADR-0004 (binary contracts, `docs/adr/0004-binary-contracts.md`)**:
   - **⚑1 — REJECTED AS FRAMED, superseded by ADR-0004.** The user rejected the runtime optional
     tier outright: anything optional is not in the contract; optional steps are not run-then-
     skipped. A run's contract is binary — every contract gate mandatory, excluded gates absent.
     The "due optional" definition and the required-needs-optional deadlock rule are **discharged
     as moot**, closing plan 05 ⚑1's deferral; unit 0 removed `Applicability.OPTIONAL` so the
     tier cannot silently return.
   - **⚑2 — RESOLVED, stronger than proposed (ADR-0004 d.4).** **ANY** contract gate returning
     SKIPPED quarantines the run (no optional-skip branch exists); the column is never set, so C4
     closes. Recovery is `clear_error` + optionally the subsystem-8 exclusion op (d.3).
   - **⚑3 — APPROVED AS REWORDED.** `next_gate` stays `(run_row, profile)` and never reads the
     pointers; the question "does Plan A build `backtrack`/`fastforward`, and if so who honors
     the pointer?" is routed to the subsystem-8/9 plans, with the binding constraint that a
     pointer-writing op with no honoring reader is a defect.
3g. ✅ **reconcile subsystem built and committed** 2026-07-28 (`8127aab`, `44b44cf`, `a52940f`,
   and this handoff commit on `main`, not pushed; see §9). Subsystem 6 complete: 4 units incl.
   the unit-0 profile amendment, reviewed per-unit, 253 → 304 tests green. **Plan A now 6 of 9
   subsystems done — db, gate, events, context, profile, reconcile all committed and green; the
   profile vocabulary is binary (`required|disabled`).**
   Standing items: `docs/adr/` now holds FOUR gitignored ADRs (0001–0004 — the 3a allowlist item
   grows again); `docs/dev-cycle/` untracked (the `06-reconcile/` artifacts being generated now);
   no git remote configured; CHTC drift since 2026-07-17 (§2/§3) still unresolved.
   **Next: plan `audit` (subsystem 7)** via the §9a loop (`… → reconcile → audit → control →
   daemon`). Carry-forwards routed by plan 06 §7: subsystem 9 (daemon) inherits the
   outcome-persist policy (SUCCESS sets the column; FAILURE and **SKIPPED** quarantine, ADR-0004
   d.4), the `next_gate is None` completion stamp + persist-then-emit (B9), and the
   `backtrack`/`fastforward` question; subsystem 8 (control) inherits the per-run exclusion op
   (ADR-0004 d.3) and the same routed question.
3h. ✅ **audit subsystem built and committed** 2026-07-28 (`bccd6f8`, `a549269`, and this
   handoff commit on `main`, not pushed; see §9). Subsystem 7 complete: 3 units, reviewed
   per-unit, 304 → 321 tests green; both ⚑ flags decided by the user 2026-07-28, both against
   the plan's recommendation, reasoning preserved in the stamps (⚑1 — no DB append-only
   triggers, code promise only enforced by the unit-3 surface guard, ladder stays at 005; ⚑2 —
   no reader, deferred to Plan C with its first caller `sluice audit`). The errata gained **§F**
   (F-ADR4: the ADR-0004 supersession record; file gitignored with the specs). **Plan A now
   7 of 9 subsystems done — db, gate, events, context, profile, reconcile, audit all committed
   and green.**
   Standing items unchanged: `docs/adr/` holds FOUR gitignored ADRs (the 3a allowlist item),
   `docs/dev-cycle/` untracked (incl. the `07-audit/` artifacts being generated now), no git
   remote configured; CHTC drift since 2026-07-17 (§2/§3) still unresolved.
   **Next: plan `control` (subsystem 8)** via the §9a loop (`… → audit → control → daemon`).
   It implements the choreography this subsystem defined — one `write_txn` per op: validate →
   mutate → `record_control_action` → commit → `control_event` — plus the per-run exclusion op
   as remediation (ADR-0004 d.3), `set_gate`'s B2 fabrication refusal, `reset_gate`'s
   experiment-gate protection, and the routed `backtrack`/`fastforward` question; it also
   decides the plan-07 §6 recommendations left to it (refused-op auditing on an autocommit
   connection, `get_status` writes no audit row, the `result` string vocabulary). No
   `reset_attempts` op exists, ever (ADR-0002).
3i. ✅ **control subsystem built and committed** 2026-07-29 (`34a1992`, `8645689`, and this
   handoff commit on `main`, not pushed; see §9). Subsystem 8 complete: built in the **new
   lightweight mode** (§9a — one implementation pass + one adversarial review sweep), all three
   ⚑ flags decided by the user 2026-07-29, 321 → 394 tests green, migration ladder now at
   **006** (`excluded_gates`). **ADR-0005 recorded** (no automatic file deletion — operators
   own destructive filesystem actions; rewinds are column-clear only; `invalidate` dormant;
   B7 reap frozen pending sign-off). **Plan A now 8 of 9 subsystems done — db, gate, events,
   context, profile, reconcile, audit, control all committed and green.**
   Standing items: `docs/adr/` now holds FIVE gitignored ADRs (the 3a allowlist item grows
   again); `docs/dev-cycle/` untracked (no `08-control/` artifacts — visual docs optional in
   lightweight mode); no git remote configured; CHTC drift since 2026-07-17 (§2/§3) still
   unresolved.
   **Next: plan `daemon` (subsystem 9)** — planning underway in lightweight mode. It inherits
   the outcome-persist policy (SUCCESS sets the column; FAILURE and SKIPPED quarantine,
   ADR-0004 d.4), the `next_gate is None` completion stamp + persist-then-emit (B9), and the
   per-run contract composition `apply_exclusions(load_profile(...), run_exclusions(row))`
   (plan 08 §6).
3j. ✅ **daemon subsystem built and committed — PLAN A COMPLETE** 2026-07-30 (`e5d4c6b`,
   `289f7c6`, and this handoff commit on `main`, not pushed; see §9). Subsystem 9 complete:
   lightweight mode (one-pass build + single adversarial review, APPROVE with two test-only
   fixes applied), both ⚑ flags APPROVED 2026-07-29 (⚑1 — B5 disk-floor auto-pause via
   audited `control.pause` actor `"daemon"`, operator-resume only; ⚑2 — drive-until-blocked),
   394 → 435 tests green. **B9 closed** — both canonical `run.finished` emission points live.
   **All nine Plan A subsystems are built, committed, and green; this handoff commit is
   tagged `plan-a-rc1`** (annotated) — release candidate pending Plan B production gates.
   **Next horizon: Plan B (production gates).** The standing items are now the front line:
   - ~~**No git remote** — the entire Plan A history exists on one machine (the real fix for
     the sync problem too; see item 4).~~ → **SUPERSEDED 2026-09-02 (§4.3): `origin` exists and
     all of Plan A is published.** True when written on 2026-07-30; do not plan against it.
   - **FIVE gitignored ADRs** (`docs/adr/0001`–`0005`) that the commits cite by name but
     history does not contain (the 3a allowlist decision).
   - `docs/dev-cycle/` untracked (`01-db/` through `07-audit/`; none for 08/09 — visual docs
     optional in lightweight mode).
   - **CHTC drift since 2026-07-17** (§2/§3) still unresolved.
   - Plan B carry-forwards: `invalidate`'s fate (dormant per ADR-0005), B7 reap sign-off
     (frozen), and the config-key fatal-class revisit raised in the daemon review.
3k. ✅ **PLAN B OPENED — production detection (increment 1) built and committed** 2026-07-31
   (`e37210b`, `4cd9860`, and this handoff commit on `main`, not pushed; see §9). The gate map
   is FINAL (eight gates: experiment → download → checksum → samplesheet → registration
   [DISABLED] → nvd → globus_submit → globus_verify; all six ⚑ flags stamped 2026-07-30/31);
   **ADR-0006 accepted** (`Gate.persists` eleventh field; output + checkmark written atomically
   in one txn; the globus split makes A4 structural; `gate_outputs` parked with revisit
   triggers); **F-B6 recorded** (no gate time bounds — user-decided, hung-gate consequence
   owned). Increment 1: lightweight mode, one adversarial review, user-approved; 435 → 461
   tests green. Detection is a world adapter behind an injected `lister` seam — sshpass
   password via `SSHPASS` env, never argv; go-live interlock means `production-v3.1` cannot be
   stamped until its profile registry entry lands.
3l. ✅ **PLAN B — increment 2 (ADR-0006 persistence substrate) built and committed** 2026-07-31
   (`80025ad`, `dd8e643`, and this handoff commit on `main`, not pushed; see §9). Lightweight
   mode, one adversarial review APPROVE. The Gate contract is now **ELEVEN fields** (first
   contract change since subsystem 2, via ADR-0006 governance): `Gate.persists` maps
   metadata keys → runs columns; the daemon writes them atomically with the satisfaction
   checkmark in one `update_run`/`write_txn` (ADR-0002 intact — gates still only RETURN).
   `RunContext` now carries a sealed `run_row` snapshot (the gate read path, no DB touch).
   **Migration ladder UNCHANGED (still 006)** — the substrate uses the existing
   `experiment_id`/`globus_task_id` columns; proven with test-local gates, `mock_gates`/
   `reference-v1` untouched. 461 → **469 tests green**.
   **Next: the `experiment` gate** (gate map §8 step 4 — gate 0, the first real LabKey
   interaction and the first real `persists` user; reads `experiment_id` off the run_row
   snapshot this increment added). The CephFS disk strategy D1 remains small and unblocking
   whenever slotted.
   Standing items: `docs/adr/` now holds **SIX** gitignored ADRs (0006 new); `notes/` and
   `specs/` untracked (persistence research note + errata §F entries exist only in this working
   copy); `docs/dev-cycle/` untracked; **the gate map (plan 10) is now committed (`8cb03ef`) —
   its production values redacted to config-key/PP pointers after the pre-commit scan refused
   the originals (§9)**; no git remote; CHTC drift since 2026-07-17 (§2/§3) unresolved; the
   poller clone path note (§9) for the guard hook.
3m. ✅ **PLAN B — increment 3 (the `experiment` gate, gate 0) built and committed** 2026-07-31
   (`7461b4f`, `8f97c12`, and this handoff commit on `main`, not pushed; see §9). Lightweight
   mode, one adversarial review APPROVE, all three ⚑ flags stamped. **The FIRST production
   gate** — order 0, the first external-service touch (LabKey), the first load-bearing
   user-facing persist (`experiment_id`, Will's reference key). The **C1 duplicate-experiment
   hazard is structurally closed** via resume-first + lookup-before-create (adopt on find,
   create only on `None`), fail-closed on any `LabKeyError` (quarantine, never fall through to
   create). The api key stays a config **path** read lazily, never logged nor in any error; the
   C2 project value comes from config key `labkey_experiments_project`, never hardcoded;
   `labkey_user_id` dropped entirely. Migration ladder now at **007** (`labkey_registered`).
   469 → **493 tests green**; lint clean; dependency-arrow guard green.
   **⚑3 — INCREMENTAL-MIGRATION POLICY ADOPTED (record for future gate increments):** one
   satisfaction column per gate, shipped WITH its gate — 007 adds ONLY `labkey_registered`,
   superseding gate-map §6's single coherent migration. Each later gate ships its own:
   **download → 008, checksum → 009, samplesheet → 010, …**
   **Next: the `download` gate** (gate map §8 step 5 — multi-TB rsync transfer of the run
   directory; the first heavy-I/O gate. NOTE: download is rsync, NOT Globus — the first
   `globus_task_id` belongs to a later gate). The CephFS disk strategy D1 remains small and
   unblocking whenever slotted.
   Standing items unchanged: `docs/adr/` holds SIX gitignored ADRs (0001–0006); `notes/` and
   `specs/` untracked; `docs/dev-cycle/` untracked; no git remote; CHTC drift since 2026-07-17
   (§2/§3) unresolved.
3n. ✅ **PREDICATE DEEPENING (plan 14) built and committed** 2026-08-01 (see §9). Not a Plan B
   increment — a **behavior-preserving locality refactor** taken from the 2026-07-31 architecture
   review (findings #1 and #2). The review's diagnosis: sluice's modules are genuinely deep; the
   friction is **locality failures**, where a load-bearing rule has one home and is then
   re-implemented inline at N sites. No schema change, no migration, ladder stays at **007**.
   - **The eligibility rule** (`lifecycle='active' AND errored_at IS NULL`, F4) went from THREE
     copies to TWO. `db.is_daemon_eligible` is the new exported Python half; `daemon.process_run`
     and `control._quiescence_refusal_why` both call it instead of hand-transcribing it.
     `ACTIVE_RUNS_WHERE` stays internal and stays SQL — SQLite cannot call a Python function, so
     that one seam is **irreducible by design, not by neglect**.
   - **The safe-path rule** went from TWO copies to **ONE**. `context.is_safe_path_component` is
     exported (context owns the identity→path boundary); `daemon._is_safe_path_component` was
     **deleted** and its two callers (detection, the per-run jsonl sink) import the real one.
     Security-load-bearing — it is what stops a run named `..` escaping onto disk.
   - **The irreducible seam is mechanically guarded, not merely documented.** New
     `tests/test_predicate_twins.py` runs both halves over the same rows (full lifecycle × error
     cross-product plus a completed-but-active row, which is what catches a future "obvious"
     `completed_at IS NULL` tightening). **Verified it actually fails** on injected drift in BOTH
     directions — Python half tightened, and SQL half loosened — same standard as
     `test_dependency_arrows.py`. Wired into `.claude/hooks/arch_guard.py`, whose single
     `GUARD_TEST` constant became a `(test, headline)` table so each guard keeps its own precise
     failure message; hook proven to exit 2 on drift and 0 when healthy.
   - 493 → **517 tests green**; lint clean; dependency-arrow guard green. Exactly **two** existing
     tests were edited, both `__all__` surface guards (`test_context.py`, `test_db.py`), both
     because a module contract deliberately changed — the plan's stop condition was that a third
     edit would mean the change was not behavior-preserving. It never fired.
   **Deliberately out of scope** (each its own pass): the three standalone `errored_at` reads
   (`events.py`, `clear_error`, `control.py:1667`) — they ask "is this run quarantined?", one
   clause, not the AND of two; review finding #3 (five inline `_conclude` twins in the 1,745-line
   `control.py`); findings #4/#5; finding #6 is an **anti-recommendation** — do NOT extract a
   `gates/` base class, it would be a shallow pass-through; document it in `CONTEXT.md` instead.
3o. ✅ **Drift-hunter agent DROPPED; `CONTEXT.md` created; ADRs finally tracked** 2026-08-01.
   The proposed custom drift-hunter agent was investigated and **cancelled — do not rebuild it**
   (reasoning preserved in plan 14 §6). The job is already done by the
   **`/improve-codebase-architecture`** skill (mattpocock plugin), which is in fact what *produced*
   the 2026-07-31 review that findings #1/#2 came from — a custom agent would have duplicated a
   tool already proven on this codebase. The gap that seemed to justify building one ("it needs a
   standing exclusion list or it will nag forever") **does not exist**: the skill already reads
   `docs/adr/` as decisions not to re-litigate, and offers to record an ADR when a candidate is
   rejected for a load-bearing reason. It is `disable-model-invocation: true`, so it cannot be
   wired into the build loop — Will runs it, periodically, after a subsystem lands.
   Two gaps closed instead:
   - **`CONTEXT.md` now exists at the repo root** — the domain glossary `.claude/agents/domain.md`
     always named but nothing had created. Defines run, experiment ID, gate, gate outcome,
     satisfaction column, profile, contract, exclusion, the two orthogonal state axes (lifecycle
     vs quarantine), **daemon-eligible**, reconcile loop, control op, run context,
     quarantine-grade, world adapter, seam, and **twin** — each with the synonyms to avoid and
     why. Finding #6's anti-recommendation (do NOT extract a `gates/` base class) is recorded
     there on purpose, so it stops being re-proposed.
   - **⛔→✅ THE LONG-STANDING 3a ITEM IS CLOSED.** `.gitignore` now allowlists
     `!/docs/adr/*.md` and `!/CONTEXT.md`. All **six** ADRs (0001–0006) enter history in this
     commit, after a clean secret/production-value scan. Commits have cited them by name since
     0001 while the files lived on one laptop only; they are also load-bearing tooling, since the
     architecture skill's do-not-re-litigate mechanism reads them.
   **Next: the `download` gate** (Plan B increment 4, gate map §8 step 5 — multi-TB rsync, migration
   008, errata D6 threadpool-contextvar, and deferred ⚑A where `resolve_context.workdir` becomes
   experiment_id-aware). Resumes as before.
   Standing items: `notes/`, `specs/`, `docs/dev-cycle/` still untracked; **git remote CONFIRMED 2026-08-01: `origin` =
   `git@github.com:wkgardner/sluice.git`, `main` tracks `origin/main`, all work pushed** (earlier
   "no git remote" entries in this file are STALE — the remote was added without a handoff update); CHTC drift since 2026-07-17
   (§2/§3) unresolved.
3p. ✅ **MVP PIPELINE COMPLETE — plan 15 increments 1-8 built and committed** 2026-08-01/02.
   Scope fixed by Will 2026-08-02: a working pipeline **through `nvd`**, Globus deferred until he
   returns; deadline end of week, after which a **colleague operates sluice unattended**. That
   constraint drove the sequencing (plan 15 §3a): every increment leaves a shippable pipeline, so
   operability (CLI, alerting) landed BEFORE the last two gates — `nvd` is the riskiest port and
   had to be the thing that slips, never the thing whose slippage makes the rest unusable.
   **`production-v3.1` now runs `experiment → download → checksum → samplesheet → nvd`.**
   Migration ladder **007 → 011**. 493 → **654 tests green**; lint clean; both guards green.
   - **1. `production-v3.1` profile** (`487c6e2`) — the blocker invisible in the gate map: the
     profile was specified but **never defined anywhere in `sluice/`**. Going live would have
     quarantined every run on `UnknownProfileError` at the daemon's first look, continuously.
   - **2. ⚑A + download gate** (`85e4fb8`, `d0a34e9`, migration 008) — `resolve_context` keys
     staging on `experiment_id` with a name-keyed fallback while it is NULL. rsync adapter is
     fail-closed; the password is never logged, returned, or put in an exception (two tests pin
     the leak surface). D1 disk pre-flight fails OPEN on unreadable xattrs but refuses a full
     quota. D4 ramp: reading production corrected two guesses — the ramp **pauses and resumes**
     (PP `:544`; latching it off deadlocked the suite) and production runs **three passes**
     (`download_with_retry`, PP `:561`), which matters because ADR-0002 quarantines on any
     failure and a transient connection block would otherwise strand a healthy run. D6 closed
     structurally: workers do pure I/O, all logging on the submitting thread.
   - **3. `checksum` gate** (`08d5e41`, migration 009) — failure modes reported **distinctly**
     (`MANIFEST_MISSING` vs `HASH_MISMATCH`) at Will's prompting, so the deferred auto-rewind
     question (plan 15 §8) stays makeable. Manifest discovery **walks** rather than globs (SHA-256
     manifests are dotfiles, `glob("*")` skips them); only `001.fastq.gz` entries are verified.
   - **4. CLI** (`564bf5b`, rebuilt on Typer+rich in `93eb020`) + config loader + **`docs/
     OPERATING.md`**, a standalone operator guide assuming no knowledge of the codebase. Two
     recorded deviations (plan 15 §9): stdlib-then-Typer, and the CLI talks to `control`
     **directly** rather than through `sluice.api` — auditing lives in control, so what the API
     adds is *remote* access, not authorization. A real bug caught: `structlog.configure` is
     process-global and the first version never restored it, breaking 42 unrelated tests.
   - **5. Slack notifier** (`c1cb762`) — and **the A5 seam completed**. `runs.slack_thread_ts` has
     existed since migration 001 and `safe_notify`'s docstring describes the thread id riding back
     to the daemon, but **nothing ever acted on it**: `detect_new_runs` discarded the return, so
     every notification would have posted un-threaded. The daemon now persists it, in its own
     transaction outside the detection one. Token from `slack_bot_token_file`/`$SLACK_BOT_TOKEN`,
     **never honoured inline from config** (tested).
   - **6. `samplesheet` gate** (`b2a4385`, migration 010) + shared NVD tool adapter. `needs=
     ("checksum",)` not `("download",)` — "verified before used" made structural. **Errata A7/D2
     fixed in passing:** production derives the NVD path from `Path.home()`; sluice reads
     `nvd_repo_dir` by subscript. Deviation (plan 15 §10): does **not** port `git checkout -f`.
   - **7. `nvd` gate** (`a41ae03`, migration 011) — D2/A7 launch dir **refused** if inside
     `local_staging_dir` (on shared storage `--resume` silently does nothing); D3 append-mode
     logs; D5 heartbeats; ⚑F no time bound. **`nvd_auto_run` is deliberately NOT honoured** — it
     is the exact ADR-0004 shape (a gate reporting satisfied without doing its work); DISABLE the
     gate instead. Migration 011 records production's `pipeline_started`+`pipeline_completed`
     collapsing onto one `nvd_completed`.
   - **8. `sluice setup` + config schema** (`bfed2c4`) — Will's request: one command that
     initialises AND upgrades, config under `~/.sluice`. **An existing value is never
     overwritten**, which is what makes an upgrade safe to run without reading a diff. The schema
     is guarded by an `ast` parse asserting it covers exactly the keys the code reads, in both
     directions — that guard immediately caught `config.example.json` carrying **34 dead keys**
     (77 → 44).
   - **⚠️ THREE INTEGRATION BUGS found by the first real end-to-end run** (`6cffb69`), against
     Will's local fixture. None was catchable by unit tests: (a) the listing command **lost its
     trailing slash**, so `rsync --list-only` described the directory instead of listing it and
     every run reported "listed zero files" — the unit test fed the parser a canned listing;
     (b) an empty `watch_dir` **scanned the current directory** (`Path("")` is `.`), minting
     `.git`/`docs`/`tests` as runs; (c) **no composition root existed** — the production rsync
     lister was built, tested, and wired into nothing, and `run_forever` was never called, so the
     pipeline was complete but **unstartable**. `sluice daemon` is that root.
     `tests/test_end_to_end.py` is the missing layer, injecting nothing below the CLI.
   **Next: `sluice doctor` (errata E2).** Setup proves the config is *complete*; doctor would
   prove it *works* — host reachable, reference paths present, NVD repo on its ref, LabKey key
   accepted. That is the class of problem that otherwise surfaces on deployment day.
   Standing items: `notes/`, `specs/`, `docs/dev-cycle/` untracked; CHTC drift since 2026-07-17
   unresolved; the Globus pair + `registration` (⚑D, needs C3 fail-closed) remain deferred.
3q. ✅ **GATE-AUTHORING ERGONOMICS — `docs/ADDING-A-GATE.md` + a boilerplate hoist** 2026-08-02.
   Not a plan-15 increment. Raised by Will directly: *"I thought we were going to make gates of one
   task object with a few shared values… now every gate is its own python file that is very
   unique."* Investigated rather than answered — the shared task object **held**: all five
   production gates declare the same eleven fields in the same order, and `validate_gates` enforces
   the set. What made it *feel* bespoke was measured instead of assumed:

   | module | file lines | docstring | **code** |
   |---|---|---|---|
   | experiment | 132 | 73 | **36** |
   | samplesheet | 114 | 45 | **40** |
   | checksum | 244 | 75 | **118** |
   | nvd | 330 | 107 | **166** |
   | download | 448 | 140 | **228** |

   The gates are 40–55% docstring (ADR-0001 working as designed), so they read as unique essays
   over what is mostly a shared skeleton. Two genuine duplications were removed and one proposed
   refactor was **rejected**:
   - **`no_op_invalidate` hoisted into `sluice/core/gate.py`** (now FIVE exported names), replacing
     four near-identical `_invalidate_X` functions in download/checksum/samplesheet/nvd. Each
     gate's specific reason moved to a comment on its `invalidate=` line — nothing lost.
     `experiment` deliberately KEEPS its own: its no-op is load-bearing (clearing `experiment_id`
     would orphan a real LabKey experiment) and must not read as boilerplate. A new test asserts
     the four share the same **function object**, so a per-gate copy creeping back fails.
   - **`_run_nvd` deleted** — a wrapper that only called through (`popen` is a default arg, so
     `run_nvd_gate` IS the production entry point).
   - **REJECTED: a shared `wire_client` helper** for the three `functools.partial` wrappers. First
     proposed, then withdrawn on reading the code: they are three different shapes, not one, and a
     generic helper would be exactly the shallow pass-through CONTEXT.md records as an
     anti-recommendation (review finding #6). Three explicit four-line functions that each name
     their own adapter beat one indirection you have to chase.
   - **`docs/ADDING-A-GATE.md`** — the recipe that existed nowhere: four steps (migration → module
     → one line in the profile → tests), a copy-pasteable skeleton with the three layers labelled,
     and a table of the five mistakes the system punishes structurally *with the error each
     produces*. The next gate is a fill-in-the-blank job, for Will or for the colleague.
   - 654 → **656 tests green**; lint clean; both guards green. Exactly ONE existing test edited
     (the `gate.__all__` surface guard) — the plan-14 stop condition never fired.
   **⚠️ FOUND WHILE COMMITTING: `docs/OPERATING.md` WAS NEVER IN GIT.** The deny-all `.gitignore`
   silently swallowed it; commit `564bf5b` recorded it as landed and §3p above repeats the claim.
   The single most handover-critical document in the repo — the operator guide written for the
   colleague who runs sluice unattended — existed on ONE laptop for a day. Fixed here with
   `!/docs/*.md` (non-recursive, so `superpowers/`, `dev-cycle/`, `teachme/`, `agents/` stay
   untracked); both docs scanned clean for secrets and production values before tracking.
   **This is the second time the deny-all gitignore has hidden a load-bearing file** (the six ADRs
   were the first, closed in 3o) — treat "the commit says it landed" as unverified until
   `git ls-files` agrees.
   **Next: `sluice doctor` (errata E2)** — unchanged.
3r. ✅ **`sluice doctor` INCREMENT A + INSTALL/SETUP USABILITY** 2026-08-03. Eight commits on `main`
   (`236fffe`, `829e22e`, `b70d4c9`, `47bd118`, `236547c`, `5423827`, `2ec98c2`, `ac490fd`), **not
   pushed**. 716 → **719 tests green**; lint clean; every intermediate commit independently green
   (657 → 659 → 661 → 715 → 716 → 719), so the history bisects.
   - **`sluice doctor` (plan 16, resolves errata E2 in part)** — `sluice setup` proves a config is
     *complete*; doctor proves it *works*. Each key's `ProbeKind` becomes a check; ok/warn/FAIL/skip;
     exit 0 unless something FAILed. Went through **two adversarial review rounds** — the first
     REJECTed with 5 blockers, **four of which were defects in the plan, not the implementation**:
     doctor mutating the db via `PRAGMA journal_mode=WAL` (persisted in the file header, so a
     "read-only" diagnostic rewrote the file it inspected — now a genuine read-only connection,
     asserted by a recursive **content** fingerprint, since that mutation changes no filename); a
     drift guard that silently exempted new gates (now a hard failure — a guard that exempts what it
     has not been taught about protects nothing at the moment it is needed); `ProbeKind.NONE`
     skipping required keys entirely; and `nvd_command` probed with `shutil.which` when the adapter
     runs it through `pixi run --manifest-path` (a PATH probe would have failed every *correct*
     install). Final verdict APPROVE WITH SHOULD-FIX, all nits closed, confirmed by build-verifier.
   - **`ProbeKind` + 44 `probe=` classifications** committed separately from doctor itself, so 44
     mechanical judgements are reviewable without being buried under a 745-line module. `probe` has
     **no default** by design (spec D5): adding a config key must be impossible without answering
     "can this be checked?".
   - **A no-behaviour-change hoist commit** gives each rule doctor shares exactly one home
     (`SOURCE_DIR_KEY`/`is_local_mode`, `nvd_manifest_path`, `TOKEN_ENV_VAR`,
     `is_unfilled_placeholder`, `managed_directory_for`). Doctor's whole value is agreeing with what
     the pipeline does; a transcribed copy of a rule is free to drift, and a preflight that
     disagrees with the runtime is worse than none.
   - ⚠️ **`sluice/gates/transfer.py:135` still holds a pre-existing copy of the local-mode rule and
     was DELIBERATELY LEFT ALONE** — it is on the download path, not detection, so changing it is a
     behavioural edit that belongs to its own commit rather than to a no-op preparation step.
   - **A real usability bug fixed: `sluice setup` wrote `~/.sluice/config.json` while every command
     read `./config.json`**, so a correct install was unusable and setup's closing message named a
     command that could not find what it had just written. Root cause was a **twin** — `default_home()`
     was private to `setup`, so the writer knew and the reader could not ask. Now shared, pinned by
     `plan_setup().config_path == resolve_config_path(None)`.
   - **Setup now writes every required key empty** (`SetupPlan.added_blank`) instead of listing key
     names in a terminal for the operator to transcribe — a typo there yields a *silently absent*
     key, which is harder to see than an empty one, for the operator and for doctor. Values are
     still never invented. **Overwrite protection** added: re-running asks, backs up to
     `config.json.bak` *before* writing, refuses non-interactively without `--yes`. The values were
     never at risk (verified); the *file* is rewritten sorted and reformatted, which is how Will
     lost hand-edits.
   - **`install.sh`** puts `sluice` on PATH so the operator guide's commands work as written —
     previously **every command in that guide would have failed** for the colleague. CLI help text
     de-jargoned (`sluice daemon --help` was explaining the composition root and DI).
   - **`docs/OPERATING.md` — REFUSED, then sanitized and committed** (`d194cb0`, the ninth commit).
     The first draft used Will's real test server as the worked example: a **real account and a real
     host** in the "Pointing sluice at your data" JSON block, plus a vendor delivery-folder name.
     The steward's pre-commit scan refused it, cited `file:line`, and did **not** self-correct it;
     the values were replaced at the source with `<angle-bracket>` placeholders matching
     `config.example.json`, a synthetic Illumina-shaped run name, and `<delivery-folder>`, then
     re-scanned clean. **The values never entered history** — the fresh-history gate working as
     intended, for the third time (gate-map §10 3m, and the two gitignore misses in 3o/3q).
     **Lesson, same as 3m:** a document does not enter history until its production values are
     placeholders or pointers — and an operator guide is copied more than any other file in the
     repo, so a real value in one propagates into every fork of it.
   - ⚠️ **LATENT BUG the guide currently documents around:** the trailing slash on
     `remote_source_path` is load-bearing and **nothing in code adds it on the remote path**.
     `build_listing_command` appends it itself in the local branch (`local_source_dir + "/"`) but
     interpolates `remote_source_path` verbatim into `{user}@{host}:{path}`. Same class as one of
     the three integration bugs fixed in `3696c94`. Documentation in two places (the key's schema
     help text and the guide) is the only thing preventing a silent mis-detection — one run named
     after the directory instead of the runs inside it. **Closing that asymmetry is unclaimed work.**
   **Next: doctor increments B and C** — **B** = the `--comprehensive` network tier (real
   connections, credentials accepted by their services, a cheap read per endpoint, plus the source
   manifest visibility check added in `6ef04e3`, and the file-vs-directory schema flag `PATH_EXISTS`
   needs and does not have). **C** = the daemon startup gate. Neither is started.
3s. ✅ **DOCTOR REPORT ORDERING** 2026-08-03/09-02. One commit, `198ec64` *(cli: order the doctor
   report least-urgent-first)*, on `main` and **published**. 719 → **720 tests green**; lint clean.
   Doctor now renders `skip → ok → warn → FAIL`, so the only rows an operator must act on finish
   the table directly above the summary. In run order the failures scattered through what is a
   **forty-odd-row table on a full config**, and the top of it had scrolled away — reading the
   report meant hunting upward from the summary for the rows that mattered.
   - **The sort is stable, deliberately.** Within a severity the checks keep the order they ran,
     so related keys stay adjacent — the `nvd reference data` failures are read and fixed as one
     group instead of being scattered by an alphabetical re-sort. They are usually one cause.
   - **`_DOCTOR_DISPLAY_ORDER` is a separate map from `doctor.worst`, and the code says why.**
     `worst` decides the exit code and ties OK with SKIP, because a skipped check is not a lesser
     pass and must never influence exit status. Reusing it for display would have interleaved the
     ok and skip rows — and, worse, coupled a presentation choice to an exit-code rule, so that a
     later change to reading order could silently move the exit code.
   - Behaviour otherwise unchanged: same rows, same content, same summary, same exit code.
3t. ✅ **OPERATOR PROSE PASS — the double `sluice setup`** 2026-08-03/09-02. One commit, `0bc8858`
   *(docs: stop telling operators to run `sluice setup` twice)*, on `main`, **published, and it is
   `main`'s current tip.** 720 passed, lint clean — documentation and one help string only, no
   executable line touched, so no count could move.
   - **The substantive fix:** `docs/OPERATING.md` told the reader to run `sluice setup` after
     every release **twice**, two paragraphs apart — once in the heading above the command block
     and again as a bolded sentence below it. Someone following the install section start to
     finish would have run it a second time assuming they had missed something. **Cause worth
     remembering: the `install.sh` section was added on top of the existing text without
     rereading the section as a whole.** Same class as the drift caught in 3w — the most-copied
     file in the repo, and no test covers it.
   - Also removed: a second duplication ("other folders are harmless" / "unrelated folders do not
     matter", eight lines apart), an inflated framing on the `remote_source_path` warning
     ("will cost you an evening" replaced with the fact that actually governs how carefully you
     read on — breaking either rule fails *quietly*), a word echo, and developer jargon from the
     Typer help.
   - **Recorded so a later reader does not "fix" them:** the rule-of-three in "no directory is
     created, no config written, no repository checked out" is three distinct guarantees, not one
     restated three times; and "setup proves your config is complete; doctor proves it works"
     keeps its parallelism because the parallelism *is* the distinction between the commands.
   - OPERATING.md re-scanned for the real host, account and vendor folder name sanitized in
     `d194cb0` (3r): none reintroduced, the placeholders are still placeholders.
3u. ✅ **CONFIG BECOMES TOML — AND NVD'S MISSING UPLOAD TARGETS** 2026-09-02. One commit,
   `9f91cee`, **on branch `config-toml-and-nvd-upload-targets`, not `main`, NOT PUSHED.**
   720 → **730 tests green**; lint clean. *(Not steward-supervised; this entry summarises the
   commit's own message, which is unusually complete. It is a summary of a real artifact, not a
   reconstruction of intent.)*
   - ⚠️ **A REAL PRODUCTION BUG, and the most consequential thing in this ledger since 3p.**
     `build_params_yaml` emitted **15 of the 24 params production writes**. The nine missing ones
     were the entire LabKey upload block plus `trim_adapters`. **Omitting a param does not
     disable the behaviour** — NVD falls back to the defaults compiled into its own repo, and
     those defaults are the **live production lists**. So a dropped param is worse than a wrong
     one: sluice's source reads clean, `sluice doctor` sees nothing, and real results upload to
     the real lists anyway. Seven new operator-supplied keys close it.
   - The new keys live in an **`nvd labkey upload` group kept distinct from the existing `labkey`
     group** — production uploads hits to a container the experiments do not live in, and one
     group would have implied one destination.
   - **`labkey_webdav` is derived, not configured** (`build_webdav_url` from `labkey_server` +
     `labkey_experiments_project`). A key of its own would have been a second place to write the
     same host down and the first place for it to disagree with the LabKey adapter.
   - 🏆 **BOTH ANTI-DRIFT GUARDS FIRED ON THIS WORK, AND BOTH WERE WIRED UP RATHER THAN
     SUPPRESSED.** The schema guard (3p increment 8) flagged the undeclared keys; `test_doctor_
     drift` (3r) flagged that the `nvd` gate had begun reading from the `labkey` group. **This is
     the plan-16 mechanism catching a live production defect — precisely the case it was built
     for**, and the first time it has done so. Record it as evidence when the next guard looks
     like overhead.
   - **`config.json` → `config.toml`** so the file an operator edits can carry each key's `help`
     text as a comment; strict JSON cannot hold one. `render_config_toml` writes one table per
     schema group, so the config and `sluice doctor` read from the same text and cannot describe
     a key differently. Reading is stdlib `tomllib`; setup regenerates the whole file each run,
     so comments are re-derived and no comment-preserving library is needed.
   - **Tables are discarded at load** — `[nvd] nvd_repo_dir` arrives as `config["nvd_repo_dir"]`,
     so every existing subscript is untouched and doctor names keys that appear **literally** in
     the file. Real nesting would have meant doctor naming a key the operator cannot find by
     searching for it. A key set in two tables is a hard `ConfigError` naming both: table names
     being cosmetic makes duplicates the one way the file can say two things, and last-wins would
     make the config mean something no reader could predict.
   - Migration is safe by construction: **the `config.json` is left exactly where it is**, so it
     *is* the backup and the migration needs neither confirmation nor one of its own
     (`SetupPlan.migrated_from` is separate from `is_upgrade` for that reason). An unparseable
     `config.json` refuses the migration rather than stranding the operator's values in a file
     nothing reads.
3v. ✅ **SHARED OPERATION ON ONE HOST — ADR-0007 + the cross-host DB guard** 2026-09-02. One
   commit, `90f2887`, **on branch `config-toml-and-nvd-upload-targets`, not `main`, NOT PUSHED.**
   730 → **745 tests green**; lint clean. *(Not steward-supervised; summarised from the commit
   message and `docs/adr/0007-shared-operations-on-one-host.md`, both read in full.)*
   - **The decision, and why the obvious answer was wrong.** Several people need to operate sluice
     on CHTC. The obvious route was Plan C, and **ADR-0007 records it as the wrong one**: the API
     adds *remote* access, not *multi-user* access, and design §13 keeps authz out of scope — an
     HTTP layer would have left access control exactly where it already was. §5 had already
     specified the case: operators SSH into the one host the daemon runs on.
   - ⚠️ **The hazard this closes had been open since the daemon was written.** The daemon runs in
     a screen session on a login node while the state file lives on shared home (CHTC has no
     scratch, and node-local storage would lose run history on reboot — so §5's "local disk, not a
     share" cannot be followed without trading corruption risk for guaranteed data loss). Nothing
     stopped a colleague on a *different* login node becoming a second NFS client on a WAL
     database. **`fcntl.flock` cannot see this — it is per-machine**, so a second daemon on
     another node takes its own lock and both run against the same file. Invisible while one host
     was in use.
   - The daemon writes its hostname to `<state_file>.host` at every startup; `assert_daemon_host`
     refuses commands from any other machine and names the one to go to. It runs *before* the lock
     is taken, so it refuses a second daemon across nodes too. Placed at `_state_file` in the CLI,
     the one choke point every DB-touching command already passes through.
   - **The claim is a plain file BESIDE the database, not a `poller_metadata` row** — load-bearing.
     Reading a metadata row means opening SQLite, which creates the WAL `-shm` segment, which is
     **the exact cross-host act being prevented**. A guard that must open the database to decide
     whether the database may be opened is not a guard. Pinned by a test that refuses correctly
     against a state file full of garbage.
   - `sluice setup --shared-group GROUP` widens the config (0640) and the state directory (setgid
     2770 — not optional, since SQLite creates `-wal`/`-shm` beside the file and without setgid
     the *second* operator fails, intermittently). **`secrets/` stays 0700**: clearing a quarantine
     never needs the rsync password or the LabKey key, only the daemon does, and that asymmetry is
     what makes widening safe at all.
   - **`--actor` removed, not ignored.** It accepted any string — harmless with one writer, fake
     evidence the moment several can write, and a trail naming who *claimed* to act is worse than
     no trail because it reads as authoritative. A script still passing it now fails loudly.
   - 📌 **PLAN C IS EFFECTIVELY CLOSED WITHOUT BEING BUILT — record it as deliberately unbuilt,
     not pending.** Its three justifications are gone: the CLI already talks to the database and
     to `control` directly (plan 15 §9, entry 3p), the heartbeat landed in `2c7eb58` (3w), and
     ADR-0007 removed the multi-user case. The only content left under that heading is a FastAPI
     service for operating sluice from a laptop without SSHing to CHTC — **which nobody has
     asked for**. ADR-0007 keeps that justification alive and notes that when it is built the
     actor must come from the transport, never from a flag. §6 and §7 still list Plan C as the
     next phase; treat that as superseded by this entry.
   - Resolved: **E1** (daemon supervision — screen, recorded in ADR-0007).
3w. ✅ **DAEMON LIVENESS IN `sluice status`** 2026-09-02. Two commits — `2c7eb58` *(status: report
   daemon liveness from the lock, not the pause flag)* and `f50fc4e` *(docs: tell operators how to
   run the daemon and check it is alive)* — **on branch `config-toml-and-nvd-upload-targets`, not
   `main`, not pushed.** 747 tests green, lint clean, verified by the steward before each commit.
   ~~⚠️ **This entry is written against a gap in the ledger.** Four commits between 3r and here
   are unrecorded — `198ec64`, `0bc8858`, `9f91cee`, `90f2887`.~~ → **GAP CLOSED 2026-09-02: they
   are now entries 3s, 3t, 3u and 3v**, and §9 is restamped. Written from Will's summaries plus
   each commit's own message and ADR-0007; 3u and 3v were not steward-supervised and say so.
   **The original refusal stands as the right call** — the steward declined to summarise them
   while it had nothing but a shortlog to go on, and named the gap instead of papering over it.
   This entry keeps its warning visible for the same reason: a ledger that hides its holes is
   worse than one that points at them.
   - **The `daemon` line answered the wrong question.** It rendered the *pause flag* and printed a
     green "running" whether or not a daemon existed. Nothing supervises the daemon (**errata E1**),
     so after a reboot it is simply gone — and the one line an operator would trust said everything
     was fine. Now **two facts, because either alone misleads**: liveness from the lock
     (`is_daemon_running` probes the flock non-blockingly and asks the OS who holds it — a real
     check, not an inference), and progress from a heartbeat (`daemon_last_look` stamped into
     `poller_metadata` at the top of each cycle, in its own transaction so a later gate failure
     cannot roll it back). **No migration** — that table has existed since 002. Pause moves to its
     own row. Renders `running, last look <ts>` or a red `NOT RUNNING — start it: sluice daemon`.
   - **Why both, and why a heartbeat alone would have been worse than nothing.** NVD has no time
     bound (⚑F), so a live daemon inside an hours-long gate shows a stale timestamp and would read
     as dead. That is the cry-wolf failure that trains operators to ignore the line. The lock
     separates *working* from *gone*; the heartbeat separates *working* from *wedged*.
   - **Machine-local by design, and that is sufficient.** `flock` cannot see another node, but every
     operator is on the daemon's host already (**ADR-0007**), and `assert_daemon_host` refuses
     commands from anywhere else.
   - **Twin killed at the source:** `daemon_lock_path()` extracted so `run_forever` and the probe
     share one derivation. Two copies would let holder and probe disagree about which file they
     mean, and the probe would then report "not running" **forever** — a monitor that is
     confidently wrong. Same class as the `default_home()` twin in 3r.
   - **The probe opens the lock `"rb"`, never `"w"`** — a status command must not create or truncate
     the file the single-instance guarantee rests on — and releases immediately on acquiring, so it
     can never become the thing that blocks a daemon from starting. Stays inside `daemon.py`'s
     pinned closed import surface (`pathlib`, not `os`). The liveness read happens in the **CLI, not
     `control`**: `daemon` imports `control`, so `control` cannot import `daemon` back, and the CLI
     is already the composition root that sees both.
   - **`docs/OPERATING.md` gains "Running the daemon"** — screen/tmux, detach and reattach, and the
     blunt statement the section exists for: **after a reboot sluice does not come back on its own.**
     Nothing will remind anyone; it is the first thing to check when runs stop being picked up.
   - **Steward refused the doc on the first pass** — not for a secret this time (it added no host,
     account or path; the 3r sanitisation held). The worked example read `daemon  not paused`, a
     string the CLI cannot emit, left over from an earlier design of this change that was replaced
     before it landed. **Exactly the drift the commit exists to kill, in the most-copied file in the
     repo**, and no test covers the guide, so it would have sat wrong indefinitely. Held, cited
     `file:line`, did not self-correct; author swept the whole file against the three strings
     `_daemon_health` can actually emit, then it committed clean. **Lesson: the pre-commit gate
     earns its keep on documentation drift, not only on secrets.**
   **Next, unchanged from 3r: doctor increments B and C.** Note **C = the daemon startup gate**
   now overlaps this work — `is_daemon_running` is the probe C would need, and C should use it
   rather than grow a second one.
3x. ✅ **THE TRAILING-SLASH ASYMMETRY IS CLOSED, AND CHTC HAS A DEPLOYMENT GUIDE** 2026-09-02.
   Two commits on `config-toml-and-nvd-upload-targets`, still unpushed. 751 tests green, lint
   clean.
   - **The latent bug named in 3r is fixed.** `build_listing_command` appended the load-bearing
     trailing slash on the local branch (`local_source_dir + "/"`) and interpolated
     `remote_source_path` **verbatim** on the remote one. An operator who omitted it got
     `rsync --list-only <dir>` describing the directory *entry* instead of listing its contents
     — sluice then detects **one run named after the source folder**, containing everything.
     Silent: no crash, no error, a plausible-looking run.
   - **Fixed as a shared helper, not a second append.** `_lists_contents` normalises both
     branches, so the two cannot drift apart again — the twin pattern 3w applied to the same
     class of defect. Idempotent, so an operator who followed the doc and one who did not
     produce identical argv.
   - **Documentation was the only guard, in three places, and all three were wrong the moment
     the code changed** — `docs/OPERATING.md` ("Nothing adds the slash for you"), the
     `remote_source_path` schema help text ("MUST END WITH A SLASH"), and the same text copied
     into `config.example.toml`. All corrected in the same commit. **The example config's
     comments are not guarded** — `test_the_example_config_matches_the_schema` compares key
     *names* only, so its prose can drift from the schema's silently. That is an open gap.
   - **The pinned `REMOTE_ARGV` changed** from `host:.` to `host:./`. Equivalent to rsync, but
     that constant is detection's byte-exact contract with the remote, so the change is
     deliberate and commented rather than absorbed.
   - **Still a third copy of the rsync rule:** `transfer.py:195` appends its own trailing slash
     after `pathlib` construction (where it is never already present). Left alone — the
     situation genuinely differs — but it is the next place this rule could rot.
   - **`docs/DEPLOYING-CHTC.md` is new**: the ordered first-install path for a CHTC login node,
     referencing `OPERATING.md` rather than restating it. Encodes ADR-0007's shape (one login
     node, DB on shared home, `--shared-group`), the no-scratch constraint on every working
     path, the operator-supplied config keys, and the reboot gap. **Records that
     `disk_quota_root` fails open** — on a filesystem without CephFS quota xattrs the headroom
     check warns and proceeds, so a clean `doctor` is not proof of disk space. Written from the
     schema and the code, **not verified against a real CHTC install**; it is untested until
     Will runs it.
   **Next: doctor increments B and C, unchanged.** Deploying CHTC first requires item 4 below.
3y. ✅ **FIRST CHTC INSTALL — DEPLOYING-CHTC.md IS NOW REAL-WORLD TESTED THROUGH STEP 7**
   2026-09-04 (CHTC session — the first since 07-17). The branch gap named in item 4 is CLOSED:
   `config-toml-and-nvd-upload-targets` was published and merged as **PR #2** (`263897c`), and the
   CHTC working copy is a current clone of `main`. Steps 1–5 of the guide (pixi, clone,
   `install.sh`, `sluice setup`) were already done before this session; this session executed
   steps 6 and 7. Nothing committed; `HANDOFF.md` edits await Will's consent.
   - **Step 6 executed: `~/.sluice/config.toml` filled from production.** The values came from
     THREE layers, and the guide only names the first: (1) PP `config.json` (the explicit
     values); (2) PP **code defaults that were the de-facto production values** —
     `labkey_server` / `labkey_experiments_project` (PP `labkey_client.py:117-118`) and
     `labkey_schema` / `labkey_query` (PP `labkey_client.py:154-155`); (3) one value the poller
     never set anywhere — `nvd_labkey_exp_id_guard_list`, whose production value is the NVD
     repo's own default (PP `~/nvd/conf/labkey.config:17`). The NVD reference-data block came
     from PP `nvd_helpers.py:87-92`. Real values live only in `~/.sluice/config.toml` and the
     PP sources, never here.
   - ⚠️ **The `labkey_schema` trap, worth keeping:** `sluice setup` pre-fills it `"lists"`, but
     production uses the custom experiments schema (PP `labkey_client.py:154`). A
     fill-only-the-empty-keys pass leaves it **silently wrong** — the prefilled defaults must be
     checked against production too, not just the blanks.
   - `remote_source_path` is `"."` — the poller listed the SSH user's home (`host:.`), and
     `_lists_contents` (3x) normalises `"."` to the pinned `host:./` argv, so detection's remote
     contract is byte-identical to production. `poll_interval_minutes` transferred at the
     production 30, not the setup default 10 (Mizzou sshd connection sensitivity, D4 territory).
   - **`slack_enabled` deliberately left `false`** with the channel id filled in: python-poller
     still posts to the team channel, and two daemons double-posting is an outward-facing
     failure. Flip it only when sluice takes over notifications.
   - **Step 7 executed: `sluice doctor` — every config check ok** (profile `production-v3.1`
     resolves; schema ladder at migration 11; `nvd_launch_base_is_local` green). The two
     remaining FAILs are the **empty secret files**, and that is correct by design: the §9b
     secrets hook blocks the agent from even naming the poller's secret files, so filling
     `secrets/remote_password` and `secrets/labkey_api_key` is the operator's step, by hand.
   - Guide friction for the next doc pass: step 6's key table omits `labkey_schema` (the
     poisoned prefill above), and the guide nowhere says the poller's `config.get` defaults are
     load-bearing production values that must be mined out of code.
   **Next: Will fills the two secrets → doctor clean → step 8 (`screen` + `sluice daemon`),
   observe-first, python-poller untouched (item 5). Doctor increments B and C still unchanged.**
3z. ✅ **SEED-AND-SKIP BACKFILL — THE 79 HISTORICAL RUNS ARE IN SLUICE'S DB AND FENCED OFF**
   2026-09-04 (same CHTC session as 3y). **Decision, user-approved: this refines, not violates,
   the §5 "greenfield DB + drain (no state-migration code)" lock.** No pipeline state was
   migrated — identity only (name, `experiment_id`, `size_tb`, `first_seen`) via sluice's own
   `db.insert_run`, then `lifecycle='skipped'` via the audited `control.skip` (actor
   `backfill`, one audit row per run, ids 1–79, each reason naming python-poller and its
   `completed_at`). **Zero gate-satisfaction columns touched** — `skip` is "a disposition,
   never a fabricated completion" (control §9), and stamping columns would have been a time
   bomb: future gate migrations (globus, 012+) default to 0 and would reopen every "completed"
   run. `skipped` is immune to schema growth.
   - **Why seeding had to precede the first daemon start:** detection lists the remote source
     and would insert every historical folder as a NEW active run, then drive it in the same
     cycle (re-download TBs, re-run NVD). There is no detect-only mode — the loop-head pause
     short-circuits detection too (§3.8). Pre-seeding makes detection a no-op for these names
     (`insert_run` is ON CONFLICT DO NOTHING), and skip removes them from `ACTIVE_RUNS_WHERE`.
   - **The drain is already complete.** The poller snapshot shows **79/79 runs fully completed
     (all nine gate flags), zero errored, zero in-flight** — including one that completed the
     same hour as the snapshot. 28 pre-SQLite rows carry NULL `experiment_id`; gate 0's
     lookup-before-create re-derives it if any is ever unskipped.
   - **Mechanism:** operator script `~/.sluice/seed_from_poller.py` (deliberately OUTSIDE the
     repo so the no-migration-code lock keeps its teeth), dry-run by default, `--execute` to
     write, safely re-runnable. It reads a **snapshot copy** of the poller DB (WAL-checkpointed,
     no `-wal` sidecar at copy time), never the live file — a live sqlite open could drop
     sidecar files into the production data dir. Seeded rows carry
     `pipeline_version='production-v3.1'` so a later unskip resolves a real contract.
   - **Takeover is `sluice unskip <run>`** — every production gate re-derives from the world
     (resume-first, lookup-before-create, idempotent rsync, NVD resume). Note honestly: gates
     RE-RUN what the world no longer evidences; unskipping a run whose staging/NVD outputs are
     gone means real recompute.
   - ⚠️ **Standing hazard until cutover: BOTH pollers are live.** Any brand-new remote folder
     will be raced by python-poller and a running sluice daemon (same staging tree, duplicate
     work). Before the first real `sluice daemon` start: re-snapshot, re-run the seeder
     (picks up any newly-completed poller runs), and have a hold/skip ready for genuinely new
     arrivals — or pick a quiet window. Cutover sequencing is undesigned; it is the next
     planning item.
   `sluice status` verified: 79 runs, all `skipped`, experiment ids rendered, daemon honestly
   NOT RUNNING. **Next unchanged from 3y: secrets → doctor clean → first daemon start (the
   unskip-one-run parity test is the natural first beta).**
3aa. ⚠️ **FIRST DAEMON START — TWO ROOT-CAUSED FINDINGS, ONE FIXED, ONE NEEDS A DESIGN CALL**
   2026-09-04 (same CHTC session). Will filled the secrets, unskipped the newest run
   (`20260828…0213`, exp 32575), and ran `sluice daemon` live for the first time. What worked,
   verbatim from the design: the `experiment` gate resumed in **0ms with zero LabKey calls**
   (seeded id, resume-first); the disk preflight read real CephFS numbers (30.6 TiB free vs
   6.23 required); the download gate **fail-closed refused** to "report a successful download
   of nothing"; quarantine was stamped before the crash (persist-then-emit held — state stayed
   correct even as the process died).
   - **Finding 1 (seeder defect — FIXED, red-green verified).** The daemon crashed in the
     quarantine emit path: `TypeError: can't subtract offset-naive and offset-aware datetimes`
     (`events._milliseconds_between_iso_timestamps`). Root cause: 3z's seeder copied poller
     `first_seen` verbatim as `detected_at` — offset-naive local Central — violating the
     documented invariant at `db.now_iso` ("Every timestamp column is written with this",
     aware UTC, lexical-ordering rationale). 45 rows repaired in place
     (`~/.sluice/repair_seeded_timestamps.py`, America/Chicago → UTC, DST-correct, idempotent,
     re-run proves 0 remaining); the seeder now normalizes at the boundary. Reproduced the
     crash on the real row before the fix and proved `build_run_event` green after.
     *Repo-side observation, flagged not fixed:* an emit-path exception AFTER the quarantine
     commit kills the whole daemon — state honest, availability not. Whether `emit_run_event`
     deserves a §3.5-style isolation catch is errata-class; decide deliberately, not in a
     hotfix.
   - **Finding 2 (production layout gap — OPEN, needs a design decision).** The download gate
     listed **zero files** because the remote run folder holds no files — only a
     **`SecureBio_02_SOL/` delivery SUBDIRECTORY** with the fastqs one level down (verified by
     reproducing the gate's exact `rsync --list-only <root>/<run>/` against Mizzou).
     `TransferClient.list_run_files` is deliberately non-recursive and drops directory
     entries, so any nested-delivery run reads as empty and quarantines. The poller's answer
     to this layout was **synthetic suffixed runs** (`<run>-SecureBio_02_SOL` — three sit in
     the seeded history as their own rows with their own experiment ids); sluice has no model
     for deliveries at all. **The sharper edge is detection, not download:** a second delivery
     landing in an already-known folder name is invisible to sluice — `insert_run` no-ops on
     the existing name and nothing ever notices the new data (the poller's in-flight exp
     32588 appears to be exactly this: today's delivery into the `20260828…` folder). Decision
     needed — is a delivery a new run (poller-style suffixed identity) or a re-detection of
     the same run? Then the mechanical fix (recursive listing, or delivery-aware identity in
     `parse_rsync_listing`) follows from the answer. This is D-class dropped production
     knowledge surfacing exactly where the beta was meant to find it.
   - The beta run was `clear_error`'d and re-`skip`ped (audited, actor `wkgardner`) — fenced
     again until Finding 2 is resolved. `sluice status`: 79 skipped, daemon not running.
   **Next: decide Finding 2's identity question, then fix the download gate (and likely
   detection) through the normal plan/review path; re-run the unskip parity test after.**
3ab. ✅ **DELIVERY-AWARE DETECTION + DOWNLOAD — 3aa FINDING 2 RESOLVED IN CODE** 2026-09-04
   (same CHTC session). **User decision, stamped: each delivery subdirectory is its own run,
   named `<folder>-<subdir>`** — poller parity with the manually-created suffixed runs.
   Built TDD (every change red-green) with one adversarial review sweep per the lightweight
   mode: **APPROVE-WITH-FIXES, both must-fixes applied and re-reviewed same session.**
   `pixi run test` → **761 passed** (751 → 758 → 761), lint clean. **NOT committed.**
   - `detection.parse_rsync_listing` now groups nested files into **delivery units**: depth-1
     files belong to the folder; deeper files to `folder-subdir`. Candidacy
     (manifest AND `*001.fastq.gz`) is judged per unit — a top-level manifest never vouches
     for a subdirectory delivery.
   - `transfer.list_run_files` is literal-first with a delivery-form fallback
     (`name.replace("-", "/", 1)`), returns paths prefixed with the directory actually
     listed (so `fetch` needed zero changes; `raw_seqs` still lands flat), and the listing
     is now **recursive** (`-r`) — review finding 1: detection could certify a candidate
     from a nested file the old non-recursive listing could never fetch, letting the gate
     succeed having downloaded only the manifest. Closed with an argv+behavior pin test.
   - Error propagation is honest (findings 4/5): a delivery-form failure after an EMPTY
     literal success raises (a throttled connection must not read as "this run has no
     files"), and a double failure names both attempted paths.
   - **Documented limitations, decided acceptable** (findings 2/3, in the code docstring):
     unit names are not injective against literal dash-named sibling folders (sizes can
     double-count; a stale literal copy with files shadows the nested delivery), and the
     inverse split is first-dash (a dash-carrying FOLDER inverts wrong). Both fail closed —
     wrong-path listings quarantine, never fetch silent wrong data — and Illumina folder
     naming is underscore-only. Revisit if a dash-named folder ever grows a delivery.
   - For the record (findings 9/10, pre-existing): `TransferError.__cause__` chains retain
     the full sshpass argv (a sink that formats exception chains would leak the password);
     the compose/invert naming convention lives in detection and transfer by convention,
     tests are the only drift guard.
   - **Confirmed: the poller structurally cannot see second deliveries** (it groups by
     top-level folder and the folder is already completed) — April's suffixed runs were
     created manually, and the current delivery's experiment (32588, staging dir prepped
     2026-09-03 by Will, yaml+samplesheet only, no data) is the same manual workflow
     half-started. sluice is now the only automatic path for deliveries.
   ⚠️ **Caution for the next daemon start: detection WILL surface the pending
   `…-SecureBio_02_SOL` delivery as a NEW ACTIVE run and drive it.** Before starting,
   either pre-seed it with Will's manually-created experiment id (insert_run with
   `experiment_id`, lifecycle active → gate 0 resumes, zero LabKey calls, no duplicate) or
   skip it if it should stay manual. Note Will has pointed the NVD upload lists at TEST
   lists in `~/.sluice/config.toml`, so an end-to-end beta run uploads nowhere production.
3ac. ⛔ **RUNAWAY SECOND DAEMON START — KILLED, FENCED, TWO NEW MUST-FIX BUGS** 2026-09-04
   (same CHTC session, ~13:54–13:56). Will unskipped the base `20260828…0213` run and started
   the daemon with the 3ab code in place. Detection worked as designed and surfaced **27
   delivery runs** (GR0032 / GR0079 / SecureBio_02_SOL subdirs exist across the whole remote
   backlog — deliveries are the NORM, not the exception). The daemon then drove the base run
   and its download submitted 2,259 files. Killed with `kill -9` + targeted `pkill` of the
   rsync tree after Ctrl-C proved useless; state fenced afterwards: **all 107 runs skipped,
   `paused=1`.** Damage: **32 files / 40 GB** of delivery data downloaded into
   `…/32575/raw_seqs/` — all identifiable (`find … -newermt "2026-09-04 13:54"`), operator
   owns deletion per ADR-0005; nothing else ran, no LabKey writes (the 27 delivery rows all
   have `experiment_id=None` — gate 0 never reached them).
   - **BUG A (HIGH): transfer ignores detection's unit boundary for BASE runs.** The 3ab
     review fix made `_list_directory` recursive (`-r`, closing finding 1), but an undashed
     run's literal listing now absorbs every delivery subdirectory beneath it — the mirror
     image of the hole it closed, and review finding 6 said this out loud ("literal always
     wins only holds when…"). The 2,259-file download WAS the delivery, fetched under the
     base run's identity into the base experiment's staging. Fix: apply detection's unit
     rule in `list_run_files` — an undashed run keeps only depth-1 files from its recursive
     listing; a delivery run keeps everything under its subdir. The 3ab test
     `test_listing_is_recursive_so_nested_files_are_reachable` encodes the WRONG expectation
     (nested files returned for an undashed run) and must be rewritten against the delivery
     branch.
   - **BUG B (HIGH): SIGINT/SIGTERM cannot stop an in-flight download.** ~40 Ctrl-C presses
     over two minutes each logged `daemon.shutdown_requested` while the wave loop kept
     submitting (`should_stop` is checked between runs/cycles but never inside the download
     wave loop). An operator's only real stop is `kill -9`, which orphans live rsyncs.
     `pause` correctly cannot help (state-only, next-cycle). Fix: thread `should_stop` into
     the wave-submission loop.
   - Also observed: sluice fetches `*_trimmed*` and stats files the poller's md5-filtered
     download never took — a deliberate-or-not scope difference worth a decision;
     and the delivery backlog means the seed-and-skip fence must cover DELIVERY units too
     (the 27 are now hand-fenced with audited skips; a re-run of the seeder covers only
     poller-known names).
   - **CORRECTED BY WILL (same day): the 27 delivery runs are mostly NOT an unanalyzed
     backlog.** The poller processed each folder as an UMBRELLA — its recursive listing
     folded every delivery subdir present at processing time into the one base run
     (verified: `…/32351/raw_seqs/` still holds `SecureBio_02_SOL_md5s.txt`, the delivery's
     manifest inside the umbrella experiment; raw data itself is cleaned post-backup, so
     staging cannot generally answer "was it analyzed"). The manual suffixed runs (April
     trio + 32588 prep) are all `-SecureBio_02_SOL` — separate re-analysis of that org's
     deliveries by policy, not late-arrival rescue. **Triage rule for the 27 fenced
     delivery runs: they ride their umbrella experiments unless Will wants a separate
     analysis (his SecureBio practice) — his call per delivery.** Forward-looking fork to
     confirm: sluice's per-delivery model deliberately DIVERGES from the poller's umbrella
     ingestion for future folders — each future delivery gets its own experiment and
     analysis. Decided 2026-09-04; reconfirm knowing the umbrella history.
   **Next session, in order: TDD Bug A + Bug B, re-review, then the 32588 pre-seed plan
   (3ab caution) for the SecureBio_02_SOL delivery beta. Working tree still uncommitted:
   3ab code + all ledger entries.**
3ad. ✅ **IDENTITY DECISION REVERSED: UMBRELLA RUNS — 3ab's per-delivery split REVERTED**
   2026-09-04 (same session). **User decision, final: one folder = one run = one
   experiment; deliveries roll up, matching the poller's actual ingestion** (3ac's
   correction). Implemented test-first: detection groups all nested files (any depth) into
   the top-level folder, a nested manifest vouches for the umbrella; transfer is ONE
   recursive listing of the literal name — the delivery-form fallback and its
   name↔path inverse mapping are DELETED, which **closes review findings 2/3/6 and
   Bug A structurally** (recursive absorb is now the intended behavior, and the reviewer's
   ``-r`` fix (finding 1) is kept). Suite **757 passed**, lint clean, uncommitted.
   - Consequences: the 27 runaway suffixed delivery rows were dead identities — never
     re-detectable — and were **DELETED by Will's decision later this session** (one txn,
     audited as `delete_runs` with all names; DB back to the 79 seeded runs, the three
     legitimate April `-SecureBio_02_SOL` re-analyses retained). The 40 GB in `…/32575/raw_seqs/`
     is now arguably the base run's own data (umbrella), though tail files may be
     partial — rsync ``-avP`` re-verifies on any future re-entry. Separate SecureBio-style
     re-analyses (April trio, 32588) stay a manual workflow outside sluice.
   - **Bug B (SIGINT ignored by the download wave loop) is now THE remaining gate before
     any daemon restart.** Everything stays skipped + paused until it lands.
3ae. ✅ **BUG B FIXED + DOWNLOAD SCOPE MATCHED TO THE POLLER** 2026-09-04 (same session,
   after a second runaway kill — the umbrella re-download was fetching `*_trimmed*`/stats
   files and Ctrl-C again could not stop it). Both TDD red-green; suite **759 passed**,
   lint clean, uncommitted.
   - **Bug B:** new core module `sluice/core/stop.py` — ONE module-level cooperative
     `threading.Event`. The daemon's signal handlers set it (run_forever now uses and
     CLEARS it at startup); the download wave loop checks it before every submission:
     in-flight transfers drain, everything unsubmitted becomes a "shutdown requested"
     failure — fail-closed, the gate can never claim success on an interrupted download.
     Placed in core because gates cannot import `core.daemon` back (registry cycle).
     Daemon import-surface pin updated (`sluice.core.stop` allowed, commented).
   - **Scope:** `_parse_listing_filenames` now keeps only `*001.fastq.gz` + `*md5s.txt`
     + `*.sha256` — the poller's include-filter scope; trimmed/stats/fastp files are
     derivable by NVD and were never production inputs. "Listed zero files" now honestly
     means "zero pipeline inputs".
   - Operational note: `poll_interval_minutes` dropped 30 → 1 by Will (the 30 was a
     poller-ism; sluice's look is one cheap listing connection and the interval is the
     operator-command latency).
   **The daemon is now safe to restart: Ctrl-C works mid-download, and the umbrella
   re-download of `20260828…` will skip everything already present and fetch only raw
   fastqs + manifests.**
4. **Sync macOS → CHTC.** ⚠️ **This item previously read "or stand up a shared git remote, which
   is the real fix" — the remote already exists** (`origin`, §4.3), so that half is done and the
   remaining work is smaller than this item used to imply. CHTC can `git clone`/`git pull`
   `main` for everything tracked. What git does **not** carry, and what still has to move out of
   band: `.claude/` (agents, hooks, settings — the guardrail hooks protect only the copy they
   live in), the subsystem map, `notes/`, `specs/`, and `docs/dev-cycle/`. Confirm the design of
   record is byte-identical across copies while doing it.
   **Note the branch gap:** a CHTC clone gets `main` at `0bc8858` and therefore does **not** get
   the TOML config, the NVD upload keys, the one-host DB guard, or the daemon liveness check —
   those are the five unpublished commits on `config-toml-and-nvd-upload-targets` (§4.3 item 14).
   Syncing CHTC before that branch is published would install a sluice missing its most recent
   production fix.
   **Drift status: UNKNOWN, not clean.** CHTC was last observed 2026-07-17 (§9) and this session
   ran entirely on macOS with no access to the CHTC working copy (§2), so the §2/§3 drift could
   not be checked here and nothing in this update should be read as having cleared it.
5. Throughout: python-poller keeps running untouched; sluice stays observe-only until Plan A+B reach
   parity and the drain completes.
