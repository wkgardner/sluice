# ADR-0004: Contracts are binary — a task is in (mandatory) or out (excluded)

Status: accepted (2026-07-27, decided by Will)

## Context

The profile subsystem shipped a three-value applicability vocabulary
(`required | optional | disabled`), and the draft reconcile plan built a
scheduling tier on top of it: "optional" gates that run opportunistically
while required work remains and are skipped at completion. Reviewing that
design, Will rejected the framing outright: "Anything that is optional is not
included in the contract. Optional steps are not still run and then skipped
once all the mandatory completes."

## Decision

1. **A run's contract is binary.** When the contract is established, task
   definitions' *traits* are evaluated against the run and the contract ends
   up containing exactly the tasks that will run — every one of them
   mandatory. Excluded tasks are simply absent. There is no runtime
   "optional" tier; no task is scheduled-then-skipped.
2. **The vocabulary shrinks to `required | disabled`.** `Applicability.OPTIONAL`
   is removed from `sluice/core/profile.py` (test-first, as reconcile's
   opening unit). `disabled` remains the word for "out of the contract";
   disabled tasks still satisfy dependents' needs by absence (needs-closure
   unchanged).
3. **Remediation is exclusion, not skipping.** A stuck run is fixed by the
   operator removing the problem step from its contract; because reconcile is
   stateless and re-reads state every look, the run resumes past the missing
   step automatically. The per-run exclusion op belongs to control
   (subsystem 8); trait-based contract construction is Plan B's profile
   story.
4. **A contract task that returns SKIPPED quarantines the run.** Every task
   in the contract is mandatory, so "skip me" from inside the contract is a
   contradiction between the gate and the contract — a human decides which
   is wrong. (Extends ADR-0002's all-failures-quarantine.)

## Consequences

- Reconcile simplifies: no "due optional" definition, no
  required-needs-optional deadlock rule — both questions evaporate (profile
  plan ⚑1's deferral is discharged as moot). `is_complete` = every contract
  gate's column truthy; `next_gate` = first contract gate with needs
  satisfied and column false, lowest order.
- Supersedes: the three-value vocabulary decision in plan 05 (its
  `REFERENCE_PROFILE`s already use only required/disabled, so both shipped
  profiles survive unchanged); the draft reconcile plan's ⚑1/⚑2 optional
  branches.
- The SKIPPED outcome stays in the Gate contract (a gate may still report
  it); its meaning is now uniformly "quarantine" when the gate is in the
  contract.
