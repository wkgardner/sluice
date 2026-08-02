# ADR-0006: Gate output persistence — declared typed columns, written with the checkmark

Status: accepted (2026-07-31, decided by Will; evidence in
docs/superpowers/notes/2026-07-31-gate-output-persistence-research.md)

## Context

Plan B's gates produce values that must survive restarts: the LabKey
experiment id and the Globus transfer task id (resume tokens), and Will asked
whether these and "larger pathing information" belong in a separate relational
outputs table. Research across Airflow (whose `xcom` table IS that design, and
whose decade of production reduced it to "small typed references only"),
Prefect (reference-in-DB from day one), Temporal (results embedded in an event
log, with hard size caps), Luigi and Nextflow (the filesystem is the
validation authority) converged: persist small identity/resume tokens in
typed storage; derive paths, never store them; validate artifacts on disk.
sluice already practices all three — `experiment_id` and `globus_task_id`
have been typed `runs` columns since migration 001, `resolve_context` derives
paths, and satisfaction columns are the on-disk-validated markers.

## Decision

1. **The core `Gate` gains an eleventh field: `persists: Mapping[str, str]`**
   (GateResult.metadata key → `runs` column name), default empty. This is a
   conscious contract change: the ten-field pin in `tests/test_gate.py` is
   updated to eleven, citing this ADR — the same governance as ADR-0002/0004/
   0005. Gates still only RETURN values (ADR-0002 intact); the daemon remains
   the sole writer.
2. **Write choreography:** on SUCCESS the daemon writes the declared outputs
   and the satisfaction column in ONE `update_run` inside ONE `write_txn` —
   output and checkmark can never disagree. A declared key missing from the
   result's metadata on SUCCESS quarantines the run (fail loud); a `persists`
   column name that doesn't exist fails loud via `update_run`'s live
   whitelist.
3. **Read path:** `RunContext` carries a sealed (read-only) snapshot of the
   run row, so a resuming gate can read its own prior tokens (e.g. the Globus
   task id) without touching the DB.
4. **The Globus gate splits into `globus_submit` and `globus_verify`.**
   Submit persists the task id atomically with its own satisfaction column
   (`globus_submitted`); verify reads the id back and polls. This makes
   errata A4's persist-before-wait structural: a crash between submit and
   verify can never resubmit a multi-TB transfer. Migration 007 carries the
   extra column.
5. **Paths are never persisted** — derived from config + identity per Plan
   A's `resolve_context` rule (unanimous across the surveyed systems).
6. **The generic `gate_outputs(run_name, gate_id, key, value, ts)` table is
   PARKED, not rejected** — its DDL lives in the research note. Revisit
   triggers, any of: a gate producing outputs that are non-identity-grade,
   not derivable, or unbounded in count; or a third resume token that would
   otherwise force a migration purely for storage. Until a trigger fires,
   the table is not built.

## Consequences

- Plan B needs ZERO new output columns beyond `globus_submitted` — the two
  real tokens already have homes.
- Adding a future *persisted output* means adding a typed column (a
  migration): deliberate, per Will's accepted trade-off — visible, typed,
  CHECK-able state beats a generic blob table until the parked triggers fire.
- Related decision recorded with the gate map (not this ADR): gates have NO
  time bounds (B6 resolved as none — Will, 2026-07-31); a hung gate blocks
  the loop until operator intervention, accepted with the merge-incident
  vector gone.
