# `sluice doctor` Increment A — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `sluice doctor` — a read-only command that proves a configured sluice installation can actually work, using local checks only.

**Architecture:** A new `sluice/doctor.py` at the package root, a consumer of both `core` and `gates` (the position `cli` occupies). Verifiability is declared per config key in the existing schema via a new `ProbeKind`; doctor turns those declarations into checks, runs only the ones the active profile needs, and reports. An ast-parse drift guard fails the suite when a gate reads a probe-bearing key doctor does not check.

**Tech Stack:** Python 3.14, pytest, Typer + rich (already present), stdlib `ast`/`shutil`/`sqlite3`. No new dependencies.

**Spec:** [`docs/superpowers/specs/2026-08-02-sluice-doctor-design.md`](../specs/2026-08-02-sluice-doctor-design.md) — increment A is §8 "A".

## Global Constraints

- **ADR-0003 dependency arrow:** `sluice.core` must never import `sluice.gates` / `api` / `cli`. `sluice/doctor.py` may import both core and gates; **nothing in `sluice/core/` may import `sluice.doctor`**. Enforced by `tests/test_dependency_arrows.py`.
- **ADR-0005 posture:** doctor never mutates. It creates no directory, writes no config, checks out no git ref, touches no run state. Every check is a read.
- **ADR-0001 docstring standard:** every module, public function and non-obvious branch carries a docstring saying *why*, not just what. Match the density of `sluice/gates/checksum.py`.
- **No new dependencies.** `pixi.toml` is not modified by this increment.
- **Secrets never surface.** A credential value is never logged, never returned in a check message, and never placed in an exception. Only the *path* and a pass/fail may appear.
- **Increment A is local-only.** No network call, no subprocess that contacts a host. `--comprehensive` is increment B and is out of scope here.
- **Verify with:** `pixi run test` (full suite) and `pixi run lint` (ruff check + format). Both must be green before any commit.

---

## File Structure

| File | Responsibility |
|---|---|
| `sluice/config/schema.py` *(modify)* | Gains `ProbeKind` and a non-defaulted `probe` field on `ConfigField`; all 44 entries classified. Still the single definition of every config key. |
| `sluice/gates/detection.py` *(modify)* | Gains exported `is_local_mode(config)`; `build_listing_command` calls it instead of inlining the rule. |
| `sluice/doctor.py` *(create)* | The whole diagnostic: `Severity`, `Check`, `CheckResult`, the probe implementations, profile-aware assembly, and `run_doctor`. |
| `sluice/cli/__init__.py` *(modify)* | The `sluice doctor` command: renders results, sets the exit code, honours `--json`. |
| `tests/test_doctor.py` *(create)* | Behaviour of every probe, severity derivation, profile-aware selection, and the assembled run. |
| `tests/test_doctor_drift.py` *(create)* | The ast-parse drift guard. Separate file because it is a *guard*, like `test_dependency_arrows.py` and `test_predicate_twins.py` — a failure here means the architecture slipped, not that a behaviour broke. |
| `docs/OPERATING.md` *(modify)* | An operator-facing section on reading doctor's output. |

---

### Task 1: `ProbeKind` and the 44 classifications

**Files:**
- Modify: `sluice/config/schema.py`
- Test: `tests/test_config_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ProbeKind` enum with members `PATH_EXISTS`, `SECRET_READABLE`, `HOST_REACHABLE`, `ENDPOINT`, `EXECUTABLE`, `NONE`; `ConfigField.probe: ProbeKind` (no default).

**⚠️ Dataclass ordering gotcha:** `probe` has no default, so it must be declared **before** `default`, `home_relative` and `required`, or Python raises `TypeError: non-default argument 'probe' follows default argument`. All 44 existing call sites pass the first four arguments positionally and everything else by keyword, so adding `probe=...` as a keyword works without touching argument order.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config_schema.py`:

```python
def test_every_config_field_declares_a_probe():
    """No config key exists without an answer to "is this checkable?".

    ``probe`` deliberately has no default (design spec D5): a defaulted field
    forgets by construction, and forgetting is the exact drift this whole
    mechanism exists to prevent. This test is belt-and-braces — a missing
    ``probe=`` is already a TypeError at import — but it states the rule where
    someone tempted to add ``= ProbeKind.NONE`` as a default will read it.
    """
    for field in CONFIG_FIELDS:
        assert isinstance(field.probe, ProbeKind), field.name


def test_tuning_values_are_not_probed_and_paths_are():
    """The classification is not uniform — it distinguishes what can be verified.

    A guard against a lazy bulk edit that stamps every key ``NONE`` (which would
    silently disable doctor) or every key ``PATH_EXISTS`` (which would make it
    fail on every install). Names one key of each kind so the assertion says
    what it means.
    """
    by_name = {field.name: field.probe for field in CONFIG_FIELDS}
    assert by_name["nvd_entropy"] is ProbeKind.NONE
    assert by_name["nvd_repo_dir"] is ProbeKind.PATH_EXISTS
    assert by_name["labkey_api_key_file"] is ProbeKind.SECRET_READABLE
    assert by_name["remote_host"] is ProbeKind.HOST_REACHABLE
    assert by_name["nvd_command"] is ProbeKind.EXECUTABLE
    assert by_name["labkey_server"] is ProbeKind.ENDPOINT
```

Update the import at the top of the file to include `ProbeKind`:

```python
from sluice.config.schema import CONFIG_FIELDS, ConfigField, FieldKind, ProbeKind
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pixi run pytest tests/test_config_schema.py -q`
Expected: FAIL — `ImportError: cannot import name 'ProbeKind' from 'sluice.config.schema'`

- [ ] **Step 3: Add the enum and the field**

In `sluice/config/schema.py`, after the `FieldKind` enum:

```python
class ProbeKind(enum.Enum):
    """How ``sluice doctor`` can verify a config key — its *verifiability*.

    Distinct from ``FieldKind``, which answers a different question: who supplies
    the value and can ``sluice setup`` invent it. The two do not line up, and
    conflating them was considered and rejected (design spec §5). Every host,
    endpoint and executable in the system is ``FieldKind.VALUE``, so a drift
    guard keyed off ``FieldKind`` would exempt precisely the deployment-day
    failures errata E2 is about.

    ``NONE`` means "nothing to verify beyond presence" — a tuning knob like
    ``nvd_entropy``, whose presence the schema guard already enforces. It is a
    real answer, not an absence of one, which is why ``ConfigField.probe`` has
    no default.
    """

    PATH_EXISTS = "path_exists"          # a directory or file that must be there
    SECRET_READABLE = "secret_readable"  # a credential file: present and non-empty
    HOST_REACHABLE = "host_reachable"    # part of a connection to another machine
    ENDPOINT = "endpoint"                # names a service or a container within one
    EXECUTABLE = "executable"            # a command that must resolve on PATH
    NONE = "none"                        # a tuning value; nothing to check
```

Then add the field to `ConfigField`, **before** `default`:

```python
@dataclasses.dataclass(frozen=True)
class ConfigField:
    """One config key: what it is, who supplies it, and what it defaults to."""

    name: str
    kind: FieldKind
    group: str
    help: str
    # No default, deliberately (design spec D5): adding a config key must be
    # impossible without answering "can doctor check this?". Declared before the
    # defaulted fields below because a non-default field cannot follow a
    # defaulted one in a dataclass.
    probe: ProbeKind
    default: object = None
    # Relative to the sluice home, for MANAGED_PATH and SECRET_FILE fields.
    home_relative: str | None = None
    required: bool = True
```

- [ ] **Step 4: Classify all 44 entries**

Add `probe=` to every `ConfigField(...)` in `CONFIG_FIELDS`, exactly as follows. The full mapping — 17 `NONE`, 14 `PATH_EXISTS`, 5 `ENDPOINT`, 4 `SECRET_READABLE`, 3 `HOST_REACHABLE`, 1 `EXECUTABLE`:

| Key | `probe=` |
|---|---|
| `state_file` | `ProbeKind.PATH_EXISTS` |
| `events_file` | `ProbeKind.PATH_EXISTS` |
| `run_events_dir` | `ProbeKind.PATH_EXISTS` |
| `events_max_mb` | `ProbeKind.NONE` |
| `events_backup_count` | `ProbeKind.NONE` |
| `poll_interval_minutes` | `ProbeKind.NONE` |
| `watch_dir` | `ProbeKind.PATH_EXISTS` |
| `remote_user` | `ProbeKind.HOST_REACHABLE` |
| `remote_host` | `ProbeKind.HOST_REACHABLE` |
| `remote_source_path` | `ProbeKind.HOST_REACHABLE` |
| `password_file` | `ProbeKind.SECRET_READABLE` |
| `local_source_dir` | `ProbeKind.PATH_EXISTS` |
| `local_staging_dir` | `ProbeKind.PATH_EXISTS` |
| `disk_quota_root` | `ProbeKind.PATH_EXISTS` |
| `disk_headroom_multiplier` | `ProbeKind.NONE` |
| `parallel_downloads` | `ProbeKind.NONE` |
| `download_wave_size` | `ProbeKind.NONE` |
| `download_ramp_delay_seconds` | `ProbeKind.NONE` |
| `labkey_server` | `ProbeKind.ENDPOINT` |
| `labkey_experiments_project` | `ProbeKind.ENDPOINT` |
| `labkey_schema` | `ProbeKind.ENDPOINT` |
| `labkey_query` | `ProbeKind.ENDPOINT` |
| `labkey_api_key_file` | `ProbeKind.SECRET_READABLE` |
| `nvd_repo_dir` | `ProbeKind.PATH_EXISTS` |
| `nvd_command` | `ProbeKind.EXECUTABLE` |
| `nvd_launch_base` | `ProbeKind.PATH_EXISTS` |
| `nvd_log_dir` | `ProbeKind.PATH_EXISTS` |
| `nvd_results_base` | `ProbeKind.PATH_EXISTS` |
| `nvd_profile` | `ProbeKind.NONE` |
| `nvd_blast_db` | `ProbeKind.PATH_EXISTS` |
| `nvd_blast_db_prefix` | `ProbeKind.NONE` |
| `nvd_blast_db_version` | `ProbeKind.NONE` |
| `nvd_taxonomy_dir` | `ProbeKind.PATH_EXISTS` |
| `nvd_virus_index` | `ProbeKind.PATH_EXISTS` |
| `nvd_virus_index_version` | `ProbeKind.NONE` |
| `nvd_cutoff_percent` | `ProbeKind.NONE` |
| `nvd_entropy` | `ProbeKind.NONE` |
| `nvd_tax_stringency` | `ProbeKind.NONE` |
| `nvd_max_blast_targets` | `ProbeKind.NONE` |
| `nvd_dedup` | `ProbeKind.NONE` |
| `slack_enabled` | `ProbeKind.NONE` |
| `slack_channel` | `ProbeKind.ENDPOINT` |
| `slack_bot_token_file` | `ProbeKind.SECRET_READABLE` |
| `slack_user_id_file` | `ProbeKind.SECRET_READABLE` |

Worked example of the edit — before:

```python
    ConfigField(
        "nvd_repo_dir",
        FieldKind.EXTERNAL_PATH,
        "nvd",
        "Checkout of the NVD pipeline repository.",
    ),
```

after:

```python
    ConfigField(
        "nvd_repo_dir",
        FieldKind.EXTERNAL_PATH,
        "nvd",
        "Checkout of the NVD pipeline repository.",
        probe=ProbeKind.PATH_EXISTS,
    ),
```

Note the three keys where the two taxonomies deliberately disagree:
`remote_host`, `labkey_server` and `nvd_command` are all `FieldKind.VALUE`
(setup cannot invent them, but they are not paths) yet carry a real probe.
That divergence is the entire reason `ProbeKind` exists.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pixi run pytest tests/test_config_schema.py -q`
Expected: PASS, including the pre-existing schema/code coverage guards.

- [ ] **Step 6: Run the full suite and lint**

Run: `pixi run test && pixi run lint`
Expected: 656 passed (+2 new = 658), lint clean.
If `sluice setup` tests fail, the `probe` field was declared in the wrong position — see the gotcha above.

- [ ] **Step 7: Commit**

```bash
git add sluice/config/schema.py tests/test_config_schema.py
git commit -m "config: classify every key's verifiability with ProbeKind

Adds ProbeKind and a non-defaulted probe field to ConfigField, classifying
all 44 keys. No default, deliberately: adding a config key is now impossible
without answering whether doctor can check it.

FieldKind is untouched. It answers a different question — who supplies the
value and can setup invent it — and every host, endpoint and executable in
the system is FieldKind.VALUE, so keying a drift guard off it would exempt
exactly the deployment-day failures errata E2 is about.

Design spec D5."
```

---

### Task 2: Export `is_local_mode` from detection

**Files:**
- Modify: `sluice/gates/detection.py`
- Test: `tests/test_detection.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `is_local_mode(config) -> bool` — `True` when `config.get("local_source_dir")` is truthy.

**Why:** doctor must not demand `remote_host` in local mode. Transcribing the rule would make it a **third predicate twin**; plan 14 established that a load-bearing rule gets one home and its copies are deleted.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_detection.py`:

```python
def test_is_local_mode_is_a_presence_check_not_a_value_check():
    """Local mode iff ``local_source_dir`` is truthy — the rule, in one place.

    An empty string is remote mode, not local: the example config documents the
    key as optional-or-empty, so an operator who clears it is switching back to
    remote and must not be silently left in local mode pointed at ``Path("")``,
    which is the current directory. That exact confusion produced one of the
    three integration bugs in 6cffb69.
    """
    assert is_local_mode({"local_source_dir": "/data/incoming"}) is True
    assert is_local_mode({"local_source_dir": ""}) is False
    assert is_local_mode({}) is False


def test_build_listing_command_uses_the_exported_predicate(monkeypatch):
    """``build_listing_command`` calls the predicate rather than re-deciding.

    The twin guard. Patching the predicate to lie must change the command that
    comes out; if it does not, a second copy of the rule has grown back inside
    ``build_listing_command`` and the two can drift.
    """
    monkeypatch.setattr(detection, "is_local_mode", lambda config: True)
    argv, overlay = detection.build_listing_command({"local_source_dir": "/x"})
    assert "sshpass" not in argv
    assert overlay == {}
```

Ensure the test module imports both the symbol and the module:

```python
from sluice.gates import detection
from sluice.gates.detection import is_local_mode
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pixi run pytest tests/test_detection.py -k is_local_mode -q`
Expected: FAIL — `ImportError: cannot import name 'is_local_mode'`

- [ ] **Step 3: Add the predicate and use it**

In `sluice/gates/detection.py`, add to `__all__` (keep it sorted):

```python
__all__ = [
    "RemoteListingError",
    "build_listing_command",
    "is_local_mode",
    "list_remote_runs",
    "parse_rsync_listing",
]
```

Add the function above `build_listing_command`:

```python
def is_local_mode(config) -> bool:
    """Whether detection reads a local directory rather than a remote host.

    A **presence** check, not a value default: the example config documents
    ``local_source_dir`` as optional-or-empty, so an operator switching back to
    remote clears the key rather than deleting it. Empty must therefore mean
    remote — treating it as local would point the sweep at ``Path("")``, which
    is the current working directory, and mint every subdirectory as a run.

    Exported because doctor needs the same rule to know which keys are even
    applicable (demanding ``remote_host`` in local mode would be a false
    failure). One home, no transcription — the plan 14 precedent for the
    eligibility and safe-path predicates.
    """
    return bool(config.get("local_source_dir"))
```

Then replace the inline decision inside `build_listing_command`:

```python
    local_source_dir = config.get("local_source_dir")
    if is_local_mode(config):
```

Keep the `local_source_dir` binding — the local branch still uses the value to build the path.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/test_detection.py -q`
Expected: PASS — including the pre-existing `test_local_mode_command_lists_directory_contents` and `test_remote_command_is_pinned_and_password_never_on_argv`, which must be untouched.

- [ ] **Step 5: Commit**

```bash
git add sluice/gates/detection.py tests/test_detection.py
git commit -m "detection: export is_local_mode, the third predicate twin

doctor needs the same rule to know which config keys apply — demanding
remote_host in local mode would be a false failure. Rather than transcribe
it, the rule gets one home and build_listing_command calls it, following
plan 14's treatment of the eligibility and safe-path predicates.

A monkeypatched predicate must change the command that comes out, so a
second copy growing back inside build_listing_command fails the suite."
```

---

### Task 3: The doctor core — severity, records, and the runner

**Files:**
- Create: `sluice/doctor.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `ConfigField`, `FieldKind`, `ProbeKind` from Task 1.
- Produces:
  - `Severity` enum: `FAIL`, `WARN`, `OK`, `SKIP`
  - `CheckResult` frozen dataclass: `(check_id: str, area: str, severity: Severity, detail: str)`
  - `Check` frozen dataclass: `(check_id: str, area: str, run: Callable[[Mapping], CheckResult])`
  - `severity_for(field: ConfigField) -> Severity`
  - `run_checks(checks, config) -> tuple[CheckResult, ...]`
  - `worst(results) -> Severity`

- [ ] **Step 1: Write the failing test**

Create `tests/test_doctor.py`:

```python
"""Behaviour of ``sluice doctor`` — increment A, local checks only."""

import pytest

from sluice.config.schema import ConfigField, FieldKind, ProbeKind
from sluice.doctor import (
    Check,
    CheckResult,
    Severity,
    run_checks,
    severity_for,
    worst,
)


def _field(name, kind, probe=ProbeKind.PATH_EXISTS, required=True):
    """A throwaway ConfigField, so tests do not depend on the real schema."""
    return ConfigField(name, kind, "test", "help text", probe=probe, required=required)


@pytest.mark.parametrize(
    "kind,expected",
    [
        (FieldKind.MANAGED_PATH, Severity.WARN),
        (FieldKind.EXTERNAL_PATH, Severity.FAIL),
        (FieldKind.SECRET_FILE, Severity.FAIL),
        (FieldKind.VALUE, Severity.FAIL),
    ],
)
def test_severity_is_derived_from_who_can_supply_the_value(kind, expected):
    """Severity is derived, never hand-assigned (design spec D6).

    A MANAGED_PATH is sluice's own working directory — setup creates it and can
    recreate it, so its absence is a WARN. Everything else can only come from a
    human, so its absence stops the pipeline and is a FAIL.
    """
    assert severity_for(_field("k", kind)) is expected


def test_run_checks_returns_one_result_per_check_in_order():
    """The runner is a plain map — no filtering, no reordering, no short-circuit.

    Order is the report's reading order, and a later FAIL must not suppress an
    earlier one: an operator fixing things wants the whole list, not the first
    problem.
    """
    checks = (
        Check("a", "universal", lambda config: CheckResult("a", "universal", Severity.OK, "fine")),
        Check("b", "universal", lambda config: CheckResult("b", "universal", Severity.FAIL, "broken")),
        Check("c", "universal", lambda config: CheckResult("c", "universal", Severity.OK, "fine")),
    )
    results = run_checks(checks, {})
    assert [result.check_id for result in results] == ["a", "b", "c"]
    assert results[1].severity is Severity.FAIL


def test_a_raising_check_becomes_a_failure_not_a_crash():
    """Doctor must survive its own bugs.

    A diagnostic that dies while diagnosing is worse than useless — it is run
    precisely when things are already broken, and an unhandled exception would
    hide every check after it. The traceback type and message go in the detail
    so the bug is still reportable.
    """
    def explode(config):
        raise OSError("disk went away")

    results = run_checks((Check("boom", "universal", explode),), {})
    assert results[0].severity is Severity.FAIL
    assert "OSError" in results[0].detail
    assert "disk went away" in results[0].detail


def test_worst_ranks_fail_above_warn_above_ok():
    """The exit code depends on this ordering, so it is pinned."""
    ok = CheckResult("a", "x", Severity.OK, "")
    warn = CheckResult("b", "x", Severity.WARN, "")
    fail = CheckResult("c", "x", Severity.FAIL, "")
    assert worst((ok, warn)) is Severity.WARN
    assert worst((ok, warn, fail)) is Severity.FAIL
    assert worst((ok,)) is Severity.OK
    assert worst(()) is Severity.OK
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pixi run pytest tests/test_doctor.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sluice.doctor'`

- [ ] **Step 3: Write the module**

Create `sluice/doctor.py`:

```python
"""``sluice doctor`` — proving a configured installation can actually work.

``sluice setup`` proves a config is **complete**; nothing proved it **works**
(errata E2). Doctor closes that: it turns each config key's declared
``ProbeKind`` into a check, runs the ones the active profile needs, and reports.

**It never mutates.** No directory is created, no config written, no git ref
checked out, no run state touched — ADR-0005's posture applied to diagnosis.
Doctor tells you; you decide.

Lives at the package root rather than under ``core`` because it must know about
production gates and profiles, and ``sluice.core`` may not import
``sluice.gates`` (ADR-0003). Nothing in ``core`` imports this module; the daemon
startup gate (increment C) runs from the CLI, which is already the composition
root.
"""

import dataclasses
import enum
from collections.abc import Callable, Iterable, Mapping

from sluice.config.schema import ConfigField, FieldKind

__all__ = [
    "Check",
    "CheckResult",
    "Severity",
    "run_checks",
    "severity_for",
    "worst",
]


class Severity(enum.Enum):
    """How bad a check's outcome is.

    Four members, ranked. ``SKIP`` is not a lesser failure — it means the check
    did not apply (an optional key that is unset, a gate not in this profile),
    and it must never influence the exit code.
    """

    OK = "ok"
    SKIP = "skip"
    WARN = "warn"
    FAIL = "fail"


# Ranking for `worst`. SKIP sits with OK deliberately: a check that did not
# apply is not a degradation, and ranking it above OK would make an install
# with an unused optional key look worse than one without it.
_RANK = {Severity.OK: 0, Severity.SKIP: 0, Severity.WARN: 1, Severity.FAIL: 2}


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """One check's outcome — what ran, where it belongs, and what it found."""

    check_id: str
    area: str
    severity: Severity
    detail: str


@dataclasses.dataclass(frozen=True)
class Check:
    """One check: an id, the area it reports under, and the callable that runs it.

    ``area`` is a gate id (``"nvd"``), or a pseudo-area (``"universal"``,
    ``"notifier"``). It is what groups the report and what the drift guard
    matches against, so it is a plain string rather than an enum — gate ids are
    already strings and a second vocabulary would need keeping in step.

    ``run`` takes the whole config and returns a ``CheckResult``. It must not
    raise; if it does, ``run_checks`` converts the escape into a FAIL rather
    than letting a diagnostic die while diagnosing.
    """

    check_id: str
    area: str
    run: Callable[[Mapping], CheckResult]


def severity_for(field: ConfigField) -> Severity:
    """How bad it is for ``field``'s subject to be missing or wrong.

    **Derived, never hand-assigned** (design spec D6). A hand-maintained
    severity column is one more thing to keep in step with the schema, and it
    would drift the moment a key changed kind. The rule instead falls out of who
    can supply the value: a ``MANAGED_PATH`` is sluice's own working directory
    that setup creates and can recreate, so its absence is a WARN; every other
    kind can only come from a human, so its absence stops the pipeline.
    """
    if field.kind is FieldKind.MANAGED_PATH:
        return Severity.WARN
    return Severity.FAIL


def run_checks(checks: Iterable[Check], config: Mapping) -> tuple[CheckResult, ...]:
    """Run every check in order and collect the results.

    No filtering and no short-circuit: an operator fixing an installation wants
    the whole list, and stopping at the first FAIL would hide the other four
    things also broken. Selection happened before this — see ``select_checks``.

    A check that raises becomes a FAIL naming the exception. Doctor runs exactly
    when things are already broken, so it must survive its own bugs; an
    unhandled escape would suppress every check after it.
    """
    results = []
    for check in checks:
        try:
            results.append(check.run(config))
        except Exception as check_error:  # noqa: BLE001 — an escape becomes FAIL
            results.append(
                CheckResult(
                    check.check_id,
                    check.area,
                    Severity.FAIL,
                    f"the check itself failed: "
                    f"{type(check_error).__name__}: {check_error}",
                )
            )
    return tuple(results)


def worst(results: Iterable[CheckResult]) -> Severity:
    """The highest severity present, or OK when there is nothing to report.

    The exit code depends on this: FAIL exits 1, everything else exits 0, so a
    degraded-but-working install stays scriptable.
    """
    return max(
        (result.severity for result in results),
        key=lambda severity: _RANK[severity],
        default=Severity.OK,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/test_doctor.py -q`
Expected: PASS — 8 tests (4 parametrised + 4).

- [ ] **Step 5: Verify the dependency arrow still holds**

Run: `pixi run pytest tests/test_dependency_arrows.py -q`
Expected: PASS. If it fails, something under `sluice/core/` imported `sluice.doctor` — reverse it; the dependency runs the other way.

- [ ] **Step 6: Commit**

```bash
git add sluice/doctor.py tests/test_doctor.py
git commit -m "doctor: the check record, derived severity, and the runner

Severity is derived from FieldKind rather than hand-assigned (design spec
D6): a MANAGED_PATH is sluice's own directory that setup can recreate, so
its absence is a WARN; anything only a human can supply is a FAIL. A
hand-maintained severity column would be one more thing to keep in step.

run_checks never short-circuits — an operator wants the whole list, not the
first problem — and converts a raising check into a FAIL, because a
diagnostic runs exactly when things are already broken and must survive its
own bugs."
```

---

### Task 4: The local probes

**Files:**
- Modify: `sluice/doctor.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `Severity`, `CheckResult`, `severity_for` from Task 3.
- Produces: `check_for_field(field: ConfigField) -> Check` — builds the local-tier check for one config key from its `ProbeKind`.

**Placeholder convention:** `sluice setup` writes unset operator-supplied keys as `<key_name>`, visible in `config.example.json` (`"<remote_host>"`). A value that starts `<` and ends `>` is therefore *unset*, not a real value, and must be reported as such rather than as "host unreachable".

- [ ] **Step 1: Write the failing test**

Add to `tests/test_doctor.py`:

```python
from sluice.doctor import check_for_field


def test_a_missing_external_path_fails_and_names_the_key_and_the_path(tmp_path):
    """The message must be actionable without reading the code.

    Names the config key (so the operator knows what to edit) and the path it
    resolved to (so they can see the typo). One of those alone is not enough.
    """
    field = _field("nvd_repo_dir", FieldKind.EXTERNAL_PATH)
    check = check_for_field(field)
    result = check.run({"nvd_repo_dir": str(tmp_path / "absent")})
    assert result.severity is Severity.FAIL
    assert "nvd_repo_dir" in result.detail
    assert "absent" in result.detail


def test_a_present_path_passes(tmp_path):
    field = _field("nvd_repo_dir", FieldKind.EXTERNAL_PATH)
    result = check_for_field(field).run({"nvd_repo_dir": str(tmp_path)})
    assert result.severity is Severity.OK


def test_a_missing_managed_path_warns_rather_than_fails(tmp_path):
    """Setup creates these, so their absence is recoverable without a human."""
    field = _field("nvd_log_dir", FieldKind.MANAGED_PATH)
    result = check_for_field(field).run({"nvd_log_dir": str(tmp_path / "absent")})
    assert result.severity is Severity.WARN


def test_an_unset_placeholder_is_reported_as_unset_not_as_broken():
    """``<remote_host>`` means setup wrote it and nobody filled it in.

    Reporting that as "host unreachable" would send an operator to the network
    when the answer is one line of config.
    """
    field = _field("remote_host", FieldKind.VALUE, probe=ProbeKind.HOST_REACHABLE)
    result = check_for_field(field).run({"remote_host": "<remote_host>"})
    assert result.severity is Severity.FAIL
    assert "not been filled in" in result.detail


def test_an_absent_optional_key_is_skipped_not_failed():
    """An unset optional key is a configuration choice, not a fault.

    ``watch_dir`` and ``local_source_dir`` are both optional — an installation
    uses one source mode or the other, so the unused one being empty is correct
    and must not colour the exit code.
    """
    field = _field("watch_dir", FieldKind.EXTERNAL_PATH, required=False)
    assert check_for_field(field).run({}).severity is Severity.SKIP
    assert check_for_field(field).run({"watch_dir": ""}).severity is Severity.SKIP


def test_an_absent_required_key_fails():
    field = _field("nvd_repo_dir", FieldKind.EXTERNAL_PATH, required=True)
    result = check_for_field(field).run({})
    assert result.severity is Severity.FAIL
    assert "nvd_repo_dir" in result.detail


def test_a_secret_file_must_exist_and_be_non_empty(tmp_path):
    """An empty credential file is the failure mode setup itself creates.

    ``sluice setup`` writes empty files under ``~/.sluice/secrets/`` for the
    operator to fill. An install where that never happened looks perfectly
    configured — the path exists — and fails at the first LabKey call.
    """
    secret = tmp_path / "key"
    secret.write_text("")
    field = _field("labkey_api_key_file", FieldKind.SECRET_FILE, probe=ProbeKind.SECRET_READABLE)
    result = check_for_field(field).run({"labkey_api_key_file": str(secret)})
    assert result.severity is Severity.FAIL
    assert "empty" in result.detail

    secret.write_text("a-real-key")
    secret.chmod(0o600)
    assert check_for_field(field).run({"labkey_api_key_file": str(secret)}).severity is Severity.OK


def test_a_world_readable_secret_warns_but_does_not_fail(tmp_path):
    """It works; it should not. A WARN says so without blocking the pipeline."""
    secret = tmp_path / "key"
    secret.write_text("a-real-key")
    secret.chmod(0o644)
    field = _field("password_file", FieldKind.SECRET_FILE, probe=ProbeKind.SECRET_READABLE)
    result = check_for_field(field).run({"password_file": str(secret)})
    assert result.severity is Severity.WARN
    assert "readable by other users" in result.detail


def test_a_secret_check_never_puts_the_credential_in_its_message(tmp_path):
    """The one rule that must never break, asserted rather than trusted."""
    secret = tmp_path / "key"
    secret.write_text("SUPER-SECRET-VALUE")
    secret.chmod(0o644)
    field = _field("password_file", FieldKind.SECRET_FILE, probe=ProbeKind.SECRET_READABLE)
    result = check_for_field(field).run({"password_file": str(secret)})
    assert "SUPER-SECRET-VALUE" not in result.detail


def test_an_executable_must_resolve_on_path():
    field = _field("nvd_command", FieldKind.VALUE, probe=ProbeKind.EXECUTABLE)
    assert check_for_field(field).run({"nvd_command": "sh"}).severity is Severity.OK
    result = check_for_field(field).run({"nvd_command": "definitely-not-a-real-command"})
    assert result.severity is Severity.FAIL
    assert "not on PATH" in result.detail


def test_a_tuning_value_is_skipped_entirely():
    """ProbeKind.NONE means the schema guard already covers it."""
    field = _field("nvd_entropy", FieldKind.VALUE, probe=ProbeKind.NONE)
    assert check_for_field(field).run({"nvd_entropy": 0.9}).severity is Severity.SKIP
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/test_doctor.py -q`
Expected: FAIL — `ImportError: cannot import name 'check_for_field'`

- [ ] **Step 3: Implement**

Add to `sluice/doctor.py` — extend the imports and `__all__` first:

```python
import pathlib
import shutil
import stat

from sluice.config.schema import ConfigField, FieldKind, ProbeKind
```

```python
__all__ = [
    "Check",
    "CheckResult",
    "Severity",
    "check_for_field",
    "run_checks",
    "severity_for",
    "worst",
]
```

Then:

```python
def _is_unfilled_placeholder(value) -> bool:
    """Whether ``value`` is a ``<key_name>`` marker setup wrote and nobody filled.

    ``sluice setup`` writes operator-supplied keys as ``<key_name>`` (visible in
    ``config.example.json``). Treating that as a real value would send an
    operator to debug the network when the answer is one line of config, so it
    is detected and reported as unset.
    """
    return isinstance(value, str) and value.startswith("<") and value.endswith(">")


def check_for_field(field: ConfigField) -> Check:
    """Build the local-tier check for one config key, from its ``ProbeKind``.

    Increment A is local-only: ``HOST_REACHABLE`` and ``ENDPOINT`` verify that
    the value is *set*, not that the far end answers — the network tier is
    increment B. That is a deliberate floor, not a stub: an unset host is by far
    the more common failure, and catching it costs no round-trip.

    The returned check never raises for an ordinary missing thing; it returns a
    result. ``run_checks`` catches genuine escapes.
    """

    def run(config: Mapping) -> CheckResult:
        area = field.group
        if field.probe is ProbeKind.NONE:
            # Presence is already guaranteed by the schema guard; there is
            # nothing else to say about a tuning value.
            return CheckResult(field.name, area, Severity.SKIP, "no check applies")

        value = config.get(field.name)

        if value is None or value == "":
            if not field.required:
                # An unused optional key is a configuration choice — an install
                # uses one source mode or the other — and must not colour the
                # exit code.
                return CheckResult(field.name, area, Severity.SKIP, "not configured")
            return CheckResult(
                field.name,
                area,
                severity_for(field),
                f"{field.name} is required but not set",
            )

        if _is_unfilled_placeholder(value):
            return CheckResult(
                field.name,
                area,
                severity_for(field),
                f"{field.name} has not been filled in — it is still the "
                f"placeholder setup wrote",
            )

        if field.probe is ProbeKind.PATH_EXISTS:
            path = pathlib.Path(str(value)).expanduser()
            if not path.exists():
                return CheckResult(
                    field.name,
                    area,
                    severity_for(field),
                    f"{field.name}: nothing at {path}",
                )
            return CheckResult(field.name, area, Severity.OK, str(path))

        if field.probe is ProbeKind.SECRET_READABLE:
            path = pathlib.Path(str(value)).expanduser()
            if not path.is_file():
                return CheckResult(
                    field.name,
                    area,
                    severity_for(field),
                    f"{field.name}: no credential file at {path}",
                )
            try:
                size = path.stat().st_size
            except OSError as stat_error:
                return CheckResult(
                    field.name,
                    area,
                    severity_for(field),
                    f"{field.name}: cannot read {path}: {type(stat_error).__name__}",
                )
            if size == 0:
                # setup creates these empty for the operator to fill, so an
                # install where that never happened looks configured and fails
                # at the first real call.
                return CheckResult(
                    field.name,
                    area,
                    severity_for(field),
                    f"{field.name}: {path} is empty — setup created it for you "
                    f"to fill in",
                )
            mode = path.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                # Never a FAIL: it works. But a credential readable by everyone
                # on a shared machine is worth saying out loud.
                return CheckResult(
                    field.name,
                    area,
                    Severity.WARN,
                    f"{field.name}: {path} is readable by other users "
                    f"(mode {oct(stat.S_IMODE(mode))})",
                )
            return CheckResult(field.name, area, Severity.OK, str(path))

        if field.probe is ProbeKind.EXECUTABLE:
            if shutil.which(str(value)) is None:
                return CheckResult(
                    field.name,
                    area,
                    severity_for(field),
                    f"{field.name}: {value!r} is not on PATH",
                )
            return CheckResult(
                field.name, area, Severity.OK, str(shutil.which(str(value)))
            )

        # HOST_REACHABLE and ENDPOINT: set and not a placeholder is all the
        # local tier can establish. Increment B contacts them.
        return CheckResult(field.name, area, Severity.OK, f"set ({value})")

    return Check(field.name, field.group, run)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/test_doctor.py -q`
Expected: PASS.

- [ ] **Step 5: Run lint**

Run: `pixi run lint`
Expected: clean. Remove any import ruff reports as unused.

- [ ] **Step 6: Commit**

```bash
git add sluice/doctor.py tests/test_doctor.py
git commit -m "doctor: the local probes

One check per config key, built from its ProbeKind. An unfilled <placeholder>
is reported as unset rather than as broken, so an operator is not sent to
debug the network when the answer is one line of config. An empty secret file
is a FAIL — setup creates those empty for the operator to fill, so an install
where that never happened looks perfectly configured and dies at the first
real call. A world-readable credential is a WARN, never a FAIL: it works, it
just should not.

A test asserts a credential's contents never reach a check message."
```

---

### Task 5: Profile-aware assembly and the universal checks

**Files:**
- Modify: `sluice/doctor.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `check_for_field` (Task 4), `is_local_mode` (Task 2).
- Produces:
  - `GATE_CONFIG_GROUPS: Mapping[str, tuple[str, ...]]` — gate id → the schema `group` names its checks come from.
  - `select_checks(config, registry, pipeline_version) -> tuple[Check, ...]`
  - `run_doctor(config, registry, pipeline_version) -> tuple[CheckResult, ...]`

**Gate → config group mapping** (the schema's `group` values, from Task 1's dump):

| Gate id | Groups |
|---|---|
| `experiment` | `labkey` |
| `download` | `detection`, `storage`, `download` |
| `checksum` | *(none — reads only files download produced)* |
| `samplesheet` | `nvd` |
| `nvd` | `nvd`, `nvd reference data`, `nvd analysis` |

- [ ] **Step 1: Write the failing test**

Add to `tests/test_doctor.py`:

```python
from sluice.core.daemon import assemble_registry
from sluice.core.profile import REFERENCE_PROFILES
from sluice.doctor import run_doctor, select_checks
from sluice.gates.profiles import PRODUCTION_PROFILES, PRODUCTION_VERSION


@pytest.fixture
def registry():
    return assemble_registry(REFERENCE_PROFILES, PRODUCTION_PROFILES)


def test_selection_covers_the_gates_in_the_active_profile(registry):
    """Every gate in production-v3.1 contributes its config keys."""
    checks = select_checks({}, registry, PRODUCTION_VERSION)
    areas = {check.area for check in checks}
    assert "labkey" in areas       # the experiment gate
    assert "nvd" in areas          # the nvd gate
    assert "storage" in areas      # the download gate


def test_selection_never_asks_about_gates_that_are_not_built(registry):
    """Globus and registration are deferred, so their absence is not a problem.

    A doctor that complained about every unbuilt gate would cry wolf on a
    correctly configured install, and an operator who learns to ignore doctor's
    output has lost the whole benefit.
    """
    checks = select_checks({}, registry, PRODUCTION_VERSION)
    ids = {check.check_id for check in checks}
    assert not any("globus" in check_id for check_id in ids)


def test_the_notifier_is_only_checked_when_slack_is_enabled(registry):
    """Slack off is a valid configuration, not a fault."""
    off = {check.area for check in select_checks({"slack_enabled": False}, registry, PRODUCTION_VERSION)}
    on = {check.area for check in select_checks({"slack_enabled": True}, registry, PRODUCTION_VERSION)}
    assert "slack" not in off
    assert "slack" in on


def test_remote_keys_are_not_demanded_in_local_mode(registry, tmp_path):
    """Local mode is a supported source mode, not a half-configured remote one."""
    config = {"local_source_dir": str(tmp_path)}
    results = run_doctor(config, registry, PRODUCTION_VERSION)
    remote = [r for r in results if r.check_id == "remote_host"]
    assert remote and remote[0].severity is Severity.SKIP


def test_local_keys_are_not_demanded_in_remote_mode(registry):
    config = {"local_source_dir": "", "remote_host": "sequencer.example"}
    results = run_doctor(config, registry, PRODUCTION_VERSION)
    local = [r for r in results if r.check_id == "local_source_dir"]
    assert local and local[0].severity is Severity.SKIP


def test_an_unknown_pipeline_version_is_a_single_clear_failure(registry):
    """The blocker that would quarantine every run, caught before it does.

    A stamped version with no registry entry raises UnknownProfileError at the
    daemon's first look, continuously, for every run. That was a real near-miss
    in plan 15 increment 1, and it is exactly what doctor exists to catch.
    """
    results = run_doctor({}, registry, "no-such-profile-v9")
    profile_results = [r for r in results if r.area == "profile"]
    assert profile_results
    assert profile_results[0].severity is Severity.FAIL
    assert "no-such-profile-v9" in profile_results[0].detail


def test_a_database_behind_its_migrations_fails(tmp_path, registry):
    """A DB the code is newer than will fail on a column that is not there yet.

    The fix is `sluice setup`, and doctor says so rather than leaving the
    operator to read a sqlite3.OperationalError.
    """
    from sluice.core.db import get_connection

    state_file = tmp_path / "state.db"
    connection = get_connection(str(state_file))
    connection.close()

    results = run_doctor({"state_file": str(state_file)}, registry, PRODUCTION_VERSION)
    ladder = [r for r in results if r.check_id == "schema_ladder"]
    assert ladder and ladder[0].severity is Severity.FAIL
    assert "sluice setup" in ladder[0].detail


def test_checking_an_absent_database_does_not_create_one(tmp_path, registry):
    """Doctor must not leave a zero-byte database behind — it never mutates.

    ``sqlite3`` creates the file when asked to open a path that does not exist,
    so a naive ``get_connection`` here would stand up an empty database next to
    a typo'd path and then report it as healthy on the next run. The existence
    check must come first.
    """
    absent = tmp_path / "not-there.db"
    results = run_doctor({"state_file": str(absent)}, registry, PRODUCTION_VERSION)

    assert not absent.exists(), "doctor created a database it was only meant to read"
    ladder = [r for r in results if r.check_id == "schema_ladder"]
    assert ladder and ladder[0].severity is Severity.FAIL
    assert "sluice setup" in ladder[0].detail
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/test_doctor.py -q`
Expected: FAIL — `ImportError: cannot import name 'select_checks'`

- [ ] **Step 3: Implement**

Add to `sluice/doctor.py`:

```python
from sluice.config.schema import CONFIG_FIELDS
from sluice.core.db import MIGRATIONS, get_connection, schema_version
from sluice.gates.detection import is_local_mode
```

Extend `__all__` with `"GATE_CONFIG_GROUPS"`, `"run_doctor"`, `"select_checks"`.

```python
# Which schema `group`s each gate's configuration lives in. Keyed by gate id so
# it reads against the profile, and matched by the drift guard: a gate whose
# module reads a probe-bearing key from a group not listed here fails the suite.
GATE_CONFIG_GROUPS: Mapping[str, tuple[str, ...]] = {
    "experiment": ("labkey",),
    "download": ("detection", "storage", "download"),
    # checksum reads only the files download produced — no configuration of its
    # own, which `produces=()` on the gate already says.
    "checksum": (),
    "samplesheet": ("nvd",),
    "nvd": ("nvd", "nvd reference data", "nvd analysis"),
}

# Config groups that are not a gate's. `sluice state` is universal; `slack`
# belongs to the notifier, which is not a gate at all.
_UNIVERSAL_GROUPS = ("sluice state",)
_NOTIFIER_GROUPS = ("slack",)


def _source_mode_skip(config: Mapping, field: ConfigField) -> bool:
    """Whether ``field`` belongs to the source mode this install is NOT using.

    Detection reads a local directory or a remote host, never both
    (``is_local_mode``). Demanding the unused mode's keys would fail every
    correctly configured install, and one that cried wolf would be ignored.
    """
    local_keys = {"local_source_dir", "watch_dir"}
    remote_keys = {"remote_host", "remote_user", "remote_source_path", "password_file"}
    if is_local_mode(config):
        return field.name in remote_keys
    return field.name in local_keys


def _profile_check(registry: Mapping, pipeline_version: str) -> Check:
    """Assert the version runs are stamped with actually resolves.

    A stamped version absent from the registry raises ``UnknownProfileError`` at
    the daemon's first look — for every run, continuously, before any gate runs.
    """

    def run(config: Mapping) -> CheckResult:
        if pipeline_version not in registry:
            known = ", ".join(sorted(registry)) or "none"
            return CheckResult(
                "profile_resolves",
                "profile",
                Severity.FAIL,
                f"pipeline version {pipeline_version!r} is not in the registry "
                f"(known: {known}) — every run would quarantine at the daemon's "
                f"first look",
            )
        return CheckResult(
            "profile_resolves", "profile", Severity.OK, pipeline_version
        )

    return Check("profile_resolves", "profile", run)


def _schema_ladder_check() -> Check:
    """Assert the database has every migration this build expects.

    A database behind the code fails on a column that does not exist yet, as a
    ``sqlite3.OperationalError`` at gate time. The fix is ``sluice setup``, so
    doctor names it.
    """

    def run(config: Mapping) -> CheckResult:
        state_file = config.get("state_file")
        if not state_file:
            return CheckResult(
                "schema_ladder",
                "universal",
                Severity.FAIL,
                "state_file is not set, so the database cannot be checked",
            )
        path = pathlib.Path(str(state_file)).expanduser()
        # MUST come before get_connection: sqlite3 CREATES the file when it is
        # asked to open a path that does not exist, so connecting here would
        # leave a zero-byte database behind — doctor mutating the world it is
        # supposed to only observe, and worse, next to a typo'd path.
        if not path.exists():
            return CheckResult(
                "schema_ladder",
                "universal",
                Severity.FAIL,
                f"no database at {path} — run `sluice setup` to create it",
            )
        expected = max(version for version, _name, _apply in MIGRATIONS)
        connection = get_connection(str(path))
        try:
            current = schema_version(connection)
        finally:
            connection.close()
        if current < expected:
            return CheckResult(
                "schema_ladder",
                "universal",
                Severity.FAIL,
                f"database is at migration {current}, this build expects "
                f"{expected} — run `sluice setup` to bring it up to date",
            )
        return CheckResult(
            "schema_ladder", "universal", Severity.OK, f"migration {current}"
        )

    return Check("schema_ladder", "universal", run)


def select_checks(
    config: Mapping, registry: Mapping, pipeline_version: str
) -> tuple[Check, ...]:
    """Every check this installation should run, in reading order.

    Selection, not filtering-at-report-time: a check for a gate that is not in
    the contract is never built, so it cannot appear in the output at all.
    Deferred and DISABLED gates therefore contribute nothing, and doctor stays
    quiet about Globus and ``registration`` rather than crying wolf on a
    correctly configured install.
    """
    profile = registry.get(pipeline_version)
    gate_ids = (
        tuple(gate.id for gate in profile.gates) if profile is not None else ()
    )

    wanted_groups = set(_UNIVERSAL_GROUPS)
    for gate_id in gate_ids:
        wanted_groups.update(GATE_CONFIG_GROUPS.get(gate_id, ()))
    if config.get("slack_enabled"):
        wanted_groups.update(_NOTIFIER_GROUPS)

    checks = [_profile_check(registry, pipeline_version), _schema_ladder_check()]
    for field in CONFIG_FIELDS:
        if field.group not in wanted_groups:
            continue
        if _source_mode_skip(config, field):
            checks.append(
                Check(
                    field.name,
                    field.group,
                    lambda config, field=field: CheckResult(
                        field.name,
                        field.group,
                        Severity.SKIP,
                        "not used in this source mode",
                    ),
                )
            )
            continue
        checks.append(check_for_field(field))
    return tuple(checks)


def run_doctor(
    config: Mapping, registry: Mapping, pipeline_version: str
) -> tuple[CheckResult, ...]:
    """Select and run every applicable check. The whole diagnostic, in one call."""
    return run_checks(select_checks(config, registry, pipeline_version), config)
```

`profile.gates` is correct — `Profile` is a frozen record of `pipeline_version`, `gates: tuple[Gate, ...]` (already in execution order) and `applicability` (`sluice/core/profile.py:49-62`). Nothing else needs adapting.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/test_doctor.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite and lint**

Run: `pixi run test && pixi run lint`
Expected: green, including `tests/test_dependency_arrows.py`.

- [ ] **Step 6: Commit**

```bash
git add sluice/doctor.py tests/test_doctor.py
git commit -m "doctor: profile-aware selection and the universal checks

Checks for gates outside the active contract are never built, so deferred
and DISABLED gates contribute nothing — doctor stays quiet about Globus and
registration rather than crying wolf on a correctly configured install. An
operator who learns to ignore doctor has lost the whole benefit.

Two universal checks earn their place from real near-misses: an unresolvable
pipeline_version (which would quarantine every run at the daemon's first
look, the plan 15 increment 1 blocker) and a database behind its migration
ladder, which otherwise surfaces as a sqlite3.OperationalError at gate time."
```

---

### Task 6: The drift guard

**Files:**
- Create: `tests/test_doctor_drift.py`

**Interfaces:**
- Consumes: `GATE_CONFIG_GROUPS` (Task 5), `CONFIG_FIELDS` + `ProbeKind` (Task 1), `_config_reads_in` from `tests/test_config_schema.py`.
- Produces: nothing — it is a guard.

**Why a separate file:** `test_dependency_arrows.py` and `test_predicate_twins.py` set the precedent. A failure here means the architecture slipped, not that a behaviour broke, and the distinct filename is what tells the next person that.

- [ ] **Step 1: Write the guard**

Create `tests/test_doctor_drift.py`:

```python
"""THE anti-drift guard for ``sluice doctor``.

A gate that starts reading a new path, secret, host or executable must not be
able to do so without doctor learning to check it. This is the mechanism that
was chosen over a twelfth ``Gate`` field (design spec D2): the coupling is a
test rather than the type, so the test has to be real.

Fails loudly and specifically, naming the gate, the key and the group, because
a guard whose message does not say what to do gets suppressed.
"""

import ast
import pathlib

from test_config_schema import _config_reads_in

from sluice.config.schema import CONFIG_FIELDS, ProbeKind
from sluice.doctor import GATE_CONFIG_GROUPS
from sluice.gates.profiles import PRODUCTION_PROFILE

# Gate id -> the module implementing it. Adapters shared by several gates are
# attributed to the gate that owns the configuration they read.
_GATE_MODULES = {
    "experiment": ("sluice/gates/experiment.py", "sluice/gates/labkey.py"),
    "download": ("sluice/gates/download.py", "sluice/gates/transfer.py",
                 "sluice/gates/detection.py"),
    "checksum": ("sluice/gates/checksum.py",),
    "samplesheet": ("sluice/gates/samplesheet.py", "sluice/gates/nvd_tool.py"),
    "nvd": ("sluice/gates/nvd.py",),
}


def _probe_by_key():
    return {field.name: field.probe for field in CONFIG_FIELDS}


def _group_by_key():
    return {field.name: field.group for field in CONFIG_FIELDS}


def test_every_probe_bearing_key_a_gate_reads_is_covered_by_doctor():
    """No gate reads a checkable config key that doctor does not check.

    Scoped to the gates in the shipped production profile: a gate that is not
    in the contract cannot break an installation, and demanding coverage for
    unbuilt gates would block the very increments that add them.
    """
    probe = _probe_by_key()
    group = _group_by_key()
    problems = []

    for gate in PRODUCTION_PROFILE.gates:
        covered_groups = set(GATE_CONFIG_GROUPS.get(gate.id, ()))
        for module_path in _GATE_MODULES.get(gate.id, ()):
            source = pathlib.Path(module_path).read_text(encoding="utf-8")
            for key in sorted(_config_reads_in(ast.parse(source))):
                if probe.get(key, ProbeKind.NONE) is ProbeKind.NONE:
                    continue
                if group.get(key) not in covered_groups:
                    problems.append(
                        f"gate {gate.id!r} reads {key!r} (group "
                        f"{group.get(key)!r}, probe {probe[key].value}) but "
                        f"doctor's GATE_CONFIG_GROUPS[{gate.id!r}] does not "
                        f"include that group"
                    )

    assert not problems, (
        "doctor has drifted from the gates — add the group to "
        "GATE_CONFIG_GROUPS in sluice/doctor.py:\n  " + "\n  ".join(problems)
    )


def test_the_guard_names_a_group_that_actually_exists():
    """A typo in GATE_CONFIG_GROUPS would silently cover nothing.

    Without this, `"nvd reference"` instead of `"nvd reference data"` would pass
    the guard above by covering no keys at all — the exact failure the guard
    exists to prevent, hidden inside the guard itself.
    """
    real_groups = {field.group for field in CONFIG_FIELDS}
    for gate_id, groups in GATE_CONFIG_GROUPS.items():
        for group in groups:
            assert group in real_groups, (
                f"GATE_CONFIG_GROUPS[{gate_id!r}] names {group!r}, which is not "
                f"a group any config field declares"
            )
```

- [ ] **Step 2: Run it and confirm it passes on the current tree**

Run: `pixi run pytest tests/test_doctor_drift.py -q`
Expected: PASS. If it fails, `GATE_CONFIG_GROUPS` in Task 5 is missing a group — add it, do not weaken the guard.

- [ ] **Step 3: Prove the guard actually fails — the mutation witness**

The project standard: a green-on-arrival guard must be shown to go red. Temporarily remove `"storage"` from `GATE_CONFIG_GROUPS["download"]` in `sluice/doctor.py`:

Run: `pixi run pytest tests/test_doctor_drift.py -q`
Expected: **FAIL**, with a message naming `download`, `disk_quota_root` and `storage`.

Then temporarily change `"nvd reference data"` to `"nvd reference"`:

Run: `pixi run pytest tests/test_doctor_drift.py -q`
Expected: **FAIL** from the second test, naming the bogus group.

Restore both edits byte-identically and re-run:

Run: `pixi run pytest tests/test_doctor_drift.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_doctor_drift.py
git commit -m "doctor: the drift guard

The coupling chosen over a twelfth Gate field (design spec D2): a gate that
starts reading a new path, secret, host or executable cannot do so without
doctor learning to check it. Since the coupling is a test rather than the
type, the test has to be real — verified failing in both directions (a
missing group, and a group name typo that would otherwise cover nothing and
pass silently) and restored byte-identically.

Scoped to gates in the shipped production profile: demanding coverage for
unbuilt gates would block the increments that add them."
```

---

### Task 7: The CLI command and the operator docs

**Files:**
- Modify: `sluice/cli/__init__.py`
- Modify: `docs/OPERATING.md`
- Test: `tests/test_cli.py` (add to the existing file)

**Interfaces:**
- Consumes: `run_doctor`, `Severity`, `worst` from Tasks 3–5; `_registry()`, `_settings()`, `_guard()`, `_emit_json()` already in the CLI.
- Produces: the `sluice doctor` command.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`. It uses `main([...])` returning an exit code plus `capsys`; the existing `deployment` fixture (`tests/test_cli.py:19`) writes a config with only `state_file` and `local_staging_dir`, which is exactly an under-configured install.

```python
# ---------------------------------------------------------------------------
# doctor.
# ---------------------------------------------------------------------------


def _write_config(path, **values):
    """A config file with exactly the keys a test cares about."""
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_doctor_exits_one_when_something_fails(deployment, capsys):
    """Exit 1 on any FAIL, so a wrapper script cannot mistake it for success.

    The deployment fixture configures only state_file and local_staging_dir, so
    every operator-supplied key is missing — the shape of a half-finished
    install, which is what doctor exists to catch.
    """
    exit_code = main(["--config", str(deployment.config_path), "doctor"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL" in output
    assert "labkey_api_key_file" in output


def test_doctor_names_the_setting_to_change_for_every_failure(deployment, capsys):
    """A failure an operator cannot act on is noise.

    Every FAIL row carries its config key, which is the thing they have to edit.
    """
    main(["--config", str(deployment.config_path), "doctor"])

    output = capsys.readouterr().out
    assert "nvd_repo_dir" in output


def test_doctor_exits_zero_on_warnings_only(tmp_path, capsys):
    """A degraded-but-working install must not break a deployment script.

    WARN means "this works and should be fixed". Treating it as failure would
    train operators to ignore the exit code, which costs more than the warning
    is worth. Here every required key is satisfied and the only complaint is a
    managed directory that setup would recreate.
    """
    from sluice.core.db import initialize_database

    state_file = tmp_path / "state.db"
    initialize_database(state_file)
    secret = tmp_path / "secret"
    secret.write_text("value", encoding="utf-8")
    secret.chmod(0o600)

    config_path = _write_config(
        tmp_path / "config.json",
        state_file=str(state_file),
        events_file=str(tmp_path / "events.jsonl"),
        run_events_dir=str(tmp_path / "absent-but-managed"),
        local_source_dir=str(tmp_path),
        local_staging_dir=str(tmp_path),
        disk_quota_root=str(tmp_path),
        labkey_server="https://labkey.example",
        labkey_experiments_project="Proj",
        labkey_schema="lists",
        labkey_query="Experiments",
        labkey_api_key_file=str(secret),
        nvd_repo_dir=str(tmp_path),
        nvd_command="sh",
        nvd_launch_base=str(tmp_path),
        nvd_log_dir=str(tmp_path),
        nvd_results_base=str(tmp_path),
        nvd_blast_db=str(tmp_path),
        nvd_taxonomy_dir=str(tmp_path),
        nvd_virus_index=str(tmp_path),
        slack_enabled=False,
    )
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")

    exit_code = main(["--config", str(config_path), "doctor"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "warn" in output.lower()


def test_doctor_json_output_is_parseable(deployment, capsys):
    """`sluice --json doctor | jq` — a list of result objects, nothing else."""
    main(["--json", "--config", str(deployment.config_path), "doctor"])

    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert {"check", "area", "severity", "detail"} <= set(payload[0])
    assert any(entry["severity"] == "fail" for entry in payload)


def test_doctor_changes_nothing_on_disk(deployment, tmp_path, capsys):
    """The never-mutates promise, asserted rather than trusted.

    Doctor is run when things are already broken. A diagnostic that creates a
    directory or a database while looking would make the next run's report a
    lie about the state it inherited.
    """
    before = sorted(p.name for p in tmp_path.iterdir())

    main(["--config", str(deployment.config_path), "doctor"])

    assert sorted(p.name for p in tmp_path.iterdir()) == before
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/test_cli.py -k doctor -q`
Expected: FAIL — no such command.

- [ ] **Step 3: Add the command**

In `sluice/cli/__init__.py`, add the import:

```python
from sluice.doctor import Severity, run_doctor, worst
```

Add the command near `status`:

```python
@app.command()
def doctor(context: typer.Context) -> None:
    """Check that this installation can actually work.

    [bold]setup proves your config is complete; doctor proves it works.[/bold]
    Reads everything, changes nothing — no directory is created, no config
    written, no repository checked out.

    Exits 0 when nothing failed (warnings still exit 0, so a working-but-
    degraded install stays scriptable) and 1 when anything failed.
    """
    settings = _settings(context)
    with _guard():
        config = load_config(settings.config)
        results = run_doctor(config, _registry(), PRODUCTION_VERSION)

    if settings.as_json:
        _emit_json(
            [
                {
                    "check": result.check_id,
                    "area": result.area,
                    "severity": result.severity.value,
                    "detail": result.detail,
                }
                for result in results
            ]
        )
    else:
        _render_doctor(results)

    if worst(results) is Severity.FAIL:
        raise typer.Exit(EXIT_REFUSED)
```

And the renderer, next to `_render_overview`:

```python
_DOCTOR_STYLES = {
    Severity.OK: ("[green]ok[/green]", "green"),
    Severity.WARN: ("[yellow]warn[/yellow]", "yellow"),
    Severity.FAIL: ("[red]FAIL[/red]", "red"),
    Severity.SKIP: ("[dim]skip[/dim]", "dim"),
}


def _render_doctor(results) -> None:
    """One row per check, grouped by area, failures last in the summary.

    Skipped checks are shown rather than hidden: "not used in this source mode"
    is information an operator wants when they are wondering why doctor said
    nothing about the host they just configured.
    """
    table = Table(title="sluice doctor", show_lines=False)
    table.add_column("")
    table.add_column("area", style="cyan")
    table.add_column("check")
    table.add_column("detail", overflow="fold")
    for result in results:
        marker, _style = _DOCTOR_STYLES[result.severity]
        table.add_row(marker, result.area, result.check_id, result.detail)
    _out.print(table)

    failures = [r for r in results if r.severity is Severity.FAIL]
    warnings = [r for r in results if r.severity is Severity.WARN]
    if failures:
        _out.print(
            f"\n[red]{len(failures)} check(s) failed[/red] — sluice cannot run "
            f"correctly until these are fixed."
        )
    elif warnings:
        _out.print(f"\n[yellow]{len(warnings)} warning(s)[/yellow] — sluice will run.")
    else:
        _out.print("\n[green]All checks passed.[/green]")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/test_cli.py -k doctor -q`
Expected: PASS.

- [ ] **Step 5: Run it for real**

Run: `pixi run sluice --config config.example.json doctor`
Expected: a table, many FAILs (the example config is all placeholders), exit code 1. Confirm every failure message names its key and says something actionable — this is the output an operator reads at 3am, and it is worth reading once yourself.

- [ ] **Step 6: Document it**

Add a section to `docs/OPERATING.md`, after "Installing and upgrading". Written for someone who has never read the codebase:

```markdown
## Checking the installation

```bash
sluice doctor
```

`sluice setup` makes sure your config has every setting. `sluice doctor` makes
sure those settings point at things that are really there — the directories
exist, the credential files are filled in, the NVD repository is where you said.

Run it after `sluice setup`, and any time something looks wrong.

Each line is one check:

- **ok** — verified.
- **warn** — sluice will run, but something should be fixed. A credential file
  readable by other users, or a working directory that is missing and will be
  recreated.
- **FAIL** — sluice cannot work until you fix it. Every failure names the config
  setting to change.
- **skip** — the check does not apply to your setup. Settings for the source
  mode you are not using are skipped, as are notification settings when Slack
  is turned off.

Doctor exits 0 unless something failed, so it is safe to put in a script.
Warnings do not make it exit non-zero.

**Doctor never changes anything.** It only reads. If it reports a problem, it
tells you what to edit; it will not edit it for you.
```

- [ ] **Step 7: Full suite, lint, and commit**

Run: `pixi run test && pixi run lint`
Expected: green.

```bash
git add sluice/cli/__init__.py tests/test_cli.py docs/OPERATING.md
git commit -m "cli: add sluice doctor

setup proves a config is complete; doctor proves it works. Exits 1 on any
FAIL and 0 otherwise — warnings deliberately do not break the exit code, so
a working-but-degraded install stays scriptable and operators do not learn to
ignore it.

Skipped checks are shown rather than hidden: 'not used in this source mode'
is exactly what an operator wants to see when wondering why doctor said
nothing about the host they just configured.

Closes increment A of the doctor design spec; errata E2 partially resolved
(the network tier is increment B)."
```

---

## Definition of done for increment A

- [ ] `pixi run test` green, `pixi run lint` clean.
- [ ] `tests/test_dependency_arrows.py` green — nothing under `sluice/core/` imports `sluice.doctor`.
- [ ] The drift guard has been **seen to fail** in both directions and restored byte-identically.
- [ ] `sluice doctor` run by hand against a real config, and every failure message read once for whether it would help someone who has never seen the code.
- [ ] `docs/OPERATING.md` has the operator-facing section.
- [ ] No new entries in `pixi.toml`.

## Deliberately NOT in increment A

- `--comprehensive` and every network round-trip — increment B, which also adds the source manifest visibility report (spec §8a).
- The `sluice daemon` startup gate and its hidden bypass flag — increment C.
- Widening detection's case-sensitive `--include` filters — spec §11, unscheduled.
