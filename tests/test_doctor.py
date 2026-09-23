"""Behaviour of ``sluice doctor`` — increment A, local checks only."""

import dataclasses
import shutil
import sqlite3
import threading

import pytest

from sluice import doctor
from sluice.config.schema import ConfigField, FieldKind, ProbeKind
from sluice.core.daemon import assemble_registry
from sluice.core.db import initialize_database, now_iso, set_metadata, write_txn
from sluice.core.profile import REFERENCE_PROFILES, Applicability, build_profile
from sluice.doctor import (
    Check,
    CheckResult,
    Severity,
    check_for_field,
    run_checks,
    run_doctor,
    select_checks,
    severity_for,
    worst,
)
from sluice.gates.download import DOWNLOAD
from sluice.gates.experiment import EXPERIMENT
from sluice.gates.profiles import PRODUCTION_PROFILES, PRODUCTION_VERSION
from sluice.gates.slack import TOKEN_ENV_VAR


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
        Check(
            "a",
            "universal",
            lambda config: CheckResult("a", "universal", Severity.OK, "fine"),
        ),
        Check(
            "b",
            "universal",
            lambda config: CheckResult("b", "universal", Severity.FAIL, "broken"),
        ),
        Check(
            "c",
            "universal",
            lambda config: CheckResult("c", "universal", Severity.OK, "fine"),
        ),
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
    skip = CheckResult("d", "x", Severity.SKIP, "")
    assert worst((ok, warn)) is Severity.WARN
    assert worst((ok, warn, fail)) is Severity.FAIL
    assert worst((ok,)) is Severity.OK
    assert worst(()) is Severity.OK
    # Nothing but skips is a healthy report, not a skipped one: the increment-C
    # startup gate may compare against OK as safely as against FAIL.
    assert worst((skip, skip)) is Severity.OK
    assert worst((skip, warn)) is Severity.WARN


# ---------------------------------------------------------------------------
# The local probes.
# ---------------------------------------------------------------------------


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


def test_a_managed_file_says_it_judged_the_directory_not_the_file(tmp_path):
    """The ``ok`` must not read as a verdict on a file that is not there.

    ``state_file`` is a MANAGED_PATH naming a file setup's *parent* directory
    holds, so its PATH_EXISTS check looks at the directory — otherwise a correct
    install warns about an ``events.jsonl`` the daemon creates on first write.
    But a bare path in the detail then reads "your state file is fine" directly
    under a ``schema_ladder`` row saying there is no database at all: two rows
    about one setting, disagreeing.
    """
    (tmp_path / "state").mkdir()
    field = _field("state_file", FieldKind.MANAGED_PATH)

    result = check_for_field(field).run(
        {"state_file": str(tmp_path / "state" / "sluice_state.db")}
    )

    assert result.severity is Severity.OK
    assert "directory" in result.detail
    assert "sluice_state.db is created on first write" in result.detail


def test_an_unset_placeholder_is_reported_as_unset_not_as_broken():
    """``<remote_host>`` means setup wrote it and nobody filled it in.

    Reporting that as "host unreachable" would send an operator to the network
    when the answer is one line of config.
    """
    field = _field("remote_host", FieldKind.VALUE, probe=ProbeKind.HOST_REACHABLE)
    result = check_for_field(field).run({"remote_host": "<remote_host>"})
    assert result.severity is Severity.FAIL
    assert "not been filled in" in result.detail


def test_an_optional_placeholder_warns_and_is_never_silent():
    """An unfilled ``<placeholder>`` is a live broken value, not an absent one.

    Nothing in this codebase treats it as absent: ``sluice/gates/nvd.py`` does
    ``if profile:`` and would run ``--profile '<nvd_profile>'``; the daemon's
    watch lister guards on ``not str(watch_dir).strip()``, which a placeholder
    passes. So the report must not be silent about it. But the key is optional,
    so it must not colour the exit code either — WARN is the only severity that
    is both.

    Required and optional are asserted against the *same value* so the two
    cannot drift apart, and the never-silent property is asserted directly:
    SKIP here is the regression that made doctor print "All checks passed" on an
    install with no data source.
    """
    same_value = {"nvd_profile": "<nvd_profile>"}

    required = check_for_field(
        _field("nvd_profile", FieldKind.VALUE, probe=ProbeKind.NONE, required=True)
    ).run(same_value)
    optional = check_for_field(
        _field("nvd_profile", FieldKind.VALUE, probe=ProbeKind.NONE, required=False)
    ).run(same_value)

    assert required.severity is Severity.FAIL
    assert optional.severity is Severity.WARN
    assert optional.severity is not Severity.SKIP, (
        "a value that is passed through verbatim must never be reported silently"
    )
    assert "nvd_profile" in optional.detail
    # Says what will actually happen, not just that it is unset.
    assert "<nvd_profile>" in optional.detail


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
    field = _field(
        "labkey_api_key_file", FieldKind.SECRET_FILE, probe=ProbeKind.SECRET_READABLE
    )
    result = check_for_field(field).run({"labkey_api_key_file": str(secret)})
    assert result.severity is Severity.FAIL
    assert "empty" in result.detail

    secret.write_text("a-real-key")
    secret.chmod(0o600)
    assert (
        check_for_field(field).run({"labkey_api_key_file": str(secret)}).severity
        is Severity.OK
    )


def test_a_world_readable_secret_warns_but_does_not_fail(tmp_path):
    """It works; it should not. A WARN says so without blocking the pipeline."""
    secret = tmp_path / "key"
    secret.write_text("a-real-key")
    secret.chmod(0o644)
    field = _field(
        "password_file", FieldKind.SECRET_FILE, probe=ProbeKind.SECRET_READABLE
    )
    result = check_for_field(field).run({"password_file": str(secret)})
    assert result.severity is Severity.WARN
    assert "readable by other users" in result.detail


def test_a_secret_check_never_puts_the_credential_in_its_message(tmp_path):
    """The one rule that must never break, asserted rather than trusted."""
    secret = tmp_path / "key"
    secret.write_text("SUPER-SECRET-VALUE")
    secret.chmod(0o644)
    field = _field(
        "password_file", FieldKind.SECRET_FILE, probe=ProbeKind.SECRET_READABLE
    )
    result = check_for_field(field).run({"password_file": str(secret)})
    assert "SUPER-SECRET-VALUE" not in result.detail


def _pixi_on_path(monkeypatch, tmp_path):
    """Put a fake ``pixi`` launcher on PATH and return the directory holding it."""
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    launcher = launcher_dir / "pixi"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(launcher_dir))
    return launcher_dir


def test_a_toolchain_command_is_checked_the_way_the_gate_invokes_it(
    tmp_path, monkeypatch
):
    """``nvd`` is never run from PATH, so doctor must not probe PATH for it.

    ``build_nvd_command`` runs it as ``pixi run --manifest-path
    <nvd_repo_dir>/pyproject.toml nvd``, precisely because a bare ``nvd`` picks
    up whatever wrapper is installed — a stale ``~/.local/bin/nvd`` shim did
    exactly that in production. A ``which('nvd')`` probe would therefore FAIL
    every correct install and pass the one broken install it was aimed at, so
    what is checked is the launcher and the manifest that pin the environment.
    """
    _pixi_on_path(monkeypatch, tmp_path)
    repo = tmp_path / "nvd-repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n")
    field = _field("nvd_command", FieldKind.VALUE, probe=ProbeKind.TOOLCHAIN)
    config = {"nvd_command": "nvd", "nvd_repo_dir": str(repo)}

    # The correct shape: nvd is NOT on PATH, and that is fine.
    assert shutil.which("nvd") is None
    assert check_for_field(field).run(config).severity is Severity.OK


def test_a_toolchain_check_fails_when_the_pinned_environment_is_missing(
    tmp_path, monkeypatch
):
    """No manifest at nvd_repo_dir means every nvd invocation will fail."""
    _pixi_on_path(monkeypatch, tmp_path)
    field = _field("nvd_command", FieldKind.VALUE, probe=ProbeKind.TOOLCHAIN)

    result = check_for_field(field).run(
        {"nvd_command": "nvd", "nvd_repo_dir": str(tmp_path / "not-a-checkout")}
    )

    assert result.severity is Severity.FAIL
    assert "pyproject.toml" in result.detail
    assert "nvd_repo_dir" in result.detail


def test_a_toolchain_check_fails_when_the_launcher_is_absent(tmp_path, monkeypatch):
    """The launcher is what the gate actually executes, so it must resolve."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))
    repo = tmp_path / "nvd-repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n")
    field = _field("nvd_command", FieldKind.VALUE, probe=ProbeKind.TOOLCHAIN)

    result = check_for_field(field).run(
        {"nvd_command": "nvd", "nvd_repo_dir": str(repo)}
    )

    assert result.severity is Severity.FAIL
    assert "pixi" in result.detail
    assert "not on PATH" in result.detail


def test_a_required_tuning_value_nobody_supplied_is_a_failure():
    """``ProbeKind.NONE`` means "nothing to check BEYOND presence", not "skip".

    ``nvd_blast_db_prefix`` and its two siblings are required, have no default,
    and are omitted by ``sluice setup`` because only a human can supply them.
    Nothing else validates a deployed config's key set — the loader declines to,
    and the schema guard reads the source tree, not the operator's file — so
    skipping them here means the first sign of the mistake is a KeyError at the
    last and most expensive gate, after several TB have been downloaded.
    """
    field = _field("nvd_blast_db_prefix", FieldKind.VALUE, probe=ProbeKind.NONE)

    missing = check_for_field(field).run({})
    assert missing.severity is Severity.FAIL
    assert "nvd_blast_db_prefix" in missing.detail

    assert (
        check_for_field(field).run({"nvd_blast_db_prefix": "nt"}).severity
        is Severity.OK
    )


# ---------------------------------------------------------------------------
# Profile-aware assembly and the universal checks.
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    return assemble_registry(REFERENCE_PROFILES, PRODUCTION_PROFILES)


def test_selection_covers_the_gates_in_the_active_profile(registry):
    """Every gate in production-v3.1 contributes its config keys."""
    checks = select_checks({}, registry, PRODUCTION_VERSION)
    areas = {check.area for check in checks}
    assert "labkey" in areas  # the experiment gate
    assert "nvd" in areas  # the nvd gate
    assert "storage" in areas  # the download gate


def test_selection_asks_only_about_the_gates_this_profile_contains():
    """Selection follows the profile, not the whole schema.

    The production profile happens to cover every group in the schema, so an
    assertion made against it alone would pass against a ``select_checks`` that
    ignored the registry entirely. A two-gate profile is the case that can fail:
    with no nvd gate, nothing may ask about the NVD reference data — a deployment
    that stops at download must not be told its BLAST database is missing.
    """
    two_gates = build_profile(
        "two-gate-v1",
        [(EXPERIMENT, Applicability.REQUIRED), (DOWNLOAD, Applicability.REQUIRED)],
    )

    areas = {
        check.area
        for check in select_checks({}, {"two-gate-v1": two_gates}, "two-gate-v1")
    }

    assert "labkey" in areas, "the experiment gate's own configuration is missing"
    assert "storage" in areas, "the download gate's own configuration is missing"
    assert "nvd" not in areas, "asked about a gate this profile does not contain"
    assert "nvd reference data" not in areas
    assert "sluice state" in areas, "universal checks apply to every profile"


def test_a_gate_doctor_knows_nothing_about_is_a_loud_failure():
    """A new gate must not slip through with no checks at all.

    The drift guard is the coupling chosen over a twelfth ``Gate`` field, and it
    only bites if an unmapped gate is a failure rather than an empty iteration.
    Three gates are already scheduled, so this is the next change, not a
    hypothetical one.
    """
    # Stands in for a gate that landed without a GATE_CONFIG_GROUPS entry.
    unmapped = dataclasses.replace(EXPERIMENT, id="globus_submit")
    with_unmapped = build_profile("invented-v1", [(unmapped, Applicability.REQUIRED)])

    results = run_doctor({}, {"invented-v1": with_unmapped}, "invented-v1")

    unregistered = [r for r in results if "globus_submit" in r.check_id]
    assert unregistered, "an unmapped gate produced no finding at all"
    assert unregistered[0].severity is Severity.FAIL
    assert "GATE_CONFIG_GROUPS" in unregistered[0].detail


def test_the_notifier_is_only_checked_when_slack_is_enabled(registry):
    """Slack off is a valid configuration, not a fault."""
    off = {
        check.area
        for check in select_checks(
            {"slack_enabled": False}, registry, PRODUCTION_VERSION
        )
    }
    on = {
        check.area
        for check in select_checks(
            {"slack_enabled": True}, registry, PRODUCTION_VERSION
        )
    }
    assert "slack" not in off
    assert "slack" in on


def test_an_enabled_but_unconfigured_notifier_fails(registry, monkeypatch):
    """Slack on with no channel and no token is broken, not "not configured".

    ``make_notifier`` returns ``None`` in that state — notifications silently
    off, one log line at startup nobody is watching for. A missing Slack token is
    one of the four E2 failures doctor exists to catch, so it must FAIL rather
    than skip four optional keys and exit 0.
    """
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

    results = run_doctor({"slack_enabled": True}, registry, PRODUCTION_VERSION)
    by_check = {result.check_id: result for result in results}

    assert by_check["slack_channel"].severity is Severity.FAIL
    assert by_check["slack_bot_token_file"].severity is Severity.FAIL
    # Without it the notifier still posts; it just @-mentions nobody.
    assert by_check["slack_user_id_file"].severity is Severity.SKIP


def test_a_token_in_the_environment_is_not_a_missing_token(registry, monkeypatch):
    """``$SLACK_BOT_TOKEN`` is a supported source, so demanding the file lies."""
    monkeypatch.setenv(TOKEN_ENV_VAR, "xoxb-not-a-real-token")

    results = run_doctor({"slack_enabled": True}, registry, PRODUCTION_VERSION)
    token = [r for r in results if r.check_id == "slack_bot_token_file"]

    assert token and token[0].severity is Severity.SKIP


def test_a_disabled_notifier_demands_nothing(registry):
    """Slack off means the whole notifier tier is absent from the report."""
    results = run_doctor({"slack_enabled": False}, registry, PRODUCTION_VERSION)

    assert not [r for r in results if r.area == "slack"]


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


def test_an_install_with_no_source_at_all_is_a_failure(registry):
    """Neither source mode configured must not report a clean bill of health.

    A one-of-N requirement that no per-key ``required`` flag can express:
    ``local_source_dir`` and ``remote_host`` are each optional, so with neither
    set every individual check passes and ``_source_mode_skip`` silences the
    other half as "not used in this source mode". An otherwise-complete config
    then printed "All checks passed" and exited 0 while the daemon detected
    nothing, forever.
    """
    results = run_doctor({}, registry, PRODUCTION_VERSION)
    mode = [r for r in results if r.check_id == "source_mode"]

    assert mode, "nothing asked whether this install has a data source at all"
    assert mode[0].severity is Severity.FAIL
    assert "local_source_dir" in mode[0].detail
    assert "remote_host" in mode[0].detail


def test_an_unfilled_local_source_placeholder_does_not_pass_as_local_mode(registry):
    """`<local_source_dir>` is truthy, and that is how the whole tier fell silent.

    Judging it "local mode" skipped all four remote keys as not-in-use, and the
    optional placeholder rule accounted for the last one — so an otherwise
    complete config reported nothing at all about detection.

    ``is_local_mode`` is deliberately placeholder-blind: a literal
    ``<local_source_dir>`` is a path like any other and ``rsync`` errors out on
    it, so the adapter needs no opinion. Doctor's whole job is to say so first,
    so the placeholder rule is *doctor's* to apply — this is the test that the
    composition, not the adapter, carries it.
    """
    results = run_doctor(
        {"local_source_dir": "<local_source_dir>"}, registry, PRODUCTION_VERSION
    )
    by_check = {result.check_id: result for result in results}

    assert by_check["source_mode"].severity is Severity.FAIL
    # The remote keys are now asked about, rather than skipped as "not in use".
    assert by_check["remote_host"].severity is Severity.FAIL
    assert worst(results) is Severity.FAIL


def test_two_source_modes_at_once_is_an_ambiguity_worth_naming(registry, tmp_path):
    """Both configured means nothing states which one detection will sweep."""
    results = run_doctor(
        {"local_source_dir": str(tmp_path), "remote_host": "sequencer.example"},
        registry,
        PRODUCTION_VERSION,
    )
    mode = [r for r in results if r.check_id == "source_mode"]

    assert mode and mode[0].severity is Severity.FAIL
    assert "both" in mode[0].detail


def test_one_source_mode_configured_is_healthy(registry, tmp_path):
    """The correct shapes must not be nagged at, in either direction."""
    local = run_doctor(
        {"local_source_dir": str(tmp_path)}, registry, PRODUCTION_VERSION
    )
    remote = run_doctor(
        {"remote_host": "sequencer.example"}, registry, PRODUCTION_VERSION
    )

    assert [r for r in local if r.check_id == "source_mode"][0].severity is Severity.OK
    assert [r for r in remote if r.check_id == "source_mode"][0].severity is Severity.OK


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


def test_reading_the_database_does_not_write_to_it(tmp_path, registry):
    """Doctor opens the state database read-only — reading is not a write.

    ``db.get_connection`` issues ``PRAGMA journal_mode=WAL``, which SQLite
    persists in the **file header**. Reusing it here would silently rewrite a
    database produced by any other tool, and would fail outright on one this user
    cannot write to — a colleague inspecting the daemon's state file gets
    "attempt to write a readonly database" instead of the migration answer they
    asked for.

    The DELETE iteration is what does the work — a restored backup or a legacy
    database is in DELETE mode, and the header rewrite shows up as changed bytes.
    Do not delete it thinking the WAL iteration covers it: a WAL database is
    already in WAL mode, so ``get_connection`` changes nothing there.

    ``mode=ro`` may create ``-shm``/``-wal`` sidecars beside a WAL database and
    cannot remove them again. That is SQLite's own protocol for a database sluice
    owns, not doctor writing data, so only the sidecars are tolerated: any *other*
    new file, and any change to sluice's own bytes, is still a failure.
    """
    tolerated_sidecars = {"-wal", "-shm", "-journal"}

    for journal_mode in ("DELETE", "WAL"):
        state_file = tmp_path / f"state-{journal_mode}.db"
        initialize_database(state_file)
        connection = sqlite3.connect(state_file)
        connection.execute(f"PRAGMA journal_mode={journal_mode}")
        connection.close()
        before_bytes = state_file.read_bytes()
        before_names = {path.name for path in tmp_path.iterdir()}

        results = run_doctor(
            {"state_file": str(state_file)}, registry, PRODUCTION_VERSION
        )

        assert state_file.read_bytes() == before_bytes, (
            f"doctor altered the {journal_mode} database it promised only to read"
        )
        appeared = {path.name for path in tmp_path.iterdir()} - before_names
        assert all(
            name.startswith(state_file.name)
            and name[len(state_file.name) :] in tolerated_sidecars
            for name in appeared
        ), f"doctor left something other than a SQLite sidecar behind: {appeared}"
        # ...and it still answered the question it was asked.
        ladder = [r for r in results if r.check_id == "schema_ladder"]
        assert ladder and ladder[0].severity is Severity.OK


def test_reading_a_database_a_writer_is_using_gives_the_real_answer(tmp_path, registry):
    """Doctor runs while the daemon is up — that is the normal case, not the edge.

    A busy database must still produce the migration answer that was asked for,
    not an error about the database.

    **This does not by itself rule out ``immutable=1``.** That failure is a race
    against WAL checkpointing which needs sustained volume to surface (measured
    at 6.6% of opens against a hammering writer on the reviewer's machine; it did
    not reproduce here at any stress level cheap enough to put in this suite).
    The connection mode is pinned separately, by
    ``test_the_read_only_connection_takes_a_lock``, which says so in as many
    words rather than letting this test look like the guard.
    """
    state_file = tmp_path / "state.db"
    initialize_database(state_file)
    stop = threading.Event()

    def keep_writing():
        while not stop.is_set():
            with write_txn(state_file) as connection:
                set_metadata(connection, "doctor_read_stress", now_iso())

    writer = threading.Thread(target=keep_writing, daemon=True)
    writer.start()
    try:
        verdicts = [
            [
                r
                for r in run_doctor(
                    {"state_file": str(state_file)}, registry, PRODUCTION_VERSION
                )
                if r.check_id == "schema_ladder"
            ][0]
            for _ in range(50)
        ]
    finally:
        stop.set()
        writer.join(timeout=5)

    wrong = [verdict for verdict in verdicts if verdict.severity is not Severity.OK]
    assert not wrong, (
        f"doctor misread a database that was merely busy: {wrong[0].detail}"
    )


def test_the_read_only_connection_takes_a_lock(tmp_path, monkeypatch):
    """``mode=ro``, never ``immutable=1`` — a **pin**, not a behavioural witness.

    Both refuse writes, so nothing observable in a single-threaded test separates
    them. The difference is that ``immutable=1`` takes no lock, so SQLite's
    *automatic* WAL checkpoint firing inside a writer's COMMIT can hand an
    unlocked reader torn pages: measured at 1086/17672 opens (6.15%) with
    auto-checkpoint on, and 0/18878 with it off, against 0/15435 for ``mode=ro``.
    Doctor renders that as "not a readable sluice database — run `sluice setup`",
    which sends an operator to restore a backup they do not need.

    Reproducing it needs a writer whose *automatic* checkpoint fires — an
    explicit ``wal_checkpoint`` from the test thread keeps the WAL under the
    threshold and structurally cannot trigger the race — so this pins the
    decision rather than pretending to demonstrate it. If a cheap deterministic
    reproduction is ever built, replace this with it.

    Asserted against the URI the function actually hands to ``sqlite3.connect``,
    not against its source text: the docstring above names ``mode=ro`` twice, so
    a source-substring check would be satisfied by a body that builds no URI at
    all. The last assertion is the half that *is* directly observable — the
    connection genuinely cannot write.
    """
    state_file = tmp_path / "state.db"
    initialize_database(state_file)
    opened_with = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened_with.append(target)
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(doctor.sqlite3, "connect", spy)
    connection = doctor._connect_read_only(state_file)
    connection.close()
    monkeypatch.undo()

    assert opened_with, "_connect_read_only opened nothing"
    uri = opened_with[-1]
    assert "mode=ro" in uri, f"the read-only connection was opened as {uri!r}"
    assert "immutable" not in uri, (
        f"the read-only connection must not use immutable=1 — it takes no lock "
        f"and misreads a database the daemon is checkpointing: {uri!r}"
    )

    connection = doctor._connect_read_only(state_file)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE scribble (x INTEGER)")
    finally:
        connection.close()


def test_a_state_file_that_is_not_a_database_names_the_setting(tmp_path, registry):
    """A typo'd state_file is a config mistake, and must not read as a doctor bug.

    Pointing state_file at a directory or a text file is an ordinary deployment
    error. Without this the generic runner catch-all reports "the check itself
    failed: DatabaseError", which tells an operator at 3am that the diagnostic is
    broken when in fact their config is.
    """
    not_a_database = tmp_path / "notes.txt"
    not_a_database.write_text("this is not a database", encoding="utf-8")

    results = run_doctor(
        {"state_file": str(not_a_database)}, registry, PRODUCTION_VERSION
    )

    ladder = [r for r in results if r.check_id == "schema_ladder"]
    assert ladder and ladder[0].severity is Severity.FAIL
    assert "state_file" in ladder[0].detail
    assert "the check itself failed" not in ladder[0].detail


def test_a_launch_base_inside_the_staging_tree_fails(tmp_path, registry):
    """The errata D2 misconfiguration, caught before the terabytes land.

    Both paths exist and each key passes its own check, so only a cross-field
    check can see it. At runtime the nvd gate refuses — after the download, at
    the last and most expensive gate — and on shared storage ``--resume``
    silently does nothing, so an interrupted run restarts from zero.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    inside = staging / "nvd-launch"
    inside.mkdir()

    results = run_doctor(
        {"local_staging_dir": str(staging), "nvd_launch_base": str(inside)},
        registry,
        PRODUCTION_VERSION,
    )
    launch = [r for r in results if r.check_id == "nvd_launch_base_is_local"]

    assert launch and launch[0].severity is Severity.FAIL
    assert "local_staging_dir" in launch[0].detail

    # Two placeholders are not "outside each other" — a confident `ok` there
    # would certify a config nobody has filled in.
    unfilled = run_doctor(
        {
            "local_staging_dir": "<local_staging_dir>",
            "nvd_launch_base": "<nvd_launch_base>",
        },
        registry,
        PRODUCTION_VERSION,
    )
    assert [r for r in unfilled if r.check_id == "nvd_launch_base_is_local"][
        0
    ].severity is Severity.SKIP

    outside = tmp_path / "nvd-launch"
    outside.mkdir()
    healthy = run_doctor(
        {"local_staging_dir": str(staging), "nvd_launch_base": str(outside)},
        registry,
        PRODUCTION_VERSION,
    )
    assert [r for r in healthy if r.check_id == "nvd_launch_base_is_local"][
        0
    ].severity is Severity.OK


def test_a_profiling_launch_base_inside_the_staging_tree_fails(tmp_path, registry):
    """The same D2 cross-field rule, for the profiling gate's launch base.

    Worse here than for NVD in one way: the site config's relative
    ``submitFileDir`` lands inside the launch dir, and HTCondor cannot submit
    from the staging tree at all.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    inside = staging / "profiling-launch"
    inside.mkdir()

    results = run_doctor(
        {"local_staging_dir": str(staging), "profiling_launch_base": str(inside)},
        registry,
        PRODUCTION_VERSION,
    )
    launch = [r for r in results if r.check_id == "profiling_launch_base_is_local"]

    assert launch and launch[0].severity is Severity.FAIL
    assert "local_staging_dir" in launch[0].detail

    unfilled = run_doctor(
        {
            "local_staging_dir": "<local_staging_dir>",
            "profiling_launch_base": "<profiling_launch_base>",
        },
        registry,
        PRODUCTION_VERSION,
    )
    assert [r for r in unfilled if r.check_id == "profiling_launch_base_is_local"][
        0
    ].severity is Severity.SKIP

    outside = tmp_path / "profiling-launch"
    outside.mkdir()
    healthy = run_doctor(
        {"local_staging_dir": str(staging), "profiling_launch_base": str(outside)},
        registry,
        PRODUCTION_VERSION,
    )
    assert [r for r in healthy if r.check_id == "profiling_launch_base_is_local"][
        0
    ].severity is Severity.OK
