# sluice — domain glossary

The ubiquitous language of this codebase. When a proposal, test name, issue, or refactor names a
concept below, it uses **this** word — not a synonym. Where a tempting synonym exists, it is listed
under *Not:* with the reason, because most confusion here comes from importing generic pipeline
vocabulary that means something subtly different.

Binding decisions live in [`docs/adr/`](docs/adr/) and are not re-litigated by exploration or
review; the build record is [`HANDOFF.md`](HANDOFF.md).

## The core nouns

**Run** — one sequencing output directory moving through the pipeline, and the row in `runs` that
tracks it. Its identity is `name`, the directory's own name, which is also the primary key. *Not:*
"job" or "task" (nothing is queued or dispatched — see **Reconcile loop**); *not:* "sample" (a run
holds many).

**Experiment ID** — the LabKey experiment number a run is registered under, persisted on the row by
the experiment gate. This is the key a **human** uses to find a run; `runs.name` is the key the
**machine** uses. Anything an operator reads should surface the experiment ID.

**Gate** — one unit of pipeline work, expressed as a frozen `Gate` record: an id, an `order`, the
`satisfied_column` it marks when done, and a callable. A gate is handed a **run context**, does its
work, and **returns** a `GateResult`. It never writes run state (ADR-0002) — the daemon does that.
*Not:* "step" or "stage" (both invite the idea of a linear script the runner walks).

**Gate outcome** — `SUCCESS`, `FAILURE`, or `SKIPPED`. SUCCESS marks the satisfaction column;
FAILURE and SKIPPED both **quarantine** (ADR-0002, ADR-0004 d.4). There is no retry counter and no
`reset_attempts` op, ever.

**Satisfaction column / checkmark** — the one column on `runs` a gate marks on SUCCESS. Progress is
these columns and nothing else; there is no separate progress table and no status string to keep in
sync. Each new gate ships its own migration adding its own column (the incremental-migration policy,
plan 13 ⚑3).

**Profile** — the named set of gates a `pipeline_version` runs, resolved from a registry.

**Contract** — the gates a *particular* run must satisfy: its profile's gates, minus any
**excluded** for that run. **Binary** by decision (ADR-0004): a gate is either in the contract or
absent from it. There is no runtime "optional" tier, and any contract gate returning SKIPPED
quarantines. *Not:* "required steps"; the contract is the whole per-run set, not a subset of one.

**Exclusion** — the per-run overlay that removes a gate from that run's contract. The sanctioned
remediation when a gate does not apply to one run (ADR-0004 d.3) — the alternative would be editing
the profile, which would change every run.

## The two state axes

These are orthogonal and compose. A run can be both held and quarantined; clearing one does not
clear the other.

**Lifecycle** — `active`, `held`, or `skipped`. The **operator's** axis: what a human decided about
this run.

**Quarantine** — `errored_at` + `error_reason`, set together (a schema CHECK forbids a half-set).
The **system's** axis: something went wrong and the run stops until an operator forgives it with
`clear_error`, the sole sanctioned recovery (ADR-0002). *Not:* "failed" as a lifecycle value — that
would collapse two independent axes into one and make "held **and** broken" unrepresentable.

**Daemon-eligible** — `lifecycle = 'active' AND errored_at IS NULL`: the single rule deciding
whether the daemon may work on a run. Deliberately says nothing about completion — a completed run
stays eligible, because `completed_at` is a re-derived cache and filtering on it could hide a run
from the loop meant to notice it (F4). The rule exists exactly twice, as `db.ACTIVE_RUNS_WHERE`
(SQL) and `db.is_daemon_eligible` (Python), because SQLite cannot call a Python function; the pair
is **irreducible by design** and `tests/test_predicate_twins.py` fails if they drift apart.

## How work happens

**Reconcile loop** — the daemon's model, and the reason there is no queue anywhere. Every look
re-reads state, re-derives the contract, and decides afresh; nothing is remembered between looks.
Consequence: a **control op** changes state and the daemon's *next* look acts on it. Nothing is ever
poked, notified, or dispatched. *Not:* "scheduler", "worker", "dispatcher" — all imply work handed
to something rather than derived from state.

**Control op** — an operator action (`hold`, `skip`, `exclude`, `clear_error`, `reset_gate`, …).
Exactly one `write_txn`: validate → mutate → write the audit row → commit → emit the event. Control
ops and the daemon are the **two write paths** (ADR-0002); the daemon writes its own progress and
never audits it, operators write through control and always do.

**Run context** — the frozen record derived fresh for one gate call: `workdir`, `db_path`, config,
and a sealed snapshot of the run row. Pure path arithmetic over its inputs — it never touches the
database and never creates a directory. Also the **identity→path boundary**: the one place a stored
name becomes a filesystem path, so it is where `is_safe_path_component` refuses a name like `..`.

**Quarantine-grade** — of a failure: severe enough to stop this **one** run, never the daemon. Its
opposite is a **fatal** failure (schema drift, a failed persist), which propagates and stops
everything. Which is which is a deliberate per-call-site decision, never a blanket `except`.

## Structure

**World adapter** — the object at the boundary to an external system (LabKey, rsync, Globus,
Slack): it owns the SDK import, the credentials, and the fail-closed translation of every world
failure into one error type. Lives on the **gates** side of the `gates → core` arrow and is injected
into core through a seam, which is how core imports and tests with every production gate absent.

> The adapters share a *shape*, not an abstraction. The 2026-07-31 architecture review looked at
> extracting a base class for them and recommended **against** it: the shared surface is a
> pass-through, so a base class would move complexity rather than concentrate it. The shape is
> documented here on purpose, so it does not get re-proposed as a refactor.

**Seam** — an injection point where core takes a callable it does not import: the gate registry, the
directory lister, the notifier. Seams are what keep `sluice.core` free of `sluice.gates`,
`sluice.api`, and `sluice.cli` — the one-way dependency `gates → core` that makes sluice a framework
rather than a tangle, enforced by `tests/test_dependency_arrows.py`.

**Twin** — two copies of one rule that genuinely cannot be merged, kept honest by a test that runs
both and asserts they agree. Currently one: the daemon-eligibility pair above. A twin is a last
resort, not a pattern to reach for — the first question is always whether the copies can collapse to
one (plan 14). An unguarded second copy is a bug waiting to happen, not a twin.
