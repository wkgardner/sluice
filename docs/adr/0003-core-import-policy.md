# ADR-0003: Core import policy — layering yes, stdlib-only no

Status: accepted (2026-07-21, decided by Will)

## Context

The bootstrap (commit 638b748) shipped two bundled import rules for
`sluice.core`: the layering rule (core never imports `sluice.gates`/`api`/
`cli`) and a stricter stdlib-only rule (no third-party imports in core at
all). The stdlib-only rule first bit at subsystem 3, where the spec's own
design says events.py ports python-poller's structlog wide-event approach —
and structlog has been a declared pixi dependency since bootstrap. When the
contradiction was surfaced, Will stated the stdlib-only restriction was never
something he asked for.

Audit at decision time: nothing built under the restriction (db, gate, mock
gates) imports anything beyond stdlib naturally — no design was distorted by
the rule, so relaxing it requires no rework.

## Decision

1. **The layering rule stands, untouched.** Nothing under `sluice.core`
   imports `sluice.gates`, `sluice.api`, or `sluice.cli`
   (`tests/test_dependency_arrows.py`). Core must import and run with every
   production gate absent.
2. **The stdlib-only rule is retired.** Core modules may import third-party
   libraries that are declared project dependencies in `pixi.toml`. First
   beneficiary: `events.py` imports structlog.
3. **Service SDKs stay out of core** — this is errata B8's real point, kept
   deliberately: `slack_sdk` (and any service client: LabKey, Globus, FastAPI)
   is never imported by core; such integrations live behind injected
   Protocols (e.g. `Notifier`) implemented in the outer layers.

## Consequences

- Import-guard tests assert the layering rule and the named service-SDK
  exclusions, not stdlib-purity. `db.py`'s existing stdlib-only test remains
  true as a fact about db.py but is no longer a core-wide policy.
- A new third-party import into core needs a `pixi.toml` entry (already the
  case operationally) — not an ADR. Service clients are the exception: those
  require revisiting decision 3.
- Subsystem 3's events plan proceeds with structlog as designed by the spec.
