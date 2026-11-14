"""The ``profiling`` gate — order 4, a REMV viral screen of the reads before NVD.

Runs our Nextflow pipeline **REMV** (Rapid Environmental MetaViromics, ``profiling_repo_dir``):
Deacon host depletion, BBTools read QC, then sylph and EsViritu calls against the Virus Pathogen
Database. It reads the same by-lane ``samplesheet.csv`` the ``samplesheet`` gate wrote for NVD. No
conversion is needed: REMV takes NVD's samplesheet format as-is (``sample_id,srr,platform,fastq1,
fastq2,fastq1_glob,fastq2_glob``, lanes matched by the ``*_glob`` columns).

(This gate first ran silly-fast-bfx. It was retargeted to REMV before any profile that holds it
went live, so no run ever recorded a silly-fast-bfx profile under this gate.)

**Order 4, before ``nvd``.** The daemon runs one blocking gate per look, so "alongside NVD" is not
expressible. ``nvd`` does NOT list this gate in ``needs`` — they share an input, not an output —
but because this gate has the lower order, a profiling failure quarantines the run and NVD waits.
That is the chosen behaviour: the operator fixes the cause and ``clear_error``s. (Excluding
``profiling`` for one run would let it go to NVD, but ``control.exclude_gate`` is not on the CLI
yet.)

## Launch shape — mirrors ``nvd.py``, deliberately copied rather than shared

The launch-directory guard (D2/A7) and the append-mode, heartbeating log (D3/D5) are copied from
``nvd.py`` so that module stays untouched. They are small; if a third Nextflow gate arrives, that is
the moment to extract them. Additionally for this pipeline:

* **Nextflow comes from the pipeline's own pixi environment.** REMV requires Nextflow
  ``>=26.04.0`` and pins its own in ``pixi.toml``; the host's Nextflow is older.
  ``pixi run --manifest-path <repo>/pixi.toml`` resolves the pinned one.
* **No YAML is generated or merged.** The operator's params file (reference paths, the shared
  ``ospool_staging_root`` that holds the containers, the accounting group) is passed with
  ``-params-file`` untouched. The per-run values (``--input``, ``--outdir``, ``--run_id``) are CLI
  params, which Nextflow ranks above the params file, and ``-work-dir`` on the CLI overrides the
  ``workDir`` the site config derives from ``ospool_staging_root``.
* **The site config** (``-c``) is REMV's ``ospool-local/ospool.config``: the nf-ospool executor,
  per-process containers and resource requests.
* **REMV writes no Nextflow trace by default**, so the command asks for one (``-with-trace``) inside
  the results dir — the trace is the success evidence below.

## Exit code 0 is not success

A site config may set ``errorStrategy = 'ignore'``, under which Nextflow exits 0 with failed tasks.
(REMV's own config retries and then stops the run, but a later edit could change that.) Reporting
that as SUCCESS would be a gate satisfied without its work done (ADR-0004). So after a clean exit
the gate reads ``pipeline_info/execution_trace.txt`` and fails on any task that never succeeded, or
on a missing trace (no evidence the work happened).

The trace has one row per *attempt*, so a task that failed once and passed on a retry leaves a
``FAILED`` row behind (REMV's 32824 run had one: ``WRITE_SAMPLE_STATUS`` exited 255, then
completed). Only a task with no ``COMPLETED`` or ``CACHED`` attempt counts as failed.
"""

import csv
import pathlib
import subprocess
import time
from typing import Any

import structlog

from sluice.core.gate import Gate, GateOutcome, GateResult, no_op_invalidate
from sluice.gates.samplesheet import SAMPLESHEET_FILENAME
from sluice.gates.seqera import LINES_TO_WAIT_FOR_WATCH_URL, find_watch_url

__all__ = [
    "PROFILING",
    "ProfilingError",
    "build_profiling_command",
    "count_failed_tasks",
    "remv_run_id_for",
    "resolve_profiling_launch_dir",
    "run_profiling_gate",
]

log = structlog.get_logger("sluice.gates.profiling")

# Where the gate asks REMV (``-with-trace``) to write its Nextflow trace, relative to its results dir.
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
    """This experiment's published REMV results directory (REMV's ``outdir``)."""
    return (
        pathlib.Path(config["profiling_results_base"]) / str(experiment_id) / "results"
    )


def remv_run_id_for(run_name, experiment_id) -> str:
    """The ``run_id`` REMV stamps on every row of its tables: ``rww_<run date>_<experiment>``.

    The date is the sequencing run's (the first ``_`` field of an Illumina run
    name, e.g. ``20261106`` from ``20261106_LH00642_0232_B25FGW7LT4``), not the
    launch date. It must be stable across attempts: ``run_id`` is a task input,
    so a value that changed on a retry day would void ``-resume``'s cache.
    """
    run_date = str(run_name).split("_")[0]
    return f"rww_{run_date}_{experiment_id}"


def build_profiling_command(
    config, samplesheet_path, experiment_id, run_name
) -> list[str]:
    """Build the REMV launch command for one run.

    Every required value is read by subscript, so a missing key is a loud
    ``KeyError`` naming itself rather than a baked-in production default.
    ``profiling_profile`` is optional: ``-profile`` is appended only when it is
    set to a non-empty value.

    Raises:
        KeyError: a required ``profiling_*`` key is absent from config.
    """
    repo_dir = str(config["profiling_repo_dir"])
    results_dir = results_dir_for(config, experiment_id)
    command = [
        "pixi",
        "run",
        "--manifest-path",
        f"{repo_dir}/pixi.toml",  # REMV's env pins the Nextflow it needs
        "nextflow",
        "run",
        repo_dir,
        "-c",
        str(config["profiling_site_config"]),
        "-params-file",
        str(config["profiling_params_file"]),
        "-work-dir",
        f"{config['profiling_results_base']}/{experiment_id}/work",
        "-with-trace",
        str(results_dir / TRACE_RELATIVE_PATH),
        "-ansi-log",
        "false",  # plain lines for the log file, not terminal redraws
        "--input",
        str(pathlib.Path(samplesheet_path).absolute()),
        "--outdir",
        str(results_dir),
        "--run_id",
        remv_run_id_for(run_name, experiment_id),
        "-resume",  # harmless when fresh; picks up checkpoints after a failure
    ]
    profile = config.get("profiling_profile")
    if profile:
        command += ["-profile", profile]
    return command


def count_failed_tasks(trace_path) -> int:
    """Count the tasks in a Nextflow ``execution_trace.txt`` that never succeeded.

    The trace is tab-separated with a header row and one row per attempt; ``name``
    and ``status`` are read by column name, so extra or reordered columns do not
    matter. A task is identified by its ``name`` (process plus tag, e.g.
    ``RAPID_WW:ESVIRITU (sample_A)``). It counts as failed when it has a ``FAILED``
    attempt and no ``COMPLETED`` or ``CACHED`` (reused by ``-resume``) one — a
    failure that a retry recovered is not a failure of the run.
    """
    failed_task_names = set()
    succeeded_task_names = set()
    with pathlib.Path(trace_path).open(encoding="utf-8", newline="") as trace_file:
        for row in csv.DictReader(trace_file, delimiter="\t"):
            if row.get("status") == "FAILED":
                failed_task_names.add(row.get("name"))
            elif row.get("status") in ("COMPLETED", "CACHED"):
                succeeded_task_names.add(row.get("name"))
    return len(failed_task_names - succeeded_task_names)


def _stream_to_log(
    process,
    log_path,
    run_name,
    announce_start=None,
    nextflow_log_path=None,
    launched_at=0.0,
) -> int:
    """Stream the pipeline's output to its log, heartbeating, and return the exit code.

    Copied from ``nvd._stream_to_log``. D3: append mode with a run-start banner,
    so a retry never destroys the failed attempt's log. D5: a coarse heartbeat
    every 100 lines distinguishes "still working" from "hung" on a gate with no
    time bound (⚑F).

    Seqera link: when ``announce_start`` is given (the daemon's callback), it is
    called exactly once — with the run's watch link as soon as Nextflow records
    it, or with ``None`` after ``LINES_TO_WAIT_FOR_WATCH_URL`` lines without one.
    ``nextflow_log_path`` (``<launch dir>/.nextflow.log``) and ``launched_at``
    are where and since when to look (see ``sluice.gates.seqera``).
    """
    log_path = pathlib.Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines_seen = 0
    watch_url_pending = announce_start is not None
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n=== profiling run start: {run_name} ===\n")
        log_file.flush()
        for line in process.stdout:
            log_file.write(line)
            log_file.flush()
            lines_seen += 1
            if watch_url_pending:
                watch_url = find_watch_url(line, nextflow_log_path, launched_at)
                if watch_url or lines_seen >= LINES_TO_WAIT_FOR_WATCH_URL:
                    announce_start(watch_url)
                    watch_url_pending = False
            if lines_seen % 100 == 0:
                log.info(
                    "profiling.heartbeat",
                    **{"run.name": run_name, "profiling.output_lines": lines_seen},
                )
    exit_code = process.wait()
    if watch_url_pending:
        # The pipeline ended inside the wait window: still say it started, so the
        # thread never jumps straight from the previous gate to this one's outcome.
        announce_start(None)
    return exit_code


# ---- Layer 2: the orchestrator. Reads the context, returns a result. ----


def run_profiling_gate(context: Any, popen=subprocess.Popen) -> GateResult:
    """Run REMV for this run and decide the gate's outcome.

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
        command = build_profiling_command(
            config, samplesheet_path, experiment_id, run_name
        )
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
        launched_at = time.time()  # a .nextflow.log older than this is stale
        process = popen(
            command,
            cwd=str(launch_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        exit_code = _stream_to_log(
            process,
            log_path,
            run_name,
            # The daemon's start callback (RunContext.announce_start); absent in
            # contexts built without one, which then announce nothing.
            announce_start=getattr(context, "announce_start", None),
            nextflow_log_path=launch_dir / ".nextflow.log",
            launched_at=launched_at,
        )
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
                f"profiling gate: run {run_name!r}: REMV exited "
                f"{exit_code}. Its full output is at {log_path} (appended, so "
                "earlier attempts are still there)"
            ),
        )

    # Exit 0 is not proof (errorStrategy 'ignore'): the trace is the evidence.
    if not trace_path.is_file():
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"profiling gate: run {run_name!r}: REMV exited 0 but "
                f"wrote no {trace_path} — no evidence the work happened. Log: "
                f"{log_path}"
            ),
        )
    failed_task_count = count_failed_tasks(trace_path)
    if failed_task_count:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"profiling gate: run {run_name!r}: {failed_task_count} task(s) "
                f"never succeeded in {trace_path} (Nextflow exited 0, so the site "
                f"config is likely ignoring task errors). Log: {log_path}"
            ),
        )
    return GateResult(GateOutcome.SUCCESS, metadata={"profiling_log": str(log_path)})


# ---- Layer 3: the task object. ----

PROFILING = Gate(
    id="profiling",
    name="REMV pipeline",
    satisfied_column="profiling_completed",  # migration 012
    order=4,  # TRUE execution order — after samplesheet (3), before nvd (5); v3.3 renumbers a copy to 5
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
"""The ``profiling`` gate — REMV viral screen (Deacon, sylph, EsViritu), order 4."""
