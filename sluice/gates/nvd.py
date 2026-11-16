"""The ``nvd`` gate — order 5, runs the NVD preprocessing pipeline.

The heaviest and highest-risk gate: a Nextflow pipeline that runs for hours. Ports PP
``run_nvd_pipeline`` (``nvd_helpers.py:222``) and carries errata **D2/A7** (launch directory),
**D3** (append-mode logs), **D5** (heartbeats), and **⚑F** (no time bound).

## D2/A7 — the launch directory is the dangerous part

Nextflow writes its ``.nextflow/`` session cache into the **current working directory**, and that
cache is the entire basis of ``--resume``. Three properties therefore matter, and getting any of
them wrong fails silently rather than loudly:

* **Local POSIX, never the CephFS staging tree.** The Nextflow os-pool plugin requires a local
  launch dir. A launch dir on CephFS makes ``--resume`` a no-op, so an interrupted multi-hour run
  silently restarts from zero instead of resuming.
* **Stable per experiment.** The same run must land in the same directory every attempt, or
  ``--resume`` finds no prior session.
* **Configured, never derived from the install location.** Production uses
  ``Path(__file__).resolve().parent / "nvd-launch"`` — errata A7. Relocating the install then moves
  the launch dir, and every in-flight run loses its resume cache with no error at all.

``resolve_launch_dir`` enforces the first and third: ``nvd_launch_base`` is read by subscript, and
a base *inside* ``local_staging_dir`` is refused outright, because that is precisely the mistake D2
describes and it is otherwise invisible until a multi-hour re-run.

## D3 — the log is appended, never truncated

Per-experiment, opened in append mode with a run-start banner. A retry must never destroy the
failed attempt's log: on a pipeline that takes hours, that log is often the only evidence of why
the first attempt failed.

## ADR-0004 — there is no ``nvd_auto_run`` runtime skip

Production checks ``nvd_auto_run`` and **returns success without running anything** when it is
false. That is the exact shape of the bug ADR-0004 was written against — a gate reporting satisfied
without doing its work, so the run either completes falsely or loops forever.

sluice has one way to not run a gate: mark it ``DISABLED`` in the profile, which removes it from the
contract entirely and satisfies its dependents by absence (ADR-0004 d.2). The setting is not read
here, deliberately.
"""

import pathlib
import subprocess
import time
from typing import Any

import structlog

from sluice.core.gate import Gate, GateOutcome, GateResult, no_op_invalidate
from sluice.gates.nvd_tool import NvdToolError, build_nvd_command
from sluice.gates.samplesheet import SAMPLESHEET_FILENAME
from sluice.gates.seqera import SECONDS_TO_WAIT_FOR_WATCH_URL, find_watch_url

__all__ = [
    "NVD",
    "PARAMS_FILENAME",
    "build_params_yaml",
    "build_webdav_url",
    "resolve_launch_dir",
    "run_nvd_gate",
]

log = structlog.get_logger("sluice.gates.nvd")

PARAMS_FILENAME = "nvd-params.yaml"


def resolve_launch_dir(config, experiment_id) -> pathlib.Path:
    """Return this experiment's Nextflow launch directory, validated (D2/A7).

    Args:
        config: the loaded config. ``nvd_launch_base`` is read by subscript — a
            missing key must fail loudly, never fall back to a path derived from
            where the code is installed (A7).
        experiment_id: the run's LabKey experiment number, which makes the
            directory stable per run across attempts.

    Returns:
        ``{nvd_launch_base}/{experiment_id}``.

    Raises:
        KeyError: ``nvd_launch_base`` is not configured.
        NvdToolError: the base sits inside ``local_staging_dir``. That is the D2
            mistake exactly — a launch dir on the CephFS staging tree makes
            ``--resume`` a silent no-op — and it is refused here because the
            alternative is discovering it after a multi-hour re-run.
    """
    launch_base = pathlib.Path(config["nvd_launch_base"])
    staging_dir = config.get("local_staging_dir")
    if staging_dir:
        try:
            launch_base.absolute().relative_to(pathlib.Path(staging_dir).absolute())
        except ValueError:
            pass  # Not under staging — the good case.
        else:
            raise NvdToolError(
                f"nvd_launch_base ({launch_base}) is inside local_staging_dir "
                f"({staging_dir}). Nextflow's session cache must live on local "
                "POSIX; on the CephFS staging tree --resume silently does "
                "nothing and an interrupted run restarts from zero (errata D2)"
            )
    return launch_base / str(experiment_id)


def build_webdav_url(config, experiment_id) -> str:
    """The WebDAV directory NVD uploads this experiment's files into.

    Derived, never configured: the URL is ``labkey_server`` and
    ``labkey_experiments_project`` — both of which sluice already reads to create
    the experiment in the first place — with the experiment id in the middle. A
    ``nvd_labkey_webdav`` key would be a second place to write the same host down,
    and therefore the first place for it to disagree with the LabKey adapter.

    The container is the **experiments** project, not ``nvd_labkey_project_name``:
    the result files ride along with the experiment, while the hit *lists* live in
    the separate upload project. Production hardcoded the whole URL, both halves
    included (PP ``nvd_helpers.py:70``).

    Slashes are normalised because the container is written both ways in real
    configs (``/dho/experiments`` and ``dho/experiments``). A doubled slash makes
    a URL that reads correctly in a log line and 404s against WebDAV.

    Raises:
        KeyError: ``labkey_server`` or ``labkey_experiments_project`` is absent.
    """
    server = str(config["labkey_server"]).strip("/")
    container = str(config["labkey_experiments_project"]).strip("/")
    return f"https://{server}/_webdav/{container}/{experiment_id}/@files/"


def build_params_yaml(config, experiment_id, samplesheet_path) -> str:
    """Render the NVD params YAML for one run.

    Every value comes from config **by subscript**, with no production defaults
    baked in. Production uses ``config.get(key, <real production path>)``
    throughout (``generate_params_yaml``), which means a missing key silently runs
    the pipeline against someone else's reference data — and puts real
    infrastructure values in the source. Guardrail §8 and the gate map's
    redaction rule both land here: a missing key is a loud ``KeyError`` naming
    itself.

    **Omitting a param is worse than getting one wrong.** NVD supplies its own
    defaults for anything absent from this file, and those defaults are the live
    production values compiled into the NVD repo. A dropped ``labkey_*`` target
    therefore leaves sluice's own source clean while real results upload to the
    real lists — a failure invisible to both `sluice doctor` and this codebase.
    So every param production emits is emitted here, including the upload
    targets and ``trim_adapters``.

    Raises:
        KeyError: any required NVD parameter is absent from config.
    """
    results_base = config["nvd_results_base"]
    quoted = {
        "samplesheet": pathlib.Path(samplesheet_path).absolute(),
        "experiment_id": experiment_id,
        "results": f"{results_base}/{experiment_id}/nvd-results",
        "work_dir": f"{results_base}/{experiment_id}/nvd-work",
        "blast_db": config["nvd_blast_db"],
        "blast_db_prefix": config["nvd_blast_db_prefix"],
        "blast_db_version": config["nvd_blast_db_version"],
        "taxonomy_dir": config["nvd_taxonomy_dir"],
        "virus_index": config["nvd_virus_index"],
        "virus_index_version": config["nvd_virus_index_version"],
        # Where NVD puts its own results. (The exp-id guard list is gone:
        # NVD v3.4.0 removed the concept, and NvdParams is extra="forbid",
        # so emitting it fails every run at param validation.)
        "labkey_server": config["nvd_labkey_server"],
        "labkey_project_name": config["nvd_labkey_project_name"],
        "labkey_schema": config["nvd_labkey_schema"],
        "labkey_blast_fasta_list": config["nvd_labkey_blast_fasta_list"],
        "labkey_blast_meta_hits_list": config["nvd_labkey_blast_meta_hits_list"],
    }
    bare = {
        "cutoff_percent": config["nvd_cutoff_percent"],
        "entropy": config["nvd_entropy"],
        "tax_stringency": config["nvd_tax_stringency"],
        "max_blast_targets": config["nvd_max_blast_targets"],
        "dedup": str(config["nvd_dedup"]).lower(),
        "trim_adapters": str(config["nvd_trim_adapters"]).lower(),
        # Both default FALSE in NVD v3.4.1 but production always ran them
        # true (decided 2026-09-12) — omission would silently change the
        # analysis, the exact failure this builder's docstring warns about.
        "experimental": str(config["nvd_experimental"]).lower(),
        "filter_low_complexity_reads": str(
            config["nvd_filter_low_complexity_reads"]
        ).lower(),
        "labkey": str(config["nvd_labkey"]).lower(),
        # Unquoted to match production's line exactly (PP ``nvd_helpers.py:117``).
        "labkey_webdav": build_webdav_url(config, experiment_id),
    }
    lines = [
        "# NVD pipeline parameters — generated by sluice, do not edit by hand.",
        f"# experiment {experiment_id}",
        "",
    ]
    lines += [f'{key}: "{value}"' for key, value in quoted.items()]
    lines += [f"{key}: {value}" for key, value in bare.items()]
    return "\n".join(lines) + "\n"


def _stream_to_log(
    process,
    log_path,
    run_name,
    announce_start=None,
    nextflow_log_path=None,
    launched_at=0.0,
) -> int:
    """Stream the pipeline's output to its log, heartbeating, and return the exit code.

    D3: the log is opened in **append** mode with a run-start banner, so a retry
    never destroys the failed attempt's log — on a pipeline that runs for hours,
    that log is often the only evidence of why the first attempt failed.

    D5: each line emits a heartbeat, which is what distinguishes "still working"
    from "hung" on a gate with no time bound (⚑F). Without it a long silent stage
    is indistinguishable from a dead process.

    Seqera link: when ``announce_start`` is given (the daemon's callback), it is
    called exactly once — with the run's watch link as soon as Nextflow records
    it, or with ``None`` once ``SECONDS_TO_WAIT_FOR_WATCH_URL`` pass without one.
    ``nextflow_log_path`` (``<launch dir>/.nextflow.log``) and ``launched_at``
    are where and since when to look (see ``sluice.gates.seqera``).
    """
    log_path = pathlib.Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines_seen = 0
    watch_url_pending = announce_start is not None
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n=== nvd run start: {run_name} ===\n")
        log_file.flush()
        for line in process.stdout:
            log_file.write(line)
            log_file.flush()
            lines_seen += 1
            if watch_url_pending:
                watch_url = find_watch_url(line, nextflow_log_path, launched_at)
                waited_seconds = time.time() - launched_at
                if watch_url or waited_seconds >= SECONDS_TO_WAIT_FOR_WATCH_URL:
                    announce_start(watch_url)
                    watch_url_pending = False
            # Coarse heartbeat: NVD is chatty, and one event per line would drown
            # the events sink on a multi-hour run.
            if lines_seen % 100 == 0:
                log.info(
                    "nvd.heartbeat",
                    **{"run.name": run_name, "nvd.output_lines": lines_seen},
                )
    exit_code = process.wait()
    if watch_url_pending:
        # The pipeline ended inside the wait window: still say it started, so the
        # thread never jumps straight from the previous gate to this one's outcome.
        announce_start(None)
    return exit_code


def run_nvd_gate(context: Any, popen=subprocess.Popen) -> GateResult:
    """Run the NVD pipeline for this run, resuming if a prior session exists.

    ``--resume`` is always passed: harmless on a fresh run, and on a failed one it
    picks up from Nextflow's last checkpoint rather than repeating hours of work.
    That is the entire reason the launch directory has to be stable and local.

    **No timeout (⚑F).** A real NVD run legitimately takes hours, and any bound
    tight enough to catch a hang would eventually kill healthy work. Liveness is
    observable through the heartbeats instead.
    """
    run_row = context.run_row
    run_name = run_row["name"]
    experiment_id = run_row["experiment_id"]
    config = context.config
    workdir = pathlib.Path(context.workdir)

    if experiment_id is None:
        # Unreachable through the contract (needs=("samplesheet",) transitively
        # requires the experiment gate), but caught explicitly because the launch
        # directory and results tree are both keyed on it — an "unknown" bucket
        # would silently collide runs.
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"nvd gate: run {run_name!r} has no experiment_id — the launch "
                "directory and results tree are keyed on it"
            ),
        )

    samplesheet_path = workdir / SAMPLESHEET_FILENAME
    if not samplesheet_path.is_file():
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"nvd gate: no {SAMPLESHEET_FILENAME} at {samplesheet_path} for "
                f"run {run_name!r} — the samplesheet gate's output is missing"
            ),
        )

    try:
        launch_dir = resolve_launch_dir(config, experiment_id)
        launch_dir.mkdir(parents=True, exist_ok=True)
        params_path = workdir / PARAMS_FILENAME
        params_path.write_text(
            build_params_yaml(config, experiment_id, samplesheet_path),
            encoding="utf-8",
        )
    except NvdToolError as misconfigured:
        return GateResult(
            GateOutcome.FAILURE, reason=f"nvd gate: run {run_name!r}: {misconfigured}"
        )
    except KeyError as missing_key:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"nvd gate: run {run_name!r}: config is missing required key "
                f"{missing_key}"
            ),
        )
    except OSError as unwritable:
        return GateResult(
            GateOutcome.FAILURE,
            reason=f"nvd gate: run {run_name!r}: {unwritable}",
        )

    command = build_nvd_command(
        config, "run", "--params-file", str(params_path), "--resume"
    )
    profile = config.get("nvd_profile")
    if profile:
        command += ["--profile", profile]

    log_path = pathlib.Path(config["nvd_log_dir"]) / f"{experiment_id}.log"
    log.info(
        "nvd.start",
        **{
            "run.name": run_name,
            "nvd.launch_dir": str(launch_dir),
            "nvd.log_path": str(log_path),
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
                f"nvd gate: run {run_name!r}: required binary not found "
                f"({missing_binary}) — is pixi on PATH and nvd_repo_dir correct?"
            ),
        )
    except OSError as launch_failure:
        return GateResult(
            GateOutcome.FAILURE,
            reason=f"nvd gate: run {run_name!r}: {launch_failure}",
        )

    if exit_code != 0:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"nvd gate: run {run_name!r}: the NVD pipeline exited "
                f"{exit_code}. Its full output is at {log_path} (appended, so "
                "earlier attempts are still there)"
            ),
        )
    return GateResult(GateOutcome.SUCCESS, metadata={"nvd_log": str(log_path)})


NVD = Gate(
    id="nvd",
    name="NVD pipeline",
    satisfied_column="nvd_completed",  # migration 011
    order=5,  # TRUE execution order — production-v3.3 renumbers a copy to 6
    needs=("samplesheet",),  # what it reads; earlier gates only run first by order
    produces=("nvd-results/",),
    # No wiring wrapper: the ``popen`` seam is a default argument, so the gate
    # function IS the production entry point.
    run=run_nvd_gate,
    # ADR-0005: the honest undo would remove a results tree that cost hours to
    # compute. Re-running is safe — ``--resume`` picks up from the last
    # checkpoint.
    invalidate=no_op_invalidate,
    terminate=None,  # ⚑E — interruption is handled by --resume on re-entry
    persists={},  # results live on disk — no token worth a column
)
"""The ``nvd`` gate — Plan B gate 5, the heaviest stage.

``needs=("samplesheet",)`` and not ``("registration",)``: NVD consumes the samplesheet, not anything
registration produces, and v3.1 and v3.2 list this gate without registration at all. In v3.3 the
lower order alone keeps registration ahead of it.
"""
