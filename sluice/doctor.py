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
import os
import pathlib
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterable, Mapping

from sluice.config import is_unfilled_placeholder
from sluice.config.schema import CONFIG_FIELDS, ConfigField, FieldKind, ProbeKind
from sluice.config.setup import managed_directory_for
from sluice.core.db import MIGRATIONS, schema_version
from sluice.gates.detection import SOURCE_DIR_KEY, is_local_mode
from sluice.gates.nvd import resolve_launch_dir
from sluice.gates.nvd_tool import NvdToolError, build_nvd_command, nvd_manifest_path
from sluice.gates.slack import TOKEN_ENV_VAR

__all__ = [
    "GATE_CONFIG_GROUPS",
    "LOCAL_SOURCE_KEYS",
    "REMOTE_SOURCE_KEYS",
    "Check",
    "CheckResult",
    "Severity",
    "check_for_field",
    "run_checks",
    "run_doctor",
    "select_checks",
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
# apply is not a degradation.
_RANK = {Severity.OK: 0, Severity.SKIP: 0, Severity.WARN: 1, Severity.FAIL: 2}

# The inverse of `_RANK`, so `worst` reports a severity an operator can act on:
# a run of nothing but skips ranks 0 and must read as OK, not as SKIP.
_SEVERITY_BY_RANK = {0: Severity.OK, 1: Severity.WARN, 2: Severity.FAIL}


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

    **Derived, never hand-assigned** (design spec D6). The rule falls out of who
    can supply the value: a ``MANAGED_PATH`` is sluice's own working directory
    that setup creates and can recreate, so its absence is a WARN; every other
    kind can only come from a human, so its absence stops the pipeline.
    """
    if field.kind is FieldKind.MANAGED_PATH:
        return Severity.WARN
    return Severity.FAIL


def _has_real_value(config: Mapping, key: str) -> bool:
    """Whether ``key`` holds something an operator actually chose.

    Empty and unfilled-placeholder are the same answer here — *not configured* —
    which is what a cross-field "one of these must be set" rule has to ask.
    """
    value = config.get(key)
    return bool(value) and not is_unfilled_placeholder(value)


def _check_path(field: ConfigField, value) -> CheckResult:
    """Local tier for ``PATH_EXISTS``: the path is there and this user can read it.

    A ``MANAGED_PATH`` is judged by ``managed_directory_for``: setup creates the
    *directory* sluice writes into, and the file itself (``events.jsonl``)
    appears on first write, so demanding the file would warn about a correct
    install. When the two differ the detail says which one was looked at — a bare
    path there reads as a verdict on the file, and for ``state_file`` it would
    contradict ``schema_ladder`` in the row above.

    Type is not checked: ``nvd_repo_dir`` naming a file rather than a directory
    still reports ``ok``, because the schema carries no file-vs-directory
    distinction to check against (design spec §5 errata).
    """
    target = pathlib.Path(str(value)).expanduser()
    checked = (
        managed_directory_for(target)
        if field.kind is FieldKind.MANAGED_PATH
        else target
    )
    if not checked.exists():
        return CheckResult(
            field.name,
            field.group,
            severity_for(field),
            f"{field.name}: nothing at {checked}",
        )
    if not os.access(checked, os.R_OK):
        return CheckResult(
            field.name,
            field.group,
            severity_for(field),
            f"{field.name}: {checked} exists but is not readable by this user",
        )
    if checked != target:
        return CheckResult(
            field.name,
            field.group,
            Severity.OK,
            f"directory {checked} exists ({target.name} is created on first write)",
        )
    return CheckResult(field.name, field.group, Severity.OK, str(checked))


def _check_secret(field: ConfigField, value) -> CheckResult:
    """Local tier for ``SECRET_READABLE``: a non-empty, owner-only credential file.

    Reads the file's metadata only, never its contents — a credential must not
    be able to reach a message, a log line or an exception.
    """
    path = pathlib.Path(str(value)).expanduser()
    try:
        # One stat for every question below: a second call could raise outside
        # this handler and surface as "the check itself failed".
        status = path.stat()
    except OSError as unreadable:
        return CheckResult(
            field.name,
            field.group,
            severity_for(field),
            f"{field.name}: no readable credential file at {path} "
            f"({type(unreadable).__name__})",
        )
    if not stat.S_ISREG(status.st_mode):
        return CheckResult(
            field.name,
            field.group,
            severity_for(field),
            f"{field.name}: {path} is not a file",
        )
    if status.st_size == 0:
        # setup creates these empty for the operator to fill, so an install
        # where that never happened looks configured and fails at the first
        # real call.
        return CheckResult(
            field.name,
            field.group,
            severity_for(field),
            f"{field.name}: {path} is empty — setup created it for you to fill in",
        )
    if status.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        # Never a FAIL: it works. But a credential readable by everyone on a
        # shared machine is worth saying out loud.
        return CheckResult(
            field.name,
            field.group,
            Severity.WARN,
            f"{field.name}: {path} is readable by other users "
            f"(mode {oct(stat.S_IMODE(status.st_mode))})",
        )
    return CheckResult(field.name, field.group, Severity.OK, str(path))


def _check_toolchain(field: ConfigField, value, config: Mapping) -> CheckResult:
    """Local tier for ``TOOLCHAIN``: the command resolves *the way the gate runs it*.

    ``nvd`` is never invoked from ``PATH`` — ``build_nvd_command`` runs it as
    ``pixi run --manifest-path <nvd_repo_dir>/pyproject.toml nvd``, because a
    bare ``nvd`` picks up whatever wrapper is installed (a stale
    ``~/.local/bin/nvd`` shim did exactly that in production). So a ``which``
    probe would fail every correct install and pass the one broken install it
    was aimed at. The launcher and the manifest come from the adapter itself,
    not from a copy of its rule.

    There is one ``TOOLCHAIN`` key today; a second pinned environment would need
    a gate-to-adapter mapping here rather than this direct call.
    """
    try:
        launcher = build_nvd_command(config)[0]
        manifest = nvd_manifest_path(config)
    except KeyError as missing_key:
        return CheckResult(
            field.name,
            field.group,
            severity_for(field),
            f"{field.name}: cannot be checked because {missing_key} is not set",
        )
    if shutil.which(launcher) is None:
        return CheckResult(
            field.name,
            field.group,
            severity_for(field),
            f"{field.name}: the launcher {launcher!r} is not on PATH — "
            f"{value!r} runs as `{launcher} run --manifest-path ...`, never "
            f"from PATH itself",
        )
    if not manifest.is_file():
        return CheckResult(
            field.name,
            field.group,
            severity_for(field),
            f"{field.name}: no pixi manifest at {manifest} — nvd_repo_dir must "
            f"point at the NVD checkout whose pyproject.toml pins the tool "
            f"version",
        )
    return CheckResult(
        field.name, field.group, Severity.OK, f"{value} via {launcher} @ {manifest}"
    )


def check_for_field(field: ConfigField) -> Check:
    """Build the local-tier check for one config key, from its ``ProbeKind``.

    Presence is checked for **every** key, whatever its probe: nothing else
    validates a deployed config's key set — ``load_config`` declines to, and the
    schema guard only reads the source tree — so a required key with no default
    is invisible until the gate that reads it raises ``KeyError`` mid-run.

    Increment A is local-only: ``HOST_REACHABLE`` and ``ENDPOINT`` verify that
    the value is *set*, not that the far end answers — the network tier is
    increment B. That is a deliberate floor, not a stub: an unset host is by far
    the more common failure, and catching it costs no round-trip.

    The returned check never raises for an ordinary missing thing; it returns a
    result. ``run_checks`` catches genuine escapes.
    """

    def run(config: Mapping) -> CheckResult:
        area = field.group
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

        if is_unfilled_placeholder(value):
            if not field.required:
                # WARN, not SKIP: no consumer in this codebase treats a
                # placeholder as absent — they all test truthiness, so the nvd
                # gate would run `--profile '<nvd_profile>'`. It is a live broken
                # value, so the report must not be silent about it; but the key
                # is optional, so it must not colour the exit code either.
                return CheckResult(
                    field.name,
                    area,
                    Severity.WARN,
                    f"{field.name} is still the placeholder setup wrote — it is "
                    f"passed through as the literal {value!r}. Fill it in, or "
                    f"clear the key if you do not use it",
                )
            return CheckResult(
                field.name,
                area,
                severity_for(field),
                f"{field.name} has not been filled in — it is still a placeholder",
            )

        if field.probe is ProbeKind.NONE:
            return CheckResult(field.name, area, Severity.OK, f"set ({value})")
        if field.probe is ProbeKind.PATH_EXISTS:
            return _check_path(field, value)
        if field.probe is ProbeKind.SECRET_READABLE:
            return _check_secret(field, value)
        if field.probe is ProbeKind.TOOLCHAIN:
            return _check_toolchain(field, value, config)
        if field.probe in (ProbeKind.HOST_REACHABLE, ProbeKind.ENDPOINT):
            # Set and not a placeholder is all the local tier can establish.
            # Increment B contacts them.
            return CheckResult(field.name, area, Severity.OK, f"set ({value})")

        # Exhaustive on purpose: a new ProbeKind falling through to "ok" would
        # report an unverified key as healthy. run_checks turns this into a FAIL
        # naming the omission.
        raise AssertionError(f"no local check for probe kind {field.probe!r}")

    return Check(field.name, field.group, run)


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
    degraded-but-working install stays scriptable. A run of nothing but skips
    reports OK rather than SKIP, so a caller may compare against ``OK`` as
    safely as against ``FAIL``.
    """
    highest = max((_RANK[result.severity] for result in results), default=0)
    return _SEVERITY_BY_RANK[highest]


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
    # "storage" because resolve_launch_dir reads local_staging_dir to refuse a
    # launch base inside the staging tree (errata D2) — the drift guard caught
    # that read, which is what it is for.
    "nvd": ("nvd", "nvd reference data", "nvd analysis", "storage"),
}

# Config groups that are not a gate's. `sluice state` is universal; `slack`
# belongs to the notifier, which is not a gate at all.
_UNIVERSAL_GROUPS = ("sluice state",)
_NOTIFIER_GROUPS = ("slack",)

# The `detection` group split by source mode. Public and exhaustive: a new
# detection key belongs to one side or the other, and
# `tests/test_doctor_drift.py` fails if one is added to neither, because an
# unclassified key would be demanded in both modes.
LOCAL_SOURCE_KEYS = frozenset({SOURCE_DIR_KEY, "watch_dir"})
REMOTE_SOURCE_KEYS = frozenset(
    {"remote_host", "remote_user", "remote_source_path", "password_file"}
)


def _is_local_source_mode(config: Mapping) -> bool:
    """Detection's own source-mode rule, minus a value nobody has filled in.

    ``is_local_mode`` is the one home for *what makes an install local*, and it
    is deliberately a plain presence check: a literal ``<local_source_dir>`` is
    a path like any other and ``rsync`` errors out on it, so the adapter needs no
    opinion about placeholders and stays free of ``sluice`` imports.

    Doctor's job is the opposite one — say so *before* the run — so it composes
    that rule with the config layer's, rather than either copying detection's
    test or asking detection to carry doctor's concern. Without this, a
    ``<local_source_dir>`` placeholder reads as "local mode configured" and
    silences all four remote keys as not-in-use, which is how an install with no
    data source at all came to report "All checks passed".
    """
    return is_local_mode(config) and not is_unfilled_placeholder(
        config.get(SOURCE_DIR_KEY)
    )


def _source_mode_skip(config: Mapping, field: ConfigField) -> bool:
    """Whether ``field`` belongs to the source mode this install is NOT using.

    Detection reads a local directory or a remote host, never both
    (``is_local_mode``). Demanding the unused mode's keys would fail every
    correctly configured install, and one that cried wolf would be ignored.
    """
    if _is_local_source_mode(config):
        return field.name in REMOTE_SOURCE_KEYS
    return field.name in LOCAL_SOURCE_KEYS


def _source_mode_check() -> Check:
    """Assert exactly one source mode is configured — detection needs one.

    A one-of-N rule, which no per-key ``required`` flag can express: both
    ``local_source_dir`` and ``remote_host`` are optional on their own, so a
    config with *neither* passes every individual check while ``_source_mode_skip``
    silences the other mode's keys as "not used". That composition is how an
    install with no data source at all reported "All checks passed" and exit 0;
    the daemon then detects nothing, forever, and says nothing.
    """

    def run(config: Mapping) -> CheckResult:
        local = _is_local_source_mode(config)
        remote = _has_real_value(config, "remote_host")
        if local and remote:
            return CheckResult(
                "source_mode",
                "detection",
                Severity.FAIL,
                f"both {SOURCE_DIR_KEY} and remote_host are set, so which one "
                f"detection sweeps is not stated anywhere — clear the one you "
                f"do not use",
            )
        if not local and not remote:
            return CheckResult(
                "source_mode",
                "detection",
                Severity.FAIL,
                f"neither {SOURCE_DIR_KEY} nor remote_host is set, so detection "
                f"has nothing to look at and no run will ever be found — set one",
            )
        return CheckResult(
            "source_mode",
            "detection",
            Severity.OK,
            "local source directory" if local else "remote host",
        )

    return Check("source_mode", "detection", run)


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
        return CheckResult("profile_resolves", "profile", Severity.OK, pipeline_version)

    return Check("profile_resolves", "profile", run)


def _connect_read_only(path: pathlib.Path) -> sqlite3.Connection:
    """Open ``path`` for reading, without writing any of sluice's data.

    Not ``db.get_connection``: that issues ``PRAGMA journal_mode=WAL``, which
    SQLite persists in the file header and which is therefore a **write** — it
    would silently rewrite a restored backup, and fail outright on a database
    this user cannot write to. ``mode=ro`` cannot alter a byte of run state.

    Not ``immutable=1`` either, though it creates nothing at all: it takes no
    lock, so reading a database the daemon is checkpointing returns torn pages
    as ``DatabaseError: database disk image is malformed`` (measured at 6.6% of
    opens against a live writer, versus zero for ``mode=ro``). Doctor renders
    that as "not a readable sluice database", which would send an operator to
    restore a backup they do not need — the worst thing a diagnostic can say.
    Running doctor while the daemon is up is the normal case.

    The accepted costs: opening a WAL database creates ``-shm``/``-wal``
    sidecars a read-only connection cannot remove again, and the open fails on
    read-only media because it cannot create them. Both are honest — the
    sidecars are SQLite's own protocol for a database sluice owns, not doctor
    writing data, and a loud failure beats a wrong answer.

    Raises:
        sqlite3.DatabaseError: the file is absent, unreadable, or not a SQLite
            database. ``OperationalError`` is a sibling, so catch the parent to
            catch both.
    """
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


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
        if not path.exists():
            return CheckResult(
                "schema_ladder",
                "universal",
                Severity.FAIL,
                f"no database at {path} — run `sluice setup` to create it",
            )
        expected = max(version for version, _name, _apply in MIGRATIONS)
        try:
            connection = _connect_read_only(path)
            try:
                current = schema_version(connection)
            finally:
                connection.close()
        except sqlite3.DatabaseError as not_a_database:
            # A state_file pointing at a directory, a text file, or a database
            # this user cannot open is an ordinary deployment mistake. Without
            # this, run_checks reports "the check itself failed", which tells an
            # operator doctor is broken when their config is.
            return CheckResult(
                "schema_ladder",
                "universal",
                Severity.FAIL,
                f"state_file: {path} is not a readable sluice database "
                f"({type(not_a_database).__name__}) — check the path, or run "
                f"`sluice setup`",
            )
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


def _launch_base_check() -> Check:
    """Assert the NVD launch base does not sit inside the staging tree (errata D2).

    A cross-field rule, so no single key's check can catch it: both paths are
    individually valid. Asked of ``resolve_launch_dir`` itself rather than
    re-derived, so doctor's answer is the gate's answer. Getting this wrong
    costs a multi-hour re-run at the last and most expensive gate, because
    ``--resume`` silently does nothing on shared storage.

    Skipped unless both keys hold a real value, since comparing two unfilled
    placeholders would always report ``ok``.
    """

    def run(config: Mapping) -> CheckResult:
        unusable = [
            key
            for key in ("nvd_launch_base", "local_staging_dir")
            if not _has_real_value(config, key)
        ]
        if unusable:
            # Each key's own check already says so. Comparing two placeholders
            # would report a confident `ok` about a config nobody has filled in.
            return CheckResult(
                "nvd_launch_base_is_local",
                "nvd",
                Severity.SKIP,
                f"{' and '.join(unusable)} not usable yet",
            )
        try:
            resolve_launch_dir(config, "doctor")
        except NvdToolError as refused:
            return CheckResult(
                "nvd_launch_base_is_local", "nvd", Severity.FAIL, str(refused)
            )
        return CheckResult(
            "nvd_launch_base_is_local",
            "nvd",
            Severity.OK,
            "nvd_launch_base is outside local_staging_dir",
        )

    return Check("nvd_launch_base_is_local", "nvd", run)


def _unregistered_gate_check(gate_id: str) -> Check:
    """Fail loudly for a profile gate doctor has no configuration map for.

    The drift guard (``tests/test_doctor_drift.py``) is the coupling chosen over
    a twelfth ``Gate`` field, and a gate absent from ``GATE_CONFIG_GROUPS``
    would otherwise contribute no groups and pass silently — no coverage for
    exactly the change the guard exists to police. Reported rather than raised,
    because a diagnostic that dies tells the operator nothing about the other
    forty checks.
    """

    def run(config: Mapping) -> CheckResult:
        return CheckResult(
            f"gate_registered:{gate_id}",
            gate_id,
            Severity.FAIL,
            f"gate {gate_id!r} is in the production profile but has no entry in "
            f"GATE_CONFIG_GROUPS, so doctor checks none of its configuration — "
            f"add one in sluice/doctor.py (an empty tuple means 'reads no "
            f"config')",
        )

    return Check(f"gate_registered:{gate_id}", gate_id, run)


def _notifier_field(config: Mapping, field: ConfigField) -> ConfigField:
    """``field``, made required when an enabled notifier cannot work without it.

    Every ``slack`` key is optional in the schema, because Slack off is a
    supported deployment. Once ``slack_enabled`` is true that stops being true:
    ``make_notifier`` returns ``None`` — notifications silently off, one log line
    nobody is watching for — when the channel is unset or no token can be found.
    Mirrors that factory's conditions, including its ``$SLACK_BOT_TOKEN``
    fallback, so an env-token install is not a false failure.
    ``slack_user_id_file`` stays optional: without it the notifier still posts,
    it just @-mentions nobody.
    """
    if not config.get("slack_enabled"):
        return field
    if field.name == "slack_channel":
        return dataclasses.replace(field, required=True)
    if field.name == "slack_bot_token_file" and not os.environ.get(TOKEN_ENV_VAR):
        return dataclasses.replace(field, required=True)
    return field


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
    gate_ids = tuple(gate.id for gate in profile.gates) if profile is not None else ()

    checks = [_profile_check(registry, pipeline_version), _schema_ladder_check()]
    wanted_groups = set(_UNIVERSAL_GROUPS)
    for gate_id in gate_ids:
        if gate_id not in GATE_CONFIG_GROUPS:
            checks.append(_unregistered_gate_check(gate_id))
            continue
        wanted_groups.update(GATE_CONFIG_GROUPS[gate_id])
    if config.get("slack_enabled"):
        wanted_groups.update(_NOTIFIER_GROUPS)

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
        checks.append(check_for_field(_notifier_field(config, field)))
    # Cross-field rules, which belong to no single key's check.
    if "detection" in wanted_groups:
        checks.append(_source_mode_check())
    if "nvd" in gate_ids:
        checks.append(_launch_base_check())
    return tuple(checks)


def run_doctor(
    config: Mapping, registry: Mapping, pipeline_version: str
) -> tuple[CheckResult, ...]:
    """Select and run every applicable check. The whole diagnostic, in one call."""
    return run_checks(select_checks(config, registry, pipeline_version), config)
