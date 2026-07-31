"""Production remote-run lister — a world adapter injected at composition (plan 11).

This module is a **world adapter** wired into the daemon's detection seam at the
composition root; it is **NOT a Gate**, exports no ``Gate`` object, and never
appears in any profile (gate map §3 "Not gates"). It only LISTS remote runs
(``rsync --list-only``) — downloading, partial resume, and the D4 wave ramp are
the download gate's business. It lives under ``sluice.gates`` so the
``gates -> core`` dependency arrow auto-guards it and core (which may not import
``subprocess``) stays clean; nothing wires it into a live path in this increment.

The sshpass password is handed to the child through the ``SSHPASS`` environment
variable (``sshpass -e``), never on argv, and never appears in any log line,
event, or exception message (subsystem-map security note; the argv-exposure fix
over the live poller).
"""

import os
import subprocess
from pathlib import Path

__all__ = [
    "RemoteListingError",
    "build_listing_command",
    "list_remote_runs",
    "parse_rsync_listing",
]


class RemoteListingError(RuntimeError):
    """A remote ``rsync --list-only`` invocation exited non-zero.

    Wraps the underlying ``CalledProcessError`` as ``__cause__``; the message
    carries the exit code and stderr (the operator's diagnostic) but never the
    argv or the password — this message becomes ``detection.failed``'s
    ``error.message`` via the daemon's containment (plan 11 unit 2), so any
    secret placed here would reach the events sink.
    """


def parse_rsync_listing(output: str) -> list[tuple[str, float]]:
    """Group an ``rsync --list-only -r`` dump into sized candidate run folders.

    Pure function, no logger and no I/O (plan 11 unit 4; the poller port). Each
    line splits into ``perms size date time path``; blank/short lines and the
    ``.`` marker are skipped. A top-level ``d``-entry registers a folder; a
    top-level plain file is ignored (a loose fastq at the root is not a run); a
    nested ``d``-entry is skipped (duplicate-download guard); a nested file joins
    its top-level folder with its comma-stripped size accumulated (an unparsable
    size skips that row only). A folder is a **candidate** iff it carries a
    checksum manifest (a path lowercased ending ``md5s.txt`` or ``.sha256``)
    **and** at least one file whose final path component ends ``001.fastq.gz``
    (production's own ``--include`` filter — stricter than "any file").

    Args:
        output: the raw stdout of ``rsync --list-only -r``.

    Returns:
        ``(name, size_tb)`` for every candidate folder, sorted by name;
        ``size_tb`` is the folder's accumulated bytes over ``1024**4``.
    """
    folders: dict[str, dict[str, object]] = {}

    def _folder(folder_name: str) -> dict[str, object]:
        return folders.setdefault(
            folder_name, {"bytes": 0.0, "manifest": False, "fastq": False}
        )

    for line in output.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5:
            continue
        perms, size_field, _date, _time, path = fields
        if path == ".":
            continue
        is_directory = perms.startswith("d")
        if "/" not in path:
            # Top-level entry: a directory registers a folder; a plain file at
            # the root is ignored (not a run).
            if is_directory:
                _folder(path)
            continue
        if is_directory:
            # A nested directory (a run's subdir) is skipped, not descended.
            continue
        # Nested file — attribute it to its top-level folder.
        top_level = path.split("/", 1)[0]
        folder = _folder(top_level)
        lowered_path = path.lower()
        if lowered_path.endswith("md5s.txt") or lowered_path.endswith(".sha256"):
            folder["manifest"] = True
        if path.rsplit("/", 1)[-1].endswith("001.fastq.gz"):
            folder["fastq"] = True
        try:
            folder["bytes"] += int(size_field.replace(",", ""))
        except ValueError:
            pass  # unparsable size: skip this row's accumulation only

    candidates = [
        (folder_name, info["bytes"] / 1024**4)
        for folder_name, info in folders.items()
        if info["manifest"] and info["fastq"]
    ]
    return sorted(candidates, key=lambda candidate: candidate[0])


def build_listing_command(config) -> tuple[list[str], dict[str, str]]:
    """Build the exact ``(argv, env_overlay)`` for a ``--list-only`` sweep.

    Local mode iff ``config["local_source_dir"]`` is truthy (a presence check,
    not a value default — the example config documents it as optional-or-empty):
    a plain ``rsync --list-only`` of the directory's contents (the trailing slash
    is load-bearing), empty overlay. Otherwise remote mode: ``remote_user``,
    ``remote_host``, ``remote_source_path`` and ``password_file`` are read by
    subscript (no fallback — a baked-in remote path would be a production default
    in code, C2's lesson), and the password (stripped) rides the ``SSHPASS``
    overlay for ``sshpass -e``, never on argv. Both argvs pin ``--list-only`` and
    the include/exclude filter sequence — this argv is detection's contract with
    the remote.

    Args:
        config: the loaded config; ``local_source_dir`` probed with ``get`` (the
            optional-mode switch), the four remote keys by subscript.

    Returns:
        ``(argv, env_overlay)`` — the overlay carries ``SSHPASS`` in remote mode,
        and is empty in local mode.

    Raises:
        KeyError: remote mode and ``config`` lacks ``remote_user``,
            ``remote_host``, ``remote_source_path`` or ``password_file``.
        OSError: the ``password_file`` cannot be read (remote mode).
        UnicodeDecodeError: the ``password_file`` is not UTF-8 — a world
            condition, not a config bug; it degrades through the daemon's
            fail-open detection containment.
    """
    local_source_dir = config.get("local_source_dir")
    if local_source_dir:
        argv = [
            "rsync",
            "--list-only",
            "--include=*/",
            "--include=*001.fastq.gz",
            "--include=*md5s.txt",
            "--include=*.sha256",
            "--exclude=*",
            "-r",
            local_source_dir + "/",
        ]
        return argv, {}

    remote_user = config["remote_user"]
    remote_host = config["remote_host"]
    remote_source_path = config["remote_source_path"]
    password_file = config["password_file"]
    password = Path(password_file).read_text(encoding="utf-8").strip()
    argv = [
        "sshpass",
        "-e",
        "rsync",
        "--list-only",
        "--include=*/",
        "--include=*001.fastq.gz",
        "--include=*md5s.txt",
        "--include=*.sha256",
        "--exclude=*",
        "-r",
        f"{remote_user}@{remote_host}:{remote_source_path}",
    ]
    return argv, {"SSHPASS": password}


def list_remote_runs(config) -> list[tuple[str, float | None]]:
    """List candidate runs on the remote via ``rsync --list-only``, then parse.

    The thin subprocess adapter (plan 11 unit 6): build the pinned argv/overlay,
    run rsync with the overlay merged onto ``os.environ`` (so ``SSHPASS`` reaches
    the child without the password on argv), and hand stdout to
    ``parse_rsync_listing``. A non-zero exit wraps to ``RemoteListingError`` with
    the stderr but never the argv or password. A missing ``rsync``/``sshpass``
    binary raises ``FileNotFoundError`` uncaught — the daemon's detection
    containment degrades it to a ``detection.failed`` event (fail-open, plan 11
    unit 2), it is not reclassified here.

    Args:
        config: the loaded config; see ``build_listing_command`` for the keys.

    Returns:
        ``(name, size_tb)`` candidates from the parser (the seam type carries an
        optional size; this adapter always supplies a float).

    Raises:
        RemoteListingError: rsync exited non-zero (wraps the
            ``CalledProcessError``; message carries stderr, never the password).
        FileNotFoundError: the ``rsync`` or ``sshpass`` binary is absent.
        KeyError: a required remote config key is missing (from
            ``build_listing_command``).
        OSError: the ``password_file`` cannot be read (from
            ``build_listing_command``).
        UnicodeDecodeError: the ``password_file`` is not UTF-8 (from
            ``build_listing_command``) — a world condition, not a config bug;
            it degrades through the daemon's fail-open detection containment.
    """
    argv, env_overlay = build_listing_command(config)
    try:
        completed = subprocess.run(
            argv,
            env={**os.environ, **env_overlay},
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as listing_error:
        raise RemoteListingError(
            f"remote listing failed (exit {listing_error.returncode}): "
            f"{listing_error.stderr}"
        ) from listing_error
    return parse_rsync_listing(completed.stdout)
