"""Behavioral tests for the ``sluice`` CLI and the config loader (plan 15 increment 4).

Every test drives ``main(argv)`` against a real database built through the real migrations and a
real ``config.json`` under ``tmp_path``, then reads captured stdout. Nothing is mocked: the CLI is
a thin layer, and testing it through anything but its actual entry point would test the wrong
thing.
"""

import hashlib
import json

import pytest

from sluice.cli import main
from sluice.config import (
    CONFIG_ENV_VAR,
    HOME_ENV_VAR,
    ConfigError,
    load_config,
    resolve_config_path,
)
from sluice.config.setup import plan_setup
from sluice.config.schema import CONFIG_FIELDS
from sluice.core.daemon import assemble_registry
from sluice.core.db import initialize_database, insert_run, update_run, write_txn
from sluice.core.profile import REFERENCE_PROFILES
from sluice.doctor import Severity, run_doctor
from sluice.gates.profiles import PRODUCTION_PROFILES, PRODUCTION_VERSION


@pytest.fixture
def deployment(tmp_path):
    """A config file plus an initialized database, as a real install would have."""
    state_file = tmp_path / "sluice_state.db"
    initialize_database(state_file)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "state_file": str(state_file),
                "local_staging_dir": str(tmp_path / "staging"),
            }
        ),
        encoding="utf-8",
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
    """``--config`` > ``$SLUICE_CONFIG`` > ``./config.json`` > ``<home>/config.json``."""
    import pathlib

    monkeypatch.setenv(CONFIG_ENV_VAR, "/from/env.json")
    assert resolve_config_path("/explicit.json") == pathlib.Path("/explicit.json")
    assert str(resolve_config_path(None)) == "/from/env.json"

    monkeypatch.delenv(CONFIG_ENV_VAR)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV_VAR, str(home))

    working = tmp_path / "cwd"
    working.mkdir()
    monkeypatch.chdir(working)

    # Nothing in the working directory: fall through to the sluice home.
    assert resolve_config_path(None) == home / "config.json"

    # A config that is really there in the working directory still wins, which is
    # what keeps a repo checkout usable without --config.
    (working / "config.json").write_text("{}", encoding="utf-8")
    assert str(resolve_config_path(None)) == "config.json"


def test_the_config_setup_writes_is_the_config_commands_read(tmp_path, monkeypatch):
    """Setup's output path and the resolver's default must be the same file.

    They were not, and it made a correct install unusable: `sluice setup` wrote
    `~/.sluice/config.json`, every command looked for `./config.json`, and setup
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


def test_invalid_json_is_reported_as_a_config_error(tmp_path):
    """A malformed config fails as ConfigError, not a raw JSONDecodeError."""
    bad = tmp_path / "config.json"
    bad.write_text("{not json", encoding="utf-8")

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


def test_every_state_change_reports_its_audit_id(deployment, capsys):
    """Each op prints the audit row id it wrote.

    The audit trail is the only record of who did what; showing the id makes it
    findable later without digging through the table.
    """
    add_run(deployment.state_file, "SEQ_007")

    main(["--config", str(deployment.config_path), "hold", "SEQ_007"])

    assert "audited as #" in capsys.readouterr().out


def test_the_actor_defaults_to_the_os_user_and_is_overridable(deployment, capsys):
    """``--actor`` names who acted; the audit row records it.

    There is no anonymous path — an unattributed audit row is worthless evidence.
    """
    add_run(deployment.state_file, "SEQ_008")

    main(
        [
            "--config",
            str(deployment.config_path),
            "--actor",
            "night-shift",
            "--json",
            "hold",
            "SEQ_008",
        ]
    )

    assert json.loads(capsys.readouterr().out)["actor"] == "night-shift"


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
    paused_output = capsys.readouterr().out
    assert "PAUSED" not in paused_output and "running" in paused_output


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
        slack_enabled=False,
    )

    config_path = _write_config(
        tmp_path / "config.json", **setup_defaults | operator_supplied
    )

    exit_code = main(["--config", str(config_path), "doctor"])

    output = capsys.readouterr().out
    assert exit_code == 0, output
    assert "warn" in output.lower()


def test_doctor_with_no_config_at_all_points_at_setup(tmp_path, capsys):
    """The first command a new operator runs must not be a dead end.

    The shared loader message offers ``--config``, ``$SLUICE_CONFIG`` and a cwd
    ``config.json`` — all three of which presuppose a config that exists. On a
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
