"""Behavioral tests for the ``nvd`` gate (plan 15 increment 7).

The heaviest gate, tested entirely offline: ``Popen`` is injected, so no Nextflow, no pixi
environment, and no hours of compute. Real files under ``tmp_path`` for the launch directory, the
params file, and the log.
"""

import pathlib
import types

import pytest

from sluice.core.gate import GateOutcome
from sluice.gates.nvd import (
    NVD,
    PARAMS_FILENAME,
    build_params_yaml,
    resolve_launch_dir,
    run_nvd_gate,
)
from sluice.gates.nvd_tool import NvdToolError
from sluice.gates.samplesheet import SAMPLESHEET_FILENAME

NVD_PARAMS = {
    "nvd_results_base": "/results",
    "nvd_blast_db": "/ref/blast",
    "nvd_blast_db_prefix": "core_nt",
    "nvd_blast_db_version": "3.0.0",
    "nvd_taxonomy_dir": "/ref/taxdump",
    "nvd_virus_index": "/ref/virus.idx",
    "nvd_virus_index_version": "v3.0",
    "nvd_cutoff_percent": 0.001,
    "nvd_entropy": 0.9,
    "nvd_tax_stringency": 0.7,
    "nvd_max_blast_targets": 100,
    "nvd_dedup": True,
    "nvd_trim_adapters": True,
    "nvd_experimental": True,
    "nvd_filter_low_complexity_reads": True,
    # LabKey upload targets — where NVD puts its own results.
    "nvd_labkey": True,
    "nvd_labkey_server": "labkey.example.org",
    "nvd_labkey_project_name": "proj/uploads",
    "nvd_labkey_schema": "lists",
    "nvd_labkey_blast_fasta_list": "example_fasta_list",
    "nvd_labkey_blast_meta_hits_list": "example_meta_list",
    # The WebDAV destination is derived from these two, not configured directly.
    "labkey_server": "labkey.example.org",
    "labkey_experiments_project": "/proj/experiments",
}


def make_context(tmp_path, *, experiment_id=4471, with_samplesheet=True, **overrides):
    """A RunContext-shaped stand-in with a completed samplesheet stage."""
    workdir = tmp_path / "staging" / str(experiment_id)
    workdir.mkdir(parents=True)
    if with_samplesheet:
        (workdir / SAMPLESHEET_FILENAME).write_text(
            "sample_id,lane\n", encoding="utf-8"
        )
    config = {
        "nvd_repo_dir": str(tmp_path / "nvd"),
        "nvd_launch_base": str(tmp_path / "nvd-launch"),
        "nvd_log_dir": str(tmp_path / "logs" / "nvd"),
        "local_staging_dir": str(tmp_path / "staging"),
        **NVD_PARAMS,
    }
    config.update(overrides)
    return types.SimpleNamespace(
        workdir=workdir,
        config=config,
        run_row={"name": "run_A", "experiment_id": experiment_id},
    )


def fake_popen(*, exit_code=0, output=("line one\n", "line two\n")):
    """A Popen stand-in that yields output lines then exits with ``exit_code``."""
    captured = {}

    def _popen(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        return types.SimpleNamespace(stdout=iter(output), wait=lambda: exit_code)

    _popen.captured = captured
    return _popen


# ---------------------------------------------------------------------------
# D2/A7 — the launch directory. The dangerous part.
# ---------------------------------------------------------------------------


def test_launch_dir_is_stable_per_experiment(tmp_path):
    """The launch directory is keyed on the experiment id, so retries reuse it.

    Nextflow's ``.nextflow/`` session cache lives in the launch directory and is
    the entire basis of ``--resume``. A directory that varied between attempts
    would make every retry a fresh multi-hour run.
    """
    config = {"nvd_launch_base": "/local/nvd-launch"}

    first = resolve_launch_dir(config, 4471)
    second = resolve_launch_dir(config, 4471)

    assert first == second == pathlib.Path("/local/nvd-launch/4471")


def test_a_launch_base_inside_the_staging_tree_is_refused(tmp_path):
    """A launch dir on the CephFS staging tree is rejected, loudly (errata D2).

    THE failure this guard exists for. The Nextflow os-pool plugin needs a local
    POSIX launch dir; on CephFS ``--resume`` silently does nothing, so an
    interrupted run restarts from zero with no error anywhere. Refusing up front
    turns a silent multi-hour loss into a config error.
    """
    config = {
        "nvd_launch_base": str(tmp_path / "staging" / "nvd-launch"),
        "local_staging_dir": str(tmp_path / "staging"),
    }

    with pytest.raises(NvdToolError, match="local POSIX"):
        resolve_launch_dir(config, 4471)


def test_a_launch_base_outside_staging_is_accepted(tmp_path):
    """The ordinary case still works — the guard rejects only the real mistake."""
    config = {
        "nvd_launch_base": str(tmp_path / "local" / "nvd-launch"),
        "local_staging_dir": str(tmp_path / "staging"),
    }

    assert resolve_launch_dir(config, 4471).name == "4471"


def test_a_missing_launch_base_fails_loudly_rather_than_guessing():
    """No ``nvd_launch_base`` is a KeyError, never a path derived from the install.

    Errata A7: production uses ``Path(__file__).resolve().parent / "nvd-launch"``,
    so relocating the install moves the launch dir and every in-flight run loses
    its resume cache with no error at all.
    """
    with pytest.raises(KeyError):
        resolve_launch_dir({}, 4471)


# ---------------------------------------------------------------------------
# Params generation — no production defaults.
# ---------------------------------------------------------------------------


def test_params_are_read_by_subscript_with_no_production_defaults():
    """A missing NVD parameter raises rather than falling back to a baked-in path.

    Production uses ``config.get(key, <real production path>)`` throughout, which
    means a missing key silently runs the pipeline against someone else's
    reference data — and puts real infrastructure values in the source. Guardrail
    §8 and the gate map's redaction rule both land here.
    """
    incomplete = dict(NVD_PARAMS)
    del incomplete["nvd_blast_db"]

    with pytest.raises(KeyError, match="nvd_blast_db"):
        build_params_yaml(incomplete, 4471, "/staging/4471/samplesheet.csv")


def test_params_yaml_carries_the_run_specific_values(tmp_path):
    """The rendered YAML names this run's samplesheet, experiment, and result tree."""
    rendered = build_params_yaml(NVD_PARAMS, 4471, tmp_path / "samplesheet.csv")

    assert 'experiment_id: "4471"' in rendered
    assert "samplesheet.csv" in rendered
    assert "/results/4471/nvd-results" in rendered
    assert "/results/4471/nvd-work" in rendered


def test_params_yaml_carries_the_labkey_upload_targets():
    """The lists NVD uploads its own results into are all named in the params.

    Omitting them does not disable the upload — NVD falls back to the production
    defaults compiled into its own repo and uploads there. So a dropped param is
    worse than a wrong one: sluice stays clean while real results land in the
    real lists. Every target is therefore emitted explicitly.
    """
    rendered = build_params_yaml(NVD_PARAMS, 4471, "/staging/4471/samplesheet.csv")

    assert "labkey: true" in rendered
    assert 'labkey_server: "labkey.example.org"' in rendered
    assert 'labkey_project_name: "proj/uploads"' in rendered
    assert 'labkey_schema: "lists"' in rendered
    assert 'labkey_blast_fasta_list: "example_fasta_list"' in rendered
    assert 'labkey_blast_meta_hits_list: "example_meta_list"' in rendered
    # Removed in NVD v3.4.0 (the concept is gone from the tool); NvdParams is
    # extra="forbid", so emitting it makes every `nvd run` fail at validation.
    assert "labkey_exp_id_guard_list" not in rendered
    assert "trim_adapters: true" in rendered
    # Production parity, decided 2026-09-12: the poller ran both true; NVD
    # v3.4.1 defaults both false, so omission would silently change analysis.
    assert "experimental: true" in rendered
    assert "filter_low_complexity_reads: true" in rendered


def test_the_webdav_destination_is_derived_from_the_experiments_container():
    """``labkey_webdav`` is built per experiment, never configured as its own key.

    It is one URL assembled from three things sluice already knows, so a separate
    config key would be a fourth place for the same host to be written down and
    the first place for it to disagree. Production hardcoded the whole URL
    (``nvd_helpers.py:70``); the container here is the *experiments* project, not
    the upload project, because the files ride along with the experiment.
    """
    rendered = build_params_yaml(NVD_PARAMS, 4471, "/staging/4471/samplesheet.csv")

    assert (
        "labkey_webdav: https://labkey.example.org/_webdav/proj/experiments"
        "/4471/@files/" in rendered
    )


def test_the_experiments_container_is_normalised_before_it_joins_the_url():
    """A leading or trailing slash on the container never doubles up in the URL.

    ``labkey_experiments_project`` is written both ways in real configs
    (``/dho/experiments`` and ``dho/experiments``). A doubled slash makes a URL
    that looks right in a log line and 404s against WebDAV.
    """
    for container in ("/proj/experiments", "proj/experiments/", "/proj/experiments/"):
        config = {**NVD_PARAMS, "labkey_experiments_project": container}

        rendered = build_params_yaml(config, 4471, "/staging/4471/samplesheet.csv")

        assert "_webdav/proj/experiments/4471/@files/" in rendered
        assert "//4471" not in rendered
        assert "_webdav//" not in rendered


def test_the_upload_targets_have_no_production_defaults():
    """A missing upload target raises rather than letting NVD pick its own.

    The same guardrail as the reference-data params above, applied to the keys
    where falling back is most costly: NVD's internal default is the live
    production list.
    """
    incomplete = dict(NVD_PARAMS)
    del incomplete["nvd_labkey_blast_meta_hits_list"]

    with pytest.raises(KeyError, match="nvd_labkey_blast_meta_hits_list"):
        build_params_yaml(incomplete, 4471, "/staging/4471/samplesheet.csv")


# ---------------------------------------------------------------------------
# Execution.
# ---------------------------------------------------------------------------


def test_resume_is_always_passed_and_the_cwd_is_the_launch_dir(tmp_path):
    """``--resume`` is always present, and the process runs IN the launch directory.

    Both halves matter together: ``--resume`` reads Nextflow's session cache, and
    that cache is found relative to the working directory. Passing the flag while
    launching from elsewhere would look correct and resume nothing.
    """
    context = make_context(tmp_path)
    popen = fake_popen()

    result = run_nvd_gate(context, popen=popen)

    assert result.outcome is GateOutcome.SUCCESS
    assert "--resume" in popen.captured["command"]
    assert popen.captured["cwd"].endswith("nvd-launch/4471")


def test_the_params_file_is_written_into_the_run_workdir(tmp_path):
    """The generated params YAML lands beside the samplesheet."""
    context = make_context(tmp_path)

    run_nvd_gate(context, popen=fake_popen())

    assert (context.workdir / PARAMS_FILENAME).is_file()


def test_the_profile_flag_is_passed_only_when_configured(tmp_path):
    """``--profile`` appears when ``nvd_profile`` is set, and not otherwise."""
    without = fake_popen()
    run_nvd_gate(make_context(tmp_path), popen=without)
    assert "--profile" not in without.captured["command"]

    with_profile = fake_popen()
    run_nvd_gate(
        make_context(tmp_path / "other", nvd_profile="chtc"), popen=with_profile
    )
    assert with_profile.captured["command"][-2:] == ["--profile", "chtc"]


def test_the_log_is_appended_so_a_retry_keeps_the_failed_attempt(tmp_path):
    """A second run appends to the log rather than truncating it (errata D3).

    On a pipeline that runs for hours, the failed attempt's log is often the only
    evidence of why it failed. Truncating on retry destroys exactly the thing an
    operator needs.
    """
    context = make_context(tmp_path)
    log_path = pathlib.Path(context.config["nvd_log_dir"]) / "4471.log"

    run_nvd_gate(context, popen=fake_popen(output=("first attempt\n",)))
    run_nvd_gate(context, popen=fake_popen(output=("second attempt\n",)))

    contents = log_path.read_text()
    assert "first attempt" in contents and "second attempt" in contents
    assert contents.count("=== nvd run start") == 2


def test_a_nonzero_exit_quarantines_and_points_at_the_log(tmp_path):
    """A failed pipeline fails the gate, naming the log that holds the detail.

    The pipeline's real output is thousands of lines, so the quarantine reason
    points at the log rather than trying to summarise it.
    """
    context = make_context(tmp_path)

    result = run_nvd_gate(context, popen=fake_popen(exit_code=1))

    assert result.outcome is GateOutcome.FAILURE
    assert "exited 1" in result.reason
    assert "4471.log" in result.reason


def test_a_missing_samplesheet_gets_its_own_message(tmp_path):
    """No samplesheet fails with a specific reason, not an opaque tool error."""
    context = make_context(tmp_path, with_samplesheet=False)

    result = run_nvd_gate(context, popen=fake_popen())

    assert result.outcome is GateOutcome.FAILURE
    assert SAMPLESHEET_FILENAME in result.reason


def test_a_missing_config_key_fails_the_run_not_the_daemon(tmp_path):
    """An incomplete NVD config quarantines this run, naming the key.

    A config error must not propagate out of the gate and stop the daemon — one
    misconfigured deployment key would otherwise take down every run at once.
    """
    context = make_context(tmp_path)
    del context.config["nvd_virus_index"]

    result = run_nvd_gate(context, popen=fake_popen())

    assert result.outcome is GateOutcome.FAILURE
    assert "nvd_virus_index" in result.reason


def test_a_launch_dir_on_staging_quarantines_with_the_d2_explanation(tmp_path):
    """The D2 misconfiguration surfaces as a run failure explaining itself."""
    context = make_context(
        tmp_path, nvd_launch_base=str(tmp_path / "staging" / "launch")
    )

    result = run_nvd_gate(context, popen=fake_popen())

    assert result.outcome is GateOutcome.FAILURE
    assert "local POSIX" in result.reason


def test_a_run_without_an_experiment_id_is_refused(tmp_path):
    """No experiment id means no stable launch dir, so the gate refuses.

    Unreachable through the contract, but caught explicitly: the launch directory
    and results tree are both keyed on the experiment id, and an "unknown" bucket
    would silently collide runs against each other's session caches.
    """
    context = make_context(tmp_path, experiment_id=4471)
    context.run_row["experiment_id"] = None

    result = run_nvd_gate(context, popen=fake_popen())

    assert result.outcome is GateOutcome.FAILURE
    assert "experiment_id" in result.reason


# ---------------------------------------------------------------------------
# ADR-0004 — no runtime skip.
# ---------------------------------------------------------------------------


def test_nvd_auto_run_false_does_not_skip_the_gate(tmp_path):
    """``nvd_auto_run: false`` is NOT honoured — the pipeline still runs.

    Production checks that flag and returns success without running anything,
    which is the exact shape ADR-0004 was written against: a gate reporting
    satisfied without doing its work, so the run either completes falsely or
    loops forever.

    sluice has one way to not run a gate — mark it DISABLED in the profile, which
    removes it from the contract and satisfies dependents by absence (d.2).
    """
    context = make_context(tmp_path, nvd_auto_run=False)
    popen = fake_popen()

    result = run_nvd_gate(context, popen=popen)

    assert result.outcome is GateOutcome.SUCCESS
    assert "command" in popen.captured, (
        "the pipeline must still have been invoked — nvd_auto_run is not a "
        "runtime skip (ADR-0004)"
    )


def test_the_gate_declares_its_place_in_the_pipeline():
    """Order 5, needs ``samplesheet``, marks ``nvd_completed``, persists nothing.

    ``needs`` is ``samplesheet`` and not ``registration``: registration is
    DISABLED (⚑D) and a disabled gate satisfies dependents by absence, so
    depending on it would be redundant and misleading about what this gate
    actually consumes.
    """
    assert NVD.order == 5
    assert NVD.needs == ("samplesheet",)
    assert NVD.satisfied_column == "nvd_completed"
    assert dict(NVD.persists) == {}


# ---------------------------------------------------------------------------
# The start announcement (ADR-0008).
# ---------------------------------------------------------------------------

WATCH_URL = (
    "https://cloud.seqera.io/orgs/OConnor_lab/workspaces/nvd/watch/5YRGhiTUzwZcD6"
)


def test_the_start_is_announced_once_with_the_seqera_link(tmp_path):
    """NVD prints its link to the console; the gate announces it as soon as it appears."""
    announced = []
    context = make_context(tmp_path)
    context.announce_start = announced.append
    output = (
        "N E X T F L O W\n",
        f"Monitor the execution with Seqera Platform using this URL: {WATCH_URL}\n",
        f"repeated later: {WATCH_URL}\n",
    )

    run_nvd_gate(context, popen=fake_popen(output=output))

    assert announced == [WATCH_URL]


def test_without_a_link_the_start_is_still_announced_once(tmp_path):
    """Tower off: the gate still says it started, exactly once, with no link."""
    announced = []
    context = make_context(tmp_path)
    context.announce_start = announced.append

    run_nvd_gate(context, popen=fake_popen())

    assert announced == [None]


def test_a_link_after_hundreds_of_warning_lines_is_still_found(tmp_path):
    """32836 (2026-11-16): NVD printed ~210 config warnings before its link.

    A 200-line window gave up first and posted "started" with no link. The wait
    is timed now, so the link on line 212 is the one announced.
    """
    announced = []
    context = make_context(tmp_path)
    context.announce_start = announced.append
    warnings = tuple(
        "WARN: There's no process matching config selector: LABKEY_X\n"
        for _ in range(211)
    )
    output = warnings + (
        f"Monitor the execution with Seqera Platform using this URL: {WATCH_URL}\n",
    )

    run_nvd_gate(context, popen=fake_popen(output=output))

    assert announced == [WATCH_URL]
