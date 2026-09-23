"""The ``profiling`` gate — order 4, a quick Deacon + Sylph profile of the raw reads before NVD.

Runs our nf-core pipeline **silly-fast-bfx** (``profiling_repo_dir``) over the same by-lane
``samplesheet.csv`` the ``samplesheet`` gate wrote for NVD. No conversion is needed: NVD's generator
emits exactly silly-fast-bfx's seven columns (``sample_id,srr,platform,fastq1,fastq2,fastq1_glob,
fastq2_glob``), one row per sample with the lanes matched by the ``*_glob`` columns, and
silly-fast-bfx accepts that format as-is (its schema forbids extra columns, so any drift there fails
loudly at the pipeline's own validation step).

**Order 4, before ``nvd``.** The daemon runs one blocking gate per look, so "alongside NVD" is not
expressible; before NVD costs little because profiling is fast. ``nvd`` does NOT list this gate in
``needs`` — they share an input, not an output — but because this gate has the lower order, a
profiling failure quarantines the run and NVD waits. That is the chosen behaviour: the operator
either fixes and ``clear_error``s, or ``exclude profiling`` lets that one run go to NVD.

## Launch shape — mirrors ``nvd.py``, deliberately copied rather than shared

The launch-directory guard (D2/A7) and the append-mode, heartbeating log (D3/D5) are copied from
``nvd.py`` so that module stays untouched. They are small; if a third Nextflow gate arrives, that is
the moment to extract them. Additionally for this pipeline:

* **Nextflow comes from the pipeline's own pixi ``dev`` env.** silly-fast-bfx hard-requires
  Nextflow ``>=26.04.6``; the host's Nextflow is older. ``pixi run --manifest-path … -e dev``
  resolves the pinned one.
* **No YAML is generated or merged.** The operator's reference params file (Deacon/Sylph reference
  specs) is passed with ``-params-file`` untouched, and the per-run values (``--input``,
  ``--results``, ``--work_dir``) are CLI params, which Nextflow ranks above the params file.
* **The site config** (``-c``) holds the CHTC/nf-ospool executor policy. Its relative
  ``submitFileDir`` lands inside the launch dir — which is on /home, as HTCondor requires.

## Exit code 0 is not success

A site config may set ``errorStrategy = 'ignore'``, under which Nextflow exits 0 with failed tasks.
Reporting that as SUCCESS would be a gate satisfied without its work done (ADR-0004). So after a
clean exit the gate reads ``pipeline_info/execution_trace.txt`` and fails on any ``FAILED`` task,
or on a missing trace (no evidence the work happened).
"""

import csv
import pathlib
import subprocess
from typing import Any

import structlog

from sluice.core.gate import Gate, GateOutcome, GateResult, no_op_invalidate
from sluice.gates.samplesheet import SAMPLESHEET_FILENAME

__all__ = [
    "PROFILING",
    "ProfilingError",
    "build_profiling_command",
    "count_failed_tasks",
    "resolve_profiling_launch_dir",
    "run_profiling_gate",
]

log = structlog.get_logger("sluice.gates.profiling")

# Where silly-fast-bfx writes its Nextflow trace, relative to its results dir.
TRACE_RELATIVE_PATH = pathlib.Path("pipeline_info") / "execution_trace.txt"


class ProfilingError(Exception):
    """A profiling misconfiguration found before launch (e.g. the D2 launch-dir mistake)."""


# ---- Layer 1: plain functions. No context, no database, no framework. ----


def resolve_profiling_launch_dir(config, experiment_id) -> pathlib.Path:
    """Return this experiment's Nextflow launch directory, validated (D2/A7).

    Copied from ``nvd.resolve_launch_dir`` (see the module docstring for why).

    Args:
        config: the loaded config. ``profiling_launch_base`` is read by subscript
            — a missing key fails loudly, never falls back to a derived path (A7).
        experiment_id: makes the directory stable per run across attempts, which
            ``-resume`` depends on.

    Returns:
        ``{profiling_launch_base}/{experiment_id}``.

    Raises:
        KeyError: ``profiling_launch_base`` is not configured.
        ProfilingError: the base sits inside ``local_staging_dir``. On the CephFS
            staging tree ``-resume`` silently does nothing, and HTCondor refuses to
            submit from /staging — both invisible until a real run.
    """
    launch_base = pathlib.Path(config["profiling_launch_base"])
    staging_dir = config.get("local_staging_dir")
    if staging_dir:
        try:
            launch_base.absolute().relative_to(pathlib.Path(staging_dir).absolute())
        except ValueError:
            pass  # Not under staging — the good case.
        else:
            raise ProfilingError(
                f"profiling_launch_base ({launch_base}) is inside local_staging_dir "
                f"({staging_dir}). Nextflow's session cache must live on local "
                "POSIX; on the CephFS staging tree -resume silently does nothing "
                "and HTCondor cannot submit from there (errata D2)"
            )
    return launch_base / str(experiment_id)


def results_dir_for(config, experiment_id) -> pathlib.Path:
    """This experiment's published silly-fast-bfx results directory."""
    return (
        pathlib.Path(config["profiling_results_base"])
        / str(experiment_id)
        / "profiling-results"
    )


def build_profiling_command(config, samplesheet_path, experiment_id) -> list[str]:
    """Build the silly-fast-bfx launch command for one run.

    Every required value is read by subscript, so a missing key is a loud
    ``KeyError`` naming itself rather than a baked-in production default.
    ``profiling_profile`` is optional: ``-profile`` is appended only when it is
    set to a non-empty value.

    Raises:
        KeyError: a required ``profiling_*`` key is absent from config.
    """
    repo_dir = str(config["profiling_repo_dir"])
    results_base = config["profiling_results_base"]
    command = [
        "pixi",
        "run",
        "--manifest-path",
        f"{repo_dir}/pyproject.toml",
        "-e",
        "dev",  # the env that pins Nextflow >=26.04.6
        "nextflow",
        "run",
        repo_dir,
        "-c",
        str(config["profiling_site_config"]),
        "-params-file",
        str(config["profiling_params_file"]),
        "--input",
        str(pathlib.Path(samplesheet_path).absolute()),
        "--results",
        str(results_dir_for(config, experiment_id)),
        "--work_dir",
        f"{results_base}/{experiment_id}/profiling-work",
        "-resume",  # harmless when fresh; picks up checkpoints after a failure
    ]
    profile = config.get("profiling_profile")
    if profile:
        command += ["-profile", profile]
    return command


def count_failed_tasks(trace_path) -> int:
    """Count the ``FAILED`` task rows in a Nextflow ``execution_trace.txt``.

    The trace is tab-separated with a header row; ``status`` is read by column
    name, so extra or reordered columns do not matter. ``COMPLETED`` and
    ``CACHED`` (a task reused by ``-resume``) are both fine.
    """
    with pathlib.Path(trace_path).open(encoding="utf-8", newline="") as trace_file:
        rows = csv.DictReader(trace_file, delimiter="\t")
        return sum(1 for row in rows if row.get("status") == "FAILED")


def _stream_to_log(process, log_path, run_name) -> int:
    """Stream the pipeline's output to its log, heartbeating, and return the exit code.

    Copied from ``nvd._stream_to_log``. D3: append mode with a run-start banner,
    so a retry never destroys the failed attempt's log. D5: a coarse heartbeat
    every 100 lines distinguishes "still working" from "hung" on a gate with no
    time bound (⚑F).
    """
    log_path = pathlib.Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines_seen = 0
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n=== profiling run start: {run_name} ===\n")
        log_file.flush()
        for line in process.stdout:
            log_file.write(line)
            log_file.flush()
            lines_seen += 1
            if lines_seen % 100 == 0:
                log.info(
                    "profiling.heartbeat",
                    **{"run.name": run_name, "profiling.output_lines": lines_seen},
                )
    return process.wait()


# ---- Layer 2: the orchestrator. Reads the context, returns a result. ----


def run_profiling_gate(context: Any, popen=subprocess.Popen) -> GateResult:
    """Run silly-fast-bfx for this run and decide the gate's outcome.

    SUCCESS only when Nextflow exits 0 AND its trace shows no failed task. Every
    problem is a FAILURE carrying an operator-readable reason; nothing is
    persisted to the run row (the results tree on disk is the artifact).

    **No timeout (⚑F)**, as with NVD — liveness is visible through heartbeats.
    """
    run_row = context.run_row
    run_name = run_row["name"]
    experiment_id = run_row["experiment_id"]
    config = context.config
    workdir = pathlib.Path(context.workdir)

    if experiment_id is None:
        # Unreachable through the contract (samplesheet transitively needs the
        # experiment gate), but the launch dir and results tree are keyed on it.
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"profiling gate: run {run_name!r} has no experiment_id — the "
                "launch directory and results tree are keyed on it"
            ),
        )

    samplesheet_path = workdir / SAMPLESHEET_FILENAME
    if not samplesheet_path.is_file():
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"profiling gate: no {SAMPLESHEET_FILENAME} at {samplesheet_path} "
                f"for run {run_name!r} — the samplesheet gate's output is missing"
            ),
        )

    try:
        launch_dir = resolve_profiling_launch_dir(config, experiment_id)
        launch_dir.mkdir(parents=True, exist_ok=True)
        command = build_profiling_command(config, samplesheet_path, experiment_id)
        log_path = pathlib.Path(config["profiling_log_dir"]) / f"{experiment_id}.log"
        trace_path = results_dir_for(config, experiment_id) / TRACE_RELATIVE_PATH
    except ProfilingError as misconfigured:
        return GateResult(
            GateOutcome.FAILURE,
            reason=f"profiling gate: run {run_name!r}: {misconfigured}",
        )
    except KeyError as missing_key:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"profiling gate: run {run_name!r}: config is missing required "
                f"key {missing_key}"
            ),
        )
    except OSError as unwritable:
        return GateResult(
            GateOutcome.FAILURE,
            reason=f"profiling gate: run {run_name!r}: {unwritable}",
        )

    log.info(
        "profiling.start",
        **{
            "run.name": run_name,
            "profiling.launch_dir": str(launch_dir),
            "profiling.log_path": str(log_path),
        },
    )

    try:
        process = popen(
            command,
            cwd=str(launch_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        exit_code = _stream_to_log(process, log_path, run_name)
    except FileNotFoundError as missing_binary:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"profiling gate: run {run_name!r}: required binary not found "
                f"({missing_binary}) — is pixi on PATH and profiling_repo_dir "
                "correct?"
            ),
        )
    except OSError as launch_failure:
        return GateResult(
            GateOutcome.FAILURE,
            reason=f"profiling gate: run {run_name!r}: {launch_failure}",
        )

    if exit_code != 0:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"profiling gate: run {run_name!r}: silly-fast-bfx exited "
                f"{exit_code}. Its full output is at {log_path} (appended, so "
                "earlier attempts are still there)"
            ),
        )

    # Exit 0 is not proof (errorStrategy 'ignore'): the trace is the evidence.
    if not trace_path.is_file():
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"profiling gate: run {run_name!r}: silly-fast-bfx exited 0 but "
                f"wrote no {trace_path} — no evidence the work happened. Log: "
                f"{log_path}"
            ),
        )
    failed_task_count = count_failed_tasks(trace_path)
    if failed_task_count:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"profiling gate: run {run_name!r}: {failed_task_count} failed "
                f"task(s) in {trace_path} (Nextflow exited 0, so the site config "
                f"is likely ignoring task errors). Log: {log_path}"
            ),
        )
    return GateResult(GateOutcome.SUCCESS, metadata={"profiling_log": str(log_path)})


# ---- Layer 3: the task object. ----

PROFILING = Gate(
    id="profiling",
    name="silly-fast-bfx profiling",
    satisfied_column="profiling_completed",  # migration 012
    order=4,  # TRUE execution order — after samplesheet (3), before nvd (5)
    needs=("samplesheet",),  # consumes samplesheet.csv; nvd does not need this gate
    produces=("profiling-results/",),
    # No wiring wrapper: the ``popen`` seam is a default argument.
    run=run_profiling_gate,
    # ADR-0005: never delete a results tree automatically. Re-running is safe —
    # ``-resume`` picks up from the last checkpoint.
    invalidate=no_op_invalidate,
    terminate=None,  # as nvd (⚑E) — interruption is handled by -resume on re-entry
    persists={},  # results live on disk — no token worth a column
)
"""The ``profiling`` gate — silly-fast-bfx Deacon + Sylph screen, order 4."""
