"""The NVD CLI world adapter — the boundary to the `nvd` tool.

Shared by the ``samplesheet`` gate (order 3) and the ``nvd`` gate (order 5), both of which run the
same binary through the same pixi environment. Owns the subprocess calls and the fail-closed
translation of every tool failure into ``NvdToolError``.

**Every path comes from config, never from where the code lives.** Production builds its command
from ``Path.home() / "nvd" / "pyproject.toml"`` (PP ``_nvd_cmd``), which is errata **A7/D2**: an
install-location coupling. D2 shows how it bites — a launch directory silently relocated onto
CephFS makes ``--resume`` a no-op and forces a multi-hour re-run. sluice reads
``nvd_repo_dir`` by subscript instead, so a missing key is a loud config error rather than a
silently wrong path. ``tests/test_context.py`` already forbids the ``__file__`` token in core for
the same reason; this module holds the line on the gates side.

**Why ``pixi run --manifest-path``.** The tool must execute from a specific, version-pinned
environment. Invoking a bare ``nvd`` from ``PATH`` picks up whatever wrapper happens to be
installed — production hit exactly that, with a stale ``~/.local/bin/nvd`` shim pinned to an older
version shadowing the intended one.
"""

import pathlib
import subprocess

import structlog

__all__ = [
    "NvdToolError",
    "NvdToolClient",
    "build_nvd_command",
    "make_nvd_client",
]

log = structlog.get_logger("sluice.gates.nvd_tool")


class NvdToolError(RuntimeError):
    """An ``nvd`` invocation failed, or its output was not produced.

    One error type for every tool failure, so a gate can fail closed on a single
    ``except`` and the daemon quarantines with a message naming what broke.
    """


def build_nvd_command(config, *arguments: str) -> list[str]:
    """Build the argv for one ``nvd`` invocation inside its pinned pixi environment.

    Args:
        config: the loaded config. ``nvd_repo_dir`` is read by subscript — a
            missing key must fail loudly rather than fall back to a guess about
            where the repo lives (guardrail §8, errata A7/D2). ``nvd_command``
            has a real default because "the binary is called nvd" is a fact about
            the tool, not a deployment secret.
        arguments: the subcommand and its flags.

    Returns:
        The full argv list.
    """
    manifest = pathlib.Path(config["nvd_repo_dir"]) / "pyproject.toml"
    nvd_command = config.get("nvd_command", "nvd")
    return [
        "pixi",
        "run",
        "--manifest-path",
        str(manifest),
        nvd_command,
        *arguments,
    ]


class NvdToolClient:
    """Runs ``nvd`` subcommands, translating every failure into ``NvdToolError``."""

    def __init__(self, config, runner=subprocess.run):
        """Store config and the subprocess runner seam.

        Args:
            config: the loaded config.
            runner: the ``subprocess.run``-shaped callable actually invoked — the
                seam every test drives instead of executing the real tool.
        """
        self._config = config
        self._runner = runner

    def _run(self, *arguments: str, timeout_seconds=None) -> str:
        """Invoke ``nvd`` with ``arguments``; return stdout or raise ``NvdToolError``."""
        command = build_nvd_command(self._config, *arguments)
        log.info("nvd.invoke", **{"nvd.args": " ".join(arguments)})
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as missing_binary:
            raise NvdToolError(
                f"nvd invocation failed: required binary not found "
                f"({missing_binary}) — is pixi on PATH and nvd_repo_dir correct?"
            ) from missing_binary
        except subprocess.TimeoutExpired as timed_out:
            raise NvdToolError(
                f"nvd {' '.join(arguments)} timed out after {timeout_seconds}s"
            ) from timed_out
        except subprocess.CalledProcessError as failed:
            raise NvdToolError(
                f"nvd {' '.join(arguments)} failed: {(failed.stderr or '').strip()}"
            ) from failed
        return completed.stdout or ""

    def generate_samplesheet(self, raw_seqs_dir, output_path) -> None:
        """Generate a by-lane samplesheet from ``raw_seqs_dir`` into ``output_path``.

        Delegates to NVD's own tool rather than writing a CSV here, so deacon
        consumes exactly what NVD emits and the two cannot drift apart in format
        (PP ``generate_by_lane_samplesheet``).

        Two flags carry real meaning:

        * ``--force`` skips the interactive "Write samplesheet to …? [Y/n]"
          confirmation. The daemon has no TTY, so without it the prompt hangs
          until the run is abandoned. It is also what makes the gate idempotent —
          re-entry overwrites rather than stopping to ask.
        * **No ``--sanitize``**, deliberately. The descriptive
          ``STATE_CITY_PLANT_DATE_S#`` sample ids have to survive into authority-list
          registration and LabKey; sanitising would rename them and break the
          join with everything downstream.

        Raises:
            NvdToolError: the tool failed, or exited cleanly without producing a
                non-empty samplesheet. The second check matters — a tool that
                returns 0 and writes nothing would otherwise mark the gate
                satisfied and hand an empty file to the pipeline.
        """
        output_path = pathlib.Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            "samplesheet",
            "generate",
            "--from-dir",
            str(raw_seqs_dir),
            "--platform",
            "illumina",
            "--group-by",
            "illumina-lanes",
            "--output",
            str(output_path),
            "--force",
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise NvdToolError(
                f"nvd samplesheet generate reported success but produced no "
                f"usable file at {output_path}"
            )


def make_nvd_client(config) -> NvdToolClient:
    """Build the production NVD client from config.

    The factory a gate defers behind a zero-arg callable, matching the
    ``make_experiment_client`` / ``make_transfer_client`` precedent.
    """
    return NvdToolClient(config)
