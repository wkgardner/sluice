# ADR-0002: Gate failure policy and persistence model

Status: accepted (2026-07-21, decided by Will)

## Context

Preparing the subsystem-2 (gate) review surfaced two points where the sources
disagreed and a decision was required rather than a reviewer's silent pick:

1. Errata B4 offers two retry postures (add `retryable`/`max_attempts` to the
   Gate contract, or quarantine on every failure), and the db plan's
   carried-forward note additionally allowed deferring the choice to the
   reconcile plan.
2. The design of record says the loop persists gate results, but one house
   source contemplated "a gate function that writes its satisfied_column",
   leaving the write model ambiguous.

## Decision

1. **Every gate failure quarantines the run** (B4 resolved, not deferred).
   A `FAILURE` outcome sets `errored_at` + `error_reason`; recovery is always
   an explicit operator `clear_error`. The Gate contract carries **no**
   `retryable` or `max_attempts` fields in Plan A. Stricter is deliberate:
   automatic retries mask real failures, and an operator-visible quarantine is
   the cheapest honest signal. If retry is ever wanted, it arrives as a new
   ADR revisiting this one — never as a quiet field addition.

2. **Gates never write run state.** `Gate.run` does the work and **returns** a
   `GateResult`; the daemon/loop is the sole writer of satisfaction columns
   and error state, in its own short transaction (the loop's persist step).
   No gate function — mock, reference, or production — touches
   `satisfied_column`, `errored_at`, or any `runs` column. Any source phrase
   suggesting gate-side satisfaction writes is to be read as wrong, not as
   license.

## Consequences

- The subsystem-2 plan must encode all-failures-quarantine and the
  loop-persists model; a plan adopting retry fields, deferring B4, or routing
  DB writes through gate code is CHANGES REQUESTED on those grounds.
- `run`'s signature stays pure with respect to run state, which keeps mock
  gates trivially testable (assert on the returned `GateResult`, not on DB
  side effects) and keeps the write path auditable in one place (the loop).
- Reconcile (subsystem 6) and daemon (subsystem 9) inherit both decisions;
  their plans cite this ADR instead of reopening B4.
