# ADR-0005: sluice never deletes files — operators own destructive filesystem actions

Status: accepted (2026-07-29, decided by Will)

## Context

The control plan's rewind ops (reset_gate/backtrack) were designed to call each
gate's `invalidate` — deleting the gate's artifacts — before clearing its
satisfaction columns, and a safety rule (hold-before-rewind) existed to keep
that deletion from racing the daemon. Asked to approve the safety rule, Will
rejected the premise: "why are we deleting files? … if need be I can delete
the files but having a daemon that does deletion of files seems wrong."

## Decision

1. **No sluice component deletes files automatically** — not control ops, not
   the daemon, not gates invoked on the system's initiative. Destructive
   filesystem actions are the operator's, performed manually.
2. **Rewind is column-clear only.** reset_gate/backtrack clear satisfaction
   columns (+ completed_at) in one transaction; no artifact I/O of any kind in
   control.py. The daemon re-runs cleared steps; the gate contract's existing
   re-entrancy requirement (run tolerates pre-existing/partial artifacts —
   the same property crash-retry already demands) is what makes this sound.
3. **`Gate.invalidate` is no longer invoked automatically by anything.** It
   remains in the committed contract as a dormant hook; whether Plan B removes
   it, repurposes it, or gives it an explicit operator-invoked path is a Plan B
   decision that must cite this ADR.
4. **Errata B7's "reap gate" recommendation (automatic raw-FASTQ deletion) is
   frozen pending Will's explicit sign-off at Plan B** — an automatic deletion
   gate contradicts decision 1 as a default; if raw cleanup is wanted, the
   choices are manual deletion or a consciously approved exception.

## Consequences

- Control's rewind choreography collapses from three phases to one clean
  transaction (no disk phase, no crash window between artifact deletion and
  column clearing — that entire failure class is gone).
- The hold-before-rewind safety rule loses its file-safety rationale. A
  residual, weaker rationale remains (rewinding an actively-running gate can
  be immediately re-stamped by the in-flight work — surprising, not harmful);
  the control plan records the chosen posture on that separately.
- Mutation-witness procedures in the build workflow (agents temporarily
  editing source) are unaffected — this ADR governs the shipped system's
  runtime behavior, not development tooling.
