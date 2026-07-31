"""Behavioral tests for ``sluice.gates.detection`` — the production rsync lister.

Detection's world adapter (plan 11 units 4-7): a pure listing parser, an exact
``(argv, env_overlay)`` builder, and a thin ``subprocess.run`` wrapper. It is
**not** a Gate. No network and no real rsync run here — the parser is pure and
``list_remote_runs`` monkeypatches ``subprocess.run``. The pins that matter are
the exact argv (``--list-only`` + the include/exclude filters) and that the
sshpass password never touches argv, logs, events, or exception text.
"""

import ast
import inspect
import pathlib
import subprocess

import pytest

from sluice.gates import detection
from sluice.gates.detection import (
    RemoteListingError,
    build_listing_command,
    list_remote_runs,
    parse_rsync_listing,
)

# The real ``rsync --list-only -r`` shape (plan 11 unit 4): a top-level folder
# with two 001 fastqs and a manifest, a nested subdir, and a stray root file.
FIXTURE = """\
drwxr-xr-x          4,096 2026/07/20 09:15:01 .
drwxr-xr-x          4,096 2026/07/20 09:15:01 RUN_A
-rw-r--r--  1,234,567,890 2026/07/20 09:20:44 RUN_A/S1_L001_R1_001.fastq.gz
-rw-r--r--  1,234,567,890 2026/07/20 09:21:03 RUN_A/S1_L001_R2_001.fastq.gz
-rw-r--r--          1,024 2026/07/20 09:22:00 RUN_A/md5s.txt
drwxr-xr-x          4,096 2026/07/20 09:23:00 RUN_A/subdir
-rw-r--r--            512 2026/07/21 10:00:00 stray_001.fastq.gz
"""


# --------------------------------------------------------------------------
# Unit 4 — parse_rsync_listing (pure parser).
# --------------------------------------------------------------------------


def test_parse_groups_by_top_level_folder_and_computes_size_tb():
    """The fixture yields one candidate; the stray file and nested subdir vanish.

    ``RUN_A`` accumulates its two fastqs and its manifest; the root ``.``, the
    stray root fastq, and the nested ``subdir`` d-entry contribute nothing.
    """
    result = parse_rsync_listing(FIXTURE)
    assert result == [
        ("RUN_A", pytest.approx((2 * 1_234_567_890 + 1024) / 1024**4)),
    ]


@pytest.mark.parametrize(
    ("body", "included"),
    [
        # fastq only, no manifest -> excluded.
        ("-rw-r--r-- 100 d t RUN/S1_L001_R1_001.fastq.gz", False),
        # manifest only, no fastq -> excluded (stricter than the poller).
        ("-rw-r--r-- 100 d t RUN/md5s.txt", False),
        # .sha256 manifest + a 001 fastq -> included.
        (
            "-rw-r--r-- 100 d t RUN/checksums.sha256\n"
            "-rw-r--r-- 100 d t RUN/S1_L001_R1_001.fastq.gz",
            True,
        ),
        # uppercase MD5S.TXT + fastq -> included (case-insensitive).
        (
            "-rw-r--r-- 100 d t RUN/MD5S.TXT\n"
            "-rw-r--r-- 100 d t RUN/S1_L001_R1_001.fastq.gz",
            True,
        ),
        # uppercase .SHA256 + fastq -> included (case-insensitive).
        (
            "-rw-r--r-- 100 d t RUN/CHECKSUMS.SHA256\n"
            "-rw-r--r-- 100 d t RUN/S1_L001_R1_001.fastq.gz",
            True,
        ),
        # a fastq without the 001 token + manifest -> excluded.
        (
            "-rw-r--r-- 100 d t RUN/md5s.txt\n-rw-r--r-- 100 d t RUN/S1_R1.fastq.gz",
            False,
        ),
    ],
)
def test_parse_requires_manifest_and_a_001_fastq(body, included):
    """Candidacy needs both a checksum manifest AND a ``*001.fastq.gz`` file."""
    listing = "drwxr-xr-x 4096 d t RUN\n" + body
    names = [name for name, _size in parse_rsync_listing(listing)]
    assert ("RUN" in names) is included


def test_parse_survives_noise():
    """Blank lines, short lines, and a non-numeric size never raise; bad row skipped.

    The manifest+fastq folder still lands, its size is the two good rows only
    (the non-numeric row is dropped, not fatal).
    """
    listing = "\n".join(
        [
            "",
            "drwxr-xr-x 4096 d t RUN",
            "-rw-r--r-- three-fields",
            "-rw-r--r-- 1,000 d t RUN/md5s.txt",
            "-rw-r--r-- not_a_number d t RUN/S1_L001_R1_001.fastq.gz",
            "-rw-r--r-- 2,000 d t RUN/S1_L001_R2_001.fastq.gz",
        ]
    )
    result = parse_rsync_listing(listing)
    assert result == [("RUN", pytest.approx((1000 + 2000) / 1024**4))]


# --------------------------------------------------------------------------
# Unit 5 — build_listing_command (the exact argv + overlay).
# --------------------------------------------------------------------------

REMOTE_ARGV = [
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
    "seq@sequencer.example.org:.",
]

LOCAL_ARGV = [
    "rsync",
    "--list-only",
    "--include=*/",
    "--include=*001.fastq.gz",
    "--include=*md5s.txt",
    "--include=*.sha256",
    "--exclude=*",
    "-r",
    "/data/src/",
]


def _remote_config(tmp_path):
    """A remote-mode config whose password file holds ``s3cret`` (with a newline)."""
    password_file = tmp_path / "remote_password"
    password_file.write_text("s3cret\n", encoding="utf-8")
    return {
        "remote_user": "seq",
        "remote_host": "sequencer.example.org",
        "remote_source_path": ".",
        "password_file": str(password_file),
    }


def test_remote_command_is_pinned_and_password_never_on_argv(tmp_path):
    """Remote argv is byte-exact; the password rides the overlay, never argv.

    Exact-equality is the detection contract with the remote — it locks
    ``--list-only`` and the three include filters against drift; the password is
    the stripped file contents in ``SSHPASS`` only, and ``-p`` never appears.
    """
    config = _remote_config(tmp_path)
    argv, overlay = build_listing_command(config)

    assert argv == REMOTE_ARGV
    assert overlay == {"SSHPASS": "s3cret"}
    assert "s3cret" not in " ".join(argv)
    assert "-p" not in argv


def test_local_mode_command_lists_directory_contents():
    """Local mode builds the trailing-slash argv with an empty overlay.

    A truthy ``local_source_dir`` selects local mode (no ``sshpass``); an empty
    string falls through to remote mode (the example-config "or empty"
    convention) and raises for the missing remote keys.
    """
    argv, overlay = build_listing_command({"local_source_dir": "/data/src"})
    assert argv == LOCAL_ARGV
    assert overlay == {}
    assert "sshpass" not in argv

    with pytest.raises(KeyError):
        build_listing_command({"local_source_dir": ""})


@pytest.mark.parametrize(
    "missing_key",
    ["remote_user", "remote_host", "remote_source_path", "password_file"],
)
def test_missing_remote_key_fails_loud(tmp_path, missing_key):
    """Each remote key is read by subscript — deleting any one raises ``KeyError``."""
    config = _remote_config(tmp_path)
    del config[missing_key]
    with pytest.raises(KeyError, match=missing_key):
        build_listing_command(config)


# --------------------------------------------------------------------------
# Unit 6 — list_remote_runs (the thin subprocess adapter).
# --------------------------------------------------------------------------


def test_list_remote_runs_parses_subprocess_stdout(tmp_path, monkeypatch):
    """The adapter runs the pinned argv with ``SSHPASS`` in env, then parses stdout."""
    config = _remote_config(tmp_path)
    captured = {}

    def fake_run(argv, *, env, capture_output, text, check):
        captured["argv"] = argv
        captured["env"] = env
        return subprocess.CompletedProcess(argv, 0, stdout=FIXTURE, stderr="")

    monkeypatch.setattr(detection.subprocess, "run", fake_run)

    result = list_remote_runs(config)

    assert result == parse_rsync_listing(FIXTURE)
    assert captured["argv"] == REMOTE_ARGV
    assert captured["env"]["SSHPASS"] == "s3cret"


def test_list_remote_runs_wraps_failure_with_stderr_never_password(
    tmp_path, monkeypatch
):
    """A non-zero exit becomes ``RemoteListingError`` — stderr in, password/argv out.

    The wrapped message carries the operator's stderr but neither the password
    nor any argv element (``sshpass``), and chains the ``CalledProcessError`` as
    ``__cause__``.
    """
    config = _remote_config(tmp_path)

    def fake_run(argv, *, env, capture_output, text, check):
        raise subprocess.CalledProcessError(
            30, argv, stderr="Connection closed by 203.0.113.7"
        )

    monkeypatch.setattr(detection.subprocess, "run", fake_run)

    with pytest.raises(RemoteListingError) as excinfo:
        list_remote_runs(config)

    message = str(excinfo.value)
    assert "Connection closed by 203.0.113.7" in message
    assert "s3cret" not in message
    assert "sshpass" not in message
    assert isinstance(excinfo.value.__cause__, subprocess.CalledProcessError)


# --------------------------------------------------------------------------
# Unit 7 — module hygiene: list-only, core-clean, pinned surface.
# --------------------------------------------------------------------------


def test_detection_module_is_list_only_and_core_clean():
    """The adapter never transfers (no ``-avP``), imports no sluice, exports four names.

    ``-avP`` is the download gate's resume-first flag — its absence here draws
    the detection/download boundary; the module needs no ``sluice`` imports at
    all (stdlib only — the seam is a plain callable); the ``__all__`` equality
    is the surface guard (the audit-plan precedent).
    """
    source = pathlib.Path(inspect.getsourcefile(detection)).read_text(encoding="utf-8")
    assert "-avP" not in source

    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    sluice_imports = sorted(
        name for name in imported if name == "sluice" or name.startswith("sluice.")
    )
    assert sluice_imports == [], (
        f"the detection adapter needs no sluice imports at all: {sluice_imports}"
    )

    assert detection.__all__ == [
        "RemoteListingError",
        "build_listing_command",
        "list_remote_runs",
        "parse_rsync_listing",
    ]
