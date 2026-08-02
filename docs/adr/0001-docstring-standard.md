# ADR-0001: Docstring standard — Google style, constraints over justifications

Status: accepted (2026-07-21)

## Context

Docstrings in sluice are read by two audiences: humans, and the agents that
plan, test, implement, and review each subsystem (plan-author, test-author,
implementer, code-reviewer, docs-builder). Agents extract docstring sections
mechanically, so structural consistency matters more than eloquence — a
`Raises:` contract buried in a prose paragraph is invisible to a scan that
found it in the previous function's `Raises:` section.

The early db work showed the failure mode is not under-documentation but
over-length and inconsistency: some functions used Google-style sections while
others carried the same contract in flowing prose, and a large share of the
length defended past decisions (code-review arguments frozen in amber) rather
than constraining future ones.

## Decision

All docstrings follow these five rules:

1. **Google style, always.** Summary line in imperative mood stating behavior
   and return value; `Args:` / `Returns:` / `Raises:` / `Yields:` sections
   whenever they apply — never that information in prose paragraphs instead.

2. **Types live in signatures only.** Never repeat a type in the docstring (it
   drifts). An `Args:` entry exists only for a parameter with constraints
   beyond its type hint: a default with meaning, a required-keyword rationale,
   a valid range.

3. **`Raises:` is mandatory and exhaustive** for anything that can escape the
   function, including exceptions propagated from callees that a caller would
   plausibly want to catch.

4. **One "why" paragraph, constraints only.** Spec and errata references
   (§n, A1…), invariants, and caller hazards ("do not wrap in `write_txn`",
   "a bare `except KeyError` swallows this") stay — they are the
   highest-value content for both audiences. Decision justifications and
   alternatives-considered move to the subsystem plan or an ADR; the
   docstring cites the reference and stops.

5. **Budget: summary + at most one why-paragraph + sections.** If a docstring
   needs more, that content belongs in `CONTEXT.md`, the subsystem plan, or an
   ADR, with a reference from the docstring.

The test for rule 4 is: does this sentence change what a reader (human or
agent) *does next*? "Do not close the yielded connection" does; "this pragma
is already the default so passing it buys no contention win" does not.

## Mechanical enforcement

The structural half of this standard (rule 1) is enforced by ruff's
pydocstyle rules: `select D` with `convention = "google"` in `ruff.toml`,
gating CI. Two deliberate deviations from the stock google convention:

- **`D417` (undocumented-param) is ignored** — it demands every parameter be
  documented, which contradicts rule 2: an `Args:` entry exists only where it
  adds a constraint beyond the type hint.
- **`D1` (missing-docstring) is relaxed under `tests/`** — short parametrized
  assertion functions whose names already state the behavior don't need a
  docstring; substantial tests still follow the "what regression does this
  pin" pattern by convention.

The substantive half (rules 2–5) cannot be linted; it is enforced by the
agents: implementer and test-author read this ADR before writing docstrings,
and code-reviewer checks compliance as a named review dimension.

## Consequences

- Docstrings shrink (the db.py exemplar drops ~60% on its longest function)
  while keeping every contract and hazard.
- Agents can rely on finding the exception contract under `Raises:` and
  parameter constraints under `Args:` in every function, repo-wide.
- code-reviewer may flag docstring drift (missing `Raises:`, prose-buried
  contracts, justification creep) the same way it flags spec drift.
- Decision rationale is not lost — it is relocated to plans and ADRs, which
  is where the reviewer agents already look for it.
