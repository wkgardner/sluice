"""The ``samplesheet`` gate — order 3, generates the by-lane samplesheet NVD consumes.

Runs NVD's own ``samplesheet generate`` over the verified ``raw_seqs`` tree and writes
``samplesheet.csv`` into the run's staging directory. Ports PP ``generate_by_lane_samplesheet``
(``nvd_helpers.py:189``).

Idempotent by construction: ``--force`` overwrites any existing file, so re-entry after a crash or
a hold simply regenerates. Nothing is persisted to the run row — the file on disk is the artifact,
so ``persists`` is empty and migration 010 adds only the ``samplesheet_created`` checkmark.

**Depends on `checksum`, not merely on `download`.** Generating a samplesheet from lane files that
have not been verified would carry a corrupt transfer forward into the pipeline, where it surfaces
much later and much more expensively. The contract makes that ordering structural rather than a
convention.

**One production behaviour deliberately NOT ported: the repo checkout.** PP's
``ensure_nvd_repo_ref`` runs ``git checkout -f main`` inside the NVD repo before generating, to pin
the tool version. sluice does not, for two reasons: ``-f`` discards uncommitted work in a
repository a human may be actively using, and pinning which version of an external tool is
installed is a *deployment* concern rather than something a per-run gate should assert. Version
determinism is instead expressed by pointing ``nvd_repo_dir`` at the intended checkout. See plan 15
§10.
"""

import functools
import pathlib
from typing import Any

from sluice.core.gate import Gate, GateOutcome, GateResult, no_op_invalidate
from sluice.gates.download import RAW_SEQS_DIRNAME
from sluice.gates.nvd_tool import NvdToolError, make_nvd_client

__all__ = ["SAMPLESHEET", "SAMPLESHEET_FILENAME", "run_samplesheet_gate"]

# Written into the run's staging directory, beside raw_seqs/ rather than inside
# it: raw_seqs holds delivered data, and a generated artifact living among it
# would be swept up by anything treating that directory as the delivery.
SAMPLESHEET_FILENAME = "samplesheet.csv"


def run_samplesheet_gate(context: Any, make_client) -> GateResult:
    """Generate this run's by-lane samplesheet. The gate's orchestration.

    ``make_client`` is the zero-arg seam (the ``make_experiment_client``
    precedent), so nothing is constructed until the work is actually needed.

    Fails closed: an absent ``raw_seqs`` directory, a tool failure, or a tool that
    exits cleanly without producing a usable file all quarantine the run rather
    than letting an empty or missing samplesheet reach NVD.
    """
    run_name = context.run_row["name"]
    workdir = pathlib.Path(context.workdir)
    raw_seqs_dir = workdir / RAW_SEQS_DIRNAME
    output_path = workdir / SAMPLESHEET_FILENAME

    if not raw_seqs_dir.is_dir():
        # Reachable if the staging tree was moved or cleaned between gates.
        # Caught here with a specific message rather than as an opaque tool
        # failure, because the fix is entirely different.
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"samplesheet gate: no {RAW_SEQS_DIRNAME} directory at "
                f"{raw_seqs_dir} for run {run_name!r} — the downloaded lanes are "
                "not where the pipeline expects them"
            ),
        )

    try:
        make_client().generate_samplesheet(raw_seqs_dir, output_path)
    except NvdToolError as tool_error:
        return GateResult(
            GateOutcome.FAILURE,
            reason=f"samplesheet gate: run {run_name!r}: {tool_error}",
        )

    return GateResult(GateOutcome.SUCCESS, metadata={"samplesheet": str(output_path)})


def _run_samplesheet(context: Any) -> GateResult:
    """Production wiring: defer client construction behind the zero-arg seam."""
    return run_samplesheet_gate(
        context, functools.partial(make_nvd_client, context.config)
    )


SAMPLESHEET = Gate(
    id="samplesheet",
    name="Generate by-lane samplesheet",
    satisfied_column="samplesheet_created",  # migration 010
    order=3,  # TRUE execution order — after checksum
    needs=("checksum",),  # never build a samplesheet from unverified lanes
    produces=(SAMPLESHEET_FILENAME,),
    run=_run_samplesheet,
    # ADR-0005: removing the generated ``samplesheet.csv`` would be the honest
    # undo, and sluice never deletes automatically. Re-running overwrites.
    invalidate=no_op_invalidate,
    terminate=None,
    persists={},  # the file on disk is the artifact — no token worth a column
)
"""The ``samplesheet`` gate — Plan B gate 3.

``needs=("checksum",)`` rather than ``("download",)`` is the load-bearing choice: it makes
"verified before used" structural instead of conventional.
"""
