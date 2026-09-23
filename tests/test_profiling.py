"""Behavioral tests for the ``profiling`` gate — silly-fast-bfx before NVD.

Tested entirely offline, like ``tests/test_nvd.py``: ``Popen`` is injected, so no Nextflow, no pixi
environment, and no HTCondor. Real files under ``tmp_path`` for the launch directory, the log, and
the Nextflow trace the gate reads to decide success.
"""

import pathlib
import types

import pytest

from sluice.core.gate import GateOutcome
from sluice.gates.profiling import (
    PROFILING,
    ProfilingError,
    build_profiling_command,
    count_failed_tasks,
    resolve_profiling_launch_dir,
    run_profiling_gate,
)
from sluice.gates.samplesheet import SAMPLESHEET_FILENAME

# The header Nextflow writes to execution_trace.txt (tab-separated), trimmed to
# the columns that matter here. The gate reads ``status`` by name, so extra or
# reordered columns in a real trace do not matter.
TRACE_HEADER = "task_id\thash\tname\tstatus\texit\n"


def make_context(tmp_path, *, experiment_id=4471, with_samplesheet=True, **overrides):
    """A RunContext-shaped stand-in whose samplesheet stage has completed."""
    workdir = tmp_path / "staging" / str(experiment_id)
    workdir.mkdir(parents=True)
    if with_samplesheet:
        (workdir / SAMPLESHEET_FILENAME).write_text(
            "sample_id,srr,platform,fastq1,fastq2,fastq1_glob,fastq2_glob\n",
            encoding="utf-8",
        )
    config = {
        "profiling_repo_dir": str(tmp_path / "silly-fast-bfx"),
        "profiling_params_file": str(tmp_path / "profiling" / "params.yaml"),
        "profiling_site_config": str(tmp_path / "profiling" / "site.config"),
        "profiling_results_base": str(tmp_path / "results"),
        "profiling_launch_base": str(tmp_path / "profiling-launch"),
        "profiling_log_dir": str(tmp_path / "logs" / "profiling"),
        "local_staging_dir": str(tmp_path / "staging"),
    }
    config.update(overrides)
    return types.SimpleNamespace(
        workdir=workdir,
        config=config,
        run_row={"name": "run_A", "experiment_id": experiment_id},
    )


def trace_path_for(context) -> pathlib.Path:
    """Where the pipeline writes its trace for this context's experiment."""
    experiment_id = context.run_row["experiment_id"]
    return (
        pathlib.Path(context.config["profiling_results_base"])
        / str(experiment_id)
        / "profiling-results"
        / "pipeline_info"
        / "execution_trace.txt"
    )


def write_trace(context, statuses) -> pathlib.Path:
    """Write a trace file with one task row per status, as Nextflow would."""
    trace_path = trace_path_for(context)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        f"{index}\tab/cdef{index}\tTASK_{index}\t{status}\t0\n"
        for index, status in enumerate(statuses, start=1)
    ]
    trace_path.write_text(TRACE_HEADER + "".join(rows), encoding="utf-8")
    return trace_path


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
# The launch directory — same D2 rule as NVD.
# ---------------------------------------------------------------------------


def test_launch_dir_is_stable_per_experiment():
    """The launch directory is keyed on the experiment id, so retries reuse it.

    Nextflow's ``.nextflow/`` session cache lives there and is the basis of
    ``-resume``. The site config's relative ``submitFileDir`` also lands inside
    it, which is why it must be on /home rather than /staging.
    """
    config = {"profiling_launch_base": "/local/profiling-launch"}

    first = resolve_profiling_launch_dir(config, 4471)
    second = resolve_profiling_launch_dir(config, 4471)

    assert first == second == pathlib.Path("/local/profiling-launch/4471")


def test_a_launch_base_inside_the_staging_tree_is_refused(tmp_path):
    """A launch dir on the CephFS staging tree is rejected, loudly (errata D2).

    On CephFS ``-resume`` silently does nothing, and HTCondor refuses to submit
    from /staging at all — both invisible until a real run.
    """
    config = {
        "profiling_launch_base": str(tmp_path / "staging" / "profiling-launch"),
        "local_staging_dir": str(tmp_path / "staging"),
    }

    with pytest.raises(ProfilingError, match="local POSIX"):
        resolve_profiling_launch_dir(config, 4471)


def test_a_missing_launch_base_fails_loudly_rather_than_guessing():
    """No ``profiling_launch_base`` is a KeyError, never a derived fallback (A7)."""
    with pytest.raises(KeyError):
        resolve_profiling_launch_dir({}, 4471)


# ---------------------------------------------------------------------------
# The command.
# ---------------------------------------------------------------------------


def test_the_command_runs_nextflow_from_the_pipelines_own_pixi_dev_env(tmp_path):
    """Nextflow comes from silly-fast-bfx's ``dev`` env, not whatever is on PATH.

    The pipeline hard-requires Nextflow ``>=26.04.6`` (``nextflowVersion = '!…'``)
    and the host's Nextflow is older; the pipeline's own pixi ``dev`` environment
    pins the right one.
    """
    context = make_context(tmp_path)
    repo = context.config["profiling_repo_dir"]

    command = build_profiling_command(
        context.config, context.workdir / SAMPLESHEET_FILENAME, 4471
    )

    assert command[:8] == [
        "pixi",
        "run",
        "--manifest-path",
        f"{repo}/pyproject.toml",
        "-e",
        "dev",
        "nextflow",
        "run",
    ]
    assert command[8] == repo


def test_the_command_carries_the_operator_files_and_per_run_paths(tmp_path):
    """Site config and reference params come from config; paths come from the run.

    The per-run values are CLI params, which Nextflow ranks above the params
    file — so the operator's reference YAML is passed through untouched and
    sluice never has to parse or merge YAML.
    """
    context = make_context(tmp_path)
    config = context.config
    samplesheet = context.workdir / SAMPLESHEET_FILENAME

    command = build_profiling_command(config, samplesheet, 4471)

    def value_after(flag):
        return command[command.index(flag) + 1]

    assert value_after("-c") == config["profiling_site_config"]
    assert value_after("-params-file") == config["profiling_params_file"]
    assert value_after("--input") == str(samplesheet.absolute())
    assert value_after("--results") == (
        f"{config['profiling_results_base']}/4471/profiling-results"
    )
    assert value_after("--work_dir") == (
        f"{config['profiling_results_base']}/4471/profiling-work"
    )
    assert "-resume" in command


def test_the_profile_flag_is_passed_only_when_configured(tmp_path):
    """``-profile`` appears when ``profiling_profile`` is set, and not otherwise."""
    context = make_context(tmp_path)
    samplesheet = context.workdir / SAMPLESHEET_FILENAME

    without = build_profiling_command(context.config, samplesheet, 4471)
    assert "-profile" not in without

    empty = build_profiling_command(
        {**context.config, "profiling_profile": ""}, samplesheet, 4471
    )
    assert "-profile" not in empty

    with_profile = build_profiling_command(
        {**context.config, "profiling_profile": "chtc_htc"}, samplesheet, 4471
    )
    assert with_profile[-2:] == ["-profile", "chtc_htc"]


def test_a_missing_command_key_raises_naming_itself(tmp_path):
    """Every required key is read by subscript — no baked-in production paths."""
    context = make_context(tmp_path)
    del context.config["profiling_params_file"]

    with pytest.raises(KeyError, match="profiling_params_file"):
        build_profiling_command(context.config, "/s/samplesheet.csv", 4471)


# ---------------------------------------------------------------------------
# The trace check — exit code 0 is not proof of success.
# ---------------------------------------------------------------------------


def test_count_failed_tasks_counts_only_failed_rows(tmp_path):
    """COMPLETED and CACHED rows are fine; only FAILED rows count."""
    context = make_context(tmp_path)
    trace_path = write_trace(
        context, ["COMPLETED", "CACHED", "FAILED", "COMPLETED", "FAILED"]
    )

    assert count_failed_tasks(trace_path) == 2


def test_count_failed_tasks_is_zero_for_a_clean_run(tmp_path):
    """A trace with no FAILED rows counts zero."""
    context = make_context(tmp_path)

    assert count_failed_tasks(write_trace(context, ["COMPLETED", "CACHED"])) == 0


# ---------------------------------------------------------------------------
# Execution.
# ---------------------------------------------------------------------------


def test_a_clean_run_succeeds_from_the_launch_dir(tmp_path):
    """Exit 0 plus a trace with no failures satisfies the gate.

    The process runs IN the launch directory: that is where ``-resume`` looks for
    the session cache.
    """
    context = make_context(tmp_path)
    write_trace(context, ["COMPLETED", "CACHED"])
    popen = fake_popen()

    result = run_profiling_gate(context, popen=popen)

    assert result.outcome is GateOutcome.SUCCESS
    assert popen.captured["cwd"].endswith("profiling-launch/4471")


def test_failed_tasks_quarantine_even_when_nextflow_exits_zero(tmp_path):
    """FAILED rows in the trace fail the gate, even on exit 0.

    The CHTC site config may use ``errorStrategy = 'ignore'``, under which
    Nextflow exits 0 with failed tasks. Reporting that as success would mark the
    gate satisfied without its work done — the shape ADR-0004 forbids.
    """
    context = make_context(tmp_path)
    trace_path = write_trace(context, ["COMPLETED", "FAILED", "FAILED"])

    result = run_profiling_gate(context, popen=fake_popen(exit_code=0))

    assert result.outcome is GateOutcome.FAILURE
    assert "2 failed task" in result.reason
    assert str(trace_path) in result.reason


def test_a_missing_trace_after_exit_zero_quarantines(tmp_path):
    """No trace means no evidence the work happened, so the gate fails."""
    context = make_context(tmp_path)

    result = run_profiling_gate(context, popen=fake_popen(exit_code=0))

    assert result.outcome is GateOutcome.FAILURE
    assert "execution_trace.txt" in result.reason


def test_a_nonzero_exit_quarantines_and_points_at_the_log(tmp_path):
    """A failed pipeline fails the gate, naming the log that holds the detail."""
    context = make_context(tmp_path)

    result = run_profiling_gate(context, popen=fake_popen(exit_code=1))

    assert result.outcome is GateOutcome.FAILURE
    assert "exited 1" in result.reason
    assert "4471.log" in result.reason


def test_the_log_is_appended_so_a_retry_keeps_the_failed_attempt(tmp_path):
    """A second run appends to the log rather than truncating it (errata D3)."""
    context = make_context(tmp_path)
    write_trace(context, ["COMPLETED"])
    log_path = pathlib.Path(context.config["profiling_log_dir"]) / "4471.log"

    run_profiling_gate(context, popen=fake_popen(output=("first attempt\n",)))
    run_profiling_gate(context, popen=fake_popen(output=("second attempt\n",)))

    contents = log_path.read_text()
    assert "first attempt" in contents and "second attempt" in contents
    assert contents.count("=== profiling run start") == 2


def test_a_missing_binary_quarantines_with_a_hint(tmp_path):
    """A missing pixi binary is a run failure that says what to check."""
    context = make_context(tmp_path)

    def missing_pixi(command, **kwargs):
        raise FileNotFoundError("pixi")

    result = run_profiling_gate(context, popen=missing_pixi)

    assert result.outcome is GateOutcome.FAILURE
    assert "binary not found" in result.reason


def test_a_missing_samplesheet_gets_its_own_message(tmp_path):
    """No samplesheet fails with a specific reason, not an opaque tool error."""
    context = make_context(tmp_path, with_samplesheet=False)

    result = run_profiling_gate(context, popen=fake_popen())

    assert result.outcome is GateOutcome.FAILURE
    assert SAMPLESHEET_FILENAME in result.reason


def test_a_missing_config_key_fails_the_run_not_the_daemon(tmp_path):
    """An incomplete profiling config quarantines this run, naming the key."""
    context = make_context(tmp_path)
    del context.config["profiling_site_config"]

    result = run_profiling_gate(context, popen=fake_popen())

    assert result.outcome is GateOutcome.FAILURE
    assert "profiling_site_config" in result.reason


def test_a_launch_dir_on_staging_quarantines_with_the_d2_explanation(tmp_path):
    """The D2 misconfiguration surfaces as a run failure explaining itself."""
    context = make_context(
        tmp_path, profiling_launch_base=str(tmp_path / "staging" / "launch")
    )

    result = run_profiling_gate(context, popen=fake_popen())

    assert result.outcome is GateOutcome.FAILURE
    assert "local POSIX" in result.reason


def test_a_run_without_an_experiment_id_is_refused(tmp_path):
    """No experiment id means no stable launch dir or results tree."""
    context = make_context(tmp_path)
    context.run_row["experiment_id"] = None

    result = run_profiling_gate(context, popen=fake_popen())

    assert result.outcome is GateOutcome.FAILURE
    assert "experiment_id" in result.reason


def test_the_gate_declares_its_place_in_the_pipeline():
    """Order 4, needs ``samplesheet``, marks ``profiling_completed``, persists nothing.

    Order 4 puts it after the samplesheet it consumes and before ``nvd`` (order
    5), which does NOT need it — the two only share an input.
    """
    assert PROFILING.id == "profiling"
    assert PROFILING.order == 4
    assert PROFILING.needs == ("samplesheet",)
    assert PROFILING.satisfied_column == "profiling_completed"
    assert dict(PROFILING.persists) == {}
