"""Behavioral tests for the ``sluice`` CLI and the config loader (plan 15 increment 4).

Every test drives ``main(argv)`` against a real database built through the real migrations and a
real ``config.json`` under ``tmp_path``, then reads captured stdout. Nothing is mocked: the CLI is
a thin layer, and testing it through anything but its actual entry point would test the wrong
thing.
"""

import json

import pytest

from sluice.cli import main
from sluice.config import CONFIG_ENV_VAR, ConfigError, load_config, resolve_config_path
from sluice.core.db import initialize_database, insert_run, update_run, write_txn
from sluice.gates.profiles import PRODUCTION_VERSION


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


def test_config_precedence_is_flag_then_env_then_cwd(tmp_path, monkeypatch):
    """``--config`` beats ``$SLUICE_CONFIG`` beats ``./config.json``."""
    monkeypatch.setenv(CONFIG_ENV_VAR, "/from/env.json")

    assert resolve_config_path("/explicit.json") == __import__("pathlib").Path(
        "/explicit.json"
    )
    assert str(resolve_config_path(None)) == "/from/env.json"

    monkeypatch.delenv(CONFIG_ENV_VAR)
    assert str(resolve_config_path(None)) == "config.json"


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

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "runs             : 0" in output
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
    assert "exp 4471" in output


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
    assert "YES — no run will advance" in capsys.readouterr().out

    assert main([*config, "resume"]) == 0
    main([*config, "status"])
    assert "daemon paused    : no" in capsys.readouterr().out


def test_help_explains_that_commands_are_state_only(capsys):
    """Every state-changing command's --help states the reconcile-loop model.

    The single most important thing for an operator to understand: nothing here
    executes anything, the daemon acts on its next look. Getting this wrong looks
    like "the command did nothing".
    """
    with pytest.raises(SystemExit):
        main(["hold", "--help"])

    assert "daemon acts on its next look" in capsys.readouterr().out


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
