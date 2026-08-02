"""The ``checksum`` gate — order 2, verifies the transfer against the delivered manifest.

Ports python-poller's verification stage (PP ``poller.py:636-830``). Idempotent by nature: hashing
the files again is the whole operation, so re-entry after a crash or a hold simply re-verifies.
Touches nothing on disk and persists no token — whether the data is intact is re-derivable by
re-reading it, so migration 009 adds only the ``checksummed`` checkmark.

**Failure modes are reported distinctly, on purpose** (plan 15 §8). A checksum failure is ambiguous
in a way that matters operationally:

* ``MANIFEST_MISSING`` — no ``md5s.txt``/``*.sha256`` was delivered, or none could be read. Re-running
  the download will never fix this; the delivery itself is incomplete.
* ``HASH_MISMATCH`` — files are present but their contents disagree with the manifest. This is what
  a truncated or corrupted transfer looks like, and it is plausibly repaired by re-transferring.

Collapsing both into "checksum failed" would foreclose the deferred auto-rewind question (plan 15
§8): a policy that rewinds to ``download`` must be able to tell "re-transfer might help" from
"re-transferring is pointless", or it loops forever on a delivery that never had a manifest.
Distinguishing them costs nothing today.

Algorithm is inferred per entry from the expected digest's length — 64 hex chars is SHA-256,
otherwise MD5 (PP ``_checksum_algorithm``, ``poller.py:710``). A wrong guess fails verification
loudly rather than passing silently.
"""

import hashlib
import os
import pathlib
from typing import Any

import structlog

from sluice.core.gate import Gate, GateOutcome, GateResult, no_op_invalidate
from sluice.gates.download import RAW_SEQS_DIRNAME

__all__ = [
    "CHECKSUM",
    "HASH_MISMATCH",
    "MANIFEST_MISSING",
    "compute_digest",
    "find_manifests",
    "parse_manifests",
    "run_checksum_gate",
    "verify_directory",
]

log = structlog.get_logger("sluice.gates.checksum")

# The two distinguishable failure classes (plan 15 §8). Prefixed onto the gate's
# reason so an operator — and any future rewind policy — can tell them apart
# without parsing prose.
MANIFEST_MISSING = "MANIFEST_MISSING"
HASH_MISMATCH = "HASH_MISMATCH"

# Streamed rather than read whole: these are multi-GB FASTQs (PP :735).
_CHUNK_BYTES = 8 * 1024 * 1024

# Only the delivered lane files are verified. Manifests routinely also list
# trimmed/derived files that were never transferred, and treating those as
# missing would fail every healthy run (PP ``parse_md5_files``, :688).
_VERIFIED_SUFFIX = "001.fastq.gz"


def find_manifests(directory) -> list[pathlib.Path]:
    """Return every checksum manifest under ``directory``.

    Walks rather than globs: the SHA-256 manifests ship as hidden dotfiles (e.g.
    ``.verified-copy.sha256``) and ``pathlib.glob("*")`` skips leading-dot names,
    so a glob would silently find nothing and report a healthy run as unverifiable
    (PP ``find_md5_files``, ``poller.py:642``).
    """
    directory = pathlib.Path(directory)
    if not directory.is_dir():
        return []
    manifests = []
    for root, _dirs, filenames in os.walk(directory):
        for filename in filenames:
            if filename.endswith("md5s.txt") or filename.endswith(".sha256"):
                manifests.append(pathlib.Path(root) / filename)
    return sorted(manifests)


def parse_manifests(manifest_paths) -> dict[str, str]:
    """Read manifests into ``{filename: expected_digest}``, keeping only lane files.

    Manifest lines are ``<digest>  <filename>`` or ``<digest> *<filename>`` (the
    ``*`` marks binary mode). Entries not naming a ``001.fastq.gz`` are skipped:
    manifests commonly also list trimmed or derived files that were never part of
    the delivery, and verifying those would fail every healthy run.

    An unreadable manifest is skipped with an error line rather than raising — the
    caller decides what an empty result means, and it has more context to decide
    with.
    """
    expected: dict[str, str] = {}
    for manifest_path in manifest_paths:
        try:
            content = pathlib.Path(manifest_path).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as read_error:
            log.error(
                "checksum.manifest_unreadable",
                **{
                    "checksum.manifest": str(manifest_path),
                    "error.type": type(read_error).__name__,
                },
            )
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line or _VERIFIED_SUFFIX not in line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            digest, filename = parts
            # Basename only: manifests are written relative to the delivery root,
            # while the transfer flattens every file into one directory.
            expected[pathlib.PurePath(filename.lstrip("*").strip()).name] = digest
    return expected


def compute_digest(file_path, algorithm: str) -> str:
    """Return ``file_path``'s lowercase hex digest, streamed in 8 MB chunks."""
    hasher = hashlib.new(algorithm)
    with open(file_path, "rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest()


def _algorithm_for(expected_digest: str) -> str:
    """Infer the hash algorithm from the expected digest's length.

    64 hex characters is SHA-256; anything else is treated as MD5 (PP
    ``_checksum_algorithm``, ``poller.py:710``). A wrong inference produces a
    mismatch, which fails loudly — never a silent pass.
    """
    return "sha256" if len(expected_digest.strip()) == 64 else "md5"


def verify_directory(directory, expected: dict[str, str]) -> list[str]:
    """Verify each expected file in ``directory``. Returns human-readable problems.

    A file named in the manifest but absent from disk is a problem, as is one whose
    digest disagrees. An empty return means every expected file is present and
    intact.
    """
    directory = pathlib.Path(directory)
    problems: list[str] = []
    for filename, expected_digest in sorted(expected.items()):
        candidate = directory / filename
        if not candidate.is_file():
            problems.append(f"{filename}: named in the manifest but not on disk")
            continue
        actual_digest = compute_digest(candidate, _algorithm_for(expected_digest))
        if actual_digest.lower() != expected_digest.strip().lower():
            problems.append(
                f"{filename}: digest mismatch "
                f"(expected {expected_digest.strip().lower()}, got {actual_digest})"
            )
    return problems


def run_checksum_gate(context: Any) -> GateResult:
    """Verify the downloaded lanes against their delivered manifest.

    No world adapter and no seam: this gate reads local files only, so there is
    nothing to inject and nothing to fail closed against. Its inputs are the
    ``raw_seqs`` tree the download gate produced.

    Returns FAILURE with a ``MANIFEST_MISSING``- or ``HASH_MISMATCH``-prefixed
    reason (plan 15 §8), or SUCCESS carrying the verified file count.
    """
    run_name = context.run_row["name"]
    raw_seqs_dir = pathlib.Path(context.workdir) / RAW_SEQS_DIRNAME

    manifests = find_manifests(raw_seqs_dir)
    if not manifests:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"{MANIFEST_MISSING}: checksum gate found no md5s.txt or *.sha256 "
                f"under {raw_seqs_dir} for run {run_name!r} — the delivery carries "
                "no manifest to verify against, which re-downloading will not fix"
            ),
        )

    expected = parse_manifests(manifests)
    if not expected:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"{MANIFEST_MISSING}: checksum gate read "
                f"{len(manifests)} manifest(s) for run {run_name!r} but found no "
                f"{_VERIFIED_SUFFIX} entries — the manifest is empty, unreadable, "
                "or in an unexpected format"
            ),
        )

    problems = verify_directory(raw_seqs_dir, expected)
    if problems:
        shown = "; ".join(problems[:3])
        more = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"{HASH_MISMATCH}: checksum gate found {len(problems)} of "
                f"{len(expected)} file(s) missing or corrupt for run {run_name!r}: "
                f"{shown}{more}"
            ),
        )

    return GateResult(GateOutcome.SUCCESS, metadata={"verified_files": len(expected)})


CHECKSUM = Gate(
    id="checksum",
    name="Verify checksums",
    satisfied_column="checksummed",  # migration 009
    order=2,  # TRUE execution order — after download
    needs=("download",),  # nothing to verify until the lanes are present
    produces=(),  # read-only: verification writes nothing
    run=run_checksum_gate,
    # Verification only reads: there is no artifact to undo. Clearing the
    # ``checksummed`` column — what the operator's ``reset_gate`` does — is the
    # entire rewind.
    invalidate=no_op_invalidate,
    terminate=None,
    persists={},  # intactness is re-derivable — no token worth a column
)
"""The ``checksum`` gate — Plan B gate 2.

``produces=()`` is the honest statement that this gate creates nothing: it is a pure verification
step whose only output is the checkmark itself.
"""
