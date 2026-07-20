# sluice agentic workflow — roster & how to run it

**This file is the canonical roster.** (`.claude/agents/README.md` points here — do not maintain a
second copy.)

These agents build the **sluice** control core (Plan A) test-first, one unit at a time, with
independent review. They target the **sluice** repo — `/home/wkgardner/sluice` (CHTC) or
`/Users/willgardner/Bioinformatics/sluice` (macOS) — **not** `python-poller`. They live in
`sluice/.claude/agents/` and **are registered**: dispatch with `subagent_type: <name>` from a
sluice-rooted session. After adding or editing one, reload (`/agents`, or restart) before it
resolves.

## Roster (10)

| Agent | Role | Writes | Suggested model |
|---|---|---|---|
| `plan-author` | one subsystem → test-first plan | plans only | opus |
| `plan-reviewer` | gate the plan before any code | nothing (read-only) | opus |
| `test-author` | one unit → one failing RED test | tests only | inherit/sonnet |
| `implementer` | make the RED test GREEN + refactor | code only | inherit/sonnet |
| `code-reviewer` | adversarial, **independent** correctness review | nothing (read-only) | opus |
| `integration-tester` | subsystem emergent-behavior test | tests only | inherit/sonnet |
| `build-verifier` | run suite + arrows guard, report | nothing (read-only) | haiku |
| `docs-builder` | mermaid/HTML review diagrams | docs only | inherit/sonnet |
| `teachme` | teach the concepts + the real code behind them | docs only | opus |
| `git-steward` | pre-commit secret/state gate, commits, build record | git + HANDOFF | inherit |

Models are recommendations — spend where the reasoning is hardest (planning + review on opus,
mechanical verify on haiku).

**Retired 2026-07-20:** `tdd-engineer` (superseded by the `test-author` → `implementer` split, which
removes the grade-your-own-homework bias of one agent writing both test and code) and
`core-reviewer` (merged into `code-reviewer`, which absorbed its spine / concurrency / errata-trap
checklist — two reviewers with ~90% overlap meant neither got read carefully).

## The loop (per subsystem)

Dispatch subsystems in **dependency order**, not the order they're listed:

```
db → gate contract (+ one mock gate early) → events → context → profile → reconcile → audit → control → daemon
```

> **One caveat on `events` at position 3 (errata B8/B9).** Everything B8 asks for is buildable
> there: the core-clean structlog helpers, the injected `Notifier` Protocol, and dropping the
> gate-exception string sniffing. But **B9 — the single canonical per-run wide event — cannot be
> closed until `reconcile` exists**, because the decided answer is "emit it at `next_gate == None`".
> So build `events` here, and treat the canonical-line wiring as a deliberate follow-up landed with
> or after `reconcile`. Say so in the `events` plan rather than leaving B9 looking resolved.

Then, for each subsystem:

1. `plan-author(subsystem)` → subsystem plan.
2. `plan-reviewer` → **READY TO BUILD** / **REVISE** (bounce back to plan-author if REVISE).
3. For each unit, in plan order:
   - `test-author(unit)` → one failing RED test  →  `implementer(unit)` → GREEN + refactor.
   - `code-reviewer(unit)` → APPROVE / CHANGES REQUESTED. **Feed it the diff + plan + spec only —
     never the implementer's reasoning.** Bounce findings back to `implementer`.
   - `build-verifier` → PASS/FAIL + the dependency-arrows result.
   - **Human commits** the green, reviewed unit (via `git-steward`, on your explicit ask).
4. `integration-tester(subsystem)` → the emergent-behavior test (control write → reconcile → act).
5. `docs-builder(subsystem)` → diagrams; `teachme(subsystem)` → the lesson. Human reviews both
   before moving on.

## Rules that bind every agent

- The **DB is the single source of truth**; control ops only *write state* (+ audit + event) and
  never start/stop work; **the daemon is the only actor**.
- **`gates → core`, never the reverse** — enforced by `tests/test_dependency_arrows.py`; keep it green.
- Gates conform to the **frozen-dataclass-of-functions** Gate contract (design of record §4) — never
  a class with methods.
- **No agent commits.** The human commits reviewed, green units. No `git add` by any agent.
- **Reviewers are independent**: fed the diff + plan + spec, never the author's narrative. A green
  suite with weak tests is exactly what the reviewer exists to catch.
- Python via **pixi** only; functional over OOP; no secrets or production defaults in code.

## Maps to installed skills

- `superpowers:subagent-driven-development` — the conductor pattern this whole loop implements.
- `superpowers:requesting-code-review` / `receiving-code-review` — the `code-reviewer` exchange.
- `superpowers:test-driven-development` — what `test-author`/`implementer` embody.
- `logging` (project skill) — the wide event / canonical log line pattern. **Invoke it when building
  `events`**; it is a direct hit on errata B9, not a general-purpose match.
- `superpowers:verification-before-completion` — what `build-verifier` embodies.
- `superpowers:using-git-worktrees` — only if you later parallelize the independent foundations
  (`db`, `events`, `gate`); keep Plan A serial at first.
