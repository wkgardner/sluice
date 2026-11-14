# ADR-0008: Nextflow gates announce their own start, with the Seqera link

Status: accepted (2026-11-13, requested by Will)

## Context

NVD and REMV are Tower-enabled Nextflow runs. The lab watches them on Seqera
Platform, and asked for the "started" message in each run's Slack thread to carry
the run's Seqera watch link.

The daemon used to post `<gate> started` itself, just before `gate.run`. That
cannot carry the link: Nextflow records it a second or two **after** launch, while
`gate.run` is blocked streaming the pipeline's output for hours. The only code
that sees the link in time is the gate.

But gates never hold the notifier (ADR-0002, events plan ⚑5), and `RunContext`'s
field set is closed (plan 04 facts 1–2) so that a gate cannot reach Slack.

## Decision

`RunContext` gains one optional field, `announce_start`: a callable taking the
watch link (or `None`). The daemon sets it only for the gates in
`GATES_ANNOUNCING_THEIR_OWN_START` (`profiling`, `nvd`), and stops pre-announcing
them. The gate calls it exactly once: when the link appears (in a console line or
in this launch's `.nextflow.log`), or with `None` after 200 lines without one, or
when the pipeline ends first.

This is narrower than a notifier. The callable takes one string and posts one
fixed-shape reply (`<gate> started · monitor progress on Seqera: <link>`). It
cannot open, rewrite or close a thread, read state, or mention anyone. The daemon
builds it on `safe_notify`, so a Slack failure never reaches the gate. Its default
is `None`, which `resolve_context` keeps, so every other gate and every directly
built context is unchanged.

## Consequences

- A Nextflow gate that fails before its pipeline prints anything (missing binary,
  bad config) posts no "started", only the quarantine. That reads correctly: it
  never started.
- Adding another Nextflow gate means adding its id to
  `GATES_ANNOUNCING_THEIR_OWN_START` and passing `context.announce_start` to its
  stream loop.
