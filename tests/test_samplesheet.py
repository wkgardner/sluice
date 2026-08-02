"""Behavioral tests for the ``samplesheet`` gate and the NVD tool adapter (plan 15 increment 6).

Offline throughout: the ``subprocess.run``-shaped runner is injected, so no pixi environment and no
``nvd`` binary are needed. Real files under ``tmp_path`` for everything the gate reads or writes.
"""

import subprocess
import types

import pytest

from sluice.core.gate import GateOutcome
from sluice.gates.download import RAW_SEQS_DIRNAME
from sluice.gates.nvd_tool import (
    NvdToolClient,
    NvdToolError,
    build_nvd_command,
)
from sluice.gates.samplesheet import (
    SAMPLESHEET,
    SAMPLESHEET_FILENAME,
    run_samplesheet_gate,
)


def make_context(tmp_path, *, with_raw_seqs=True, name="run_A"):
    """A RunContext-shaped stand-in with the staging tree a real run would have."""
    workdir = tmp_path / "staging" / "4471"
    if with_raw_seqs:
        raw_seqs = workdir / RAW_SEQS_DIRNAME
        raw_seqs.mkdir(parents=True)
        (raw_seqs / "sample_L001_R1_001.fastq.gz").write_bytes(b"ACGT")
    else:
        workdir.mkdir(parents=True)
    return types.SimpleNamespace(
        workdir=workdir,
        config={"nvd_repo_dir": str(tmp_path / "nvd"), "nvd_command": "nvd"},
        run_row={"name": name, "experiment_id": 4471},
    )


def writing_runner(content="sample_id,lane,file\nS1,L001,x.fastq.gz\n"):
    """A runner that writes the samplesheet the real tool would, then succeeds."""

    def _run(command, **kwargs):
        output_path = command[command.index("--output") + 1]
        __import__("pathlib").Path(output_path).write_text(content, encoding="utf-8")
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    return _run


# ---------------------------------------------------------------------------
# The tool adapter — command construction.
# ---------------------------------------------------------------------------


def test_the_command_runs_nvd_inside_its_pinned_pixi_environment(tmp_path):
    """``nvd`` is invoked via ``pixi run --manifest-path``, not bare from PATH.

    A bare ``nvd`` picks up whatever wrapper is installed — production hit exactly
    that, with a stale shim pinned to an older version shadowing the intended one.
    Pinning the manifest is what makes the tool version deterministic.
    """
    command = build_nvd_command({"nvd_repo_dir": "/opt/nvd"}, "samplesheet", "generate")

    assert command[:4] == ["pixi", "run", "--manifest-path", "/opt/nvd/pyproject.toml"]
    assert command[4] == "nvd"


def test_a_missing_repo_dir_fails_loudly_rather_than_guessing(tmp_path):
    """No ``nvd_repo_dir`` is a KeyError, never a fallback to a derived path.

    Errata A7/D2: production derives this from ``Path.home()``, an
    install-location coupling that silently breaks when the install moves. A
    guess here would be wrong in exactly the way that is hardest to notice.
    """
    with pytest.raises(KeyError):
        build_nvd_command({}, "samplesheet", "generate")


def test_samplesheet_generation_passes_force_and_omits_sanitize(tmp_path):
    """``--force`` is passed; ``--sanitize`` is not.

    ``--force`` skips the interactive write confirmation — the daemon has no TTY,
    so without it the prompt hangs until the run is abandoned — and it is what
    makes re-entry idempotent.

    ``--sanitize`` is omitted deliberately: the descriptive
    STATE_CITY_PLANT_DATE_S# sample ids must survive into authority-list
    registration and LabKey, and sanitising would rename them and break the join
    with everything downstream.
    """
    captured = {}

    def capturing_runner(command, **kwargs):
        captured["command"] = command
        output_path = command[command.index("--output") + 1]
        __import__("pathlib").Path(output_path).write_text("x", encoding="utf-8")
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    client = NvdToolClient(
        {"nvd_repo_dir": str(tmp_path / "nvd")}, runner=capturing_runner
    )
    client.generate_samplesheet(tmp_path / "raw_seqs", tmp_path / "samplesheet.csv")

    assert "--force" in captured["command"]
    assert "--sanitize" not in captured["command"]
    assert "illumina-lanes" in captured["command"]


def test_a_tool_that_succeeds_without_writing_anything_is_an_error(tmp_path):
    """Exit 0 with no output file raises, rather than reporting success.

    The silent-success mode this adapter must not have: a satisfied checkmark plus
    an absent samplesheet would hand an empty pipeline stage to NVD, and the real
    cause would surface much later.
    """
    client = NvdToolClient(
        {"nvd_repo_dir": str(tmp_path / "nvd")},
        runner=lambda command, **kwargs: types.SimpleNamespace(
            stdout="", stderr="", returncode=0
        ),
    )

    with pytest.raises(NvdToolError, match="no usable file"):
        client.generate_samplesheet(tmp_path / "raw_seqs", tmp_path / "out.csv")


def test_an_empty_output_file_is_also_an_error(tmp_path):
    """A zero-byte samplesheet is treated as no samplesheet."""

    def empty_writer(command, **kwargs):
        output_path = command[command.index("--output") + 1]
        __import__("pathlib").Path(output_path).write_text("", encoding="utf-8")
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    client = NvdToolClient({"nvd_repo_dir": str(tmp_path / "nvd")}, runner=empty_writer)

    with pytest.raises(NvdToolError):
        client.generate_samplesheet(tmp_path / "raw_seqs", tmp_path / "out.csv")


def test_a_failing_tool_becomes_an_nvd_tool_error_carrying_stderr(tmp_path):
    """A non-zero exit is translated, with the tool's stderr preserved.

    The stderr becomes the run's quarantine reason, so it is the only thing an
    operator has to work from.
    """

    def failing_runner(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=2, cmd=command, stderr="unrecognised platform 'illumina'"
        )

    client = NvdToolClient(
        {"nvd_repo_dir": str(tmp_path / "nvd")}, runner=failing_runner
    )

    with pytest.raises(NvdToolError, match="unrecognised platform"):
        client.generate_samplesheet(tmp_path / "raw_seqs", tmp_path / "out.csv")


def test_a_missing_pixi_binary_is_reported_as_a_deployment_problem(tmp_path):
    """``FileNotFoundError`` becomes an NvdToolError naming the likely cause."""

    def absent_binary(command, **kwargs):
        raise FileNotFoundError("pixi")

    client = NvdToolClient(
        {"nvd_repo_dir": str(tmp_path / "nvd")}, runner=absent_binary
    )

    with pytest.raises(NvdToolError, match="nvd_repo_dir"):
        client.generate_samplesheet(tmp_path / "raw_seqs", tmp_path / "out.csv")


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


def test_the_gate_writes_the_samplesheet_beside_raw_seqs(tmp_path):
    """SUCCESS writes ``samplesheet.csv`` into the workdir, not inside raw_seqs.

    raw_seqs holds delivered data; a generated artifact living among it would be
    swept up by anything treating that directory as the delivery.
    """
    context = make_context(tmp_path)
    client = NvdToolClient(context.config, runner=writing_runner())

    result = run_samplesheet_gate(context, lambda: client)

    assert result.outcome is GateOutcome.SUCCESS
    assert (context.workdir / SAMPLESHEET_FILENAME).is_file()
    assert not (context.workdir / RAW_SEQS_DIRNAME / SAMPLESHEET_FILENAME).exists()


def test_a_missing_raw_seqs_directory_gets_its_own_message(tmp_path):
    """An absent staging tree fails with a specific reason, not an opaque tool error.

    Reachable if the staging tree was moved or cleaned between gates. The fix is
    entirely different from a tool failure, so the message has to be too.
    """
    context = make_context(tmp_path, with_raw_seqs=False)

    result = run_samplesheet_gate(context, lambda: None)

    assert result.outcome is GateOutcome.FAILURE
    assert RAW_SEQS_DIRNAME in result.reason


def test_a_tool_failure_quarantines_with_the_tool_message(tmp_path):
    """An NvdToolError becomes FAILURE, naming the run and carrying the cause."""
    context = make_context(tmp_path)

    def failing_runner(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=2, cmd=command, stderr="deacon not found"
        )

    client = NvdToolClient(context.config, runner=failing_runner)

    result = run_samplesheet_gate(context, lambda: client)

    assert result.outcome is GateOutcome.FAILURE
    assert "run_A" in result.reason and "deacon not found" in result.reason


def test_regeneration_overwrites_rather_than_failing(tmp_path):
    """Re-running the gate over an existing samplesheet succeeds and replaces it.

    ``--force`` is what makes re-entry after a crash or a hold a no-op rather than
    a conflict.
    """
    context = make_context(tmp_path)
    (context.workdir / SAMPLESHEET_FILENAME).write_text("stale", encoding="utf-8")
    client = NvdToolClient(context.config, runner=writing_runner("fresh,content\n"))

    result = run_samplesheet_gate(context, lambda: client)

    assert result.outcome is GateOutcome.SUCCESS
    assert "fresh" in (context.workdir / SAMPLESHEET_FILENAME).read_text()


def test_the_gate_depends_on_checksum_not_merely_download():
    """``needs=("checksum",)`` — never build a samplesheet from unverified lanes.

    The load-bearing ordering choice. Depending only on ``download`` would let a
    corrupt transfer flow into the pipeline, where it surfaces much later and much
    more expensively. This makes "verified before used" structural.
    """
    assert SAMPLESHEET.needs == ("checksum",)
    assert SAMPLESHEET.order == 3
    assert SAMPLESHEET.satisfied_column == "samplesheet_created"
    assert dict(SAMPLESHEET.persists) == {}
