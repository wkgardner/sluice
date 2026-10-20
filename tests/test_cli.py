"""Behavioral tests for the ``sluice`` CLI and the config loader (plan 15 increment 4).

Every test drives ``main(argv)`` against a real database built through the real migrations and a
real ``config.toml`` under ``tmp_path``, then reads captured stdout. Nothing is mocked: the CLI is
a thin layer, and testing it through anything but its actual entry point would test the wrong
thing.
"""

import getpass
import hashlib
import json
import socket

import pytest

from sluice.cli import main
from sluice.config import (
    CONFIG_ENV_VAR,
    HOME_ENV_VAR,
    ConfigError,
    load_config,
    parse_config_text,
    resolve_config_path,
)
from sluice.config.setup import plan_setup, render_config_toml
from sluice.config.schema import CONFIG_FIELDS
from sluice.core.daemon import assemble_registry, record_daemon_host
from sluice.core.db import initialize_database, insert_run, update_run, write_txn
from sluice.core.profile import REFERENCE_PROFILES
from sluice.doctor import Severity, run_doctor
from sluice.gates.profiles import PRODUCTION_PROFILES, PRODUCTION_VERSION


@pytest.fixture
def deployment(tmp_path):
    """A config file plus an initialized database, as a real install would have."""
    state_file = tmp_path / "sluice_state.db"
    initialize_database(state_file)
    config_path = _write_config(
        tmp_path / "config.toml",
        state_file=str(state_file),
        local_staging_dir=str(tmp_path / "staging"),
    )
    return types_namespace(config_path=config_path, state_file=state_file)


def types_namespace(**kwargs):
    """A tiny attribute bag — clearer at call sites than a dict of two paths."""
    import types

    return types.SimpleNamespace(**kwargs)


def add_run(state_file, name, **columns):
    """Insert a run and apply any extra column values."""
    with write_txn(state_file) as connection:
        insert_run(connection, name, pipeline_version=PRODUCTION_VERSION)
        if columns:
            update_run(connection, name, columns)


# ---------------------------------------------------------------------------
# The config loader.
# ---------------------------------------------------------------------------


def test_config_precedence_is_flag_then_env_then_cwd_then_home(tmp_path, monkeypatch):
    """``--config`` > ``$SLUICE_CONFIG`` > ``./config.toml`` > ``<home>/config.toml``."""
    import pathlib

    monkeypatch.setenv(CONFIG_ENV_VAR, "/from/env.toml")
    assert resolve_config_path("/explicit.toml") == pathlib.Path("/explicit.toml")
    assert str(resolve_config_path(None)) == "/from/env.toml"

    monkeypatch.delenv(CONFIG_ENV_VAR)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV_VAR, str(home))

    working = tmp_path / "cwd"
    working.mkdir()
    monkeypatch.chdir(working)

    # Nothing in the working directory: fall through to the sluice home.
    assert resolve_config_path(None) == home / "config.toml"

    # A config that is really there in the working directory still wins, which is
    # what keeps a repo checkout usable without --config.
    (working / "config.toml").write_text("", encoding="utf-8")
    assert str(resolve_config_path(None)) == "config.toml"

    # A pre-TOML install is still found, so upgrading sluice does not break
    # every command before the operator reaches `sluice setup`.
    (working / "config.toml").unlink()
    (working / "config.json").write_text("{}", encoding="utf-8")
    assert str(resolve_config_path(None)) == "config.json"


def test_the_config_setup_writes_is_the_config_commands_read(tmp_path, monkeypatch):
    """Setup's output path and the resolver's default must be the same file.

    They were not, and it made a correct install unusable: `sluice setup` wrote
    `~/.sluice/config.toml`, every command looked for `./config.toml`, and setup
    then told the operator to run a command that could not find what it had just
    written. Pinned as one assertion so the writer and the reader cannot drift
    apart again.
    """
    home = tmp_path / "home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    plan = plan_setup()

    assert plan.config_path == resolve_config_path(None)


def test_a_missing_config_names_the_path_it_tried(tmp_path):
    """The error says WHICH path was missing, and how to point at another.

    Loading a different config than intended is the most common real config
    failure, so "config not found" is not good enough — the path has to be in the
    message.
    """
    with pytest.raises(ConfigError) as raised:
        load_config(str(tmp_path / "absent.json"))

    assert "absent.json" in str(raised.value)
    assert CONFIG_ENV_VAR in str(raised.value)


def test_invalid_config_text_is_reported_as_a_config_error(tmp_path):
    """A malformed config fails as ConfigError, not a raw parser exception."""
    bad = tmp_path / "config.toml"
    bad.write_text("not [ toml", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(str(bad))


# ---------------------------------------------------------------------------
# status / show.
# ---------------------------------------------------------------------------


def test_status_on_an_empty_deployment_says_so(deployment, capsys):
    """An empty board reports zero runs rather than printing nothing.

    Silence is indistinguishable from a broken command; the operator needs to know
    the query worked and there is simply nothing there.
    """
    exit_code = main(["--config", str(deployment.config_path), "status"])

    output = capsys.readouterr().out.lower()
    assert exit_code == 0
    assert "no runs detected yet" in output


def test_status_lists_runs_with_their_experiment_id(deployment, capsys):
    """Each run line carries the experiment id — the key a human actually uses.

    ``runs.name`` is the machine key; the experiment number is what appears in
    LabKey and names the directory on disk, so a board without it forces the
    operator to translate.
    """
    add_run(deployment.state_file, "SEQ_001", experiment_id=4471)

    main(["--config", str(deployment.config_path), "status"])

    output = capsys.readouterr().out
    assert "SEQ_001" in output
    assert "4471" in output


def test_status_surfaces_quarantined_runs_with_the_next_action(deployment, capsys):
    """A quarantined run shows its reason AND what to do about it.

    A quarantined run waits forever by design — there is no automatic retry — so
    the board has to say so explicitly rather than leaving it to be noticed.
    """
    add_run(
        deployment.state_file,
        "SEQ_002",
        errored_at="2026-08-02T10:00:00+00:00",
        error_reason="HASH_MISMATCH: 2 of 8 files corrupt",
    )

    main(["--config", str(deployment.config_path), "status"])

    output = capsys.readouterr().out
    assert "HASH_MISMATCH" in output
    assert "1 run(s) quarantined" in output
    assert "sluice clear-error" in output


def test_show_reports_gate_progress(deployment, capsys):
    """``show`` splits the contract into satisfied and pending gates.

    "Which gate is it stuck on" is the first question anyone asks about a run that
    is not finishing, and the answer must not require reading the database.
    """
    add_run(deployment.state_file, "SEQ_003", experiment_id=4471, labkey_registered=1)

    exit_code = main(["--config", str(deployment.config_path), "show", "SEQ_003"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "gates done" in output and "experiment" in output
    assert "gates pending" in output and "download" in output


def test_json_output_is_machine_readable(deployment, capsys):
    """``--json`` emits parseable JSON, for scripting and for the future API."""
    add_run(deployment.state_file, "SEQ_004", experiment_id=4471)

    main(["--config", str(deployment.config_path), "--json", "status"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["runs"][0]["name"] == "SEQ_004"


# ---------------------------------------------------------------------------
# State-changing ops.
# ---------------------------------------------------------------------------


def test_hold_then_unhold_round_trips(deployment, capsys):
    """``hold`` marks the run held and ``unhold`` returns it to active."""
    add_run(deployment.state_file, "SEQ_005")
    config = ["--config", str(deployment.config_path)]

    assert main([*config, "hold", "SEQ_005", "--reason", "waiting on lab"]) == 0
    main([*config, "show", "SEQ_005"])
    assert "held" in capsys.readouterr().out

    assert main([*config, "unhold", "SEQ_005"]) == 0
    main([*config, "show", "SEQ_005"])
    assert "active" in capsys.readouterr().out


def test_clear_error_makes_a_quarantined_run_eligible_again(deployment, capsys):
    """``clear-error`` is the recovery path: the error clears and the run is active."""
    add_run(
        deployment.state_file,
        "SEQ_006",
        errored_at="2026-08-02T10:00:00+00:00",
        error_reason="transfer failed",
    )
    config = ["--config", str(deployment.config_path)]

    exit_code = main([*config, "clear-error", "SEQ_006", "--reason", "server was down"])

    assert exit_code == 0
    main([*config, "show", "SEQ_006"])
    assert "transfer failed" not in capsys.readouterr().out


def test_reset_gate_rewinds_a_held_run_so_the_gate_runs_again(deployment, capsys):
    """``reset-gate`` clears one gate on a held run, so the daemon re-runs it.

    The motivating case: an NVD run that Nextflow called OK while every task's
    error was ignored. The gate looks done, so there is no quarantine to clear —
    rewinding the gate is how it runs again.
    """
    add_run(
        deployment.state_file,
        "SEQ_020",
        experiment_id=4471,
        labkey_registered=1,
        downloaded=1,
        checksummed=1,
        samplesheet_created=1,
        nvd_completed=1,
        completed_at="2026-10-19T09:35:22+00:00",
    )
    config = ["--config", str(deployment.config_path)]
    main([*config, "hold", "SEQ_020"])
    capsys.readouterr()

    exit_code = main(
        [*config, "reset-gate", "SEQ_020", "nvd", "--reason", "jq missing in image"]
    )

    assert exit_code == 0
    capsys.readouterr()  # Drop reset-gate's own output before reading the JSON.
    main([*config, "--json", "show", "SEQ_020"])
    shown = json.loads(capsys.readouterr().out)
    assert "nvd" in shown["gates"]["unsatisfied"]
    assert "samplesheet" in shown["gates"]["satisfied"]


def test_reset_gate_refuses_a_run_that_is_not_held(deployment, capsys):
    """An active run is refused: a gate in flight would re-stamp the column at once."""
    add_run(deployment.state_file, "SEQ_021", experiment_id=4471, labkey_registered=1)

    exit_code = main(
        ["--config", str(deployment.config_path), "reset-gate", "SEQ_021", "experiment"]
    )

    assert exit_code == 1
    assert "hold" in capsys.readouterr().err


def test_reset_gate_names_an_unknown_gate_plainly(deployment, capsys):
    """A mistyped gate is reported as an unknown gate, not as a config problem."""
    add_run(deployment.state_file, "SEQ_022", lifecycle="held")

    exit_code = main(
        ["--config", str(deployment.config_path), "reset-gate", "SEQ_022", "nvdd"]
    )

    error_output = capsys.readouterr().err
    assert exit_code == 1
    assert "nvdd" in error_output
    assert "config is missing" not in error_output


def test_every_state_change_reports_its_audit_id(deployment, capsys):
    """Each op prints the audit row id it wrote.

    The audit trail is the only record of who did what; showing the id makes it
    findable later without digging through the table.
    """
    add_run(deployment.state_file, "SEQ_007")

    main(["--config", str(deployment.config_path), "hold", "SEQ_007"])

    assert "audited as #" in capsys.readouterr().out


def test_the_actor_is_the_os_user_and_cannot_be_chosen(deployment, capsys):
    """The audit row names whoever is logged in, and nothing can override it.

    ``--actor`` used to accept any string. That was harmless while one person
    could write to the database and became fake evidence the moment several
    could: a trail recording who *claimed* to act is worse than no trail, since
    it reads as authoritative. The OS user is the one identity the CLI does not
    have to take anyone's word for.
    """
    add_run(deployment.state_file, "SEQ_008")

    main(["--config", str(deployment.config_path), "--json", "hold", "SEQ_008"])

    assert json.loads(capsys.readouterr().out)["actor"] == getpass.getuser()


def test_the_actor_flag_is_gone_rather_than_quietly_ignored(deployment, capsys):
    """``--actor`` is a usage error now, not a silently discarded option.

    Accepting and ignoring it would let an existing script keep running while
    recording something other than what it names — the failure mode this change
    exists to remove, reintroduced as a surprise.
    """
    exit_code = main(
        ["--config", str(deployment.config_path), "--actor", "someone", "hold", "X"]
    )

    assert exit_code == 2


def test_an_unknown_run_exits_nonzero_with_a_clear_message(deployment, capsys):
    """A typo'd run name is exit 1 and a sentence, never a traceback."""
    exit_code = main(["--config", str(deployment.config_path), "hold", "NOPE"])

    assert exit_code == 1
    assert "NOPE" in capsys.readouterr().err


def test_a_missing_config_exits_nonzero_without_a_traceback(tmp_path, capsys):
    """A bad --config path is a clear message and exit 1."""
    exit_code = main(["--config", str(tmp_path / "nope.json"), "status"])

    assert exit_code == 1
    assert "no config file at" in capsys.readouterr().err


def test_pause_and_resume_toggle_the_global_flag(deployment, capsys):
    """``pause`` stops everything; ``status`` says so loudly; ``resume`` clears it."""
    config = ["--config", str(deployment.config_path)]

    assert main([*config, "pause", "--reason", "disk full"]) == 0
    main([*config, "status"])
    assert "PAUSED" in capsys.readouterr().out

    assert main([*config, "resume"]) == 0
    main([*config, "status"])
    resumed_output = capsys.readouterr().out
    assert "PAUSED" not in resumed_output


def test_set_default_profile_changes_what_new_runs_are_stamped_with(deployment, capsys):
    """``set-default-profile`` switches the active default, visible on ``status``.

    The rollout op for a new gate: shipped profiles are frozen, so a gate joins a
    new version, and this is how the live DB starts stamping it on NEW runs.
    Existing runs keep their stamped version (stamp-once).
    """
    config = ["--config", str(deployment.config_path)]

    # No runs exist, so the only place a version can appear is the default.
    assert main([*config, "set-default-profile", "production-v3.1"]) == 0
    capsys.readouterr()
    main([*config, "status"])
    output = capsys.readouterr().out
    assert "production-v3.1" in output and "production-v3.2" not in output

    assert main([*config, "set-default-profile", "production-v3.2"]) == 0
    capsys.readouterr()
    main([*config, "status"])
    output = capsys.readouterr().out
    assert "production-v3.2" in output and "production-v3.1" not in output


def test_set_default_profile_refuses_an_unknown_version(deployment, capsys):
    """A typo'd version is refused with one clear line, never stored.

    An unresolvable default would quarantine every newly detected run, so it
    must never become the default.
    """
    config = ["--config", str(deployment.config_path)]

    exit_code = main([*config, "set-default-profile", "production-v9.9"])

    assert exit_code == 1
    assert "production-v9.9" in capsys.readouterr().err


def test_status_says_so_when_no_daemon_is_running(deployment, capsys):
    """A dead daemon is reported as dead, and the fix is on the same line.

    Nothing supervises the daemon (errata E1), so after a reboot it is simply
    not there — and this line is the only place an operator would find that
    out. It used to print "running" off the pause flag, which is a different
    question entirely, and would have sent someone hunting anywhere except the
    real cause.

    No daemon runs during tests, so the honest answer here is NOT RUNNING.
    """
    main(["--config", str(deployment.config_path), "status"])

    output = capsys.readouterr().out
    assert "NOT RUNNING" in output
    assert "sluice daemon" in output, "the report must say how to fix it"


def test_status_reports_liveness_and_last_look_separately(deployment, capsys):
    """A live daemon mid-gate must not read as a dead one.

    Liveness comes from the lock, progress from the heartbeat. A gate can run
    for hours with no time bound (⚑F), so an old heartbeat on a live daemon is
    normal work — reporting that as a fault is the cry-wolf failure that trains
    operators to ignore the line.
    """
    from sluice.core.daemon import DAEMON_LAST_LOOK_KEY, acquire_daemon_lock
    from sluice.core.db import set_metadata, write_txn

    with write_txn(deployment.state_file) as connection:
        set_metadata(connection, DAEMON_LAST_LOOK_KEY, "2026-08-03T01:00:00+00:00")

    # Hold the lock exactly as a running daemon would.
    handle = acquire_daemon_lock(str(deployment.state_file) + ".lock")
    try:
        main(["--config", str(deployment.config_path), "status"])
    finally:
        handle.close()

    output = capsys.readouterr().out
    assert "NOT RUNNING" not in output
    assert "running" in output and "2026-08-03T01:00:00+00:00" in output


def test_help_explains_that_commands_are_state_only(capsys):
    """Every state-changing command's --help states the reconcile-loop model.

    The single most important thing for an operator to understand: nothing here
    executes anything, the daemon acts on its next look. Getting this wrong looks
    like "the command did nothing".
    """
    exit_code = main(["hold", "--help"])

    # Rich wraps help text, so compare on collapsed whitespace rather than
    # depending on where the terminal width happens to break the line.
    rendered = " ".join(capsys.readouterr().out.split())
    assert exit_code == 0
    assert "daemon acts on its next look" in rendered


def test_main_restores_global_logging_configuration(deployment):
    """``main`` leaves structlog's global config exactly as it found it.

    The CLI routes logs to stderr so ``--json`` output stays parseable, but
    ``structlog.configure`` is PROCESS-GLOBAL. A main() that configured and walked
    away would silently change logging for everything else in the process — the
    daemon, another library, the rest of a test session. This was not hypothetical:
    the first version of this broke 42 unrelated tests that ran after it.
    """
    import structlog

    before = structlog.get_config().copy()

    main(["--config", str(deployment.config_path), "status"])

    after = structlog.get_config()
    assert after["logger_factory"] is before["logger_factory"], (
        "main() changed the process-wide structlog logger factory and did not "
        "restore it — anything running later in this process now logs somewhere "
        "it did not choose."
    )


def test_json_output_is_not_polluted_by_log_lines(deployment, capsys):
    """``--json`` stdout parses cleanly even though the op emits a log event.

    The reason logs go to stderr at all. Control ops emit a canonical
    ``control.finished`` event; if that lands on stdout, ``sluice --json ... | jq``
    fails on interleaved text.
    """
    add_run(deployment.state_file, "SEQ_009")

    main(["--config", str(deployment.config_path), "--json", "hold", "SEQ_009"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "hold"


def test_every_outcome_the_system_can_report_has_a_style():
    """The colour map covers exactly the vocabulary ``run_outcome`` produces.

    A near-miss key ("complete" for "completed") falls through to a default and
    the colour silently stops carrying meaning — which is worse than no colour,
    because the operator has learned to trust it. This was a real bug: the first
    version keyed on "complete" and completed runs rendered uncoloured.
    """
    import inspect

    from sluice.cli import _OUTCOME_STYLES
    from sluice.core import events

    produced = {
        line.split('return "')[1].split('"')[0]
        for line in inspect.getsource(events.run_outcome).splitlines()
        if 'return "' in line
    }

    assert produced <= set(_OUTCOME_STYLES), (
        f"run_outcome can return {sorted(produced - set(_OUTCOME_STYLES))}, which "
        "the CLI has no style for — those runs would render uncoloured."
    )


# ---------------------------------------------------------------------------
# doctor.
# ---------------------------------------------------------------------------


def _write_config(path, **values):
    """A config file with exactly the keys a test cares about.

    Written through the real emitter rather than a hand-rolled TOML string, so a
    test config is the same shape as one ``sluice setup`` produces — including
    for keys the schema does not declare, which ride along as unrecognised.
    """
    known = {field.name for field in CONFIG_FIELDS}
    unknown = tuple(key for key in values if key not in known)
    path.write_text(render_config_toml(values, unknown), encoding="utf-8")
    return path


def _read_config(path) -> dict:
    """The config file as flat keys, the way every sluice command reads it."""
    return parse_config_text(path.read_text(encoding="utf-8"), str(path))


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


def test_doctor_names_the_setting_to_change_for_every_failure(deployment):
    """A failure an operator cannot act on is noise.

    Asserted over the results rather than the rendered table: the table prints
    ``check_id`` in its own column, so any assertion made against the output is
    satisfied by the table existing, whatever the detail says. Every failure
    whose check *is* a config key must repeat that key in its detail, because
    the detail is the sentence an operator reads and acts on.
    """
    config = load_config(str(deployment.config_path))
    registry = assemble_registry(REFERENCE_PROFILES, PRODUCTION_PROFILES)
    config_keys = {field.name for field in CONFIG_FIELDS}

    failures = [
        result
        for result in run_doctor(config, registry, PRODUCTION_VERSION)
        if result.severity is Severity.FAIL and result.check_id in config_keys
    ]

    assert failures, "the half-configured fixture produced no config failures"
    unnamed = [
        result.check_id for result in failures if result.check_id not in result.detail
    ]
    assert not unnamed, f"failures that never name the setting to change: {unnamed}"


def test_doctor_exits_zero_on_warnings_only(tmp_path, capsys, monkeypatch):
    """A degraded-but-working install must not break a deployment script.

    WARN means "this works and should be fixed". Treating it as failure would
    train operators to ignore the exit code, which costs more than the warning
    is worth. Here every required key is satisfied and the only complaint is a
    managed directory that setup would recreate.
    """
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    (launcher_dir / "pixi").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (launcher_dir / "pixi").chmod(0o755)
    monkeypatch.setenv("PATH", str(launcher_dir))

    state_file = tmp_path / "state.db"
    initialize_database(state_file)
    secret = tmp_path / "secret"
    secret.write_text("value", encoding="utf-8")
    secret.chmod(0o600)
    repo = tmp_path / "nvd-repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    launch_base = tmp_path / "launch"
    launch_base.mkdir()

    # Everything `sluice setup` writes from the schema defaults. Listing them by
    # hand would rot the day a tuning key is added, and their absence is a real
    # FAIL — the daemon and the gates read them by subscript, so a config
    # missing one raises KeyError mid-run.
    setup_defaults = {
        field.name: field.default
        for field in CONFIG_FIELDS
        if field.default is not None
    }

    operator_supplied = dict(
        state_file=str(state_file),
        events_file=str(tmp_path / "events.jsonl"),
        run_events_dir=str(tmp_path / "absent-but-managed"),
        local_source_dir=str(tmp_path),
        local_staging_dir=str(staging),
        disk_quota_root=str(tmp_path),
        labkey_server="https://labkey.example",
        labkey_experiments_project="Proj",
        labkey_query="Experiments",
        labkey_api_key_file=str(secret),
        nvd_repo_dir=str(repo),
        nvd_launch_base=str(launch_base),
        nvd_log_dir=str(tmp_path),
        nvd_results_base=str(tmp_path),
        nvd_blast_db=str(tmp_path),
        nvd_taxonomy_dir=str(tmp_path),
        nvd_virus_index=str(tmp_path),
        # The three reference-data keys with no default: only a human can
        # supply them, and nothing but doctor ever asks.
        nvd_blast_db_prefix="nt",
        nvd_blast_db_version="2024-01",
        nvd_virus_index_version="1.4",
        # NVD's own upload targets. No default is possible for any of them: a
        # baked-in list name would be a production value in the source, and the
        # one it would be is the live results list.
        nvd_labkey_server="https://labkey.example",
        nvd_labkey_project_name="Proj/Uploads",
        nvd_labkey_blast_fasta_list="example_fasta_list",
        nvd_labkey_blast_meta_hits_list="example_meta_list",
        nvd_labkey_exp_id_guard_list="example_guard_list",
        # The profiling gate's operator-owned files. Existing paths are enough
        # for doctor: it checks they exist, never what is inside them.
        profiling_repo_dir=str(repo),
        profiling_params_file=str(secret),
        profiling_site_config=str(secret),
        profiling_results_base=str(tmp_path),
        profiling_launch_base=str(launch_base),
        profiling_log_dir=str(tmp_path),
        slack_enabled=False,
    )

    config_path = _write_config(
        tmp_path / "config.toml", **setup_defaults | operator_supplied
    )

    exit_code = main(["--config", str(config_path), "doctor"])

    output = capsys.readouterr().out
    assert exit_code == 0, output
    assert "warn" in output.lower()


def test_doctor_with_no_config_at_all_points_at_setup(tmp_path, capsys):
    """The first command a new operator runs must not be a dead end.

    The shared loader message offers ``--config``, ``$SLUICE_CONFIG`` and a cwd
    ``config.toml`` — all three of which presuppose a config that exists. On a
    machine where sluice has never been installed the answer is `sluice setup`,
    and doctor is where that operator will be standing.
    """
    exit_code = main(["--config", str(tmp_path / "nope.json"), "doctor"])

    error_output = capsys.readouterr().err
    assert exit_code == 1
    assert "no config file at" in error_output
    assert "sluice setup" in error_output


def test_doctor_json_output_is_parseable(deployment, capsys):
    """`sluice --json doctor | jq` — a list of result objects, nothing else."""
    main(["--json", "--config", str(deployment.config_path), "doctor"])

    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert {"check", "area", "severity", "detail"} <= set(payload[0])
    assert any(entry["severity"] == "fail" for entry in payload)


# SQLite's own sidecars beside a WAL database. Opening one read-only creates
# them and a read-only connection cannot remove them again; they carry no sluice
# data, and the daemon creates and destroys them on every write. The alternative
# — an unlocked `immutable=1` read — misreads a database the daemon is
# checkpointing and reports it as corrupt, which is a far worse thing to say.
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _tree_fingerprint(root):
    """Every path under ``root``, with its mode and its content hash.

    Recursive and content-sensitive on purpose: comparing top-level *names*
    would miss a file written into a subdirectory, and would miss a database
    rewritten in place — ``PRAGMA journal_mode`` is persisted in the SQLite file
    header, so a diagnostic that "only reads" through the ordinary connection
    factory changes the bytes without changing any name.
    """
    fingerprint = {}
    for path in sorted(root.rglob("*")):
        if path.name.endswith(_SQLITE_SIDECAR_SUFFIXES):
            continue
        stat_result = path.lstat()
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
        )
        fingerprint[str(path.relative_to(root))] = (stat_result.st_mode, digest)
    return fingerprint


def test_doctor_changes_nothing_on_disk(deployment, tmp_path, capsys):
    """The never-mutates promise, asserted rather than trusted.

    Doctor is run when things are already broken. A diagnostic that creates a
    directory or a database while looking would make the next run's report a
    lie about the state it inherited.
    """
    before = _tree_fingerprint(tmp_path)

    main(["--config", str(deployment.config_path), "doctor"])

    assert _tree_fingerprint(tmp_path) == before


# ---------------------------------------------------------------------------
# setup — overwrite protection.
# ---------------------------------------------------------------------------


def test_a_fresh_install_does_not_prompt(tmp_path, capsys):
    """Nothing exists yet, so there is nothing to lose and nothing to ask about."""
    home = tmp_path / "h"

    exit_code = main(["setup", "--home", str(home)])

    assert exit_code == 0
    assert (home / "config.toml").is_file()


def test_rewriting_an_existing_config_refuses_without_a_terminal(tmp_path, capsys):
    """Non-interactive re-runs refuse rather than rewriting unasked.

    A script that re-runs setup must not silently reformat an operator's config.
    Refusing is recoverable in a way that a surprise rewrite is not, and the
    message names the flag that opts in.
    """
    home = tmp_path / "h"
    main(["setup", "--home", str(home)])
    config_path = home / "config.toml"
    edited = _read_config(config_path)
    edited["remote_host"] = "seq.example.edu"
    _write_config(config_path, **edited)
    before = config_path.read_bytes()

    exit_code = main(["setup", "--home", str(home)])

    assert exit_code == 1
    assert config_path.read_bytes() == before, "the config was rewritten anyway"
    assert "--yes" in capsys.readouterr().err


def test_yes_rewrites_and_keeps_a_backup_of_what_was_there(tmp_path, capsys):
    """--yes proceeds, and the backup holds the OLD file, not a copy of the new one.

    The backup is the real protection: values survive a rewrite either way, but
    the file is regenerated from the schema, and an editor holding it open is a
    live way to lose work. A backup taken after writing would be worthless.
    """
    home = tmp_path / "h"
    main(["setup", "--home", str(home)])
    config_path = home / "config.toml"
    edited = _read_config(config_path)
    edited["remote_host"] = "seq.example.edu"
    _write_config(config_path, **edited)
    before = config_path.read_bytes()

    exit_code = main(["setup", "--home", str(home), "--yes"])

    assert exit_code == 0
    backup = home / "config.toml.bak"
    assert backup.is_file()
    assert backup.read_bytes() == before, "the backup is not the pre-write file"
    # And the hand-set value survived the rewrite, as it always must.
    assert _read_config(config_path)["remote_host"] == "seq.example.edu"


def test_doctor_puts_failures_at_the_bottom(deployment, capsys):
    """Failures finish the table, next to the summary the operator reads.

    A full config runs to forty-odd rows. In run order the failures scatter
    through them and scroll off the top, leaving the reader to hunt upward for
    the only rows they have to act on.
    """
    main(["--config", str(deployment.config_path), "doctor"])

    lines = [line for line in capsys.readouterr().out.splitlines() if "│" in line]
    severity_rows = [
        line for line in lines if any(s in line for s in ("ok", "warn", "FAIL", "skip"))
    ]
    last_ok = max(
        (i for i, line in enumerate(severity_rows) if " ok " in line), default=-1
    )
    first_fail = min(
        (i for i, line in enumerate(severity_rows) if "FAIL" in line),
        default=len(severity_rows),
    )

    assert first_fail > last_ok, "a passing check was rendered below a failure"


# ---------------------------------------------------------------------------
# The cross-host guard.
# ---------------------------------------------------------------------------


def test_a_command_from_the_wrong_host_is_refused(deployment, capsys, monkeypatch):
    """Every DB-touching command refuses from a node the daemon does not own.

    The state file lives on shared storage on a cluster, so a colleague running
    this from another login node would be a second NFS client on a WAL database
    — silent corruption, taking the run history with it (design §5). The refusal
    names the host to go to, because "wrong host" without "which host" leaves
    someone guessing across a dozen login nodes.
    """
    record_daemon_host(deployment.state_file, "submit1")
    monkeypatch.setattr(socket, "gethostname", lambda: "submit2")

    exit_code = main(["--config", str(deployment.config_path), "status"])

    assert exit_code == 1
    error_output = capsys.readouterr().err
    assert "submit1" in error_output


def test_a_control_write_from_the_wrong_host_is_refused(
    deployment, capsys, monkeypatch
):
    """The guard covers writes, not just reads — the case that corrupts."""
    add_run(deployment.state_file, "SEQ_001")
    record_daemon_host(deployment.state_file, "submit1")
    monkeypatch.setattr(socket, "gethostname", lambda: "submit2")

    exit_code = main(["--config", str(deployment.config_path), "hold", "SEQ_001"])

    assert exit_code == 1
    assert "submit1" in capsys.readouterr().err


def test_commands_work_normally_on_the_claimed_host(deployment, capsys, monkeypatch):
    """The ordinary case is untouched: right host, no interference."""
    record_daemon_host(deployment.state_file, "submit1")
    monkeypatch.setattr(socket, "gethostname", lambda: "submit1")

    exit_code = main(["--config", str(deployment.config_path), "status"])

    assert exit_code == 0


def test_an_unclaimed_database_is_usable_from_anywhere(deployment, capsys, monkeypatch):
    """Before any daemon has run, nothing is pinned and setup must still work."""
    monkeypatch.setattr(socket, "gethostname", lambda: "whatever")

    assert main(["--config", str(deployment.config_path), "status"]) == 0


def test_daemon_configures_file_logging_before_looping(
    deployment, tmp_path, monkeypatch
):
    """``sluice daemon`` must wire the file sinks before the loop starts.

    ``configure_logging`` was built, tested, and exported — and called by
    nobody, so every daemon so far logged to console only and a 4-hour
    checksum ran with zero visible progress. The behavioral pin: after the
    daemon command runs, the rotating events file and per-run directory exist.
    """
    events_file = tmp_path / "logs" / "events.jsonl"
    run_events_dir = tmp_path / "logs" / "runs"
    config_path = _write_config(
        tmp_path / "logging_config.toml",
        state_file=str(deployment.state_file),
        local_staging_dir=str(tmp_path / "staging"),
        events_file=str(events_file),
        run_events_dir=str(run_events_dir),
        events_max_mb=1,
        events_backup_count=1,
    )
    monkeypatch.setattr("sluice.cli.run_forever", lambda *args, **kwargs: None)

    assert main(["--config", str(config_path), "daemon", "--once"]) == 0
    assert events_file.exists(), "the rotating events sink was never created"
    assert run_events_dir.is_dir()
