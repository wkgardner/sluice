# Plan 15 — road to MVP (what is left, in what order)

Written 2026-08-01 at Will's request: *"How many more dev cycles before we can start testing, and
what does a working state for a colleague look like this week?"*

Not a build plan — a sequencing document. Each numbered increment below still gets its own
test-first plan citing the gate map ([plan 10](10-plan-b-gate-map.md)).

## 1. The finding that shapes everything

**You do not need all eight gates to put real samples through sluice.**

This falls out of ADR-0004 (binary contracts). A run's contract is its profile's gates minus
exclusions; a DISABLED gate is *satisfied by absence*, and a run whose last enabled gate succeeds
completes **deliberately** — not with a silent "final gate lies" (C4, resolved structurally in the
gate map §4).

So a `production-v3.1` profile with the built gates REQUIRED and the unbuilt ones DISABLED is a
legitimate profile, not a hack. Real runs flow through it, complete honestly at the last enabled
gate, and each new gate increment extends the pipeline without reworking anything behind it.

That turns "wait for parity, then test" into "pilot now, widen weekly."

## 2. Where the build actually is

Plan A (control core) is complete and tagged. Plan B is 3 of its 8 build-sequence steps done.

| Gate map §8 step | Status |
|---|---|
| 1. Detection | ✅ `e37210b` |
| — `production-v3.1` profile registry entry | ❌ **missing** — was meant to land with detection |
| 2. CephFS disk strategy (D1) | ❌ not started (small; unblocks download) |
| 3. Persistence substrate (ADR-0006) | ✅ `80025ad` |
| 4. `experiment` gate (order 0) | ✅ `7461b4f` |
| 5. `download` → `checksum` → `samplesheet` | ❌ not started |
| 6. `nvd` gate | ❌ not started — **highest-risk item in the whole port** |
| 7. `globus_submit` + `globus_verify` | ❌ not started |
| 8. `registration` gate | DISABLED by ⚑D — **not needed for MVP at all** |
| Plan C — CLI / API | ❌ not started — **the gap that blocks a colleague** |

## 3. Two blockers that are not gates

Both are cheap, and both are invisible if you only read the gate map.

**B-1. The `production-v3.1` profile.** Nothing in `sluice/` mentions it; only `REFERENCE_PROFILES`
(mock gates) exists. Detection stamps `pipeline_version` from `metadata.pipeline_profile`, and a
value absent from the registry quarantines every run on `UnknownProfileError` at the daemon's first
look. Half a cycle, and it must precede any live run.

**B-2. No operator surface.** Every control op is built and audited, and none is reachable without
writing Python. For a second person to use sluice, they need at minimum to *see* run state and
*forgive* a quarantine. This is Plan C, and it is the single largest thing standing between "the
engine works" and "a colleague can use it."

## 3a. SCOPE FIXED 2026-08-01 (Will) — read this before §4

Two constraints landed after §4 was written; §4's milestones are superseded by the sequence here.

- **Target is a working pipeline through `nvd`.** The Globus pair is **deferred** — Will takes it
  after vacation. This needs no special work: the gate map §7 already sanctions it (*"a site that
  does not want Globus … ships a profile variant with them DISABLED — and then the run completes at
  `nvd` deliberately, never a silent 'final gate lies'"*, C4 structural). So `production-v3.1`
  ships with `globus_submit`, `globus_verify`, **and** `registration` DISABLED, and enabling the
  pair later is a profile change plus one increment, with nothing behind it reworked.
- **python-poller is not a conflict.** Will controls it and will disable it. The two-systems-
  detecting-the-same-runs question in §6 is closed.
- **Deadline: end of this week**, then Will is away and a colleague operates sluice **unattended**.

### The ordering principle this forces

Every increment leaves a *shippable* pipeline (again the DISABLED-gate property). So the question
is not "what is left" but **"what do we leave Will's colleague with if the last item slips?"**

`nvd` is the highest-risk port in the project and the most likely to overrun. It must therefore be
the thing that slips — never the thing whose slippage makes everything else unusable. Operability
(CLI, alerting) is what makes an unattended week survivable; gates are what make the pipeline
complete. **Operability goes first.**

| # | Increment | Rough | Why here |
|---|---|---|---|
| 1 | `production-v3.1` profile + CephFS disk (D1) | 0.5d | Blocking, entirely offline, no credentials needed. |
| 2 | `download` gate (008) | 1d | First real data. D6 threadpool, ⚑A `experiment_id`-aware workdir. |
| 3 | `checksum` gate (009) | 0.25d | Small, idempotent. |
| 4 | **Minimal CLI** | 0.75d | **Must not be cuttable.** Without it nobody but Will can operate sluice. |
| 5 | **Slack notifier** | 0.5d | **Must not be cuttable.** Implements the `Notifier` Protocol that `events.py:45` always reserved for Plan B. Without it a Tuesday quarantine is discovered Friday. |
| 6 | `samplesheet` gate (010) | 0.5d | NVD tool, `--force` makes re-entry idempotent. |
| 7 | `nvd` gate (011) | 1.5d | Riskiest. D2/A7 launch dir, D3 append log, D5 heartbeats. Budget the most review here; let this be what slips. |

≈5 days of work against ~4–5 available. **It fits only if nothing blocks.** The likeliest blocker is
not code: increments 2 and 7 need real credentials and real paths (`remote_user`, `remote_host`,
`password_file` / `local_source_dir`; the `nvd` CLI on the host sluice will run on). **Confirm those
early** — a credential discovered missing on Thursday is fatal to the deadline, and cannot be
worked around by building faster.

---

## 4. The road, in three milestones *(superseded by §3a — kept for the reasoning)*

Cycle counts are calibrated against observed velocity — three increments landed on 2026-07-31 in
one day — but `download` and `nvd` are materially heavier than anything built so far, so they carry
wider ranges. Treat these as planning ranges, not commitments.

### Milestone 1 — "a colleague can watch real runs" (~4–5 cycles)

The first point at which sluice does real work on real samples and a second person can observe it.

| # | Increment | Cycles | Notes |
|---|---|---|---|
| 1 | `production-v3.1` profile + CephFS disk strategy (D1) | 1 | Combine — both small, both blocking. B-1 above. |
| 2 | `download` gate (migration 008) | 1–2 | Multi-TB rsync. Carries errata **D6** (threadpool contextvar) and deferred **⚑A** — the first gate needing disk, so `resolve_context.workdir` becomes `experiment_id`-aware here. Resume-first: `rsync -avP` skips transferred files. |
| 3 | `checksum` gate (009) | 0.5–1 | Small and idempotent by nature. |
| 4 | **Minimal read-mostly CLI** | 1–2 | `sluice status`, `sluice show <run>`, `sluice clear-error`, `sluice hold`/`unhold`. Deliberately not the full API — a thin Typer client over the existing control ops. |

**End state:** real runs detected, real LabKey experiments created, real data downloaded and
checksum-verified, completing honestly at `checksum`. Your colleague can see state and unstick a
quarantined run without touching Python.

### Milestone 2 — "the pipeline actually processes" (~3–4 cycles)

| # | Increment | Cycles | Notes |
|---|---|---|---|
| 5 | `samplesheet` gate (010) | 1 | NVD tool, `--force` makes re-entry idempotent. |
| 6 | `nvd` gate (011) | 2 | **The highest-risk port.** D2/A7 launch dir must be local POSIX and validated at load; D3 append-mode logs; D5 heartbeats; no time bound (⚑F). Budget the most review here. |

### Milestone 3 — "parity" (~2–3 cycles)

| # | Increment | Cycles | Notes |
|---|---|---|---|
| 7 | `globus_submit` + `globus_verify` (012) | 1–2 | One increment by design — verify is untestable without submit's persisted token. Submit persists `globus_task_id` atomically with its checkmark, so a crash can never resubmit a multi-TB transfer (A4, structural). |
| 8 | `sluice doctor` + daemon supervision (E1/E2) | 1 | Preflight for Globus token, CephFS xattrs, `nvd` CLI, Slack token; systemd vs screen. |

`registration` (order 4) stays DISABLED. C3 fail-closed is the precondition to ever enabling it.

**Total to parity: roughly 9–12 cycles.** To the Milestone 1 pilot: **4–5**.

## 5. The honest answer on "this week"

Milestone 1 is achievable this week at the velocity of the last few sessions. Full parity is not —
`nvd` is the riskiest port in the project and rushing it is how a pipeline silently corrupts a run.

The rate limiter is not agent throughput. It is:

- **Real credentials and real paths.** Every remaining gate touches something outside this laptop
  (the download server, CephFS, the `nvd` CLI, Globus). Config keys exist; whether they are filled
  in and reachable from the machine sluice will run on is unverified.
- **Where the daemon will actually run.** The CHTC copy has been unverified since 2026-07-17.
- **Your review time**, which is the one input no amount of parallelism replaces.

## 6. The decision that is not technical

HANDOFF §8 carries a standing guardrail: *sluice stays observe-only until Plan A+B reach parity and
the drain completes.* A Milestone 1 pilot **changes that decision**, and the change is not trivial:

- The `experiment` gate creates **real LabKey experiments** in a project other people use. The C1
  duplicate hazard is structurally closed (resume-first, lookup-before-create, fail-closed), but the
  rows are real.
- The `download` gate moves **multi-TB** of real data onto real storage.
- python-poller keeps running throughout. Two systems detecting the same runs need a deliberate
  answer — separate `watch_dir`, a distinct LabKey project, or a hand-picked set of pilot runs —
  or they will both act on the same sample.

That last point is the one to settle before any pilot, and it is a question about the lab's
environment, not about this code.

## 7. Recommended next action

Start Milestone 1 increment 1 (`production-v3.1` profile + D1 disk strategy). It is small, it is
blocking, it is entirely offline, and it needs no credential decisions — so it can proceed while §6
is being settled.
