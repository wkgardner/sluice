# sluice — Handoff & Compaction Record

**Updated:** 2026-07-20 (macOS session — bootstrap executed, agent team + guardrail hooks landed)
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

**Still no pipeline logic and still no commits.** The immediate path is: commit the bootstrap →
build **Plan A (control core)** subsystem by subsystem via the agent team (§9a) → Plan B (production
gates) → Plan C (API + CLI). python-poller keeps running untouched throughout; sluice stays
observe-only until Plan A+B reach parity.

---

## 2. Two environments (critical context)

There are **two working copies** of the sluice repo plus the live production repo. Know which you're
in before acting.

| Environment | Path | Role |
|---|---|---|
| **macOS (osx-arm64)** | `/Users/willgardner/Bioinformatics/sluice` | ⭐ **Currently the most advanced copy.** Bootstrap executed; 12 agents, 2 guardrail hooks, subsystem map all present. |
| **CHTC (linux-64)** | `/home/wkgardner/sluice` | Behind macOS as of 2026-07-20 (see §9). **Also hosts the live production `python-poller`** at `/home/wkgardner/python-poller`. |
| **Production** | `/home/wkgardner/python-poller` (CHTC) | **LIVE. DO NOT DISTURB.** Last poll 2026-07-09; 69 runs; dirty working tree. sluice is additive; python-poller is never modified as part of this work. |

**Sync direction has flipped.** The 2026-07-17 handoff had CHTC as the working session; as of
2026-07-20 macOS carries the bootstrap, the agent team, and the hooks. Until a shared git remote
exists, **sync macOS → CHTC**, not the reverse. Note that `.claude/` (agents, hooks, settings) is
part of what must be synced — the production/secrets hook only protects the copy it is installed in.

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
- **Plan C (API + CLI):** daemon supervision — systemd vs screen (E1); `sluice doctor` preflight for
  Globus token / CephFS xattrs / `nvd` CLI / Slack token (E2).

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

### macOS (`/Users/willgardner/Bioinformatics/sluice`) — as of 2026-07-21

**Bootstrap + db subsystem: complete and committed. `pixi run test` → 131 passed (Python 3.14.6, pytest 9.1.1).**

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
- ⚠️ **Untracked-by-design, this machine only:** `docs/adr/0001-docstring-standard.md` (ADR-0001
  itself), `.claude/CLAUDE.md`, the amended `.claude/agents/*.md`
  (implementer/test-author/code-reviewer/docs-builder now enforce ADR-0001), and the refreshed
  `docs/dev-cycle/` 01-db artifacts (symbol-based citations) are all still behind the deny-all
  allowlist `.gitignore` — the human curates the allowlist. Until rules are added, the ADR and the
  agent-level enforcement exist only in this working copy; `ruff.toml`'s comment references the ADR
  by path.
- `pixi.toml` (name `sluice`, `platforms = ["linux-64", "osx-arm64"]`, deps: python 3.14 / pytest 9 /
  structlog) + `pixi.lock` (solved with pixi 0.72.1).
- `config.example.json` — ~68 keys, **placeholders only**, `slack_bot_token` deliberately omitted.
- `.github/workflows/test.yml` — hermetic `pixi run test`, `locked: true`, pinned `pixi-version: v0.72.1`.
- `tests/test_dependency_arrows.py` — the arch guard; installs a `sys.meta_path` blocker and imports
  every `sluice.core` module with `gates`/`api`/`cli` forbidden. **Verified it actually fails** on an
  injected violation.
- `tests/test_guard_hook.py` — 54 cases covering the production/secrets hook on both hosts' paths.
- Package skeleton: `sluice/{__init__,core,api,cli,gates,config}/__init__.py`, `tests/__init__.py`.
- `README.md`, `.gitignore`.
- Docs: design of record, transition, errata, **subsystem map (present here, missing on CHTC)**.
- `.claude/`: 12 agents + README, 2 hooks, `settings.json`, `skills/logging/`.

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
   commit on `main`, not pushed; see §9). Working tree is clean apart from the deliberately
   untracked files listed in §9. **Open item:** decide whether to add `.gitignore` allowlist rules
   for `docs/adr/`, `.claude/CLAUDE.md`, `.claude/agents/*.md`, and `docs/dev-cycle/` so ADR-0001
   and the agents' enforcement of it enter history (human curates the allowlist).
4. **Sync macOS → CHTC** (or stand up a shared git remote, which is the real fix). Must include
   `.claude/` — the guardrail hooks protect only the copy they live in — plus the subsystem map.
   Confirm the design of record is byte-identical across copies while doing it.
5. Throughout: python-poller keeps running untouched; sluice stays observe-only until Plan A+B reach
   parity and the drain completes.
