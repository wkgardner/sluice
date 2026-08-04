# Adding a gate

A gate is the unit of pipeline work in sluice. Adding one is five edits in a fixed
order. Nothing else in the system changes — not the daemon, not the CLI, not the
composition root.

This document assumes you have read the **Gate** and **Contract** entries in
[`CONTEXT.md`](../CONTEXT.md). It does not assume you have read any plan.

---

## What a gate actually is

One frozen record of eleven fields, defined in [`sluice/core/gate.py`](../sluice/core/gate.py).
There is no base class and no `def run(self)` — deliberately. Every gate in the
system fills in exactly the same eleven fields; only the function bodies differ.

The single rule that shapes everything else:

> **A gate returns a result. It never writes to the database.**

A gate has no database handle and cannot mark itself done. It returns a
`GateResult`, and the daemon writes the satisfaction column and any declared
outputs in one transaction ([`daemon.py`](../sluice/core/daemon.py), the
`GateOutcome.SUCCESS` branch). This is ADR-0002, and it is why a crash mid-gate
can never leave a half-recorded run.

---

## The four steps

### 1. Add the migration

Every gate owns exactly one `runs` column meaning "this gate is done". It ships in
the same change as the gate — never batched with others (the incremental-migration
policy, plan 13 ⚑3).

Append to the bottom of the ladder in [`sluice/core/db.py`](../sluice/core/db.py),
next to `_migration_011_nvd_completed`:

```python
def _migration_012_my_gate_done(conn: sqlite3.Connection) -> None:
    """Add the ``my_gate`` gate's satisfaction column to ``runs``.

    ``INTEGER NOT NULL DEFAULT 0``, never nullable: a nullable satisfaction
    column gives reconcile three-valued logic (``WHERE col = 0`` silently misses
    NULL), and the constant default keeps the ALTER legal on a populated table —
    pre-existing rows land unsatisfied, which is correct because they never ran
    the gate.
    """
    conn.execute(
        "ALTER TABLE runs ADD COLUMN my_gate_done INTEGER NOT NULL DEFAULT 0",
    )
```

Then register it in the `MIGRATIONS` tuple:

```python
    (12, "012_my_gate_done", _migration_012_my_gate_done),
```

**A shipped migration is never edited.** If you get the column wrong, add 013.

### 2. Write the gate module

One file: `sluice/gates/my_gate.py`. The shape is always the same three layers.

```python
"""The ``my_gate`` gate — order N, one sentence on what it does.

Say here what the gate reads, what it produces, and what its failure means
operationally. If you ported it from python-poller, cite the source lines and
name every place you deliberately deviated.
"""

import pathlib
from typing import Any

import structlog

from sluice.core.gate import Gate, GateOutcome, GateResult, no_op_invalidate

__all__ = ["MY_GATE", "run_my_gate"]

log = structlog.get_logger("sluice.gates.my_gate")


# ---- Layer 1: plain functions. No context, no database, no framework. ----
# This is where the real work goes, and it is where your tests should aim.


def do_the_actual_work(some_dir: pathlib.Path) -> list[str]:
    """Ordinary Python taking ordinary arguments."""
    ...


# ---- Layer 2: the orchestrator. Reads the context, returns a result. ----


def run_my_gate(context: Any) -> GateResult:
    """Decide this gate's outcome for one run.

    Reads ``context.run_row`` (a sealed snapshot — no DB touch),
    ``context.workdir``, and ``context.config``. Returns; never writes.
    """
    run_name = context.run_row["name"]
    workdir = pathlib.Path(context.workdir)

    problems = do_the_actual_work(workdir)
    if problems:
        # A FAILURE *must* carry a reason — the schema cannot persist a
        # quarantine without one, so GateResult refuses to construct.
        return GateResult(
            GateOutcome.FAILURE,
            reason=f"my_gate: run {run_name!r}: {problems[0]}",
        )

    return GateResult(GateOutcome.SUCCESS, metadata={"things_done": 3})


# ---- Layer 3: the task object. ----

MY_GATE = Gate(
    id="my_gate",
    name="Human-readable label",
    satisfied_column="my_gate_done",  # migration 012
    order=6,  # TRUE execution order
    needs=("nvd",),  # gate ids that must be satisfied first
    produces=("my-output/",),  # what it creates, honestly
    run=run_my_gate,
    invalidate=no_op_invalidate,  # say WHY in this comment
    terminate=None,
    persists={},  # metadata key -> runs column
)
```

**If your gate talks to the outside world**, put the adapter in its own module
(the [`transfer.py`](../sluice/gates/transfer.py) / [`labkey.py`](../sluice/gates/labkey.py)
precedent) and inject it behind a zero-argument callable so tests can substitute
it:

```python
def run_my_gate(context: Any, make_client) -> GateResult:
    ...
    client = make_client()          # constructed only when actually needed


def _run_my_gate(context: Any) -> GateResult:
    """Production wiring: defer client construction behind the zero-arg seam."""
    return run_my_gate(context, functools.partial(make_my_client, context.config))
```

Then `run=_run_my_gate`. If your seam is a plain default argument instead
(`popen=subprocess.Popen`, as in [`nvd.py`](../sluice/gates/nvd.py)), skip the
wrapper and point `run` at the gate function directly.

### 3. Add it to the profile

One line in [`sluice/gates/profiles.py`](../sluice/gates/profiles.py):

```python
        (MY_GATE, Applicability.REQUIRED),
```

There is no third state. Under ADR-0004 a contract is binary: a gate is
`REQUIRED` or `DISABLED`, and a gate you have not built yet is simply **absent**.
A run whose built gates all succeed completes deliberately.

Nothing downstream needs wiring. The composition root at
[`cli/__init__.py:144`](../sluice/cli/__init__.py#L144) already unions the
reference and production registries.

### 4. Teach `sluice doctor` about it

**The drift guard fails the suite until you do this.** A gate that reads a new path, secret
or host without doctor learning to check it would ship a preflight that misses the thing
most likely to be wrong.

If your gate reads **any new config key**, that key needs a `probe=` classification in
[`sluice/config/schema.py`](../sluice/config/schema.py). `ProbeKind` has no default, so
you cannot add a key without answering "can this be checked?":

```python
    ConfigField(
        "my_gate_reference_data",
        FieldKind.EXTERNAL_PATH,
        "my gate",
        "Reference data the gate reads.",
        probe=ProbeKind.PATH_EXISTS,   # or NONE, for a tuning value
    ),
```

Then register the gate in **both** maps in [`sluice/doctor.py`](../sluice/doctor.py):

```python
GATE_CONFIG_GROUPS = {
    ...
    "my_gate": ("my gate",),          # the schema `group`s its config lives in
}
```

and in `_GATE_MODULES` in [`tests/test_doctor_drift.py`](../tests/test_doctor_drift.py),
naming every module the gate's config is read from — including any shared adapter:

```python
    "my_gate": ("sluice/gates/my_gate.py", "sluice/gates/my_adapter.py"),
```

A gate present in the production profile but missing from either map is a **hard failure**
of the guard, not a silent exemption. If `pixi run test` tells you doctor does not cover a
key you just started reading, add the group — do not weaken the guard.

Severity is derived, not chosen: a missing `MANAGED_PATH` warns (setup recreates it),
anything only a human can supply fails.

### 5. Write the tests

`tests/test_my_gate.py`. Aim most of them at layer 1 — the plain functions — and
cover at minimum:

- each distinct FAILURE path, asserting on the reason text an operator will read;
- the SUCCESS path, asserting the `metadata` keys your `persists` declares;
- resume behaviour: run the gate twice and check the second run is safe.

---

## The six mistakes the system punishes structurally

These fail loudly rather than silently, but knowing them saves an hour each.

| Mistake | What happens |
|---|---|
| `order` copied from column order | `validate_gates` raises at import: *"needs X, which does not strictly precede it"*. `order` is **true execution order** — the poller's `PIPELINE_GATE_COLUMNS` lists `downloaded` before the registration that must run first (errata B3/A6). |
| A `persists` key your gate does not return | The daemon quarantines the run: *"returned SUCCESS but omitted declared persists key(s)"*. The output and its checkmark move together or not at all (ADR-0006 d.2). |
| `persists` pointing at your own `satisfied_column` | `validate_gates` raises — the persisted value would clobber the checkmark written in the same transaction. |
| A FAILURE with no `reason` | `GateResult` refuses to construct. The schema's `CHECK ((error_reason IS NULL) = (errored_at IS NULL))` makes a reasonless quarantine unpersistable. |
| Reading a config key not in the schema | `tests/test_config_schema.py` fails. It `ast`-parses the code for `config["key"]` reads and asserts the schema covers exactly those keys, in both directions. Add the key to [`sluice/config/schema.py`](../sluice/config/schema.py). |
| A gate doctor does not know about | `tests/test_doctor_drift.py` fails, naming the gate and the config group to add. A gate in the production profile that is absent from `GATE_CONFIG_GROUPS` or `_GATE_MODULES` is a hard failure, never a silent pass — see step 4. |

Two more that are quieter:

- **A gate that reports satisfied without doing its work is forbidden.** If a
  config flag would make your gate a no-op, do not honour it — `DISABLE` the gate
  instead. See the `nvd_auto_run` refusal in [`nvd.py`](../sluice/gates/nvd.py).
- **`invalidate` never deletes anything automatically** (ADR-0005). Use
  `no_op_invalidate` and say why in the comment. Write your own only when the
  no-op is load-bearing — [`experiment.py`](../sluice/gates/experiment.py) keeps
  its own because clearing `experiment_id` would orphan a real LabKey experiment.

---

## Checking it worked

```bash
pixi run test          # full suite
pixi run lint          # ruff check + format
```

Two architecture guards run inside the suite and will catch the structural
mistakes above: `tests/test_dependency_arrows.py` (nothing in `sluice/core/` may
import `sluice.gates`) and `tests/test_predicate_twins.py`.

To see your gate in the real pipeline rather than in tests, `tests/test_end_to_end.py`
drives the daemon through the CLI with nothing injected below it. A new gate
should not need to change that test — if it does, that is a signal worth reading.
